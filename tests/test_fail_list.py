"""The durable company-failure list (company_failures table): the pipeline
records company-specific drops (contacts can't be found/verified), never
transient ones, clears the record when a later run drafts the company, and
run_discovery's review-gate flag reads from it even when the run reports that
recorded the failure have been pruned (KEEP_RUNS_PER_CANDIDATE). Real data
showed companies that failed 6+ runs back resurfacing with no warning."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import config, run_store

config.settings.CONTACT_LEADS_LIMIT = 0  # never hit Hunter's live API from tests
from app.llm import LLMError
from app.models import Candidate, CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.steps import DraftResult, ResearchResult, VerifyResult

CAND = Candidate(id="failtest01", name="T", email="", current_employer="E", resume_text="r")


def run_company(identify_outcome):
    """identify_outcome: 'none' | 'error' | a Contact (drafts successfully)."""

    async def fake_identify(candidate, company_name, domain, excluded, **kw):
        if identify_outcome == "none":
            return None, None
        if identify_outcome == "error":
            raise LLMError("kaboom")
        return identify_outcome, None

    async def fake_lookup(domain, contact, blocked):
        return "jane@x.com", 90, ""

    async def fake_research(contact, company_name, domain, red_flags_enabled, on_progress=None):
        return ResearchResult(summary="s")

    async def fake_draft(candidate, contact, company_name, research,
                         used_accomplishments=None, on_progress=None):
        return DraftResult(subject="s", body="b")

    async def fake_verify(contact, company_name, domain, subject, body, on_progress=None):
        return VerifyResult()

    real = (steps.identify_contact, steps.lookup_email, steps.research_contact,
            steps.draft_email, steps.verify_draft, pipeline.sent_list.add_entry)
    steps.identify_contact = fake_identify
    steps.lookup_email = fake_lookup
    steps.research_contact = fake_research
    steps.draft_email = fake_draft
    steps.verify_draft = fake_verify
    pipeline.sent_list.add_entry = lambda *a, **k: None
    comp = CompanyState(name="X", domain="failco.com")
    try:
        asyncio.run(pipeline._run_company(CAND, comp, [], set(), []))
    finally:
        (steps.identify_contact, steps.lookup_email, steps.research_contact,
         steps.draft_email, steps.verify_draft, pipeline.sent_list.add_entry) = real
    return comp


try:
    # 1. A company-specific drop is recorded on the durable list
    comp = run_company("none")
    assert comp.status == CompanyStatus.DROPPED
    failures = run_store.company_failures(CAND.id)
    assert failures == {"failco.com": "no contact identified"}, failures
    print("PASS: 'no contact identified' drop lands on the durable fail list")

    # 2. A transient error drop is NOT recorded (and doesn't clear the entry)
    comp = run_company("error")
    assert comp.status == CompanyStatus.DROPPED and comp.drop_reason.startswith("error:")
    failures = run_store.company_failures(CAND.id)
    assert failures == {"failco.com": "no contact identified"}, failures
    print("PASS: a transient-error drop stays off the fail list")

    # 3. The review-gate flag comes from the durable list even with NO
    #    retained past runs (reports pruned) — the bug this table fixes
    async def fake_discover(candidate, count, on_progress=None, excluded_domains=None):
        return [
            {"name": "FailCo", "domain": "failco.com", "reason": "fit"},
            {"name": "NewCo", "domain": "newco.com", "reason": "fit"},
        ]

    real_discover = steps.discover_companies
    steps.discover_companies = fake_discover
    run = RunState(id="testfaillist1", candidate_id=CAND.id)
    try:
        asyncio.run(pipeline.run_discovery(run, CAND, previous_runs=[]))
    finally:
        steps.discover_companies = real_discover
        run_store.delete_run(run.id)
    assert run.phase == RunPhase.REVIEW
    by_domain = {c["domain"]: c for c in run.discovered}
    assert by_domain["failco.com"]["failed_before"] == "no contact identified"
    assert "failed_before" not in by_domain["newco.com"]
    print("PASS: review gate flags from the durable list after run reports are pruned")

    # 4. A later successful draft clears the record
    contact = Contact(first_name="Jane", last_name="Doe", title="CTO", employment_verified=True)
    comp = run_company(contact)
    assert comp.status == CompanyStatus.DONE, (comp.status, comp.drop_reason)
    assert run_store.company_failures(CAND.id) == {}
    print("PASS: a successful draft clears the company's fail-list entry")
finally:
    run_store.clear_company_failure(CAND.id, "failco.com")
