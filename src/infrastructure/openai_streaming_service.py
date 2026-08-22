"""
OpenAI Streaming LLM Service
============================
OpenAI-compatible streaming adapter for :class:`src.infrastructure.llm_service.LLMService`.

The adapter targets the YOUR-AI-ENDPOINT/OpenAI-compatible chat-completions endpoint by
default and keeps the same public event contract as ``AnthropicStreamingService``:

  • ``StreamTextDeltaEvent`` for each text delta
  • ``StreamToolCallEvent`` when a streamed tool call's JSON arguments are complete
  • ``StreamDoneEvent`` with the final ``LLMChatResult``

It intentionally uses ``httpx`` directly instead of the OpenAI SDK so packaging
only needs the dependency already used by the Anthropic adapter.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncGenerator, Literal, Optional
from urllib.parse import urljoin

import httpx

from .anthropic_streaming_service import (
    StreamDoneEvent,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
)
from .llm_service import LLMChatResult, LLMService, ToolCallInfo
from ..models.token_usage import TokenUsage

_DEFAULT_BASE_URL = "https://YOUR-AI-ENDPOINT-api.COMPANY.com/v1"
_DEFAULT_CONTEXT_WINDOW = 200_000
_DEFAULT_MAX_OUTPUT_TOKENS = 16_384
_RETRY_AFTER_RE = re.compile(r"['\"]retry_after['\"]\s*:\s*['\"]?([0-9]+(?:\.[0-9]+)?)")
_LONG_WINDOW_LIMIT_TYPES = frozenset({"tokens_per_day", "requests_per_day", "tokens_per_month"})


def _chat_completions_url(base_url: str) -> str:
    base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _coerce_seconds(raw: Any) -> float:
    if raw is None:
        return 0.0
    digits = re.sub(r"[^0-9.]", "", str(raw))
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Error-body helpers — used in chat_stream error handling
# ---------------------------------------------------------------------------

def _error_text(exc: Exception) -> str:
    """Return a compact string representation of the error body for logging.

    Tries the structured JSON body first (httpx ``HTTPStatusError``), falls
    back to raw ``text`` / ``content`` attributes. Returns ``""`` when no
    body is accessible so callers can safely embed this in f-strings.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        return str(response.json())[:500]
    except Exception:
        pass
    text = getattr(response, "text", None)
    if text:
        return str(text)[:500]
    content = getattr(response, "content", None)
    if content:
        return str(content)[:500]
    return ""


# Patterns that extract a parameter name from an unsupported-parameter 400.
# Checked in order; first match wins.
_UNSUPPORTED_PARAM_RE = re.compile(
    r"[Uu]nsupported parameter[:\s]+['\"]?([a-zA-Z0-9_.]+)['\"]?"
    r"|parameter ['\"]([a-zA-Z0-9_.]+)['\"] is not supported"
    r"|['\"]param['\"]:\s*['\"]([a-zA-Z0-9_.]+)['\"]",
)


def _unsupported_parameter_from_error(exc: Exception) -> Optional[str]:
    """Return the unsupported parameter name from a 400 error, or ``None``.

    Checks the structured JSON body first: OpenAI-compatible error bodies
    carry the offending parameter in ``error.param``.  Falls back to regex
    over the stringified error / response text.

    Used in ``chat_stream`` to strip a rejected parameter (e.g. ``temperature``
    for reasoning-model endpoints) and retry the same request without it.
    """
    response = getattr(exc, "response", None)
    text = str(exc)
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict):
                err = body.get("error") or {}
                if isinstance(err, dict):
                    param = err.get("param")
                    if param and isinstance(param, str):
                        return param
                    text = str(err.get("message") or "") or text
        except Exception:
            body_text = getattr(response, "text", "") or ""
            if body_text:
                text = body_text
    m = _UNSUPPORTED_PARAM_RE.search(text)
    if m:
        return next(g for g in m.groups() if g is not None)
    return None


def _model_disallows_sampling_params(model: str) -> bool:
    m = (model or "").lower()
    if m.startswith(("azure::gpt-5", "openai::gpt-5", "gpt-5")):
        return True
    if m.startswith(("azure::o", "openai::o", "o1", "o3", "o4")):
        return True
    return False


