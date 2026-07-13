"""Claim revision (step 6 follow-up): a draft with flagged claims gets ONE
no-web revision, rechecked by a grounding-only call and adopted only when
cleaner; failures keep the original draft and its flags. Also covers the
structured-research rendering in the run report. Open mode."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import prompts
from app.draft_hygiene import SIGNATURE_SEP, email_signature
from app.llm import LLMError
from app.models import Candidate, CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.run_manager import manager
from app.steps import DraftResult, ResearchResult, VerifyResult

RUNS = manager.runs

CAND = Candidate(
    id="testrev", name="Pat Doe", email="pat@x.com", current_employer="E",
    resume_text="r", linkedin_url="https://linkedin.com/in/pat",
)
CONTACT = Contact(first_name="Jane", last_name="Doe", title="VP Engineering")
RESEARCH = ResearchResult(
    summary="s",
    items=[{
        "fact": "their blog post described automating medical coding",
        "source": "AKASA blog", "date": "2026-05", "url": "https://akasa.com/blog",
    }],
)
OLD_SUBJECT = "old subject"
OLD_BODY = "Hi Jane,\n\nold body with a bold claim.\n\nBest,"
FLAGS = ["unsupported: uses long-context models (no source names an architecture)"]


def revise():
    return asyncio.run(steps.revise_flagged_draft(
        CAND, CONTACT, "AKASA", RESEARCH, OLD_SUBJECT, OLD_BODY, list(FLAGS),
    ))


# 1. Clean revision is returned: new subject/body, remaining flags empty;
#    the revise call runs without web and shares the draft cache prefix
calls = []

async def fake_ask_json_clean(system, user, **kw):
    calls.append({"system": system, "user": user, **kw})
    if kw.get("label", "").startswith("revise:"):
        return {"subject": "new subject",
                "body": "Hi Jane,\n\nsofter body.\n\nBest,",
                "featured_accomplishment": "same"}
    return {"claims": [{"claim": "automates medical coding", "verdict": "supported",
                        "note": "research item 1"}]}

real_ask_json = steps.ask_json
steps.ask_json = fake_ask_json_clean
try:
    result = revise()
    assert result is not None
    assert result.subject == "new subject"
    assert result.body == "Hi Jane,\n\nsofter body.\n\nBest,"
    assert result.flagged_claims == []
    print("PASS: clean revision returned with the remaining flags cleared")

    revise_call, recheck_call = calls
    assert revise_call["web_search"] is False and recheck_call["web_search"] is False
    assert revise_call["schema"] is prompts.DRAFT_SCHEMA
    assert recheck_call["schema"] is prompts.GROUNDING_SCHEMA
    assert "unsupported: uses long-context models" in revise_call["user"]
    assert SIGNATURE_SEP not in revise_call["user"]  # sign-off never shown to the model
    assert revise_call["cache_prefix"] == steps._draft_cache_prefix(CAND)
    print("PASS: revise + recheck both run without web; revision shares the draft cache prefix")

    # 2. Revision that rechecks dirtier than the original is rejected
    async def fake_ask_json_dirtier(system, user, **kw):
        if kw.get("label", "").startswith("revise:"):
            return {"subject": "worse subject", "body": "Hi Jane,\n\nworse.\n\nBest,",
                    "featured_accomplishment": "same"}
        return {"claims": [
            {"claim": "a", "verdict": "unsupported", "note": ""},
            {"claim": "b", "verdict": "unverified", "note": ""},
        ]}
    steps.ask_json = fake_ask_json_dirtier
    assert revise() is None
    print("PASS: dirtier revision rejected — caller keeps the original draft and flags")

    # 3. A failed revision call keeps the original draft and its flags
    async def boom(system, user, **kw):
        raise LLMError("kaboom")
    steps.ask_json = boom
    assert revise() is None
    print("PASS: failed revision call keeps the original draft and its flags")
finally:
    steps.ask_json = real_ask_json

# 4. _run_company wiring: flags from the fact-check trigger exactly one
#    revision (applied with the signature re-appended); a clean fact-check
#    skips it; the company still completes
revise_calls = []

async def fake_identify(candidate, company_name, domain, excluded, **kw):
    return Contact(first_name="Jane", last_name="Doe", title="CTO", employment_verified=True)

async def fake_lookup(domain, contact, blocked):
    return "jane@x.com", 90, ""

async def fake_research(contact, company_name, domain, red_flags_enabled, on_progress=None):
    return ResearchResult()

async def fake_draft(candidate, contact, company_name, research,
                     used_accomplishments=None, on_progress=None):
    return DraftResult(subject="s", body="b")

async def fake_verify_flagging(contact, company_name, domain, research, subject, body,
                               on_progress=None):
    return VerifyResult(flagged_claims=["unsupported: x"])

async def fake_verify_clean(contact, company_name, domain, research, subject, body,
                            on_progress=None):
    return VerifyResult()

async def fake_revise(candidate, contact, company_name, research, subject,
                      body_without_signature, flagged_claims, on_progress=None):
    revise_calls.append(body_without_signature)
    return DraftResult(subject="revised s", body="revised b", flagged_claims=[])

real = (steps.identify_contact, steps.lookup_email, steps.research_contact,
        steps.draft_email, steps.verify_draft, steps.revise_flagged_draft,
        pipeline.sent_list.add_entry)
steps.identify_contact = fake_identify
steps.lookup_email = fake_lookup
steps.research_contact = fake_research
steps.draft_email = fake_draft
steps.verify_draft = fake_verify_flagging
steps.revise_flagged_draft = fake_revise
pipeline.sent_list.add_entry = lambda *a, **k: None
try:
    comp = CompanyState(name="X", domain="x.com")
    asyncio.run(pipeline._run_company(CAND, comp, [], set(), []))
    assert comp.status == CompanyStatus.DONE and len(revise_calls) == 1
    # The orchestrator strips the signature before the revise call and
    # re-appends it to the adopted revision.
    assert SIGNATURE_SEP not in revise_calls[0]
    assert comp.draft_subject == "revised s"
    assert comp.draft_body == "revised b" + email_signature(CAND)
    assert comp.draft_flagged_claims == []
    steps.verify_draft = fake_verify_clean
    comp2 = CompanyState(name="Y", domain="y.com")
    asyncio.run(pipeline._run_company(CAND, comp2, [], set(), []))
    assert comp2.status == CompanyStatus.DONE and len(revise_calls) == 1
    print("PASS: flagged fact-check triggers one applied revision; clean fact-check skips it")
finally:
    (steps.identify_contact, steps.lookup_email, steps.research_contact,
     steps.draft_email, steps.verify_draft, steps.revise_flagged_draft,
     pipeline.sent_list.add_entry) = real

# 5. Run report renders structured research items (and still renders old
#    plain-string items from pre-schema run files)
from fastapi.testclient import TestClient
from app.main import app as fastapi_app

run = RunState(id="testrevrun1", candidate_id="516e7c4751", phase=RunPhase.DONE)
done = CompanyState(name="AKASA", domain="akasa.com")
done.contact_used = CONTACT
done.status = CompanyStatus.DONE
done.email = "jane@akasa.com"
done.draft_subject = OLD_SUBJECT
done.draft_body = OLD_BODY + email_signature(CAND)
done.research_items = [
    {"fact": "announced a coding automation launch", "source": "AKASA blog",
     "date": "2026-05", "url": "https://akasa.com/blog/launch"},
    "an old plain-string item",
]
run.companies = [done]
RUNS[run.id] = run
try:
    html = TestClient(fastapi_app).get(f"/runs/{run.id}/panel").text
    assert "announced a coding automation launch" in html
    assert "AKASA blog" in html and "2026-05" in html
    assert 'href="https://akasa.com/blog/launch"' in html
    assert "an old plain-string item" in html
    print("PASS: run report renders structured items with provenance and old string items")
finally:
    RUNS.pop(run.id, None)
