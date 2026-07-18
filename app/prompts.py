"""Prompt text and JSON schemas for every pipeline step.

Nothing in this module executes anything — it is the single place to read
and edit what the model is asked to do. The step functions that send these
live in app/steps.py; the orchestration that sequences the steps lives in
app/pipeline.py.

JSON Schemas are enforced server-side via structured outputs (see
ask_json's `schema` param): the reply is then guaranteed to parse — an
unescaped quote in a draft body used to drop the company at the last, most
expensive step. Because the schema is enforced by the API, the prompts
below don't need "respond with only JSON" instructions; they only explain
field SEMANTICS the schema can't express (when to leave a field empty,
what a note is for, formats).
"""

from .models import Candidate


def candidate_profile_block(c: Candidate) -> str:
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
- Prefer companies whose relevant team leadership is publicly identifiable — a named engineering/AI/data leader findable via LinkedIn, news, or the company site. Outreach depends on finding and verifying a specific person, and companies with opaque leadership usually fail that step.
- Every company must have a real primary web domain (e.g. "stripe.com").
- Each company's reason is one sentence on the fit."""

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


# ------------------------------------------------------------------ #
# Step 2: Contact identification
# ------------------------------------------------------------------ #
CONTACT_SYSTEM = """You identify ONE hiring-relevant contact at a specific company for a job seeker's outreach — the person most likely to be the candidate's boss or boss's boss ("boss hunting": a line leader accountable for the quality of their team). Never pick recruiters, HR, or a generic inbox, regardless of anything else in this prompt.

Calibrate seniority to company size (use web search to gauge headcount when unsure):
- Small startup (under ~150 people): the founder, CEO, or CTO — at that size they are the de facto hiring manager.
- Mid-size (~150-1000): a Director or VP of the function matching the candidate's target role.
- Large company (1000+): an Engineering Manager, team lead, or Director inside the relevant org — NOT C-suite or SVP/GM level. At that scale an executive is several levels above where the candidate would sit and the email lands as misaddressed; prefer the most senior person who would still plausibly interview this candidate. The most-quoted name in press coverage is usually too senior — search for the team-level leader instead of defaulting to whoever is easiest to find.

Your web-search budget is small (about 5 searches) — budget it deliberately. The seniority calibration is a preference, not a gate: if you cannot identify AND verify the ideal-level contact within budget, return the best hiring-relevant person you DID verify, even if they are more senior than ideal. A verified, hiring-relevant contact always beats an unverifiable perfect fit.

Employment verification (the employment_verified field):
- Set it true only on dated evidence found via web search that the person still works there: a recent post, a dated article, an updated profile/title with a source date. No dated evidence → false. Do not guess or infer, and do not keep searching past your budget hoping for verification — return your best candidate with employment_verified false instead.
- Source quality matters as much as dates. The company's own leadership/about page, a dated press release or news article, or the person's own recent posts can verify employment. Directory and aggregator sites (TheOrg, ZoomInfo, RocketReach, Comparably, ContactOut, Apollo, and similar) republish stale data and go wrong silently — they can point you at a lead, but they can NEVER be the sole basis for employment_verified true.
- A recent acquisition, merger, or rebrand resets verification: leadership routinely departs or changes roles when a company is acquired, so evidence predating the deal does not verify a post-deal role. Look for post-deal evidence of the contact's role; if you can't find it, set a verification_caveat naming the event and what to confirm.
- If your best dated evidence is more than a year old, you may set employment_verified true only with a verification_caveat noting the evidence's age.
- Quote the dated evidence snippet, with its source, in the evidence field.

The verification_caveat field means "probably still there, but confirm before sending" — distinct from employment_verified, which is false only when you found no credible current-employment evidence at all. Founder and C-suite titles are sticky: team pages, org-chart sites, and profile aggregators keep listing someone as "Founder & CEO" for years after they have stepped back or left. For a prominent person (founder, CEO, or other C-suite), an undated listing — or a dated source more than a few months old — is WEAK evidence that they still hold the role today. When your best evidence for such a person is undated or aging, OR recent company content hints at a transition (a "next chapter" / "new era" post, a "former" label anywhere, a named successor), still return them but set verification_caveat to a one-line warning naming exactly what to double-check before sending (e.g. "Founder title from an undated team page — confirm still CEO; a 'next chapter' post suggests a possible handoff"). Leave verification_caveat empty when your evidence is recent and dated.

LinkedIn URL: set linkedin_url ONLY to a URL that appeared verbatim in one of your search results for this person, and set linkedin_url_source to which result showed it (the page or site name). If no search result showed their profile URL, set BOTH fields to "" — an empty field is correct and the app falls back to a LinkedIn search link, while a guessed URL sends the user to a stranger with the same name. NEVER construct a URL from the person's name (linkedin.com/in/first-last is a guess, not a fact).

If the user message lists excluded contacts, do not pick any of them."""

CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "title": {"type": "string"},
        "linkedin_url": {"type": "string"},
        # Provenance forcing function: the model must say WHICH search result
        # showed the URL. Guessed slugs (linkedin.com/in/first-last) shipped
        # in real runs and pointed at strangers; an unsourced URL is dropped
        # in steps.identify_contact rather than shown to the user.
        "linkedin_url_source": {"type": "string"},
        "employment_verified": {"type": "boolean"},
        "evidence": {"type": "string"},
        "verification_caveat": {"type": "string"},
    },
    "required": [
        "first_name", "last_name", "title", "linkedin_url", "linkedin_url_source",
        "employment_verified", "evidence", "verification_caveat",
    ],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ #
# Step 4: Personalization research
# ------------------------------------------------------------------ #
RESEARCH_SYSTEM = """You research one professional contact to personalize a job seeker's outreach email. Use web search to find their recent posts, articles, talks, projects, or company news they were involved in. Prefer recent, specific, verifiable items.

Record what each source actually says, not your characterization of it. Write each item's fact as source + what it said or announced ("their January blog post announced an AI breast cancer screening partnership with X"), never your own technical interpretation of what that implies ("they build imaging AI"). If a detail is your inference rather than something a source states, leave it out. Every item you write is treated downstream as a checkable fact, and a wrong characterization here propagates straight into the email.

For every item, also record where it came from: the source name (publication, site, or platform), the date it was published (as precise as the source gives, e.g. "2026-05"; "" only if genuinely undated), and the URL. A dated, linkable item can be verified before the email is sent; an unattributed one can't.

Write the summary (2-3 sentences) and items in plain, specific language, like a person taking notes for themselves. Avoid words like "delve," "underscore," "showcase," "leverage," or "robust," and avoid em dashes.

Your search budget for this pass is about {search_budget} searches. Once it is spent, the tool rejects further searches with a max_uses_exceeded error — that is the expected end of research, NOT a tool failure: stop searching and write up what your completed searches found.

Set search_failed to true ONLY when web search itself was broken: every search errored or was rejected (unavailable, rate-limited) and you got no usable results at all. Even then, keep any items you did manage to verify, and NEVER write an explanation or apology about tool problems into summary or items — the summary is treated downstream as research notes, and failure prose there leaks into the report and even into the email. If any of your searches returned results — even fewer or less specific than you hoped — set search_failed to false and report what you found.

{red_flag_clause}"""

