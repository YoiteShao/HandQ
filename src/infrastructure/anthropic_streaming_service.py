"""
Anthropic Streaming LLM Service
================================
Implements :class:`src.infrastructure.llm_service.LLMService` using
``anthropic.AsyncAnthropic`` with streaming **always enabled**.

Why streaming by default
------------------------
When the model issues tool calls, each tool's arguments are delivered via
``content_block_stop`` events as soon as that block is complete — before the
full response stream ends.  This means:

  • Tool N's arguments are available the moment its ``content_block_stop``
    fires, even while the model is still generating tool N+1.
  • The caller (RuntimeAgent / act()) can start executing tool N immediately
    rather than waiting for all tools to be described.

This mirrors the ``StreamingToolExecutor`` pattern from claude-code:
  isConcurrencySafe=True  → start executing as soon as the block stops
  isConcurrencySafe=False → queue until safe to run

Interface compatibility
-----------------------
``chat()`` still returns ``LLMChatResult`` (same as every other LLMService
adapter) so RuntimeAgent / call_with_fallback need zero changes for the
non-streaming use-case.

For early-dispatch streaming, callers can pass ``stream=True`` to receive an
``AsyncGenerator[StreamToolCallEvent, None]`` that yields one event per
completed tool-call block, then a final ``StreamDoneEvent``.

Usage
-----
    from src.infrastructure.anthropic_streaming_service import AnthropicStreamingService

    svc = AnthropicStreamingService(
        api_key="...",
        base_url="https://qgenie-api.qualcomm.com/",
        model="anthropic::claude-4-5-haiku",
    )

    # Standard (non-streaming) — identical to every other LLMService adapter:
    result: LLMChatResult = await svc.chat(messages=..., tools=...)

    # Streaming early-dispatch — yields tool calls as they complete:
    async for event in await svc.chat(messages=..., tools=..., stream=True):
        if isinstance(event, StreamToolCallEvent):
            # tool call is ready — can start executing immediately
            ...
        elif isinstance(event, StreamDoneEvent):
            result = event.result  # final LLMChatResult
"""
import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Literal, Optional, Union

import anthropic
import httpx

from .llm_service import LLMChatResult, LLMService, ToolCallInfo
from .logger import get_logger
from ..models.token_usage import TokenUsage


# ---------------------------------------------------------------------------
# Streaming event types
# ---------------------------------------------------------------------------

@dataclass
class StreamToolCallEvent:
    """Emitted when a single tool-call block completes (content_block_stop).

    The tool's ``name`` and ``args`` are fully parsed and ready to execute.
    ``call_id`` matches the id from the API so it can be paired with the
    tool-result message later.
    """
    call_id: str
    tool_name: str
    args: dict
    block_index: int  # position in the response (0-based)


@dataclass
class StreamTextDeltaEvent:
    """Emitted for each text delta chunk (content_block_delta, type=text_delta)."""
    text: str


@dataclass
class StreamDoneEvent:
    """Emitted once after the stream ends.  ``result`` is the full LLMChatResult."""
    result: LLMChatResult


# ---------------------------------------------------------------------------
# Per-model output token ceilings (Anthropic API requires an explicit value)
# Keys are substrings matched against the model name (case-insensitive).
# The first matching entry wins; the fallback is used when nothing matches.
# ---------------------------------------------------------------------------
_MODEL_MAX_OUTPUT_TOKENS: list[tuple[str, int]] = [
    # ── 1M-context variants ──────────────────────────────────────────────
    # Sonnet at 1M context still caps output at 64K (only Opus 1M reaches 128K).
    # These MUST come before the generic ":1m" rule below.
    ("claude-4-6-sonnet:1m", 64000),
    ("claude-4-5-sonnet:1m", 64000),
    ("claude-sonnet-4:1m",   64000),  # legacy Anthropic naming
    # Generic 1M-context suffix → assume 128K output (Opus 1M variants)
    (":1m",                  128000),
    ("[1m]",                 128000),

    # ── Native 1M Opus (4.7+) ────────────────────────────────────────────
    ("claude-4-8-opus",      128000),
    ("claude-opus-4-8",      128000),  # legacy Anthropic naming
    ("claude-4-7-opus",      128000),
    ("claude-opus-4-7",      128000),  # legacy Anthropic naming

    # ── Opus 4.x → 32K output ────────────────────────────────────────────
    ("claude-4-5-opus",       32000),
    ("claude-4-opus",         32000),
    ("claude-opus-4",         32000),  # legacy Anthropic naming

    # ── Sonnet 4.x → 64K output ──────────────────────────────────────────
    ("claude-4-6-sonnet",     64000),
    ("claude-4-5-sonnet",     64000),
    ("claude-sonnet-4",       64000),  # legacy Anthropic naming (4.5+ direct API)

    # ── Haiku 4.5 → 16K output ───────────────────────────────────────────
    ("claude-4-5-haiku",      16000),
    ("claude-haiku-4",        16000),  # legacy Anthropic naming (4.5+ direct API)
]
_DEFAULT_MAX_OUTPUT_TOKENS = 64000  # safe large default for unknown models


