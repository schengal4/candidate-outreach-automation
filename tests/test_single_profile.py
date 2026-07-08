"""One-profile-per-account flow + delete-profile route. Uses a throwaway
second account for create/delete; the owner's real data is only read."""
import io
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from docx import Document
from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
from app import sent_list, storage
from app.models import RunState
from app.pipeline import RUNS
from app.resume import resume_docx_path, resume_pdf_path

main.LOGIN_REQUIRED = True
OWNER = "venkatachengalvala@gmail.com"
TEMP = "temp.tester@example.com"


def login(email, name="T"):
    c = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": email, "name": name}
    c.get("/auth/callback?code=x&state=y")
    return c


def docx_bytes():
    d = Document()
    d.add_paragraph("Temp resume with enough text to parse.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def create_profile(client, name):
    return client.post(
        "/candidates",
        data={"name": name, "current_employer": "X"},
        files={"resume": ("r.docx", docx_bytes(),
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        follow_redirects=False,
    )


# 1. Owner now has exactly one profile -> / goes straight to it
owner = login(OWNER, "Venkata")
r = owner.get("/", follow_redirects=False)
assert r.status_code == 303 and r.headers["location"].startswith("/candidates/")
print("PASS: owner (single profile) is redirected straight to their profile")

# 1b. Legacy multi-profile state (simulated via storage) -> table + note, no form
from app.models import Candidate as _C
LEGACY = "legacy.tester@example.com"
for i in (1, 2):
    storage.save_candidate(_C(id=f"legacytest{i}", name=f"Legacy {i}", email="",
                              current_employer="X", resume_text="r", owner_email=LEGACY))
try:
    legacy_client = login(LEGACY)
    r = legacy_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "more than one profile" in r.text and "Legacy 1" in r.text and "Legacy 2" in r.text
    assert 'action="/candidates"' not in r.text, "create form should be hidden when profiles exist"
    print("PASS: legacy 2-profile account sees the table + cleanup note, no create form")
finally:
    storage.delete_candidate("legacytest1")
    storage.delete_candidate("legacytest2")

# 2. Fresh account -> setup form, no table
temp = login(TEMP)
r = temp.get("/", follow_redirects=False)
assert r.status_code == 200 and "Set up your profile" in r.text and "<table>" not in r.text
print("PASS: fresh account sees the setup form only")

# 3. Create -> / now redirects straight to the profile
r = create_profile(temp, "Temp Tester")
assert r.status_code == 303
cid = r.headers["location"].rsplit("/", 1)[1]
try:
    r = temp.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/candidates/{cid}"
    print("PASS: with exactly one profile, / redirects straight to it")

    # 4. Second create is blocked -> lands on the existing profile
    r = create_profile(temp, "Duplicate")
    assert r.status_code == 303 and r.headers["location"] == f"/candidates/{cid}"
    assert len(storage.list_candidates(owner_email=TEMP)) == 1
    print("PASS: creating a second profile just lands on the existing one")

    # 5. Delete is ownership-gated
    r = owner.post(f"/candidates/{cid}/delete", follow_redirects=False)
    assert r.status_code == 404 and storage.get_candidate(cid) is not None
    print("PASS: another account cannot delete the profile")

    # 6. Delete removes record, files, sent list, and in-memory runs
    cand = storage.get_candidate(cid)
    sent_list.add_entry(cid, "x.com", "A B", "a@x.com", confirmed_sent=True)
    RUNS["temprun1"] = RunState(id="temprun1", candidate_id=cid)
    docx, pdf = resume_docx_path(cand), resume_pdf_path(cand)
    assert docx.exists()
    r = temp.post(f"/candidates/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert storage.get_candidate(cid) is None
    assert not docx.exists() and not pdf.exists()
    assert sent_list.load_entries(cid) == []
    assert "temprun1" not in RUNS
    r = temp.get(f"/candidates/{cid}", follow_redirects=False)
    assert r.status_code == 404
    r = temp.get("/", follow_redirects=False)
    assert r.status_code == 200 and "Set up your profile" in r.text
    print("PASS: delete removes record, resume files, sent list, runs; account is back to setup")
finally:
    # Safety net if an assertion fired mid-flight
    if storage.get_candidate(cid):
        cand = storage.get_candidate(cid)
        for p in (resume_docx_path(cand), resume_pdf_path(cand)):
            if p.exists():
                p.unlink()
        sent_list.delete_list(cid)
        storage.delete_candidate(cid)
    RUNS.pop("temprun1", None)

# 7. Open mode unchanged: table + create form for everyone
main.LOGIN_REQUIRED = False
open_client = TestClient(main.app)
r = open_client.get("/", follow_redirects=False)
assert r.status_code == 200
assert "Venkata Chengalvala" in r.text and 'action="/candidates"' in r.text
assert "more than one profile" not in r.text
print("PASS: open mode keeps the original multi-candidate table + form")

# Owner's real data untouched (single profile since Demo Candidate was deleted)
assert len(storage.list_candidates(owner_email=OWNER)) == 1
print("PASS: owner's real candidate untouched")
