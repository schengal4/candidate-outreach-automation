"""Async Anthropic client helpers.

All pipeline steps funnel through ask_json(): one Claude call (optionally with
the server-side web search tool), streamed to avoid HTTP timeouts, with
pause_turn continuation handling, rate-limit backoff, and robust JSON
extraction from the reply.
"""

import asyncio
import json
import logging
import random
import re
import time
from datetime import date
from typing import Any, Callable, List, Optional

import anthropic

logger = logging.getLogger("app.llm")

from . import config

_client: Optional[anthropic.AsyncAnthropic] = None

# Every company in a run fires its LLM calls concurrently (see pipeline.py --
# there's no artificial cap), so bursts of simultaneous requests are expected.
# These retries absorb 429/529/5xx responses from that burst instead of
# dropping the company. Separate from the SDK's own built-in retries (which
# cover a couple of quick transient failures) -- this is for sustained
# rate-limit pressure from running every company at once.
RATE_LIMIT_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
    anthropic.InternalServerError,
)
RATE_LIMIT_MAX_RETRIES = 6
RATE_LIMIT_BASE_DELAY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 60.0


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


class LLMError(Exception):
    pass


class LLMTimeoutError(LLMError):
    """The per-call safety valve fired (LLM_CALL_TIMEOUT_SECONDS or a
    step-specific override). Distinct from LLMError so callers can retry
    timeouts specifically — a timed-out call usually wandered (too many
    searches, marathon thinking) and often succeeds on a tighter budget,
    whereas refusals/truncation/parse failures wouldn't."""


def extract_json(text: str) -> Any:
    """Pull the JSON object/array out of a model reply, tolerating prose around it."""
    # 1. fenced code block
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    # 2. whole reply
    try:
        return json.loads(text)
    except ValueError:
        pass
    # 3. outermost braces / brackets
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise LLMError(f"Could not parse JSON from model reply: {text[:400]!r}")