# ---------------------------------------------------------------------------
# Per-model context window sizes
# Keys are substrings matched against the model name (case-insensitive).
# The first matching entry wins; the fallback is used when nothing matches.
# IMPORTANT: more-specific substrings (e.g. "[1m]") must come before
# broader ones (e.g. "claude-sonnet-4") so they are matched first.
# ---------------------------------------------------------------------------
_MODEL_CONTEXT_WINDOW: list[tuple[str, int]] = [
    # 1M-context variants — identified by the ":1m"/":1M" or "[1m]"/"[1M]" suffix
    (":1m",              1_000_000),
    (":1M",              1_000_000),
    ("[1m]",             1_000_000),
    ("[1M]",             1_000_000),
    # claude-4-7-opus and claude-4-8-opus have native 1M context window.
    # The legacy "claude-opus-4-N" variants must be matched BEFORE the
    # generic "claude-opus-4" rule below (200K) to avoid mis-resolution.
    ("claude-4-8-opus",  1_000_000),
    ("claude-opus-4-8",  1_000_000),
    ("claude-4-7-opus",  1_000_000),
    ("claude-opus-4-7",  1_000_000),
    # claude-4 series
    ("claude-opus-4",      200_000),
    ("claude-sonnet-4",    200_000),
    ("claude-haiku-4",     200_000),
]
_DEFAULT_CONTEXT_WINDOW = 200_000  # conservative default for unknown models


def _resolve_max_tokens(
    model: str,
    requested: Optional[int],
    thinking_budget_tokens: Optional[int] = None,
) -> int:
    """Return the effective max_tokens for an Anthropic API call.

    Resolution rules:
      * The per-model ceiling from ``_MODEL_MAX_OUTPUT_TOKENS`` is always
        looked up first (fallback ``_DEFAULT_MAX_OUTPUT_TOKENS`` for
        unknown models).
      * If *requested* is ``None`` or non-positive, the ceiling is used —
        this prevents legacy default values (e.g. an old ``4096``) from
        capping modern models far below their real capacity.
      * If *requested* is positive, it is clamped to the ceiling so the
        API never receives a value the model cannot satisfy (would be a
        400) while still letting callers (vision, reranker, triage, etc.)
        deliberately throttle output for short-response use cases.
      * Anthropic requires ``max_tokens`` to strictly exceed
        ``thinking.budget_tokens``. If a caller combines a small explicit
        *requested* with a thinking budget that would violate this (latent
        today — no current caller passes both — but reachable via the
        public ``chat_stream`` interface), the result is raised to clear
        the budget with headroom, still capped at the model's ceiling.
    """
    model_lower = model.lower()
    ceiling = _DEFAULT_MAX_OUTPUT_TOKENS
    for substring, c in _MODEL_MAX_OUTPUT_TOKENS:
        if substring in model_lower:
            ceiling = c
            break
    resolved = ceiling if requested is None or requested <= 0 else min(requested, ceiling)
    if thinking_budget_tokens and resolved <= thinking_budget_tokens:
        resolved = min(ceiling, thinking_budget_tokens + 1024)
    return resolved


def _resolve_context_window(model: str, override: Optional[int]) -> int:
    """Return the context window size for *model*.

    If *override* is provided and positive, it is returned as-is (allows the
    caller to set an explicit value, e.g. for a custom deployment).
    Otherwise the per-model size from ``_MODEL_CONTEXT_WINDOW`` is used,
    falling back to ``_DEFAULT_CONTEXT_WINDOW`` for unknown models.
    """
    if override is not None and override > 0:
        return override
    model_lower = model.lower()
    for substring, size in _MODEL_CONTEXT_WINDOW:
        if substring in model_lower:
            return size
    return _DEFAULT_CONTEXT_WINDOW


_DEFAULT_THINKING_BUDGET_TOKENS = 4096


# Rate-limit detail extraction.
#
# The QGenie/Bedrock gateway does NOT send an HTTP ``Retry-After`` header on a
# 429. It puts the wait inside the JSON body, as a STRING WITH A UNIT SUFFIX:
#
#   {'error': {'code': 'RATE_LIMIT_EXCEEDED',
#              'message': 'Rate limit exceeded for model: anthropic::claude-4-6-sonnet',
#              'details': {'retry_after': '53881s', 'limit_type': 'tokens_per_day'}}}
#
# Reading headers only meant ``retry_after_secs`` stayed 0.0 forever, the
# ``> 300s`` test never fired, and ``_exhausted`` was never set. Observed
# 2026-08-03: 34/34 throttle log lines printed "retry_after=0s" while the body
# they quoted on the SAME LINE said '53881s' — a ~15h tokens_per_day limit. The
# pool then re-walked the two dead hops on all 16 following turns.
#
# Note ``float('53881s')`` also raises, so even a header carrying the gateway's
# format would have been dropped. Strip to digits before converting.
_RETRY_AFTER_RE = re.compile(r"['\"]retry_after['\"]\s*:\s*['\"]?([0-9]+(?:\.[0-9]+)?)")
_LIMIT_TYPE_RE = re.compile(r"['\"]limit_type['\"]\s*:\s*['\"]([a-zA-Z_]+)['\"]")

# Limit types that are per-DAY (or longer) budgets rather than burst throttles.
# These justify marking a service exhausted for the rest of the session
# regardless of the parsed seconds — the number is advisory, the class is not.
_LONG_WINDOW_LIMIT_TYPES: frozenset = frozenset({
    "tokens_per_day", "requests_per_day", "tokens_per_month",
})


