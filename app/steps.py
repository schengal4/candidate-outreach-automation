"""The pipeline's individual steps as pure(ish) functions.

Each step takes explicit inputs and returns a result object — none of them
mutate CompanyState or RunState. The orchestrator (app/pipeline.py) alone
applies results to the run's state, so a step's full effect is visible in
its signature and each step is testable with plain values. Prompt text and
schemas live in app/prompts.py; deterministic post-processing lives in
app/draft_hygiene.py.

The only shared mutable argument is draft_email's `used_accomplishments`
accumulator, which is the point: it threads "what other drafts in this run
already featured" across concurrent draft calls.
"""

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import config, hunter_async, prompts
from .draft_hygiene import banned_style_hits, finalize_body, strip_cite_tags
from .llm import LLMError, LLMTimeoutError, ask_json
from .models import Candidate, Contact

logger = logging.getLogger("app.steps")


# ------------------------------------------------------------------ #
# Step results
# ------------------------------------------------------------------ #
@dataclass
class ResearchResult:
    summary: str = ""
    # Structured items: {"fact", "source", "date", "url"} dicts.
    items: List[Dict] = field(default_factory=list)
    red_flags: List[str] = field(default_factory=list)
    # Research passes that produced nothing usable — human-readable reasons
    # ("person research failed: ...") shown in the run report so the user
    # knows this draft was written with reduced (or, when items is empty,
    # zero) personalization and can add a hook before sending. Failure
    # detail deliberately lives HERE, never in summary: real runs stored
    # the model's search-failure apologia as the summary, the report
    # rendered it as research notes, and one draft even echoed it.
    failures: List[str] = field(default_factory=list)

    def as_prompt_payload(self) -> str:
        # Items are numbered so the draft can bind each factual claim to the
        # item that supports it (claims_used in DRAFT_SCHEMA) — the binding
        # is what lets a deterministic check catch claims the draft invented.
        payload: Dict = {
            "summary": self.summary,
            "items": [{"id": i, **it} for i, it in enumerate(self.items, start=1)],
        }
        if self.failures:
            # A generic status line, not the failure detail (internal error
            # text in a drafting prompt invites the model to echo it). The
            # draft prompt's no-research rule keys off this: write a plain,
            # honest email from what's here — never invent specifics.
            payload["research_status"] = (
                "Personalization research could not be fully completed for "
                "this contact. The summary and items above are everything "
                "that is available — do not invent, assume, or pad beyond them."
            )
        return json.dumps(payload, indent=2, ensure_ascii=False)


