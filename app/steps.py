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

    def as_prompt_payload(self) -> str:
        return json.dumps(
            {"summary": self.summary, "items": self.items},
            indent=2, ensure_ascii=False,
        )


@dataclass
class DraftResult:
    subject: str
    body: str  # finalized (greeting/closing/paragraphs), WITHOUT signature
    featured_accomplishment: str = ""
    # Banned style phrasings that survived the one redraft attempt — the run
    # report surfaces them as a "review the wording" flag.
    banned_phrases: List[str] = field(default_factory=list)
    # For revisions: claims the grounding recheck still flagged.
    flagged_claims: List[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    # Claims the fact-check could not ground — "unsupported: ..." /
    # "unverified: ..." strings shown as a review flag in the run report.
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
    on_progress: Optional[Callable[[str], None]] = None,
    label: str = "contact",
) -> Optional[Contact]:
    excluded = "\n".join(f"- {n}" for n in excluded_names) or "(none)"
    # Candidate-specific but company-independent — identical on every
    # identify_contact call in this run (primary + backup, every company),
    # so it's cached separately from the company-specific part below.
    cache_prefix = (
        f"Candidate's target role/industry: {candidate.target_industry_role or 'see resume'}\n\n"
        f"Candidate summary (for relevance):\n{candidate.resume_text[:3000]}"
    )
    user = (
        f"Company: {company_name} ({domain})\n\n"
        f"Excluded contacts (do not pick these):\n{excluded}\n\n"
        "Identify the single best contact."
    )
    raw = await ask_json(
        prompts.CONTACT_SYSTEM, user, web_search=True, on_progress=on_progress, label=label,
        cache_prefix=cache_prefix, schema=prompts.CONTACT_SCHEMA,
        # Shorter leash than research: contact calls reliably spend their whole
        # search budget, so the budget IS the call duration (see config).
        max_uses=config.settings.CONTACT_WEB_SEARCH_MAX_USES,
        timeout_seconds=config.settings.CONTACT_CALL_TIMEOUT_SECONDS,
    )
    contact = Contact.from_dict(raw)
    # Anti-fabrication gate: a LinkedIn URL without provenance (which search
    # result showed it) is a guessed slug until proven otherwise — real runs
    # shipped plausible-looking /in/first-last URLs that belonged to strangers.
    # Better no link (the UI can fall back to a LinkedIn search) than a wrong
    # person's profile presented as fact.
    if contact and contact.linkedin_url and isinstance(raw, dict):
        if not str(raw.get("linkedin_url_source", "")).strip():
            logger.info(
                "%s: dropping unsourced LinkedIn URL %s (no search result cited)",
                label, contact.linkedin_url,
            )
            contact.linkedin_url = ""
    return contact


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
async def research_contact(
    contact: Contact,
    company_name: str,
    domain: str,
    red_flags_enabled: bool,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ResearchResult:
    system = prompts.RESEARCH_SYSTEM.format(
        red_flag_clause=prompts.RED_FLAG_ON if red_flags_enabled else prompts.RED_FLAG_OFF
    )
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name} ({domain})\n"
        f"LinkedIn: {contact.linkedin_url or 'unknown'}\n\n"
        "Gather personalization research for outreach to this person."
    )

    async def _call(max_uses: int):
        return await ask_json(
            system, user, web_search=True, on_progress=on_progress,
            label=f"research:{company_name}", schema=prompts.RESEARCH_SCHEMA,
            # Tighter than the default budget: research legitimately reads a
            # few sources, but uncapped it burned 8 searches / 9-10 minutes on
            # some companies and gated the whole run's wall-clock (see config).
            max_uses=max_uses,
            timeout_seconds=config.settings.RESEARCH_CALL_TIMEOUT_SECONDS,
        )

    try:
        raw = await _call(config.settings.RESEARCH_WEB_SEARCH_MAX_USES)
    except LLMTimeoutError:
        # By this point the company has a verified contact and usually an
        # email — dropping it over a wandering research call is the most
        # expensive kind of drop (a real run lost 4 of 10 companies here).
        # One retry on a halved search budget: tighter calls reliably finish.
        retry_uses = config.settings.RESEARCH_TIMEOUT_RETRY_MAX_USES
        logger.warning(
            "research:%s: timed out — one retry with a smaller budget (%d searches)",
            company_name, retry_uses,
        )
        if on_progress:
            on_progress("Research ran long — retrying with a tighter budget…")
        raw = await _call(retry_uses)
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
    return ResearchResult(
        summary=strip_cite_tags(str(raw.get("summary", ""))),
        items=items,
        red_flags=(
            [strip_cite_tags(str(x)) for x in raw.get("red_flags", []) if x]
            if red_flags_enabled else []
        ),
    )


