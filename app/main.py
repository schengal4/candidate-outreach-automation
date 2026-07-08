"""FastAPI + HTMX prototype UI.

Run with:  python -m uvicorn app.main:app --reload
Requires:  ANTHROPIC_API_KEY and HUNTER_API_KEY in the environment.

Pipeline logic lives in app/pipeline.py and is UI-agnostic, so this layer can
be swapped for React/Next.js later.
"""

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# Configure once for the whole app (uvicorn's own loggers are separate).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app.main")

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, backup, gmail_client, run_store, sent_list, storage
from .auth import LoginError
from .config import (
    DEFAULT_MAX_COMPANIES,
    DEFAULT_RETENTION_MONTHS,
    LOGIN_REQUIRED,
    MAX_COMPANIES_HARD_CAP,
    MAX_RUNS_PER_DAY,
    RETENTION_MAX,
    RETENTION_MIN,
    SESSION_MAX_AGE_SECONDS,
    SESSION_SECRET,
    TIMEOUT_BUTTON_AFTER_SECONDS,
)
from .fsutil import atomic_write_bytes
from .gmail_client import GmailNotConfigured
from .models import Candidate, CompanyStatus, RunPhase, RunState
from .pipeline import RUNS, run_discovery, run_pipeline
from .resume import ensure_resume_pdf, extract_docx_text, resume_docx_path, resume_pdf_path

app = FastAPI(title="Candidate Outreach Automation")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Daily-ish safety copy of data/ (candidates, sent lists, runs, tokens).
# Never raises — see app/backup.py.
backup.backup_data_dir()

# ------------------------------------------------------------------ #
# Login wall (Google Sign-In — see app/auth.py). The guard is defined
# BEFORE SessionMiddleware is added: Starlette runs the last-added
# middleware outermost, so adding SessionMiddleware second guarantees
# request.session exists by the time the guard runs.
# ------------------------------------------------------------------ #
_PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/favicon.ico"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if (
        LOGIN_REQUIRED
        and request.url.path not in _PUBLIC_PATHS
        and not request.session.get("user_email")
    ):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=SESSION_MAX_AGE_SECONDS,
    same_site="lax",
)

# Keep references to background tasks so they aren't garbage collected mid-run.
_background_tasks: set = set()


def _on_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        # Pipeline code catches its own errors into run state; anything that
        # reaches here would otherwise disappear without a trace.
        logger.error("Background task %s failed", task.get_name(), exc_info=exc)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)


def _candidate_not_found() -> HTMLResponse:
    return HTMLResponse("<p>Candidate not found. <a href='/'>Back</a></p>", status_code=404)


# ------------------------------------------------------------------ #
# Per-account data isolation. Every candidate belongs to the Google
# account that created it; all lookups below scope to the logged-in user.
# ------------------------------------------------------------------ #
def _session_owner(request: Request) -> Optional[str]:
    """The login email data is scoped to — None in open mode (REQUIRE_LOGIN=0),
    where the app intentionally behaves like the original single-user version."""
    if not LOGIN_REQUIRED:
        return None
    return str(request.session.get("user_email", "")).strip().lower()


def _owned_candidate(request: Request, candidate_id: str) -> Optional[Candidate]:
    """The candidate, but only if the logged-in user owns it.

    A candidate owned by someone else returns None — indistinguishable from
    not-found, so candidate IDs can't be probed for existence."""
    candidate = storage.get_candidate(candidate_id)
    if not candidate:
        return None
    owner = _session_owner(request)
    if owner is not None and candidate.owner_email.strip().lower() != owner:
        return None
    return candidate


def _owned_run(request: Request, run_id: str) -> Tuple[Optional[RunState], Optional[Candidate]]:
    """(run, candidate) for a run the logged-in user owns, else (None, None).
    Runs have no owner of their own — they inherit the candidate's."""
    run = RUNS.get(run_id)
    if not run:
        return None, None
    candidate = _owned_candidate(request, run.candidate_id)
    if not candidate:
        return None, None
    return run, candidate


