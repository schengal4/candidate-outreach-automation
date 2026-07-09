"""The outreach pipeline (spec steps 1-5).

Step 1  Company discovery (LLM + web search) -> candidate review gate
Step 2  Contact identification, one at a time: find the best contact, try
        Hunter on them immediately, and only look for a backup contact if
        that fails. Avoids paying to research a backup that never gets used.
Step 3  Email lookup via Hunter (interleaved with step 2 -- see above)
Step 4  Personalization research (+ optional red flag detection)
Step 5  Draft generation (+ plain sign-off signature, auto-add to Sent List)

Steps 2-5 run sequentially within a company; companies run concurrently.
"""

import asyncio
import json
import logging
import random
import re
import time
from typing import Callable, Dict, List, Optional

from . import hunter_async, run_store, sent_list
from .config import (
    MAX_COMPANIES_HARD_CAP,
    RUN_HARD_TIMEOUT_SECONDS,
    RUN_LAUNCH_JITTER_SECONDS,
)
from .llm import LLMError, ask_json
from .models import (
    Candidate,
    CompanyState,
    CompanyStatus,
    Contact,
    RunPhase,
    RunState,
)

logger = logging.getLogger("app.pipeline")

# Registry of runs — seeded from disk at startup (see app/run_store.py) so a
# server restart doesn't lose finished reports or a run parked at the review
# gate. Live state during a run is in-memory; checkpoints persist it.
RUNS: Dict[str, RunState] = run_store.load_all_runs()


def _candidate_profile_block(c: Candidate) -> str:
    parts = [
        f"Resume:\n{c.resume_text}",
        f"Current employer (MUST NOT be targeted): {c.current_employer}",
    ]
    if c.linkedin_url:
        parts.append(f"LinkedIn: {c.linkedin_url}")
    if c.career_goals:
        parts.append(f"Career goals: {c.career_goals}")
    if c.culture_prefs:
        parts.append(f"Culture preferences: {c.culture_prefs}")
    if c.target_industry_role:
        parts.append(f"Target industry/role: {c.target_industry_role}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ #
# Step 1: Company discovery
# ------------------------------------------------------------------ #
DISCOVERY_SYSTEM = """You are a job-search research assistant. You identify real companies that are a strong match for a candidate, using web search to verify each company exists and to check for recent signals (hiring, growth, relevant teams).

Rules:
- NEVER include the candidate's current employer.
- NEVER include any company on the already-contacted list in the user message, if one is given — the candidate has recently reached out there and re-surfacing it wastes one of the requested slots.
- Prefer companies where the candidate's target role plausibly exists.
- Every company must have a real primary web domain (e.g. "stripe.com").
- Respond with ONLY a JSON object, no prose, no markdown fences:
  {"companies": [{"name": "...", "domain": "...", "reason": "one sentence on the fit"}]}"""

# JSON Schemas enforced server-side via structured outputs (see ask_json's
# `schema` param): the reply is then guaranteed to parse — an unescaped quote
# in a draft body used to drop the company at the last, most expensive step.
DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "domain": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "domain", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["companies"],
    "additionalProperties": False,
}


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
        f"{_candidate_profile_block(candidate)}{excluded_block}"
    )
    raw = await ask_json(
        DISCOVERY_SYSTEM, user, web_search=True, on_progress=on_progress, label="discovery",
        schema=DISCOVERY_SCHEMA,
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
# Step 2 + 3: Contact identification, one at a time, gated on Hunter
# ------------------------------------------------------------------ #
CONTACT_SYSTEM = """You identify ONE hiring-relevant contact at a specific company for a job seeker's outreach — the person most likely to be the candidate's boss or boss's boss ("boss hunting": line leaders accountable for the quality of their team, never recruiters, HR, or a generic inbox).

Calibrate seniority to company size (use web search to gauge headcount when unsure):
- Small startup (under ~150 people): the founder, CEO, or CTO is the right contact — at that size they are the de facto hiring manager.
- Mid-size (~150-1000): a Director or VP of the function matching the candidate's target role.
- Large company (1000+): an Engineering Manager, team lead, or Director inside the relevant org — NOT C-suite or SVP/GM level. At that scale an executive is several levels above where the candidate would sit and the email lands as misaddressed; prefer the most senior person who would still plausibly interview this candidate. Beware that the most-quoted name in press coverage is usually too senior — search for the team-level leader instead of defaulting to whoever is easiest to find.

You MUST verify this person still works at the company using dated evidence found via web search (a recent post, a dated article, an updated profile/title with a source date). If you cannot find dated evidence, set employment_verified to false — do NOT guess or infer.

If the user message lists excluded contacts, do not pick any of them.

Respond with ONLY a JSON object, no prose, no markdown fences:
{"first_name": "...", "last_name": "...", "title": "...", "linkedin_url": "...", "employment_verified": true, "evidence": "dated evidence snippet with source"}"""

CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "title": {"type": "string"},
        "linkedin_url": {"type": "string"},
        "employment_verified": {"type": "boolean"},
        "evidence": {"type": "string"},
    },
    "required": ["first_name", "last_name", "title", "linkedin_url", "employment_verified", "evidence"],
    "additionalProperties": False,
}


