"""ask_json's guards: `schema` rides in output_config.format (merged with
effort, without clobbering it), a call that outlives LLM_CALL_TIMEOUT_SECONDS
raises LLMError instead of hanging until the run-wide timeout kills the whole
run, and a stream that goes silent is abandoned by the no-progress watchdog
(LLM_STALL_TIMEOUT_SECONDS) long before the wall clock."""
import asyncio
import sys
import time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.llm as llm
from app import config

REAL_SEND_REQUEST = llm._send_request

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


class FakeUsage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class FakeMessage:
    def __init__(self, content):
        self.stop_reason = "end_turn"
        self.content = content
        self.container = None
        self.usage = FakeUsage()


class FakeTextBlock:
    type = "text"
    def __init__(self, text):
        self.text = text


async def test_schema_sets_output_format():
    captured = []

    async def fake_send_request(client, kwargs, report, label=""):
        captured.append(kwargs)
        return FakeMessage([FakeTextBlock('{"ok": true}')])

    llm._send_request = fake_send_request
    await llm.ask_json("SYSTEM", "user", schema=SCHEMA)
    assert captured[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA}
    }, captured[0]
    print("PASS: schema alone -> output_config carries only the format")

    await llm.ask_json("SYSTEM", "user", schema=SCHEMA, effort="medium")
    assert captured[1]["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }, captured[1]
    print("PASS: schema + effort merge into one output_config")


async def test_timeout_raises_llmerror():
    async def hanging_send_request(client, kwargs, report, label=""):
        await asyncio.sleep(3600)  # a marathon web-search call

    llm._send_request = hanging_send_request
    config.settings.LLM_CALL_TIMEOUT_SECONDS = 0.2
    try:
        await llm.ask_json("SYSTEM", "user", label="contact:SlowCo")
        raise AssertionError("expected LLMError")
    except llm.LLMTimeoutError as exc:
        # The step label rides in the message — it becomes the company's
        # drop reason, so the report says WHICH call timed out.
        assert "timed out" in str(exc) and "contact:SlowCo" in str(exc), exc
        assert isinstance(exc, llm.LLMError)  # existing catch-sites still work
    print("PASS: a hung call raises a labeled LLMTimeoutError instead of running forever")


async def test_stall_watchdog():
    # Real _send_request, fake client whose stream yields one event and then
    # goes silent: the watchdog must abandon the call in ~LLM_STALL_TIMEOUT
    # seconds, not ride the (much larger) wall-clock ceiling, and surface as
    # the same labeled LLMTimeoutError the steps' timeout salvage retries on.
    llm._send_request = REAL_SEND_REQUEST
    config.settings.LLM_CALL_TIMEOUT_SECONDS = 60  # must NOT be what fires
    config.settings.LLM_STALL_TIMEOUT_SECONDS = 0.2

    class Event:
        type = "ping"

    class SilentStream:
        def __init__(self):
            self.sent = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.sent:
                self.sent = True
                return Event()
            await asyncio.sleep(3600)  # the stream wedges mid-response

        async def get_final_message(self):
            raise AssertionError("stream should have been abandoned")

    class FakeClient:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return SilentStream()

    real_get_client = llm.get_client
    llm.get_client = lambda: FakeClient()
    started = time.monotonic()
    try:
        try:
            await llm.ask_json("SYSTEM", "user", label="research:SilentCo")
            raise AssertionError("expected LLMTimeoutError")
        except llm.LLMTimeoutError as exc:
            assert "stalled" in str(exc) and "research:SilentCo" in str(exc), exc
        took = time.monotonic() - started
        assert took < 5, f"watchdog should fire in ~0.2s, took {took:.1f}s"
    finally:
        llm.get_client = real_get_client
    print("PASS: a silent stream is abandoned by the no-progress watchdog")


async def test_global_concurrency_cap():
    # LLM_MAX_CONCURRENT_CALLS throttles in-flight requests: with the cap at
    # 1, four concurrent ask_json calls must execute strictly one at a time.
    config.settings.LLM_CALL_TIMEOUT_SECONDS = 60
    config.settings.LLM_MAX_CONCURRENT_CALLS = 1
    state = {"active": 0, "max_active": 0}

    async def counting_send_request(client, kwargs, report, label=""):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return FakeMessage([FakeTextBlock('{"ok": true}')])

    llm._send_request = counting_send_request
    results = await asyncio.gather(*[llm.ask_json("S", "u") for _ in range(4)])
    assert all(r == {"ok": True} for r in results)
    assert state["max_active"] == 1, state
    print("PASS: the global cap holds concurrent LLM requests to the configured limit")


async def test_search_concurrency_cap():
    # Web-search-enabled calls take a second, tighter semaphore
    # (LLM_MAX_CONCURRENT_SEARCH_CALLS) so a run can't saturate the
    # org-level searches-per-minute limit; non-search calls (drafting,
    # revisions) are unaffected by it. With the search cap at 1 and the
    # general cap at 10, three search calls must serialize while three
    # plain calls still overlap.
    config.settings.LLM_CALL_TIMEOUT_SECONDS = 60
    config.settings.LLM_MAX_CONCURRENT_CALLS = 10
    config.settings.LLM_MAX_CONCURRENT_SEARCH_CALLS = 1
    state = {"search": 0, "max_search": 0, "plain": 0, "max_plain": 0}

    async def counting_send_request(client, kwargs, report, label=""):
        key = "search" if kwargs.get("tools") else "plain"
        state[key] += 1
        state[f"max_{key}"] = max(state[f"max_{key}"], state[key])
        await asyncio.sleep(0.02)
        state[key] -= 1
        return FakeMessage([FakeTextBlock('{"ok": true}')])

    llm._send_request = counting_send_request
    results = await asyncio.gather(
        *[llm.ask_json("S", "u", web_search=True) for _ in range(3)],
        *[llm.ask_json("S", "u") for _ in range(3)],
    )
    assert all(r == {"ok": True} for r in results)
    assert state["max_search"] == 1, state
    assert state["max_plain"] >= 2, state  # plain calls never queue on the search cap
    print("PASS: search calls honor the tighter search cap; plain calls are unaffected")


async def test_web_search_error_visibility():
    # Web-search failures (the org-level search rate limit included) arrive
    # IN-BAND: HTTP 200, with an error object where the result list would be.
    # The HTTP-429 backoff never sees them, so ask_json must surface them
    # itself — in the activity line and in the log — instead of reporting
    # "Reviewing search results…" over a failed search.

    class ErrorContent:
        error_code = "too_many_requests"

    class ErrorResultBlock:
        type = "web_search_tool_result"
        content = ErrorContent()

    class OkResultBlock:
        type = "web_search_tool_result"
        content = [{"type": "web_search_result", "url": "https://x"}]

    # The helper: list content = success, object/dict with error_code = error
    assert llm._web_search_error_code(OkResultBlock()) == ""
    assert llm._web_search_error_code(ErrorResultBlock()) == "too_many_requests"

    class DictErrorBlock:  # raw-dict shape, belt and suspenders
        type = "web_search_tool_result"
        content = {"type": "web_search_tool_result_error", "error_code": "unavailable"}

    assert llm._web_search_error_code(DictErrorBlock()) == "unavailable"
    print("PASS: _web_search_error_code separates error blocks from result lists")

    # End to end through the real _send_request: a stream that delivers an
    # errored search block must put the failure in the activity line, and the
    # call still completes normally with its text payload.
    class StartEvent:
        type = "content_block_start"
        content_block = ErrorResultBlock()

    final = FakeMessage([ErrorResultBlock(), FakeTextBlock('{"ok": true}')])

    class OneErrorStream:
        def __init__(self):
            self.events = [StartEvent()]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.events:
                return self.events.pop(0)
            raise StopAsyncIteration

        async def get_final_message(self):
            return final

    class FakeClient:
        class messages:
            @staticmethod
            def stream(**kwargs):
                return OneErrorStream()

    llm._send_request = REAL_SEND_REQUEST
    config.settings.LLM_CALL_TIMEOUT_SECONDS = 60
    config.settings.LLM_STALL_TIMEOUT_SECONDS = 60
    progress = []
    real_get_client = llm.get_client
    llm.get_client = lambda: FakeClient()
    try:
        result = await llm.ask_json(
            "S", "u", web_search=True, on_progress=progress.append,
            label="research:RateLimitedCo",
        )
    finally:
        llm.get_client = real_get_client
    assert result == {"ok": True}
    assert any("rate-limited" in p for p in progress), progress
    print("PASS: an in-band search rate limit reaches the activity line; call still completes")


asyncio.run(test_schema_sets_output_format())
asyncio.run(test_timeout_raises_llmerror())
asyncio.run(test_stall_watchdog())
asyncio.run(test_global_concurrency_cap())
asyncio.run(test_search_concurrency_cap())
asyncio.run(test_web_search_error_visibility())
