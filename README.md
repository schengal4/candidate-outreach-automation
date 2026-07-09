# Candidate Outreach Automation (V1)

Implements the pipeline in [plan.md](plan.md): LLM company discovery → candidate
review gate → contact identification (primary + backup, employment verified with
dated evidence) → gated Hunter email lookup → personalization research (optional
red-flag detection) → personalized draft with a plain sign-off → per-candidate
Sent List (CSV) with retention-window exclusion and reconciliation nudge.

## Setup

```powershell
# from this directory, with the .env virtualenv active
.\.env\Scripts\Activate.ps1
pip install -r requirements.txt

# API keys (new terminals pick these up after setx; or set per-session)
$env:HUNTER_API_KEY    = "your_hunter_key"
$env:ANTHROPIC_API_KEY = "your_anthropic_key"

# Optional: Gmail draft integration (see "Gmail integration" section below)
$env:GOOGLE_CLIENT_ID     = "your_client_id.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET = "your_client_secret"
```

## Run

```powershell
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## Flow

1. **Create a candidate** — upload resume (.docx, required), current employer
   (required, never targeted), optional LinkedIn/goals/culture/target role,
   optional email drafting instructions or a template (style preferences,
   phrases to use/avoid — editable later from the candidate page; where they
   conflict with the built-in style rules, the candidate's instructions win,
   but hard rules like "no false claims" always apply), and settings
   (companies per run, retention months 6–18, red-flag toggle).
2. **Start a run** — discovery runs with Claude + web search, then pauses at the
   **review gate**: deselect any companies before research proceeds. Live progress
   text (thinking snippets, "searching the web…") is shown while each step runs,
   since a single web-search-heavy call can take a couple of minutes.
3. **Pipeline** — approved companies all run concurrently with no cap (steps
   2→5 sequential within each company); Hunter calls are still capped by a
   semaphore of 10 (within Hunter's own rate limit). Claude calls have no
   local cap — a burst of simultaneous requests is expected and 429/overload
   responses are retried with backoff (see `app/llm.py`) rather than dropping
   the company. A "stop now & view emails completed so far" button appears
   on the run page after 10 minutes; the run also stops itself automatically
   after 30 minutes regardless, keeping whatever companies already finished.
4. **Results** — per company: contact used + employment evidence, email +
   Hunter score, research summary, red flags (if enabled, shown alongside the
   draft — your call), draft with one-click copy. A plain sign-off (name, email,
   LinkedIn if provided) is appended automatically and isn't editable — no
   "unsubscribe" language, since this is one-to-one personal correspondence,
   not bulk mail.
5. **Sent List** — drafts are auto-added (`date_sent` = generation date). On
   your next visit the candidate page nudges "Did you send to these?". Entries
   are editable/removable any time; "Interview Arranged" is a manual checkbox.
   Contacts reached *outside* the app (LinkedIn, in person, another tool) can
   be added manually from the Sent List page so future runs don't resurface
   them. If a contact asks not to be contacted again, the "Never contact"
   toggle excludes them permanently — it overrides the retention window
   regardless of date sent and survives pruning.
6. **Gmail (optional)** — each candidate can connect their own Gmail account
   from their candidate page. Once connected, every completed draft in a run
   report shows a "Save to Gmail" button that creates it directly in that
   candidate's Gmail Drafts folder — nothing is ever sent automatically.
   Each draft has the candidate's resume attached as a PDF (converted from the
   uploaded .docx once, at upload time, using the locally installed MS Word —
   see `app/pdf_convert.py`; requires Word on the host machine). A "Save all …
   remaining drafts" button appears at the bottom of a finished run report when
   two or more drafts haven't been saved yet — after all drafts have been on
   screen for review.

## Gmail integration

Drafts-only, per-candidate, opt-in. Before it'll work you need your own Google
Cloud OAuth credentials — this app can't create those for you:

1. Go to [console.cloud.google.com](https://console.cloud.google.com/), create
   or select a project.
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen** → configure it (External is fine
   for personal use), add the scope `.../auth/gmail.compose`, and add your own
   Google account under **Test users** (while the app is in "Testing" status,
   only test users can complete the OAuth flow — up to 100 without needing
   Google's app verification process).
4. **APIs & Services → Credentials** → **Create Credentials → OAuth client ID**
   → Application type **Web application** → add **both** of these as
   **Authorized redirect URIs** (each must match its config value exactly,
   including port):
   - `http://localhost:8000/gmail/callback` (Gmail drafts — `GOOGLE_REDIRECT_URI`)
   - `http://localhost:8000/auth/callback` (app sign-in — `GOOGLE_LOGIN_REDIRECT_URI`)
