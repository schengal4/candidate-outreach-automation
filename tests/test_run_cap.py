"""Daily run cap: a profile that already started MAX_RUNS_PER_DAY runs in the
last 24h gets a 429 instead of burning more API money; older runs don't
count; pruning run reports does NOT reset the cap (it counts the never-pruned
ledger). Uses a throwaway account/candidate and a mocked discovery."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import time

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
import app.pipeline as pipeline
import app.run_store as run_store
from app import config, storage
from app.models import Candidate, RunPhase
from app.run_manager import manager

EMAIL = "runcap.test@example.com"
CID = "runcaptest01"
CAP = config.settings.MAX_RUNS_PER_DAY

config.settings.LOGIN_REQUIRED = True
storage.save_candidate(Candidate(id=CID, name="Cap Test", email="", current_employer="X",
                                 resume_text="r", owner_email=EMAIL))

async def fake_discovery(run, candidate, previous_runs=()):
    run.phase = RunPhase.REVIEW
pipeline.run_discovery = fake_discovery

client = TestClient(main.app)
auth_mod.handle_login_callback = lambda code, state: {"email": EMAIL, "name": "T"}
client.get("/auth/callback?code=x&state=y")

try:
    # Plant CAP ledger entries from "today"
    for _ in range(CAP):
        run_store.record_run_started(CID, time.time())

    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 429, resp.status_code
    assert "Daily run limit" in resp.text
    print(f"PASS: run #{CAP + 1} within 24h is refused with 429")

    # Rolling window: entries older than 24h don't count against the cap
    manager.delete_candidate_runs(CID)  # clears runs + ledger
    for _ in range(CAP - 1):
        run_store.record_run_started(CID, time.time())
    run_store.record_run_started(CID, time.time() - 25 * 3600)  # aged out
    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/runs/")
    print("PASS: once a run ages past 24h, a new one is allowed")

    # That run consumed the last slot (it was ledgered) -> refused again
    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 429
    print("PASS: a started run is ledgered immediately and counts toward the cap")

    # Pruning run REPORTS must not reset the cap — the ledger is never pruned
    run_store.prune_candidate_runs(CID, manager.runs, keep=0)
    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 429, "pruned reports must still count toward the cap"
    print("PASS: pruning old run reports cannot disable the cost guardrail")

    # Ownership still enforced before the cap check
    other = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": "x@y.com", "name": "X"}
    other.get("/auth/callback?code=x&state=y")
    assert other.post(f"/candidates/{CID}/runs", follow_redirects=False).status_code == 404
    print("PASS: non-owners still get 404, not a cap response")
finally:
    manager.delete_candidate_runs(CID)  # removes runs + ledger rows
    storage.delete_candidate(CID)
    print("cleanup: temp candidate + planted runs removed")
