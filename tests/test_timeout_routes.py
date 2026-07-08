"""Verify /runs/{id}/panel shows the button only after the threshold, and
/runs/{id}/stop_early sets the stop_event on a RUNNING run."""
import sys
import time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app.main import app
from app.models import RunState, RunPhase, CompanyState, CompanyStatus
from app.pipeline import RUNS

client = TestClient(app)

# Case 1: just started running (elapsed ~0s) -- no button
run1 = RunState(id="panelrun1", candidate_id="516e7c4751", phase=RunPhase.RUNNING)
run1.started_running_at = time.time()
run1.companies = [CompanyState(name="A", domain="a.com", status=CompanyStatus.CONTACTS)]
RUNS[run1.id] = run1
html = client.get(f"/runs/{run1.id}/panel").text
assert "stop_early" not in html, "button should not show before threshold"
print("PASS: no button before the 10-minute threshold")

# Case 2: started 11 minutes ago -- button shows
run2 = RunState(id="panelrun2", candidate_id="516e7c4751", phase=RunPhase.RUNNING)
run2.started_running_at = time.time() - 11 * 60
run2.companies = [CompanyState(name="A", domain="a.com", status=CompanyStatus.CONTACTS)]
RUNS[run2.id] = run2
html = client.get(f"/runs/{run2.id}/panel").text
assert f"/runs/{run2.id}/stop_early" in html, "button should show past threshold"
assert "Stop now" in html
print("PASS: button appears past the 10-minute threshold")

# Case 3: stop_early sets the event on a RUNNING run
assert not run2.stop_event.is_set()
resp = client.post(f"/runs/{run2.id}/stop_early", follow_redirects=False)
assert resp.status_code == 303
assert run2.stop_event.is_set(), "stop_event should be set after posting stop_early"
print("PASS: /stop_early sets run.stop_event")

# Case 4: stop_early is a no-op on a non-running run (e.g. already done)
run3 = RunState(id="panelrun3", candidate_id="516e7c4751", phase=RunPhase.DONE)
RUNS[run3.id] = run3
client.post(f"/runs/{run3.id}/stop_early", follow_redirects=False)
assert not run3.stop_event.is_set()
print("PASS: /stop_early is a no-op when the run isn't RUNNING")

for rid in (run1.id, run2.id, run3.id):
    RUNS.pop(rid, None)
