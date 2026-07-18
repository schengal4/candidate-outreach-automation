"""Claim revision (step 6 follow-up): a draft with flagged claims gets a
no-web revision, rechecked by an independent claims-only web call and adopted
only when no worse on BOTH total flags and 'unsupported' flags; failures keep
the original draft and its flags. The orchestrator escalates to exactly ONE
removal-mode round when 'unsupported' flags survive round 1 OR when round 1
produced no adoptable revision at all (rechecked worse / call failed);
'unverified' leftovers from an ADOPTED round 1 ship with the warning instead.
A revision that reintroduces banned wording gets one restyle pass before its
recheck. Also covers the structured-research rendering in the run report.
Open mode."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import config, prompts
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
#    the revise call runs without web and shares the draft cache prefix,
#    while the recheck runs WITH web (independent verification) and never
#    sees the research notes
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
    assert revise_call["web_search"] is False and recheck_call["web_search"] is True
    assert revise_call["schema"] is prompts.DRAFT_SCHEMA
    assert recheck_call["schema"] is prompts.RECHECK_SCHEMA
    assert recheck_call["max_uses"] == config.settings.RECHECK_WEB_SEARCH_MAX_USES
    assert "automating medical coding" not in recheck_call["user"], \
        "research notes must never reach the recheck"
    assert "unsupported: uses long-context models" in revise_call["user"]
    assert SIGNATURE_SEP not in revise_call["user"]  # sign-off never shown to the model
    assert revise_call["cache_prefix"] == steps._draft_cache_prefix(CAND)
    print("PASS: revise runs without web; recheck web-verifies independently of the notes")

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

    # 2b. Severity-aware rejection: same TOTAL flag count, but the revision
    #     traded an 'unverified' flag for an 'unsupported' (contradicted) one
    async def fake_ask_json_severity(system, user, **kw):
        if kw.get("label", "").startswith("revise:"):
            return {"subject": "s2", "body": "Hi Jane,\n\nb2.\n\nBest,",
                    "featured_accomplishment": "same"}
        return {"claims": [{"claim": "z", "verdict": "unsupported", "note": ""}]}
    steps.ask_json = fake_ask_json_severity
    result = asyncio.run(steps.revise_flagged_draft(
        CAND, CONTACT, "AKASA", RESEARCH, OLD_SUBJECT, OLD_BODY, ["unverified: y"],
    ))
    assert result is None
    print("PASS: equal-count trade of unverified for unsupported is rejected")

    # 2c. Removal mode: replace-or-remove on a small web budget (round 1
    #     stays no-web — asserted in case 1 above); the recheck still gates
    calls.clear()
    steps.ask_json = fake_ask_json_clean
    result = asyncio.run(steps.revise_flagged_draft(
        CAND, CONTACT, "AKASA", RESEARCH, OLD_SUBJECT, OLD_BODY, list(FLAGS),
        remove_entirely=True,
    ))
    assert result is not None
    removal_call = calls[0]
    assert removal_call["web_search"] is True
    assert removal_call["max_uses"] == config.settings.REVISE_WEB_SEARCH_MAX_USES
    assert f"about {config.settings.REVISE_WEB_SEARCH_MAX_USES} searches" \
        in removal_call["user"], "the prompt must announce the search budget"
    assert "REPLACE" in removal_call["user"] and "REMOVE" in removal_call["user"]
    assert "soften each flagged claim" not in removal_call["user"]
    assert calls[1]["web_search"] is True, "recheck still runs with web"
    print("PASS: removal mode replaces-or-removes with a small announced web budget")

    # 2d. A revision that reintroduces banned wording gets ONE restyle pass,
    #     and the RECHECK runs on the restyled text (that's what ships)
    labels = []

    async def fake_ask_json_restyle(system, user, **kw):
        label = kw.get("label", "")
        labels.append(label)
        if label.startswith("revise:"):
            return {"subject": "s", "body": "Hi Jane,\n\nI think there's a fit.\n\nBest,",
                    "featured_accomplishment": "same"}
        if label.startswith("restyle:"):
            assert "there's a fit" in user, "restyle must name the banned phrasing"
            return {"subject": "s", "body": "Hi Jane,\n\nI think I could help.\n\nBest,",
                    "featured_accomplishment": "same"}
        assert "I could help" in user, "recheck must see the restyled body"
        return {"claims": []}

    steps.ask_json = fake_ask_json_restyle
    result = revise()
    assert result is not None
    assert "I could help" in result.body and "there's a fit" not in result.body
    assert result.banned_phrases == []
    assert [l.split(":")[0] for l in labels] == ["revise", "restyle", "recheck"], labels
    print("PASS: a revision with banned wording is restyled before its recheck")

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

async def fake_verify_flagging(contact, company_name, domain, subject, body,
                               on_progress=None):
    return VerifyResult(flagged_claims=["unsupported: x"])

async def fake_verify_clean(contact, company_name, domain, subject, body,
                            on_progress=None):
    return VerifyResult()

async def fake_revise(candidate, contact, company_name, research, subject,
                      body_without_signature, flagged_claims,
                      remove_entirely=False, on_progress=None):
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

    # 4b. Escalation: an 'unsupported' flag surviving round 1 triggers exactly
    #     ONE removal-mode round (remove_entirely=True), then stops
    revise_modes = []

    def make_fake_revise(results):
        seq = list(results)

        async def fake(candidate, contact, company_name, research, subject,
                       body_without_signature, flagged_claims,
                       remove_entirely=False, on_progress=None):
            revise_modes.append(remove_entirely)
            return seq.pop(0)

        return fake

    async def fake_verify_unsupported(contact, company_name, domain, subject,
                                      body, on_progress=None):
        return VerifyResult(flagged_claims=["unsupported: x"])

    steps.verify_draft = fake_verify_unsupported
    steps.revise_flagged_draft = make_fake_revise([
        DraftResult(subject="r1", body="b1", flagged_claims=["unsupported: x"]),
        DraftResult(subject="r2", body="b2", flagged_claims=[]),
    ])
    comp3 = CompanyState(name="Z", domain="z.com")
    asyncio.run(pipeline._run_company(CAND, comp3, [], set(), []))
    assert comp3.status == CompanyStatus.DONE
    assert revise_modes == [False, True], revise_modes
    assert comp3.draft_subject == "r2" and comp3.draft_flagged_claims == []
    print("PASS: a surviving unsupported flag escalates to one removal-mode round")

    # 4c. 'unverified' leftovers do NOT escalate — they ship with the warning
    revise_modes.clear()

    async def fake_verify_unverified(contact, company_name, domain, subject,
                                     body, on_progress=None):
        return VerifyResult(flagged_claims=["unverified: y"])

    steps.verify_draft = fake_verify_unverified
    steps.revise_flagged_draft = make_fake_revise([
        DraftResult(subject="r1", body="b1", flagged_claims=["unverified: y"]),
    ])
    comp4 = CompanyState(name="W", domain="w.com")
    asyncio.run(pipeline._run_company(CAND, comp4, [], set(), []))
    assert comp4.status == CompanyStatus.DONE
    assert revise_modes == [False], revise_modes
    assert comp4.draft_flagged_claims == ["unverified: y"]
    print("PASS: unverified-only leftovers ship flagged, with no escalation round")

    # 4d. Round 1 REJECTED (revision rechecked worse -> None) escalates to the
    #     removal round even for unverified-only flags — a real run shipped a
    #     flagged false attribution ("you introduced X at RSNA") because a
    #     rejected round 1 used to end the process here.
    revise_modes.clear()
    steps.verify_draft = fake_verify_unverified
    steps.revise_flagged_draft = make_fake_revise([
        None,
        DraftResult(subject="r2", body="b2", flagged_claims=[]),
    ])
    comp5 = CompanyState(name="V", domain="v.com")
    asyncio.run(pipeline._run_company(CAND, comp5, [], set(), []))
    assert comp5.status == CompanyStatus.DONE
    assert revise_modes == [False, True], revise_modes
    assert comp5.draft_subject == "r2" and comp5.draft_flagged_claims == []
    print("PASS: a rejected round-1 revision escalates to the removal round")
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
