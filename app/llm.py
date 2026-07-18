"""Async Anthropic client helpers.

All pipeline steps funnel through ask_json(): one Claude call (optionally with
the server-side web search tool), streamed to avoid HTTP timeouts, with
pause_turn continuation handling, rate-limit backoff, and robust JSON
extraction from the reply.
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from datetime import date
from typing import Any, Callable, List, Optional

import anthropic
import httpx

logger = logging.getLogger("app.llm")

from contextvars import ContextVar

from . import config

_client: Optional[anthropic.AsyncAnthropic] = None

# Per-run usage rollup. pipeline.run_discovery / run_pipeline set a fresh
# accumulator here; every ask_json below them adds each response's usage
# (child tasks inherit the contextvar, and context copies are shallow, so
# they all mutate the SAME dict the run reads), and the run's closing log
# line prints the totals — answering "what did run X cost, and which step ate
# it" used to mean hand-summing dozens of scattered per-call usage lines.
# Deliberately raw counts, not dollars: a hardcoded price table goes silently
# stale and breaks when ANTHROPIC_MODEL changes. Calls abandoned by the
# stall/timeout watchdog return no usage object, so totals are a floor.
usage_acc_var: ContextVar[Optional[dict]] = ContextVar("llm_usage_acc", default=None)


def new_usage_accumulator() -> dict:
    return {
        "calls": 0, "searches": 0, "search_errors": 0,
        "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
        # label prefix ("research:Stripe" -> "research") -> sub-totals, so
        # the rollup can say WHICH step ate the budget.
        "by_step": {},
    }


def format_usage(acc: dict) -> str:
    return (
        "calls=%d searches=%d search_errors=%d input=%d output=%d "
        "cache_write=%d cache_read=%d"
        % (
            acc["calls"], acc["searches"], acc["search_errors"],
            acc["input"], acc["output"], acc["cache_write"], acc["cache_read"],
        )
    )

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

# The classes above only cover failures on the HTTP handshake. When the same
# overload/rate-limit hits MID-STREAM, the response already returned 200 and
# the error arrives as an SSE `error` event — the SDK maps exceptions by
# response.status_code, so it raises a bare APIStatusError with the real type
# only in the body ({"error": {"type": "overloaded_error", ...}}). A real run
# died exactly this way: the retry loop never saw the overload because only
# the status-mapped classes were caught. "api_error" is the in-body type of a
# 500, mirroring InternalServerError in the tuple.
_TRANSIENT_ERROR_TYPES = {"overloaded_error", "rate_limit_error", "api_error"}


def _is_transient(exc: BaseException) -> bool:
    """True if the error is worth retrying with backoff (see RATE_LIMIT_RETRYABLE
    and _TRANSIENT_ERROR_TYPES)."""
    if isinstance(exc, RATE_LIMIT_RETRYABLE):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        error = exc.body.get("error") if isinstance(exc.body, dict) else None
        etype = error.get("type") if isinstance(error, dict) else None
        return etype in _TRANSIENT_ERROR_TYPES
    return False


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    return _client


# Global throttle on in-flight LLM requests (config.LLM_MAX_CONCURRENT_CALLS):
# queueing excess calls beats stampeding the tokens-per-minute window, where a
# mid-stream 429 discards the whole partial generation. Recreated when the
# event loop (tests run many asyncio.run loops) or the configured size changes.
_call_sem: Optional[asyncio.Semaphore] = None
_call_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_call_sem_size: int = 0


def _get_call_semaphore() -> asyncio.Semaphore:
    global _call_sem, _call_sem_loop, _call_sem_size
    loop = asyncio.get_running_loop()
    size = max(1, config.settings.LLM_MAX_CONCURRENT_CALLS)
    if _call_sem is None or _call_sem_loop is not loop or _call_sem_size != size:
        _call_sem = asyncio.Semaphore(size)
        _call_sem_loop = loop
        _call_sem_size = size
    return _call_sem


# Tighter throttle for calls that use web search (config
# LLM_MAX_CONCURRENT_SEARCH_CALLS): search failures are IN-BAND errors the
# HTTP backoff never sees (see _web_search_error_code), so the only defense
# against saturating the org's searches-per-minute limit is not to fire this
# many search loops at once. Non-search calls never take this semaphore.
_search_sem: Optional[asyncio.Semaphore] = None
_search_sem_loop: Optional[asyncio.AbstractEventLoop] = None
_search_sem_size: int = 0


def _get_search_semaphore() -> asyncio.Semaphore:
    global _search_sem, _search_sem_loop, _search_sem_size
    loop = asyncio.get_running_loop()
    size = max(1, config.settings.LLM_MAX_CONCURRENT_SEARCH_CALLS)
    if _search_sem is None or _search_sem_loop is not loop or _search_sem_size != size:
        _search_sem = asyncio.Semaphore(size)
        _search_sem_loop = loop
        _search_sem_size = size
    return _search_sem


class LLMError(Exception):
    pass


class LLMTimeoutError(LLMError):
    """The per-call safety valve fired (LLM_CALL_TIMEOUT_SECONDS or a
    step-specific override). Distinct from LLMError so callers can retry
    timeouts specifically — a timed-out call usually wandered (too many
    searches, marathon thinking) and often succeeds on a tighter budget,
    whereas refusals/truncation/parse failures wouldn't."""


