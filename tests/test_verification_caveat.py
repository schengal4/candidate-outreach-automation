"""verification_caveat: the soft-evidence warning survives round-trips, is part
of the enforced contact schema, and surfaces in the run report on both a
drafted company and a salvaged (dropped-with-contact) one. Open mode."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import prompts
from app.main import app as fastapi_app
from app.models import CompanyState, CompanyStatus, Contact, RunPhase, RunState
from app.run_manager import manager

RUNS = manager.runs

CAVEAT = "Founder title from an undated team page — confirm still CEO before sending."

# 1. Round-trips through to_dict/from_dict, and empty-by-default
c = Contact(first_name="A", last_name="B", title="CEO", verification_caveat=CAVEAT)
assert Contact.from_dict(c.to_dict()).verification_caveat == CAVEAT
assert Contact.from_dict({"first_name": "A"}).verification_caveat == ""  # old run files
print("PASS: verification_caveat round-trips and defaults empty")

# 2. Part of the enforced contact schema — for the primary AND the fallback
for person in ("primary", "fallback"):
    assert "verification_caveat" in prompts.CONTACT_SCHEMA["properties"][person]["required"]
print("PASS: verification_caveat is a required field for both schema contacts")

# 3. Drafted company shows the "verify before sending" banner
run = RunState(id="testcaveat001", candidate_id="516e7c4751", phase=RunPhase.DONE)
done = CompanyState(name="Unlearnish", domain="u.ai", status=CompanyStatus.DONE)
done.contact_used = Contact(
    first_name="Charles", last_name="F", title="CEO",
    linkedin_url="https://linkedin.com/in/cf", employment_verified=True,
    verification_caveat=CAVEAT,
)
done.email = "c@u.ai"
done.draft_subject = "s"
done.draft_body = "Hi Charles,\n\nbody\n\nThanks,"
run.companies = [done]
RUNS[run.id] = run
try:
    html = TestClient(fastapi_app).get(f"/runs/{run.id}/panel").text
    assert "Verify this contact before sending" in html and CAVEAT in html
    print("PASS: drafted company renders the verify-before-sending banner")
finally:
    RUNS.pop(run.id, None)

# 3b. Employment-doubt classification: departure signals are priority,
#     routine confirm-the-title nudges are not
from app.models import employment_doubt

assert employment_doubt("a newer profile suggests they may have left the company")
assert employment_doubt("the fact-check found an updated role/title: CTO")
assert employment_doubt("company was acquired in May; leadership routinely departs")
assert not employment_doubt(CAVEAT)  # confirm-still-CEO nudge stays routine
assert not employment_doubt("Title sourced from an org-chart aggregator; confirm precise title")
assert not employment_doubt("")
print("PASS: employment_doubt separates departure signals from routine nudges")

# 3c. An employment-doubt caveat renders in the red priority tier, and the
#     report leads with the priority digest
run3 = RunState(id="testcaveat003", candidate_id="516e7c4751", phase=RunPhase.DONE)
doubt = CompanyState(name="GoneCo", domain="gone.com", status=CompanyStatus.DONE)
doubt.contact_used = Contact(
    first_name="Steve", last_name="B", title="CTO", employment_verified=True,
    verification_caveat="a newer LinkedIn profile suggests they may have left the company",
)
doubt.email = "s@gone.com"
doubt.draft_subject = "s"
doubt.draft_body = "Hi Steve,\n\nbody\n\nThanks,"
run3.companies = [doubt]
RUNS[run3.id] = run3
try:
    html = TestClient(fastapi_app).get(f"/runs/{run3.id}/panel").text
    assert "Serious question about this contact" in html
    assert "flag priority" in html
    assert "need attention before sending" in html
    assert "employment in question" in html
    print("PASS: employment-doubt caveat renders red, with the digest up top")
finally:
    RUNS.pop(run3.id, None)

# 4. Salvaged (dropped-with-contact) company shows the caveat inline
run2 = RunState(id="testcaveat002", candidate_id="516e7c4751", phase=RunPhase.DONE)
dropped = CompanyState(name="StaleCo", domain="stale.com", status=CompanyStatus.DROPPED)
dropped.drop_reason = "no valid email found"
dropped.primary = Contact(
    first_name="Jane", last_name="Doe", title="Founder & CEO",
    linkedin_url="https://linkedin.com/in/jd", employment_verified=True,
    verification_caveat=CAVEAT,
)
run2.companies = [dropped]
RUNS[run2.id] = run2
try:
    html = TestClient(fastapi_app).get(f"/runs/{run2.id}/panel").text
    assert "verify first" in html and CAVEAT in html
    print("PASS: salvaged dropped-company contact renders the caveat inline")
finally:
    RUNS.pop(run2.id, None)
