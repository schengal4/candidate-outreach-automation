"""Research runs as TWO parallel focused passes (person / company-news) on
half the search budget each — same total searches, roughly half the wall
clock. Per pass: a timeout gets ONE retry on a smaller budget and a tighter
clock; non-timeout failures don't retry. A failing pass — even BOTH failing —
never drops the company (the contact is already verified by this point):
the draft proceeds on whatever survived, and every failure lands in
ResearchResult.failures for the run report. A pass that reports
search_failed (the structured failure channel in RESEARCH_SCHEMA) keeps its
items but has its summary DISCARDED — models used to narrate the failure
there, and that prose shipped as research notes and even leaked into a
draft."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app import config
from app.llm import LLMError, LLMTimeoutError
from app.models import Contact

CONTACT = Contact(first_name="Jane", last_name="Doe", title="VP")
PERSON_REPLY = {"summary": "person notes.",
                "items": [{"fact": "gave a talk", "source": "s", "date": "", "url": "https://a"}],
                "red_flags": []}
COMPANY_REPLY = {"summary": "company notes.",
                 "items": [{"fact": "raised a round", "source": "s", "date": "", "url": "https://b"}],
                 "red_flags": []}

PER_PASS = max(2, config.settings.RESEARCH_WEB_SEARCH_MAX_USES // 2)
RETRY = max(2, config.settings.RESEARCH_TIMEOUT_RETRY_MAX_USES)

calls = {}


def make_fake(outcomes_by_label):
    """outcomes: label -> list of 'timeout' | 'error' | reply dict, per call."""
    seqs = {k: list(v) for k, v in outcomes_by_label.items()}

    async def fake_ask_json(system, user, **kw):
        label = kw.get("label")
        calls.setdefault(label, []).append((kw.get("max_uses"), kw.get("timeout_seconds")))
        outcome = seqs[label].pop(0)
        if outcome == "timeout":
            raise LLMTimeoutError(f"LLM call timed out ({label}).")
        if outcome == "error":
            raise LLMError("kaboom")
        return outcome

    return fake_ask_json


real_ask_json = steps.ask_json
try:
    # 1. Both passes succeed -> merged items and summary, half budget each
    calls.clear()
    steps.ask_json = make_fake({
        "research:X": [PERSON_REPLY], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert research.summary == "person notes. company notes."
    assert [i["fact"] for i in research.items] == ["gave a talk", "raised a round"]
    expected = (PER_PASS, config.settings.RESEARCH_CALL_TIMEOUT_SECONDS)
    assert calls == {"research:X": [expected], "research-co:X": [expected]}, calls
    print("PASS: two parallel passes on half the budget each; results merged")

    # 2. One pass times out -> retried once on the smaller budget and tighter
    #    clock; still merged
    calls.clear()
    steps.ask_json = make_fake({
        "research:X": ["timeout", PERSON_REPLY], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert len(research.items) == 2
    assert calls["research:X"] == [
        (PER_PASS, config.settings.RESEARCH_CALL_TIMEOUT_SECONDS),
        (RETRY, config.settings.RESEARCH_TIMEOUT_RETRY_TIMEOUT_SECONDS),
    ], calls
    print("PASS: a pass timeout retries once on the smaller budget and tighter clock")

    # 3. A second timeout on the same pass propagates to that pass, but the
    #    OTHER pass's results still come back — no drop, and the failure is
    #    recorded for the run report
    calls.clear()
    steps.ask_json = make_fake({
        "research:X": ["timeout", "timeout"], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert [i["fact"] for i in research.items] == ["raised a round"]
    assert research.summary == "company notes."
    assert len(research.failures) == 1 and research.failures[0].startswith(
        "person research failed"
    ), research.failures
    print("PASS: one pass failing degrades to the other pass instead of dropping")

    # 4. Non-timeout failures don't retry within a pass
    calls.clear()
    steps.ask_json = make_fake({
        "research:X": ["error"], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert len(calls["research:X"]) == 1, calls
    assert len(research.items) == 1
    print("PASS: non-timeout pass failures are not retried")

    # 5. BOTH passes failing no longer raises: the company keeps its verified
    #    contact, the draft runs in no-research mode, and both failures reach
    #    the run report
    calls.clear()
    steps.ask_json = make_fake({
        "research:X": ["error"], "research-co:X": ["timeout", "timeout"],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert research.items == [] and research.summary == ""
    assert len(research.failures) == 2, research.failures
    # ...and the draft prompt payload carries the honest status line instead
    # of leaked failure prose, so the model writes a plain email rather than
    # inventing personalization.
    assert "research_status" in research.as_prompt_payload()
    print("PASS: both passes failing degrades to a no-research draft, not a drop")

    # 5b. search_failed (the structured failure channel): items the pass
    #     still verified are kept, its summary is DISCARDED (failure prose
    #     must never ship as research notes), and the failure is recorded.
    calls.clear()
    starved = {"summary": "I was unable to complete this research pass because "
                          "the web search tool hit its usage limit.",
               "items": [dict(PERSON_REPLY["items"][0])],
               "red_flags": [], "search_failed": True}
    steps.ask_json = make_fake({
        "research:X": [starved], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert [i["fact"] for i in research.items] == ["gave a talk", "raised a round"]
    assert research.summary == "company notes.", research.summary
    assert len(research.failures) == 1 and "web searches" in research.failures[0]
    print("PASS: a search_failed pass keeps its items but its summary never ships")

    # 5c. A clean result carries no failures and no research_status payload key
    steps.ask_json = make_fake({
        "research:X": [PERSON_REPLY], "research-co:X": [COMPANY_REPLY],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert research.failures == []
    assert "research_status" not in research.as_prompt_payload()
    print("PASS: clean research carries no failure flags or status line")

    # 6. The same source found by both passes is deduped (by URL)
    calls.clear()
    dup = {"summary": "dup.", "items": [dict(PERSON_REPLY["items"][0])], "red_flags": []}
    steps.ask_json = make_fake({
        "research:X": [PERSON_REPLY], "research-co:X": [dup],
    })
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert [i["fact"] for i in research.items] == ["gave a talk"], research.items
    print("PASS: duplicate findings across passes are deduped")

    # 7. The system prompt announces the pass's actual search budget. Without
    #    it, models read the max_uses_exceeded rejections after the cap as a
    #    tool outage and set search_failed on every pass — a real run shipped
    #    10 unpersonalized drafts this way.
    systems = {}

    async def capture_ask_json(system, user, **kw):
        systems[kw.get("label")] = (system, kw.get("max_uses"))
        return dict(PERSON_REPLY)

    steps.ask_json = capture_ask_json
    asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert len(systems) == 2, systems.keys()
    for label, (system, max_uses) in systems.items():
        assert f"about {max_uses} searches" in system, (label, max_uses)
        assert "{search_budget}" not in system
    print("PASS: the research system prompt announces the pass's search budget")
finally:
    steps.ask_json = real_ask_json
