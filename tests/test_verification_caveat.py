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

# 2. Part of the enforced contact schema
assert "verification_caveat" in prompts.CONTACT_SCHEMA["required"]
print("PASS: verification_caveat is a required field in the contact schema")

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
