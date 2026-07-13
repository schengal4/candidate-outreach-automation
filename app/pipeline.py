"""The outreach pipeline orchestrator (spec steps 1-5, plus a post-draft
fact-check).

Step 1  Company discovery (LLM + web search) -> candidate review gate
Step 2  Contact identification, one at a time: find the best contact, try
        Hunter on them immediately, and only look for a backup contact if
        that fails. Avoids paying to research a backup that never gets used.
Step 3  Email lookup via Hunter (interleaved with step 2 -- see above)
Step 4  Personalization research (+ optional red flag detection)
Step 5  Draft generation (+ plain sign-off signature)
Step 6  Draft fact-check: a fresh call grounds the draft's claims against the
        research and spot-checks the most specific ones (plus the contact's
        employment) on the web; only a clean/flagged draft reaches the Sent
        List. A departed contact drops the company -- no replacement contact.

Steps 2-6 run sequentially within a company; companies run concurrently.

This module only sequences the work and applies results to run state. The
steps themselves are pure functions in app/steps.py (called through the
module namespace so a test can stub `steps.X` once and it takes effect
everywhere); prompt text lives in app/prompts.py; run registration and task
spawning live in app/run_manager.py.
"""

import asyncio
import logging
import random
import re
import time
from collections import deque
from typing import Dict, Iterable, List

from . import config, run_store, sent_list, steps
from .draft_hygiene import SIGNATURE_SEP, email_signature
from .llm import LLMError
from .models import (
    Candidate,
    CompanyState,
    CompanyStatus,
    Contact,
    RunPhase,
    RunState,
    normalize_linkedin_url,
)

logger = logging.getLogger("app.pipeline")

_LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def _linkedin_slug(url: str) -> str:
    """The /in/<slug> part of a LinkedIn URL, lowercased — profile identity
    without host/scheme/tracking noise. "" when there is none."""
    m = _LINKEDIN_SLUG_RE.search(url or "")
    return m.group(1).strip().lower() if m else ""


def _append_caveat(contact: Contact, text: str) -> None:
    contact.verification_caveat = (
        f"{contact.verification_caveat}; {text}" if contact.verification_caveat else text
    )


def _apply_hunter_linkedin(contact: Contact, hunter_url: str) -> None:
    """Reconcile the identification model's LinkedIn URL with the one Hunter
    returned alongside the email. Hunter's comes from pages it actually
    observed, while the model has been known to guess plausible slugs — so
    Hunter fills a blank, and on a real disagreement Hunter wins and the
    mismatch surfaces as a verification caveat instead of shipping silently."""
    hunter_url = normalize_linkedin_url(hunter_url)
    if not hunter_url:
        return
    if not contact.linkedin_url:
        contact.linkedin_url = hunter_url
        return
    old_slug, new_slug = _linkedin_slug(contact.linkedin_url), _linkedin_slug(hunter_url)
    if old_slug and new_slug and old_slug == new_slug:
        return  # same profile (host/scheme differences don't matter)
    logger.info(
        "%s %s: LinkedIn URL mismatch (model=%s hunter=%s) — using Hunter's",
        contact.first_name, contact.last_name, contact.linkedin_url, hunter_url,
    )
    _append_caveat(
        contact,
        f"conflicting LinkedIn profiles for this contact — the research cited "
        f"{contact.linkedin_url} but Hunter's records show {hunter_url}; using "
        f"Hunter's, confirm it opens the right person",
    )
    contact.linkedin_url = hunter_url


def _apply_verify_contact_updates(contact: Contact, verdict: steps.VerifyResult) -> None:
    """Post-fact-check contact hygiene (never a drop): a changed title becomes
    a caveat, and the LinkedIn URL is settled — a found correction wins, a
    wrong-person URL is removed so the report falls back to a search link."""
    if verdict.contact_update:
        _append_caveat(
            contact, f"the fact-check found an updated role/title: {verdict.contact_update}"
        )
    correction = normalize_linkedin_url(verdict.linkedin_correction)
    if correction:
        old_slug, new_slug = _linkedin_slug(contact.linkedin_url), _linkedin_slug(correction)
        if not contact.linkedin_url:
            contact.linkedin_url = correction
        elif not (old_slug and new_slug and old_slug == new_slug):
            _append_caveat(
                contact,
                f"the fact-check found a different LinkedIn profile than the research "
                f"cited (was {contact.linkedin_url}) — confirm it opens the right person",
            )
            contact.linkedin_url = correction
    elif verdict.linkedin_verdict == "wrong-person" and contact.linkedin_url:
        logger.info(
            "%s %s: fact-check judged LinkedIn URL %s wrong-person — removed",
            contact.first_name, contact.last_name, contact.linkedin_url,
        )
        _append_caveat(
            contact,
            "the LinkedIn URL from research could not be matched to this person "
            "and was removed — use the LinkedIn search link instead",
        )
        contact.linkedin_url = ""


