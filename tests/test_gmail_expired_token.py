"""An expired/revoked Gmail grant (Google's invalid_grant) must be recoverable
from the UI. Three guarantees, each broken by a real run on 2026-07-17:
  1. disconnect works on a dead token (it used to refresh first, raise
     RefreshError, and 500 — leaving the user unable to reconnect).
  2. _load_credentials raises the friendly GmailAuthExpired instead of
     leaking Google's raw ('invalid_grant: ...', {...}) tuple into the UI.
  3. save_all_to_gmail stops after the first GmailAuthExpired and marks all
     remaining drafts, instead of re-failing per company."""
import asyncio
import json
import pathlib
import sys
import tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import requests
from google.auth.exceptions import RefreshError

from app import config, db, gmail_client

# Point data at a temp dir so token files never touch real data.
_original_dir = config.settings.DATA_DIR
db.close()
tmp = pathlib.Path(tempfile.mkdtemp())
config.settings.DATA_DIR = tmp
config.ensure_dirs()

CID = "testgmailexp"
TOKEN_FILE = config.settings.GMAIL_TOKENS_DIR / f"{CID}.json"

try:
    # 1. disconnect with a dead token: no refresh attempted, revoke called
    #    with the refresh token, file deleted — even when revoke itself fails.
    TOKEN_FILE.write_text(json.dumps({
        "token": "expired-access-token",
        "refresh_token": "dead-refresh-token",
        "client_id": "x", "client_secret": "y",
        "expiry": "2020-01-01T00:00:00Z",
    }), encoding="utf-8")

    revoked_with = []
    real_post = gmail_client.requests.post

    def fake_post(url, **kw):
        revoked_with.append(kw.get("params", {}).get("token"))
        raise requests.ConnectionError("revoke endpoint unreachable")

    gmail_client.requests.post = fake_post
    try:
        gmail_client.disconnect(CID)
    finally:
        gmail_client.requests.post = real_post
    assert not TOKEN_FILE.exists(), "token file must be deleted"
    assert revoked_with == ["dead-refresh-token"], revoked_with
    print("PASS: disconnect deletes a dead token without refreshing it")

    # 2. _load_credentials wraps RefreshError in GmailAuthExpired
    TOKEN_FILE.write_text(json.dumps({
        "token": "expired-access-token",
        "refresh_token": "dead-refresh-token",
        "client_id": "x", "client_secret": "y",
        "expiry": "2020-01-01T00:00:00Z",
    }), encoding="utf-8")

    class FakeCreds:
        expired = True
        refresh_token = "dead-refresh-token"

        def refresh(self, request):
            raise RefreshError(
                "invalid_grant: Token has been expired or revoked.",
                {"error": "invalid_grant"},
            )

    real_from_info = gmail_client.Credentials.from_authorized_user_info
    gmail_client.Credentials.from_authorized_user_info = staticmethod(
        lambda info, scopes=None: FakeCreds()
    )
    try:
        gmail_client._load_credentials(CID)
        assert False, "expected GmailAuthExpired"
    except gmail_client.GmailAuthExpired as exc:
        assert "Reconnect Gmail" in str(exc), str(exc)
        assert "invalid_grant" not in str(exc), "raw Google error must not leak"
    finally:
        gmail_client.Credentials.from_authorized_user_info = real_from_info
    TOKEN_FILE.unlink()
    print("PASS: a failed refresh surfaces as the friendly GmailAuthExpired")

    # 3. save_all_to_gmail: first GmailAuthExpired stops the batch and marks
    #    every remaining eligible draft with the same message.
    from fastapi.testclient import TestClient

    from app import storage
    from app.main import app
    import app.main as main_mod
    from app.models import Candidate, CompanyState, CompanyStatus, RunPhase, RunState
    from app.run_manager import manager

    config.settings.LOGIN_REQUIRED = False
    storage.save_candidate(Candidate(id=CID, name="G", email="g@g.com",
                                     current_employer="X", resume_text="r"))
    run = RunState(id="gmailexprun", candidate_id=CID, phase=RunPhase.DONE)
    for n in ("A", "B", "C"):
        c = CompanyState(name=n, domain=f"{n.lower()}.com", status=CompanyStatus.DONE)
        c.draft_subject, c.draft_body, c.email = "s", "b", f"x@{n.lower()}.com"
        run.companies.append(c)
    manager.runs[run.id] = run

    attempts = []

    async def fake_create_draft(candidate, company):
        attempts.append(company.name)
        raise gmail_client.GmailAuthExpired()

    real_create_draft = main_mod.gmail_client.create_draft
    main_mod.gmail_client.create_draft = fake_create_draft
    try:
        client = TestClient(app)
        resp = client.post(f"/runs/{run.id}/save_all_to_gmail", follow_redirects=False)
        assert resp.status_code == 303
    finally:
        main_mod.gmail_client.create_draft = real_create_draft
    assert attempts == ["A"], f"should stop after the first failure: {attempts}"
    for c in run.companies:
        assert "Reconnect Gmail" in c.gmail_error, (c.name, c.gmail_error)
        assert not c.gmail_draft_created
    print("PASS: save-all stops at the first expired-auth failure and marks the rest")

    manager.runs.pop(run.id, None)
    from app import run_store
    run_store.delete_run(run.id)
    storage.delete_candidate(CID)
finally:
    db.close()
    config.settings.DATA_DIR = _original_dir
