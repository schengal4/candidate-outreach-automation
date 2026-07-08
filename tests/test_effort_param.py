"""Verify the effort parameter only shows up as output_config when explicitly
passed, and that it's absent by default (contact/research/discovery keep the
"high" default; only draft_email's call sets effort="medium")."""
import asyncio
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import app.llm as llm


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


async def test_effort_omitted_by_default():
    captured = []

    async def fake_send_request(client, kwargs, report):
        captured.append(kwargs)
        return FakeMessage([FakeTextBlock('{"ok": true}')])

    llm._send_request = fake_send_request
    await llm.ask_json("SYSTEM", "user text")
    assert "output_config" not in captured[0], captured[0]
    print("PASS: no effort override -> output_config absent (default 'high' applies)")


async def test_effort_medium_sets_output_config():
    captured = []

    async def fake_send_request(client, kwargs, report):
        captured.append(kwargs)
        return FakeMessage([FakeTextBlock('{"ok": true}')])

    llm._send_request = fake_send_request
    await llm.ask_json("SYSTEM", "user text", effort="medium")
    assert captured[0]["output_config"] == {"effort": "medium"}, captured[0]
    print("PASS: effort='medium' sets output_config={'effort': 'medium'}")


asyncio.run(test_effort_omitted_by_default())
asyncio.run(test_effort_medium_sets_output_config())
