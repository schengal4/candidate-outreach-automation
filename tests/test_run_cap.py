"""Daily run cap: a profile that already started MAX_RUNS_PER_DAY runs in the
last 24h gets a 429 instead of burning more API money; older runs don't
count. Uses a throwaway account/candidate and a mocked discovery."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import time

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
import app.run_store as run_store
from app import storage
from app.models import Candidate, RunPhase, RunState
from app.pipeline import RUNS

EMAIL = "runcap.test@example.com"
CID = "runcaptest01"
TEST_RUN_IDS = []

main.LOGIN_REQUIRED = True
storage.save_candidate(Candidate(id=CID, name="Cap Test", email="", current_employer="X",
                                 resume_text="r", owner_email=EMAIL))

async def fake_discovery(run, candidate):
    run.phase = RunPhase.REVIEW
main.run_discovery = fake_discovery

client = TestClient(main.app)
auth_mod.handle_login_callback = lambda code, state: {"email": EMAIL, "name": "T"}
client.get("/auth/callback?code=x&state=y")

try:
    # Plant MAX_RUNS_PER_DAY runs from "today"
    for i in range(main.MAX_RUNS_PER_DAY):
        r = RunState(id=f"capplant{i:04d}", candidate_id=CID, phase=RunPhase.DONE)
        RUNS[r.id] = r
        TEST_RUN_IDS.append(r.id)

    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 429, resp.status_code
    assert "Daily run limit" in resp.text
    print(f"PASS: run #{main.MAX_RUNS_PER_DAY + 1} within 24h is refused with 429")

    # Age one run past 24h -> a slot frees up
    RUNS[TEST_RUN_IDS[0]].created_at = time.time() - 25 * 3600
    resp = client.post(f"/candidates/{CID}/runs", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/runs/")
    new_run_id = resp.headers["location"].rsplit("/", 1)[1]
    TEST_RUN_IDS.append(new_run_id)
    print("PASS: once a run ages past 24h, a new one is allowed")

    # Ownership still enforced before the cap check
    other = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": "x@y.com", "name": "X"}
    other.get("/auth/callback?code=x&state=y")
    assert other.post(f"/candidates/{CID}/runs", follow_redirects=False).status_code == 404
    print("PASS: non-owners still get 404, not a cap response")
finally:
    for rid in list(TEST_RUN_IDS):
        RUNS.pop(rid, None)
        run_store.delete_run(rid)
    storage.delete_candidate(CID)
    print("cleanup: temp candidate + planted runs removed")
