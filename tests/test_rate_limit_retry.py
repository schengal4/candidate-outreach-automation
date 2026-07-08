"""Verify _send_request retries on RateLimitError/OverloadedError with
backoff (honoring Retry-After) and gives up after RATE_LIMIT_MAX_RETRIES,
without making any real network calls."""
import asyncio
import sys
import httpx
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import anthropic
import app.llm as llm


def make_rate_limit_error(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, headers=headers, request=request, json={"error": {"type": "rate_limit_error"}})
    return anthropic.RateLimitError("rate limited", response=response, body={"error": {"type": "rate_limit_error"}})


class FakeStreamCM:
    """Mimics `async with client.messages.stream(**kwargs) as stream:` for one attempt."""
    def __init__(self, outcome):
        self.outcome = outcome  # either an exception instance, or a final "message" sentinel

    async def __aenter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            return
            yield  # pragma: no cover - empty async generator
        return gen()

    async def get_final_message(self):
        return self.outcome


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        return FakeStreamCM(outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.messages = FakeMessages(outcomes)


async def test_retries_then_succeeds():
    llm.RATE_LIMIT_BASE_DELAY_SECONDS = 0.01  # keep the test fast
    llm.RATE_LIMIT_MAX_DELAY_SECONDS = 0.02
    final_message = object()
    client = FakeClient([
        make_rate_limit_error(retry_after=0.01),
        make_rate_limit_error(),
        final_message,
    ])
    reports = []
    msg = await llm._send_request(client, {}, reports.append)
    assert msg is final_message
    assert client.messages.calls == 3, client.messages.calls
    assert any("Rate limited" in r for r in reports), reports
    print("PASS: retries through repeated 429s, then returns the successful message")


async def test_gives_up_after_max_retries():
    llm.RATE_LIMIT_BASE_DELAY_SECONDS = 0.01
    llm.RATE_LIMIT_MAX_DELAY_SECONDS = 0.02
    client = FakeClient([make_rate_limit_error() for _ in range(llm.RATE_LIMIT_MAX_RETRIES + 1)])
    try:
        await llm._send_request(client, {}, lambda t: None)
        assert False, "expected RateLimitError to propagate"
    except anthropic.RateLimitError:
        pass
    assert client.messages.calls == llm.RATE_LIMIT_MAX_RETRIES + 1, client.messages.calls
    print(f"PASS: gives up and raises after {llm.RATE_LIMIT_MAX_RETRIES + 1} attempts")


async def test_non_retryable_propagates_immediately():
    client = FakeClient([anthropic.BadRequestError(
        "bad request",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
        body={"error": {"type": "invalid_request_error"}},
    )])
    try:
        await llm._send_request(client, {}, lambda t: None)
        assert False, "expected BadRequestError to propagate"
    except anthropic.BadRequestError:
        pass
    assert client.messages.calls == 1, client.messages.calls
    print("PASS: a non-rate-limit error (400) is not retried")


asyncio.run(test_retries_then_succeeds())
asyncio.run(test_gives_up_after_max_retries())
asyncio.run(test_non_retryable_propagates_immediately())
