"""
LLM Pool utilities — stateless per-call fallback for prioritised model lists.

Design
------
Each component (Planner, Receptionist, RuntimeAgent) receives a *pre-sliced*
list of LLMService instances.  The slice defines the allowed model range for
that component/function.  Within a single call, services are tried in order
(index 0 = highest priority); if one fails the next is tried.  There is no
cross-call state — every call starts fresh from index 0.

All LLM calls go through streaming (chat_stream) internally.  call_with_fallback
collects the stream into a LLMChatResult transparently; call_with_fallback_stream
exposes the raw event stream to the caller.

Public API
----------
call_with_fallback(services, chat_kwargs, on_fallback=None)
    Async helper that tries each service in order and returns the first
    successful LLMChatResult.  Raises the last exception if all fail.
    Internally uses chat_stream(); fallback happens before the stream starts.

call_with_fallback_stream(services, chat_kwargs, on_fallback=None)
    Async generator that tries each service in order and yields stream events
    from the first service whose chat_stream() call succeeds.  Fallback only
    happens before the stream starts; mid-stream errors propagate to the caller.

make_from_data_services(all_services)
    Build the from_data slice: index 2+ if available, else last model only.
    Used for Plan.from_data / Decision.from_data which must never use the
    top-priority models (index 0 or 1).
"""
from typing import Any, AsyncGenerator, Callable, List, Optional

from .llm_service import LLMChatResult, LLMService
from .logger import get_logger

_logger = get_logger()


async def call_with_fallback(
    services: List[LLMService],
    chat_kwargs: dict,
    on_fallback: Optional[Callable[[int, Exception], None]] = None,
) -> LLMChatResult:
    """
    Try each LLM service in order; return the first successful LLMChatResult.

    Internally opens a stream via chat_stream() and collects the result.
    Fallback only happens before the stream starts — if chat_stream() raises
    before yielding any event, the next service is tried.  Mid-stream errors
    propagate immediately.

    Args:
        services:     Pre-sliced list of LLM services (index 0 = highest priority).
        chat_kwargs:  Keyword arguments forwarded verbatim to service.chat_stream().
        on_fallback:  Optional callback invoked as on_fallback(next_index, error)
                      just before advancing to the next service.

    Returns:
        LLMChatResult from the first service that succeeds.

    Raises:
        The last exception raised if every service in the list fails.
        ValueError if *services* is empty.

    Fast-fail behaviour:
        If a service raises a non-retryable client error (4xx), the exception is
        re-raised immediately without trying remaining services.  Prompt-too-long
        errors are NOT fast-failed here; they fall through to normal fallback so
        that a service with a larger context window can still succeed.
    """
    from .anthropic_streaming_service import StreamDoneEvent  # noqa: PLC0415

    if not services:
        raise ValueError("call_with_fallback: services list is empty")

    last_exc: Optional[Exception] = None
    for i, service in enumerate(services):
        if service._exhausted:
            _logger.debug(
                f"call_with_fallback: skipping session-exhausted service index {i} ({service.model})"
            )
            continue
        try:
            result: Optional[LLMChatResult] = None
            async for event in service.chat_stream(**chat_kwargs):
                if isinstance(event, StreamDoneEvent):
                    result = event.result
            if result is None:
                raise RuntimeError("chat_stream ended without a StreamDoneEvent")
            return result
        except Exception as exc:
            last_exc = exc

            # Non-retryable 4xx client error: re-raise immediately.
            if service._is_client_error(exc):
                _logger.warning(
                    f"call_with_fallback: non-retryable client error from service "
                    f"index {i}, not trying remaining services: {exc}",
                )
                raise

            if i < len(services) - 1:
                # More services to try — invoke callback and continue.
                if on_fallback is not None:
                    try:
                        on_fallback(i + 1, exc)
                    except Exception:
                        pass  # never let callback errors abort the retry loop
            else:
                # Last service also failed — will raise below.
                _logger.warning(
                    f"call_with_fallback: all {len(services)} service(s) failed; "
                    f"last error: {exc}",
                )

    # All services failed or were skipped — re-raise the last exception.
    if last_exc is None:
        raise RuntimeError("All configured LLM services are session-exhausted (rate limit)")
    raise last_exc  # type: ignore[misc]


async def call_with_fallback_stream(
    services: List[LLMService],
    chat_kwargs: dict,
    on_fallback: Optional[Callable[[int, Exception], None]] = None,
) -> AsyncGenerator[Any, None]:
    """
    Try each LLM service in order and yield stream events from the first that
    successfully opens a stream.

    Fallback policy
    ---------------
    Fallback only happens *before* the stream starts — i.e. if
    service.chat_stream() raises before yielding any events.  Once the first
    event has been yielded, the stream is considered "open" and any subsequent
    error is re-raised directly to the caller.  The caller should treat such
    mid-stream errors as observations for the next agent iteration rather than
    retrying the same request on a different service.

    Args:
        services:     Pre-sliced list of LLM services (index 0 = highest priority).
        chat_kwargs:  Keyword arguments forwarded verbatim to service.chat_stream().
        on_fallback:  Optional callback invoked as on_fallback(next_index, error)
                      just before advancing to the next service.

    Yields:
        Stream events from the first service that successfully opens a stream.

    Raises:
        The last open-stream exception if every service fails to open.
        ValueError if *services* is empty.
        Any mid-stream exception from the chosen service (re-raised as-is).
    """
    if not services:
        raise ValueError("call_with_fallback_stream: services list is empty")

    last_exc: Optional[Exception] = None

    for i, service in enumerate(services):
        if service._exhausted:
            _logger.debug(
                f"call_with_fallback_stream: skipping session-exhausted service index {i} ({service.model})"
            )
            continue
        try:
            gen = service.chat_stream(**chat_kwargs)
            # Fetch the first event to confirm the stream opened successfully.
            # If this raises, we can still fall back to the next service.
            first_event = await gen.__anext__()
        except StopAsyncIteration:
            # Empty stream — treat as success with no events.
            return
        except Exception as exc:
            last_exc = exc

            if service._is_client_error(exc):
                _logger.warning(
                    f"call_with_fallback_stream: non-retryable client error from "
                    f"service index {i}, not trying remaining services: {exc}",
                )
                raise

            if i < len(services) - 1:
                if on_fallback is not None:
                    try:
                        on_fallback(i + 1, exc)
                    except Exception:
                        pass
                continue
            else:
                _logger.warning(
                    f"call_with_fallback_stream: all {len(services)} service(s) "
                    f"failed to open stream; last error: {exc}",
                )
                raise

        # Stream opened — yield first event then the rest; mid-stream errors propagate.
        yield first_event
        async for event in gen:
            yield event
        return

    raise last_exc  # type: ignore[misc]


def make_from_data_services(all_services: List[LLMService]) -> List[LLMService]:
    """
    Return the from_data slice of the global model list.

    Rule: use index 2+ (never the top-priority models at index 0 or 1).
    If fewer than 3 models are configured, fall back to the last model only
    so that from_data always has at least one service to call.

    Args:
        all_services: The full, priority-ordered list of LLM services.

    Returns:
        A non-empty list starting at index 2, or [all_services[-1]].
    """
    if not all_services:
        raise ValueError("make_from_data_services: all_services is empty")
    if len(all_services) >= 3:
        return all_services[2:]
    return all_services[-1:]
