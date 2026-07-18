"""Contact-search timeout salvage, two layers:

1. steps.identify_contact — a timed-out contact search gets ONE retry on a
   smaller budget and a tighter clock (same salvage research already had).
2. pipeline._run_company — when the BACKUP search still times out but the
   primary contact is already verified, the company falls back to the
   manual-outreach draft instead of dropping (a real run dropped Regard this
   way with a verified CEO in hand). Related gate fix: a verified primary
   with no email is drafted for manual outreach even when the backup turns
   out unverifiable — the "no verified contact" drop now requires BOTH to be
   unverified.
"""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
import app.steps as steps
from app import config
from app.llm import LLMError, LLMTimeoutError
from app.models import Candidate, CompanyState, CompanyStatus, Contact
from app.steps import DraftResult, ResearchResult, VerifyResult

CONTACT_REPLY = {"first_name": "Jane", "last_name": "Doe", "title": "CTO",
                 "employment_verified": True}
CAND = Candidate(id="tctr", name="T", email="", current_employer="E", resume_text="r")

# ---- 1. steps.identify_contact retry ------------------------------------ #
calls = []


def make_fake(outcomes):
    seq = list(outcomes)

    async def fake_ask_json(system, user, **kw):
        calls.append((kw.get("max_uses"), kw.get("timeout_seconds")))
        outcome = seq.pop(0)
        if outcome == "timeout":
            raise LLMTimeoutError("LLM call timed out after 8 minutes (backup:X).")
        if outcome == "error":
            raise LLMError("kaboom")
        return CONTACT_REPLY

    return fake_ask_json


real_ask_json = steps.ask_json
try:
    # Timeout -> one retry on the smaller budget AND the tighter clock
    calls.clear()
    steps.ask_json = make_fake(["timeout", "ok"])
    contact = asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
    assert contact and contact.full_name == "Jane Doe"
    assert calls == [
        (config.settings.CONTACT_WEB_SEARCH_MAX_USES,
         config.settings.CONTACT_CALL_TIMEOUT_SECONDS),
        (config.settings.CONTACT_TIMEOUT_RETRY_MAX_USES,
         config.settings.CONTACT_TIMEOUT_RETRY_TIMEOUT_SECONDS),
    ], calls
    print("PASS: contact timeout retries once on a smaller budget and tighter clock")

    # A second timeout propagates — one retry, never a loop
    calls.clear()
    steps.ask_json = make_fake(["timeout", "timeout"])
    try:
        asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
        raise AssertionError("expected LLMTimeoutError")
    except LLMTimeoutError:
        pass
    assert len(calls) == 2, calls
    print("PASS: a second contact timeout propagates (no retry loop)")

    # Non-timeout failures don't retry
    calls.clear()
    steps.ask_json = make_fake(["error"])
    try:
        asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
        raise AssertionError("expected LLMError")
    except LLMTimeoutError:
        raise AssertionError("plain LLMError must not be treated as a timeout")
    except LLMError:
        pass
    assert len(calls) == 1, calls
    print("PASS: non-timeout contact failures are not retried")
finally:
    steps.ask_json = real_ask_json

# ---- 2. pipeline fallback when the backup search times out --------------- #
VERIFIED = dict(first_name="Jane", last_name="Doe", title="CTO", employment_verified=True)
UNVERIFIED = dict(first_name="Bob", last_name="Roe", title="VP", employment_verified=False)


def run_scenario(primary_verified: bool, backup_outcome):
    """backup_outcome: 'timeout' | a Contact to return."""

    async def fake_identify(candidate, company_name, domain, excluded, **kw):
        if kw.get("label", "").startswith("backup:"):
            if backup_outcome == "timeout":
                raise LLMTimeoutError("LLM call timed out after 8 minutes (backup:X).")
            return backup_outcome
        return Contact(**(VERIFIED if primary_verified else UNVERIFIED))

    async def fake_lookup(domain, contact, blocked):
        return None, None, ""  # Hunter never finds an email in these scenarios

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
    return comp


# 2a. Verified primary + backup search timeout -> manual-outreach draft
comp = run_scenario(primary_verified=True, backup_outcome="timeout")
assert comp.status == CompanyStatus.DONE, (comp.status, comp.drop_reason)
assert comp.contact_used is comp.primary
assert comp.email == ""  # manual outreach — no Gmail draft downstream
print("PASS: backup timeout with a verified primary falls back to manual outreach")

# 2b. UNverified primary + backup search timeout -> still drops (nothing to
#     fall back to), with the timeout as the reason
comp = run_scenario(primary_verified=False, backup_outcome="timeout")
assert comp.status == CompanyStatus.DROPPED, comp.status
assert "timed out" in comp.drop_reason, comp.drop_reason
print("PASS: backup timeout with no verified primary still drops the company")

# 2c. Gate fix: verified primary + backup found but UNverified -> manual
#     outreach with the primary, not a drop
comp = run_scenario(primary_verified=True, backup_outcome=Contact(**UNVERIFIED))
assert comp.status == CompanyStatus.DONE, (comp.status, comp.drop_reason)
assert comp.contact_used is comp.primary
print("PASS: verified primary is drafted even when the backup is unverifiable")

# 2d. Neither contact verified -> the no-verified-contact drop still fires
comp = run_scenario(primary_verified=False, backup_outcome=Contact(**UNVERIFIED))
assert comp.status == CompanyStatus.DROPPED, comp.status
assert comp.drop_reason == "employment could not be verified for any contact", comp.drop_reason
print("PASS: no verified contact at all still drops with the original reason")
