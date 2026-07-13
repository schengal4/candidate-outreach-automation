"""Run persistence, backed by SQLite (see app/db.py).

Run state is written at checkpoints (discovery done, pipeline start, each
company finishing, run end, Gmail-save flags) and loaded back on startup by
the RunManager (see app/run_manager.py).

What a restart can and can't preserve: the asyncio tasks doing the actual
LLM work die with the process, so a run caught mid-DISCOVERING or mid-RUNNING
can't resume computing. On load those are settled to a truthful final state
(see _settle_interrupted): finished companies keep their drafts, in-flight
ones are marked dropped. A run parked at the REVIEW gate survives fully —
its discovery list is on disk, and approving it starts the pipeline fresh.

The run_ledger table records every run ever started and is never pruned:
the MAX_RUNS_PER_DAY cost guardrail counts it via runs_started_since(), so
report retention (KEEP_RUNS_PER_CANDIDATE) and cost capping are independent
knobs — pruning old reports can't quietly disable the guardrail, which was
a fragile cross-module invariant in the file-per-run era.
"""

import json
import logging
from typing import Dict

from . import db
from .models import CompanyStatus, RunPhase, RunState

logger = logging.getLogger("app.run_store")

# Keep this many most-recent run reports per candidate; older rows are pruned
# when a new run starts, so the reports you might still want stay reachable
# without the runs table growing forever.
KEEP_RUNS_PER_CANDIDATE = 5


def save_run(run: RunState) -> None:
    db.execute(
        "INSERT OR REPLACE INTO runs (id, candidate_id, created_at, data)"
        " VALUES (?, ?, ?, ?)",
        (
            run.id,
            run.candidate_id,
            run.created_at,
            json.dumps(run.to_dict(), ensure_ascii=False),
        ),
    )


def delete_run(run_id: str) -> None:
    db.execute("DELETE FROM runs WHERE id = ?", (run_id,))


def record_run_started(candidate_id: str, created_at: float) -> None:
    """Add a run to the never-pruned cost-cap ledger."""
    db.execute(
        "INSERT INTO run_ledger (candidate_id, created_at) VALUES (?, ?)",
        (candidate_id, created_at),
    )


def runs_started_since(candidate_id: str, since_ts: float) -> int:
    """How many runs this candidate has started since `since_ts` — counted
    from the ledger, so pruned reports still count."""
    rows = db.query(
        "SELECT COUNT(*) AS n FROM run_ledger WHERE candidate_id = ? AND created_at > ?",
        (candidate_id, since_ts),
    )
    return int(rows[0]["n"])


def _settle_interrupted(run: RunState) -> bool:
    """Bring a run that died mid-flight to a truthful final state. Returns
    True if anything changed (caller persists the settled state)."""
    if run.phase == RunPhase.DISCOVERING:
        run.phase = RunPhase.ERROR
        run.error = "Interrupted by a server restart during discovery — start a new run."
        run.activity = ""
        return True
    if run.phase == RunPhase.RUNNING:
        for c in run.companies:
            if c.status not in (CompanyStatus.DONE, CompanyStatus.DROPPED):
                c.status = CompanyStatus.DROPPED
                c.drop_reason = "interrupted by server restart"
                c.activity = ""
        run.phase = RunPhase.DONE
        return True
    return False


def load_all_runs() -> Dict[str, RunState]:
    """All persisted runs, with interrupted ones settled (and re-saved)."""
    runs: Dict[str, RunState] = {}
    for row in db.query("SELECT id, data FROM runs ORDER BY created_at"):
        try:
            run = RunState.from_dict(json.loads(row["data"]))
        except Exception:
            logger.warning("Skipping unreadable run row %s", row["id"], exc_info=True)
            continue  # corrupt row — skip rather than fail startup
        if _settle_interrupted(run):
            save_run(run)
        runs[run.id] = run
    return runs


def prune_candidate_runs(candidate_id: str, runs: Dict[str, RunState],
                         keep: int = KEEP_RUNS_PER_CANDIDATE) -> None:
    """Drop the oldest run reports beyond `keep` for this candidate (memory +
    DB). The cost-cap ledger is untouched — see runs_started_since()."""
    mine = sorted(
        (r for r in runs.values() if r.candidate_id == candidate_id),
        key=lambda r: r.created_at,
        reverse=True,
    )
    for old in mine[keep:]:
        runs.pop(old.id, None)
        delete_run(old.id)


def delete_candidate_runs(candidate_id: str, runs: Dict[str, RunState]) -> None:
    """Remove every run belonging to a deleted profile (memory + DB)."""
    for run_id in [rid for rid, r in runs.items() if r.candidate_id == candidate_id]:
        runs.pop(run_id, None)
        delete_run(run_id)
    db.execute("DELETE FROM runs WHERE candidate_id = ?", (candidate_id,))
    db.execute("DELETE FROM run_ledger WHERE candidate_id = ?", (candidate_id,))
