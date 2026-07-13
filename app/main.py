"""FastAPI + HTMX prototype UI.

Run with:  python -m uvicorn app.main:app --reload
Requires:  ANTHROPIC_API_KEY and HUNTER_API_KEY in the environment.

Structure: create_app() is the factory; the module-level `app` at the bottom
keeps the uvicorn command above working. Importing this module has no heavy
side effects — logging setup, the startup backup, the legacy-data migration,
and reloading persisted runs all happen in the lifespan handler, which runs
when the server (or a TestClient used as a context manager) starts.

Pipeline logic lives in app/pipeline.py + app/steps.py and is UI-agnostic;
run lifecycle (spawning, stopping, the daily cap) is owned by the RunManager
(app/run_manager.py); per-account ownership checks are FastAPI dependencies
(app/deps.py), so this layer is just routes.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, backup, config, db, gmail_client, sent_list, storage
from .auth import LoginError
from .deps import NotFoundError, owned_candidate, owned_run, session_owner
from .fsutil import atomic_write_bytes
from .gmail_client import GmailNotConfigured
from .logging_setup import configure_logging
from .models import Candidate, CompanyStatus, RunPhase
from .resume import ensure_resume_pdf, extract_docx_text, resume_docx_path, resume_pdf_path
from .run_manager import manager

logger = logging.getLogger("app.main")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()

_PUBLIC_PATHS = {"/login", "/auth/login", "/auth/callback", "/favicon.ico"}


def _not_found_response(exc: NotFoundError) -> HTMLResponse:
    return HTMLResponse(
        f"<p>{exc.message} <a href='/'>Back</a></p>", status_code=404
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    # Configure once for the whole app (uvicorn's own loggers are separate) —
    # console + rotating file under data/logs/; see app/logging_setup.py.
    configure_logging()
    # Daily-ish safety copy of data/ (candidates, sent lists, runs, tokens).
    # Runs BEFORE the first DB connect so the pre-migration legacy files are
    # zipped away too. Never raises — see app/backup.py.
    backup.backup_data_dir()
    db.connect()  # creates the schema; imports legacy files on first run
    # Seed the run registry from disk so a restart doesn't lose finished
    # reports or a run parked at the review gate.
    manager.load_persisted()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Candidate Outreach Automation", lifespan=lifespan)

    # Login wall (Google Sign-In — see app/auth.py). The guard is registered
    # BEFORE SessionMiddleware is added: Starlette runs the last-added
    # middleware outermost, so adding SessionMiddleware second guarantees
    # request.session exists by the time the guard runs.
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if (
            config.settings.LOGIN_REQUIRED
            and request.url.path not in _PUBLIC_PATHS
            and not request.session.get("user_email")
        ):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret(),
        max_age=config.settings.SESSION_MAX_AGE_SECONDS,
        same_site="lax",
    )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return _not_found_response(exc)

    app.include_router(router)
    return app


# ------------------------------------------------------------------ #
# Home / candidates
# ------------------------------------------------------------------ #
@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    owner = session_owner(request)
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
                "max_companies": config.settings.DEFAULT_MAX_COMPANIES,
                "hard_cap": config.settings.MAX_COMPANIES_HARD_CAP,
                "retention": config.settings.DEFAULT_RETENTION_MONTHS,
                "retention_min": config.settings.RETENTION_MIN,
                "retention_max": config.settings.RETENTION_MAX,
            },
        },
    )


@router.post("/candidates")
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
    retention_months: Optional[int] = Form(None),
    max_companies: Optional[int] = Form(None),
):
    # One profile per account (when login is on): a second create just lands
    # on the existing profile instead of quietly making a duplicate.
    owner = session_owner(request)
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

    s = config.settings
    if retention_months is None:
        retention_months = s.DEFAULT_RETENTION_MONTHS
    if max_companies is None:
        max_companies = s.DEFAULT_MAX_COMPANIES
    candidate = Candidate(
        id=Candidate.new_id(),
        name=name.strip(),
        email=email.strip(),
        owner_email=owner or "",
        current_employer=current_employer.strip(),
        resume_text=resume_text,
        resume_filename=resume.filename or "resume.docx",
        linkedin_url=linkedin_url.strip(),
        career_goals=career_goals.strip(),
        culture_prefs=culture_prefs.strip(),
        target_industry_role=target_industry_role.strip(),
        draft_instructions=draft_instructions.strip(),
        red_flag_detection=bool(red_flag_detection),
        retention_months=max(s.RETENTION_MIN, min(s.RETENTION_MAX, retention_months)),
        max_companies=max(1, min(s.MAX_COMPANIES_HARD_CAP, max_companies)),
    )
    atomic_write_bytes(resume_docx_path(candidate), data)
    storage.save_candidate(candidate)
    logger.info("Created candidate %s (owner=%s)", candidate.id, candidate.owner_email or "-")
    # Cache the PDF render now (same file every draft, so convert once here,
    # not per-email). Best-effort: on failure, draft creation retries the
    # conversion and surfaces the error next to the draft instead.
    try:
        await asyncio.to_thread(ensure_resume_pdf, candidate)
    except Exception:
        logger.warning(
            "Resume PDF pre-conversion failed for candidate %s (draft creation will retry)",
            candidate.id, exc_info=True,
        )
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
async def candidate_page(request: Request, candidate: Candidate = Depends(owned_candidate)):
    past_runs = [
        {
            "id": r.id,
            "when": datetime.fromtimestamp(r.created_at).strftime("%b %d, %Y %I:%M %p"),
            "phase": r.phase,
            "drafts": sum(1 for co in r.companies if co.status == CompanyStatus.DONE),
            "companies": len(r.companies) or len(r.discovered),
        }
        for r in manager.for_candidate(candidate.id)
    ]
    return templates.TemplateResponse(
        request,
        "candidate.html",
        {
            "c": candidate,
            "pending": sent_list.pending_confirmation(candidate.id),
            "gmail_connected": gmail_client.is_connected(candidate.id),
            "past_runs": past_runs,
        },
    )


@router.post("/candidates/{candidate_id}/delete")
async def delete_candidate(candidate: Candidate = Depends(owned_candidate)):
    """Remove a profile and everything belonging to it: the record, resume
    files, the Gmail grant (revoked at Google), the sent list, and any
    in-memory runs. Irreversible — the UI confirms before posting here."""
    logger.info("Deleting candidate %s and all associated data", candidate.id)
    # Best-effort revoke + token file removal; blocking network call to
    # Google (up to its 10s timeout) — keep it off the event loop.
    await asyncio.to_thread(gmail_client.disconnect, candidate.id)
    for path in (resume_docx_path(candidate), resume_pdf_path(candidate)):
        if path.exists():
            path.unlink()
    sent_list.delete_list(candidate.id)
    manager.delete_candidate_runs(candidate.id)
    storage.delete_candidate(candidate.id)
    return RedirectResponse("/", status_code=303)


@router.post("/candidates/{candidate_id}/draft_instructions")
async def update_draft_instructions(
    candidate: Candidate = Depends(owned_candidate), draft_instructions: str = Form("")
):
    """Edit the per-candidate email drafting instructions/template any time —
    it's the field users iterate on between runs as they see real drafts."""
    candidate.draft_instructions = draft_instructions.strip()
    storage.save_candidate(candidate)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.post("/candidates/{candidate_id}/resume")
