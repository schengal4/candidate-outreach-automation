"""Web-search replies sometimes leak literal <cite index="..."> markup into
research text (seen in a real run report) — research_contact must strip it
from the summary, items, and red flags before it reaches the UI or a draft."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.pipeline as pipeline
from app.models import CompanyState, Contact


async def fake_ask_json(system, user, **kw):
    return {
        "summary": 'He said <cite index="4-1">a technology company</cite> at launch.',
        "items": [
            'Described the deal as <cite index="4-3">broadening our reach</cite> in a release',
            "A plain item with no markup",
        ],
        "red_flags": ['<cite index="1-0">concerning post</cite>'],
    }


pipeline.ask_json = fake_ask_json

company = CompanyState(name="TestCo", domain="testco.com")
company.contact_used = Contact(first_name="A", last_name="B", title="VP")

asyncio.run(pipeline.research_contact(company, red_flags_enabled=True))

assert company.research_summary == "He said a technology company at launch."
print("PASS: cite tags stripped from the research summary")

assert company.research_items[0] == "Described the deal as broadening our reach in a release"
assert company.research_items[1] == "A plain item with no markup"
print("PASS: cite tags stripped from research items; clean items untouched")

assert company.red_flags == ["concerning post"]
print("PASS: cite tags stripped from red flags")
