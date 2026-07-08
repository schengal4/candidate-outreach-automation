"""Google Sign-In for the app itself (identity only).

Deliberately separate from gmail_client.py, per the design note in TODO.md:
the identity provider (how someone logs in) and the draft-destination
provider (Gmail vs. Outlook) are separable decisions. This flow requests
only openid/email/profile — it never sees the gmail.compose grant, and the
Gmail draft flow never sees this one. Both share the same Google Cloud
OAuth client ID; this flow's redirect URI (/auth/callback) must be added
as its own Authorized redirect URI in the Cloud Console.
"""

import secrets
from typing import Dict, Optional

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow

from .config import (
    ALLOWED_LOGIN_EMAILS,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_LOGIN_REDIRECT_URI,
    LOGIN_SCOPES,
)


class LoginError(Exception):
    """Raised when a login attempt can't be completed or isn't allowed."""


def _client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_LOGIN_REDIRECT_URI],
        }
    }


# Same PKCE bridge as gmail_client.py: authorization_url() generates a
# code_verifier on the Flow that built it, but the callback constructs a
# fresh Flow. Bridge the two steps via the random `state` token (which here
# also serves its normal anti-CSRF purpose — the callback only accepts a
# state this process issued). In-memory, single-process, lost on reload —
# a half-finished login just starts over.
_pending_verifiers: Dict[str, str] = {}


def build_login_url() -> str:
    flow = Flow.from_client_config(
        _client_config(), scopes=LOGIN_SCOPES, redirect_uri=GOOGLE_LOGIN_REDIRECT_URI
    )
    state = secrets.token_urlsafe(24)
    auth_url, _ = flow.authorization_url(state=state)
    _pending_verifiers[state] = flow.code_verifier
    return auth_url


def handle_login_callback(code: str, state: str) -> dict:
    """Exchange the code, verify the ID token, and return the user's identity.

    Returns {"email": ..., "name": ...}. Raises LoginError on any failure,
    including an email outside ALLOWED_LOGIN_EMAILS (when configured).
    """
    code_verifier = _pending_verifiers.pop(state, None)
    if not code_verifier:
        raise LoginError(
            "Login session not found (server restarted mid-login?). Please try again."
        )
    flow = Flow.from_client_config(
        _client_config(),
        scopes=LOGIN_SCOPES,
        redirect_uri=GOOGLE_LOGIN_REDIRECT_URI,
        code_verifier=code_verifier,
    )
    flow.fetch_token(code=code)

    raw_id_token: Optional[str] = getattr(flow.credentials, "id_token", None)
    if not raw_id_token:
        raise LoginError("Google did not return an identity token.")
    # Signature/audience/expiry verification — don't trust the JWT contents
    # without it. Small clock-skew tolerance for local-machine clocks.
    claims = google_id_token.verify_oauth2_token(
        raw_id_token, GoogleAuthRequest(), GOOGLE_CLIENT_ID, clock_skew_in_seconds=10
    )

    email = str(claims.get("email", "")).strip().lower()
    if not email or not claims.get("email_verified", False):
        raise LoginError("Google account has no verified email address.")
    if ALLOWED_LOGIN_EMAILS and email not in ALLOWED_LOGIN_EMAILS:
        raise LoginError(f"{email} is not authorized to use this app.")
    return {"email": email, "name": str(claims.get("name", "") or "")}
