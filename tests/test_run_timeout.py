"""Exercise run_pipeline's stop-button and hard-timeout cutoff paths without
waiting the real 10/30 minutes -- monkeypatch RUN_HARD_TIMEOUT_SECONDS low and
make _run_company simulate slow (never-finishing-in-time) companies."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
from app import config, run_store
from app.models import Candidate, CompanyState, CompanyStatus, RunPhase, RunState

config.settings.RUN_LAUNCH_JITTER_SECONDS = 0  # tests use sub-second timeouts


def make_candidate():
    return Candidate(
        id="testcand01", name="Test", email="t@example.com", current_employer="X",
        resume_text="resume", resume_filename="r.docx",
    )


async def slow_company_ok(candidate, company, *_a, **_kw):
    # Finishes quickly -- should survive both cutoff scenarios.
    company.status = CompanyStatus.DONE
    company.draft_subject = "hi"
    company.draft_body = "body"


async def slow_company_never(candidate, company, *_a, **_kw):
    company.status = CompanyStatus.RESEARCH
    company.activity = "researching forever"
    await asyncio.sleep(3600)  # cancelled before this returns
    company.status = CompanyStatus.DONE  # unreachable in the test


async def test_hard_timeout():
    config.settings.RUN_HARD_TIMEOUT_SECONDS = 0.3
    pipeline._run_company = lambda c, comp, *a, **kw: (
        slow_company_ok(c, comp) if comp.name == "FastCo" else slow_company_never(c, comp)
    )
    # active_entries/active_blocked_emails hit the real sent_list module -- use
    # a candidate id with no CSV so they return empty without touching disk state.
    run = RunState(id="r1", candidate_id="testcand01")
    run.discovered = [
        {"name": "FastCo", "domain": "fastco.com", "reason": ""},
        {"name": "SlowCo", "domain": "slowco.com", "reason": ""},
    ]
    candidate = make_candidate()

    start = asyncio.get_event_loop().time()
    await pipeline.run_pipeline(run, candidate, ["fastco.com", "slowco.com"])
    elapsed = asyncio.get_event_loop().time() - start

    assert run.phase == RunPhase.DONE, run.phase
    fast = next(c for c in run.companies if c.name == "FastCo")
    slow = next(c for c in run.companies if c.name == "SlowCo")
    assert fast.status == CompanyStatus.DONE, fast.status
    assert slow.status == CompanyStatus.DROPPED, slow.status
    assert slow.drop_reason == "run stopped early (timeout)", slow.drop_reason
    assert elapsed < 5, f"took too long: {elapsed}s (should cut off near 0.3s)"
    print(f"PASS: hard timeout cuts off slow company, keeps fast one done ({elapsed:.2f}s)")


async def test_stop_button():
    config.settings.RUN_HARD_TIMEOUT_SECONDS = 3600  # long -- stop_event must win the race
    pipeline._run_company = lambda c, comp, *a, **kw: (
        slow_company_ok(c, comp) if comp.name == "FastCo" else slow_company_never(c, comp)
    )
    run = RunState(id="r2", candidate_id="testcand01")
    run.discovered = [
        {"name": "FastCo", "domain": "fastco.com", "reason": ""},
        {"name": "SlowCo", "domain": "slowco.com", "reason": ""},
    ]
    candidate = make_candidate()

    async def press_button_soon():
        await asyncio.sleep(0.2)
        run.stop_event.set()

    start = asyncio.get_event_loop().time()
    await asyncio.gather(
        pipeline.run_pipeline(run, candidate, ["fastco.com", "slowco.com"]),
        press_button_soon(),
    )
    elapsed = asyncio.get_event_loop().time() - start

    assert run.phase == RunPhase.DONE, run.phase
    slow = next(c for c in run.companies if c.name == "SlowCo")
    assert slow.status == CompanyStatus.DROPPED, slow.status
    assert elapsed < 5, f"took too long: {elapsed}s (should cut off near 0.2s)"
    print(f"PASS: stop button cuts the run short before the hard timeout ({elapsed:.2f}s)")


try:
    asyncio.run(test_hard_timeout())
    asyncio.run(test_stop_button())
finally:
    # run_pipeline checkpoints the throwaway runs to the DB — remove them.
    for rid in ("r1", "r2"):
        run_store.delete_run(rid)