def _coerce_seconds(raw: Any) -> float:
    """Parse a retry-after value that may carry a unit suffix ('53881s')."""
    if raw is None:
        return 0.0
    digits = re.sub(r"[^0-9.]", "", str(raw))
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def _parse_rate_limit_details(error: Exception) -> tuple[float, str]:
    """Return ``(retry_after_seconds, limit_type)`` for a 429.

    Tries, in order: the HTTP ``Retry-After`` header (spec-compliant servers),
    the structured JSON body (``error.details``), then a regex over ``str(e)``
    as the last resort. Any layer may be missing; ``(0.0, "")`` means nothing
    was parseable.
    """
    # (1) Header — standards-compliant path.
    try:
        hdrs = getattr(getattr(error, "response", None), "headers", None)
        if hdrs is not None:
            ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
            if ra is not None:
                secs = _coerce_seconds(ra)
                if secs > 0:
                    return secs, ""
    except (TypeError, ValueError, AttributeError):
        pass

    # (2) Structured body — what the QGenie gateway actually sends.
    secs = 0.0
    limit_type = ""
    try:
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            details = ((body.get("error") or {}).get("details")) or {}
            if isinstance(details, dict):
                secs = _coerce_seconds(details.get("retry_after"))
                limit_type = str(details.get("limit_type") or "")
    except (TypeError, AttributeError):
        pass
    if secs > 0 or limit_type:
        return secs, limit_type

    # (3) Regex over the stringified error — survives arbitrary wrapping.
    text = str(error)
    m = _RETRY_AFTER_RE.search(text)
    if m:
        secs = _coerce_seconds(m.group(1))
    m = _LIMIT_TYPE_RE.search(text)
    if m:
        limit_type = m.group(1)
    return secs, limit_type


def _model_supports_extended_thinking(model: str) -> bool:
    """True when *model*'s configured name opts into extended thinking.

    Mirrors the existing ``:1m``-suffix convention (a model-name marker the
    Settings UI lets the user pick per entry) — a model isn't assumed to
    support the ``thinking`` API param unless its name carries ``:thinking``.
    The model string itself is never mutated; this is a local capability
    signal only, resolved before the raw model id is sent to the API.
    """
    return ":thinking" in model.lower()


# ---------------------------------------------------------------------------
# output_config.effort — per-model accepted values
#
# The gateway itself does NOT validate `output_config.effort` (a garbage
# value like "banana" is silently forwarded and returns 200). The real
# enforcement lives in Bedrock's model-parameter validation downstream,
# and that layer has been observed to flip its position:
#
#   * 2026-07-16 — claude-4-6-sonnet (both plain and ":1M") REJECTED
#     "xhigh" with a hard 400:
#       ValidationException: output_config.effort: Input should be
#       'low', 'medium', 'high' or 'max'
#     — matching Anthropic's public docs (xhigh introduced with Opus 4.7;
#     Sonnet 4.6 and earlier only ever had low/medium/high/max).
#   * 2026-07-17 — same request, 3× per variant, both streaming and
#     non-streaming: all 12 returned 200 end_turn. Something in the
#     gateway or Bedrock backend loosened validation in the intervening
#     day. Cause unknown.
#
# The clamping table below is KEPT DESPITE the current 200s: it downgrades
# unsupported effort→high, which is free of side effects (high is a
# universally-accepted value), and it defends against the next flip back
# — a bare "xhigh" against Sonnet 4.6 that gets a 400 in production is
# painful to debug and re-fix. The cost of keeping the clamp during
# permissive windows is zero; the cost of losing it during a strict
# window is a production incident. So: keep the clamp, and treat the
# comment above as documenting a real observed flap, not a currently-live
# rejection.
#
# claude-4-7-opus/4-8-opus/5-sonnet were confirmed to both accept "xhigh"
# AND show a measurable behavioral difference (output_tokens step-changes
# up at xhigh/max; thinking_chars varies but not strictly monotonically)
# vs "high" on the same prompt — a real effect, not a no-op. Older/haiku
# models returned 200 for "xhigh" but showed no reliably distinguishable
# effect from "high" in that same probe, so they are treated as
# unverified and clamped down rather than assumed to work — the gateway's
# own lack of validation means a 200 response is not proof the field did
# anything.
#
# "high" itself was confirmed live to be accepted by EVERY model tested
# (Opus 4.7/4.8, Sonnet 5, Sonnet 4.6, Sonnet 4.5, Haiku) — it is the
# universally-safe value every accepted-set below includes, which is why
# orchestrator.py's coordinator calls request "high" unconditionally with
# no per-model clamping needed, while persistent_agent.py's agent loop
# requests "xhigh" and relies on the clamping in _resolve_effort below.
#
# Each entry is the model's full accepted-value set. Keys are substrings
# matched against the model name (case-insensitive), same convention as
# _MODEL_MAX_OUTPUT_TOKENS above. First match wins.
# ---------------------------------------------------------------------------
_EFFORT_XHIGH_CAPABLE = frozenset({"low", "medium", "high", "xhigh", "max"})
_EFFORT_NO_XHIGH = frozenset({"low", "medium", "high", "max"})

_MODEL_EFFORT_VALUES: list[tuple[str, frozenset]] = [
    # Confirmed to accept + act on "xhigh".
    ("claude-5-sonnet",  _EFFORT_XHIGH_CAPABLE),
    ("claude-4-8-opus",  _EFFORT_XHIGH_CAPABLE),
    ("claude-4-7-opus",  _EFFORT_XHIGH_CAPABLE),
    # Confirmed 400 on "xhigh" — not in this model's Bedrock-enforced enum.
    ("claude-4-6-sonnet", _EFFORT_NO_XHIGH),
]
# Everything else (4-5-sonnet, haiku, unknown future names): "xhigh"
# returned 200 but with no confirmed distinguishable effect — treat as
# unverified and restrict to the safe, universally-supported set rather
# than trust an unvalidated 200.
_DEFAULT_EFFORT_VALUES = _EFFORT_NO_XHIGH

# Downgrade path when a requested value isn't in the model's accepted set.
# "xhigh" is the only value observed to be rejected outright; per explicit
# product decision, fall back to "high" (not "max") on models that don't
# support it — "high" is universally supported and is the safe baseline
# every model in _DEFAULT_EFFORT_VALUES / _EFFORT_NO_XHIGH accepts.
_EFFORT_DOWNGRADE = {"xhigh": "high"}


