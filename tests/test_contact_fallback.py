"""Contact identification cost fixes: (1) the contact call reports an
opportunistic FALLBACK from the same searches, which the orchestrator tries
before paying for a second identification call (a real run spent that second
call on 5 of 12 companies); (2) Hunter's domain listing feeds the call as
LEADS so its searches verify people instead of discovering them. Leads are
aggregator-grade — never verification — and the pull is skippable
(CONTACT_LEADS_LIMIT=0) so tests can never hit Hunter's live API."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.hunter_async as hunter_async
import app.pipeline as pipeline
import app.steps as steps
from app import config
from app.models import Candidate, CompanyState, CompanyStatus, Contact
from app.steps import DraftResult, ResearchResult, VerifyResult

config.settings.CONTACT_LEADS_LIMIT = 0  # never hit Hunter's live API from tests

CAND = Candidate(id="tcfb", name="T", email="", current_employer="E", resume_text="r")

PRIMARY = {"first_name": "Jane", "last_name": "Doe", "title": "CTO",
           "linkedin_url": "", "linkedin_url_source": "",
           "employment_verified": True, "evidence": "e", "verification_caveat": ""}
FALLBACK = {"first_name": "Bob", "last_name": "Roe", "title": "VP Engineering",
            "linkedin_url": "https://linkedin.com/in/bob-roe", "linkedin_url_source": "",
            "employment_verified": True, "evidence": "team page", "verification_caveat": ""}
NO_FALLBACK = {"first_name": "", "last_name": "", "title": "", "linkedin_url": "",
               "linkedin_url_source": "", "employment_verified": False,
               "evidence": "", "verification_caveat": ""}

# ---- 1. identify_contact: (primary, fallback) parsing ---------------------- #
captured = {}

async def fake_ask_json(system, user, **kw):
    captured["user"] = user
    return {"primary": dict(PRIMARY), "fallback": dict(FALLBACK)}

real_ask_json = steps.ask_json
try:
    steps.ask_json = fake_ask_json
    primary, fallback = asyncio.run(steps.identify_contact(
        CAND, "X", "x.com", [],
        leads=[{"name": "Jane Doe", "title": "CTO"}, {"name": "Bob Roe", "title": ""}],
    ))
    assert primary.full_name == "Jane Doe" and fallback.full_name == "Bob Roe"
    # The provenance gate applies to the fallback too: its URL cited no source.
    assert fallback.linkedin_url == "", fallback.linkedin_url
    assert "Jane Doe — CTO" in captured["user"], captured["user"]
    assert "Bob Roe" in captured["user"]
    assert "leads to verify, not verification" in captured["user"]
    print("PASS: primary+fallback parsed; provenance gate covers the fallback; leads rendered")

    # No leads -> no leads block; empty-name fallback -> None
    async def fake_no_fallback(system, user, **kw):
        captured["user"] = user
        return {"primary": dict(PRIMARY), "fallback": dict(NO_FALLBACK)}

    steps.ask_json = fake_no_fallback
    primary, fallback = asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
    assert primary.full_name == "Jane Doe" and fallback is None
    assert "Hunter's database" not in captured["user"]
    print("PASS: empty-name fallback parses to None; no leads block without leads")
finally:
    steps.ask_json = real_ask_json

# ---- 2. hunter_async.list_people ------------------------------------------ #
from hunter_client import HunterAPIError


class FakeHunter:
    def __init__(self, outcome):
        self.outcome = outcome

    def domain_search(self, **kw):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


real_client = hunter_async._client
try:
    hunter_async._client = FakeHunter({"data": {"emails": [
        {"first_name": "Jane", "last_name": "Doe", "position": "CTO"},
        {"first_name": "", "last_name": "X", "position": "skip me"},  # no first name
        {"first_name": "Bob", "last_name": "Roe", "position": None},
    ]}})
    people = asyncio.run(hunter_async.list_people("x.com", limit=10))
    assert people == [{"name": "Jane Doe", "title": "CTO"},
                      {"name": "Bob Roe", "title": ""}], people
    print("PASS: list_people parses names+titles and skips nameless entries")

    hunter_async._client = FakeHunter(HunterAPIError(429, "rate limited"))
    assert asyncio.run(hunter_async.list_people("x.com", limit=10)) == []
    print("PASS: a Hunter API error degrades to no leads, never an exception")

    hunter_async._client = FakeHunter({"data": {"emails": [{"first_name": "A", "last_name": "B"}]}})
    assert asyncio.run(hunter_async.list_people("x.com", limit=0)) == []
    assert asyncio.run(hunter_async.list_people("x.com")) == []  # config limit is 0 here
    print("PASS: limit 0 (the test-suite setting) skips the pull entirely")
finally:
    hunter_async._client = real_client

# ---- 3. pipeline: inline fallback is tried before a second call ----------- #
def run_scenario(inline_fallback, email_for):
    """email_for: set of full names Hunter finds an email for."""
    labels = []
    excluded_seen = {}

    async def fake_identify(candidate, company_name, domain, excluded, **kw):
        label = kw.get("label", "")
        labels.append(label.split(":")[0])
        excluded_seen[label.split(":")[0]] = list(excluded)
        if label.startswith("backup:"):
            return Contact(first_name="New", last_name="Person", title="Dir",
                           employment_verified=True), None
        return Contact(first_name="Jane", last_name="Doe", title="CTO",
                       employment_verified=True), inline_fallback

    async def fake_lookup(domain, contact, blocked):
        if contact.full_name in email_for:
            return f"{contact.first_name.lower()}@x.com", 90, ""
        return None, None, ""

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
    comp = CompanyState(name="X", domain="x.com")
    try:
        asyncio.run(pipeline._run_company(CAND, comp, [], set(), []))
    finally:
        (steps.identify_contact, steps.lookup_email, steps.research_contact,
         steps.draft_email, steps.verify_draft, pipeline.sent_list.add_entry) = real
    return comp, labels, excluded_seen


# 3a. Verified inline fallback with an email -> used directly, NO second call
fb = Contact(first_name="Bob", last_name="Roe", title="VP", employment_verified=True)
comp, labels, _ = run_scenario(fb, email_for={"Bob Roe"})
assert comp.status == CompanyStatus.DONE, (comp.status, comp.drop_reason)
assert labels == ["contact"], labels  # no backup: call
assert comp.contact_used.full_name == "Bob Roe" and comp.email == "bob@x.com"
print("PASS: a verified inline fallback replaces the second identification call")

# 3b. Unverified inline fallback -> escalation call runs, both names excluded
fb = Contact(first_name="Bob", last_name="Roe", title="VP", employment_verified=False)
comp, labels, excluded_seen = run_scenario(fb, email_for={"New Person"})
assert labels == ["contact", "backup"], labels
assert excluded_seen["backup"] == ["Jane Doe", "Bob Roe"], excluded_seen
assert comp.contact_used.full_name == "New Person"
print("PASS: an unverified fallback still escalates, excluding both known names")

# 3c. No inline fallback -> escalation exactly as before
comp, labels, excluded_seen = run_scenario(None, email_for={"New Person"})
assert labels == ["contact", "backup"], labels
assert excluded_seen["backup"] == ["Jane Doe"], excluded_seen
print("PASS: no fallback surfaced -> the second call runs exactly as before")
