"""Central configuration for the outreach pipeline."""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
DATA_DIR.mkdir(exist_ok=True)
RESUME_DIR.mkdir(exist_ok=True)

# LLM
ANTHROPIC_MODEL = "claude-sonnet-5"
WEB_SEARCH_MAX_USES = 8
LLM_MAX_TOKENS = 16000
# Each continuation resends the whole growing conversation so far as input
# tokens, so keep this low — 2 means at most 3 requests per ask_json() call.
MAX_PAUSE_TURN_CONTINUATIONS = 2

# Pipeline caps
MAX_COMPANIES_HARD_CAP = 30   # absolute cap per run
DEFAULT_MAX_COMPANIES = 10    # beta default
# All companies in a run fire their LLM calls concurrently, uncapped (bounded
# only by MAX_COMPANIES_HARD_CAP) — see llm.py's rate-limit retry/backoff for
# how bursts of simultaneous requests are handled.
HUNTER_CONCURRENCY = 10       # concurrent Hunter API calls (semaphore) — within Hunter's 15 req/s email-finder limit
# Each company task starts after a random 0..N-second delay instead of all
# slamming the Anthropic tokens-per-minute window in the same instant. A
# mid-stream 429 throws away the partial generation and restarts the call, so
# spreading the initial burst is cheaper than retrying it. Negligible next to
# the minutes-long calls themselves.
RUN_LAUNCH_JITTER_SECONDS = 5.0

# Cost guardrail: each run costs real money on the owner's Anthropic/Hunter
# keys (~$5 at current settings), so cap how many runs one profile can start
# per rolling 24 hours. Counted from persisted runs (run_store keeps at least
# this many per candidate — see KEEP_RUNS_PER_CANDIDATE there).
MAX_RUNS_PER_DAY = max(1, int(os.environ.get("MAX_RUNS_PER_DAY", "5")))

# Long-running-pipeline safety valve. A "retrieve what's done" button appears
# on the run page once a run has been in the RUNNING phase this long; the run
# stops itself (keeping whatever companies already finished) after
# RUN_HARD_TIMEOUT_SECONDS regardless of whether the button was clicked.
TIMEOUT_BUTTON_AFTER_SECONDS = 10 * 60
RUN_HARD_TIMEOUT_SECONDS = 30 * 60

# Hunter: minimum confidence score (0-100) to treat an email as valid
MIN_EMAIL_SCORE = 50

# Sent list retention
DEFAULT_RETENTION_MONTHS = 12
RETENTION_MIN = 6
RETENTION_MAX = 18

# Gmail integration (drafts only — see app/gmail_client.py). Requires a Google
# Cloud OAuth Client ID (Web application type); see README for setup steps.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/gmail/callback")
# gmail.compose is the narrowest scope that can create drafts. It technically
# also permits sending, but this app never calls the send endpoint.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
GMAIL_TOKENS_DIR = DATA_DIR / "gmail_tokens"
GMAIL_TOKENS_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------------ #
# App login (Google Sign-In) — identity only, deliberately separate from
# the gmail.compose draft flow above. Uses the same Google Cloud OAuth
# client, but its own narrower scopes and its own redirect URI (which must
# also be added as an Authorized redirect URI in the Google Cloud Console).
# ------------------------------------------------------------------ #
# Full-URL scope forms: Google returns granted scopes in this form, and
# oauthlib rejects the token exchange as "scope changed" if we requested
# the short aliases ("email", "profile") instead.
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
GOOGLE_LOGIN_REDIRECT_URI = os.environ.get(
    "GOOGLE_LOGIN_REDIRECT_URI", "http://localhost:8000/auth/callback"
)
# Login is required whenever the Google OAuth client is configured (without
# it the login flow can't work at all, so the app runs open as before).
# Set REQUIRE_LOGIN=0 to explicitly opt out while keeping Gmail drafts.
LOGIN_REQUIRED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET) and (
    os.environ.get("REQUIRE_LOGIN", "1").strip().lower() not in {"0", "false", "no"}
)
# Optional allowlist (comma-separated emails). When empty, anyone who can
# complete the Google OAuth flow may log in — note that while the OAuth app
# is in "Testing" status, Google itself already restricts that to the test
# users added in the Cloud Console.
ALLOWED_LOGIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ALLOWED_LOGIN_EMAILS", "").split(",")
    if e.strip()
}

# Secret for signing the session cookie. Generated once and persisted under
# data/ (gitignored) so logins survive server restarts; env var overrides.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    _secret_file = DATA_DIR / "session_secret"
    if _secret_file.exists():
        SESSION_SECRET = _secret_file.read_text(encoding="utf-8").strip()
    if not SESSION_SECRET:
        SESSION_SECRET = secrets.token_hex(32)
        _secret_file.write_text(SESSION_SECRET, encoding="utf-8")
SESSION_MAX_AGE_SECONDS = 14 * 24 * 3600  # re-login after two weeks
