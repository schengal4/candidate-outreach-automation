"""Previously-failed companies resurface at the review gate flagged with the
failure reason and unchecked, so retrying is an explicit user choice. Only
COMPANY-SPECIFIC failures count (contacts couldn't be found/verified) —
transient drops (LLM timeouts, parse errors, run cutoffs) are not flagged,
since a retry usually just works.
Runs in open mode (REQUIRE_LOGIN=0) — renders the review panel via the route."""
import asyncio
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import run_store
from app.models import Candidate, CompanyState, CompanyStatus, RunPhase, RunState
from app.run_manager import manager

RUNS = manager.runs

cand = Candidate(
    id="testfail001", name="T", email="", current_employer="X", resume_text="r",
)

# A past run: one company failed for a real company-specific reason, one was
# only cut off by the stop button, and one dropped on a transient LLM error —
# only the first is the company's fault and may be held against it.
past = RunState(id="testfailpast1", candidate_id=cand.id, phase=RunPhase.DONE)
bad = CompanyState(name="BadCo", domain="badco.com", status=CompanyStatus.DROPPED)
bad.drop_reason = "employment could not be verified for any contact"
cut = CompanyState(name="CutCo", domain="cutco.com", status=CompanyStatus.DROPPED)
cut.drop_reason = "run stopped early (stopped by user)"
err = CompanyState(name="ErrCo", domain="errco.com", status=CompanyStatus.DROPPED)
err.drop_reason = "error: LLM call timed out after 8 minutes (research:ErrCo)."
past.companies = [bad, cut, err]
RUNS[past.id] = past

run = RunState(id="testfailrun1", candidate_id=cand.id)
RUNS[run.id] = run


async def fake_discover(candidate, count, on_progress=None, excluded_domains=None):
    return [
        {"name": "BadCo", "domain": "badco.com", "reason": "fit"},
        {"name": "CutCo", "domain": "cutco.com", "reason": "fit"},
        {"name": "ErrCo", "domain": "errco.com", "reason": "fit"},
        {"name": "NewCo", "domain": "newco.com", "reason": "fit"},
    ]


real_discover = steps.discover_companies
steps.discover_companies = fake_discover
try:
    # The RunManager passes the candidate's other runs; here we pass them
    # directly, the way manager.start_run does.
    asyncio.run(pipeline.run_discovery(run, cand, previous_runs=[past]))
    assert run.phase == RunPhase.REVIEW
    by_domain = {c["domain"]: c for c in run.discovered}
    assert by_domain["badco.com"]["failed_before"] == "employment could not be verified for any contact"
    assert "failed_before" not in by_domain["cutco.com"]
    assert "failed_before" not in by_domain["errco.com"], "transient LLM errors must not flag"
    assert "failed_before" not in by_domain["newco.com"]
    print("PASS: contact failures annotated; stop-early, transient-error, and new companies aren't")
finally:
    steps.discover_companies = real_discover
    for rid in (past.id, run.id):
        run_store.delete_run(rid)
        RUNS.pop(rid, None)

# Review-gate rendering: failed company unchecked + reason shown, new one checked
from fastapi.testclient import TestClient
from app.main import app as fastapi_app

run2 = RunState(id="testfailrun2", candidate_id="516e7c4751", phase=RunPhase.REVIEW)
run2.discovered = [
    {"name": "BadCo", "domain": "badco.com", "reason": "fit",
     "failed_before": "no valid email found"},
    {"name": "NewCo", "domain": "newco.com", "reason": "fit"},
]
RUNS[run2.id] = run2
try:
    html = TestClient(fastapi_app).get(f"/runs/{run2.id}/panel").text
    assert "failed last time" in html and "no valid email found" in html
    bad_cb = re.search(r'<input[^>]*value="badco\.com"[^>]*>', html).group(0)
    new_cb = re.search(r'<input[^>]*value="newco\.com"[^>]*>', html).group(0)
    assert "checked" not in bad_cb, f"failed-before company should start unchecked: {bad_cb}"
    assert "checked" in new_cb, f"fresh company should start checked: {new_cb}"
    print("PASS: review gate unchecks failed-before companies and shows the reason")
finally:
    RUNS.pop(run2.id, None)
