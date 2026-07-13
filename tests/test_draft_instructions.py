"""Draft-instructions field: save via the new route (ownership-enforced),
render on the candidate page, and land in draft_email's cache_prefix.
Restores the candidate's original value afterwards."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main
import app.auth as auth_mod
import app.steps as steps
from app import config, storage
from app.models import Candidate, Contact
from app.steps import ResearchResult

config.settings.LOGIN_REQUIRED = True
OWNER = "venkatachengalvala@gmail.com"
CID = "516e7c4751"
INSTR = "Keep it under 100 words. Mention my availability for on-site work. Template: greeting, one observation, one accomplishment, ask."

original = storage.get_candidate(CID).draft_instructions

client = TestClient(main.app)
auth_mod.handle_login_callback = lambda code, state: {"email": OWNER, "name": "Venkata"}
client.get("/auth/callback?code=x&state=y")

try:
    # 1. Save via the route
    r = client.post(f"/candidates/{CID}/draft_instructions",
                    data={"draft_instructions": INSTR}, follow_redirects=False)
    assert r.status_code == 303
    assert storage.get_candidate(CID).draft_instructions == INSTR
    print("PASS: instructions saved via the candidate-page route")

    # 2. Rendered back in the editable textarea
    html = client.get(f"/candidates/{CID}").text
    assert "Mention my availability" in html and "✓ set" in html
    print("PASS: candidate page shows the saved instructions in the edit box")

    # 3. Ownership enforced on the edit route
    other = TestClient(main.app)
    auth_mod.handle_login_callback = lambda code, state: {"email": "x@example.com", "name": "X"}
    other.get("/auth/callback?code=x&state=y")
    r = other.post(f"/candidates/{CID}/draft_instructions",
                   data={"draft_instructions": "HIJACKED"}, follow_redirects=False)
    assert r.status_code == 404
    assert storage.get_candidate(CID).draft_instructions == INSTR
    print("PASS: another account cannot edit the instructions (404, unchanged)")

    # 4. draft_email includes them in the cached prefix; omits when empty
    captured = {}

    async def fake_ask_json(system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        captured["cache_prefix"] = kw.get("cache_prefix", "")
        return {"subject": "s", "body": "b"}

    steps.ask_json = fake_ask_json
    contact = Contact(first_name="A", last_name="B", title="VP")
    cand = storage.get_candidate(CID)
    asyncio.run(steps.draft_email(cand, contact, "TestCo", ResearchResult()))
    assert "Candidate's own drafting instructions" in captured["cache_prefix"]
    assert INSTR in captured["cache_prefix"]
    assert "Candidate's own drafting instructions" in captured["system"]  # DRAFT_SYSTEM precedence rule
    print("PASS: draft prompt carries the instructions in the cache prefix")

    cand.draft_instructions = ""
    asyncio.run(steps.draft_email(cand, contact, "TestCo", ResearchResult()))
    assert "drafting instructions" not in captured["cache_prefix"]
    print("PASS: no instructions -> prompt unchanged from before the feature")
finally:
    c = storage.get_candidate(CID)
    c.draft_instructions = original
    storage.save_candidate(c)
    print(f"cleanup: restored original instructions ({original!r})")
