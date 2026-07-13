"""Resume replacement route: swaps the text and on-disk files, rejects bad
uploads, and enforces ownership. Uses a throwaway candidate, removed in
finally."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.main as main
from app import config, storage
from app.models import Candidate
from app.resume import resume_docx_path, resume_pdf_path

config.settings.LOGIN_REQUIRED = True
main.ensure_resume_pdf = lambda c: None  # Word COM stays out of the suite
OWNER = "resume-test@example.com"


def docx_bytes(text: str) -> bytes:
    buf = BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


cand = Candidate(
    id="testresume1",
    name="Resume Tester",
    email="",
    owner_email=OWNER,
    current_employer="Acme",
    resume_text="OLD RESUME TEXT",
    resume_filename="old_resume.docx",
)
storage.save_candidate(cand)
old_docx = resume_docx_path(cand)
old_pdf = resume_pdf_path(cand)
old_docx.write_bytes(docx_bytes("OLD RESUME TEXT"))
old_pdf.write_bytes(b"%PDF-fake")

client = TestClient(main.app)
auth_mod.handle_login_callback = lambda code, state: {"email": OWNER, "name": "T"}
client.get("/auth/callback?code=x&state=y")

try:
    # 1. Replace via the route: text, filename, and files all swap
    r = client.post(
        f"/candidates/{cand.id}/resume",
        files={"resume": ("new_resume.docx", docx_bytes("NEW RESUME TEXT"),
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cand = storage.get_candidate(cand.id)
    assert cand.resume_text == "NEW RESUME TEXT"
    assert cand.resume_filename == "new_resume.docx"
    print("PASS: resume text and filename replaced via the route")

    assert resume_docx_path(cand).exists(), "new .docx not written"
    assert not old_docx.exists() and not old_pdf.exists(), "old files not removed"
    print("PASS: new .docx on disk, old .docx and cached .pdf removed")

    # 2. Unreadable upload -> 400, candidate untouched
    r = client.post(
        f"/candidates/{cand.id}/resume",
        files={"resume": ("bad.docx", b"not a docx", "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert storage.get_candidate(cand.id).resume_text == "NEW RESUME TEXT"
    print("PASS: bad upload rejected with 400, resume unchanged")

    # 3. Ownership enforced
    other = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": "x@example.com", "name": "X"}
    other.get("/auth/callback?code=x&state=y")
    r = other.post(
        f"/candidates/{cand.id}/resume",
        files={"resume": ("hijack.docx", docx_bytes("HIJACKED"),
                          "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 404
    assert storage.get_candidate(cand.id).resume_text == "NEW RESUME TEXT"
    print("PASS: another account cannot replace the resume (404, unchanged)")
finally:
    c = storage.get_candidate(cand.id)
    if c:
        for path in (resume_docx_path(c), resume_pdf_path(c)):
            if path.exists():
                path.unlink()
    for path in (old_docx, old_pdf):
        if path.exists():
            path.unlink()
    storage.delete_candidate(cand.id)
    print("cleanup: throwaway candidate and resume files removed")