# ------------------------------------------------------------------ #
# Step 5: Generate outreach
# ------------------------------------------------------------------ #
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

    async def _ask(prompt: str) -> dict:
        return await ask_json(
            prompts.DRAFT_SYSTEM, prompt, web_search=False, on_progress=on_progress,
            label=f"draft:{company_name}", cache_prefix=cache_prefix,
            schema=prompts.DRAFT_SCHEMA,
            # Drafting is rule-bound (follows DRAFT_SYSTEM's explicit constraints)
            # rather than open-ended, and doesn't touch verification or research
            # depth -- a cost/quality tradeoff worth taking here specifically.
            # Contact identification and research stay at the "high" default.
            effort="medium",
        )

    raw = await _ask(user)
    hits = banned_style_hits(f"{raw.get('subject', '')}\n{raw.get('body', '')}")
    if hits:
        # One redraft, then take whichever version leaked less — a second
        # retry rarely helps and doubles the cost of an already-cheap step.
        logger.info(
            "draft:%s: redrafting once — banned phrasing: %s",
            company_name, ", ".join(hits),
        )
        redraft = await _ask(
            user
            + "\n\nYour previous draft (below) used wording the style rules ban: "
            + ", ".join(hits)
            + ". Rewrite it with fresh phrasing that avoids these and the other "
            "banned wordings, keeping the same substance.\n\n"
            + json.dumps(raw, ensure_ascii=False)
        )
        rehits = banned_style_hits(f"{redraft.get('subject', '')}\n{redraft.get('body', '')}")
        if len(rehits) <= len(hits):
            raw, hits = redraft, rehits
        if hits:
            logger.warning(
                "draft:%s: banned phrasing survived redraft: %s",
                company_name, ", ".join(hits),
            )

    accomplishment = str(raw.get("featured_accomplishment", "")).strip()
    if used_accomplishments is not None and accomplishment:
        used_accomplishments.append(accomplishment)

    return DraftResult(
        subject=str(raw.get("subject", "")).strip(),
        body=finalize_body(str(raw.get("body", "")), contact.first_name),
        featured_accomplishment=accomplishment,
        # Whatever banned phrasing survived the redraft (empty when clean) —
        # the run report flags it so the user edits it out before sending,
        # instead of the leak living only in a log line.
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


async def verify_draft(
    contact: Contact,
    company_name: str,
    domain: str,
    research: ResearchResult,
    subject: str,
    body: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> VerifyResult:
    """Fact-check the finished draft (body includes the signature; the prompt
    tells the checker to skip it). Never raises — a flaky safety net must not
    discard a finished draft, so a failed check comes back as .error."""
    system = prompts.VERIFY_SYSTEM.format(
        search_budget=config.settings.VERIFY_WEB_SEARCH_MAX_USES
    )
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name} ({domain})\n"
        f"LinkedIn: {contact.linkedin_url or 'unknown'}\n\n"
        f"Research notes the draft was written from:\n{research.as_prompt_payload()}\n\n"
        f"Draft subject: {subject}\n"
        f"Draft body:\n{body}\n\n"
        "Fact-check this draft."
    )
    try:
        raw = await ask_json(
            system, user, web_search=True, on_progress=on_progress,
            label=f"verify:{company_name}", schema=prompts.VERIFY_SCHEMA,
            max_uses=config.settings.VERIFY_WEB_SEARCH_MAX_USES,
            timeout_seconds=config.settings.VERIFY_CALL_TIMEOUT_SECONDS,
        )
    except LLMError as exc:
        logger.warning(
            "verify:%s: fact-check call failed (%s) — draft kept, flagged as unchecked",
            company_name, exc,
        )
        return VerifyResult(error=str(exc))

    flagged = _flagged_from_claims(raw.get("claims", []))
    if flagged:
        logger.info(
            "verify:%s: %d claim(s) flagged: %s",
            company_name, len(flagged), "; ".join(flagged),
        )
    # contact_note is only meaningful under one of the two booleans (see
    # VERIFY_SYSTEM): departure evidence under contact_departed, the newer
    # title under contact_title_changed. Anything else (models have filled
    # it with "still listed as X" confirmations) is discarded rather than
    # rendered as a warning banner.
    note = strip_cite_tags(str(raw.get("contact_note", "")))
    departed, update = "", ""
    if bool(raw.get("contact_departed")):
        departed = note or "fact-check found evidence the contact left the company"
    elif bool(raw.get("contact_title_changed")) and note:
        update = note
    return VerifyResult(
        flagged_claims=flagged,
        departed_evidence=departed,
        contact_update=update,
        linkedin_verdict=str(raw.get("linkedin_url_verdict", "")).strip().lower(),
        linkedin_correction=str(raw.get("linkedin_url_correction", "")).strip(),
    )