async def replace_resume(resume: UploadFile, candidate: Candidate = Depends(owned_candidate)):
    """Replace the candidate's resume with a newly uploaded .docx. Takes effect
    from the next run onward; drafts in existing runs keep the old text."""
    data = await resume.read()
    try:
        resume_text = extract_docx_text(data)
    except Exception:
        return HTMLResponse(
            "<p>Could not read that resume. Please upload a .docx file. "
            f"<a href='/candidates/{candidate.id}'>Back</a></p>",
            status_code=400,
        )
    # Remove the old files first — their paths depend on the old filename.
    for path in (resume_docx_path(candidate), resume_pdf_path(candidate)):
        if path.exists():
            path.unlink()
    candidate.resume_text = resume_text
    candidate.resume_filename = resume.filename or "resume.docx"
    atomic_write_bytes(resume_docx_path(candidate), data)
    storage.save_candidate(candidate)
    logger.info("Replaced resume for candidate %s (%s)", candidate.id, candidate.resume_filename)
    # Same best-effort PDF pre-conversion as at profile creation.
    try:
        await asyncio.to_thread(ensure_resume_pdf, candidate)
    except Exception:
        logger.warning(
            "Resume PDF pre-conversion failed for candidate %s (draft creation will retry)",
            candidate.id, exc_info=True,
        )
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


# ------------------------------------------------------------------ #
# App login (Google Sign-In — identity only; see app/auth.py)
# ------------------------------------------------------------------ #
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not config.settings.LOGIN_REQUIRED or request.session.get("user_email"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": request.session.pop("login_error", "")}
    )


@router.get("/auth/login")
async def auth_login(request: Request):
    if not config.settings.LOGIN_REQUIRED:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(auth.build_login_url())


@router.get("/auth/callback")
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
        logger.warning("Login failed: %s", exc)
        request.session["login_error"] = str(exc)
        return RedirectResponse("/login", status_code=303)
    logger.info("Login: %s", user["email"])
    request.session["user_email"] = user["email"]
    request.session["user_name"] = user["name"]
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ #
# Gmail (drafts only — see app/gmail_client.py for the compliance notes)
# ------------------------------------------------------------------ #
@router.get("/candidates/{candidate_id}/gmail/connect", response_class=HTMLResponse)
async def gmail_connect_explainer(
    request: Request, candidate: Candidate = Depends(owned_candidate)
):
    """Our own consent step, shown before handing off to Google's own screen."""
    return templates.TemplateResponse(request, "gmail_connect.html", {"c": candidate})


