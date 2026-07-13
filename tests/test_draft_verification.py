"""Draft fact-check (step 6): ungrounded claims are flagged in the run report,
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

# 1. Schema shape: departure flag + LinkedIn URL check required, verdicts
#    enforced server-side
assert set(prompts.VERIFY_SCHEMA["required"]) == {
    "contact_departed", "contact_title_changed", "contact_note",
    "linkedin_url_verdict", "linkedin_url_correction", "claims",
}
claim_item = prompts.VERIFY_SCHEMA["properties"]["claims"]["items"]
assert claim_item["properties"]["verdict"]["enum"] == ["supported", "unsupported", "unverified"]
assert prompts.VERIFY_SCHEMA["properties"]["linkedin_url_verdict"]["enum"] == [
    "confirmed", "wrong-person", "not-found",
]
print("PASS: verify schema enforces verdicts, the departure flag, and the URL check")

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


# 3. verify_draft flags non-supported claims, uses the verify budget, no drop
captured = {}

async def fake_ask_json(system, user, **kw):
    captured.update(kw)
    return {
        "contact_departed": False,
        "contact_note": "",
        "claims": [
            {"claim": "uses long-context models for coding", "verdict": "unsupported",
             "note": "their blog describes rules plus review, no model architecture named"},
            {"claim": "raised a Series C", "verdict": "unverified", "note": ""},
            {"claim": "works in revenue cycle automation", "verdict": "supported",
             "note": "research item 1"},
        ],
    }

real_ask_json = steps.ask_json
steps.ask_json = fake_ask_json
try:
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", RESEARCH, "subj", "Hi Jane,\n\nbody\n\nBest,"
    ))
    assert verdict.departed_evidence == ""
    assert captured["max_uses"] == config.settings.VERIFY_WEB_SEARCH_MAX_USES
    assert captured["timeout_seconds"] == config.settings.VERIFY_CALL_TIMEOUT_SECONDS
    assert len(verdict.flagged_claims) == 2
    assert verdict.flagged_claims[0].startswith("unsupported: uses long-context")
    assert verdict.flagged_claims[1] == "unverified: raised a Series C"
    assert verdict.error == ""
    print("PASS: unsupported/unverified claims flagged; supported ones aren't; verify budget used")

    # 4. Departure evidence is returned for the caller to drop on
    async def fake_departed(system, user, **kw):
        return {"contact_departed": True,
                "contact_note": "LinkedIn shows a new employer since May", "claims": []}
    steps.ask_json = fake_departed
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", RESEARCH, "subj", "body"
    ))
    assert "new employer" in verdict.departed_evidence
    print("PASS: departed contact returns the evidence line")

    # 5. A failed check keeps the draft and flags it as unchecked
    async def boom(system, user, **kw):
        raise LLMError("kaboom")
    steps.ask_json = boom
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "AKASA", "akasa.com", RESEARCH, "subj", "body"
    ))
    assert verdict.departed_evidence == "" and "kaboom" in verdict.error
    print("PASS: failed fact-check call keeps the draft, flagged as unchecked")

    # 5b. contact_note only counts under its booleans: a title change (with
    #     contact_title_changed) becomes contact_update, and the URL verdict
    #     fields pass through
    async def fake_promoted(system, user, **kw):
        return {"contact_departed": False,
                "contact_title_changed": True,
                "contact_note": "now Director, Enterprise Engineering (LinkedIn headline)",
                "linkedin_url_verdict": "wrong-person",
                "linkedin_url_correction": "",
                "claims": []}
    steps.ask_json = fake_promoted
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "Datavant", "datavant.com", RESEARCH, "subj", "body"
    ))
    assert verdict.departed_evidence == ""
    assert "Director, Enterprise Engineering" in verdict.contact_update
    assert verdict.linkedin_verdict == "wrong-person"
    print("PASS: a title change comes back as contact_update, not a departure")

    # 5c. A confirmation stuffed into contact_note WITHOUT the boolean is
    #     discarded — a real run rendered "still listed as VP of Engineering"
    #     as a scary "updated role/title" banner.
    async def fake_confirmation(system, user, **kw):
        return {"contact_departed": False,
                "contact_title_changed": False,
                "contact_note": "No evidence of departure; still listed as VP of Engineering",
                "linkedin_url_verdict": "confirmed",
                "linkedin_url_correction": "",
                "claims": []}
    steps.ask_json = fake_confirmation
    verdict = asyncio.run(steps.verify_draft(
        CONTACT, "Rad AI", "rad.ai", RESEARCH, "subj", "body"
    ))
    assert verdict.contact_update == "" and verdict.departed_evidence == ""
    print("PASS: a confirmation note without the title-changed flag is discarded")
finally:
    steps.ask_json = real_ask_json

# 6. _run_company: departed contact -> dropped with the reason, exactly ONE
#    contact call (no replacement chosen), nothing added to the Sent List
contact_calls = []
sent_calls = []

async def fake_identify(candidate, company_name, domain, excluded, **kw):
    contact_calls.append(domain)
    return Contact(first_name="Jane", last_name="Doe", title="CTO", employment_verified=True)

async def fake_lookup(domain, contact, blocked):
    return "jane@x.com", 90, ""

async def fake_research(contact, company_name, domain, red_flags_enabled, on_progress=None):
    return ResearchResult()

async def fake_draft(candidate, contact, company_name, research,
                     used_accomplishments=None, on_progress=None):
    return DraftResult(subject="s", body="b")

async def fake_verify(contact, company_name, domain, research, subject, body, on_progress=None):
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
    "unsupported: uses long-context models (their blog describes rules plus review)"
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
    assert "Check these claims before sending" in html
    assert "uses long-context models" in html
    assert "This draft was not fact-checked" in html
    assert "timed out after 5 minutes" in html
    print("PASS: run report renders the claim flag and the not-fact-checked flag")
finally:
    RUNS.pop(run.id, None)

# 8. Research prompt records sources, not characterizations (the AKASA class
#    of error is handled at its source)
assert "checkable fact" in prompts.RESEARCH_SYSTEM
print("PASS: research prompt tells the model to record sources, not interpretations")