async def revise_flagged_draft(
    candidate: Candidate,
    contact: Contact,
    company_name: str,
    research: ResearchResult,
    subject: str,
    body_without_signature: str,
    flagged_claims: List[str],
    on_progress: Optional[Callable[[str], None]] = None,
) -> Optional[DraftResult]:
    """One targeted revision of a draft whose fact-check flagged claims,
    mirroring the banned-phrase redraft's contract: one attempt, keep
    whichever version is cleaner, never loop.

    The revised draft is re-checked by a grounding-only call (no web — the
    research notes haven't changed since the full check, so draft-vs-notes
    comparison is enough) and returned only when it flags no more claims
    than the original; the result carries the remaining flags. Returns None
    on any failure or when the revision came back worse — the caller keeps
    the original draft and its flags either way.
    """
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company_name}\n\n"
        f"Personalization research:\n{research.as_prompt_payload()}\n\n"
        "A fact-check flagged these claims in your draft (below) as going beyond "
        "what the research actually says, or as unverifiable:\n"
        + "\n".join(f"- {c}" for c in flagged_claims)
        + "\n\nRewrite the email so every factual claim stays within what the "
        "research items state: bring each flagged claim down to its research "
        "item's own specificity, or drop the point entirely if nothing supports "
        "it. Keep the structure, tone, and featured accomplishment the same.\n\n"
        "Draft to revise:\n"
        + json.dumps({"subject": subject, "body": body_without_signature}, ensure_ascii=False)
    )
    try:
        raw = await ask_json(
            prompts.DRAFT_SYSTEM, user, web_search=False, on_progress=on_progress,
            label=f"revise:{company_name}", cache_prefix=_draft_cache_prefix(candidate),
            schema=prompts.DRAFT_SCHEMA, effort="medium",
        )
        revised_subject = str(raw.get("subject", "")).strip()
        revised_body = finalize_body(str(raw.get("body", "")), contact.first_name)
        if not (revised_subject and revised_body):
            return None
        recheck = await ask_json(
            prompts.GROUNDING_SYSTEM,
            f"Research notes:\n{research.as_prompt_payload()}\n\n"
            f"Draft subject: {revised_subject}\n"
            f"Draft body:\n{revised_body}\n\n"
            "Fact-check this draft against the notes.",
            web_search=False, on_progress=on_progress,
            label=f"recheck:{company_name}", schema=prompts.GROUNDING_SCHEMA, effort="medium",
        )
    except LLMError as exc:
        logger.warning(
            "revise:%s: revision failed (%s) — keeping the original draft and its flags",
            company_name, exc,
        )
        return None

    new_flags = _flagged_from_claims(recheck.get("claims", []))
    if len(new_flags) > len(flagged_claims):
        logger.info(
            "revise:%s: revision flagged more claims (%d, was %d) — keeping original",
            company_name, len(new_flags), len(flagged_claims),
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
        # re-derive the wording flag for the body that actually ships.
        banned_phrases=banned_style_hits(f"{revised_subject}\n{revised_body}"),
    )
