"""In-process test of the batch save-all-to-Gmail route and run-panel template."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import app
from app.models import CompanyState, CompanyStatus, RunState, RunPhase
from app import run_store
from app.run_manager import manager

RUNS = manager.runs

# Synthetic finished run for the real on-disk candidate (has a Gmail token,
# but we won't hit Gmail here — see fake-candidate case below for the route).
run = RunState(id="testrun00001", candidate_id="516e7c4751", phase=RunPhase.DONE)
for i, saved in enumerate([False, False, True]):
    c = CompanyState(name=f"TestCo{i}", domain=f"testco{i}.com", status=CompanyStatus.DONE)
    c.email = f"contact@testco{i}.com"
    c.draft_subject = f"Subject {i}"
    c.draft_body = f"Body {i}"
    c.gmail_draft_created = saved
    from app.models import Contact
    c.contact_used = Contact(first_name="Test", last_name=f"Person{i}", title="VP", evidence="ev")
    run.companies.append(c)
RUNS[run.id] = run

client = TestClient(app)

try:
    # 1. Panel shows the batch button with the right count (2 unsaved of 3 done)
    html = client.get(f"/runs/{run.id}/panel").text
    assert "Save all 2 remaining drafts to Gmail" in html, "batch button missing"
    assert f"/runs/{run.id}/save_all_to_gmail" in html
    print("PASS: batch button rendered with count 2")

    # 2. With only 1 unsaved, button hides (per-draft button already covers it)
    run.companies[0].gmail_draft_created = True
    html = client.get(f"/runs/{run.id}/panel").text
    assert "remaining drafts to Gmail" not in html, "button should hide at 1 unsaved"
    print("PASS: button hidden when only 1 unsaved draft")
    run.companies[0].gmail_draft_created = False

    # 3. Route: candidate with NO gmail token -> per-company errors recorded,
    #    saved one untouched, redirect back to the run page.
    run2 = RunState(id="testrun00002", candidate_id="516e7c4751", phase=RunPhase.DONE)
    run2.companies = run.companies
    # point at a candidate id with no token by temporarily faking is_connected? No —
    # simplest: use a run whose candidate exists but token path won't matter; instead
    # monkeypatch create_draft to fail for one company and succeed for the other.
    import app.main as main_mod

    calls = []
    async def fake_create_draft(candidate, company):
        calls.append(company.name)
        if company.name == "TestCo1":
            raise RuntimeError("simulated Gmail failure")
    main_mod.gmail_client.create_draft = fake_create_draft

    resp = client.post(f"/runs/{run.id}/save_all_to_gmail", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/runs/{run.id}"
    assert calls == ["TestCo0", "TestCo1"], f"unexpected calls: {calls}"  # TestCo2 already saved -> skipped
    assert run.companies[0].gmail_draft_created is True
    assert run.companies[1].gmail_draft_created is False
    assert "simulated Gmail failure" in run.companies[1].gmail_error
    print("PASS: batch route saves unsaved drafts, skips saved, records per-draft errors")

    # 4. Panel now shows the error next to the failed draft and no batch button (1 left)
    html = client.get(f"/runs/{run.id}/panel").text
    assert "simulated Gmail failure" in html
    assert "remaining drafts to Gmail" not in html
    print("PASS: panel reflects post-batch state")
finally:
    # The batch route persists the run via manager.save — remove the throwaway
    # run from the in-memory registry AND the on-disk DB.
    for rid in ("testrun00001", "testrun00002"):
        RUNS.pop(rid, None)
        run_store.delete_run(rid)