async def _run_company(
    candidate: Candidate,
    company: CompanyState,
    recently_contacted_names: List[str],
    blocked_emails: set,
    used_accomplishments: List[str],
) -> None:
    """Steps 2 -> 6 sequentially for one company.

    All companies in a run call this concurrently (no artificial concurrency
    cap) -- rate-limit/overload responses from that burst are absorbed by the
    retry-with-backoff in llm.py's _send_request, not handled here.
    """

    def report(text: str) -> None:
        company.activity = text

    def set_status(status: str, reason: str = "") -> None:
        company.status = status
        company.activity = ""
        if reason:
            company.drop_reason = reason
            logger.info("%s (%s): dropped — %s", company.name, company.domain, reason)

    try:
        set_status(CompanyStatus.CONTACTS)
        company.primary = await steps.identify_contact(
            candidate,
            company.name,
            company.domain,
            recently_contacted_names,
            on_progress=report,
            label=f"contact:{company.name}",
        )

        if not company.primary:
            set_status(CompanyStatus.DROPPED, "no contact identified")
            return

        found_email = False
        if company.primary.employment_verified:
            set_status(CompanyStatus.EMAIL)
            email, score, hunter_linkedin = await steps.lookup_email(
                company.domain, company.primary, blocked_emails
            )
            if email:
                company.contact_used, company.email, company.email_score = (
                    company.primary, email, score,
                )
                _apply_hunter_linkedin(company.primary, hunter_linkedin)
                found_email = True

        if not found_email:
            # Primary wasn't verified or Hunter couldn't find them — only now
            # spend a second call looking for a backup contact.
            set_status(CompanyStatus.CONTACTS)
            excluded = recently_contacted_names + [company.primary.full_name]
            company.backup = await steps.identify_contact(
                candidate,
                company.name,
                company.domain,
                excluded,
                on_progress=report,
                label=f"backup:{company.name}",
            )

            if not (company.backup and company.backup.employment_verified):
                set_status(CompanyStatus.DROPPED, "employment could not be verified for any contact")
                return

            set_status(CompanyStatus.EMAIL)
            email, score, hunter_linkedin = await steps.lookup_email(
                company.domain, company.backup, blocked_emails
            )
            if email:
                company.contact_used, company.email, company.email_score = (
                    company.backup, email, score,
                )
                _apply_hunter_linkedin(company.backup, hunter_linkedin)
                found_email = True

        if not found_email:
            # No Hunter email, but we still have a verified contact — the
            # backup-verified gate above guarantees at least one. Rather than
            # discard verified, well-matched work, draft the outreach anyway
            # for manual sending (e.g. LinkedIn InMail). No email means no
            # Gmail draft downstream — the run report handles that case.
            company.contact_used = (
                company.primary
                if (company.primary and company.primary.employment_verified)
                else company.backup
            )
            company.email = ""
            company.email_score = None
            logger.info(
                "%s (%s): no email found — drafting for manual outreach (contact: %s)",
                company.name, company.domain, company.contact_used.full_name,
            )

        set_status(CompanyStatus.RESEARCH)
        research = await steps.research_contact(
            company.contact_used, company.name, company.domain,
            candidate.red_flag_detection, on_progress=report,
        )
        company.research_summary = research.summary
        company.research_items = research.items
        company.red_flags = research.red_flags

        set_status(CompanyStatus.DRAFTING)
        draft = await steps.draft_email(
            candidate, company.contact_used, company.name, research,
            used_accomplishments=used_accomplishments, on_progress=report,
        )
        company.draft_subject = draft.subject
        company.draft_body = draft.body + email_signature(candidate)
        company.draft_banned_phrases = draft.banned_phrases

        set_status(CompanyStatus.VERIFYING)
        verdict = await steps.verify_draft(
            company.contact_used, company.name, company.domain, research,
            company.draft_subject, company.draft_body, on_progress=report,
        )
        company.draft_verify_error = verdict.error
        company.draft_flagged_claims = verdict.flagged_claims
        if verdict.departed_evidence:
            # Deliberately no replacement contact here: re-picking would mean
            # redoing research + draft on a company that already spent its
            # budget, and the step-2 verification already had its chance. The
            # company drops with the evidence, and the bench backfills the
            # slot. Runs before the Sent List add, so this contact is never
            # recorded as contacted.
            set_status(
                CompanyStatus.DROPPED,
                f"contact no longer at company (found during draft fact-check): "
                f"{company.contact_used.full_name} — {verdict.departed_evidence}",
            )
            return

        # Still at the company: fold the fact-check's contact findings (newer
        # title, LinkedIn URL confirmation/correction) into the report.
        _apply_verify_contact_updates(company.contact_used, verdict)

        if company.draft_flagged_claims:
            # Fix flagged claims instead of only flagging them: one revision,
            # rechecked without web, adopted only if cleaner. Whatever flags
            # remain still reach the run report.
            revision = await steps.revise_flagged_draft(
                candidate, company.contact_used, company.name, research,
                company.draft_subject,
                company.draft_body.split(SIGNATURE_SEP)[0],
                company.draft_flagged_claims,
                on_progress=report,
            )
            if revision:
                company.draft_subject = revision.subject
                company.draft_body = revision.body + email_signature(candidate)
                company.draft_flagged_claims = revision.flagged_claims
                company.draft_banned_phrases = revision.banned_phrases

        # Auto-add to Sent List once the draft passes the fact-check
        # (date_sent = today).
        sent_list.add_entry(
            candidate.id,
            company.domain,
            company.contact_used.full_name,
            company.email,
        )
        set_status(CompanyStatus.DONE)
        logger.info(
            "%s (%s): draft complete (contact: %s)",
            company.name, company.domain, company.contact_used.full_name,
        )
    except LLMError as exc:
        set_status(CompanyStatus.DROPPED, f"error: {exc}")
    except Exception as exc:  # keep one company's failure from killing the run
        logger.exception("%s (%s): unexpected error", company.name, company.domain)
        # Some exceptions (httpx network errors, notably) stringify to "" —
        # always name the type so the run report never shows a blank reason.
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        set_status(CompanyStatus.DROPPED, f"unexpected error: {detail}")