def _resolve_effort(model: str, requested: Optional[str]) -> Optional[str]:
    """Clamp *requested* effort to a value *model*'s backend actually accepts.

    Returns ``None`` when *requested* is ``None`` (no ``output_config`` is
    sent at all — callers that never opt in see the same implicit-default
    behavior as before this parameter existed). Otherwise returns
    *requested* unchanged if it's in the model's confirmed accepted set, or
    the downgraded equivalent if not (e.g. "xhigh" on a claude-4-6-sonnet
    model becomes "high" instead of a value that 400s).

    Call-site convention (see llm_service.LLMService.chat_stream's
    ``effort`` docstring for the full rationale):

      * orchestrator.py's coordinator calls (_call_and_parse /
        _call_and_parse_streaming) always request "high" — confirmed live
        to be accepted by every model in the pool (old and new, including
        both whitelisted and non-whitelisted entries below), so it never
        needs downgrading and never 400s regardless of which model the
        user has selected.
      * persistent_agent.py's PersistentAgent._think_streaming always
        requests "xhigh" — only accepted by the whitelist below; every
        other model is downgraded here.

    Fallback safety: ``model`` is always the model actually about to serve
    THIS call, not necessarily the model the caller originally intended.
    call_with_fallback / call_with_fallback_stream (llm_pool.py) pass the
    same chat_kwargs dict — including whatever `effort` was requested —
    unchanged to every service in the fallback list, but each service's
    own chat_stream calls this function with ITS OWN self.model. So a
    fallback chain that starts on a whitelisted model (e.g. Opus, which
    accepts "xhigh") and ends up being served by a non-whitelisted one
    (e.g. Haiku, after Opus failed) still gets correctly downgraded on the
    hop that actually serves the request — verified live 2026-07-16:
    requesting "xhigh" against Opus, forcing a fallback to Haiku, Haiku
    completes with no 400.
    """
    if requested is None:
        return None
    model_lower = model.lower()
    accepted = _DEFAULT_EFFORT_VALUES
    for substring, values in _MODEL_EFFORT_VALUES:
        if substring in model_lower:
            accepted = values
            break
    if requested in accepted:
        return requested
    return _EFFORT_DOWNGRADE.get(requested, "high")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AnthropicStreamingService(LLMService):
    """Anthropic adapter that uses streaming for every request.

    Parameters
    ----------
    api_key:
        Anthropic / QGenie API key.
    base_url:
        Optional custom base URL (e.g. ``"https://qgenie-api.qualcomm.com/"``).
    model:
        Default model identifier (e.g. ``"anthropic::claude-4-5-haiku"``).
    max_tokens:
        Default maximum tokens to generate.  When ``None`` (default),
        the per-model ceiling from ``_MODEL_MAX_OUTPUT_TOKENS`` is used
        so output is never truncated by an artificially low limit.
    temperature:
        Default sampling temperature (default: 0.7).
    max_retries:
        How many times a transient failure is retried (default: 2).
    timeout:
        Per-request timeout in seconds (default: 600).
    verify_ssl:
        SSL certificate verification — ``True``/``False`` (default: ``False``).
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = "https://qgenie-api.qualcomm.com/",
        model: str = "anthropic::claude-4-5-haiku",
        max_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = 0.7,
        max_retries: int = 2,
        timeout: int = 600,
        verify_ssl: bool = False,
        context_window: Optional[int] = None,
    ) -> None:
        super().__init__(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            context_window=_resolve_context_window(model, context_window),
        )
        http_client = httpx.AsyncClient(
            verify=verify_ssl,
            limits=httpx.Limits(
                max_keepalive_connections=5,
                # Discard idle connections after 20s so we never reuse a
                # connection the server has already closed (ReadError on reuse).
                keepalive_expiry=20,
            ),
        )
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            max_retries=0,  # retries handled here
            timeout=timeout,
        )
        self._base_url = base_url
        self._timeout = timeout
        self._verify_ssl = verify_ssl

    # ------------------------------------------------------------------
    # Internal: consume the raw anthropic stream
    # ------------------------------------------------------------------

    async def _consume_stream(
        self,
        stream: Any,
    ) -> AsyncGenerator[Union[StreamToolCallEvent, StreamTextDeltaEvent, StreamDoneEvent], None]:
        """Consume a raw ``anthropic.AsyncAnthropic`` stream.

        Yields:
          • ``StreamTextDeltaEvent``  for each text chunk
          • ``StreamToolCallEvent``   when a tool-call block completes
          • ``StreamDoneEvent``       once (last event), carrying the full result
        """
        # In-progress tool-call blocks keyed by block index
        pending: dict[int, dict] = {}
        # In-progress thinking blocks keyed by block index — tracks both the
        # accumulated text and its signature so the finished block can be
        # replayed VERBATIM in a later assistant turn (Anthropic forbids any
        # modification of thinking/redacted_thinking content once issued).
        pending_thinking: dict[int, dict] = {}
        thinking_blocks: list[dict] = []  # completed, in original order
        text_parts: list[str] = []
        thinking_parts: list[str] = []  # extended thinking content (flat text, for logging)
        all_tool_calls: list[ToolCallInfo] = []
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0
        stop_reason: Optional[str] = None

        async for event in stream:
            etype = event.type

            # Log structural events in full; skip high-frequency delta events to
            # avoid flooding the log file with one timestamped line per token.
            if etype not in ("content_block_delta",):
                try:
                    event_data = json.dumps(vars(event), indent=2, default=str)
                except Exception:
                    event_data = repr(event)
                self.logger.debug(
                    f"AnthropicStreaming stream event [{etype}]:\n{event_data}",
                    component="AnthropicStreamingService",
                )

            if etype == "content_block_start":
                blk = event.content_block
                if blk.type == "tool_use":
                    pending[event.index] = {
                        "id": blk.id,
                        "name": blk.name,
                        "input_json": "",
                    }
                elif blk.type == "thinking":
                    pending_thinking[event.index] = {"thinking": "", "signature": ""}
                elif blk.type == "redacted_thinking":
                    # Arrives fully-formed (no delta events) — the server
                    # withheld the reasoning; `data` is opaque and must be
                    # replayed verbatim, same as a normal thinking block.
                    thinking_blocks.append({
                        "type": "redacted_thinking",
                        "data": getattr(blk, "data", ""),
                    })
                # text blocks are handled via delta events

            elif etype == "content_block_delta":
                delta = event.delta
                if delta.type == "input_json_delta":
                    blk = pending.get(event.index)
                    if blk is not None:
                        blk["input_json"] += delta.partial_json
                elif delta.type == "text_delta":
                    text_parts.append(delta.text)
                    yield StreamTextDeltaEvent(text=delta.text)
                elif delta.type == "thinking_delta":
                    # Extended thinking content — accumulate but don't yield
                    thinking_parts.append(delta.thinking)
                    tblk = pending_thinking.get(event.index)
                    if tblk is not None:
                        tblk["thinking"] += delta.thinking
                elif delta.type == "signature_delta":
                    # Server-issued signature over the thinking block's
                    # content — opaque, never regenerated or edited locally.
                    tblk = pending_thinking.get(event.index)
                    if tblk is not None:
                        tblk["signature"] += delta.signature

            elif etype == "content_block_stop":
                blk = pending.pop(event.index, None)
                if blk is not None:
                    try:
                        args = json.loads(blk["input_json"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tc = ToolCallInfo(
                        call_id=blk["id"],
                        tool_name=blk["name"],
                        tool_arguments=blk["input_json"] or "{}",
                    )
                    all_tool_calls.append(tc)
                    yield StreamToolCallEvent(
                        call_id=blk["id"],
                        tool_name=blk["name"],
                        args=args,
                        block_index=event.index,
                    )
                tblk = pending_thinking.pop(event.index, None)
                if tblk is not None:
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": tblk["thinking"],
                        "signature": tblk["signature"],
                    })
            elif etype == "message_delta":
                # message_delta carries the real input+output token counts for
                # streaming AND the cache_creation / cache_read fields. The
                # message_start branch below is a fallback only; on Anthropic
                # (and the QGenie gateway) cache tokens land here.
                # ``delta.stop_reason`` also arrives here — critical for
                # detecting truncated (max_tokens) completions downstream.
                delta_obj = getattr(event, "delta", None)
                if delta_obj is not None:
                    sr = getattr(delta_obj, "stop_reason", None)
                    if sr:
                        stop_reason = sr
                usage = getattr(event, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                    _cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    _cr = getattr(usage, "cache_read_input_tokens", 0) or 0
                    if _cc:
                        cache_creation_input_tokens = _cc
                    if _cr:
                        cache_read_input_tokens = _cr

            elif etype == "message_start":
                # Fallback only — most server implementations leave cache
                # fields null here and report them in message_delta. Kept
                # for non-standard servers that put them on message_start.
                msg = getattr(event, "message", None)
                if msg is not None:
                    usage = getattr(msg, "usage", None)
                    if usage is not None:
                        _in = getattr(usage, "input_tokens", 0) or 0
                        _out = getattr(usage, "output_tokens", 0) or 0
                        if _in or _out:  # only override if non-zero
                            input_tokens = _in
                            output_tokens = _out
                        _cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
                        _cr = getattr(usage, "cache_read_input_tokens", 0) or 0
                        if _cc:
                            cache_creation_input_tokens = _cc
                        if _cr:
                            cache_read_input_tokens = _cr

        # Build final LLMChatResult
        text_content = "".join(text_parts) or None
        reasoning_content = "".join(thinking_parts) or None
        tool_name_val = all_tool_calls[0].tool_name if all_tool_calls else None
        tool_args_val = all_tool_calls[0].tool_arguments if all_tool_calls else None

        result = LLMChatResult(
            content=text_content,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
            tool_name=tool_name_val,
            tool_arguments=tool_args_val,
            tool_calls=all_tool_calls,
            token_usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_input_tokens,
                cache_read_tokens=cache_read_input_tokens,
            ),
            stop_reason=stop_reason,
        )
        self.logger.debug(
            f"AnthropicStreaming response summary: "
            f"text_len={len(text_content) if text_content else 0}, "
            f"tool_calls={len(all_tool_calls)}, "
            f"stop_reason={stop_reason}, "
            f"tokens=in:{input_tokens} out:{output_tokens} total:{input_tokens + output_tokens}"
            + (f" cache_create:{cache_creation_input_tokens}" if cache_creation_input_tokens else "")
            + (f" cache_read:{cache_read_input_tokens}" if cache_read_input_tokens else "")
            + f"\n  text: {text_content!r}\n"
            f"  tool_calls: {[{'name': tc.tool_name, 'args': tc.tool_arguments} for tc in all_tool_calls]}",
            component="AnthropicStreamingService",
        )
        yield StreamDoneEvent(result=result)

    # ------------------------------------------------------------------
    # Internal: build Anthropic API kwargs from LLMService parameters
    # ------------------------------------------------------------------

    def _build_api_kwargs(
        self,
        messages: list[Any],
        model: str,
        temperature: float,
        max_tokens: int,
        stop: Optional[list[str]],
        top_k: Optional[int],
        top_p: Optional[float],
        tools: Optional[list[dict[str, Any]]],
        tool_choice: Optional[Any],
        thinking_budget_tokens: Optional[int],
        effort: Optional[str] = None,
    ) -> dict[str, Any]:
        """Separate system messages, convert OpenAI→Anthropic format, build kwargs.

        OpenAI multi-turn tool-calling format uses:
          • assistant message with ``tool_calls`` list
          • follow-up messages with ``role: "tool"`` and ``tool_call_id``

        Anthropic uses:
          • assistant message with ``content`` list containing ``tool_use`` blocks
          • follow-up ``role: "user"`` message with ``content`` list containing
            ``tool_result`` blocks (one per tool call, grouped into one message)

        This method converts the OpenAI format produced by
        _build_messages_from_observations() into the Anthropic format expected
        by the API.

        Extended thinking
        -----------------
        When ``thinking_budget_tokens`` is set, the ``thinking`` parameter is
        added with ``{"type": "enabled", "budget_tokens": N}``.  Temperature
        must be 1.0 when thinking is enabled (Anthropic requirement).

        Effort
        ------
        When ``effort`` is set, ``output_config.effort`` is added. Already
        clamped to the model's confirmed ceiling by ``_resolve_effort`` in
        ``chat_stream`` before reaching here — this method sends whatever
        value it's given unchanged.
        """
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        converted = self._convert_messages_to_anthropic(other_msgs)

        # Anthropic requires temperature=1 when extended thinking is enabled
        effective_temp = 1.0 if thinking_budget_tokens else temperature

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "temperature": effective_temp,
            "max_tokens": _resolve_max_tokens(model, max_tokens, thinking_budget_tokens),
        }

        # Anthropic's `system` param accepts an array of independently
        # cacheable text blocks (not just one opaque string) — mirrors Claude
        # Code's practice of sending the system prompt as named sections so
        # a stable prefix (identity/behavior rules) can be cached separately
        # from a more volatile suffix (Environment: cwd/platform/etc). Every
        # system message we receive here becomes its own array element; a
        # cache breakpoint is placed on the LAST block that precedes the
        # final one (treated as the volatile "Environment" tail), so the
        # stable prefix is served from prefix cache even when the tail
        # changes. A single system message (e.g. the Coordinator's INTENT
        # call) degenerates to one block with no breakpoint, unchanged from
        # before.
        if len(system_msgs) >= 2:
            system_blocks: list[dict[str, Any]] = [
                {"type": "text", "text": m.get("content", "") or ""}
                for m in system_msgs
            ]
            system_blocks[-2]["cache_control"] = {"type": "ephemeral"}
            api_kwargs["system"] = system_blocks
        else:
            system_content = "\n\n".join(m.get("content", "") for m in system_msgs)
            if system_content:
                api_kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system_content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
        if stop:
            api_kwargs["stop_sequences"] = stop
        if top_k is not None:
            api_kwargs["top_k"] = top_k
        if top_p is not None:
            api_kwargs["top_p"] = top_p
        if tools:
            converted_tools = self._convert_tools(tools)
            if converted_tools:
                converted_tools[-1]["cache_control"] = {"type": "ephemeral"}
            api_kwargs["tools"] = converted_tools
        if tool_choice is not None:
            api_kwargs["tool_choice"] = tool_choice
        if thinking_budget_tokens is not None:
            api_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            }
        if effort is not None:
            api_kwargs["output_config"] = {"effort": effort}

        return api_kwargs

    @staticmethod
    def _convert_messages_to_anthropic(messages: list[dict]) -> list[dict]:
        """Convert an OpenAI-format message list to Anthropic format.

        Handles three special cases:
        1. assistant message with ``tool_calls``
           → assistant message with content list of ``tool_use`` blocks
        2. ``role: "tool"`` messages (one per tool call)
           → grouped into a single ``role: "user"`` message with
             ``tool_result`` content blocks
        3. All other messages pass through with None-content sanitised to "".
        """
        result: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            # ── assistant with tool_calls → Anthropic tool_use blocks ─────────
            if role == "assistant" and msg.get("tool_calls"):
                content_blocks: list[dict] = []
                # Thinking/redacted_thinking blocks MUST lead the content
                # array and MUST be replayed byte-for-byte as the API issued
                # them — Anthropic rejects (or silently degrades quality of)
                # a modified thinking block. `thinking_blocks` was captured
                # verbatim in _consume_stream and stored untouched since.
                for tb in (msg.get("thinking_blocks") or []):
                    content_blocks.append(tb)
                text = msg.get("content") or ""
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    try:
                        input_dict = json.loads(fn.get("arguments", "{}") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        input_dict = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": input_dict,
                    })
                result.append({"role": "assistant", "content": content_blocks})
                i += 1
                continue

            # ── assistant with thinking_blocks but no tool_calls (a
            # completion turn) → same verbatim-leading-block treatment ─────
            if role == "assistant" and msg.get("thinking_blocks"):
                content_blocks = list(msg["thinking_blocks"])
                text = msg.get("content") or ""
                if text:
                    content_blocks.append({"type": "text", "text": text})
                result.append({"role": "assistant", "content": content_blocks})
                i += 1
                continue

            # ── role: "tool" → grouped user message with tool_result blocks ───
            if role == "tool":
                tool_result_blocks: list[dict] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    tm = messages[i]
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tm.get("tool_call_id", ""),
                        "content": tm.get("content") or "",
                    })
                    i += 1
                result.append({"role": "user", "content": tool_result_blocks})
                continue

            # ── all other messages: sanitise None content ─────────────────────
            content = msg.get("content")
            if content is None:
                content = ""
            out_msg = {**msg, "content": content}
            # `_cache_anchor` is a private convention from PersistentAgent
            # (see _build_messages): the last skill-prelude message carries
            # this flag so we attach a cache_control breakpoint here. The
            # flag is stripped before sending to the API.
            anchor = out_msg.pop("_cache_anchor", False)
            if anchor:
                if isinstance(content, str):
                    out_msg["content"] = [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }]
                elif isinstance(content, list) and content:
                    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                    out_msg["content"] = content
            result.append(out_msg)
            i += 1

        return AnthropicStreamingService._merge_adjacent_same_role(result)

    @staticmethod
    def _merge_adjacent_same_role(messages: list[dict]) -> list[dict]:
        """Coalesce consecutive messages that share a role.

        The Anthropic API requires user/assistant roles to alternate; two
        consecutive same-role messages are rejected with a 400. Two callers
        legitimately produce adjacency:
          - PersistentAgent's instruction-at-bottom layout places a tool-result
            user message (the last turn's observations) directly before the
            per-item instruction user message.
          - An obs-less completion turn can sit next to another assistant
            message.
        Content is normalised to a block list before concatenation so text and
        tool_use/tool_result blocks combine cleanly; tool_result blocks keep
        their leading position because turns always precede the instruction.
        Any cache_control already attached to a block is preserved.
        """
        def _as_blocks(content: Any) -> list[dict]:
            if isinstance(content, list):
                return content
            if isinstance(content, str):
                return [{"type": "text", "text": content}] if content else []
            return []

        merged: list[dict] = []
        for msg in messages:
            if merged and merged[-1].get("role") == msg.get("role"):
                prev = merged[-1]
                combined = _as_blocks(prev.get("content")) + _as_blocks(msg.get("content"))
                if prev.get("role") == "assistant":
                    # Anthropic requires thinking/redacted_thinking blocks to
                    # lead an assistant turn's content. Merging two adjacent
                    # assistant messages (see docstring) can otherwise strand
                    # the second message's thinking block mid-array — hoist
                    # ALL thinking blocks to the front, preserving their
                    # relative order and leaving each block's own content
                    # untouched (only array position changes).
                    thinking = [b for b in combined if b.get("type") in ("thinking", "redacted_thinking")]
                    rest = [b for b in combined if b.get("type") not in ("thinking", "redacted_thinking")]
                    combined = thinking + rest
                prev["content"] = combined
            else:
                merged.append(dict(msg))
        return merged

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI function-calling tool defs to Anthropic tool format.

        OpenAI format:
            {"type": "function", "function": {"name": ..., "description": ...,
                                               "parameters": {...}}}
        Anthropic format:
            {"name": ..., "description": ..., "input_schema": {...}}

        If the tool is already in Anthropic format (has "input_schema"), it is
        passed through unchanged.
        """
        converted = []
        for t in tools:
            if "input_schema" in t:
                # Already Anthropic format
                converted.append(t)
            elif t.get("type") == "function" and "function" in t:
                fn = t["function"]
                converted.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            else:
                # Unknown format — pass through and let the API reject it
                converted.append(t)
        return converted

    # ------------------------------------------------------------------
    # LLMService.chat_stream() — the main entry point
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[Any],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        json_mode: bool = True,
        first_content: bool = True,
        frequency_penalty: Optional[float] = None,   # ignored — Anthropic only
        presence_penalty: Optional[float] = None,    # ignored — Anthropic only
        repetition_penalty: Optional[float] = None,  # ignored — Anthropic only
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[Literal["low", "medium", "high"]] = None,
        thinking_budget_tokens: Optional[int] = None,
        effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Any] = None,       # ignored — Anthropic only
        stream_options: Optional[Any] = None,        # ignored — Anthropic only
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Open a streaming request and return an async generator of events.

        Yields ``StreamTextDeltaEvent``, ``StreamToolCallEvent``, and a
        final ``StreamDoneEvent`` carrying the full ``LLMChatResult``.

        Retries transient errors up to ``max_retries`` times.  PTL and 4xx
        client errors are raised immediately without retrying.
        """
        model_name = model if model is not None else self.model
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        # Resolve thinking budget: explicit request > reasoning_effort mapping
        # > model-name default (":thinking" marker, mirrors ":1m"). A model
        # whose configured name doesn't opt in never gets the "thinking" API
        # param, so unsupported providers/models take the unchanged path.
        budget: Optional[int] = thinking_budget_tokens
        if budget is None and reasoning_effort is not None:
            budget = {"low": 1024, "medium": 4096, "high": 10000}.get(reasoning_effort)
        if budget is None and _model_supports_extended_thinking(model_name):
            budget = _DEFAULT_THINKING_BUDGET_TOKENS

        # Clamp requested effort to what this model's backend actually
        # accepts. Historical context: "xhigh" on claude-4-6-sonnet was
        # observed to 400 on 2026-07-16 but return 200 on 2026-07-17 —
        # see the extended note above _MODEL_EFFORT_VALUES for the flap
        # and the rationale for keeping the clamp as a safety net.
        resolved_effort = _resolve_effort(model_name, effort)

        api_kwargs = self._build_api_kwargs(
            messages=messages,
            model=model_name,
            temperature=temp,
            max_tokens=max_tok,
            stop=stop,
            top_k=top_k,
            top_p=top_p,
            tools=tools,
            tool_choice=tool_choice,
            thinking_budget_tokens=budget,
            effort=resolved_effort,
        )

        self.logger.debug(
            f"AnthropicStreaming request: model={model_name}, "
            f"messages={len(messages)}, "
            f"tools={len(tools) if tools else 0}"
            + (f", thinking_budget={budget}" if budget else "")
            + (f", effort={resolved_effort}" if resolved_effort else ""),
            component="AnthropicStreamingService",
        )

        # Log raw request payload (exclude system prompt for brevity)
        _log_kwargs = {k: v for k, v in api_kwargs.items() if k != "system"}
        self.logger.debug(
            f"AnthropicStreaming raw request:\n{json.dumps(_log_kwargs, indent=2, default=str)}",
            component="AnthropicStreamingService",
        )

        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                raw_stream = await self._client.messages.create(
                    **api_kwargs,
                    stream=True,
                )
                # Yield all events from the stream; errors after first event
                # propagate directly to the caller.
                async for event in self._consume_stream(raw_stream):
                    yield event
                return

            except Exception as e:
                last_error = e

                # Cheapest and most certain check, so it goes first: a failure
                # that never left this machine cannot be fixed by waiting (see
                # LLMService._is_permanent_local_error). With max_retries=10 and a
                # 2**n ladder, retrying one of these spends ~17 minutes before
                # anyone hears about it — and on a remote-driven session, silence
                # is the only thing the operator sees while it happens.
                if self._is_permanent_local_error(e):
                    self.logger.error(
                        f"AnthropicStreaming request failed (permanent local "
                        f"error, not retrying): {type(e).__name__}: {e}. Most "
                        f"likely llm.API_KEY is empty in this machine's "
                        f"handq_config.yaml and neither ANTHROPIC_API_KEY nor "
                        f"ANTHROPIC_AUTH_TOKEN is set in its environment.",
                        component="AnthropicStreamingService",
                    )
                    raise

                if self._is_prompt_too_long_error(e):
                    self.logger.error(
                        f"AnthropicStreaming request failed (prompt too long): {type(e).__name__}: {e}",
                        component="AnthropicStreamingService",
                    )
                    raise

                if self._is_client_error(e):
                    self.logger.error(
                        f"AnthropicStreaming request failed (client error, not retrying): {type(e).__name__}: {e}",
                        component="AnthropicStreamingService",
                    )
                    raise

                # Rate-limit (429): fast-fail so llm_pool can fall back to the
                # next service immediately. Retrying the same throttled service
                # within a single call is futile.
                #
                # Whether to mark the service _exhausted (skip it for the rest
                # of the session) depends on the retry window: for tokens_per_day
                # the Retry-After is many hours, so a session-wide skip is
                # correct; for RPM/burst throttles it's tens of seconds, so
                # marking exhausted would silently shrink the pool for the
                # rest of the session even though the service will recover
                # within the same call's lifetime.
                is_429 = (
                    isinstance(e, anthropic.RateLimitError)
                    or "rate limit exceeded" in str(e).lower()
                )
                if is_429:
                    retry_after_secs, limit_type = _parse_rate_limit_details(e)
                    # A per-day/-month budget is a session-ending condition for
                    # this service no matter what the seconds field says: the
                    # class of the limit is authoritative, the number is
                    # advisory (and may be absent entirely). Long explicit
                    # waits still qualify on the seconds path alone, so a
                    # gateway that reports only Retry-After keeps working.
                    long_window = limit_type in _LONG_WINDOW_LIMIT_TYPES
                    if long_window or retry_after_secs > 300.0:
                        self.mark_exhausted(retry_after_secs)
                        self.logger.warning(
                            f"AnthropicStreaming service marked exhausted "
                            f"(model={self.model}, limit_type={limit_type or 'unknown'}, "
                            f"retry_after={retry_after_secs:.0f}s) — skipping it for "
                            f"the rest of this session: {type(e).__name__}: {e}",
                            component="AnthropicStreamingService",
                        )
                    else:
                        self.logger.warning(
                            f"AnthropicStreaming throttled "
                            f"(model={self.model}, limit_type={limit_type or 'unknown'}, "
                            f"retry_after={retry_after_secs:.0f}s); "
                            f"falling back without marking exhausted: "
                            f"{type(e).__name__}: {e}",
                            component="AnthropicStreamingService",
                        )
                    raise

                if attempt < self.max_retries - 1:
                    wait_time = min(600, 2 ** (attempt + 1))
                    self.logger.warning(
                        f"AnthropicStreaming request failed "
                        f"(attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {wait_time}s: {type(e).__name__}: {e}",
                        component="AnthropicStreamingService",
                    )
                    if self.on_server_error is not None:
                        try:
                            self.on_server_error(
                                f"[{self.model}] {self._user_friendly_error(e)}",
                                wait_time,
                                self.max_retries - attempt - 1,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(
                        f"AnthropicStreaming request failed after "
                        f"{self.max_retries} retries: {type(e).__name__}: {e}",
                        component="AnthropicStreamingService",
                    )

        assert last_error is not None
        raise last_error

    async def close(self) -> None:
        """Close the underlying Anthropic client."""
        await self._client.close()

    def _user_friendly_error(self, e: Exception) -> str:
        """Return a short, user-friendly description of a server-side error."""
        if isinstance(e, TypeError) and "authentication" in str(e).lower():
            # Not a server error at all — the request never left this machine.
            # Named explicitly because the SDK's own wording ("Could not resolve
            # authentication method") gives no hint about which file to edit.
            return (
                "LLM 认证凭据未配置：这台机器的 handq_config.yaml 里 llm.API_KEY 为空，"
                "且环境变量 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 也没有设置"
            )
        if isinstance(e, anthropic.APIStatusError):
            status = getattr(e, 'status_code', None)
            if status and 500 <= status < 600:
                return f"API server error (HTTP {status})"
            return f"API error (HTTP {status or '?'})"
        if isinstance(e, anthropic.APITimeoutError):
            return "API request timed out"
        if isinstance(e, anthropic.APIConnectionError):
            return "API connection failed"
        return f"Temporary error ({type(e).__name__})"
