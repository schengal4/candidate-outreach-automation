"""ask_json's two new guards: `schema` rides in output_config.format (merged
with effort, without clobbering it), and a call that outlives
LLM_CALL_TIMEOUT_SECONDS raises LLMError instead of hanging until the
run-wide timeout kills the whole run."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.llm as llm
from app import config

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

    async def fake_send_request(client, kwargs, report):
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
    async def hanging_send_request(client, kwargs, report):
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


asyncio.run(test_schema_sets_output_format())
asyncio.run(test_timeout_raises_llmerror())