@router.get("/candidates/{candidate_id}/gmail/authorize")
async def gmail_authorize(candidate: Candidate = Depends(owned_candidate)):
    try:
        return RedirectResponse(gmail_client.build_auth_url(candidate.id))
    except GmailNotConfigured as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=500)


@router.get("/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state:
        return HTMLResponse(f"<p>Gmail connection was not completed ({error or 'no code returned'}).</p>")
    # `state` is the candidate_id — only store the token if the logged-in
    # user owns that candidate (blocks token-planting via a forged callback).
    # Raises NotFoundError (-> 404 page) otherwise.
    owned_candidate(request, state)
    # Blocking token exchange with Google — keep it off the event loop.
    await asyncio.to_thread(gmail_client.handle_oauth_callback, code, candidate_id=state)
    return RedirectResponse(f"/candidates/{state}", status_code=303)


@router.post("/candidates/{candidate_id}/gmail/disconnect")
async def gmail_disconnect(candidate: Candidate = Depends(owned_candidate)):
    # Blocking revoke call to Google (up to its 10s timeout) — off the loop.
    await asyncio.to_thread(gmail_client.disconnect, candidate.id)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


# ------------------------------------------------------------------ #
# Runs
# ------------------------------------------------------------------ #
@router.post("/candidates/{candidate_id}/runs")
async def start_run(candidate: Candidate = Depends(owned_candidate)):
    if manager.daily_cap_reached(candidate.id):
        cap = config.settings.MAX_RUNS_PER_DAY
        logger.warning(
            "Run refused for candidate %s: daily cap reached (%d/24h)", candidate.id, cap
        )
        return HTMLResponse(
            f"<p>Daily run limit reached ({cap} per profile per 24 hours) — "
            f"each run costs real API money, so this is capped. Try again later. "
            f"<a href='/candidates/{candidate.id}'>Back</a></p>",
            status_code=429,
        )
    run = manager.start_run(candidate)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, owned=Depends(owned_run)):
    run, candidate = owned
    # True elapsed time of the run itself (the old counter measured time since
    # the page loaded). Frozen once the run is finished; ticks while it works.
    start = run.started_running_at or run.created_at
    end = run.finished_at or time.time()
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "run": run,
            "c": candidate,
            "elapsed": max(0, int(end - start)),
            "ticking": run.phase in (RunPhase.DISCOVERING, RunPhase.RUNNING),
        },
    )


@router.get("/runs/{run_id}/panel", response_class=HTMLResponse)
async def run_panel(request: Request, run_id: str):
    # No Depends here: a missing/unowned run answers HTTP 286 (not 404) so
    # HTMX stops polling a dead panel instead of retrying forever.
    try:
        run, candidate = owned_run(request, run_id)
    except NotFoundError:
        return HTMLResponse("<p>Run not found.</p>", status_code=286)
    show_timeout_button = (
        run.phase == RunPhase.RUNNING
        and run.started_running_at is not None
        and (time.time() - run.started_running_at) >= config.settings.TIMEOUT_BUTTON_AFTER_SECONDS
    )
    response = templates.TemplateResponse(
        request,
        "_run_panel.html",
        {
            "run": run,
            "c": candidate,
            "gmail_connected": candidate and gmail_client.is_connected(candidate.id),
            "show_timeout_button": show_timeout_button,
            "hard_timeout_minutes": config.settings.RUN_HARD_TIMEOUT_SECONDS // 60,
        },
    )
    # HTTP 286 tells HTMX to stop polling once we've reached a stable phase.
    if run.phase in (RunPhase.REVIEW, RunPhase.DONE, RunPhase.ERROR):
        response.status_code = 286
    return response


@router.post("/runs/{run_id}/stop_early")
async def stop_run_early(owned=Depends(owned_run)):
    """Triggered by the "retrieve what's done" button — cuts a running
    pipeline short at the next check, keeping whatever already finished."""
    run, _ = owned
    manager.stop_early(run)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/companies/{index}/save_to_gmail")
