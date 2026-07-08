"""Run persistence — one JSON file per run under data/runs/.

Runs used to live only in memory, so a server restart (reload, laptop shut)
lost everything including finished drafts. Now the run state is written at
checkpoints (discovery done, pipeline start, each company finishing, run end,
Gmail-save flags) and loaded back on startup.

What a restart can and can't preserve: the asyncio tasks doing the actual
LLM work die with the process, so a run caught mid-DISCOVERING or mid-RUNNING
can't resume computing. On load those are settled to a truthful final state
(see _settle_interrupted): finished companies keep their drafts, in-flight
ones are marked dropped. A run parked at the REVIEW gate survives fully —
its discovery list is on disk, and approving it starts the pipeline fresh.
"""

import json
import logging
from typing import Dict

logger = logging.getLogger("app.run_store")

from .config import DATA_DIR, MAX_RUNS_PER_DAY
from .fsutil import atomic_write_text
from .models import CompanyStatus, RunPhase, RunState

RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

# Keep this many most-recent runs per candidate; older files are pruned when
# a new run starts, so the reports you might still want stay reachable
# without data/runs/ growing forever. Never below the daily run cap — the
# cap is enforced by counting persisted runs, so pruning under it would
# silently disable the cost guardrail.
KEEP_RUNS_PER_CANDIDATE = max(5, MAX_RUNS_PER_DAY)


def _path(run_id: str):
    return RUNS_DIR / f"{run_id}.json"


def save_run(run: RunState) -> None:
    atomic_write_text(_path(run.id), json.dumps(run.to_dict(), ensure_ascii=False))


def delete_run(run_id: str) -> None:
    p = _path(run_id)
    if p.exists():
        p.unlink()


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
    for f in sorted(RUNS_DIR.glob("*.json")):
        try:
            run = RunState.from_dict(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable run file %s", f.name, exc_info=True)
            continue  # corrupt file — skip rather than fail startup
        if _settle_interrupted(run):
            save_run(run)
        runs[run.id] = run
    return runs


def prune_candidate_runs(candidate_id: str, runs: Dict[str, RunState],
                         keep: int = KEEP_RUNS_PER_CANDIDATE) -> None:
    """Drop the oldest runs beyond `keep` for this candidate (memory + disk)."""
    mine = sorted(
        (r for r in runs.values() if r.candidate_id == candidate_id),
        key=lambda r: r.created_at,
        reverse=True,
    )
    for old in mine[keep:]:
        runs.pop(old.id, None)
        delete_run(old.id)


def delete_candidate_runs(candidate_id: str, runs: Dict[str, RunState]) -> None:
    """Remove every run belonging to a deleted profile (memory + disk)."""
    for run_id in [rid for rid, r in runs.items() if r.candidate_id == candidate_id]:
        runs.pop(run_id, None)
        delete_run(run_id)
