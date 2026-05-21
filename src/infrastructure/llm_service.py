"""
LLM Service - Abstract Base Class
==================================
Defines the unified interface that all LLM client adapters must implement.

Design
-----------------
Subclass ``LLMService`` and implement the two abstract methods:

  * ``chat_stream()`` – open a streaming request, yield events, handle retries.
  * ``close()``       – release underlying client / connection-pool resources.

The ``_is_prompt_too_long_error()`` and ``_is_client_error()`` detectors are
provided as concrete methods because they are pure string operations
independent of any LLM backend.

JSON parsing / repair lives in ``Plan.from_data()`` and
``Decision.from_data()`` -- not here.

"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Literal, Optional
import anthropic as _anthropic
from .logger import get_logger
from ..models.token_usage import TokenUsage


@dataclass
class ToolCallInfo:
    """A single tool call from the LLM response."""
    call_id: str
    tool_name: str
    tool_arguments: str  # raw JSON string from function.arguments

    def tool_arguments_dict(self) -> dict:
        """Parse tool_arguments JSON string into a dict. Returns {} on failure."""
        try:
            return json.loads(self.tool_arguments)
        except Exception:
            return {}


@dataclass
class LLMChatResult:
    """Structured result returned by LLMService.chat().

    Extracts the key fields from the raw SDK response so callers never need
    to import or inspect provider-specific response objects.

    Fields
    ------
    content:
        Text content from ``message.content``.  ``None`` when the model
        issued a tool call without any accompanying text.
    reasoning_content:
        Thinking / reasoning content (e.g. Claude extended thinking).
        ``None`` when the model does not emit a separate reasoning field.
    tool_name / tool_arguments:
        Populated from the FIRST tool call when the model issues tool calls;
        both are ``None`` when the model replies with plain text.
        ``tool_arguments`` is the raw JSON string from ``function.arguments``.
    tool_calls:
        All tool calls issued by the model in this response (may be empty).
        When non-empty, tool_name/tool_arguments mirror tool_calls[0] for
        backward compatibility.
    token_usage:
        All token counts for this response as a :class:`TokenUsage` instance.
        Individual fields are also accessible as properties for backward
        compatibility: ``input_tokens``, ``output_tokens``, ``total_tokens``,
        ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
    """
    # Text content from message.content
    content: Optional[str] = None
    # Thinking/reasoning content (e.g. Claude extended thinking)
    reasoning_content: Optional[str] = None
    # Tool call fields — both present or both None (mirrors tool_calls[0] when present)
    tool_name: Optional[str] = None
    tool_arguments: Optional[str] = None   # raw JSON string from function.arguments
    # All tool calls in this response (parallel tool use)
    tool_calls: "list[ToolCallInfo]" = field(default_factory=list)
    # Token usage — all counts in one place
    token_usage: TokenUsage = field(default_factory=TokenUsage)

    # ── Backward-compat property accessors ───────────────────────────────────
    # These allow existing call sites to keep reading llm_result.input_tokens etc.

    @property
    def input_tokens(self) -> int:
        return self.token_usage.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.token_usage.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.token_usage.total_tokens

    @property
    def cache_creation_input_tokens(self) -> int:
        return self.token_usage.cache_creation_tokens

    @property
    def cache_read_input_tokens(self) -> int:
        return self.token_usage.cache_read_tokens

    @property
    def has_tool_call(self) -> bool:
        """Return True if the model issued at least one tool call."""
        return bool(self.tool_calls) or self.tool_name is not None

    @property
    def has_multiple_tool_calls(self) -> bool:
        """Return True if the model issued more than one tool call."""
        return len(self.tool_calls) > 1

    def tool_arguments_dict(self) -> dict:
        """Parse tool_arguments JSON string into a dict. Returns {} on failure."""
        if not self.tool_arguments:
            return {}
        try:
            return json.loads(self.tool_arguments)
        except Exception:
            return {}


class LLMService(ABC):
    """Abstract base class for LLM service adapters.

    Subclasses must implement :meth:`chat_stream` and :meth:`close`.

    Parameters
    ----------
    model:
        Default model identifier used when ``chat()`` is called without an
        explicit ``model`` argument.
    max_tokens:
        Default maximum number of tokens to generate.
    temperature:
        Default sampling temperature.
    max_retries:
        How many times a transient failure should be retried before
        propagating the exception.
    context_window:
        Maximum number of input tokens this model accepts.  Used by the
        Planner to decide when to compress step history.  Defaults to
        200 000 (conservative — safe for all current Anthropic models).
    """

    def __init__(
        self,
        model: str,
        max_tokens: int = 10240,
        temperature: float = 0.7,
        max_retries: int = 3,
        context_window: int = 200_000,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.context_window = context_window
        self.logger = get_logger()
        # Session-level exhaustion flag: set True when a long-duration rate limit
        # (tokens_per_day / retry_after > 5min) is hit. call_with_fallback skips
        # exhausted services for the remainder of the session.
        self._exhausted: bool = False

    # ------------------------------------------------------------------
    # Abstract interface – must be implemented by every concrete adapter
    # ------------------------------------------------------------------

    @abstractmethod
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
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Any] = None,
        stream_options: Optional[Any] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Any, None]:
        """Open a streaming request and return an async generator of events.

        All adapters yield the same three event types (imported from
        ``anthropic_streaming_service``):

          ``StreamTextDeltaEvent``  — each text chunk as it arrives
          ``StreamToolCallEvent``   — when a tool-call block completes
          ``StreamDoneEvent``       — final event carrying the full
                                      :class:`LLMChatResult`

        The generator must raise immediately (before yielding any event) for
        prompt-too-long and non-retryable 4xx errors so that
        :func:`call_with_fallback` can try the next service.  Errors that
        occur after the first event has been yielded are propagated as-is to
        the caller.

        Parameters
        ----------
        messages:
            Message list, e.g. ``[{"role": "user", "content": "..."}]``.
        model:
            Model name; overrides the instance default when provided.
        temperature:
            Sampling temperature; overrides the instance default when provided.
        max_tokens:
            Max tokens to generate; overrides the instance default when provided.
        stop:
            Stop sequences.
        json_mode:
            If ``True`` (default), instruct the model to return valid JSON.
        first_content:
            Ignored — kept for backward-compatible call sites.
        frequency_penalty:
            Frequency penalty (−2.0 to 2.0).  OpenAI only.
        presence_penalty:
            Presence penalty (−2.0 to 2.0).  OpenAI only.
        repetition_penalty:
            Repetition penalty.  Not supported by all providers.
        top_k:
            Top-K sampling.  Supported by Anthropic and some others.
        top_p:
            Top-P (nucleus) sampling.
        reasoning_effort:
            ``"low"`` | ``"medium"`` | ``"high"``.
        thinking_budget_tokens:
            Explicit token budget for Anthropic extended thinking.
        tools:
            Tool / function-calling definitions.
        tool_choice:
            Controls which tool the model calls.  Provider-specific format.
        response_format:
            Explicit response-format object (overrides ``json_mode``).
        stream_options:
            Provider-specific streaming options.
        **kwargs:
            Extra parameters forwarded to the underlying client.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release underlying client / connection-pool resources."""

    # ------------------------------------------------------------------
    # Concrete helpers – backend-agnostic, available to all adapters
    # ------------------------------------------------------------------


    def _is_prompt_too_long_error(self, error: Exception) -> bool:
        """Return ``True`` if *error* indicates the prompt exceeded the model's context limit.

        Covers error messages from:
          - QGenie / generic: "prompt is too long", "limit exceeded"
          - Anthropic SDK:    400 BadRequestError with "prompt is too long"
                              or context-window messages
        """
        error_str = str(error).lower()
        # Exclude rate-limit errors (429): "rate limit exceeded" contains "limit exceeded"
        # but is NOT a prompt-too-long error.
        if "rate limit" in error_str or "429" in error_str:
            return False
        return (
            "prompt is too long" in error_str
            or "limit exceeded" in error_str
            or "context_length_exceeded" in error_str
            or "context window" in error_str
            or ("400" in error_str and "too long" in error_str)
        )

    def _is_client_error(self, error: Exception) -> bool:
        """Return ``True`` if *error* is a non-retryable 4xx client error.

        These errors indicate a structurally invalid request (bad message
        format, auth failure, etc.) that will not succeed on retry.
        Adapters should raise immediately without retrying.
        """
        try:
            
            if isinstance(error, _anthropic.BadRequestError):
                return True
            if isinstance(error, _anthropic.AuthenticationError):
                return True
            if isinstance(error, _anthropic.PermissionDeniedError):
                return True
            if isinstance(error, _anthropic.NotFoundError):
                return True
            if isinstance(error, _anthropic.UnprocessableEntityError):
                return True
        except ImportError:
            pass
        error_str = str(error)
        # Generic HTTP status check for other providers
        for code in ("400", "401", "403", "404", "422"):
            if f"http_status={code}" in error_str or f"status code: {code}" in error_str:
                return True
        return False