@dataclass
class DraftResult:
    subject: str
    body: str  # finalized (greeting/closing/paragraphs), WITHOUT signature
    featured_accomplishment: str = ""
    # Banned style phrasings detected in the draft. Diagnostics only (logs,
    # tests, prompt tuning) — deliberately NOT rendered in the run report,
    # where wording nits buried the factual flags users must act on.
    banned_phrases: List[str] = field(default_factory=list)
    # For revisions: claims the web recheck still flagged.
    flagged_claims: List[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    # Claims the fact-check could not independently verify — "unsupported: ..."
    # / "unverified: ..." strings shown as a review flag in the run report.
    flagged_claims: List[str] = field(default_factory=list)
    # One-line evidence when the contact appears to have left the company;
    # "" otherwise. The orchestrator drops the company on this.
    departed_evidence: str = ""
    # Contact still at the company but under a newer title (promotion/reorg) —
    # one line with source. Surfaced as a verification caveat, not a drop.
    contact_update: str = ""
    # LinkedIn URL check: "confirmed" / "wrong-person" / "not-found";
    # "" when the check didn't run (e.g. the call failed).
    linkedin_verdict: str = ""
    # The person's actual profile URL when a search result showed one.
    linkedin_correction: str = ""
    # Non-empty when the fact-check call itself failed: the draft is kept
    # (the check is a safety net, not a gate) but flagged as unchecked.
    error: str = ""


# ------------------------------------------------------------------ #
# Step 1: Company discovery
# ------------------------------------------------------------------ #
async def discover_companies(
    candidate: Candidate,
    count: int,
    on_progress: Optional[Callable[[str], None]] = None,
    excluded_domains: Optional[set] = None,
) -> List[Dict[str, str]]:
    """`excluded_domains` — company domains with an active Sent List entry;
    excluded in the prompt (so the model doesn't waste picks on them) AND
    hard-filtered from the results, same belt-and-suspenders as the
    current-employer rule."""
    excluded_domains = {d.strip().lower().removeprefix("www.") for d in (excluded_domains or set()) if d.strip()}
    excluded_block = ""
    if excluded_domains:
        listed = "\n".join(f"- {d}" for d in sorted(excluded_domains))
        excluded_block = f"\n\nAlready-contacted companies (do NOT include any of these):\n{listed}"
    user = (
        f"Find {count} target companies for this candidate.\n\n"
        f"{prompts.candidate_profile_block(candidate)}{excluded_block}"
    )
    raw = await ask_json(
        prompts.DISCOVERY_SYSTEM, user, web_search=True, on_progress=on_progress,
        label="discovery", schema=prompts.DISCOVERY_SCHEMA,
    )
    if isinstance(raw, dict):
        raw = raw.get("companies", [])
    if not isinstance(raw, list):
        raise LLMError("Discovery did not return a list of companies.")
    employer = candidate.current_employer.strip().lower()
    companies = []
    for item in raw:
        name = str(item.get("name", "")).strip()
        domain = str(item.get("domain", "")).strip().lower().removeprefix("www.")
        if not name or not domain:
            continue
        # Hard exclusion of current employer, belt and suspenders on top of the prompt.
        if employer and (employer in name.lower() or employer in domain):
            continue
        # Hard exclusion of already-contacted companies (active Sent List entries).
        if domain in excluded_domains:
            continue
        companies.append({"name": name, "domain": domain, "reason": str(item.get("reason", ""))})
    return companies[:count]


# ------------------------------------------------------------------ #
# Step 2 + 3: Contact identification and Hunter email lookup
# ------------------------------------------------------------------ #
async def identify_contact(
    candidate: Candidate,
    company_name: str,
    domain: str,
    excluded_names: List[str],
    leads: Optional[List[Dict]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    label: str = "contact",
) -> Tuple[Optional[Contact], Optional[Contact]]:
    """Returns (primary, fallback). The fallback is whatever second
    hiring-relevant person surfaced in the SAME searches (None when none
    did) — the orchestrator tries it before paying for a second call.
    `leads` is Hunter's directory listing for the domain (hunter_async
    .list_people): starting points the model verifies instead of spending
    searches on discovery; never verification by itself."""
    excluded = "\n".join(f"- {n}" for n in excluded_names) or "(none)"
    # Candidate-specific but company-independent — identical on every
    # identify_contact call in this run (primary + backup, every company),
    # so it's cached separately from the company-specific part below.
    cache_prefix = (
        f"Candidate's target role/industry: {candidate.target_industry_role or 'see resume'}\n\n"
        f"Candidate summary (for relevance):\n{candidate.resume_text[:3000]}"
    )
    leads_block = ""
    if leads:
        leads_block = (
            "\n\nContacts Hunter's database lists at this domain (leads to"
            " verify, not verification — see system prompt):\n"
            + "\n".join(
                f"- {p['name']}" + (f" — {p['title']}" if p.get("title") else "")
                for p in leads
            )
        )
    user = (
        f"Company: {company_name} ({domain})\n\n"
        f"Excluded contacts (do not pick these):\n{excluded}"
        f"{leads_block}\n\n"
        "Identify the best contact (and an opportunistic fallback, per the system prompt)."
    )
    async def _call(max_uses: int, timeout_seconds: int):
        return await ask_json(
            prompts.CONTACT_SYSTEM, user, web_search=True, on_progress=on_progress, label=label,
            cache_prefix=cache_prefix, schema=prompts.CONTACT_SCHEMA,
            # Shorter leash than research: contact calls reliably spend their whole
            # search budget, so the budget IS the call duration (see config).
            max_uses=max_uses,
            timeout_seconds=timeout_seconds,
        )

    try:
        raw = await _call(
            config.settings.CONTACT_WEB_SEARCH_MAX_USES,
            config.settings.CONTACT_CALL_TIMEOUT_SECONDS,
        )
    except LLMTimeoutError:
        # Same salvage as research_contact below: a timed-out contact search
        # usually wandered, and a tighter call reliably finishes. One retry
        # on a smaller budget and clock; a second timeout propagates.
        logger.warning(
            "%s: timed out — one retry with a smaller budget (%d searches)",
            label, config.settings.CONTACT_TIMEOUT_RETRY_MAX_USES,
        )
        if on_progress:
            on_progress("Contact search ran long — retrying with a tighter budget…")
        raw = await _call(
            config.settings.CONTACT_TIMEOUT_RETRY_MAX_USES,
            config.settings.CONTACT_TIMEOUT_RETRY_TIMEOUT_SECONDS,
        )
    def _parse(person_raw) -> Optional[Contact]:
        if not isinstance(person_raw, dict):
            return None
        contact = Contact.from_dict(person_raw)
        # Empty-name sentinel: the schema requires the fallback object, so
        # "none surfaced" arrives as all-empty fields (see CONTACT_SCHEMA).
        if not contact or not contact.full_name:
            return None
        # Anti-fabrication gate: a LinkedIn URL without provenance (which
        # search result showed it) is a guessed slug until proven otherwise —
        # real runs shipped plausible-looking /in/first-last URLs that
        # belonged to strangers. Better no link (the UI can fall back to a
        # LinkedIn search) than a wrong person's profile presented as fact.
        if contact.linkedin_url and not str(person_raw.get("linkedin_url_source", "")).strip():
            logger.info(
                "%s: dropping unsourced LinkedIn URL %s (no search result cited)",
                label, contact.linkedin_url,
            )
            contact.linkedin_url = ""
        return contact

    if not isinstance(raw, dict):
        return None, None
    return _parse(raw.get("primary")), _parse(raw.get("fallback"))


async def lookup_email(
    domain: str, contact: Contact, blocked_emails: set
) -> Tuple[Optional[str], Optional[int], str]:
    """Try Hunter for exactly one contact. Returns (email, score,
    hunter_linkedin_url), or (None, None, "") when Hunter has no confident
    match or the address is on the candidate's do-not-recontact list.

    hunter_linkedin_url comes from Hunter's observed source data — unlike the
    identification model, Hunter can't hallucinate a slug, so the orchestrator
    prefers it over the model's URL (see pipeline._apply_hunter_linkedin)."""
    if not (contact.first_name and contact.last_name):
        return None, None, ""
    email, score, linkedin_url = await hunter_async.find_email(
        domain, contact.first_name, contact.last_name
    )
    if not email or email.strip().lower() in blocked_emails:
        return None, None, ""
    return email, score, linkedin_url


# ------------------------------------------------------------------ #
# Step 4: Personalization research
# ------------------------------------------------------------------ #
# Research runs as TWO parallel focused passes instead of one monolithic
# call. Inside a single streamed request the searches run serially (each is
# a think+read round), so one 6-search call takes roughly twice as long as
# two concurrent 3-search calls — and a single big call was the run's worst
# wall-clock tail (two real companies burned 2x480s in research timeouts).
# Same total search budget, so no depth is lost; each pass gets a dedicated
# angle so the split can't silently halve coverage of either.
_PERSON_FOCUS = (
    "\n\nFocus THIS research pass on the person themselves: their own posts, "
    "articles, talks, interviews, and projects. Company-wide news is covered "
    "by a separate pass — skip it here."
)
_COMPANY_FOCUS = (
    "\n\nFocus THIS research pass on the company: recent launches, funding, "
    "partnerships, and news the contact's team is plausibly involved in. The "
    "person's own posts and talks are covered by a separate pass — skip them here."
)


def _normalize_items(raw: Dict) -> List[Dict]:
    items = []
    for it in raw.get("items", []):
        if isinstance(it, dict):
            fact = strip_cite_tags(str(it.get("fact", "")))
            if not fact:
                continue
            items.append({
                "fact": fact,
                "source": strip_cite_tags(str(it.get("source", ""))),
                "date": strip_cite_tags(str(it.get("date", ""))),
                "url": str(it.get("url", "")).strip(),
            })
        elif it:
            # Plain-string item (pre-structured shape) — normalize, unattributed.
            items.append({"fact": strip_cite_tags(str(it)), "source": "", "date": "", "url": ""})
    return items


async def research_contact(
    contact: Contact,
    company_name: str,
    domain: str,
    red_flags_enabled: bool,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ResearchResult:
    def _system(search_budget: int) -> str:
        # The budget is announced in the prompt (like every other web-search
        # step) so the model treats max_uses_exceeded as "budget spent, write
        # up what you found" instead of a tool failure — a real run's models
        # read the unexplained rejections as an outage, set search_failed on
        # all 22 passes, and shipped 10 unpersonalized drafts.
        return prompts.RESEARCH_SYSTEM.format(
            red_flag_clause=prompts.RED_FLAG_ON if red_flags_enabled else prompts.RED_FLAG_OFF,
            search_budget=search_budget,
        )

    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name} ({domain})\n"
        f"LinkedIn: {contact.linkedin_url or 'unknown'}\n\n"
        "Gather personalization research for outreach to this person."
    )
    per_pass_uses = max(2, config.settings.RESEARCH_WEB_SEARCH_MAX_USES // 2)

    async def _one_pass(focus: str, label: str):
        async def _call(max_uses: int, timeout_seconds: int):
            return await ask_json(
                _system(max_uses), user + focus, web_search=True,
                on_progress=on_progress,
                label=label, schema=prompts.RESEARCH_SCHEMA,
                max_uses=max_uses,
                timeout_seconds=timeout_seconds,
            )

        try:
            return await _call(
                per_pass_uses, config.settings.RESEARCH_CALL_TIMEOUT_SECONDS
            )
        except LLMTimeoutError:
            # By this point the company has a verified contact and usually an
            # email — dropping it over a wandering research call is the most
            # expensive kind of drop. One retry on a small budget AND a
            # tighter clock: tighter calls reliably finish.
            retry_uses = max(2, config.settings.RESEARCH_TIMEOUT_RETRY_MAX_USES)
            logger.warning(
                "%s: timed out — one retry with a smaller budget (%d searches)",
                label, retry_uses,
            )
            if on_progress:
                on_progress("Research ran long — retrying with a tighter budget…")
            return await _call(
                retry_uses, config.settings.RESEARCH_TIMEOUT_RETRY_TIMEOUT_SECONDS
            )

    person, company = await asyncio.gather(
        _one_pass(_PERSON_FOCUS, f"research:{company_name}"),
        _one_pass(_COMPANY_FOCUS, f"research-co:{company_name}"),
        return_exceptions=True,
    )
    # A failing pass — even BOTH failing — never drops the company: by this
    # point the contact is verified and the email usually found, the two
    # expensive wins. The draft is written from whatever survived (the draft
    # prompt has an explicit no-research mode that forbids inventing
    # specifics), and every failure is flagged on the run report so the user
    # can personalize the email themselves before sending.
    items: List[Dict] = []
    seen: set = set()
    summaries: List[str] = []
    red_flags: List[str] = []
    failures: List[str] = []
    for raw, which in ((person, "person"), (company, "company")):
        if isinstance(raw, BaseException):
            logger.warning(
                "research:%s: %s pass failed (%s) — continuing without it",
                company_name, which, raw,
            )
            failures.append(f"{which} research failed: {raw}")
            continue
        kept = 0
        for item in _normalize_items(raw):
            # Both passes can surface the same source/fact — dedupe by URL
            # when there is one, by the fact text otherwise.
            key = item["url"] or item["fact"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            kept += 1
        if red_flags_enabled:
            red_flags.extend(strip_cite_tags(str(x)) for x in raw.get("red_flags", []) if x)
        if raw.get("search_failed"):
            # The model reported (structured field, see RESEARCH_SCHEMA) that
            # its web searches couldn't run. Keep any items it still verified,
            # but DISCARD its summary — models used to narrate the failure
            # there, and that prose shipped as "research notes" in the report
            # and even leaked into a draft ("I don't have fresh research on a
            # specific project of yours to point to").
            logger.warning(
                "research:%s: %s pass reported failed web searches (%d item(s) kept)",
                company_name, which, kept,
            )
            failures.append(f"{which} research could not complete its web searches")
            continue
        s = strip_cite_tags(str(raw.get("summary", ""))).strip()
        if s:
            summaries.append(s)
    return ResearchResult(
        summary=" ".join(summaries), items=items, red_flags=red_flags,
        failures=failures,
    )


# ------------------------------------------------------------------ #
# Step 5: Generate outreach
# ------------------------------------------------------------------ #
def _unbound_claims(raw: Dict, item_count: int) -> List[str]:
    """Claims the draft itself could not tie to a numbered research item —
    an empty item_ids list (self-confessed) or ids that don't exist. This is
    deliberately a soft gate: a claim the model omits from claims_used
    entirely can't be caught here, which is why the independent web
    fact-check (step 6) stays the final authority."""
    unbound = []
    for entry in raw.get("claims_used") or []:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim", "")).strip()
        ids = entry.get("item_ids") or []
        valid = [i for i in ids if isinstance(i, int) and 1 <= i <= item_count]
        if claim and not valid:
            unbound.append(claim)
    return unbound


def _draft_cache_prefix(candidate: Candidate) -> str:
    """The candidate-profile block shared by every draft-type call in a run
    (first drafts and claim revisions) — one string so they all hit the same
    cache breakpoint."""
    prefix = f"Candidate profile:\n{prompts.candidate_profile_block(candidate)}"
    if candidate.draft_instructions:
        prefix += (
            f"\n\nCandidate's own drafting instructions (follow these — "
            f"see system prompt for precedence):\n{candidate.draft_instructions}"
        )
    return prefix


async def _style_redraft(
    candidate: Candidate,
    company_name: str,
    subject: str,
    body: str,
    hits: List[str],
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """One no-web rewording pass for a draft that uses banned wording (see
    draft_hygiene._BANNED_STYLE_RE — the prompt rule alone keeps leaking).
    Returns the raw reply dict only when the redo is strictly cleaner, else
    None. The instruction pins every factual claim, and callers run their
    independent fact-check AFTER this, so the checked text is the text that
    ships. Style is never worth losing a draft over — all failures return
    None and the caller keeps what it had."""
    user = (
        "The email below is final except for wording: it uses phrasing your "
        "style rules explicitly ban:\n"
        + "\n".join(f"- {h}" for h in hits)
        + "\n\nRewrite it, replacing ONLY that wording with plain, specific "
        "language. Do not change any factual claims, names, numbers, the "
        "paragraph structure, or the featured accomplishment.\n\n"
        + json.dumps({"subject": subject, "body": body}, ensure_ascii=False)
    )
    try:
        raw = await ask_json(
            prompts.DRAFT_SYSTEM, user, web_search=False, on_progress=on_progress,
            label=f"restyle:{company_name}", cache_prefix=_draft_cache_prefix(candidate),
            schema=prompts.DRAFT_SCHEMA, effort="medium",
        )
    except LLMError as exc:
        logger.warning(
            "restyle:%s: style redraft failed (%s) — keeping the wording",
            company_name, exc,
        )
        return None
    new_subject = str(raw.get("subject", "")).strip()
    new_body = str(raw.get("body", "")).strip()
    if not (new_subject and new_body):
        return None
    if len(banned_style_hits(f"{new_subject}\n{new_body}")) >= len(hits):
        logger.warning(
            "restyle:%s: redo no cleaner — keeping the original wording", company_name
        )
        return None
    return raw


async def draft_email(
    candidate: Candidate,
    contact: Contact,
    company_name: str,
    research: ResearchResult,
    used_accomplishments: Optional[List[str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> DraftResult:
    """Draft the outreach email for one company.

    `used_accomplishments` is a list shared across all of a run's draft calls:
    each draft reports which candidate accomplishment it featured, and later
    drafts are told to pick a different one, so the run spreads across the
    resume instead of every email showcasing the same project.
    """
    # The candidate profile (including the full resume, up to 30k chars) and
    # their drafting instructions are identical on every company's draft call
    # in this run — cache them separately from the per-company
    # contact/research part below, instead of paying full price to resend
    # the whole resume on every draft.
    cache_prefix = _draft_cache_prefix(candidate)
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name}\n\n"
        f"Personalization research:\n{research.as_prompt_payload()}\n\n"
        f"Write the outreach email. {random.choice(prompts.DRAFT_STRUCTURE_HINTS)}"
    )
    if used_accomplishments:
        user += (
            "\n\nAccomplishments already featured in other emails from this batch "
            "(feature a different one unless none of the others fit this contact):\n"
            + "\n".join(f"- {a}" for a in used_accomplishments)
        )

    raw = await ask_json(
        prompts.DRAFT_SYSTEM, user, web_search=False, on_progress=on_progress,
        label=f"draft:{company_name}", cache_prefix=cache_prefix,
        schema=prompts.DRAFT_SCHEMA,
        # Drafting is rule-bound (follows DRAFT_SYSTEM's explicit constraints)
        # rather than open-ended, and doesn't touch verification or research
        # depth -- a cost/quality tradeoff worth taking here specifically.
        # Contact identification and research stay at the "high" default.
        effort="medium",
    )

    unbound = _unbound_claims(raw, len(research.items))
    if unbound:
        # The draft admitted (via claims_used) to claims no research item
        # supports — exactly the elaboration failures the web fact-check
        # keeps flagging (invented contrasts, merged items, upgraded specifics).
        # ONE grounding redraft now, ~15s with no web searches, is far cheaper
        # than the fact-check's flag -> revise -> web recheck cycle later.
        # The fact-check itself is untouched and still checks everything.
        logger.warning(
            "draft:%s: %d claim(s) not grounded in research items: %s — one grounding redraft",
            company_name, len(unbound), "; ".join(unbound),
        )
        if on_progress:
            on_progress("Draft made ungrounded claims — redrafting…")
        redo_user = (
            user
            + "\n\nYour previous attempt (below) asserted claims that no numbered "
            "research item supports:\n"
            + "\n".join(f"- {c}" for c in unbound)
            + "\n\nWrite the email again: drop each unsupported COMPANY/CONTACT "
            "claim or soften it to exactly what a research item states. If a "
            "listed claim is actually about the CANDIDATE's own background, keep "
            "it as-is — it is supported by the resume, not the research items — "
            "and simply leave it out of claims_used. Everything else about the "
            "task is unchanged.\n\nPrevious attempt:\n"
            + json.dumps(
                {"subject": raw.get("subject", ""), "body": raw.get("body", "")},
                ensure_ascii=False,
            )
        )
        redo = await ask_json(
            prompts.DRAFT_SYSTEM, redo_user, web_search=False, on_progress=on_progress,
            label=f"reground:{company_name}", cache_prefix=cache_prefix,
            schema=prompts.DRAFT_SCHEMA, effort="medium",
        )
        redo_unbound = _unbound_claims(redo, len(research.items))
        if len(redo_unbound) < len(unbound):
            raw, unbound = redo, redo_unbound
        if unbound:
            # One attempt, never a loop — whatever is still unbound gets its
            # chance at the independent web fact-check like any other claim.
            logger.warning(
                "draft:%s: %d claim(s) still ungrounded after redraft: %s",
                company_name, len(unbound), "; ".join(unbound),
            )

    hits = banned_style_hits(f"{raw.get('subject', '')}\n{raw.get('body', '')}")
    if hits:
        # One rewording pass — BEFORE the fact-check, so the checked draft is
        # the exact text that ships. Real runs shipped "there's a fit" and
        # "caught my attention" in 4 of 10 drafts while this was log-only.
        # Adopted only when strictly cleaner; leftovers stay on the result.
        logger.warning(
            "draft:%s: banned phrasing in draft: %s — one style redraft",
            company_name, ", ".join(hits),
        )
        if on_progress:
            on_progress("Rewording banned phrasing…")
        redo = await _style_redraft(
            candidate, company_name,
            str(raw.get("subject", "")), str(raw.get("body", "")),
            hits, on_progress=on_progress,
        )
        if redo:
            # Keep the original featured_accomplishment/claims bookkeeping —
            # only the wording was allowed to change.
            raw["subject"], raw["body"] = redo.get("subject", ""), redo.get("body", "")
            hits = banned_style_hits(f"{raw['subject']}\n{raw['body']}")

    accomplishment = str(raw.get("featured_accomplishment", "")).strip()
    if used_accomplishments is not None and accomplishment:
        used_accomplishments.append(accomplishment)

    return DraftResult(
        subject=str(raw.get("subject", "")).strip(),
        body=finalize_body(str(raw.get("body", "")), contact.first_name),
        featured_accomplishment=accomplishment,
        # Banned phrasing detected in the draft (empty when clean) — kept on
        # the result for logs and tests; the report doesn't render it.
        banned_phrases=hits,
    )


# ------------------------------------------------------------------ #
# Step 6: Draft fact-check (+ one targeted revision)
# ------------------------------------------------------------------ #
def _flagged_from_claims(claims: list) -> List[str]:
    """Format non-supported claim verdicts into the run report's flag strings."""
    flagged = []
    for item in claims:
        verdict = str(item.get("verdict", "")).strip().lower()
        claim = strip_cite_tags(str(item.get("claim", "")))
        note = strip_cite_tags(str(item.get("note", "")))
        if verdict in ("unsupported", "unverified") and claim:
            flagged.append(f"{verdict}: {claim}" + (f" ({note})" if note else ""))
    return flagged


def substantial_flags(flags: List[str]) -> List[str]:
    """The 'unsupported' flags — claims the independent web check found
    contradicted, or more specific than any source actually states. These are
    the substantial failures that justify escalating the revision (see the
    orchestrator's removal-mode round); 'unverified' only means the search
    budget couldn't settle the claim either way."""
    return [f for f in flags if f.startswith("unsupported")]


async def verify_draft(
    contact: Contact,
    company_name: str,
    domain: str,
    subject: str,
    body: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> VerifyResult:
    """Fact-check the finished draft (body includes the signature; the prompt
    tells the checker to skip it). The checkers deliberately get only the
    contact and the draft — never the research notes, which are themselves
    LLM output and would make the check circular. Every claim is verified
    independently on the web.

    Runs as TWO PARALLEL passes — contact employment/LinkedIn and the
    draft's claims — for the same reason research runs as two: searches
    inside one streamed request execute serially, so the old single
    12-search call was the longest serial link in every company's chain.
    Same combined budget, so no verification depth is lost.

    Never raises on a failed check — a flaky safety net must not discard a
    finished draft. A pass that fails comes back as .error (draft flagged
    "not fact-checked" in the report) while the other pass's findings still
    apply, so a departed-contact verdict survives a failed claims check."""
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name} ({domain})\n"
        f"LinkedIn: {contact.linkedin_url or 'unknown'}\n\n"
        f"Draft subject: {subject}\n"
        f"Draft body:\n{body}\n\n"
        "Fact-check this draft."
    )
    contact_raw, claims_raw = await asyncio.gather(
        ask_json(
            prompts.VERIFY_CONTACT_SYSTEM.format(
                search_budget=config.settings.VERIFY_CONTACT_WEB_SEARCH_MAX_USES
            ),
            user, web_search=True, on_progress=on_progress,
            label=f"verify-contact:{company_name}",
            schema=prompts.VERIFY_CONTACT_SCHEMA,
            max_uses=config.settings.VERIFY_CONTACT_WEB_SEARCH_MAX_USES,
            timeout_seconds=config.settings.VERIFY_CALL_TIMEOUT_SECONDS,
        ),
        ask_json(
            prompts.VERIFY_CLAIMS_SYSTEM.format(
                search_budget=config.settings.VERIFY_CLAIMS_WEB_SEARCH_MAX_USES
            ),
            user, web_search=True, on_progress=on_progress,
            label=f"verify:{company_name}",
            schema=prompts.VERIFY_CLAIMS_SCHEMA,
            max_uses=config.settings.VERIFY_CLAIMS_WEB_SEARCH_MAX_USES,
            timeout_seconds=config.settings.VERIFY_CALL_TIMEOUT_SECONDS,
        ),
        return_exceptions=True,
    )
    # Only LLM failures are the safety-net case; anything else (a bug) should
    # surface loudly, exactly as it did when this was one call.
    for r in (contact_raw, claims_raw):
        if isinstance(r, BaseException) and not isinstance(r, LLMError):
            raise r

    errors = []
    flagged: List[str] = []
    if isinstance(claims_raw, LLMError):
        logger.warning(
            "verify:%s: claims check failed (%s) — draft kept, flagged as unchecked",
            company_name, claims_raw,
        )
        errors.append(f"claims check failed: {claims_raw}")
    else:
        flagged = _flagged_from_claims(claims_raw.get("claims", []))
        if flagged:
            logger.info(
                "verify:%s: %d claim(s) flagged: %s",
                company_name, len(flagged), "; ".join(flagged),
            )

    departed, update = "", ""
    linkedin_verdict, linkedin_correction = "", ""
    if isinstance(contact_raw, LLMError):
        logger.warning(
            "verify-contact:%s: contact check failed (%s) — draft kept, flagged as unchecked",
            company_name, contact_raw,
        )
        errors.append(f"contact check failed: {contact_raw}")
    else:
        # contact_note is only meaningful under one of the two booleans (see
        # VERIFY_CONTACT_SYSTEM): departure evidence under contact_departed,
        # the newer title under contact_title_changed. Anything else (models
        # have filled it with "still listed as X" confirmations) is discarded
        # rather than rendered as a warning banner.
        note = strip_cite_tags(str(contact_raw.get("contact_note", "")))
        if bool(contact_raw.get("contact_departed")):
            departed = note or "fact-check found evidence the contact left the company"
        elif bool(contact_raw.get("contact_title_changed")) and note:
            update = note
        linkedin_verdict = str(contact_raw.get("linkedin_url_verdict", "")).strip().lower()
        linkedin_correction = str(contact_raw.get("linkedin_url_correction", "")).strip()

    return VerifyResult(
        flagged_claims=flagged,
        departed_evidence=departed,
        contact_update=update,
        linkedin_verdict=linkedin_verdict,
        linkedin_correction=linkedin_correction,
        error="; ".join(errors),
    )


async def revise_flagged_draft(
    candidate: Candidate,
    contact: Contact,
    company_name: str,
    research: ResearchResult,
    subject: str,
    body_without_signature: str,
    flagged_claims: List[str],
    remove_entirely: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[DraftResult]:
    """One targeted revision of a draft whose fact-check flagged claims:
    one attempt per call, keep whichever version is cleaner, never loop here
    (the orchestrator decides whether a removal-mode escalation round runs).

    `remove_entirely` switches the instruction from soften-to-what-the-note-
    leaves-standing to replace-or-remove: the call gets a small web-search
    budget (REVISE_WEB_SEARCH_MAX_USES) to verify a corrected claim and only
    cuts the claim when nothing verifiable replaces it. Used when the soften
    round still failed the recheck on 'unsupported' (contradicted) claims, or
    produced nothing adoptable at all.

    The revised draft is re-checked by a claims-only web call — same
    independent standard as the main fact-check (a notes-based recheck would
    reintroduce the circularity the main check avoids) — and returned only
    when it is no worse than the original on BOTH total flags and
    'unsupported' flags (an equal-count trade of an unverified claim for a
    contradicted one is a downgrade). Returns None on any failure or when
    the revision came back worse — the caller keeps the original draft and
    its flags either way.
    """
    if remove_entirely:
        instruction = (
            "\n\nAn earlier, softened revision still failed the independent "
            "fact-check on these claims. For each flagged claim, use web "
            "search to check what is actually true, then either REPLACE the "
            "claim with a corrected version stating only what a source you "
            "just found says, or — if you cannot verify a replacement within "
            "budget — REMOVE the claim entirely and rework the affected "
            "sentences so the email reads naturally without it. Never assert "
            "anything from memory: every claim you add must come from a "
            "search result you just read. Your search budget is about "
            f"{config.settings.REVISE_WEB_SEARCH_MAX_USES} searches; once it "
            "is spent the tool rejects further searches (max_uses_exceeded) — "
            "that is expected, just write the email with what you have. Keep "
            "the structure, tone, and featured accomplishment the same.\n\n"
        )
    else:
        instruction = (
            "\n\nRewrite the email so it no longer asserts the flagged specifics: "
            "soften each flagged claim to what its flag note leaves standing, or "
            "drop the point entirely if nothing survives. Do not introduce new "
            "specific factual claims. Keep the structure, tone, and featured "
            "accomplishment the same.\n\n"
        )
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name}\n\n"
        f"Personalization research:\n{research.as_prompt_payload()}\n\n"
        "An independent web fact-check flagged these claims in your draft "
        "(below) as unsupported or unverifiable:\n"
        + "\n".join(f"- {c}" for c in flagged_claims)
        + instruction
        + "Draft to revise:\n"
        + json.dumps({"subject": subject, "body": body_without_signature}, ensure_ascii=False)
    )
    # Round 1 revises from the flag notes + research items alone (fast, and
    # the truth it needs is already in context). Removal mode gets a small
    # search budget so it can verify a replacement instead of only cutting —
    # anything it writes still faces the independent recheck below.
    if remove_entirely:
        revise_kwargs: dict = dict(
            web_search=True,
            max_uses=config.settings.REVISE_WEB_SEARCH_MAX_USES,
            timeout_seconds=config.settings.VERIFY_CALL_TIMEOUT_SECONDS,
        )
    else:
        revise_kwargs = dict(web_search=False)
    try:
        raw = await ask_json(
            prompts.DRAFT_SYSTEM, user, on_progress=on_progress,
            label=f"revise:{company_name}", cache_prefix=_draft_cache_prefix(candidate),
            schema=prompts.DRAFT_SCHEMA, effort="medium", **revise_kwargs,
        )
        revised_subject = str(raw.get("subject", "")).strip()
        revised_body = finalize_body(str(raw.get("body", "")), contact.first_name)
        if not (revised_subject and revised_body):
            return None
        style_hits = banned_style_hits(f"{revised_subject}\n{revised_body}")
        if style_hits:
            # Revisions reintroduce banned wording the first draft's style
            # pass already fixed (a real run's adopted revision shipped
            # "there's a fit" this way). Reword BEFORE the recheck below, so
            # the independently rechecked text is the text that ships.
            redo = await _style_redraft(
                candidate, company_name, revised_subject, revised_body,
                style_hits, on_progress=on_progress,
            )
            if redo:
                revised_subject = str(redo.get("subject", "")).strip()
                revised_body = finalize_body(str(redo.get("body", "")), contact.first_name)
        recheck = await ask_json(
            prompts.RECHECK_SYSTEM.format(
                search_budget=config.settings.RECHECK_WEB_SEARCH_MAX_USES
            ),
            f"Contact: {contact.full_name}, {contact.title} at {company_name}\n\n"
            f"Draft subject: {revised_subject}\n"
            f"Draft body:\n{revised_body}\n\n"
            "Fact-check this revised draft.",
            web_search=True, on_progress=on_progress,
            label=f"recheck:{company_name}", schema=prompts.RECHECK_SCHEMA,
            max_uses=config.settings.RECHECK_WEB_SEARCH_MAX_USES,
            timeout_seconds=config.settings.VERIFY_CALL_TIMEOUT_SECONDS,
        )
    except LLMError as exc:
        logger.warning(
            "revise:%s: revision failed (%s) — keeping the original draft and its flags",
            company_name, exc,
        )
        return None

    new_flags = _flagged_from_claims(recheck.get("claims", []))
    if (
        len(new_flags) > len(flagged_claims)
        or len(substantial_flags(new_flags)) > len(substantial_flags(flagged_claims))
    ):
        logger.info(
            "revise:%s: revision rechecked worse (%d flags / %d unsupported, "
            "was %d / %d) — keeping original",
            company_name, len(new_flags), len(substantial_flags(new_flags)),
            len(flagged_claims), len(substantial_flags(flagged_claims)),
        )
        return None
    logger.info(
        "revise:%s: revision adopted (%d flag(s), was %d)",
        company_name, len(new_flags), len(flagged_claims),
    )
    return DraftResult(
        subject=revised_subject,
        body=revised_body,
        flagged_claims=new_flags,
        # The revision can reintroduce banned wording the first draft avoided —
        # re-derive the diagnostic for the body that actually ships.
        banned_phrases=banned_style_hits(f"{revised_subject}\n{revised_body}"),
    )
