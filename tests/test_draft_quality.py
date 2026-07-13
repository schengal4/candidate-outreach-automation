"""Draft writing-quality backstops: the banned-phrase detector, the
one-redraft pass in draft_email, and accomplishment rotation across a batch."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app.draft_hygiene import banned_style_hits
from app.models import Candidate, Contact
from app.steps import ResearchResult

cand = Candidate(
    id="testdq00001", name="Test Person", email="",
    current_employer="Acme", resume_text="resume text",
)
contact = Contact(first_name="Ann", last_name="Lee", title="VP")
research = ResearchResult(summary="summary")


# 1. Detector flags the phrases that leaked in real runs, passes clean text
hits = banned_style_hits(
    "The FLDL model caught my attention — your talk stuck with me, "
    "and it seems central to what your group is doing."
)
assert "caught my attention" in hits and "stuck with me" in hits and "—" in hits
assert "seems central to what" in hits
assert banned_style_hits(
    "The ASCO integration is the right way to handle clinical AI trust."
) == []
print("PASS: detector flags real-run leaks and passes clean prose")

# 2. Banned phrasing triggers exactly one redraft; clean version is kept
calls = []

async def fake_ask_json(system, user, **kw):
    calls.append(user)
    if len(calls) == 1:
        return {"subject": "s", "body": "Hi Ann,\n\nYour work caught my attention.\n\nThanks,",
                "featured_accomplishment": "trial matching app"}
    return {"subject": "s", "body": "Hi Ann,\n\nFresh specific wording.\n\nThanks,",
            "featured_accomplishment": "trial matching app"}

steps.ask_json = fake_ask_json
result = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert len(calls) == 2, f"expected 1 redraft, saw {len(calls)} calls"
assert "caught my attention" in calls[1], "retry prompt should name the violation"
assert "caught my attention" not in result.body
print("PASS: banned phrasing triggers one redraft and the clean version is kept")

# 3. Clean first draft -> no second call
calls.clear()

async def fake_clean(system, user, **kw):
    calls.append(user)
    return {"subject": "s", "body": "Hi Ann,\n\nClean and specific.\n\nThanks,",
            "featured_accomplishment": "segmentation tool"}

steps.ask_json = fake_clean
asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert len(calls) == 1
print("PASS: clean draft is not redrafted")

# 4. Accomplishment rotation: tracked in the shared list, fed to later drafts
calls.clear()
used = []
asyncio.run(steps.draft_email(cand, contact, "TestCo", research, used_accomplishments=used))
assert used == ["segmentation tool"]
assert "already featured" not in calls[0], "first draft should get no exclusion list"
asyncio.run(steps.draft_email(cand, contact, "TestCo", research, used_accomplishments=used))
assert "already featured" in calls[1] and "segmentation tool" in calls[1]
assert used == ["segmentation tool", "segmentation tool"]
print("PASS: featured accomplishments are tracked and fed to later drafts")

# 5. Phrasing that survives the redraft is reported on the result so the run
#    report can flag it, instead of the leak living only in a log line. A clean
#    draft leaves the list empty.
async def fake_always_banned(system, user, **kw):
    return {"subject": "s", "body": "Hi Ann,\n\nYour talk stuck with me.\n\nThanks,",
            "featured_accomplishment": "trial matching app"}

steps.ask_json = fake_always_banned
survived = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert "stuck with me" in survived.banned_phrases, survived.banned_phrases
print("PASS: surviving banned phrasing is reported on the draft result")

steps.ask_json = fake_clean
clean = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert clean.banned_phrases == []
print("PASS: a clean draft records no banned phrasing")
