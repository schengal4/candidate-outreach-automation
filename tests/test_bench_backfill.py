"""Bench/backfill: dropped companies are replaced from approved extras, unused
bench companies vanish from the report, contact calls get the tighter
search/time budget, and dropped-company contacts are salvaged in the report.
Runs in open mode (REQUIRE_LOGIN=0) — uses the run-panel route directly."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import config, run_store
from app.models import Candidate, CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.run_manager import manager

RUNS = manager.runs
config.settings.RUN_LAUNCH_JITTER_SECONDS = 0  # deterministic slot order

cand = Candidate(
    id="testbench01", name="T", email="", current_employer="X",
    resume_text="r", max_companies=3,
)

# 1+2. Five approved, target 3: the drop (b) is backfilled by d; e never runs
run = RunState(id="testbenchrun1", candidate_id=cand.id)
run.discovered = [{"name": n.upper(), "domain": f"{n}.com", "reason": ""} for n in "abcde"]
outcomes = {
    "a.com": CompanyStatus.DONE, "b.com": CompanyStatus.DROPPED,
    "c.com": CompanyStatus.DONE, "d.com": CompanyStatus.DONE,
    "e.com": CompanyStatus.DONE,
}
attempted = []

async def fake_run_company(candidate, company, *a, **kw):
    attempted.append(company.domain)
    company.status = outcomes[company.domain]
    if company.status == CompanyStatus.DROPPED:
        company.drop_reason = "no valid email found"

real_run_company = pipeline._run_company
pipeline._run_company = fake_run_company
try:
    asyncio.run(pipeline.run_pipeline(run, cand, [c["domain"] for c in run.discovered]))
    assert run.phase == RunPhase.DONE
    assert attempted == ["a.com", "b.com", "c.com", "d.com"], attempted
    print("PASS: drop backfilled from the bench; unneeded bench company never started")

    assert [c.domain for c in run.companies] == ["a.com", "b.com", "c.com", "d.com"]
    s = run.summary()
    assert s["drafts"] == 3 and s["attempted"] == 4
    assert run.approved_count == 5 and s["excluded_at_review"] == 0
    print("PASS: report counts attempts only; approved_count keeps review math honest")
finally:
    pipeline._run_company = real_run_company
    run_store.delete_run(run.id)

# 3. Contact calls carry the tighter search budget and timeout
captured = {}

async def fake_ask_json(system, user, **kw):
    captured.update(kw)
    return {"first_name": "A", "last_name": "B", "title": "VP",
            "linkedin_url": "", "employment_verified": True, "evidence": "e"}

real_ask_json = steps.ask_json
steps.ask_json = fake_ask_json
try:
    asyncio.run(steps.identify_contact(cand, "X", "x.com", []))
    assert captured["max_uses"] == config.settings.CONTACT_WEB_SEARCH_MAX_USES
    assert captured["timeout_seconds"] == config.settings.CONTACT_CALL_TIMEOUT_SECONDS
    print("PASS: contact calls use the contact-specific search budget and timeout")
finally:
    steps.ask_json = real_ask_json

# 4. Run report salvages a found contact on a dropped company
from fastapi.testclient import TestClient
from app.main import app as fastapi_app

run2 = RunState(id="testbenchrun2", candidate_id="516e7c4751", phase=RunPhase.DONE)
comp = CompanyState(name="SalvageCo", domain="salvage.com", status=CompanyStatus.DROPPED)
comp.drop_reason = "no valid email found"
comp.primary = Contact(
    first_name="Jane", last_name="Doe", title="CTO",
    linkedin_url="https://linkedin.com/in/janedoe", employment_verified=True,
)
# No profile URL (e.g. dropped as unsourced) -> the report falls back to a
# LinkedIn people-search link instead of showing nothing.
comp.backup = Contact(first_name="Pat", last_name="Lee", title="VP", employment_verified=True)
run2.companies = [comp]
RUNS[run2.id] = run2
try:
    html = TestClient(fastapi_app).get(f"/runs/{run2.id}/panel").text
    assert "Jane Doe" in html and "linkedin.com/in/janedoe" in html
    assert "employment verified" in html and "reach out on LinkedIn" in html
    assert "search LinkedIn for them" in html
    assert "linkedin.com/search/results/people/?keywords=Pat%20Lee%20SalvageCo" in html
    assert "verify role and profile yourself" in html  # not-fact-checked disclaimer
    print("PASS: dropped company's contacts are salvaged, with a search-link fallback")
finally:
    RUNS.pop(run2.id, None)
