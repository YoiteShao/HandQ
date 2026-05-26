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
    # claude-4 / claude-opus-4 series
    # 1M-context variants support 128K output — must come before generic claude-sonnet-4 entry
    (":1m",             128000),
    (":1M",             128000),
    ("[1m]",            128000),
    ("[1M]",            128000),
    # claude-4-7-opus has 1M context and 128K output natively
    ("claude-4-7-opus", 128000),
    ("claude-opus-4",   32000),
    ("claude-sonnet-4", 64000),
    ("claude-haiku-4",  16000),
    # claude-3.7 series
    ("claude-3-7",      64000),
    ("claude-3.7",      64000),
    # claude-3.5 series
    ("claude-3-5",       8192),
    ("claude-3.5",       8192),
    # claude-3 series
    ("claude-3",         4096),
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
    # claude-4-7-opus has native 1M context window
    ("claude-4-7-opus",  1_000_000),
    # claude-4 series
    ("claude-opus-4",      200_000),
    ("claude-sonnet-4",    200_000),
    ("claude-haiku-4",     200_000),
    # claude-3.7 series
    ("claude-3-7",         200_000),
    ("claude-3.7",         200_000),
    # claude-3.5 series
    ("claude-3-5",         200_000),
    ("claude-3.5",         200_000),
    # claude-3 series
    ("claude-3",           200_000),
]
_DEFAULT_CONTEXT_WINDOW = 200_000  # conservative default for unknown models


def _resolve_max_tokens(model: str, requested: Optional[int]) -> int:
    """Return the effective max_tokens for an Anthropic API call.

    If *requested* is provided and positive, it is returned as-is.
    Otherwise the per-model ceiling from ``_MODEL_MAX_OUTPUT_TOKENS`` is used,
    falling back to ``_DEFAULT_MAX_OUTPUT_TOKENS`` for unknown models.
    """
    if requested is not None:
        return requested
    model_lower = model.lower()
    for substring, ceiling in _MODEL_MAX_OUTPUT_TOKENS:
        if substring in model_lower:
            return ceiling
    return _DEFAULT_MAX_OUTPUT_TOKENS


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
        text_parts: list[str] = []
        thinking_parts: list[str] = []  # extended thinking content
        all_tool_calls: list[ToolCallInfo] = []
        input_tokens: int = 0
        output_tokens: int = 0
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0

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
                # text and thinking blocks are handled via delta events

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
            elif etype == "message_delta":
                # message_delta carries the real input+output token counts for streaming
                usage = getattr(event, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0

            elif etype == "message_start":
                # message_start usage is always 0 for streaming (confirmed in logs);
                # kept here only as a fallback for non-standard server implementations.
                msg = getattr(event, "message", None)
                if msg is not None:
                    usage = getattr(msg, "usage", None)
                    if usage is not None:
                        _in = getattr(usage, "input_tokens", 0) or 0
                        _out = getattr(usage, "output_tokens", 0) or 0
                        if _in or _out:  # only override if non-zero
                            input_tokens = _in
                            output_tokens = _out
                        # Cache tokens are typically reported in message_start
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
            tool_name=tool_name_val,
            tool_arguments=tool_args_val,
            tool_calls=all_tool_calls,
            token_usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_input_tokens,
                cache_read_tokens=cache_read_input_tokens,
            ),
        )
        self.logger.debug(
            f"AnthropicStreaming response summary: "
            f"text_len={len(text_content) if text_content else 0}, "
            f"tool_calls={len(all_tool_calls)}, "
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
            "max_tokens": _resolve_max_tokens(model, max_tokens),
        }

        system_content = "\n\n".join(m.get("content", "") for m in system_msgs)
        if system_content:
            api_kwargs["system"] = system_content
        if stop:
            api_kwargs["stop_sequences"] = stop
        if top_k is not None:
            api_kwargs["top_k"] = top_k
        if top_p is not None:
            api_kwargs["top_p"] = top_p
        if tools:
            api_kwargs["tools"] = self._convert_tools(tools)
        if tool_choice is not None:
            api_kwargs["tool_choice"] = tool_choice
        if thinking_budget_tokens is not None:
            api_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            }

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
            result.append({**msg, "content": content})
            i += 1

        return result

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

        # Resolve thinking budget: explicit > reasoning_effort mapping
        budget: Optional[int] = thinking_budget_tokens
        if budget is None and reasoning_effort is not None:
            budget = {"low": 1024, "medium": 4096, "high": 10000}.get(reasoning_effort)

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
        )

        self.logger.debug(
            f"AnthropicStreaming request: model={model_name}, "
            f"messages={len(messages)}, "
            f"tools={len(tools) if tools else 0}"
            + (f", thinking_budget={budget}" if budget else ""),
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

                # Rate-limit (429): fast-fail so llm_pool can fall back to the next
                # service immediately.  Retrying the same exhausted service is futile
                # (tokens_per_day limits can have retry_after of many hours).
                if isinstance(e, anthropic.RateLimitError) or "rate limit exceeded" in str(e).lower():
                    self._exhausted = True
                    self.logger.warning(
                        f"AnthropicStreaming service marked exhausted for this session: {type(e).__name__}: {e}",
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
                                self._user_friendly_error(e),
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