async def run_discovery(
    run: RunState,
    candidate: Candidate,
    previous_runs: Iterable[RunState] = (),
) -> None:
    """Phase 1: discovery, then park at the candidate review gate.

    `previous_runs` — this candidate's other runs (the RunManager passes
    them), used to flag companies that already failed in an earlier run.
    """

    def report(text: str) -> None:
        run.activity = text

    try:
        # Over-fetch beyond max_companies: the extras become the bench that
        # backfills companies dropped mid-run (see run_pipeline).
        count = min(
            candidate.max_companies + config.settings.DISCOVERY_BENCH_EXTRA,
            config.settings.MAX_COMPANIES_HARD_CAP,
        )
        # Companies with an active Sent List entry (inside the retention
        # window, or permanently excluded) don't resurface in discovery —
        # without this, only the *contact* was excluded downstream and the
        # same company could come back run after run.
        excluded_domains = {
            e["company_domain"]
            for e in sent_list.active_entries(candidate.id, candidate.retention_months)
            if e.get("company_domain")
        }
        logger.info(
            "Run %s: discovery started for candidate %s (%d companies, %d domains excluded)",
            run.id, candidate.id, count, len(excluded_domains),
        )
        run.discovered = await steps.discover_companies(
            candidate, count, on_progress=report, excluded_domains=excluded_domains
        )
        run.activity = ""
        if not run.discovered:
            logger.warning("Run %s: discovery returned no companies", run.id)
            run.phase = RunPhase.ERROR
            run.error = "Discovery returned no companies."
            return
        # Flag companies that failed in a previous run (dropped for a real,
        # company-specific reason — not a run-level stop/timeout cutoff). The
        # review gate shows the reason and leaves them UNCHECKED, so retrying
        # a known failure is the user's explicit choice instead of a silent
        # repeat: logged runs re-attempted the same unverifiable companies
        # run after run. Derived from persisted run history — no separate
        # store to maintain.
        past_failures: Dict[str, str] = {}
        for past in sorted(
            (r for r in previous_runs if r.candidate_id == candidate.id and r.id != run.id),
            key=lambda r: r.created_at,  # ascending, so the latest reason wins
        ):
            for comp in past.companies:
                if (
                    comp.status == CompanyStatus.DROPPED
                    and comp.drop_reason
                    and not comp.drop_reason.startswith("run stopped early")
                ):
                    past_failures[comp.domain] = comp.drop_reason
        for c in run.discovered:
            if c["domain"] in past_failures:
                c["failed_before"] = past_failures[c["domain"]]

        logger.info("Run %s: discovery found %d companies — awaiting review", run.id, len(run.discovered))
        run.phase = RunPhase.REVIEW
    except Exception as exc:
        logger.exception("Run %s: discovery failed", run.id)
        run.phase = RunPhase.ERROR
        run.error = str(exc) or type(exc).__name__
        run.finished_at = time.time()
    finally:
        # Checkpoint: the review gate (or the failure) survives a restart.
        run_store.save_run(run)


