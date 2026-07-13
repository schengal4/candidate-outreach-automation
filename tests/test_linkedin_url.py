"""LinkedIn URL guardrails: real runs shipped model-guessed profile slugs
(/in/karl-thiele, /in/justin-warner) that belonged to strangers, and one
scheme-less URL that rendered as a broken localhost-relative link. Covers:
scheme normalization at the model boundary, the provenance gate that drops
unsourced URLs, and the Hunter-vs-model reconciliation in the orchestrator."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app import prompts
from app.models import Candidate, Contact, normalize_linkedin_url
from app.pipeline import _apply_hunter_linkedin, _apply_verify_contact_updates
from app.steps import VerifyResult

# 1. Scheme normalization in Contact.from_dict (also repairs old persisted runs)
c = Contact.from_dict({"first_name": "A", "last_name": "B",
                       "linkedin_url": "linkedin.com/in/othmanlaraki"})
assert c.linkedin_url == "https://linkedin.com/in/othmanlaraki"
c = Contact.from_dict({"first_name": "A", "last_name": "B",
                       "linkedin_url": "https://www.linkedin.com/in/x"})
assert c.linkedin_url == "https://www.linkedin.com/in/x"
c = Contact.from_dict({"first_name": "A", "last_name": "B", "linkedin_url": ""})
assert c.linkedin_url == ""
assert normalize_linkedin_url("  www.linkedin.com/in/y ") == "https://www.linkedin.com/in/y"
print("PASS: scheme-less LinkedIn URLs are normalized at the model boundary")

# 2. Schema demands provenance for the URL
assert "linkedin_url_source" in prompts.CONTACT_SCHEMA["required"]
assert "guess" in prompts.CONTACT_SYSTEM  # the don't-guess rule is in the prompt
print("PASS: contact schema requires linkedin_url_source; prompt bans guessed URLs")

# 3. identify_contact drops an unsourced URL, keeps a sourced one
CAND = Candidate(id="testliurl", name="T", email="", current_employer="E", resume_text="r")


def contact_reply(url, source):
    return {"first_name": "Jane", "last_name": "Doe", "title": "VP",
            "linkedin_url": url, "linkedin_url_source": source,
            "employment_verified": True, "evidence": "e", "verification_caveat": ""}


async def fake_unsourced(system, user, **kw):
    return contact_reply("https://linkedin.com/in/jane-doe", "")

async def fake_sourced(system, user, **kw):
    return contact_reply("linkedin.com/in/janedoe", "LinkedIn result in web search")

real_ask_json = steps.ask_json
try:
    steps.ask_json = fake_unsourced
    got = asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
    assert got.linkedin_url == "", got.linkedin_url
    steps.ask_json = fake_sourced
    got = asyncio.run(steps.identify_contact(CAND, "X", "x.com", []))
    assert got.linkedin_url == "https://linkedin.com/in/janedoe"
finally:
    steps.ask_json = real_ask_json
print("PASS: unsourced LinkedIn URLs are dropped; sourced ones kept (and normalized)")

# 4. Hunter reconciliation: fills a blank, agrees quietly, wins loudly on conflict
contact = Contact(first_name="J", last_name="D", linkedin_url="")
_apply_hunter_linkedin(contact, "www.linkedin.com/in/real")
assert contact.linkedin_url == "https://www.linkedin.com/in/real"
assert contact.verification_caveat == ""
print("PASS: Hunter's URL fills a blank without a caveat")

contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://de.linkedin.com/in/JaneDoe/")
_apply_hunter_linkedin(contact, "https://www.linkedin.com/in/janedoe")
assert contact.linkedin_url == "https://de.linkedin.com/in/JaneDoe/"  # same slug — no change
assert contact.verification_caveat == ""
print("PASS: same slug on a different host/case is treated as agreement")

contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://linkedin.com/in/jane-doe",
                  verification_caveat="title from an undated page")
_apply_hunter_linkedin(contact, "https://linkedin.com/in/janedoe42")
assert contact.linkedin_url == "https://linkedin.com/in/janedoe42"  # Hunter wins
assert "conflicting LinkedIn profiles" in contact.verification_caveat
assert contact.verification_caveat.startswith("title from an undated page; ")
print("PASS: conflicting URLs -> Hunter's wins and the mismatch becomes a caveat")

contact = Contact(first_name="J", last_name="D", linkedin_url="https://linkedin.com/in/x")
_apply_hunter_linkedin(contact, "")
assert contact.linkedin_url == "https://linkedin.com/in/x" and contact.verification_caveat == ""
print("PASS: no Hunter URL leaves the contact untouched")

# 5. lookup_email passes Hunter's URL through (and "" when there's no match)
import app.hunter_async as hunter_async

async def fake_find(domain, first, last):
    return "jane@x.com", 91, "https://linkedin.com/in/janedoe"

real_find = hunter_async.find_email
try:
    steps.hunter_async.find_email = fake_find
    email, score, url = asyncio.run(
        steps.lookup_email("x.com", Contact(first_name="Jane", last_name="Doe"), set())
    )
    assert (email, score, url) == ("jane@x.com", 91, "https://linkedin.com/in/janedoe")
    # Blocked email -> no result at all, including no URL
    email, score, url = asyncio.run(
        steps.lookup_email("x.com", Contact(first_name="Jane", last_name="Doe"),
                           {"jane@x.com"})
    )
    assert (email, score, url) == (None, None, "")
finally:
    steps.hunter_async.find_email = real_find
print("PASS: lookup_email forwards Hunter's LinkedIn URL; blocked matches return nothing")

# 6. Post-fact-check contact updates (pipeline._apply_verify_contact_updates)
# 6a. A newer title becomes a caveat, never a drop
contact = Contact(first_name="J", last_name="D", title="Senior Engineering Manager")
_apply_verify_contact_updates(contact, VerifyResult(contact_update="now Director (LinkedIn)"))
assert "updated role/title: now Director (LinkedIn)" in contact.verification_caveat
print("PASS: a title change from the fact-check becomes a verification caveat")

# 6b. A found correction fills a blank URL quietly
contact = Contact(first_name="J", last_name="D")
_apply_verify_contact_updates(
    contact, VerifyResult(linkedin_verdict="not-found",
                          linkedin_correction="linkedin.com/in/janedoe"))
assert contact.linkedin_url == "https://linkedin.com/in/janedoe"
assert contact.verification_caveat == ""
print("PASS: fact-check's found profile URL fills a blank without a caveat")

# 6c. A differing correction replaces the URL with a caveat; same slug is quiet
contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://linkedin.com/in/jane-doe")
_apply_verify_contact_updates(
    contact, VerifyResult(linkedin_correction="https://linkedin.com/in/janedoe42"))
assert contact.linkedin_url == "https://linkedin.com/in/janedoe42"
assert "different LinkedIn profile" in contact.verification_caveat
contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://de.linkedin.com/in/JaneDoe")
_apply_verify_contact_updates(
    contact, VerifyResult(linkedin_verdict="confirmed",
                          linkedin_correction="https://www.linkedin.com/in/janedoe"))
assert contact.linkedin_url == "https://de.linkedin.com/in/JaneDoe"
assert contact.verification_caveat == ""
print("PASS: differing correction wins with a caveat; same-slug confirmation is quiet")

# 6d. wrong-person with no correction removes the URL (report falls back to search)
contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://linkedin.com/in/stranger")
_apply_verify_contact_updates(contact, VerifyResult(linkedin_verdict="wrong-person"))
assert contact.linkedin_url == ""
assert "could not be matched" in contact.verification_caveat
# ...but "not-found" (couldn't tell) leaves a URL alone — absence isn't evidence
contact = Contact(first_name="J", last_name="D",
                  linkedin_url="https://linkedin.com/in/janedoe")
_apply_verify_contact_updates(contact, VerifyResult(linkedin_verdict="not-found"))
assert contact.linkedin_url == "https://linkedin.com/in/janedoe"
assert contact.verification_caveat == ""
print("PASS: wrong-person URLs are removed; inconclusive checks change nothing")
