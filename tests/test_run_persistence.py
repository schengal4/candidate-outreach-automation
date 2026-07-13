"""Run persistence: serialization round-trip, interrupted-run settling on
load, review-gate survival across a simulated restart (via real routes),
mid-run checkpoints, pruning, and profile-delete cleanup.
All runs use throwaway ids and are removed at the end."""
import asyncio
import sys
import time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
import app.pipeline as pipeline
import app.run_store as run_store
from app import config, storage
from app.models import Candidate, CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.run_manager import manager

RUNS = manager.runs

EMAIL = "persist.test@example.com"
CID = "persisttest1"
TEST_IDS = []


def make_run(rid, phase, created_at=None):
    run = RunState(id=rid, candidate_id=CID, phase=phase)
    if created_at:
        run.created_at = created_at
    TEST_IDS.append(rid)
    return run


storage.save_candidate(Candidate(id=CID, name="Persist Test", email="", current_employer="X",
                                 resume_text="r", owner_email=EMAIL))
try:
    # --- 1. Round-trip: full run with contacts and drafts survives the DB ---
    run = make_run("ptroundtrip1", RunPhase.DONE)
    co = CompanyState(name="RoundCo", domain="roundco.com", status=CompanyStatus.DONE)
    co.contact_used = Contact(first_name="Ann", last_name="Lee", title="VP",
                              employment_verified=True, evidence="dated post")
    co.email = "ann@roundco.com"
    co.email_score = 93
    co.research_items = ["item1", "item2"]
    co.draft_subject = "Hello"
    co.draft_body = "Body\n\nwith paragraphs"
    co.gmail_draft_created = True
    run.companies = [co]
    run.discovered = [{"name": "RoundCo", "domain": "roundco.com", "reason": "fit"}]
    run_store.save_run(run)

    loaded = run_store.load_all_runs()[run.id]
    assert loaded.to_dict() == run.to_dict(), "round-trip must be lossless"
    assert loaded.companies[0].contact_used.full_name == "Ann Lee"
    assert not loaded.stop_event.is_set() and loaded.stop_event is not run.stop_event
    print("PASS: full run (contacts, drafts, gmail flags) round-trips losslessly; fresh stop_event")

    # --- 2. Interrupted runs are settled truthfully on load ---
    r_disc = make_run("ptdisc00001", RunPhase.DISCOVERING)
    run_store.save_run(r_disc)
    r_running = make_run("ptrunning01", RunPhase.RUNNING)
    done_co = CompanyState(name="DoneCo", domain="d.com", status=CompanyStatus.DONE)
    done_co.draft_subject = "kept"
    inflight = CompanyState(name="MidCo", domain="m.com", status=CompanyStatus.RESEARCH)
    r_running.companies = [done_co, inflight]
    run_store.save_run(r_running)
    r_review = make_run("ptreview001", RunPhase.REVIEW)
    r_review.discovered = [{"name": "GateCo", "domain": "gateco.com", "reason": "x"}]
    run_store.save_run(r_review)

    reloaded = run_store.load_all_runs()
    assert reloaded["ptdisc00001"].phase == RunPhase.ERROR
    assert "restart" in reloaded["ptdisc00001"].error
    rr = reloaded["ptrunning01"]
    assert rr.phase == RunPhase.DONE
    assert rr.companies[0].status == CompanyStatus.DONE and rr.companies[0].draft_subject == "kept"
    assert rr.companies[1].status == CompanyStatus.DROPPED
    assert rr.companies[1].drop_reason == "interrupted by server restart"
    assert reloaded["ptreview001"].phase == RunPhase.REVIEW, "review gate must survive untouched"
    print("PASS: on load - discovery run errored, running run settled (drafts kept, in-flight dropped), review gate intact")

    # --- 3. Review gate usable through real routes after 'restart' ---
    RUNS.clear()
    RUNS.update(reloaded)
    config.settings.LOGIN_REQUIRED = True
    client = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": EMAIL, "name": "T"}
    client.get("/auth/callback?code=x&state=y")
    html = client.get("/runs/ptreview001/panel").text
    assert "GateCo" in html and "Approve" in html, "review checklist must render after restart"
    pipeline_calls = []
    async def fake_run_pipeline(run, candidate, approved):
        pipeline_calls.append(approved)
        run.phase = RunPhase.DONE
    real_run_pipeline = pipeline.run_pipeline
    pipeline.run_pipeline = fake_run_pipeline  # the manager resolves it at call time
    try:
        r = client.post("/runs/ptreview001/approve", data={"approved": "gateco.com"}, follow_redirects=False)
        assert r.status_code == 303 and pipeline_calls == [["gateco.com"]]
    finally:
        pipeline.run_pipeline = real_run_pipeline
    print("PASS: a review-gate run reloaded from disk renders and can be approved")

    # --- 4. Mid-run checkpoints: each company completion hits the DB ---
    config.settings.RUN_LAUNCH_JITTER_SECONDS = 0
    async def fake_company(candidate, company, *a, **kw):
        company.status = CompanyStatus.DONE
        company.draft_subject = "midrun draft"
    pipeline._run_company = fake_company
    live = make_run("ptlive00001", RunPhase.REVIEW)
    live.discovered = [{"name": "LiveCo", "domain": "liveco.com", "reason": "x"}]
    RUNS[live.id] = live
    cand = storage.get_candidate(CID)
    asyncio.run(pipeline.run_pipeline(live, cand, ["liveco.com"]))
    on_disk = run_store.load_all_runs()[live.id].to_dict()
    assert on_disk["phase"] == RunPhase.DONE
    assert on_disk["companies"][0]["draft_subject"] == "midrun draft"
    print("PASS: pipeline checkpoints write finished drafts to the DB")

    # --- 5. Pruning keeps the newest 5; profile delete removes all ---
    now = time.time()
    for i in range(7):
        r = make_run(f"ptprune000{i}", RunPhase.DONE, created_at=now - (7 - i) * 60)
        RUNS[r.id] = r
        run_store.save_run(r)
    run_store.prune_candidate_runs(CID, RUNS, keep=5)
    mine = [rid for rid, r in RUNS.items() if r.candidate_id == CID]
    assert len(mine) == 5, f"expected 5 kept, got {len(mine)}"
    assert "ptprune0000" not in RUNS and "ptprune0001" not in RUNS, "oldest two should be pruned"
    assert "ptprune0000" not in run_store.load_all_runs()
    print("PASS: pruning keeps the 5 newest runs (memory + DB)")

    run_store.delete_candidate_runs(CID, RUNS)
    assert not any(r.candidate_id == CID for r in RUNS.values())
    persisted = run_store.load_all_runs()
    assert not any(rid in persisted for rid in TEST_IDS)
    print("PASS: deleting the profile removes every persisted run")
finally:
    for rid in TEST_IDS:
        RUNS.pop(rid, None)
        run_store.delete_run(rid)
    storage.delete_candidate(CID)
    print("cleanup: temp candidate + run rows removed")