# ------------------------------------------------------------------ #
# Home / candidates
# ------------------------------------------------------------------ #
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    owner = _session_owner(request)
    candidates = storage.list_candidates(owner_email=owner)
    # One account is one job-seeker profile: with exactly one candidate, the
    # home page IS that profile. Zero shows the setup form; two or more (a
    # legacy state, or open mode where the app is deliberately multi-profile)
    # falls back to the original table.
    if owner is not None and len(candidates) == 1:
        return RedirectResponse(f"/candidates/{candidates[0].id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "candidates": candidates,
            "single_profile_mode": owner is not None,
            "defaults": {
                "max_companies": DEFAULT_MAX_COMPANIES,
                "hard_cap": MAX_COMPANIES_HARD_CAP,
                "retention": DEFAULT_RETENTION_MONTHS,
                "retention_min": RETENTION_MIN,
                "retention_max": RETENTION_MAX,
            },
        },
    )


@app.post("/candidates")
async def create_candidate(
    request: Request,
    resume: UploadFile,
    name: str = Form(...),
    email: str = Form(""),
    current_employer: str = Form(...),
    linkedin_url: str = Form(""),
    career_goals: str = Form(""),
    culture_prefs: str = Form(""),
    target_industry_role: str = Form(""),
    draft_instructions: str = Form(""),
    red_flag_detection: bool = Form(False),
    retention_months: int = Form(DEFAULT_RETENTION_MONTHS),
    max_companies: int = Form(DEFAULT_MAX_COMPANIES),
):
    # One profile per account (when login is on): a second create just lands
    # on the existing profile instead of quietly making a duplicate.
    owner = _session_owner(request)
    if owner is not None:
        existing = storage.list_candidates(owner_email=owner)
        if existing:
            return RedirectResponse(f"/candidates/{existing[0].id}", status_code=303)

    data = await resume.read()
    try:
        resume_text = extract_docx_text(data)
    except Exception:
        return HTMLResponse(
            "<p>Could not read that resume. Please upload a .docx file. "
            "<a href='/'>Back</a></p>",
            status_code=400,
        )

    candidate = Candidate(
        id=Candidate.new_id(),
        name=name.strip(),
        email=email.strip(),
        owner_email=_session_owner(request) or "",
        current_employer=current_employer.strip(),
        resume_text=resume_text,
        resume_filename=resume.filename or "resume.docx",
        linkedin_url=linkedin_url.strip(),
        career_goals=career_goals.strip(),
        culture_prefs=culture_prefs.strip(),
        target_industry_role=target_industry_role.strip(),
        draft_instructions=draft_instructions.strip(),
        red_flag_detection=bool(red_flag_detection),
        retention_months=max(RETENTION_MIN, min(RETENTION_MAX, retention_months)),
        max_companies=max(1, min(MAX_COMPANIES_HARD_CAP, max_companies)),
    )
    atomic_write_bytes(resume_docx_path(candidate), data)
    storage.save_candidate(candidate)
    # Cache the PDF render now (same file every draft, so convert once here,
    # not per-email). Best-effort: on failure, draft creation retries the
    # conversion and surfaces the error next to the draft instead.
    try:
        await asyncio.to_thread(ensure_resume_pdf, candidate)
    except Exception:
        pass
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
async def candidate_page(request: Request, candidate_id: str):
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    past_runs = [
        {
            "id": r.id,
            "when": datetime.fromtimestamp(r.created_at).strftime("%b %d, %Y %I:%M %p"),
            "phase": r.phase,
            "drafts": sum(1 for co in r.companies if co.status == CompanyStatus.DONE),
            "companies": len(r.companies) or len(r.discovered),
        }
        for r in sorted(
            (r for r in RUNS.values() if r.candidate_id == candidate_id),
            key=lambda r: r.created_at,
            reverse=True,
        )
    ]
    return templates.TemplateResponse(
        request,
        "candidate.html",
        {
            "c": candidate,
            "pending": sent_list.pending_confirmation(candidate_id),
            "gmail_connected": gmail_client.is_connected(candidate_id),
            "past_runs": past_runs,
        },
    )