class LLMStallError(LLMError):
    """Internal to this module: the response stream delivered no events for
    LLM_STALL_TIMEOUT_SECONDS. ask_json converts it to a labeled
    LLMTimeoutError, so callers only ever see that type (and their
    timeout-salvage retries apply to stalls too)."""


class LLMConnectionError(LLMError):
    """The request could not reach the API (network/DNS down) and
    connectivity did not come back within the outage-salvage window (see
    config CONNECTION_*). A subclass of LLMError so the pipeline drops the
    company with this clear reason instead of "unexpected error"."""


# Errors that mean the request never reached (or lost) the API — a network
# problem on this machine's side, not a model or rate-limit problem. The
# anthropic SDK wraps connect failures in APIConnectionError; raw httpx
# transport errors can still escape from mid-stream iteration.
CONNECTION_ERRORS = (anthropic.APIConnectionError, httpx.TransportError)


async def _wait_for_connectivity(report: Callable[[str], None], label: str = "") -> bool:
    """Block until DNS can resolve the API host again — the exact check the
    observed outage failed ("getaddrinfo failed") — probing every
    CONNECTION_PROBE_INTERVAL_SECONDS for up to CONNECTION_WAIT_SECONDS.
    Returns True when connectivity came back, False when the window expired.
    Deliberately called OUTSIDE the per-call timeout: waiting out an outage
    must not eat the budget of the request it is trying to save."""
    deadline = time.monotonic() + config.settings.CONNECTION_WAIT_SECONDS
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.getaddrinfo("api.anthropic.com", 443)
            return True
        except OSError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "%s: network did not come back within %ds",
                label or "call", config.settings.CONNECTION_WAIT_SECONDS,
            )
            return False
        report("Network connection lost — waiting for it to come back…")
        await asyncio.sleep(
            min(config.settings.CONNECTION_PROBE_INTERVAL_SECONDS, remaining)
        )


def _web_search_error_code(block: Any) -> str:
    """The error_code of a failed web_search_tool_result block, "" on success.

    Web-search failures — including the org-level searches-per-minute rate
    limit — never surface as HTTP errors: the API returns 200 and puts an
    error object where the result list would be (`content` is a LIST of
    results on success, a single object with `error_code` on failure). The
    rate-limit backoff in _send_request therefore never sees them; without
    this check they were completely invisible outside the model's own
    thinking, which made search-starved runs undiagnosable from the log.
    Error codes: too_many_requests (rate limit), max_uses_exceeded (budget
    spent — expected, the prompt tells the model to wrap up), unavailable,
    query_too_long, invalid_tool_input, request_too_large."""
    content = getattr(block, "content", None)
    if isinstance(content, list) or content is None:
        return ""
    code = getattr(content, "error_code", None)
    if code is None and isinstance(content, dict):
        code = content.get("error_code")
    return str(code or "")


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
    # 3. scan for complete JSON values anywhere in the text. raw_decode reads
    #    one value and reports where it ended, so unlike a first-{-to-last-}
    #    slice this also survives a false start concatenated with the real
    #    payload (a real run's research reply was
    #    '{"summary": "hold","items":[],"red_flags":[]}{...actual research...}')
    #    — keep the meatiest value, not the first.
    decoder = json.JSONDecoder()
    values = []
    pos = 0
    while True:
        opener = re.search(r"[\[{]", text[pos:])
        if not opener:
            break
        start = pos + opener.start()
        try:
            value, end = decoder.raw_decode(text, start)
            values.append(value)
            pos = end
        except ValueError:
            pos = start + 1
    if values:
        return max(values, key=lambda v: len(json.dumps(v)))
    raise LLMError(f"Could not parse JSON from model reply: {text[:400]!r}")