async def identify_contact(
    candidate: Candidate,
    company: CompanyState,
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
        f"Company: {company.name} ({company.domain})\n\n"
        f"Excluded contacts (do not pick these):\n{excluded}\n\n"
        "Identify the single best contact."
    )
    raw = await ask_json(
        CONTACT_SYSTEM, user, web_search=True, on_progress=on_progress, label=label,
        cache_prefix=cache_prefix, schema=CONTACT_SCHEMA,
    )
    return Contact.from_dict(raw)


async def lookup_email_single(company: CompanyState, contact: Contact, blocked_emails: set) -> bool:
    """Try Hunter for exactly one contact. Sets contact_used/email/email_score on success."""
    if not (contact.first_name and contact.last_name):
        return False
    email, score = await hunter_async.find_email(company.domain, contact.first_name, contact.last_name)
    if not email or email.strip().lower() in blocked_emails:
        return False
    company.contact_used = contact
    company.email = email
    company.email_score = score
    return True


# ------------------------------------------------------------------ #
# Step 4: Personalization research
# ------------------------------------------------------------------ #
RESEARCH_SYSTEM = """You research one professional contact to personalize a job seeker's outreach email. Use web search to find their recent posts, articles, talks, projects, or company news they were involved in. Prefer recent, specific, verifiable items.

Write the summary and items in plain, specific language, like a person taking notes for themselves. Avoid words like "delve," "underscore," "showcase," "leverage," or "robust," and avoid em dashes.

{red_flag_clause}

Respond with ONLY a JSON object, no prose, no markdown fences:
{{"summary": "2-3 sentence research summary",
  "items": ["specific recent post/article/project 1", "..."],
  "red_flags": ["..."]}}
red_flags must be [] when none were found or red-flag screening is disabled."""

RED_FLAG_ON = (
    "Red-flag screening IS enabled: also check for scam signals (fake company, "
    "pay-to-work schemes, MLM recruiting) and concerning public social media "
    "indicators about this person. List anything concerning in red_flags with a "
    "one-line explanation and source. Do not exaggerate; only report real findings."
)
RED_FLAG_OFF = "Red-flag screening is DISABLED: leave red_flags as an empty array."

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "items", "red_flags"],
    "additionalProperties": False,
}

# Web-search replies sometimes leak literal <cite index="..."> markup into the
# JSON strings; it renders raw in the run report and could bleed into a draft.
_CITE_TAG_RE = re.compile(r"</?cite[^>]*>")


def _strip_cite_tags(text: str) -> str:
    return _CITE_TAG_RE.sub("", text).strip()


async def research_contact(
    company: CompanyState,
    red_flags_enabled: bool,
    on_progress: Optional[Callable[[str], None]] = None,
) -> None:
    contact = company.contact_used
    system = RESEARCH_SYSTEM.format(
        red_flag_clause=RED_FLAG_ON if red_flags_enabled else RED_FLAG_OFF
    )
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company.name} ({company.domain})\n"
        f"LinkedIn: {contact.linkedin_url or 'unknown'}\n\n"
        "Gather personalization research for outreach to this person."
    )
    raw = await ask_json(
        system, user, web_search=True, on_progress=on_progress, label=f"research:{company.name}",
        schema=RESEARCH_SCHEMA,
    )
    company.research_summary = _strip_cite_tags(str(raw.get("summary", "")))
    company.research_items = [_strip_cite_tags(str(x)) for x in raw.get("items", []) if x]
    company.red_flags = (
        [_strip_cite_tags(str(x)) for x in raw.get("red_flags", []) if x]
        if red_flags_enabled else []
    )


