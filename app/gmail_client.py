"""Gmail OAuth + draft creation, scoped to drafts only.

Design notes (compliance-relevant):
- Scope is gmail.compose only — the narrowest scope Google offers that can
  create drafts. There is no scope that permits draft-creation but
  structurally forbids sending; gmail.compose does technically allow it.
  The guarantee that we "never send automatically" is enforced by this file
  never calling the send endpoint, not by the OAuth grant itself.
- Each candidate connects their own Gmail account (outreach should come from
  the candidate's own inbox, not a shared app account). One Google Cloud
  OAuth Client ID (configured via env vars) is shared across candidates;
  each candidate's own consent produces their own token, stored separately.
- Tokens are stored one file per candidate under data/gmail_tokens/, never
  inside candidates.json. Disconnecting revokes the grant at Google, not
  just deletes the local copy.
"""

import asyncio
import base64
import json
import logging
from email.message import EmailMessage
from html import escape
from typing import Dict, Optional

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .config import (
    GMAIL_SCOPES,
    GMAIL_TOKENS_DIR,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
)
from .fsutil import atomic_write_text
from .models import Candidate, CompanyState
from .resume import ensure_resume_pdf, resume_pdf_display_name

logger = logging.getLogger("app.gmail")


class GmailNotConfigured(Exception):
    """Raised when GOOGLE_CLIENT_ID/SECRET aren't set."""


def _require_configured() -> None:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise GmailNotConfigured(
            "Gmail integration isn't configured — set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (see README)."
        )


def _client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }


def _token_path(candidate_id: str):
    return GMAIL_TOKENS_DIR / f"{candidate_id}.json"


def is_connected(candidate_id: str) -> bool:
    return _token_path(candidate_id).exists()


# Flow.authorization_url() auto-generates a PKCE code_verifier on the Flow
# instance that built it; the callback constructs a brand-new Flow, which
# would otherwise have no verifier to complete the exchange with ("Missing
# code verifier"). Bridge the two steps by candidate_id, same in-memory
# pattern as pipeline.RUNS — fine for a single-process app, lost on reload
# same as an in-progress run would be.
_pending_verifiers: Dict[str, str] = {}


def build_auth_url(candidate_id: str) -> str:
    """Start the OAuth flow. `candidate_id` is round-tripped via `state`."""
    _require_configured()
    flow = Flow.from_client_config(
        _client_config(), scopes=GMAIL_SCOPES, redirect_uri=GOOGLE_REDIRECT_URI
    )
    # access_type=offline + prompt=consent guarantee a refresh_token is
    # issued, including on a repeat authorization by the same user.
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", state=candidate_id
    )
    _pending_verifiers[candidate_id] = flow.code_verifier
    return auth_url


def handle_oauth_callback(code: str, candidate_id: str) -> None:
    """Exchange the authorization code for tokens and persist them."""
    _require_configured()
    code_verifier = _pending_verifiers.pop(candidate_id, None)
    flow = Flow.from_client_config(
        _client_config(),
        scopes=GMAIL_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        code_verifier=code_verifier,
    )
    flow.fetch_token(code=code)
    _save_credentials(candidate_id, flow.credentials)
    logger.info("Gmail connected for candidate %s", candidate_id)


def _save_credentials(candidate_id: str, creds: Credentials) -> None:
    atomic_write_text(_token_path(candidate_id), creds.to_json())


def _load_credentials(candidate_id: str) -> Optional[Credentials]:
    path = _token_path(candidate_id)
    if not path.exists():
        return None
    info = json.loads(path.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        _save_credentials(candidate_id, creds)  # access token rotated — persist it
    return creds


def disconnect(candidate_id: str) -> None:
    """Revoke the grant at Google, then remove the local token file."""
    creds = _load_credentials(candidate_id)
    if creds:
        try:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": creds.token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except requests.RequestException:
            # best-effort — still remove the local copy below
            logger.warning(
                "Gmail token revoke failed for candidate %s (removing local token anyway)",
                candidate_id, exc_info=True,
            )
    path = _token_path(candidate_id)
    if path.exists():
        path.unlink()
        logger.info("Gmail disconnected for candidate %s", candidate_id)


def _body_to_html(body: str) -> str:
    """The draft body as minimal HTML: paragraphs as <p>, single line breaks
    (the signature block) as <br>. Deliberately unstyled — it must read as a
    personal, hand-written email, not a designed one."""
    paragraphs = [
        "<p>" + escape(p).replace("\n", "<br>") + "</p>"
        for p in body.split("\n\n")
    ]
    return "<html><body>" + "".join(paragraphs) + "</body></html>"


def _build_raw_message(
    to_addr: str, subject: str, body: str, attachment: Optional[bytes] = None,
    attachment_name: str = "",
) -> str:
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    # HTML alternative alongside the plain text. A text/plain-only draft puts
    # Gmail's composer in plain-text mode, and Gmail hard-wraps plain text at
    # ~70 chars on SEND — the recipient then sees a narrow ragged column
    # instead of reflowing prose. With an HTML part, Gmail composes and sends
    # rich text, which reflows to the reader's pane width.
    msg.add_alternative(_body_to_html(body), subtype="html")
    if attachment is not None:
        msg.add_attachment(
            attachment, maintype="application", subtype="pdf", filename=attachment_name
        )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _create_draft_sync(candidate: Candidate, to_addr: str, subject: str, body: str) -> None:
    creds = _load_credentials(candidate.id)
    if not creds:
        raise GmailNotConfigured(f"No Gmail account connected for candidate {candidate.id}.")
    # The resume PDF must attach successfully — the draft body references the
    # attachment (DRAFT_SYSTEM assumes it), so failing loudly beats creating a
    # draft that mentions an attachment that isn't there.
    pdf_bytes = ensure_resume_pdf(candidate).read_bytes()
    service = build("gmail", "v1", credentials=creds)
    raw = _build_raw_message(
        to_addr, subject, body,
        attachment=pdf_bytes, attachment_name=resume_pdf_display_name(candidate),
    )
    service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    logger.info("Gmail draft created for candidate %s (subject: %s)", candidate.id, subject)


async def create_draft(candidate: Candidate, company: CompanyState) -> None:
    """Create a Gmail draft (resume PDF attached) for one company's generated
    email. Raises on failure."""
    await asyncio.to_thread(
        _create_draft_sync, candidate, company.email, company.draft_subject, company.draft_body
    )