5. Copy the Client ID and Client Secret into `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET` (see Setup above).

## App sign-in (Google)

Setting `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` also turns on a login wall
for the whole app: every page redirects to `/login` until you sign in with
Google. This is identity only (`openid email profile` scopes) — a separate,
narrower OAuth flow from the per-candidate `gmail.compose` drafts connection,
sharing the same OAuth client. See `app/auth.py`.

- **Who can sign in:** while the OAuth app is in "Testing" status, Google only
  lets the Test users you added complete the flow. On top of that, set
  `ALLOWED_LOGIN_EMAILS` (comma-separated) to enforce an app-side allowlist.
- **Data isolation:** every candidate belongs to the Google account that
  created it (`owner_email` on the candidate record). The home page lists only
  your candidates, and every candidate-scoped page and action (candidate page,
  runs, sent list, Gmail connect/save) returns 404 for anyone else's — another
  account's candidate looks identical to one that doesn't exist.
- **One profile per account:** a signed-in account holds exactly one candidate
  profile. With no profile, the home page is the setup form; with one, it
  redirects straight to that profile; creating a second just lands on the
  existing one. (Accounts with multiple profiles from before this rule see the
  old table until they delete down to one.) "Delete this profile" on the
  candidate page removes everything belonging to it: the record, resume files,
  the Gmail grant (revoked at Google), and the entire sent list. Open mode
  (`REQUIRE_LOGIN=0`) keeps the original multi-candidate behavior.
- **Opt out:** set `REQUIRE_LOGIN=0` to run open (the pre-login behavior),
  e.g. if you use the app purely locally and don't want the wall.
- **Sessions:** signed cookies (14-day expiry). The signing secret is
  generated once into `data/session_secret` (gitignored); set `SESSION_SECRET`
  to override.

**Compliance notes:**
- The only scope requested is `gmail.compose` — the narrowest scope Google
  offers that can create drafts. It technically also permits sending, but
  `app/gmail_client.py` never calls the send endpoint — the "never sends
  automatically" guarantee is enforced by our code, not by the OAuth grant.
- Connecting shows the app's own plain-language consent page
  (`app/templates/gmail_connect.html`) *before* handing off to Google's own
  consent screen.
- OAuth tokens are stored one file per candidate under `data/gmail_tokens/`,
  separate from `candidates.json`. Disconnecting (from the candidate page)
  revokes the grant at Google, not just the local copy.
- `data/` (which now also holds these tokens) is gitignored — see `.gitignore`.
- Scaling past a handful of personal users would require Google's OAuth app
  verification (and likely a security assessment, since `gmail.compose` is a
  restricted/sensitive scope) — not needed for personal/beta use in Testing mode.

## Project structure

```
Automatic_Email_Generation/
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── models.py
│   ├── resume.py
│   ├── storage.py
│   ├── sent_list.py
│   ├── llm.py
│   ├── hunter_async.py
│   ├── pdf_convert.py
│   ├── gmail_client.py
│   ├── pipeline.py
│   ├── main.py
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── candidate.html
│       ├── gmail_connect.html
│       ├── run.html
│       ├── _run_panel.html
│       └── sent_list.html
├── data/
│   ├── candidates.json
│   ├── resumes/
│   ├── gmail_tokens/
│   └── sent_list_<id>.csv
├── hunter_client.py
├── Hunter_API_Documentation.md
├── Email_Target_Identification.py
├── plan.md
├── requirements.txt
├── README.md
├── .gitignore
└── .claude/launch.json
```

### What each piece does

**`app/` — the actual application**