def _parse_rate_limit_details(exc: Exception) -> tuple[float, str]:
    retry_after = 0.0
    limit_type = ""
    response = getattr(exc, "response", None)
    if response is not None:
        retry_after = _coerce_seconds(response.headers.get("retry-after"))
        try:
            body = response.json()
            details = ((body.get("error") or {}).get("details") or {}) if isinstance(body, dict) else {}
            retry_after = retry_after or _coerce_seconds(details.get("retry_after"))
            limit_type = str(details.get("limit_type") or "")
        except Exception:
            body_text = getattr(response, "text", "") or ""
            m = _RETRY_AFTER_RE.search(body_text)
            if m:
                retry_after = retry_after or _coerce_seconds(m.group(1))
            m = re.search(r"['\"]limit_type['\"]\s*:\s*['\"]([a-zA-Z_]+)['\"]", body_text)
            if m:
                limit_type = m.group(1)
    return retry_after, limit_type


def _resolve_context_window(model: str, override: Optional[int]) -> int:
    if override and override > 0:
        return override
    ml = (model or "").lower()
    if "gpt-5" in ml or "gpt-4.1" in ml or "gpt-4o" in ml:
        return 200_000
    return _DEFAULT_CONTEXT_WINDOW


def _resolve_max_tokens(requested: Optional[int]) -> int:
    if requested is None or requested <= 0:
        return _DEFAULT_MAX_OUTPUT_TOKENS
    return requested