# ------------------------------------------------------------------ #
# Step 5: Generate outreach
# ------------------------------------------------------------------ #
DRAFT_SYSTEM = """You write short, personalized cold outreach emails for a job seeker contacting a hiring-relevant person at a target company. The candidate's resume is attached to the email separately, so the email itself does not need to summarize their whole background.

Requirements:
- 120-180 words body. Warm, specific, zero fluff, no false claims.
- Start with a greeting line addressing the contact by first name (e.g. "Hi Razik," or "Dear Razik,"), then a blank line before the first paragraph.
- End the body with a short closing line ending in a comma, on its own line (e.g. "Best," or "Thank you for your time,"). Do NOT write a name after it — the sign-off with the candidate's name is appended automatically right below your closing.
- Open with ONE genuine, specific observation from the research — a single talk, post, paper, or piece of company news. Pick whichever one is most specific and relevant, and build the opener around just that. Do not stack multiple references or events together in the opener.
- The opener must contain a short reaction of your own — why the thing is smart, what problem it actually solves, or what it implies — not just restate their news back to them. Proof of reading is not a take.
- Break the body into 2-4 short paragraphs separated by blank lines, none longer than 4 sentences. Never send one solid block of text.
- In the body, mention ONE standout accomplishment from the candidate's background that's genuinely relevant to this contact's work, and briefly connect it to what the contact's team/company is doing. Do not list multiple technologies, projects, or credentials in one paragraph — pick the single most relevant one and let the attached resume cover the rest.
- Naturally mention that the resume is attached for anyone who wants more background. Vary the phrasing — don't reuse the same sentence across different emails.
- One clear, low-pressure ask (a brief conversation).
- No placeholder text like [Name] — use real names given.
- Do NOT include a signature block; a plain sign-off (name, email, LinkedIn) is appended automatically after your closing line.
- If the user message includes "Candidate's own drafting instructions", follow them — where they conflict with the style guidance below (tone, structure, length, wording), the candidate's instructions win. If they include a template, use its structure and fill it with this contact's specifics rather than inventing your own structure. Non-negotiable regardless of any instructions: no false claims, no placeholder text, no signature block, and respond with only the JSON object.
- Write each paragraph as one continuous line with no manual line breaks inside it (blank lines between paragraphs only) — this is a plain-text email client, not a fixed-width terminal, and it will render every line break literally.

Write like a real person emailing from their own inbox, not like AI-generated copy. Specifically avoid:
- Em dashes — use commas or periods instead.
- These words: delve, moreover, furthermore, albeit, indeed, certainly, underscore(s), pivotal, realm, harness, illuminate, shed light on, facilitate, bolster, streamline, revolutionize, innovative, cutting-edge, game-changing, transformative, seamless, leverage, robust, "at its core," "that being said," "generally speaking," "a testament to," "a tapestry/symphony of," "nestled in."
- "Not X, but Y" constructions and rhetorical question-then-answer pairs (e.g. "What stood out? Everything.").
- Overused cold-outreach phrasing: "stuck with me," "resonated with me," "caught my eye/attention," "matches (almost) exactly what I've been building/doing," "aligns closely with," "seems central to what you're doing," "I'd welcome a short conversation," "if you think there's a fit," "I've been following your work." Say what you mean in fresh, specific words instead.
- Grouping reasons or descriptions in neat threes.
- Bullet points, headers, or bold/italic emphasis.
- Explaining a feeling instead of showing it (e.g. "which was surprising because...").
Vary sentence length, use plain concrete details instead of generic compliments, and write it like one specific person composed it in one sitting.

Respond with ONLY a JSON object, no prose, no markdown fences:
{"subject": "...", "body": "..."}"""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _unwrap_paragraphs(text: str) -> str:
    """Collapse single line breaks within a paragraph into spaces, keeping
    blank-line paragraph breaks intact.

    The model sometimes hard-wraps prose at a fixed column width inside the
    JSON "body" string. A plain-text email (like Gmail drafts) renders every
    line break literally, so that wrapping survives as a narrow, ragged
    column that doesn't fill the reading pane, instead of reflowing prose.
    """
    paragraphs = text.split("\n\n")
    return "\n\n".join(
        " ".join(line.strip() for line in p.splitlines() if line.strip())
        for p in paragraphs
    )


