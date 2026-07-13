"""Web-search replies sometimes leak literal <cite index="..."> markup into
research text (seen in a real run report) — research_contact must strip it
from the summary, items, and red flags before it reaches the UI or a draft.
Also covers the structured item shape (fact/source/date/url) and tolerance
for plain-string items."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app import prompts
from app.models import Contact


async def fake_ask_json(system, user, **kw):
    return {
        "summary": 'He said <cite index="4-1">a technology company</cite> at launch.',
        "items": [
            {
                "fact": 'Described the deal as <cite index="4-3">broadening our reach</cite> in a release',
                "source": '<cite index="4-3">Business Wire</cite>',
                "date": "2026-03",
                "url": "https://example.com/release",
            },
            "A plain item with no markup",  # pre-structured shape, still tolerated
            {"fact": "", "source": "x", "date": "", "url": ""},  # empty fact -> dropped
        ],
        "red_flags": ['<cite index="1-0">concerning post</cite>'],
    }


steps.ask_json = fake_ask_json

contact = Contact(first_name="A", last_name="B", title="VP")
research = asyncio.run(
    steps.research_contact(contact, "TestCo", "testco.com", red_flags_enabled=True)
)

assert research.summary == "He said a technology company at launch."
print("PASS: cite tags stripped from the research summary")

assert research.items[0] == {
    "fact": "Described the deal as broadening our reach in a release",
    "source": "Business Wire",
    "date": "2026-03",
    "url": "https://example.com/release",
}
print("PASS: cite tags stripped from structured item fields; provenance kept")

assert research.items[1] == {
    "fact": "A plain item with no markup", "source": "", "date": "", "url": "",
}
assert len(research.items) == 2  # the empty-fact item was dropped
print("PASS: plain-string items normalized; empty-fact items dropped")

assert research.red_flags == ["concerning post"]
print("PASS: cite tags stripped from red flags")

# The schema requires full provenance on every item, so the model can't quietly
# omit sources/dates.
item_schema = prompts.RESEARCH_SCHEMA["properties"]["items"]["items"]
assert set(item_schema["required"]) == {"fact", "source", "date", "url"}
print("PASS: research schema requires fact/source/date/url on every item")
