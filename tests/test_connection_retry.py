"""Connection-outage salvage in ask_json. A real run lost the network mid-run
("getaddrinfo failed") and FIVE companies dropped in four seconds as
"unexpected error: APIConnectionError" — all with verified contacts already
paid for. ask_json now treats a connection error as "wait for the network,
then retry the request" (up to CONNECTION_ERROR_MAX_RETRIES times, with the
wait NOT counting against the per-call timeout), and only fails — with a
clear LLMConnectionError naming the step — when the network stays down."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import anthropic
import httpx

import app.llm as llm
from app import config

REAL_SEND_REQUEST = llm._send_request
REAL_WAIT = llm._wait_for_connectivity

config.settings.LLM_CALL_TIMEOUT_SECONDS = 60
config.settings.CONNECTION_PROBE_INTERVAL_SECONDS = 0.01  # shrink settle sleep


class FakeUsage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class FakeMessage:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [FakeTextBlock(text)]
        self.container = None
        self.usage = FakeUsage()


class FakeTextBlock:
    type = "text"
    def __init__(self, text):
        self.text = text


def conn_error():
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


async def test_outage_then_recovery():
    # First request dies on a connection error; connectivity comes back; the
    # retried request succeeds. The caller never sees the error.
    config.settings.CONNECTION_ERROR_MAX_RETRIES = 3
    outcomes = [conn_error(), FakeMessage('{"ok": true}')]
    waits = []

    async def flaky_send_request(client, kwargs, report, label=""):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_wait(report, label=""):
        waits.append(label)
        return True

    llm._send_request = flaky_send_request
    llm._wait_for_connectivity = fake_wait
    result = await llm.ask_json("S", "u", label="draft:BlipCo")
    assert result == {"ok": True}
    assert waits == ["draft:BlipCo"], waits
    print("PASS: a connection blip is waited out and the request retried")


async def test_mid_stream_httpx_error_also_retried():
    # Raw httpx transport errors can escape from mid-stream iteration without
    # the SDK's APIConnectionError wrapper — same salvage applies.
    config.settings.CONNECTION_ERROR_MAX_RETRIES = 3
    outcomes = [httpx.ReadError("connection lost mid-stream"),
                FakeMessage('{"ok": true}')]

    async def flaky_send_request(client, kwargs, report, label=""):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_wait(report, label=""):
        return True

    llm._send_request = flaky_send_request
    llm._wait_for_connectivity = fake_wait
    result = await llm.ask_json("S", "u", label="research:DropCo")
    assert result == {"ok": True}
    print("PASS: a mid-stream httpx transport error gets the same salvage")


async def test_retries_exhausted():
    # Network flaps: connectivity "returns" each time but every request
    # attempt still fails. After CONNECTION_ERROR_MAX_RETRIES the call gives
    # up with a labeled LLMConnectionError (an LLMError, so the pipeline
    # drops the company with a clear reason, not "unexpected error").
    config.settings.CONNECTION_ERROR_MAX_RETRIES = 2
    attempts = {"n": 0}

    async def always_failing(client, kwargs, report, label=""):
        attempts["n"] += 1
        raise conn_error()

    async def fake_wait(report, label=""):
        return True

    llm._send_request = always_failing
    llm._wait_for_connectivity = fake_wait
    try:
        await llm.ask_json("S", "u", label="contact:FlapCo")
        raise AssertionError("expected LLMConnectionError")
    except llm.LLMConnectionError as exc:
        assert "contact:FlapCo" in str(exc) and "connection" in str(exc).lower(), exc
        assert isinstance(exc, llm.LLMError)  # existing catch-sites still work
    assert attempts["n"] == 3, attempts  # original + 2 retries
    print("PASS: exhausted connection retries raise a labeled LLMConnectionError")


async def test_network_stays_down():
    # Connectivity never comes back within the wait window: fail immediately
    # with the clear network message instead of burning more retries.
    config.settings.CONNECTION_ERROR_MAX_RETRIES = 3
    attempts = {"n": 0}

    async def always_failing(client, kwargs, report, label=""):
        attempts["n"] += 1
        raise conn_error()

    async def fake_wait(report, label=""):
        return False

    llm._send_request = always_failing
    llm._wait_for_connectivity = fake_wait
    try:
        await llm.ask_json("S", "u", label="verify:DarkCo")
        raise AssertionError("expected LLMConnectionError")
    except llm.LLMConnectionError as exc:
        assert "verify:DarkCo" in str(exc) and "did not come back" in str(exc), exc
    assert attempts["n"] == 1, attempts  # no pointless retries while offline
    print("PASS: a network that stays down fails fast with a clear reason")


try:
    asyncio.run(test_outage_then_recovery())
    asyncio.run(test_mid_stream_httpx_error_also_retried())
    asyncio.run(test_retries_exhausted())
    asyncio.run(test_network_stays_down())
finally:
    llm._send_request = REAL_SEND_REQUEST
    llm._wait_for_connectivity = REAL_WAIT