async def save_to_gmail(index: int, owned=Depends(owned_run)):
    run, candidate = owned
    if not (0 <= index < len(run.companies)):
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    company = run.companies[index]
    # Require an email: manual-outreach drafts (verified contact, no Hunter
    # email) have no recipient, and the UI shows no button for them — but guard
    # the route too so a stale form or double-submit can't create an empty draft.
    if company.draft_subject and company.email:
        try:
            await gmail_client.create_draft(candidate, company)
            company.gmail_draft_created = True
            company.gmail_error = ""
        except Exception as exc:
            logger.warning(
                "Run %s: Gmail draft failed for %s: %s", run.id, company.name, exc
            )
            company.gmail_error = str(exc)
        manager.save(run)  # the saved-to-Gmail marks survive restarts too
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/save_all_to_gmail")
async def save_all_to_gmail(owned=Depends(owned_run)):
    """Batch version of save_to_gmail — one explicit click after all drafts in
    the run are visible on screen. Per-draft failures don't stop the rest."""
    run, candidate = owned
    for company in run.companies:
        # Skip manual-outreach drafts with no email (see save_to_gmail).
        if company.draft_subject and company.email and not company.gmail_draft_created:
            try:
                await gmail_client.create_draft(candidate, company)
                company.gmail_draft_created = True
                company.gmail_error = ""
            except Exception as exc:
                logger.warning(
                    "Run %s: Gmail draft failed for %s: %s", run.id, company.name, exc
                )
                company.gmail_error = str(exc)
    manager.save(run)  # the saved-to-Gmail marks survive restarts too
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.post("/runs/{run_id}/approve")
async def approve_companies(request: Request, owned=Depends(owned_run)):
    run, candidate = owned
    if run.phase != RunPhase.REVIEW:
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    form = await request.form()
    approved = form.getlist("approved")
    if not approved:
        return RedirectResponse(f"/runs/{run.id}", status_code=303)
    manager.approve(run, candidate, [str(d) for d in approved])
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


# ------------------------------------------------------------------ #
# Sent list
# ------------------------------------------------------------------ #
@router.get("/sent/{candidate_id}", response_class=HTMLResponse)
async def sent_page(request: Request, candidate: Candidate = Depends(owned_candidate)):
    return templates.TemplateResponse(
        request,
        "sent_list.html",
        {
            "c": candidate,
            "entries": sent_list.load_entries(candidate.id),
        },
    )


@router.post("/sent/{candidate_id}/add")
async def sent_add(
    candidate: Candidate = Depends(owned_candidate),
    contact_email: str = Form(...),
    company_domain: str = Form(""),
    contact_name: str = Form(""),
    date_sent: str = Form(""),
    permanently_excluded: bool = Form(False),
):
    """Manual entry for a contact reached outside the app (LinkedIn, in person,
    another tool) so future runs treat them as recently contacted."""
    if contact_email.strip():
        sent_list.add_entry(
            candidate.id,
            company_domain.strip(),
            contact_name.strip(),
            contact_email.strip(),
            date_sent=date_sent.strip(),
            confirmed_sent=True,  # already sent — no reconciliation nudge needed
            permanently_excluded=bool(permanently_excluded),
        )
    return RedirectResponse(f"/sent/{candidate.id}", status_code=303)


@router.post("/sent/{candidate_id}/{entry_id}/update")
async def sent_update(
    entry_id: int,
    candidate: Candidate = Depends(owned_candidate),
    contact_email: str = Form(None),
    date_sent: str = Form(None),
    action: str = Form(...),
):
    # Entries are addressed by their stable DB id (never by table position —
    # a running pipeline may be appending entries concurrently with an edit).
    cid = candidate.id
    if action == "delete":
        sent_list.remove_entry(cid, entry_id)
    elif action == "toggle_interview":
        entry = next((e for e in sent_list.load_entries(cid) if e["id"] == entry_id), None)
        if entry:
            sent_list.update_entry(
                cid, entry_id, interview_arranged=not entry["interview_arranged"]
            )
    elif action == "toggle_permanent":
        entry = next((e for e in sent_list.load_entries(cid) if e["id"] == entry_id), None)
        if entry:
            sent_list.update_entry(
                cid, entry_id, permanently_excluded=not entry["permanently_excluded"]
            )
    elif action == "confirm_sent":
        sent_list.update_entry(cid, entry_id, confirmed_sent=True)
    elif action == "save":
        changes = {}
        if contact_email is not None:
            changes["contact_email"] = contact_email.strip()
        if date_sent:
            changes["date_sent"] = date_sent.strip()
        if changes:
            sent_list.update_entry(cid, entry_id, **changes)
    return RedirectResponse(f"/sent/{cid}", status_code=303)


@router.post("/sent/{candidate_id}/confirm_all")
async def sent_confirm_all(candidate: Candidate = Depends(owned_candidate)):
    sent_list.confirm_all(candidate.id)
    return RedirectResponse(f"/candidates/{candidate.id}", status_code=303)


@router.post("/sent/{candidate_id}/prune")
async def sent_prune(candidate: Candidate = Depends(owned_candidate)):
    sent_list.prune_expired(candidate.id, candidate.retention_months)
    return RedirectResponse(f"/sent/{candidate.id}", status_code=303)


app = create_app()