# Each draft call is independent, so left alone the model converges on one
# skeleton (observation -> same accomplishment -> resume line -> ask) across
# every email in a run. Recipients within a run cluster in one professional
# community — and that skeleton is also what everyone else's AI outreach
# sounds like — so rotate the structural lead per draft to break the mold.
_DRAFT_STRUCTURE_HINTS = [
    "Structure this one: lead with the research observation and your reaction to it, then connect the candidate's work to it.",
    "Structure this one: lead with the most relevant thing the candidate built, then tie it to what this contact is working on.",
    "Structure this one: lead with a specific point or question about the contact's work, using the candidate's experience to back it up.",
    "Structure this one: lead with the shared problem both the contact and the candidate are working on, then get specific about each side.",
]

# Belt and suspenders on top of DRAFT_SYSTEM's greeting/closing rules: real
# runs produced drafts that opened mid-thought or ended with no sign-off, so
# the guarantee can't live in the prompt alone.
_GREETING_RE = re.compile(r"^(hi|hello|hey|dear)\b", re.IGNORECASE)


def _ensure_greeting_and_closing(body: str, first_name: str) -> str:
    """Guarantee the email opens by addressing the contact and ends with a
    closing line ("Best," etc.) ahead of the auto-appended signature."""
    body = body.strip()
    if not body:
        return body
    if first_name and not _GREETING_RE.match(body):
        body = f"Hi {first_name},\n\n{body}"
    if not body.splitlines()[-1].strip().endswith(","):
        body += "\n\nBest,"
    return body


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def _break_up_wall_of_text(body: str, max_sentences: int = 4) -> str:
    """Backstop for DRAFT_SYSTEM's paragraph rule: split any paragraph longer
    than max_sentences into roughly equal chunks of whole sentences. Real runs
    occasionally produced the entire email as one solid block, which is the
    single fastest way to get a cold email closed unread."""
    out = []
    for p in body.split("\n\n"):
        sentences = _SENTENCE_END_RE.split(p.strip())
        if len(sentences) <= max_sentences:
            out.append(p)
            continue
        n_chunks = -(-len(sentences) // 3)  # ceil: aim for ~3 sentences each
        size = -(-len(sentences) // n_chunks)
        out.extend(
            " ".join(sentences[i : i + size]) for i in range(0, len(sentences), size)
        )
    return "\n\n".join(out)


def email_signature(candidate: Candidate) -> str:
    """A plain sign-off appended to every draft: name, email, LinkedIn if given —
    like a normal email, not bulk-mail boilerplate. Not editable by the candidate."""
    lines = [candidate.name]
    if candidate.email:
        lines.append(candidate.email)
    if candidate.linkedin_url:
        lines.append(candidate.linkedin_url)
    return "\n\n--\n" + "\n".join(lines)


async def draft_email(
    candidate: Candidate,
    company: CompanyState,
    on_progress: Optional[Callable[[str], None]] = None,
) -> None:
    contact = company.contact_used
    research = {
        "summary": company.research_summary,
        "items": company.research_items,
    }
    # The candidate profile (including the full resume, up to 30k chars) and
    # their drafting instructions are identical on every company's draft call
    # in this run — cache them separately from the per-company
    # contact/research part below, instead of paying full price to resend
    # the whole resume on every draft.
    cache_prefix = f"Candidate profile:\n{_candidate_profile_block(candidate)}"
    if candidate.draft_instructions:
        cache_prefix += (
            f"\n\nCandidate's own drafting instructions (follow these — "
            f"see system prompt for precedence):\n{candidate.draft_instructions}"
        )
    user = (
        f"Contact: {contact.full_name}, {contact.title} at {company.name}\n\n"
        f"Personalization research:\n{json.dumps(research, indent=2, ensure_ascii=False)}\n\n"
        f"Write the outreach email. {random.choice(_DRAFT_STRUCTURE_HINTS)}"
    )
    raw = await ask_json(
        DRAFT_SYSTEM, user, web_search=False, on_progress=on_progress, label=f"draft:{company.name}",
        cache_prefix=cache_prefix, schema=DRAFT_SCHEMA,
        # Drafting is rule-bound (follows DRAFT_SYSTEM's explicit constraints)
        # rather than open-ended, and doesn't touch verification or research
        # depth -- a cost/quality tradeoff worth taking here specifically.
        # Contact identification and research stay at the "high" default.
        effort="medium",
    )
    body = _unwrap_paragraphs(str(raw.get("body", "")).strip())
    body = _break_up_wall_of_text(body)
    body = _ensure_greeting_and_closing(body, contact.first_name)
    company.draft_subject = str(raw.get("subject", "")).strip()
    company.draft_body = body + email_signature(candidate)


# ------------------------------------------------------------------ #
# Orchestration
# ------------------------------------------------------------------ #
async def _run_company(
    candidate: Candidate,
    company: CompanyState,
    recently_contacted_names: List[str],
    blocked_emails: set,
) -> None:
    """Steps 2 -> 5 sequentially for one company.

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
        company.primary = await identify_contact(
            candidate,
            company,
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
            found_email = await lookup_email_single(company, company.primary, blocked_emails)

        if not found_email:
            # Primary wasn't verified or Hunter couldn't find them — only now
            # spend a second call looking for a backup contact.
            set_status(CompanyStatus.CONTACTS)
            excluded = recently_contacted_names + [company.primary.full_name]
            company.backup = await identify_contact(
                candidate,
                company,
                excluded,
                on_progress=report,
                label=f"backup:{company.name}",
            )

            if not (company.backup and company.backup.employment_verified):
                set_status(CompanyStatus.DROPPED, "employment could not be verified for any contact")
                return

            set_status(CompanyStatus.EMAIL)
            found_email = await lookup_email_single(company, company.backup, blocked_emails)

        if not found_email:
            set_status(CompanyStatus.DROPPED, "no valid email found")
            return

        set_status(CompanyStatus.RESEARCH)
        await research_contact(company, candidate.red_flag_detection, on_progress=report)

        set_status(CompanyStatus.DRAFTING)
        await draft_email(candidate, company, on_progress=report)

        # Auto-add to Sent List on draft generation (date_sent = today).
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


async def run_discovery(run: RunState, candidate: Candidate) -> None:
    """Phase 1: discovery, then park at the candidate review gate."""

    def report(text: str) -> None:
        run.activity = text

    try:
        count = min(candidate.max_companies, MAX_COMPANIES_HARD_CAP)
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
        run.discovered = await discover_companies(
            candidate, count, on_progress=report, excluded_domains=excluded_domains
        )
        run.activity = ""
        if not run.discovered:
            logger.warning("Run %s: discovery returned no companies", run.id)
            run.phase = RunPhase.ERROR
            run.error = "Discovery returned no companies."
            return
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
    """Phases 2-5 for all approved companies, concurrently.

    Cuts the run short — keeping whatever companies already finished — if
    either the user clicks the "retrieve what's done" button (run.stop_event,
    set by the /stop_early route) or RUN_HARD_TIMEOUT_SECONDS elapses,
    whichever happens first. A single web-search-heavy company can otherwise
    take much longer than a user wants to wait on the whole run.
    """
    try:
        approved = [c for c in run.discovered if c["domain"] in set(approved_domains)]
        run.companies = [
            CompanyState(name=c["name"], domain=c["domain"], reason=c.get("reason", ""))
            for c in approved[:MAX_COMPANIES_HARD_CAP]
        ]
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

        # No concurrency cap here — every company fires its LLM calls at once,
        # staggered by a small random delay (see RUN_LAUNCH_JITTER_SECONDS) so
        # the initial burst doesn't hit the rate-limit window in one instant.
        # Sustained rate-limit pressure beyond that is absorbed by the retry-
        # with-backoff in llm.py's _send_request, not by throttling companies.
        async def _staggered(company: CompanyState) -> None:
            if RUN_LAUNCH_JITTER_SECONDS > 0:
                await asyncio.sleep(random.uniform(0, RUN_LAUNCH_JITTER_SECONDS))
            try:
                await _run_company(candidate, company, recently_contacted_names, blocked_emails)
            finally:
                # Checkpoint after every company settles (done or dropped) so a
                # mid-run restart keeps every draft finished up to that point.
                run_store.save_run(run)

        company_tasks = [
            asyncio.create_task(_staggered(c))
            for c in run.companies
        ]
        all_done = asyncio.gather(*company_tasks)
        stop_waiter = asyncio.create_task(run.stop_event.wait())
        finished, _ = await asyncio.wait(
            {all_done, stop_waiter},
            timeout=RUN_HARD_TIMEOUT_SECONDS,
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
                if company.status not in (CompanyStatus.DONE, CompanyStatus.DROPPED):
                    company.status = CompanyStatus.DROPPED
                    company.drop_reason = cut_reason
                    company.activity = ""

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