async def run_pipeline(run: RunState, candidate: Candidate, approved_domains: List[str]) -> None:
    """Phases 2-6 for all approved companies, concurrently.

    Cuts the run short — keeping whatever companies already finished — if
    either the user clicks the "retrieve what's done" button (run.stop_event,
    set via the RunManager) or RUN_HARD_TIMEOUT_SECONDS elapses, whichever
    happens first. A single web-search-heavy company can otherwise take much
    longer than a user wants to wait on the whole run.
    """
    try:
        approved = [c for c in run.discovered if c["domain"] in set(approved_domains)]
        run.companies = [
            CompanyState(name=c["name"], domain=c["domain"], reason=c.get("reason", ""))
            for c in approved[: config.settings.MAX_COMPANIES_HARD_CAP]
        ]
        run.approved_count = len(run.companies)
        # Draft target: only this many companies run at once; approved
        # companies beyond it are the bench (discovery over-fetches for this —
        # see DISCOVERY_BENCH_EXTRA), pulled in only when a company drops.
        target = min(candidate.max_companies, len(run.companies))
        run.phase = RunPhase.RUNNING
        run.started_running_at = time.time()
        logger.info(
            "Run %s: pipeline started with %d approved companies", run.id, len(run.companies)
        )
        run_store.save_run(run)  # checkpoint: approved company list on disk

        active = sent_list.active_entries(candidate.id, candidate.retention_months)
        recently_contacted_names = sorted(
            {e["contact_name"] for e in active if e.get("contact_name")}
        )
        blocked_emails = sent_list.active_blocked_emails(
            candidate.id, candidate.retention_months
        )
        # Shared across every company's draft call so the run features
        # different resume accomplishments instead of repeating one — see
        # steps.draft_email. Recipients in a run cluster in one community.
        used_accomplishments: List[str] = []

        # Bench/backfill: `target` slots run concurrently, each working toward
        # ONE finished draft. A slot whose company drops pulls the next bench
        # company instead of dying with its slot, so failures don't shrink the
        # number of drafts delivered. Launches are staggered by a small random
        # delay so the initial burst of web-search calls doesn't land in the
        # same instant.
        pending = deque(run.companies)

        async def _slot() -> None:
            if config.settings.RUN_LAUNCH_JITTER_SECONDS > 0:
                await asyncio.sleep(
                    random.uniform(0, config.settings.RUN_LAUNCH_JITTER_SECONDS)
                )
            while pending:
                company = pending.popleft()
                try:
                    await _run_company(
                        candidate, company, recently_contacted_names,
                        blocked_emails, used_accomplishments,
                    )
                finally:
                    # Checkpoint after every company settles (done or dropped)
                    # so a mid-run restart keeps every draft finished so far.
                    run_store.save_run(run)
                if company.status == CompanyStatus.DONE:
                    return  # slot delivered its draft; bench stays for failed slots

        company_tasks = [asyncio.create_task(_slot()) for _ in range(target)]
        all_done = asyncio.gather(*company_tasks)
        stop_waiter = asyncio.create_task(run.stop_event.wait())
        finished, _ = await asyncio.wait(
            {all_done, stop_waiter},
            timeout=config.settings.RUN_HARD_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop_waiter.cancel()

        if all_done not in finished:
            # Cut short by the stop button or the hard timeout — cancel
            # whatever's still in flight; whatever already finished stays.
            cut_reason = (
                "run stopped early (stopped by user)"
                if run.stop_event.is_set()
                else "run stopped early (timeout)"
            )
            logger.warning(
                "Run %s: cut short (%s) — unfinished companies dropped",
                run.id,
                "stop button" if run.stop_event.is_set() else "hard timeout",
            )
            all_done.cancel()
            try:
                await all_done
            except asyncio.CancelledError:
                pass
            for company in run.companies:
                # PENDING = a bench company that never started — not an
                # attempt, so it gets removed below rather than marked dropped.
                if company.status not in (
                    CompanyStatus.DONE, CompanyStatus.DROPPED, CompanyStatus.PENDING
                ):
                    company.status = CompanyStatus.DROPPED
                    company.drop_reason = cut_reason
                    company.activity = ""

        # Bench companies that were never needed aren't attempts — drop them
        # from the report so "Companies attempted" reflects real work.
        run.companies = [c for c in run.companies if c.status != CompanyStatus.PENDING]
        run.phase = RunPhase.DONE
        logger.info(
            "Run %s: finished — %d drafted, %d dropped",
            run.id,
            sum(1 for c in run.companies if c.status == CompanyStatus.DONE),
            sum(1 for c in run.companies if c.status == CompanyStatus.DROPPED),
        )
    except Exception as exc:
        logger.exception("Run %s: pipeline failed", run.id)
        run.phase = RunPhase.ERROR
        run.error = str(exc) or type(exc).__name__
    finally:
        run.finished_at = time.time()  # the run page shows true elapsed time
        run_store.save_run(run)  # checkpoint: final report on disk