| File | What it does |
|---|---|
| `__init__.py` | Empty — just marks this folder as a Python package. Nothing to see here. |
| `config.py` | All the tunable settings in one place: which Claude model to use, how many companies to research per run, rate limits, retention-window defaults. |
| `logging_setup.py` | Wires up app-wide logging: everything the app logs goes to the console **and** a rotating file at `data/logs/app.log` (so a run's history survives closing the terminal). Set the `LOG_LEVEL` env var (`DEBUG`, `INFO`, `WARNING`, `ERROR`) to change verbosity; default is `INFO`. |
| `models.py` | The "shapes" of the app's data — blank forms that get filled in as things run, e.g. what a Candidate looks like, what a Contact looks like, what a Run looks like. |
| `resume.py` | Opens an uploaded `.docx` resume and pulls the plain text out of it so Claude can read it. Also owns where resume files live on disk and keeps a cached PDF copy for email attachments. |
| `pdf_convert.py` | Converts the uploaded `.docx` resume to PDF using the locally installed MS Word (no LLM, no cloud service — a faithful render of the file). Windows-only; requires Word. |
| `auth.py` | Google Sign-In for the app itself (identity only — name and email). Separate flow from the Gmail drafts connection; powers the login wall in `main.py`. |
| `storage.py` | Saves and loads candidate profiles to/from a JSON file — the "candidate database." |
| `sent_list.py` | Tracks who's been contacted per candidate (the CSV "Sent List"): who, when, whether they confirmed sending, whether an interview happened, and enforces the "don't recontact the same person within X months" rule. |
| `llm.py` | Talks to Claude: sends requests, lets it search the web, and streams back live "what am I doing right now" updates so the UI doesn't look frozen during a long web-search call. |
| `hunter_async.py` | Wraps the pre-existing Hunter.io email-lookup client so several lookups can run at the same time without overloading Hunter's rate limit. |
| `gmail_client.py` | Handles the Gmail OAuth flow and creates drafts in a connected candidate's Gmail account. Drafts only — never calls the send endpoint. |
| `pipeline.py` | The brain of the app — runs the actual outreach process step by step: find companies → find contacts at each company → verify they still work there → find their email → research them → write a personalized email. |
| `main.py` | The web server — defines every page/URL (create candidate, view a run, view sent list, etc.) and connects user clicks to the pipeline logic above. |

**`app/templates/` — the actual HTML pages the browser shows**

| File | What it does |
|---|---|
| `base.html` | The shared page frame every other page builds on — header, styling, the "copy to clipboard" button logic. |
| `index.html` | Home page: the list of candidates plus the "add a new candidate" form. |
| `candidate.html` | One candidate's page: their info, the "did you send these?" reminder, Gmail connection status, and the button to start a new outreach run. |
| `gmail_connect.html` | The app's own plain-language consent page, shown before handing off to Google's OAuth consent screen. |
| `run.html` | The page you land on right after starting a run — just holds a placeholder that keeps asking the server for updates. |
| `_run_panel.html` | The part that actually refreshes every few seconds: discovery progress, the company review checklist, live per-company status while running, and the final report with drafted emails. |
| `sent_list.html` | The editable table of everyone you've reached out to, including the "interview arranged" checkbox. |

**Everything else**

| File/folder | What it does |
|---|---|
| `data/` | The app's storage — created automatically the first time you run it. Holds `candidates.json` (all saved candidate profiles), `resumes/` (uploaded resume files), `gmail_tokens/` (one OAuth token file per connected candidate), one `sent_list_<id>.csv` per candidate, and `logs/` (the app's log files, rotated). Gitignored. |
| `.gitignore` | Excludes `data/` (personal info + OAuth tokens) and the `.env/` virtualenv from git. |
| `hunter_client.py` | Pre-existing helper that knows how to call the Hunter.io API directly (look up an email, verify one, etc.) — `app/hunter_async.py` wraps this so the web app can use it without blocking. |
| `Hunter_API_Documentation.md` | Reference notes for the Hunter.io API. |
| `Email_Target_Identification.py` | An early exploratory script from before the web app existed. Not used by the running app anymore — kept for reference. |
| `plan.md` | The original build spec this whole project was built from. |
| `requirements.txt` | The list of Python packages needed to run the project. |
| `.claude/launch.json` | Tells Claude Code's preview tool how to start the dev server — only relevant if Claude Code is running it for you. |
| `.env/` *(not shown above)* | The Python virtual environment — an isolated Python install with the packages from `requirements.txt` already installed. Never edit anything inside it. |

## Notes / V2 hooks (deferred by design)

- Daily auto-run, Hunter Discover pre-filter, LinkedIn PDF upload for research,
  per-company resume tailoring, Outlook draft creation, caching — all V2.
  (Gmail draft creation is implemented — see "Gmail integration" above.)
- Runs persist to disk (`data/runs/`, one JSON per run, last 5 kept per
  profile) and reload on startup, so a restart doesn't lose finished reports —
  the profile page lists recent runs. A run caught mid-flight can't resume its
  LLM work: after a restart, companies already finished keep their drafts,
  in-flight ones are marked "interrupted by server restart", and a run parked
  at the review gate survives fully and can still be approved.
