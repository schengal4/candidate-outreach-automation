"""Claim-to-item grounding in draft_email: research items are numbered in the
prompt payload, the draft must bind every company/contact claim to an item id
(claims_used), and a draft admitting to unbound claims gets exactly ONE
grounding redraft — adopted only when cleaner — before the (unchanged)
independent web fact-check. Targets the observed fabrication pattern: every
flagged claim in real runs was a draft-time elaboration of accurate research."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app.models import Candidate, Contact
from app.steps import ResearchResult

CAND = Candidate(id="groundtest", name="T", email="", current_employer="X", resume_text="r")
CONTACT = Contact(first_name="Jane", last_name="Doe", title="CTO")
RESEARCH = ResearchResult(summary="s", items=[
    {"fact": "blog post about coronary CT segmentation", "source": "Cleerly blog",
     "date": "2026-06", "url": "https://x.com/post"},
])

# 0. The draft prompt payload numbers the items (what claims_used ids refer to)
assert '"id": 1' in RESEARCH.as_prompt_payload()
print("PASS: research items are numbered in the draft prompt payload")

# 0a. Candidate-background claims are explicitly excluded from claims_used —
#     a real run flagged the candidate's own resume accomplishments as
#     "unbound" three times, and the redraft risked stripping them out
from app import prompts
assert "candidate's own background" in prompts.DRAFT_SYSTEM
print("PASS: DRAFT_SYSTEM excludes candidate-background claims from claims_used")

# 0b. _unbound_claims: empty ids and out-of-range ids are unbound; valid bind isn't
assert steps._unbound_claims(
    {"claims_used": [{"claim": "ok", "item_ids": [1]}]}, 1) == []
assert steps._unbound_claims(
    {"claims_used": [{"claim": "confessed", "item_ids": []},
                     {"claim": "phantom item", "item_ids": [7]}]}, 1
) == ["confessed", "phantom item"]
assert steps._unbound_claims({"claims_used": [{"claim": "c", "item_ids": [1]}]}, 0) == ["c"]
print("PASS: unbound-claim detection covers empty, out-of-range, and no-items cases")

GOOD = {"subject": "s", "body": "clean grounded body", "featured_accomplishment": "a",
        "claims_used": [{"claim": "coronary CT segmentation blog", "item_ids": [1]}]}
BAD = {"subject": "s", "body": "body with an invented claim", "featured_accomplishment": "a",
       "claims_used": [{"claim": "CT/MRI segmentation pipeline", "item_ids": []},
                       {"claim": "reacts to claims after the fact", "item_ids": [7]}]}

calls = []


def make_fake(replies):
    seq = list(replies)

    async def fake_ask_json(system, user, **kw):
        calls.append((kw.get("label"), user))
        return seq.pop(0)

    return fake_ask_json


real_ask_json = steps.ask_json
try:
    # 1. Fully grounded draft -> accepted, exactly one call
    calls.clear()
    steps.ask_json = make_fake([GOOD])
    draft = asyncio.run(steps.draft_email(CAND, CONTACT, "X", RESEARCH))
    assert [c[0] for c in calls] == ["draft:X"], calls
    assert "clean grounded body" in draft.body
    print("PASS: a fully grounded draft needs no extra call")

    # 2. Unbound claims -> ONE grounding redraft naming the violations;
    #    the cleaner redraft is adopted
    calls.clear()
    steps.ask_json = make_fake([BAD, GOOD])
    draft = asyncio.run(steps.draft_email(CAND, CONTACT, "X", RESEARCH))
    assert [c[0] for c in calls] == ["draft:X", "reground:X"], calls
    assert "CT/MRI segmentation pipeline" in calls[1][1], "violation must be named in the redraft prompt"
    assert "clean grounded body" in draft.body, "cleaner redraft should be adopted"
    print("PASS: unbound claims trigger one grounding redraft; cleaner redraft adopted")

    # 3. Redraft no better -> original kept, never a loop
    calls.clear()
    steps.ask_json = make_fake([BAD, BAD])
    draft = asyncio.run(steps.draft_email(CAND, CONTACT, "X", RESEARCH))
    assert len(calls) == 2, calls
    assert "invented claim" in draft.body
    print("PASS: a redraft that is no cleaner is discarded, with no retry loop")
finally:
    steps.ask_json = real_ask_json