@app.post("/candidates/{candidate_id}/delete")
async def delete_candidate(request: Request, candidate_id: str):
    """Remove a profile and everything belonging to it: the record, resume
    files, the Gmail grant (revoked at Google), the sent list, and any
    in-memory runs. Irreversible — the UI confirms before posting here."""
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    # Best-effort revoke + token file removal; blocking network call to
    # Google (up to its 10s timeout) — keep it off the event loop.
    await asyncio.to_thread(gmail_client.disconnect, candidate_id)
    for path in (resume_docx_path(candidate), resume_pdf_path(candidate)):
        if path.exists():
            path.unlink()
    sent_list.delete_list(candidate_id)
    run_store.delete_candidate_runs(candidate_id, RUNS)
    storage.delete_candidate(candidate_id)
    return RedirectResponse("/", status_code=303)


@app.post("/candidates/{candidate_id}/draft_instructions")
async def update_draft_instructions(
    request: Request, candidate_id: str, draft_instructions: str = Form("")
):
    """Edit the per-candidate email drafting instructions/template any time —
    the only candidate field editable after creation, since it's the one users
    iterate on between runs as they see real drafts."""
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    candidate.draft_instructions = draft_instructions.strip()
    storage.save_candidate(candidate)
    return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)


# ------------------------------------------------------------------ #
# App login (Google Sign-In — identity only; see app/auth.py)
# ------------------------------------------------------------------ #
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not LOGIN_REQUIRED or request.session.get("user_email"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": request.session.pop("login_error", "")}
    )


@app.get("/auth/login")
async def auth_login(request: Request):
    if not LOGIN_REQUIRED:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(auth.build_login_url())


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state:
        request.session["login_error"] = f"Sign-in was not completed ({error or 'no code returned'})."
        return RedirectResponse("/login", status_code=303)
    try:
        # Blocking network round-trips to Google (token exchange + cert
        # fetch) — off the event loop so in-flight requests (e.g. run-panel
        # polls) don't freeze behind a slow Google response.
        user = await asyncio.to_thread(auth.handle_login_callback, code, state)
    except LoginError as exc:
        request.session["login_error"] = str(exc)
        return RedirectResponse("/login", status_code=303)
    request.session["user_email"] = user["email"]
    request.session["user_name"] = user["name"]
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ #
# Gmail (drafts only — see app/gmail_client.py for the compliance notes)
# ------------------------------------------------------------------ #
@app.get("/candidates/{candidate_id}/gmail/connect", response_class=HTMLResponse)
async def gmail_connect_explainer(request: Request, candidate_id: str):
    """Our own consent step, shown before handing off to Google's own screen."""
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    return templates.TemplateResponse(request, "gmail_connect.html", {"c": candidate})


@app.get("/candidates/{candidate_id}/gmail/authorize")
async def gmail_authorize(request: Request, candidate_id: str):
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    try:
        return RedirectResponse(gmail_client.build_auth_url(candidate_id))
    except GmailNotConfigured as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=500)


@app.get("/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state:
        return HTMLResponse(f"<p>Gmail connection was not completed ({error or 'no code returned'}).</p>")
    # `state` is the candidate_id — only store the token if the logged-in
    # user owns that candidate (blocks token-planting via a forged callback).
    if not _owned_candidate(request, state):
        return _candidate_not_found()
    # Blocking token exchange with Google — keep it off the event loop.
    await asyncio.to_thread(gmail_client.handle_oauth_callback, code, candidate_id=state)
    return RedirectResponse(f"/candidates/{state}", status_code=303)


@app.post("/candidates/{candidate_id}/gmail/disconnect")
async def gmail_disconnect(request: Request, candidate_id: str):
    if not _owned_candidate(request, candidate_id):
        return _candidate_not_found()
    # Blocking revoke call to Google (up to its 10s timeout) — off the loop.
    await asyncio.to_thread(gmail_client.disconnect, candidate_id)
    return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)


