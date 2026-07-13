"""Single owner of the run lifecycle.

Previously the run registry was a module global in pipeline.py, seeded at
import; main.py mutated it, enforced the daily cap against it, and kept its
own background-task set; run_store pruned it. Nobody owned the invariants.
The RunManager owns all of it: the in-memory registry, task spawning (and
keeping task references so they aren't garbage collected mid-run), phase
transitions triggered by the UI, the daily cost cap, pruning, and deletion.
The pipeline module stays a pure orchestrator that receives everything it
needs as arguments.

`manager` at the bottom is the process-wide instance. Its registry starts
empty; app startup calls load_persisted() (see main.py's lifespan) to seed
it from the database.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from . import config, pipeline, run_store
from .models import Candidate, RunPhase, RunState

logger = logging.getLogger("app.run_manager")


class RunManager:
    def __init__(self) -> None:
        self.runs: Dict[str, RunState] = {}
        # Keep references to background tasks so they aren't garbage
        # collected mid-run.
        self._tasks: set = set()

    # ---- registry ----
    def load_persisted(self) -> None:
        """Seed the registry from the database (interrupted runs settled)."""
        self.runs.update(run_store.load_all_runs())

    def get(self, run_id: str) -> Optional[RunState]:
        return self.runs.get(run_id)

    def for_candidate(self, candidate_id: str) -> List[RunState]:
        """This candidate's runs, newest first."""
        return sorted(
            (r for r in self.runs.values() if r.candidate_id == candidate_id),
            key=lambda r: r.created_at,
            reverse=True,
        )

    def save(self, run: RunState) -> None:
        """Persist a run mutated outside the pipeline (e.g. Gmail-save marks)."""
        run_store.save_run(run)

    # ---- lifecycle ----
    def daily_cap_reached(self, candidate_id: str) -> bool:
        """Cost guardrail: every run spends real money on the app owner's API
        keys, so cap runs per profile per rolling 24h. Counted from the
        never-pruned run ledger (see app/run_store.py)."""
        started = run_store.runs_started_since(candidate_id, time.time() - 24 * 3600)
        return started >= config.settings.MAX_RUNS_PER_DAY

    def start_run(self, candidate: Candidate) -> RunState:
        """Create, register, persist, and ledger a new run, prune old
        reports, and kick off discovery in the background."""
        run = RunState(id=RunState.new_id(), candidate_id=candidate.id)
        self.runs[run.id] = run
        run_store.save_run(run)
        run_store.record_run_started(candidate.id, run.created_at)
        # Starting a new run is when old ones age out (keep the most recent few).
        run_store.prune_candidate_runs(candidate.id, self.runs)
        previous = [r for r in self.runs.values()
                    if r.candidate_id == candidate.id and r.id != run.id]
        self._spawn(pipeline.run_discovery(run, candidate, previous_runs=previous))
        return run

    def approve(self, run: RunState, candidate: Candidate, approved_domains: List[str]) -> None:
        """Start the pipeline for the review gate's approved companies."""
        self._spawn(pipeline.run_pipeline(run, candidate, approved_domains))
        run.phase = RunPhase.RUNNING  # flip immediately so the page starts polling

    def stop_early(self, run: RunState) -> None:
        """The "retrieve what's done" button — cuts a running pipeline short
        at the next check, keeping whatever already finished."""
        if run.phase == RunPhase.RUNNING:
            logger.info("Run %s: stop-early requested", run.id)
            run.stop_event.set()

    def delete_candidate_runs(self, candidate_id: str) -> None:
        run_store.delete_candidate_runs(candidate_id, self.runs)

    # ---- background tasks ----
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            # Pipeline code catches its own errors into run state; anything
            # that reaches here would otherwise disappear without a trace.
            logger.error("Background task %s failed", task.get_name(), exc_info=exc)


manager = RunManager()
