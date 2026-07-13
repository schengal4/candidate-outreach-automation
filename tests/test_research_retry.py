"""Research-timeout salvage: a research call that blows its time valve gets
exactly ONE retry on a halved search budget instead of dropping the company
(a real run lost 4 of 10 companies to research timeouts after their contacts
were already verified and emails found). Non-timeout failures don't retry,
and a second timeout still drops."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.steps as steps
from app import config
from app.llm import LLMError, LLMTimeoutError
from app.models import Contact

CONTACT = Contact(first_name="Jane", last_name="Doe", title="VP")
REPLY = {"summary": "s", "items": [], "red_flags": []}

calls = []


def make_fake(outcomes):
    """outcomes: list of 'timeout' | 'error' | 'ok', consumed per call."""
    seq = list(outcomes)

    async def fake_ask_json(system, user, **kw):
        calls.append(kw.get("max_uses"))
        outcome = seq.pop(0)
        if outcome == "timeout":
            raise LLMTimeoutError("LLM call timed out after 8 minutes (research:X).")
        if outcome == "error":
            raise LLMError("kaboom")
        return REPLY

    return fake_ask_json


real_ask_json = steps.ask_json
try:
    # 1. Timeout -> one retry with the smaller budget; result comes back
    calls.clear()
    steps.ask_json = make_fake(["timeout", "ok"])
    research = asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
    assert research.summary == "s"
    assert calls == [
        config.settings.RESEARCH_WEB_SEARCH_MAX_USES,
        config.settings.RESEARCH_TIMEOUT_RETRY_MAX_USES,
    ], calls
    print("PASS: research timeout retries once on the smaller search budget")

    # 2. A second timeout propagates — one retry, never a loop
    calls.clear()
    steps.ask_json = make_fake(["timeout", "timeout"])
    try:
        asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
        raise AssertionError("expected LLMTimeoutError")
    except LLMTimeoutError:
        pass
    assert len(calls) == 2, calls
    print("PASS: a second timeout still drops the company (no retry loop)")

    # 3. Non-timeout failures don't retry — a refusal/parse error on a smaller
    #    budget would fail the same way
    calls.clear()
    steps.ask_json = make_fake(["error"])
    try:
        asyncio.run(steps.research_contact(CONTACT, "X", "x.com", False))
        raise AssertionError("expected LLMError")
    except LLMTimeoutError:
        raise AssertionError("plain LLMError must not be treated as a timeout")
    except LLMError:
        pass
    assert len(calls) == 1, calls
    print("PASS: non-timeout research failures are not retried")
finally:
    steps.ask_json = real_ask_json