# ------------------------------------------------------------------ #
# Runs
# ------------------------------------------------------------------ #
@app.post("/candidates/{candidate_id}/runs")
async def start_run(request: Request, candidate_id: str):
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    # Cost guardrail: every run spends real money on the app owner's API
    # keys, so cap runs per profile per rolling 24h. Counted from persisted
    # runs (run_store keeps at least MAX_RUNS_PER_DAY per candidate).
    runs_today = sum(
        1 for r in RUNS.values()
        if r.candidate_id == candidate_id and (time.time() - r.created_at) < 24 * 3600
    )
    if runs_today >= MAX_RUNS_PER_DAY:
        return HTMLResponse(
            f"<p>Daily run limit reached ({MAX_RUNS_PER_DAY} per profile per 24 hours) — "
            f"each run costs real API money, so this is capped. Try again later. "
            f"<a href='/candidates/{candidate_id}'>Back</a></p>",
            status_code=429,
        )
    run = RunState(id=RunState.new_id(), candidate_id=candidate_id)
    RUNS[run.id] = run
    run_store.save_run(run)
    # Starting a new run is when old ones age out (keep the most recent few).
    run_store.prune_candidate_runs(candidate_id, RUNS)
    _spawn(run_discovery(run, candidate))
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str):
    run, candidate = _owned_run(request, run_id)
    if not run:
        return HTMLResponse("<p>Run not found. <a href='/'>Back</a></p>", status_code=404)
    return templates.TemplateResponse(
        request, "run.html", {"run": run, "c": candidate}
    )


@app.get("/runs/{run_id}/panel", response_class=HTMLResponse)
async def run_panel(request: Request, run_id: str):
    run, candidate = _owned_run(request, run_id)
    if not run:
        return HTMLResponse("<p>Run not found.</p>", status_code=286)
    show_timeout_button = (
        run.phase == RunPhase.RUNNING
        and run.started_running_at is not None
        and (time.time() - run.started_running_at) >= TIMEOUT_BUTTON_AFTER_SECONDS
    )
    response = templates.TemplateResponse(
        request,
        "_run_panel.html",
        {
            "run": run,
            "c": candidate,
            "gmail_connected": candidate and gmail_client.is_connected(candidate.id),
            "show_timeout_button": show_timeout_button,
        },
    )
    # HTTP 286 tells HTMX to stop polling once we've reached a stable phase.
    if run.phase in (RunPhase.REVIEW, RunPhase.DONE, RunPhase.ERROR):
        response.status_code = 286
    return response


@app.post("/runs/{run_id}/stop_early")
async def stop_run_early(request: Request, run_id: str):
    """Triggered by the "retrieve what's done" button — cuts a running
    pipeline short at the next check, keeping whatever already finished."""
    run, _ = _owned_run(request, run_id)
    if run and run.phase == RunPhase.RUNNING:
        run.stop_event.set()
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/companies/{index}/save_to_gmail")
async def save_to_gmail(request: Request, run_id: str, index: int):
    run, candidate = _owned_run(request, run_id)
    if not run or not (0 <= index < len(run.companies)):
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    company = run.companies[index]
    if candidate and company.draft_subject:
        try:
            await gmail_client.create_draft(candidate, company)
            company.gmail_draft_created = True
            company.gmail_error = ""
        except Exception as exc:
            company.gmail_error = str(exc)
        run_store.save_run(run)  # the saved-to-Gmail marks survive restarts too
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/save_all_to_gmail")
async def save_all_to_gmail(request: Request, run_id: str):
    """Batch version of save_to_gmail — one explicit click after all drafts in
    the run are visible on screen. Per-draft failures don't stop the rest."""
    run, candidate = _owned_run(request, run_id)
    if not run:
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    if candidate:
        for company in run.companies:
            if company.draft_subject and not company.gmail_draft_created:
                try:
                    await gmail_client.create_draft(candidate, company)
                    company.gmail_draft_created = True
                    company.gmail_error = ""
                except Exception as exc:
                    company.gmail_error = str(exc)
        run_store.save_run(run)  # the saved-to-Gmail marks survive restarts too
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/approve")
async def approve_companies(request: Request, run_id: str):
    run, candidate = _owned_run(request, run_id)
    if not run or run.phase != RunPhase.REVIEW:
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    form = await request.form()
    approved = form.getlist("approved")
    if not approved:
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    _spawn(run_pipeline(run, candidate, [str(d) for d in approved]))
    run.phase = RunPhase.RUNNING  # flip immediately so the page starts polling
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ------------------------------------------------------------------ #
# Sent list
# ------------------------------------------------------------------ #
@app.get("/sent/{candidate_id}", response_class=HTMLResponse)
async def sent_page(request: Request, candidate_id: str):
    candidate = _owned_candidate(request, candidate_id)
    if not candidate:
        return _candidate_not_found()
    return templates.TemplateResponse(
        request,
        "sent_list.html",
        {
            "c": candidate,
            "entries": sent_list.load_entries(candidate_id),
        },
    )