async def _send_request(client: anthropic.AsyncAnthropic, kwargs: dict, report: Callable[[str], None]):
    """Run one streamed request, retrying on rate-limit / overload responses
    with exponential backoff (+ jitter), honoring the server's Retry-After
    header when present."""
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            thinking_buffer = ""
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    try:
                        if event.type == "content_block_start":
                            block_type = event.content_block.type
                            if block_type == "server_tool_use":
                                report("Searching the web…")
                            elif block_type == "web_search_tool_result":
                                report("Reviewing search results…")
                            elif block_type == "thinking":
                                thinking_buffer = ""
                            elif block_type == "text":
                                report("Writing the response…")
                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "thinking_delta" and delta.thinking:
                                thinking_buffer += delta.thinking
                                last_line = thinking_buffer.strip().splitlines()[-1] if thinking_buffer.strip() else ""
                                if last_line:
                                    report(last_line[-140:])
                    except Exception:
                        pass  # never let progress parsing break the actual call
                return await stream.get_final_message()
        except RATE_LIMIT_RETRYABLE as exc:
            if attempt == RATE_LIMIT_MAX_RETRIES:
                raise
            retry_after = None
            try:
                retry_after = float(exc.response.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                pass
            delay = retry_after if retry_after is not None else min(
                RATE_LIMIT_BASE_DELAY_SECONDS * (2**attempt), RATE_LIMIT_MAX_DELAY_SECONDS
            )
            delay += random.uniform(0, delay * 0.25)  # jitter, avoid a thundering herd on retry
            report(f"Rate limited — retrying in {delay:.0f}s…")
            await asyncio.sleep(delay)


async def ask_json(
    system: str,
    user: str,
    web_search: bool = False,
    max_tokens: Optional[int] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    label: str = "",
    cache_prefix: Optional[str] = None,
    effort: Optional[str] = None,
    schema: Optional[dict] = None,
    max_uses: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
) -> Any:
    """One Claude request; returns the parsed JSON payload of the reply.

    If on_progress is given, it's called with short status strings as the
    response streams in (thinking snippets, "searching the web...", etc.) so
    a caller can show live progress instead of a static spinner during the
    minutes a web-search-heavy call can take.

    `label` is just a tag for the console usage log (e.g. "research:Stripe")
    so concurrent calls are distinguishable in the terminal.

    `cache_prefix`, if given, is content that's identical across many calls of
    this *type* within a run (e.g. the candidate's resume, reused on every
    company's draft call) — it gets its own cache breakpoint ahead of `user`
    so repeat calls read it at ~10% of input price instead of full price.

    `effort`, if given, overrides the default "high" thinking/token budget
    (low|medium|high|xhigh|max). Only worth lowering for steps where less
    reasoning depth doesn't risk the substance of the output — e.g. drafting,
    which is rule-bound rather than open-ended. Leave unset (default "high")
    for anything where depth matters, like verifying employment or gathering
    personalization research.

    `schema`, if given, is a JSON Schema enforced server-side via structured
    outputs (output_config.format) — the reply text is then guaranteed valid
    JSON matching it. Without it, replies occasionally carry an unescaped
    quote or raw newline inside a JSON string and the parse below fails after
    all the expensive upstream work has already been paid for.

    `max_uses` / `timeout_seconds` override the default web-search budget and
    per-call time limit — used to keep contact-identification calls (which
    reliably spend their entire budget) on a shorter leash than research.
    """
    client = get_client()
    call_timeout = timeout_seconds or config.settings.LLM_CALL_TIMEOUT_SECONDS
    if max_tokens is None:
        max_tokens = config.settings.LLM_MAX_TOKENS
    # Anchor "now": without this the model judges words like "recent" against
    # its training data — a real run framed a 16-month-old funding round as
    # news. Constant within a run, so system-prompt caching is unaffected.
    system = f"Today's date: {date.today().isoformat()}.\n\n{system}"
    tools: List[dict] = []
    if web_search:
        tools.append(
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": max_uses or config.settings.WEB_SEARCH_MAX_USES,
            }
        )

    def report(text: str) -> None:
        if on_progress:
            try:
                on_progress(text)
            except Exception:
                pass  # progress reporting must never break the actual call

    if cache_prefix:
        content: Any = [
            {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user},
        ]
    else:
        content = user

    messages: List[dict] = [{"role": "user", "content": content}]
    container_id: Optional[str] = None

    for attempt in range(config.settings.MAX_PAUSE_TURN_CONTINUATIONS + 1):
        kwargs: dict = dict(
            model=config.settings.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            # system/tools text is identical across every call of this type in
            # a run (it never varies per company) — its own breakpoint here
            # caches it (plus the tool definition, which renders just before
            # it) so only the first call of each type in a run pays full
            # price; every later one reads it at cache price.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive", "display": "summarized"},
            messages=messages,
            # On top of the explicit breakpoints above, auto-caches the last
            # block of THIS request too. On a pause_turn continuation the
            # resent prefix matches what was cached last time, so only the
            # newly-generated part is billed at full price.
            cache_control={"type": "ephemeral"},
        )
        if tools:
            kwargs["tools"] = tools
        output_config: dict = {}
        if effort:
            output_config["effort"] = effort
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config
        if container_id:
            # A pause_turn can leave pending tool use from a server-side code
            # execution container; the continuation must reuse the same
            # container or the API rejects it ("container_id is required...").
            kwargs["container"] = container_id

        # Hard per-call time limit (covers the stream and its rate-limit
        # retries). Web-search calls on hard companies have run 20+ minutes
        # as one request — better to drop that company with a clear reason
        # than let it ride until the run-wide timeout kills the whole run.
        attempt_started = time.monotonic()
        try:
            msg = await asyncio.wait_for(
                _send_request(client, kwargs, report), timeout=call_timeout
            )
        except asyncio.TimeoutError:
            # Without this line, the slowest calls (the ones that matter most
            # for speed diagnosis) would leave no usage/timing trace at all.
            logger.warning(
                "%s attempt %d: timed out after %.0fs (request abandoned mid-stream)",
                label or "call", attempt + 1, time.monotonic() - attempt_started,
            )
            # Name the step in the message — it becomes the company's drop
            # reason in the run report, and "LLM call timed out" alone made
            # real runs undiagnosable without the log file.
            raise LLMTimeoutError(
                f"LLM call timed out after {call_timeout // 60} minutes"
                + (f" ({label})" if label else "") + "."
            )

        if msg.container:
            container_id = msg.container.id

        u = msg.usage
        # Web searches happen server-side inside the request; the count is the
        # main driver of a call's duration (each search is a think+read round).
        server_tools = getattr(u, "server_tool_use", None)
        searches = getattr(server_tools, "web_search_requests", 0) or 0
        logger.info(
            "%s attempt %d: stop=%s took=%.0fs searches=%d input=%d output=%d cache_write=%d cache_read=%d",
            label or "call", attempt + 1, msg.stop_reason,
            time.monotonic() - attempt_started, searches,
            u.input_tokens, u.output_tokens,
            u.cache_creation_input_tokens, u.cache_read_input_tokens,
        )

        if msg.stop_reason == "refusal":
            raise LLMError("The model declined this request (safety refusal).")
        if msg.stop_reason == "pause_turn":
            # Server-side tool loop paused — resend to let it resume.
            report("Continuing…")
            messages = [
                {"role": "user", "content": content},
                {"role": "assistant", "content": msg.content},
            ]
            continue
        if msg.stop_reason == "max_tokens":
            raise LLMError("Model output was truncated (max_tokens). Try again.")

        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return extract_json(text)
        except LLMError:
            # The LLMError message truncates the reply at 400 chars — keep the
            # full text in the log so parse failures are actually diagnosable.
            logger.warning("%s: unparseable model reply, full text:\n%s", label or "call", text)
            raise

    raise LLMError("Model did not finish within the pause_turn continuation limit.")
