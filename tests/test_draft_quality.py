"""Draft writing-quality backstops: the banned-phrase detector, the one-shot
style redraft it triggers (adopted only when strictly cleaner), the
banned-phrase flag on the draft result, and accomplishment rotation across
a batch."""
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
# Tells from the 2026-07-17 run's recipient-perspective review: resume-speak,
# the curiosity-dressed ask, and presumed peer status.
hits = banned_style_hits(
    "I offer close to three years of experience. I'd like to hear how your "
    "team thinks about this, from one builder to another."
)
assert "i offer" in hits and "from one builder to another" in hits, hits
assert any(h.startswith("i'd like to hear") for h in hits), hits
assert banned_style_hits("I built the offer-comparison flow last year.") == []
print("PASS: detector flags real-run leaks and passes clean prose")

# 2. Banned phrasing triggers ONE style redraft; a cleaner redo is adopted
#    (with the original featured_accomplishment kept) and the flag clears
calls = []

async def fake_ask_json(system, user, **kw):
    calls.append({"user": user, "label": kw.get("label", "")})
    if kw.get("label", "").startswith("restyle:"):
        return {"subject": "s", "body": "Hi Ann,\n\nYour work stood out to me.\n\nThanks,",
                "featured_accomplishment": "SHOULD BE IGNORED"}
    return {"subject": "s", "body": "Hi Ann,\n\nYour work caught my attention.\n\nThanks,",
            "featured_accomplishment": "trial matching app"}

steps.ask_json = fake_ask_json
result = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert len(calls) == 2, f"expected draft + restyle, saw {len(calls)} calls"
assert calls[1]["label"] == "restyle:TestCo"
assert "caught my attention" in calls[1]["user"], "redraft must name the banned phrasing"
assert "stood out to me" in result.body and "caught my attention" not in result.body
assert result.banned_phrases == [], result.banned_phrases
assert result.featured_accomplishment == "trial matching app", \
    "restyle must not change the accomplishment bookkeeping"
print("PASS: banned phrasing triggers one style redraft; cleaner redo adopted")

# 2b. A redo that is no cleaner is discarded — original kept, still flagged
async def fake_no_cleaner(system, user, **kw):
    return {"subject": "s", "body": "Hi Ann,\n\nYour work caught my attention.\n\nThanks,",
            "featured_accomplishment": "trial matching app"}

steps.ask_json = fake_no_cleaner
result = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert "caught my attention" in result.banned_phrases, result.banned_phrases
print("PASS: a no-cleaner redo is discarded and the leak stays flagged")

# 3. Clean draft -> one call, no flag
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

# 5. Banned phrasing is reported on the result so the run report can flag it,
#    instead of the leak living only in a log line. A clean draft leaves the
#    list empty.
async def fake_banned(system, user, **kw):
    return {"subject": "s", "body": "Hi Ann,\n\nYour talk stuck with me.\n\nThanks,",
            "featured_accomplishment": "trial matching app"}

steps.ask_json = fake_banned
flagged = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert "stuck with me" in flagged.banned_phrases, flagged.banned_phrases
print("PASS: banned phrasing is reported on the draft result")

steps.ask_json = fake_clean
clean = asyncio.run(steps.draft_email(cand, contact, "TestCo", research))
assert clean.banned_phrases == []
print("PASS: a clean draft records no banned phrasing")
