"""Central configuration for the outreach pipeline.

All tunables live on the Settings dataclass below; the module-level
``settings`` instance is the one the app uses. Reading environment variables
happens once, in ``Settings.from_env()`` — but importing this module has NO
filesystem side effects (no mkdir, no secret generation). Directories are
created lazily by the code that writes into them (see app/fsutil.py and
app/db.py) and at app startup (see app/main.py's lifespan).

Tests can override any value in-process::

    from app import config
    config.settings.MAX_RUNS_PER_DAY = 1

or swap the whole object: ``config.settings = replace(config.settings, ...)``.
App code therefore reads ``config.settings.X`` at call time instead of
importing constants (which would freeze the value at import).
"""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    return max(minimum, int(os.environ.get(name, str(default))))


@dataclass
class Settings:
    # ---- storage ----
    BASE_DIR: Path = BASE_DIR
    DATA_DIR: Path = BASE_DIR / "data"

    # Logging (see app/logging_setup.py): console + rotating file under
    # data/logs/. LOG_LEVEL takes the standard names — DEBUG, INFO, WARNING,
    # ERROR. LOG_DIR is overridable so the test runner can keep test noise
    # out of the real log file (it is a field, not derived from DATA_DIR,
    # for exactly that reason).
    LOG_LEVEL: str = "INFO"
    LOG_DIR: Path = BASE_DIR / "data" / "logs"

    # ---- LLM ----
    ANTHROPIC_MODEL: str = "claude-sonnet-5"
    WEB_SEARCH_MAX_USES: int = 8
    # Contact identification gets a tighter leash than discovery/research:
    # logged runs showed EVERY web-search call burning its full 8-search
    # budget, and the hard-to-verify companies riding the full 15-minute call
    # timeout without producing anyone usable. Fewer searches + an explicit
    # give-up instruction in CONTACT_SYSTEM turns those 15-minute timeouts
    # into fast clean failures, which the bench (see DISCOVERY_BENCH_EXTRA)
    # can then backfill.
    CONTACT_WEB_SEARCH_MAX_USES: int = 5
    CONTACT_CALL_TIMEOUT_SECONDS: int = 8 * 60
    # Personalization research gets its own leash too — looser than contact
    # identification (reading a few sources is the point) but tighter than
    # the 15-minute default. Logged runs had single research calls burning
    # the full 8-search budget and riding 9-10 minutes, and since a run only
    # ends when the slowest company's draft lands, those calls set the whole
    # run's wall-clock. Capping searches + the per-call ceiling bounds that
    # tail.
    RESEARCH_WEB_SEARCH_MAX_USES: int = 6
    RESEARCH_CALL_TIMEOUT_SECONDS: int = 8 * 60
    # When a research call blows its time valve, it gets ONE retry at this
    # smaller search budget instead of dropping the company — by then the
    # contact is verified and the email found, the two expensive wins.
    RESEARCH_TIMEOUT_RETRY_MAX_USES: int = 3
    # Draft verification (step 6) fact-checks each finished draft with a
    # fresh call: every specific claim is grounded against the research
    # notes, and the small search budget goes to the contact-employment
    # re-check, one search confirming the contact's LinkedIn URL, plus the one
    # or two most specific product claims. Deliberately the tightest leash of
    # any web step — it spot-checks, it must not become a second research pass.
    VERIFY_WEB_SEARCH_MAX_USES: int = 5
    # 8 (not 5) minutes since the step gained the LinkedIn URL search and the
    # currency checks — still the tightest-budgeted web step per search.
    VERIFY_CALL_TIMEOUT_SECONDS: int = 8 * 60
    LLM_MAX_TOKENS: int = 16000
    # Each continuation resends the whole growing conversation so far as
    # input tokens, so keep this low — 2 means at most 3 requests per
    # ask_json() call.
    MAX_PAUSE_TURN_CONTINUATIONS: int = 2
    # Safety valve for a single LLM call. Web-search-heavy contact searches
    # have run 20+ minutes as ONE streaming request on hard-to-verify
    # companies (the stream keeps delivering thinking, so no HTTP timeout
    # ever fires) — bound each call so a marathon company drops itself with
    # a clear reason instead of squatting until the run-wide timeout kills
    # everything. Applies per request, including its rate-limit retries.
    LLM_CALL_TIMEOUT_SECONDS: int = 15 * 60

    # ---- pipeline caps ----
    MAX_COMPANIES_HARD_CAP: int = 30   # absolute cap per run
    DEFAULT_MAX_COMPANIES: int = 10    # beta default
    # Discovery over-fetches this many companies beyond the candidate's
    # max_companies. The extras form a bench: when a company drops mid-run
    # (contact unverifiable, no email, timeout), the next bench company
    # starts in its place, so failures don't shrink the number of drafts
    # delivered.
    DISCOVERY_BENCH_EXTRA: int = 5
    # All companies in a run fire their LLM calls concurrently, uncapped
    # (bounded only by MAX_COMPANIES_HARD_CAP) — see llm.py's rate-limit
    # retry/backoff for how bursts of simultaneous requests are handled.
    HUNTER_CONCURRENCY: int = 10  # concurrent Hunter API calls (semaphore) — within Hunter's 15 req/s email-finder limit
    # Each company task starts after a random 0..N-second delay instead of
    # all slamming the Anthropic tokens-per-minute window in the same
    # instant. A mid-stream 429 throws away the partial generation and
    # restarts the call, so spreading the initial burst is cheaper than
    # retrying it. Negligible next to the minutes-long calls themselves.
    RUN_LAUNCH_JITTER_SECONDS: float = 5.0
    # Cost guardrail: each run costs real money on the owner's
    # Anthropic/Hunter keys (~$5 at current settings), so cap how many runs
    # one profile can start per rolling 24 hours. Counted from the run
    # ledger (see app/run_store.py), which is never pruned — so pruning old
    # run reports can't silently disable the guardrail.
    MAX_RUNS_PER_DAY: int = 5

    # Long-running-pipeline safety valve. A "retrieve what's done" button
    # appears on the run page once a run has been in the RUNNING phase this
    # long; the run stops itself (keeping whatever companies already
    # finished) after RUN_HARD_TIMEOUT_SECONDS regardless of whether the
    # button was clicked. 40 (not 30) minutes since the post-draft
    # fact-check + claim revision lengthened each company's chain — a real
    # run was cut at 32 minutes with drafts still in flight. Env-overridable.
    TIMEOUT_BUTTON_AFTER_SECONDS: int = 10 * 60
    RUN_HARD_TIMEOUT_SECONDS: int = 40 * 60

    # Hunter: minimum confidence score (0-100) to treat an email as valid
    MIN_EMAIL_SCORE: int = 50

    # Sent list retention
    DEFAULT_RETENTION_MONTHS: int = 12
    RETENTION_MIN: int = 6
    RETENTION_MAX: int = 18

    # ---- Gmail integration (drafts only — see app/gmail_client.py) ----
    # Requires a Google Cloud OAuth Client ID (Web application type); see
    # README for setup steps.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/gmail/callback"
    # gmail.compose is the narrowest scope that can create drafts. It
    # technically also permits sending, but this app never calls the send
    # endpoint.
    GMAIL_SCOPES: tuple = ("https://www.googleapis.com/auth/gmail.compose",)

    # ---- app login (Google Sign-In) — identity only, deliberately separate
    # from the gmail.compose draft flow above. Uses the same Google Cloud
    # OAuth client, but its own narrower scopes and its own redirect URI
    # (which must also be added as an Authorized redirect URI in the Google
    # Cloud Console). Full-URL scope forms: Google returns granted scopes in
    # this form, and oauthlib rejects the token exchange as "scope changed"
    # if we requested the short aliases ("email", "profile") instead.
    LOGIN_SCOPES: tuple = (
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    )
    GOOGLE_LOGIN_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    # Login is required whenever the Google OAuth client is configured
    # (without it the login flow can't work at all, so the app runs open as
    # before). Set REQUIRE_LOGIN=0 to explicitly opt out while keeping
    # Gmail drafts.
    LOGIN_REQUIRED: bool = False
    # Optional allowlist (comma-separated emails in ALLOWED_LOGIN_EMAILS).
    # When empty, anyone who can complete the Google OAuth flow may log in —
    # note that while the OAuth app is in "Testing" status, Google itself
    # already restricts that to the test users added in the Cloud Console.
    ALLOWED_LOGIN_EMAILS: set = field(default_factory=set)

    # Secret for signing the session cookie. Empty means "load or generate
    # one on demand" — see session_secret(), called at app creation, so
    # importing config never writes a file.
    SESSION_SECRET: str = ""
    SESSION_MAX_AGE_SECONDS: int = 14 * 24 * 3600  # re-login after two weeks

    # ---- derived paths (follow DATA_DIR overrides automatically) ----
    @property
    def RESUME_DIR(self) -> Path:
        return self.DATA_DIR / "resumes"

    @property
    def GMAIL_TOKENS_DIR(self) -> Path:
        return self.DATA_DIR / "gmail_tokens"

    @property
    def DB_PATH(self) -> Path:
        return self.DATA_DIR / "app.db"

    @classmethod
    def from_env(cls) -> "Settings":
        google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        return cls(
            LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
            LOG_DIR=Path(os.environ.get("LOG_DIR") or (BASE_DIR / "data" / "logs")),
            CONTACT_CALL_TIMEOUT_SECONDS=_env_int(
                "CONTACT_CALL_TIMEOUT_SECONDS", 8 * 60, minimum=60
            ),
            RESEARCH_CALL_TIMEOUT_SECONDS=_env_int(
                "RESEARCH_CALL_TIMEOUT_SECONDS", 8 * 60, minimum=60
            ),
            VERIFY_CALL_TIMEOUT_SECONDS=_env_int(
                "VERIFY_CALL_TIMEOUT_SECONDS", 8 * 60, minimum=60
            ),
            LLM_CALL_TIMEOUT_SECONDS=_env_int(
                "LLM_CALL_TIMEOUT_SECONDS", 15 * 60, minimum=60
            ),
            RUN_HARD_TIMEOUT_SECONDS=_env_int(
                "RUN_HARD_TIMEOUT_SECONDS", 40 * 60, minimum=60
            ),
            MAX_RUNS_PER_DAY=_env_int("MAX_RUNS_PER_DAY", 5, minimum=1),
            GOOGLE_CLIENT_ID=google_client_id,
            GOOGLE_CLIENT_SECRET=google_client_secret,
            GOOGLE_REDIRECT_URI=os.environ.get(
                "GOOGLE_REDIRECT_URI", "http://localhost:8000/gmail/callback"
            ),
            GOOGLE_LOGIN_REDIRECT_URI=os.environ.get(
                "GOOGLE_LOGIN_REDIRECT_URI", "http://localhost:8000/auth/callback"
            ),
            LOGIN_REQUIRED=bool(google_client_id and google_client_secret)
            and (
                os.environ.get("REQUIRE_LOGIN", "1").strip().lower()
                not in {"0", "false", "no"}
            ),
            ALLOWED_LOGIN_EMAILS={
                e.strip().lower()
                for e in os.environ.get("ALLOWED_LOGIN_EMAILS", "").split(",")
                if e.strip()
            },
            SESSION_SECRET=os.environ.get("SESSION_SECRET", ""),
        )


settings = Settings.from_env()


def session_secret() -> str:
    """The cookie-signing secret: env var, else a secret generated once and
    persisted under data/ (gitignored) so logins survive server restarts.
    Called at app creation (see app/main.py), never at import."""
    if settings.SESSION_SECRET:
        return settings.SESSION_SECRET
    secret_file = settings.DATA_DIR / "session_secret"
    if secret_file.exists():
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            settings.SESSION_SECRET = secret
            return secret
    secret = secrets.token_hex(32)
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    secret_file.write_text(secret, encoding="utf-8")
    settings.SESSION_SECRET = secret
    return secret


def ensure_dirs() -> None:
    """Create the data directories. Called from app startup and lazily by
    writers — never at import."""
    for d in (settings.DATA_DIR, settings.RESUME_DIR, settings.GMAIL_TOKENS_DIR, settings.LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
