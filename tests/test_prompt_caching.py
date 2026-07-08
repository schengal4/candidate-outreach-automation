"""Verify ask_json builds the request with the new cache breakpoints:
system as a cache-controlled block, cache_prefix as its own leading block
ahead of the per-call user text, and that pause_turn continuation resends the
same structured content (not a bare string) -- also confirms the container_id
fix from an earlier session still works alongside this change."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.llm as llm


class FakeContainer:
    def __init__(self, cid):
        self.id = cid


class FakeUsage:
    input_tokens = 1
    output_tokens = 1
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class FakeMessage:
    def __init__(self, stop_reason, content, container=None):
        self.stop_reason = stop_reason
        self.content = content
        self.container = container
        self.usage = FakeUsage()


class FakeTextBlock:
    type = "text"
    def __init__(self, text):
        self.text = text


async def test_cache_prefix_shapes_the_request():
    captured_kwargs = []

    async def fake_send_request(client, kwargs, report):
        captured_kwargs.append(kwargs)
        return FakeMessage("end_turn", [FakeTextBlock('{"ok": true}')])

    llm._send_request = fake_send_request
    result = await llm.ask_json(
        "SYSTEM PROMPT", "per-company text", cache_prefix="shared resume text"
    )
    assert result == {"ok": True}

    kwargs = captured_kwargs[0]
    assert kwargs["system"] == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ], kwargs["system"]
    content = kwargs["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "shared resume text", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "per-company text"},
    ], content
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    print("PASS: system prompt and cache_prefix each get their own cache breakpoint")


async def test_no_cache_prefix_falls_back_to_plain_string():
    captured_kwargs = []

    async def fake_send_request(client, kwargs, report):
        captured_kwargs.append(kwargs)
        return FakeMessage("end_turn", [FakeTextBlock('{"ok": true}')])

    llm._send_request = fake_send_request
    await llm.ask_json("SYSTEM PROMPT", "just the user text")
    content = captured_kwargs[0]["messages"][0]["content"]
    assert content == "just the user text", content
    print("PASS: without cache_prefix, content stays a plain string (no behavior change)")


async def test_pause_turn_continuation_resends_structured_content_and_container():
    captured_kwargs = []
    calls = {"n": 0}

    async def fake_send_request(client, kwargs, report):
        captured_kwargs.append(kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeMessage("pause_turn", [FakeTextBlock("partial")], container=FakeContainer("cont_abc"))
        return FakeMessage("end_turn", [FakeTextBlock('{"ok": true}')], container=FakeContainer("cont_abc"))

    llm._send_request = fake_send_request
    result = await llm.ask_json(
        "SYSTEM PROMPT", "per-company text", cache_prefix="shared resume text"
    )
    assert result == {"ok": True}
    assert calls["n"] == 2, "expected exactly one continuation"

    # First request: no container yet.
    assert "container" not in captured_kwargs[0]
    # Continuation: container_id carried forward, and the resent user message
    # is the SAME structured (cache_prefix + user) content, not a bare string.
    second = captured_kwargs[1]
    assert second["container"] == "cont_abc"
    resent_user_content = second["messages"][0]["content"]
    assert resent_user_content == [
        {"type": "text", "text": "shared resume text", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "per-company text"},
    ], resent_user_content
    assert second["messages"][1]["role"] == "assistant"
    print("PASS: pause_turn continuation reuses the structured cache_prefix content and the container id")


asyncio.run(test_cache_prefix_shapes_the_request())
asyncio.run(test_no_cache_prefix_falls_back_to_plain_string())
asyncio.run(test_pause_turn_continuation_resends_structured_content_and_container())
