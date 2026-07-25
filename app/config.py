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
    LOG_LEVEL: str = "DEBUG"
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
    # One Hunter domain-search per company feeds the contact call a lead list
    # (names + titles Hunter's index knows at the domain), so its web searches
    # go to VERIFYING people instead of discovering them. 0 disables the pull
    # entirely (the test suite sets this so no test can hit Hunter's live API).
    CONTACT_LEADS_LIMIT: int = 10
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
    # ...and on a tighter clock. A 3-search retry that hasn't finished in 4
    # minutes is wandering (or stuck behind rate-limit backoff) and isn't
    # going to finish — a real run's two research retries each rode the full
    # 8-minute ceiling before dropping anyway, and one of them set the whole
    # run's wall-clock.
    RESEARCH_TIMEOUT_RETRY_TIMEOUT_SECONDS: int = 4 * 60
    # Contact identification gets the same timeout salvage as research: one
    # retry on a smaller budget and a tighter clock. A real run dropped a
    # company whose PRIMARY contact was already found and verified because
    # the BACKUP contact search timed out with no second chance.
    CONTACT_TIMEOUT_RETRY_MAX_USES: int = 3
    CONTACT_TIMEOUT_RETRY_TIMEOUT_SECONDS: int = 4 * 60
    # Draft verification (step 6) fact-checks each finished draft with fresh
    # calls that see ONLY the contact and the draft (never the research
    # notes — they're LLM output themselves, and grounding against them is
    # circular) and verify every specific claim independently against live
    # sources. It runs as TWO PARALLEL passes (see steps.verify_draft):
    # searches inside one streamed request run serially (each is a
    # think+read round), so one 12-search call was the longest serial link
    # in every company's chain — the same reason research was split. The
    # split keeps the combined budget at 12, so no verification depth is
    # lost; each pass has a dedicated job so the split can't silently thin
    # coverage of either. Deliberately the most generous combined budget of
    # any web step: a wrong claim in a sent email is the worst failure this
    # product can ship, and the extra searches cost cents. Not uncapped,
    # though — a truly unlimited search loop just rides into the
    # pause_turn/timeout limits and comes back as a FAILED check, which
    # protects nothing. Whatever the budget can't reach is flagged
    # "unverified" rather than assumed correct.
    # Pass 1: contact employment + LinkedIn URL ownership.
    VERIFY_CONTACT_WEB_SEARCH_MAX_USES: int = 4
    # Pass 2: every factual claim in the draft body.
    VERIFY_CLAIMS_WEB_SEARCH_MAX_USES: int = 8
    # Claims-only web recheck of a revised draft (steps.revise_flagged_draft):
    # no contact/LinkedIn re-check, so a smaller budget covers it.
    RECHECK_WEB_SEARCH_MAX_USES: int = 6
    # The removal-mode revision round only: a few searches so it can REPLACE
    # a false claim with one it just verified instead of only cutting it
    # (cutting is how a draft loses its hook). Round-1 revisions stay
    # no-web — the flag notes and research items already carry the truth
    # there, and the recheck above independently gates whatever this round
    # writes either way.
    REVISE_WEB_SEARCH_MAX_USES: int = 3
    # 15 minutes (the global per-call ceiling): independent verification of
    # every claim runs more searches than the old spot-check did, and each
    # search is a think+read round.
    VERIFY_CALL_TIMEOUT_SECONDS: int = 15 * 60
    LLM_MAX_TOKENS: int = 16000
    # Each continuation resends the whole growing conversation so far as
    # input tokens, so keep this low — 2 means at most 3 requests per
    # ask_json() call.
    MAX_PAUSE_TURN_CONTINUATIONS: int = 2
    # No-progress watchdog for a single streamed request: abandon the call
    # when the stream delivers NO events for this long. Even mid-search the
    # stream stays chatty (thinking deltas, block starts, tool results land
    # within seconds of each other), so a 2-minute silence means the request
    # is wedged — better to fail fast into the step's timeout salvage
    # (smaller-budget retry) than ride the full wall-clock ceiling below:
    # a real run burned 2x480s on one company's stalled research calls.
    LLM_STALL_TIMEOUT_SECONDS: int = 120
    # Safety valve for a single LLM call. Web-search-heavy contact searches
    # have run 20+ minutes as ONE streaming request on hard-to-verify
    # companies (the stream keeps delivering thinking, so no HTTP timeout
    # ever fires) — bound each call so a marathon company drops itself with
    # a clear reason instead of squatting until the run-wide timeout kills
    # everything. Applies per request, including its rate-limit retries.
    LLM_CALL_TIMEOUT_SECONDS: int = 15 * 60

    # Connection-outage salvage. An APIConnectionError means the request
    # never reached the API (or lost it mid-flight) — in a real run that was
    # a local network/DNS outage ("getaddrinfo failed"), and it dropped FIVE
    # companies in four seconds as "unexpected error", all with verified
    # contacts already paid for. Instead of failing the call on the first
    # connection error, ask_json waits for connectivity to come back
    # (probing DNS every CONNECTION_PROBE_INTERVAL_SECONDS, for up to
    # CONNECTION_WAIT_SECONDS per outage — this wait does NOT count against
    # the call's timeout) and retries the request, up to
    # CONNECTION_ERROR_MAX_RETRIES times per call. Only when the network
    # stays down longer than the wait window does the call fail, with a
    # clear "check your connection" reason instead of a raw traceback.
    CONNECTION_ERROR_MAX_RETRIES: int = 3
    CONNECTION_WAIT_SECONDS: int = 120
    CONNECTION_PROBE_INTERVAL_SECONDS: int = 5

    # Global cap on in-flight LLM requests across the whole run. Companies
    # still run their step chains concurrently, but only this many streamed
    # requests execute at once — the rest queue (queue time does NOT count
    # against the call's timeout). Rationale for capping: a mid-stream 429
    # throws away the whole partial generation and restarts the call,
    # backoff burns the per-call clock, and logged runs showed late-run
    # calls starved into their 8-minute timeouts (one 3-search retry took
    # 414s). Currently 30 — effectively UNCAPPED (a run tops out around
    # max_companies concurrent chains, ~10). Search-enabled calls are
    # additionally throttled by LLM_MAX_CONCURRENT_SEARCH_CALLS below,
    # which is the cap that actually binds in practice.
    LLM_MAX_CONCURRENT_CALLS: int = 30

    # Separate, tighter cap on in-flight calls that USE WEB SEARCH (contact
    # identification, research, discovery, fact-check). Web search has its
    # own org-level searches-per-minute rate limit at Anthropic, and its
    # failures arrive IN-BAND (an error object inside a 200 response — see
    # llm._web_search_error_code), so the HTTP-429 backoff never absorbs
    # them. A real run with ~15-20 concurrent search-enabled calls had
    # nearly every research pass starved by "server tool use limit
    # exceeded" errors: models burned minutes retrying, and several drafts
    # were written from one research pass instead of two — a quality loss,
    # not just a speed one. Capping concurrency keeps the run inside the
    # per-minute search budget: queued calls start later but their searches
    # SUCCEED (queue time doesn't count against the call timeout), a net
    # win for both accuracy and wall-clock. Cheap non-search calls
    # (drafting, revisions) never touch this cap. Tune with the log: grep
    # "web search failed" after a run — errors clustering means lower it;
    # zero errors means it has headroom to go up.
    LLM_MAX_CONCURRENT_SEARCH_CALLS: int = 6

    # ---- pipeline caps ----
    MAX_COMPANIES_HARD_CAP: int = 30   # absolute cap per run
    DEFAULT_MAX_COMPANIES: int = 10    # beta default
    # Discovery over-fetches this many companies beyond the candidate's
    # max_companies. The extras form a bench: when a company drops mid-run
    # (contact unverifiable, no email, timeout), the next bench company
    # starts in its place, so failures don't shrink the number of drafts
    # delivered.
    DISCOVERY_BENCH_EXTRA: int = 5
    # All companies in a run advance their step chains concurrently; actual
    # LLM requests are throttled by LLM_MAX_CONCURRENT_CALLS above, and
    # whatever still slips through to a 429 is absorbed by llm.py's
    # retry/backoff.
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
    # button was clicked. 50 minutes since independent draft verification
    # (bigger search budget, 15-minute ceiling, web recheck after a claim
    # revision) lengthened each company's chain — earlier, 40 was chosen
    # when a real run was cut at 32 minutes with drafts still in flight.
    # Env-overridable.
    TIMEOUT_BUTTON_AFTER_SECONDS: int = 10 * 60
    RUN_HARD_TIMEOUT_SECONDS: int = 50 * 60

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
            LOG_LEVEL=os.environ.get("LOG_LEVEL", "DEBUG").strip().upper(),
            # Overridable so the test runner can point the whole app (SQLite
            # DB, resumes, tokens) at a throwaway copy of data/ — the suite
            # used to run against the REAL database, and test writes/cleanup
            # polluted and damaged real user data.
            DATA_DIR=Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data")),
            LOG_DIR=Path(os.environ.get("LOG_DIR") or (BASE_DIR / "data" / "logs")),
            CONTACT_CALL_TIMEOUT_SECONDS=_env_int(
                "CONTACT_CALL_TIMEOUT_SECONDS", 8 * 60, minimum=60
            ),
            CONTACT_LEADS_LIMIT=_env_int("CONTACT_LEADS_LIMIT", 10, minimum=0),
            RESEARCH_CALL_TIMEOUT_SECONDS=_env_int(
                "RESEARCH_CALL_TIMEOUT_SECONDS", 8 * 60, minimum=60
            ),
            VERIFY_CALL_TIMEOUT_SECONDS=_env_int(
                "VERIFY_CALL_TIMEOUT_SECONDS", 15 * 60, minimum=60
            ),
            LLM_CALL_TIMEOUT_SECONDS=_env_int(
                "LLM_CALL_TIMEOUT_SECONDS", 15 * 60, minimum=60
            ),
            CONNECTION_ERROR_MAX_RETRIES=_env_int(
                "CONNECTION_ERROR_MAX_RETRIES", 3, minimum=0
            ),
            CONNECTION_WAIT_SECONDS=_env_int(
                "CONNECTION_WAIT_SECONDS", 120, minimum=1
            ),
            CONNECTION_PROBE_INTERVAL_SECONDS=_env_int(
                "CONNECTION_PROBE_INTERVAL_SECONDS", 5, minimum=1
            ),
            LLM_STALL_TIMEOUT_SECONDS=_env_int(
                "LLM_STALL_TIMEOUT_SECONDS", 120, minimum=10
            ),
            LLM_MAX_CONCURRENT_CALLS=_env_int(
                "LLM_MAX_CONCURRENT_CALLS", 30, minimum=1
            ),
            LLM_MAX_CONCURRENT_SEARCH_CALLS=_env_int(
                "LLM_MAX_CONCURRENT_SEARCH_CALLS", 6, minimum=1
            ),
            RUN_HARD_TIMEOUT_SECONDS=_env_int(
                "RUN_HARD_TIMEOUT_SECONDS", 50 * 60, minimum=60
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