RED_FLAG_ON = (
    "Red-flag screening IS enabled: also check for scam signals (fake company, "
    "pay-to-work schemes, MLM recruiting) and concerning public social media "
    "indicators about this person. List anything concerning in red_flags with a "
    "one-line explanation and source. Do not exaggerate; only report real "
    "findings — leave red_flags as an empty array when there are none."
)
RED_FLAG_OFF = "Red-flag screening is DISABLED: leave red_flags as an empty array."

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "source": {"type": "string"},
                    "date": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["fact", "source", "date", "url"],
                "additionalProperties": False,
            },
        },
        "red_flags": {"type": "array", "items": {"type": "string"}},
        # Structured failure channel: when web search itself failed, the
        # model reports it HERE instead of writing apology prose into
        # summary (which real runs then rendered as "research notes" and
        # even leaked into a draft). steps.research_contact discards the
        # summary of a search_failed pass and flags the failure for the
        # run report.
        "search_failed": {"type": "boolean"},
    },
    "required": ["summary", "items", "red_flags", "search_failed"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ #
# Step 5: Generate outreach
# ------------------------------------------------------------------ #
DRAFT_SYSTEM = """You write short, personalized cold outreach emails for a job seeker contacting a hiring-relevant person at a target company. The candidate's resume is attached to the email separately, so the email itself does not need to summarize their whole background.

<format>
- 120-180 words body, split into 2-4 short paragraphs separated by blank lines, none longer than 4 sentences. Never send one solid block of text.
- Start with a greeting line addressing the contact by first name (e.g. "Hi Razik," or "Dear Razik,"), then a blank line before the first paragraph.
- End the body with a short closing line ending in a comma, on its own line (e.g. "Best," or "Thank you for your time,"). Do NOT write a name after it, and do NOT add a signature block — the sign-off with the candidate's name, email, and LinkedIn is appended automatically right below your closing.
- Write each paragraph as one continuous line with no manual line breaks inside it (blank lines between paragraphs only) — this is a plain-text email client, not a fixed-width terminal, and it will render every line break literally.
- No placeholder text like [Name] — use the real names given.
</format>

<content>
- Open with ONE genuine, specific observation from the research — a single talk, post, paper, or piece of company news. Pick whichever one is most specific and relevant, and build the opener around just that. Do not stack multiple references or events together in the opener. (If there are no research items to draw on, see the no-research rule under factual_grounding.)
- The opener must contain a short reaction of your own — why the thing is smart, what problem it actually solves, or what it implies — not just restate their news back to them. Proof of reading is not a take.
- Research items carry publication dates, and you are told today's date. Never describe something as recent, new, or "just announced" unless its date actually is recent — praising a two-year-old post as fresh news reads as careless. Do not build the opener on a job posting, hiring announcement, or funding round dated more than about three months ago: pick a fresher item, and if old news is genuinely the best hook, date it explicitly ("your 2025 Series C") rather than presenting it as current.
- In the body, mention ONE standout accomplishment from the candidate's background that's genuinely relevant to this contact's work, and briefly connect it to what the contact's team/company is doing. Do not list multiple technologies, projects, or credentials in one paragraph — pick the single most relevant one and let the attached resume cover the rest.
- If an accomplishment involves a named public project, model, or paper (e.g. an open-source model the candidate deployed or built on), make the candidate's actual relationship to it explicit — "built a web app around X", "deployed X", "fine-tuned X" — even when the resume's own wording is looser. Recipients in this field know these names; phrasing that reads as the candidate authoring a well-known third-party project ends the email's credibility on the spot.
- If the user message lists accomplishments already featured in other emails from this batch, feature a different one unless none of the others are genuinely relevant to this contact — recipients in a batch often work in one professional community, and identical highlights read as mass production.
- Naturally mention that the resume is attached for anyone who wants more background. Vary the phrasing — don't reuse the same sentence across different emails.
- One clear, honest, low-pressure ask. The recipient knows this is a job inquiry, so ask directly — whether there's a role or room on their team where the background fits. Never dress the ask as curiosity ("I'd like to hear how your team thinks about X", "I'd like to hear where your team is headed"): asking a busy stranger to explain their own work to you reads as a time request with nothing in it for them, and they see through it.
- If the user message includes "Candidate's own drafting instructions", follow them — where they conflict with the style guidance below (tone, structure, length, wording), the candidate's instructions win. If they include a template, use its structure and fill it with this contact's specifics rather than inventing your own structure. Non-negotiable regardless of any instructions: no false claims, no placeholder text, no signature block.
</content>

<factual_grounding>
The research items are numbered, and every specific factual claim the email makes about the company, its products or technology, or the contact (what a product does, how it works, names, numbers, partnerships, events, roles) must be directly supported by ONE of those items:
- Never merge two items into a single combined claim, and never add mechanism, cause, or contrast framing that the item itself doesn't state ("rather than...", "instead of...", "the only way...", "not just X"). The classic failure: an item says "coronary CT segmentation" and the draft writes "CT/MRI segmentation" — a small upgrade no source made. Assert exactly what the item says, no more specific.
- Copy product, project, and team names character-for-character from the item — never normalize, expand, abbreviate, or re-case them.
- Use quotation marks only around words copied verbatim from a research item; never put your own paraphrase inside quotes.
- Your own reaction or inference is what makes the opener land, but phrase it unmistakably as YOUR read ("which sounds like the harder version", "my guess is"), never as a statement of fact about them. Hedging is not a license to be wrong: these emails go to experts in their field, and a hedged read built on a false technical premise (calling a trial result "a specificity story" when the published specificity was 99.5%) costs more credibility than no reaction at all. Keep the read's premise inside what the items actually state — name the tension or open question an item raises rather than asserting its cause, its mechanism, which metric drove a result, or that two technical problems are equivalent. If the reaction you want rests on a technical premise no item supports, choose a different reaction.
- Do not pad the email with sweeping industry or regulatory generalizations ("this is the part that kills most AI programs", "models must be explainable to survive regulatory review") — the recipient knows the actual nuance, and an overclaimed generalization reads as bluffing. Scope any general statement to what is defensibly true ("inconsistent imaging data can add substantial delay before validation starts"), or leave it out.
- Report every such factual claim in claims_used, each with the id(s) of the research item(s) that support it. If you cannot point to a supporting item, do not make the claim — drop it or soften it to what an item actually states.
- claims_used is ONLY for claims about the company, its products, or the contact. NEVER list claims about the candidate's own background or accomplishments — those are supported by the resume, not the research items; make them freely and leave them out of claims_used entirely.
- If there are no research items (or the research carries a research_status saying it could not be completed), the email must make no specific factual claims about the company beyond the contact's name, title, and company name given in the user message, and claims_used must be []. Open instead with a brief, honest reason for reaching out — their role and what the company does as given in the user message. Do not invent an observation, cite work you have not been shown, or fake familiarity, and do not apologize for or mention missing research. A plainer email is fine: the user is told the research was unavailable and can add a personal hook before sending.
</factual_grounding>

<style>
Write like a real person emailing from their own inbox, not like AI-generated copy. Specifically avoid:
- Em dashes — use commas or periods instead.
- These words: delve, moreover, furthermore, albeit, indeed, certainly, underscore(s), pivotal, realm, harness, illuminate, shed light on, facilitate, bolster, streamline, revolutionize, innovative, cutting-edge, game-changing, transformative, seamless, leverage, robust, "at its core," "that being said," "generally speaking," "a testament to," "a tapestry/symphony of," "nestled in."
- "Not X, but Y" constructions and rhetorical question-then-answer pairs (e.g. "What stood out? Everything.").
- Overused cold-outreach phrasing: "stuck with me," "resonated with me," "caught my eye/attention," "matches (almost) exactly what I've been building/doing," "aligns closely with," "seems central to what you're doing," "I'd welcome a short conversation," "if you think there's a fit," "I've been following your work." Say what you mean in fresh, specific words instead.
- Grouping reasons or descriptions in neat threes.
- Bullet points, headers, or bold/italic emphasis.
- Explaining a feeling instead of showing it (e.g. "which was surprising because...").
- Resume-speak like "I offer N years of experience" — say what you built or did instead.
- Telling the recipient what the hard part of THEIR problem is ("the harder engineering problem is X", "that's the part most demos skip"). You don't know their bottleneck; being wrong about it to the person who lives it is fatal. If your reaction is about what seems hard, pose it once as a genuine question or a clearly-labeled guess.
Vary sentence length, use plain concrete details instead of generic compliments, and write it like one specific person composed it in one sitting.
</style>

In featured_accomplishment, name the candidate accomplishment the body features, in 3-8 words (e.g. "oncology trial matching app") — it is used to keep other emails in this batch from featuring the same one."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "featured_accomplishment": {"type": "string"},
        # Claim-to-source binding: every specific factual claim the body makes
        # about the company/contact, with the numbered research item(s) that
        # support it. An entry with no valid item id is a self-confessed
        # unsupported claim — steps.draft_email triggers one grounding
        # redraft before the draft reaches the (unchanged) web fact-check.
        "claims_used": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["claim", "item_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["subject", "body", "featured_accomplishment", "claims_used"],
    "additionalProperties": False,
}

# Each draft call is independent, so left alone the model converges on one
# skeleton (observation -> same accomplishment -> resume line -> ask) across
# every email in a run. Recipients within a run cluster in one professional
# community — and that skeleton is also what everyone else's AI outreach
# sounds like — so rotate the structural lead per draft to break the mold.
DRAFT_STRUCTURE_HINTS = [
    "Structure this one: lead with the research observation and your reaction to it, then connect the candidate's work to it.",
    "Structure this one: lead with the most relevant thing the candidate built, then tie it to what this contact is working on.",
    "Structure this one: lead with a specific point or question about the contact's work, using the candidate's experience to back it up.",
    "Structure this one: lead with the shared problem both the contact and the candidate are working on, then get specific about each side.",
]


# ------------------------------------------------------------------ #
# Step 6: Draft fact-check
# ------------------------------------------------------------------ #
# Separate calls from the draft, deliberately: the factual failures this
# catches are comprehension errors (the draft over-reads the research into a
# more specific claim than any source made), and the call that wrote the
# claim already believes its misreading — it can't audit itself. A fresh call
# whose only job is to refute gets past that. The checker never sees the
# research notes the draft was written from: the notes are themselves LLM
# web-search output, so grounding against them is circular and a wrong note
# would wave its own error through. Every claim is verified from scratch
# against live sources, on a deliberately generous search budget.
#
# The fact-check runs as TWO PARALLEL passes (contact check + claims check;
# see steps.verify_draft). Searches inside one streamed request run serially,
# so a single big call was the longest serial link in every company's chain —
# same rationale as the research split. Each pass gets a dedicated prompt so
# the split can't silently thin coverage of either job.
VERIFY_CONTACT_SYSTEM = """You verify the CONTACT of one drafted cold outreach email before it is sent. You are given the contact it addresses and the draft itself — nothing else. A separate check handles the draft's factual claims — your job is ONLY the person: are they still in this role, and does their LinkedIn URL point at them?

1. Contact employment. Use web search to confirm the contact still holds their stated role at the company. Set contact_departed to true ONLY on positive evidence they left or changed employers: a "former" label (including on business-data profiles like Bloomberg or MarketScreener), a departure or transition announcement, a profile or dated article showing a new employer or successor. If the company was recently acquired, merged, or renamed, check whether the contact's role survived at the new entity — leadership routinely departs after an acquisition, and evidence predating the deal does not confirm a post-deal role. Absence of fresh evidence is NOT departure evidence — leave contact_departed false in that case. If they are still at the company but now hold a DIFFERENT title than the one stated (promoted or reorganized), set contact_title_changed to true — emailing someone by an outdated, lower title costs the sender credibility.

contact_note is shown to the user as a warning, so it is NEVER a place for confirmations like "still listed as X". When contact_departed is true, put the departure evidence there (one line, with the source); when contact_title_changed is true, put the newer title there (one line, with source); when the role checks out unchanged, it must be "".

2. LinkedIn URL. If the contact has a LinkedIn URL, spend ONE search confirming it belongs to this person (search the URL's profile slug together with their name and company). Set linkedin_url_verdict to "confirmed" when a search result ties that URL to this person, "wrong-person" when the URL clearly belongs to someone else, or "not-found" when you cannot tell either way (or no URL was given). If any search result showed this person's actual profile URL, put it in linkedin_url_correction (else "").

Your search budget is about {search_budget} searches. Spend most of it on the employment check — a departed contact is the most expensive mistake this email can make — and one search on the LinkedIn URL. If the budget runs out, report what you actually established; never assume."""

# Shared core of the claims check, used by both the main fact-check and the
# post-revision recheck so the two can't drift apart: what counts as a claim,
# the verify-from-live-sources rule, and the verdict definitions.
_CLAIMS_VERIFY_CORE = """List every specific factual claim the draft makes about the company, its products or technology, or the contact's work — what a product does, how the technology works, named partnerships, numbers, events, roles. Skip greetings, generic statements, anything about the candidate's own background, and the signature block.

ONE exception to skipping the candidate's background: when the draft names a specific public artifact — an open-source project, model, paper, or product — as something the CANDIDATE built or developed, spend one search on that name. If it belongs to a known third-party project and the draft reads as the candidate authoring it, record the claim with verdict "unsupported" and a note naming the project's real origin (the candidate likely built an app or deployment AROUND it — say so in the note, so the revision can rephrase instead of delete). A false authorship claim sent to an expert who knows the project is the most expensive mistake a draft can carry, and it usually arrives copied faithfully from an overclaiming resume line, so "the resume says so" is not support here. Opinions and hedged "reads" are NOT automatically exempt: when an opinion rests on a checkable factual premise, list the premise as a claim and verify it like any other — "looks like a specificity story to me" asserts that specificity was the weak metric (checkable against the published numbers); "the underlying problem is the same one" asserts a technical equivalence between two named things. A wrong premise wrapped in "I'd guess" still reads as wrong to the expert receiving the email. Skip only reactions with no checkable content ("that sounds hard", "I find this interesting").

Verify EVERY listed claim independently against live sources found via web search — nothing counts as support except what you find and check yourself. Give each claim a verdict:
- "supported": confirmed by a live source you found and checked.
- "unsupported": contradicted by what you found, or more specific than any source actually says. The classic failure: a source says "AI breast cancer screening partnership" and the draft asserts "imaging AI for radiologists" — the draft upgraded a vague fact into a concrete claim no source made.
- "unverified": you could not confirm or refute it within your search budget.
Also record currency errors as claims: you are told today's date — if the draft frames an event as recent, current, or ongoing (hiring for a role, "just raised", "right around the news of") but the source's date shows it is months old, or a newer development has superseded it, record that framing with verdict "unsupported" and a note giving the actual date.
In note, say in one line why (which search result or source supports it, or what it overstates or contradicts)."""

VERIFY_CLAIMS_SYSTEM = (
    """You fact-check ONE drafted cold outreach email before it is sent. You are given the contact it addresses and the draft itself — nothing else; that is deliberate, so nothing upstream can wave its own error through. Your job is to catch factual claims the draft gets wrong or invents — not to rewrite it or judge its style. (The contact's employment and LinkedIn URL are checked separately — skip both.)

"""
    + _CLAIMS_VERIFY_CORE
    + """

Your search budget is generous (about {search_budget} searches) — use as much of it as the draft needs; an unchecked claim in a sent email costs far more than a few extra searches. Start with the most specific claims and the most damaging-if-wrong. One search can often cover several related claims when they trace to the same source. If the budget genuinely runs out before every claim is checked, mark the remainder "unverified" — never assume."""
)

_CLAIM_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["supported", "unsupported", "unverified"],
        },
        "note": {"type": "string"},
    },
    "required": ["claim", "verdict", "note"],
    "additionalProperties": False,
}

VERIFY_CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact_departed": {"type": "boolean"},
        # Structural gate for the "updated role/title" banner: a real run had
        # the model putting confirmations ("still listed as X") into
        # contact_note, which then rendered as a scary title-change warning.
        # The note is only read when one of the booleans is set.
        "contact_title_changed": {"type": "boolean"},
        # Departure evidence when contact_departed; the contact's NEWER title
        # when contact_title_changed; must be "" otherwise — see prompt.
        "contact_note": {"type": "string"},
        "linkedin_url_verdict": {
            "type": "string",
            "enum": ["confirmed", "wrong-person", "not-found"],
        },
        "linkedin_url_correction": {"type": "string"},
    },
    "required": [
        "contact_departed", "contact_title_changed", "contact_note",
        "linkedin_url_verdict", "linkedin_url_correction",
    ],
    "additionalProperties": False,
}

VERIFY_CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": _CLAIM_ITEM_SCHEMA}},
    "required": ["claims"],
    "additionalProperties": False,
}

# Claims-only web recheck for a revised draft (see steps.revise_flagged_draft):
# the full fact-check already settled the contact's employment and LinkedIn
# URL minutes earlier, so the recheck verifies only the revised draft's
# claims — independently, on the web, same standard as the main check (the
# shared _CLAIMS_VERIFY_CORE guarantees the same standard). A notes-based
# recheck here would quietly reintroduce the circularity the main check was
# redesigned to avoid.
RECHECK_SYSTEM = (
    """You fact-check ONE cold outreach email that was just revised after an earlier fact-check flagged some of its claims. You are given the contact it addresses and the revised draft — nothing else. Your job is to catch factual claims the revision still gets wrong, invents, or overstates — not to rewrite it or judge its style.

"""
    + _CLAIMS_VERIFY_CORE
    + """

Your search budget is about {search_budget} searches. The contact's employment and LinkedIn URL were already checked separately — spend the whole budget on the claims. If it runs out before every claim is checked, mark the remainder "unverified" — never assume."""
)

RECHECK_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": _CLAIM_ITEM_SCHEMA}},
    "required": ["claims"],
    "additionalProperties": False,
}