@app.post("/sent/{candidate_id}/add")
async def sent_add(
    request: Request,
    candidate_id: str,
    contact_email: str = Form(...),
    company_domain: str = Form(""),
    contact_name: str = Form(""),
    date_sent: str = Form(""),
    permanently_excluded: bool = Form(False),
):
    """Manual entry for a contact reached outside the app (LinkedIn, in person,
    another tool) so future runs treat them as recently contacted."""
    if _owned_candidate(request, candidate_id) and contact_email.strip():
        sent_list.add_entry(
            candidate_id,
            company_domain.strip(),
            contact_name.strip(),
            contact_email.strip(),
            date_sent=date_sent.strip(),
            confirmed_sent=True,  # already sent — no reconciliation nudge needed
            permanently_excluded=bool(permanently_excluded),
        )
    return RedirectResponse(f"/sent/{candidate_id}", status_code=303)


@app.post("/sent/{candidate_id}/{index}/update")
async def sent_update(
    request: Request,
    candidate_id: str,
    index: int,
    contact_email: str = Form(None),
    date_sent: str = Form(None),
    action: str = Form(...),
):
    if not _owned_candidate(request, candidate_id):
        return _candidate_not_found()
    if action == "delete":
        sent_list.remove_entry(candidate_id, index)
    elif action == "toggle_interview":
        entries = sent_list.load_entries(candidate_id)
        if 0 <= index < len(entries):
            sent_list.update_entry(
                candidate_id, index,
                interview_arranged=not entries[index]["interview_arranged"],
            )
    elif action == "toggle_permanent":
        entries = sent_list.load_entries(candidate_id)
        if 0 <= index < len(entries):
            sent_list.update_entry(
                candidate_id, index,
                permanently_excluded=not entries[index]["permanently_excluded"],
            )
    elif action == "confirm_sent":
        sent_list.update_entry(candidate_id, index, confirmed_sent=True)
    elif action == "save":
        changes = {}
        if contact_email is not None:
            changes["contact_email"] = contact_email.strip()
        if date_sent:
            changes["date_sent"] = date_sent.strip()
        if changes:
            sent_list.update_entry(candidate_id, index, **changes)
    return RedirectResponse(f"/sent/{candidate_id}", status_code=303)


@app.post("/sent/{candidate_id}/confirm_all")
async def sent_confirm_all(request: Request, candidate_id: str):
    if not _owned_candidate(request, candidate_id):
        return _candidate_not_found()
    entries = sent_list.load_entries(candidate_id)
    for e in entries:
        e["confirmed_sent"] = True
    sent_list.save_entries(candidate_id, entries)
    return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)


@app.post("/sent/{candidate_id}/prune")
async def sent_prune(request: Request, candidate_id: str):
    candidate = _owned_candidate(request, candidate_id)
    if candidate:
        sent_list.prune_expired(candidate_id, candidate.retention_months)
    return RedirectResponse(f"/sent/{candidate_id}", status_code=303)
