"""Per-account data isolation tests. Two simulated Google accounts hit the
real routes via TestClient; only the Google token exchange is mocked.
A temp candidate created as the second user is cleaned up at the end."""
import io
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from docx import Document
from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
from app import config, storage
from app.models import RunState, RunPhase
from app.run_manager import manager
from app.resume import resume_docx_path

RUNS = manager.runs
config.settings.LOGIN_REQUIRED = True
OWNER = "venkatachengalvala@gmail.com"
OTHER = "other.person@example.com"
VENKATA_ID = "516e7c4751"


def login_as(client, email, name):
    auth_mod.handle_login_callback = lambda code, state: {"email": email, "name": name}
    r = client.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def make_docx_bytes():
    doc = Document()
    doc.add_paragraph("Test resume for isolation check. Skills: testing.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


owner_client = TestClient(main.app)
other_client = TestClient(main.app)

# --- Owner (single profile) is taken straight to it ---
login_as(owner_client, OWNER, "Venkata")
r = owner_client.get("/", follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == f"/candidates/{VENKATA_ID}"
html = owner_client.get("/").text  # follow the redirect
assert "Venkata Chengalvala" in html
print("PASS: owner's home page goes straight to their profile")

# --- Second account sees nothing and can't reach owner's data ---
login_as(other_client, OTHER, "Other Person")
html = other_client.get("/").text
assert "Venkata Chengalvala" not in html
print("PASS: another account's home page shows none of the owner's data")

for path, method in [
    (f"/candidates/{VENKATA_ID}", "get"),
    (f"/sent/{VENKATA_ID}", "get"),
    (f"/candidates/{VENKATA_ID}/gmail/connect", "get"),
    (f"/candidates/{VENKATA_ID}/gmail/authorize", "get"),
]:
    r = getattr(other_client, method)(path, follow_redirects=False)
    assert r.status_code == 404, (path, r.status_code)
r = other_client.post(f"/candidates/{VENKATA_ID}/runs", follow_redirects=False)
assert r.status_code == 404
r = other_client.post(f"/sent/{VENKATA_ID}/add", data={"contact_email": "a@b.com"}, follow_redirects=False)
assert r.status_code == 404  # not-owned candidate is a 404, and must not have written
assert all(e["contact_email"] != "a@b.com" for e in __import__("app.sent_list", fromlist=["x"]).load_entries(VENKATA_ID))
r = other_client.post(f"/sent/{VENKATA_ID}/0/update", data={"action": "delete"}, follow_redirects=False)
assert r.status_code == 404
print("PASS: candidate page, sent list (read+write), gmail, and run-start all 404 for non-owners")

# --- Run ownership: runs inherit the candidate's owner ---
run = RunState(id="isotestrun1", candidate_id=VENKATA_ID, phase=RunPhase.REVIEW)
RUNS[run.id] = run
assert owner_client.get(f"/runs/{run.id}").status_code == 200
assert other_client.get(f"/runs/{run.id}").status_code == 404
assert other_client.get(f"/runs/{run.id}/panel").status_code == 286  # "Run not found" panel
assert "Run not found" in other_client.get(f"/runs/{run.id}/panel").text
RUNS.pop(run.id, None)
print("PASS: runs are visible to the candidate's owner only")

# --- New candidates get stamped with their creator ---
resp = other_client.post(
    "/candidates",
    data={"name": "Other's Candidate", "current_employer": "X Corp"},
    files={"resume": ("r.docx", make_docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    follow_redirects=False,
)
assert resp.status_code == 303
new_id = resp.headers["location"].rsplit("/", 1)[1]
try:
    created = storage.get_candidate(new_id)
    assert created.owner_email == OTHER, created.owner_email
    # Visible to its creator, invisible to the original owner
    other_home = other_client.get("/").text
    assert "Other&#39;s Candidate" in other_home or "Other's Candidate" in other_home
    owner_home = owner_client.get("/").text
    assert "Other&#39;s Candidate" not in owner_home and "Other's Candidate" not in owner_home
    assert owner_client.get(f"/candidates/{new_id}").status_code == 404
    print("PASS: new candidate is stamped with its creator and hidden from other accounts")
finally:
    p = resume_docx_path(created)
    storage.delete_candidate(new_id)
    if p.exists():
        p.unlink()
    pdf = p.with_suffix(".pdf")
    if pdf.exists():
        pdf.unlink()

# --- Open mode preserves the original single-user behavior ---
config.settings.LOGIN_REQUIRED = False
open_client = TestClient(main.app)
html = open_client.get("/").text
assert "Venkata Chengalvala" in html
assert open_client.get(f"/candidates/{VENKATA_ID}").status_code == 200
print("PASS: open mode (REQUIRE_LOGIN=0) still shows everything, as before")