async def _send_request(
    client: anthropic.AsyncAnthropic,
    kwargs: dict,
    report: Callable[[str], None],
    label: str = "",
):
    """Run one streamed request, retrying on rate-limit / overload responses
    with exponential backoff (+ jitter), honoring the server's Retry-After
    header when present."""
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            thinking_buffer = ""
            async with client.messages.stream(**kwargs) as stream:
                events = stream.__aiter__()
                while True:
                    # No-progress watchdog: a healthy stream stays chatty
                    # (thinking deltas, block starts, tool results arrive
                    # seconds apart, even mid-web-search). Total silence for
                    # LLM_STALL_TIMEOUT_SECONDS means the request is wedged —
                    # abandon it now so the step's timeout salvage (retry on
                    # a smaller budget) runs, instead of riding the full
                    # wall-clock ceiling: a real run burned 2x480s on one
                    # company's stalled research calls.
                    try:
                        event = await asyncio.wait_for(
                            events.__anext__(),
                            timeout=config.settings.LLM_STALL_TIMEOUT_SECONDS,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise LLMStallError(
                            f"no stream events for "
                            f"{config.settings.LLM_STALL_TIMEOUT_SECONDS}s"
                        ) from None
                    try:
                        if event.type == "content_block_start":
                            block_type = event.content_block.type
                            if block_type == "server_tool_use":
                                report("Searching the web…")
                            elif block_type == "web_search_tool_result":
                                error_code = _web_search_error_code(event.content_block)
                                if error_code:
                                    # In-band failure (see _web_search_error_code)
                                    # — log it so `grep "web search failed"`
                                    # gives real counts per run, and say so in
                                    # the activity line instead of the
                                    # misleading "Reviewing search results…".
                                    logger.warning(
                                        "%s: web search failed (%s)",
                                        label or "call", error_code,
                                    )
                                    report(
                                        "A web search was rate-limited — continuing…"
                                        if error_code == "too_many_requests"
                                        else f"A web search failed ({error_code}) — continuing…"
                                    )
                                else:
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
        except anthropic.APIStatusError as exc:
            if not _is_transient(exc) or attempt == RATE_LIMIT_MAX_RETRIES:
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
            # Log it too: this backoff used to be invisible outside the UI
            # spinner, which made 429-driven slowness undiagnosable from the
            # log file (the delay just disappeared into the call's took=).
            # A 429's anthropic-ratelimit-* headers name WHICH bucket bound
            # (requests vs input tokens vs output tokens) — the limits are
            # per-minute token buckets with continuous refill, so bursts and
            # sustained overconsumption both surface here; the headers tell
            # them apart (bursts exhaust "requests", sustained load exhausts
            # a token bucket).
            buckets = ""
            headers = getattr(getattr(exc, "response", None), "headers", None)
            if headers:
                parts = []
                for h in (
                    "anthropic-ratelimit-requests-remaining",
                    "anthropic-ratelimit-input-tokens-remaining",
                    "anthropic-ratelimit-output-tokens-remaining",
                    "anthropic-ratelimit-tokens-remaining",
                ):
                    value = headers.get(h)
                    if value is not None:
                        name = h.removeprefix("anthropic-ratelimit-").removesuffix("-remaining")
                        parts.append(f"{name}={value}")
                if parts:
                    buckets = " [" + " ".join(parts) + "]"
            logger.warning(
                "%s: %s — retry %d/%d in %.0fs%s",
                label or "call", type(exc).__name__,
                attempt + 1, RATE_LIMIT_MAX_RETRIES, delay, buckets,
            )
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
    # Connection-outage retries are counted per ask_json call (not per
    # pause_turn continuation) so a flapping network can't multiply them.
    conn_retries = 0

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
        while True:
            try:
                # In-flight caps: queue here — with the timeout clock NOT
                # running — rather than stampede the rate limiter. Search calls
                # take the (tighter) search semaphore FIRST, then the general
                # one, so a call queued for a search slot doesn't sit on a
                # general slot it isn't using; same acquire order everywhere,
                # so no deadlock. See config LLM_MAX_CONCURRENT_CALLS /
                # LLM_MAX_CONCURRENT_SEARCH_CALLS.
                sem = _get_call_semaphore()
                search_sem = _get_search_semaphore() if web_search else None
                if search_sem is not None and search_sem.locked():
                    report("Waiting for a free web-search slot…")
                elif sem.locked():
                    report("Waiting for a free request slot…")
                queue_started = time.monotonic()
                async with contextlib.AsyncExitStack() as stack:
                    if search_sem is not None:
                        await stack.enter_async_context(search_sem)
                    await stack.enter_async_context(sem)
                    # Queue time is otherwise invisible: it doesn't count
                    # against the call timeout and only showed in the UI
                    # spinner, so a run slowed by slot contention looked
                    # identical in the log to one slowed by slow calls.
                    # Long waits escalate to INFO.
                    waited = time.monotonic() - queue_started
                    if waited >= 1.0:
                        logger.log(
                            logging.INFO if waited >= 30 else logging.DEBUG,
                            "%s: waited %.0fs for a request slot",
                            label or "call", waited,
                        )
                    attempt_started = time.monotonic()
                    msg = await asyncio.wait_for(
                        _send_request(client, kwargs, report, label), timeout=call_timeout
                    )
                break
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
            except LLMStallError as exc:
                logger.warning(
                    "%s attempt %d: stream stalled after %.0fs (%s) — request abandoned",
                    label or "call", attempt + 1, time.monotonic() - attempt_started, exc,
                )
                # Same type as a wall-clock timeout so the steps' timeout salvage
                # (one retry on a smaller budget) applies to stalls too.
                raise LLMTimeoutError(
                    f"LLM call stalled ({exc})" + (f" ({label})" if label else "") + "."
                )
            except CONNECTION_ERRORS as exc:
                # The request never reached (or lost) the API — a local
                # network/DNS outage, not a model problem. A real outage
                # dropped every in-flight company at once as "unexpected
                # error". Wait out the outage (outside the per-call timeout
                # clock, and without holding a request slot) and retry the
                # whole request, up to the configured limit.
                conn_retries += 1
                if conn_retries > config.settings.CONNECTION_ERROR_MAX_RETRIES:
                    raise LLMConnectionError(
                        "Could not reach the Anthropic API after "
                        f"{config.settings.CONNECTION_ERROR_MAX_RETRIES} "
                        "connection retries"
                        + (f" ({label})" if label else "")
                        + " — check your internet connection."
                    )
                logger.warning(
                    "%s attempt %d: connection error (%s: %s) — waiting for "
                    "the network, retry %d/%d",
                    label or "call", attempt + 1, type(exc).__name__, exc,
                    conn_retries, config.settings.CONNECTION_ERROR_MAX_RETRIES,
                )
                if not await _wait_for_connectivity(report, label):
                    raise LLMConnectionError(
                        "Network connection to the Anthropic API was lost and "
                        "did not come back within "
                        f"{config.settings.CONNECTION_WAIT_SECONDS}s"
                        + (f" ({label})" if label else "")
                        + " — check your internet connection."
                    )
                # DNS resolves again, but give the connection a moment to
                # settle — an interface that just came up can still drop the
                # first request, and a hot retry loop would burn the budget.
                await asyncio.sleep(
                    min(2.0, config.settings.CONNECTION_PROBE_INTERVAL_SECONDS)
                )
                report("Connection restored — retrying…")

        if msg.container:
            container_id = msg.container.id

        u = msg.usage
        # Web searches happen server-side inside the request; the count is the
        # main driver of a call's duration (each search is a think+read round).
        server_tools = getattr(u, "server_tool_use", None)
        searches = getattr(server_tools, "web_search_requests", 0) or 0
        # Failed searches (rate limits included) arrive in-band as error
        # blocks, not HTTP errors — count them here so a search-starved call
        # is distinguishable from a slow one in the log.
        search_errors = sum(
            1 for b in msg.content
            if getattr(b, "type", "") == "web_search_tool_result"
            and _web_search_error_code(b)
        )
        logger.info(
            "%s attempt %d: stop=%s took=%.0fs searches=%d search_errors=%d input=%d output=%d cache_write=%d cache_read=%d",
            label or "call", attempt + 1, msg.stop_reason,
            time.monotonic() - attempt_started, searches, search_errors,
            u.input_tokens, u.output_tokens,
            u.cache_creation_input_tokens, u.cache_read_input_tokens,
        )
        acc = usage_acc_var.get()
        if acc is not None:
            acc["calls"] += 1
            acc["searches"] += searches
            acc["search_errors"] += search_errors
            acc["input"] += u.input_tokens
            acc["output"] += u.output_tokens
            acc["cache_write"] += u.cache_creation_input_tokens
            acc["cache_read"] += u.cache_read_input_tokens
            step = acc["by_step"].setdefault(
                (label or "call").split(":", 1)[0],
                {"calls": 0, "searches": 0, "input": 0, "output": 0},
            )
            step["calls"] += 1
            step["searches"] += searches
            step["input"] += u.input_tokens
            step["output"] += u.output_tokens

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
