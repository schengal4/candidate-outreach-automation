"""Draft fact-check (step 6): claims the independent web check can't confirm
are flagged in the run report, the checker never sees the research notes,
a departed contact drops the company without choosing a replacement (and never
reaches the Sent List), and a failed check flags the draft instead of
discarding it. Open mode."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import config, prompts
from app.llm import LLMError
from app.models import Candidate, CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.run_manager import manager
from app.steps import DraftResult, ResearchResult, VerifyResult

RUNS = manager.runs

# 1. Schema shape across the two parallel passes: departure flag + LinkedIn
#    URL check required on the contact pass, verdicts enforced server-side on
#    the claims pass — and the split must not shrink the combined budget.
assert set(prompts.VERIFY_CONTACT_SCHEMA["required"]) == {
    "contact_departed", "contact_title_changed", "contact_note",
    "linkedin_url_verdict", "linkedin_url_correction",
}
assert set(prompts.VERIFY_CLAIMS_SCHEMA["required"]) == {"claims"}
claim_item = prompts.VERIFY_CLAIMS_SCHEMA["properties"]["claims"]["items"]
assert claim_item["properties"]["verdict"]["enum"] == ["supported", "unsupported", "unverified"]
assert prompts.VERIFY_CONTACT_SCHEMA["properties"]["linkedin_url_verdict"]["enum"] == [
    "confirmed", "wrong-person", "not-found",
]
# The split exists for wall-clock (parallel passes), NOT to cut verification
# depth — the combined budget must stay at the old single-call 12.
assert (
    config.settings.VERIFY_CONTACT_WEB_SEARCH_MAX_USES
    + config.settings.VERIFY_CLAIMS_WEB_SEARCH_MAX_USES
    == 12
)
print("PASS: verify pass schemas enforce verdicts, the departure flag, and the URL check")

# 2. New CompanyState fields round-trip and default empty on old run files
comp = CompanyState(name="A", domain="a.com")
comp.draft_flagged_claims = ["unsupported: x"]
comp.draft_verify_error = "boom"
rt = CompanyState.from_dict(comp.to_dict())
assert rt.draft_flagged_claims == ["unsupported: x"] and rt.draft_verify_error == "boom"
old = CompanyState.from_dict({"name": "A", "domain": "a.com"})
assert old.draft_flagged_claims == [] and old.draft_verify_error == ""
print("PASS: fact-check fields round-trip and default empty")


CONTACT = Contact(
    first_name="Jane", last_name="Doe", title="VP Engineering", employment_verified=True,
)
RESEARCH = ResearchResult(
    summary="s",
    items=[{"fact": "their blog post described automating medical coding",
            "source": "AKASA blog", "date": "2026-05", "url": ""}],
)


def make_company() -> CompanyState:
    c = CompanyState(name="AKASA", domain="akasa.com")
    c.contact_used = CONTACT
    c.research_summary = RESEARCH.summary
    c.research_items = list(RESEARCH.items)
    c.draft_subject = "subj"
    c.draft_body = "Hi Jane,\n\nbody\n\nBest,"
    return c


# 3. verify_draft runs contact and claims as two parallel passes, each on its
#    own budget; non-supported claims are flagged, no drop; neither checker
#    ever sees the research notes (independent verification)
CONTACT_OK = {
    "contact_departed": False, "contact_title_changed": False, "contact_note": "",
    "linkedin_url_verdict": "not-found", "linkedin_url_correction": "",
}


def make_fake_verify_ask(contact_resp, claims_resp, captured=None):
    """A fake ask_json that answers both verify passes, dispatching on the
    schema the way the real call is routed. An Exception value is raised."""
    async def fake(system, user, **kw):
        if kw.get("schema") is prompts.VERIFY_CLAIMS_SCHEMA:
            resp, key = claims_resp, "claims"
        else:
            assert kw.get("schema") is prompts.VERIFY_CONTACT_SCHEMA
            resp, key = contact_resp, "contact"
        if captured is not None:
            captured[key] = {"user": user, **kw}
        if isinstance(resp, Exception):
            raise resp
        return resp
    return fake


captured = {}
real_ask_json = steps.ask_json
steps.ask_json = make_fake_verify_ask(
    CONTACT_OK,
    {"claims": [
        {"claim": "uses long-context models for coding", "verdict": "unsupported",
         "note": "their blog describes rules plus review, no model architecture named"},
        {"claim": "raised a Series C", "verdict": "unverified", "note": ""},
        {"claim": "works in revenue cycle automation", "verdict": "supported",
         "note": "research item 1"},
    ]},
    captured,
)
try:
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", "subj", "Hi Jane,\n\nbody\n\nBest,"
    ))
    assert verdict.departed_evidence == ""
    assert set(captured) == {"contact", "claims"}, "both passes must run"
    assert captured["contact"]["max_uses"] == config.settings.VERIFY_CONTACT_WEB_SEARCH_MAX_USES
    assert captured["claims"]["max_uses"] == config.settings.VERIFY_CLAIMS_WEB_SEARCH_MAX_USES
    for pass_kw in captured.values():
        assert pass_kw["timeout_seconds"] == config.settings.VERIFY_CALL_TIMEOUT_SECONDS
        assert pass_kw["web_search"] is True
        assert "automating medical coding" not in pass_kw["user"], \
            "research notes must never reach the fact-checker"
        assert "Research notes" not in pass_kw["user"]
    assert len(verdict.flagged_claims) == 2
    assert verdict.flagged_claims[0].startswith("unsupported: uses long-context")
    assert verdict.flagged_claims[1] == "unverified: raised a Series C"
    assert verdict.error == ""
    print("PASS: parallel passes flag unsupported/unverified claims; budgets split; notes withheld")

    # 4. Departure evidence is returned for the caller to drop on
    steps.ask_json = make_fake_verify_ask(
        {**CONTACT_OK, "contact_departed": True,
         "contact_note": "LinkedIn shows a new employer since May"},
        {"claims": []},
    )
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", "subj", "body"
    ))
    assert "new employer" in verdict.departed_evidence
    print("PASS: departed contact returns the evidence line")

    # 5. Both checks failing keeps the draft and flags it as unchecked
    steps.ask_json = make_fake_verify_ask(LLMError("kaboom"), LLMError("kaboom"))
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", "subj", "body"
    ))
    assert verdict.departed_evidence == "" and "kaboom" in verdict.error
    print("PASS: failed fact-check calls keep the draft, flagged as unchecked")

    # 5a. ONE pass failing still applies the other pass's findings AND flags
    #     the draft — a departed-contact verdict must survive a failed claims
    #     check, and a failed contact check must not silently pass as clean.
    steps.ask_json = make_fake_verify_ask(
        {**CONTACT_OK, "contact_departed": True, "contact_note": "left in May"},
        LLMError("claims kaboom"),
    )
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", "subj", "body"
    ))
    assert "left in May" in verdict.departed_evidence
    assert "claims check failed" in verdict.error
    steps.ask_json = make_fake_verify_ask(
        LLMError("contact kaboom"),
        {"claims": [{"claim": "x", "verdict": "unverified", "note": ""}]},
    )
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", "subj", "body"
    ))
    assert verdict.flagged_claims == ["unverified: x"]
    assert "contact check failed" in verdict.error and verdict.departed_evidence == ""
    print("PASS: a single failed pass keeps the other pass's findings and sets .error")

    # 5b. contact_note only counts under its booleans: a title change (with
    #     contact_title_changed) becomes contact_update, and the URL verdict
    #     fields pass through
    steps.ask_json = make_fake_verify_ask(
        {"contact_departed": False,
         "contact_title_changed": True,
         "contact_note": "now Director, Enterprise Engineering (LinkedIn headline)",
         "linkedin_url_verdict": "wrong-person",
         "linkedin_url_correction": ""},
        {"claims": []},
    )
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "Datavant", "datavant.com", "subj", "body"
    ))
    assert verdict.departed_evidence == ""
    assert "Director, Enterprise Engineering" in verdict.contact_update
    assert verdict.linkedin_verdict == "wrong-person"
    print("PASS: a title change comes back as contact_update, not a departure")

    # 5c. A confirmation stuffed into contact_note WITHOUT the boolean is
    #     discarded — a real run rendered "still listed as VP of Engineering"
    #     as a scary "updated role/title" banner.
    steps.ask_json = make_fake_verify_ask(
        {"contact_departed": False,
         "contact_title_changed": False,
         "contact_note": "No evidence of departure; still listed as VP of Engineering",
         "linkedin_url_verdict": "confirmed",
         "linkedin_url_correction": ""},
        {"claims": []},
    )
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "Rad AI", "rad.ai", "subj", "body"
    ))
    assert verdict.contact_update == "" and verdict.departed_evidence == ""
    print("PASS: a confirmation note without the title-changed flag is discarded")
finally:
    steps.ask_json = real_ask_json

# 6. _run_company: departed contact -> dropped with the reason, exactly ONE
#    contact call (no replacement chosen), nothing added to the Sent List
contact_calls = []
sent_calls = []

config.settings.CONTACT_LEADS_LIMIT = 0  # never hit Hunter's live API from tests

async def fake_identify(candidate, company_name, domain, excluded, **kw):
    contact_calls.append(domain)
    return Contact(first_name="Jane", last_name="Doe", title="CTO", employment_verified=True), None

async def fake_lookup(domain, contact, blocked):
    return "jane@x.com", 90, ""

async def fake_research(contact, company_name, domain, red_flags_enabled, on_progress=None):
    return ResearchResult()

async def fake_draft(candidate, contact, company_name, research,
                     used_accomplishments=None, on_progress=None):
    return DraftResult(subject="s", body="b")

async def fake_verify(contact, company_name, domain, subject, body, on_progress=None):
    return VerifyResult(departed_evidence="LinkedIn shows they left in May")

real = (steps.identify_contact, steps.lookup_email, steps.research_contact,
        steps.draft_email, steps.verify_draft, pipeline.sent_list.add_entry)
steps.identify_contact = fake_identify
steps.lookup_email = fake_lookup
steps.research_contact = fake_research
steps.draft_email = fake_draft
steps.verify_draft = fake_verify
pipeline.sent_list.add_entry = lambda *a, **k: sent_calls.append(a)
try:
    cand = Candidate(id="testverify", name="T", email="", current_employer="E", resume_text="r")
    comp = CompanyState(name="X", domain="x.com")
    asyncio.run(pipeline._run_company(cand, comp, [], set(), []))
    assert comp.status == CompanyStatus.DROPPED
    assert "contact no longer at company" in comp.drop_reason
    assert "left in May" in comp.drop_reason
    assert contact_calls == ["x.com"], contact_calls  # no replacement contact chosen
    assert sent_calls == []  # never recorded as contacted
    print("PASS: departed contact drops the company; no replacement; not on the Sent List")
finally:
    (steps.identify_contact, steps.lookup_email, steps.research_contact,
     steps.draft_email, steps.verify_draft, pipeline.sent_list.add_entry) = real

# 7. Run report renders the claim flag and the not-fact-checked flag
from fastapi.testclient import TestClient
from app.main import app as fastapi_app

run = RunState(id="testverifyrun1", candidate_id="516e7c4751", phase=RunPhase.DONE)
flagged = make_company()
flagged.status = CompanyStatus.DONE
flagged.email = "jane@akasa.com"
flagged.draft_flagged_claims = [
    "unsupported: uses long-context models (their blog describes rules plus review)",
    "unverified: partners with three health systems (could not confirm either way)",
]
unchecked = make_company()
unchecked.name, unchecked.domain = "OtherCo", "other.com"
unchecked.status = CompanyStatus.DONE
unchecked.email = "jane@other.com"
unchecked.draft_verify_error = "LLM call timed out after 5 minutes."
run.companies = [flagged, unchecked]
RUNS[run.id] = run
try:
    html = TestClient(fastapi_app).get(f"/runs/{run.id}/panel").text
    # Tiered flags: 'unsupported' (contradicted) renders in the red priority
    # box, 'unverified' in the routine amber box, and the priority digest at
    # the top names the companies that need a look.
    assert "These claims failed the fact-check" in html
    assert "uses long-context models" in html
    assert "Check these claims before sending" in html
    assert "partners with three health systems" in html
    assert "This draft was not fact-checked" in html
    assert "timed out after 5 minutes" in html
    assert "flag priority" in html
    assert "drafts need attention before sending" in html
    print("PASS: run report tiers the flags and leads with the priority digest")
finally:
    RUNS.pop(run.id, None)

# 8. Research prompt records sources, not characterizations (the AKASA class
#    of error is handled at its source)
assert "checkable fact" in prompts.RESEARCH_SYSTEM
print("PASS: research prompt tells the model to record sources, not interpretations")

# 9. Opinion-wrapped factual premises are covered on BOTH sides: the draft
#    prompt forbids reads built on unsupported technical premises, and both
#    claim checkers (main fact-check + revision recheck) are told hedged
#    opinions with checkable premises are claims — an external fact-check
#    caught a draft calling a 99.55%-specificity trial result "a specificity
#    story", hedged as opinion, which both the draft's grounding rules and
#    the checker's "skip opinions" rule had exempted.
assert "Hedging is not a license to be wrong" in prompts.DRAFT_SYSTEM
assert "sweeping industry or regulatory generalizations" in prompts.DRAFT_SYSTEM
assert "NOT automatically exempt" in prompts.VERIFY_CLAIMS_SYSTEM
assert "checkable factual premise" in prompts.VERIFY_CLAIMS_SYSTEM
assert "NOT automatically exempt" in prompts.RECHECK_SYSTEM
assert "checkable factual premise" in prompts.RECHECK_SYSTEM
print("PASS: opinion-premise rules present in the draft prompt and both claim checkers")

# 10. Third-party attribution is covered on BOTH sides too: the draft prompt
#     requires stating the candidate's actual relationship to named public
#     projects, and both claim checkers get the one exception to the
#     skip-candidate-background rule — a real run mailed "SAM-Med2D" (a
#     published Shanghai AI Lab model, phrased as the candidate's own work,
#     copied faithfully from the resume) to Microsoft's health CSO, and the
#     resume-trust rule waved it through.
assert "candidate's actual relationship" in prompts.DRAFT_SYSTEM
assert "ONE exception to skipping the candidate's background" in prompts.VERIFY_CLAIMS_SYSTEM
assert "ONE exception to skipping the candidate's background" in prompts.RECHECK_SYSTEM
assert "overclaiming resume" in prompts.VERIFY_CLAIMS_SYSTEM
print("PASS: third-party attribution rules present in the draft prompt and both checkers")