class OpenAIStreamingService(LLMService):
    """OpenAI-compatible chat-completions streaming adapter.

    ``model`` is passed through unchanged.  This is required for YOUR-AI-ENDPOINT model
    identifiers such as ``azure::gpt-5.4``.
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = _DEFAULT_BASE_URL,
        model: str = "azure::gpt-5.4",
        max_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: Optional[float] = None,
        max_retries: int = 2,
        timeout: int = 600,
        verify_ssl: bool = False,
        context_window: Optional[int] = None,
    ) -> None:
        super().__init__(
            model=model,
            max_tokens=max_tokens,
            # LLMService stores a concrete value for compatibility, but this
            # adapter treats None as "omit temperature from the payload".
            temperature=temperature if temperature is not None else 0.0,
            max_retries=max_retries,
            context_window=_resolve_context_window(model, context_window),
        )
        self._default_temperature = temperature
        self._base_url = base_url or _DEFAULT_BASE_URL
        self._url = _chat_completions_url(self._base_url)
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._client = httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=20),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    # ------------------------------------------------------------------
    # Payload conversion helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_messages(messages: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            msg = dict(raw)
            # Anthropic-only metadata must never be sent to OpenAI-compatible APIs.
            msg.pop("thinking_blocks", None)
            msg.pop("reasoning_content", None)
            msg.pop("_cache_anchor", None)
            content = msg.get("content")
            if content is None:
                msg["content"] = ""
            elif isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    p = dict(part)
                    p.pop("cache_control", None)
                    ptype = p.get("type")
                    if ptype in ("text", "image_url", "input_text", "input_image"):
                        parts.append(p)
                    elif ptype == "tool_result":
                        # This should normally arrive as role=tool. If an
                        # Anthropic-style block leaks through, preserve its text.
                        parts.append({"type": "text", "text": str(p.get("content") or "")})
                    # Drop Anthropic-only tool_use/thinking/redacted_thinking blocks.
                msg["content"] = parts if parts else ""
            out.append(msg)
        return out

    @staticmethod
    def _sanitize_schema_for_azure(schema: dict[str, Any]) -> dict[str, Any]:
        """Flatten JSON Schema constructs that Azure OpenAI rejects in function parameters.

        Azure OpenAI requires function parameter schemas to have ``type: object``
        at the top level and must not contain ``oneOf`` / ``anyOf`` / ``allOf`` /
        ``enum`` / ``const`` / ``not`` at any level.  We apply a best-effort
        flattening so tools defined with rich JSON Schema still work:

        * ``oneOf`` / ``anyOf`` / ``allOf``: if any branch carries a concrete
          ``type`` field, merge the first such branch into the result (sibling
          keys like ``description`` override the branch).  If no branch has a
          ``type`` (e.g. ``anyOf: [{required:[path]}, {required:[paths]}]``),
          drop the keyword entirely — the constraint is unenforced but the
          schema stays structurally valid.
        * ``not`` / ``const``: drop unconditionally (very rare in tool schemas).
        * Recurse into ``properties`` values and ``items`` schemas.
        """
        if not isinstance(schema, dict):
            return schema

        result: dict[str, Any] = {}

        # Flatten polymorphic keywords first so explicit sibling keys can override
        for poly_key in ("oneOf", "anyOf", "allOf"):
            if poly_key in schema:
                branches = [b for b in schema[poly_key] if isinstance(b, dict)]
                typed_branch = next((b for b in branches if "type" in b), None)
                if typed_branch:
                    result.update(
                        OpenAIStreamingService._sanitize_schema_for_azure(typed_branch)
                    )
                # If no branch carries a type (e.g. anyOf:[{required:[x]},{required:[y]}])
                # drop the keyword — no merge, just omit.

        for k, v in schema.items():
            if k in ("oneOf", "anyOf", "allOf", "not", "const"):
                continue  # already handled above or explicitly dropped
            if k == "properties" and isinstance(v, dict):
                result["properties"] = {
                    prop_name: OpenAIStreamingService._sanitize_schema_for_azure(prop_schema)
                    for prop_name, prop_schema in v.items()
                }
            elif k == "items" and isinstance(v, dict):
                result["items"] = OpenAIStreamingService._sanitize_schema_for_azure(v)
            else:
                result[k] = v

        return result

    @staticmethod
    def _convert_tools(tools: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        if not tools:
            return None
        converted: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and "function" in t:
                # Sanitise the parameters schema for Azure/OpenAI compatibility
                # (removes oneOf/anyOf/allOf/not/const which Azure rejects).
                fn = dict(t["function"])
                if "parameters" in fn and isinstance(fn["parameters"], dict):
                    fn["parameters"] = OpenAIStreamingService._sanitize_schema_for_azure(
                        fn["parameters"]
                    )
                converted.append({"type": "function", "function": fn})
            elif "input_schema" in t:
                raw_params = t.get("input_schema") or {"type": "object", "properties": {}}
                converted.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": OpenAIStreamingService._sanitize_schema_for_azure(
                            raw_params
                        ),
                    },
                })
            else:
                converted.append(t)
        return converted or None

    @staticmethod
    def _convert_tool_choice(tool_choice: Optional[Any]) -> Optional[Any]:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, str):
            return tool_choice
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function":
                return tool_choice
            if tool_choice.get("type") == "tool" and tool_choice.get("name"):
                return {"type": "function", "function": {"name": tool_choice["name"]}}
            if tool_choice.get("type") == "any":
                return "required"
            if tool_choice.get("type") == "auto":
                return "auto"
        return tool_choice

    def _build_payload(
        self,
        messages: list[Any],
        model: str,
        temperature: float,
        max_tokens: int,
        stop: Optional[list[str]],
        json_mode: bool,
        frequency_penalty: Optional[float],
        presence_penalty: Optional[float],
        top_p: Optional[float],
        reasoning_effort: Optional[str],
        effort: Optional[str],
        tools: Optional[list[dict[str, Any]]],
        tool_choice: Optional[Any],
        response_format: Optional[Any],
        stream_options: Optional[Any],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._normalise_messages(messages),
            "stream": True,
            "max_tokens": _resolve_max_tokens(max_tokens),
            # Ask OpenAI-compatible servers to include a final usage-only chunk.
            "stream_options": stream_options if stream_options is not None else {"include_usage": True},
        }
        # Keep sampling knobs configurable but omit them for reasoning models
        # that reject `temperature`/`top_p` (for example YOUR-AI-ENDPOINT Azure GPT-5.x).
        omit_sampling = _model_disallows_sampling_params(model)
        if temperature is not None and not omit_sampling:
            payload["temperature"] = temperature
        if stop:
            payload["stop"] = stop
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty
        if presence_penalty is not None:
            payload["presence_penalty"] = presence_penalty
        if top_p is not None and not omit_sampling:
            payload["top_p"] = top_p
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
        converted_choice = self._convert_tool_choice(tool_choice)
        if converted_choice is not None:
            payload["tool_choice"] = converted_choice
        # Do not force JSON mode implicitly: OpenAI JSON mode can 400 if the
        # prompt does not mention JSON. Respect explicit caller format instead.
        if response_format is not None:
            payload["response_format"] = response_format
        # YOUR-AI-ENDPOINT/OpenAI-compatible reasoning models accept reasoning_effort;
        # prefer explicit reasoning_effort, otherwise map the newer HandQ
        # effort knob through unchanged (low/medium/high/xhigh/max).
        resolved_effort = reasoning_effort or effort
        if resolved_effort and resolved_effort != "none":
            payload["reasoning_effort"] = resolved_effort
        for k, v in extra.items():
            if v is not None and k not in payload:
                payload[k] = v
        return payload

    # ------------------------------------------------------------------
    # Streaming parser
    # ------------------------------------------------------------------
    @staticmethod
    def _json_dumps_compact(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    async def _emit_completed_tool_calls(
        self,
        pending: dict[int, dict[str, Any]],
        emitted: set[int],
        all_tool_calls: list[ToolCallInfo],
        *,
        only_before_index: Optional[int] = None,
    ) -> AsyncGenerator[StreamToolCallEvent, None]:
        for index in sorted(pending):
            if index in emitted:
                continue
            if only_before_index is not None and index >= only_before_index:
                continue
            blk = pending[index]
            args_str = blk.get("arguments") or "{}"
            try:
                args = json.loads(args_str)
            except Exception:
                args = {}
            call_id = blk.get("id") or f"call_{index}"
            name = blk.get("name") or ""
            tc = ToolCallInfo(call_id=call_id, tool_name=name, tool_arguments=args_str)
            all_tool_calls.append(tc)
            emitted.add(index)
            yield StreamToolCallEvent(call_id=call_id, tool_name=name, args=args, block_index=index)

    async def _consume_sse_response(
        self,
        response: httpx.Response,
    ) -> AsyncGenerator[Any, None]:
        text_parts: list[str] = []
        pending_tools: dict[int, dict[str, Any]] = {}
        emitted_tool_indexes: set[int] = set()
        all_tool_calls: list[ToolCallInfo] = []
        usage_in = usage_out = usage_total = 0
        stop_reason: Optional[str] = None

        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            usage = chunk.get("usage") or {}
            if usage:
                usage_in = usage.get("prompt_tokens") or usage.get("input_tokens") or usage_in
                usage_out = usage.get("completion_tokens") or usage.get("output_tokens") or usage_out
                usage_total = usage.get("total_tokens") or usage_total

            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    stop_reason = choice.get("finish_reason")
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield StreamTextDeltaEvent(text=content)

                for tc_delta in delta.get("tool_calls") or []:
                    index = int(tc_delta.get("index") or 0)
                    # In normal OpenAI streams, tool calls are sent by index in
                    # order. When a later index appears, earlier indexes are
                    # complete enough to dispatch before the final [DONE].
                    async for ev in self._emit_completed_tool_calls(
                        pending_tools,
                        emitted_tool_indexes,
                        all_tool_calls,
                        only_before_index=index,
                    ):
                        yield ev
                    blk = pending_tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tc_delta.get("id"):
                        blk["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        blk["name"] = fn["name"]
                    if fn.get("arguments"):
                        blk["arguments"] += fn["arguments"]

                if choice.get("finish_reason") in ("tool_calls", "stop", "length"):
                    async for ev in self._emit_completed_tool_calls(
                        pending_tools,
                        emitted_tool_indexes,
                        all_tool_calls,
                    ):
                        yield ev

        # Defensive flush in case a server ends with [DONE] and no finish_reason.
        async for ev in self._emit_completed_tool_calls(
            pending_tools,
            emitted_tool_indexes,
            all_tool_calls,
        ):
            yield ev

        text_content = "".join(text_parts) or None
        result = LLMChatResult(
            content=text_content,
            reasoning_content=None,
            thinking_blocks=[],
            tool_name=all_tool_calls[0].tool_name if all_tool_calls else None,
            tool_arguments=all_tool_calls[0].tool_arguments if all_tool_calls else None,
            tool_calls=all_tool_calls,
            token_usage=TokenUsage(
                input_tokens=usage_in,
                output_tokens=usage_out,
                # TokenUsage calculates total from input/output in most call sites;
                # usage_total is intentionally not stored because the model class
                # exposes separate concrete fields.
            ),
            stop_reason=stop_reason,
        )
        self.logger.debug(
            f"OpenAIStreaming response summary: text_len={len(text_content) if text_content else 0}, "
            f"tool_calls={len(all_tool_calls)}, stop_reason={stop_reason}, "
            f"tokens=in:{usage_in} out:{usage_out} total:{usage_total or (usage_in + usage_out)}",
            component="OpenAIStreamingService",
        )
        yield StreamDoneEvent(result=result)

    async def chat_stream(
        self,
        messages: list[Any],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        json_mode: bool = True,
        first_content: bool = True,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        reasoning_effort: Optional[Literal["low", "medium", "high"]] = None,
        thinking_budget_tokens: Optional[int] = None,
        effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = None,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Any] = None,
        stream_options: Optional[Any] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        model_name = model if model is not None else self.model
        temp = temperature if temperature is not None else self._default_temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens
        payload = self._build_payload(
            messages=messages,
            model=model_name,
            temperature=temp,
            max_tokens=max_tok,
            stop=stop,
            json_mode=json_mode,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
            effort=effort,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stream_options=stream_options,
            extra=kwargs,
        )
        self.logger.debug(
            f"OpenAIStreaming request: model={model_name}, messages={len(messages)}, "
            f"tools={len(tools) if tools else 0}"
            + (f", reasoning_effort={payload.get('reasoning_effort')}" if payload.get("reasoning_effort") else ""),
            component="OpenAIStreamingService",
        )
        _log_payload = {k: v for k, v in payload.items() if k != "messages"}
        self.logger.debug(
            f"OpenAIStreaming raw request:\n{json.dumps(_log_payload, indent=2, default=str)}",
            component="OpenAIStreamingService",
        )

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                async with self._client.stream("POST", self._url, json=payload) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        body = await resp.aread()
                        # Attach body text for classifiers/logging.
                        resp._content = body  # httpx-compatible cached content
                        raise e
                    async for event in self._consume_sse_response(resp):
                        yield event
                return
            except Exception as e:
                last_error = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if self._is_permanent_local_error(e):
                    self.logger.error(
                        f"OpenAIStreaming request failed (permanent local error, not retrying): "
                        f"{type(e).__name__}: {e}",
                        component="OpenAIStreamingService",
                    )
                    raise
                if self._is_prompt_too_long_error(e):
                    self.logger.error(
                        f"OpenAIStreaming request failed (prompt too long): {type(e).__name__}: {e}",
                        component="OpenAIStreamingService",
                    )
                    raise
                unsupported_param = _unsupported_parameter_from_error(e)
                if status == 400 and unsupported_param and unsupported_param in payload:
                    payload.pop(unsupported_param, None)
                    # Some reasoning gateways reject multiple sampling knobs; if
                    # temperature is rejected, proactively drop top_p as well.
                    if unsupported_param == "temperature":
                        payload.pop("top_p", None)
                    self.logger.warning(
                        f"OpenAIStreaming request rejected unsupported parameter '{unsupported_param}'; "
                        f"retrying once without it",
                        component="OpenAIStreamingService",
                    )
                    continue
                if status in (400, 401, 403, 404, 422) or self._is_client_error(e):
                    self.logger.error(
                        f"OpenAIStreaming request failed (client error, not retrying): "
                        f"{type(e).__name__}: {e}; body={_error_text(e)}",
                        component="OpenAIStreamingService",
                    )
                    raise
                if status == 429 or "rate limit" in str(e).lower():
                    retry_after_secs, limit_type = _parse_rate_limit_details(e)
                    if limit_type in _LONG_WINDOW_LIMIT_TYPES or retry_after_secs > 300.0:
                        self.mark_exhausted(retry_after_secs)
                    self.logger.warning(
                        f"OpenAIStreaming throttled (model={self.model}, limit_type={limit_type or 'unknown'}, "
                        f"retry_after={retry_after_secs:.0f}s); falling back: {type(e).__name__}: {e}",
                        component="OpenAIStreamingService",
                    )
                    raise
                if attempt < self.max_retries - 1:
                    wait_time = min(600, 2 ** (attempt + 1))
                    self.logger.warning(
                        f"OpenAIStreaming request failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {wait_time}s: {type(e).__name__}: {e}",
                        component="OpenAIStreamingService",
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
                        f"OpenAIStreaming request failed after {self.max_retries} retries: "
                        f"{type(e).__name__}: {e}",
                        component="OpenAIStreamingService",
                    )
        assert last_error is not None
        raise last_error

    async def close(self) -> None:
        await self._client.aclose()

    def _user_friendly_error(self, e: Exception) -> str:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status and 500 <= status < 600:
            return f"API server error (HTTP {status})"
        if status:
            return f"API error (HTTP {status})"
        if isinstance(e, httpx.TimeoutException):
            return "API request timed out"
        if isinstance(e, httpx.NetworkError):
            return "API connection failed"
        return f"Temporary error ({type(e).__name__})"
