"""
LLM Pool utilities — fallback for prioritised model lists with built-in
local-network outage handling.

Design
------
Each component (Orchestrator, PersistentAgent, background workers, …)
receives a *pre-sliced* list of LLMService instances.  Within a single call,
services are tried in order (index 0 = highest priority); if one fails
the next is tried.  When **every** service in the slice fails AND the
last error looks like a connectivity failure, the pool runs a single TCP
probe to the LLM endpoint host and chooses one of two outcomes:

  • Probe succeeds → external service issue.  Re-raise the underlying
    error unchanged so the caller fails fast.

  • Probe fails → local network is down.  Two policies:

      ``wait_on_network_down=True`` (default) — Pause and reprobe on a
      triangular cycle (30s, 300s, 600s, 1800s, 3600s, 1800s, 600s,
      300s, 30s, then repeat). When the host comes back, retry the
      original call from ``services[0]``. Used by long-running callers
      (Orchestrator, Agent, background workers).
      :class:`asyncio.CancelledError` propagates through the wait, so
      cancelling the asyncio task (e.g. when the user starts a fresh
      session) ends the wait cleanly.

      ``wait_on_network_down=False`` — Raise
      :class:`NetworkUnavailableError` immediately so the caller can
      fail fast.  Used where the user is staring at the cursor and
      indefinite waiting would be worse than skipping.

Probe state (cache + lock + last reachability) is module-level so
concurrent callers across roles share the same view of network health.
``is_network_down()`` exposes the most-recent probe result for callers
that want to short-circuit before even trying the LLM.

Public API
----------
``call_with_fallback(services, chat_kwargs, on_fallback=None, *,
                     on_service_selected=None, wait_on_network_down=True,
                     on_network_event=None)``
``call_with_fallback_stream(services, chat_kwargs, on_fallback=None, *,
                            on_service_selected=None, wait_on_network_down=True,
                            on_network_event=None)``
``is_network_down() -> bool``
``NetworkUnavailableError``
``make_from_data_services(all_services)``
``set_fallback_notifier(cb)``
``set_network_event_notifier(cb)``
"""
import asyncio
import time
from typing import Any, AsyncGenerator, Callable, List, Optional, Tuple
from urllib.parse import urlparse

from .llm_service import LLMChatResult, LLMService
from .logger import get_logger

_logger = get_logger()


class NetworkUnavailableError(Exception):
    """Raised by ``call_with_fallback*`` when the LLM endpoint host is
    unreachable AND the caller passed ``wait_on_network_down=False``.

    The original LLM error is preserved on ``__cause__`` so callers can
    surface diagnostic detail.  Callers that pass the default
    ``wait_on_network_down=True`` will never observe this exception —
    the wrapper waits forever instead.
    """


# ── Network probe (shared state across all callers) ─────────────────────────
#
# Probe target = (host, port) parsed from services[0]._base_url.  All
# services in a single call list typically share the same host (e.g. one
# YOUR-AI-ENDPOINT gateway proxying many models), so we just probe whichever target
# the first service exposes.

_PROBE_TIMEOUT: float = 1.0      # per-attempt TCP connect timeout
_PROBE_TTL: float = 5.0          # cache TTL — coalesces probe storms

# Wait schedule used by ``_wait_for_network``.
#
# A single triangular cycle that climbs from short to long and back
# down: 30s → 300s → 600s → 1800s → 3600s → 1800s → 600s → 300s → 30s,
# then repeats forever. Each cycle ≈ 2h31m of wall-clock waiting.
#
# Why triangular instead of monotone exponential?
#   • The descending half re-introduces short probes within every cycle,
#     so the *average* recovery-detection latency stays low even during
#     a multi-hour outage.
#   • Adjacent 30s probes at the wraparound (end of one cycle / start of
#     the next) give a quick cluster of checks every ~2.5h to catch a
#     network that came back during the long-wait phase.
#
# Probe count over a sustained 24h outage: ~9.5 cycles × 9 probes ≈ 85,
# total TCP work ≈ 85s.  Negligible.

_WAIT_CYCLE: List[int] = [30, 300, 600, 1800, 3600, 1800, 600, 300, 30]

_probe_lock: asyncio.Lock = asyncio.Lock()
_probe_status: Optional[bool] = None    # None = not probed yet, True = up, False = down
_probe_at: float = 0.0

# Module-level hook called by the bridge to surface service-fallback events
# in the renderer UI.  Set once by stdio_bridge._ensure_flow via
# set_fallback_notifier(); None in tests / non-bridge contexts.
# Signature: (from_model: str, to_model: str, exc: Exception) -> None
_fallback_notifier: Optional[Callable[[str, str, Exception], None]] = None


def set_fallback_notifier(
    cb: Optional[Callable[[str, str, Exception], None]],
) -> None:
    """Register (or clear) a bridge hook for service-fallback notifications."""
    global _fallback_notifier
    _fallback_notifier = cb


# Module-level hook for network-state changes ("down" / "waiting" / "restored").
# Wired by stdio_bridge._ensure_flow; fires for ALL roles (Orchestrator,
# Agent) so the renderer can show a single, coherent offline banner.
# Signature: (state: str, attempt: int, sleep_secs: int) -> None
_network_event_notifier: Optional[Callable[[str, int, int], None]] = None


def set_network_event_notifier(
    cb: Optional[Callable[[str, int, int], None]],
) -> None:
    """Register (or clear) the bridge hook for network-state notifications."""
    global _network_event_notifier
    _network_event_notifier = cb


def is_network_down() -> bool:
    """Return ``True`` iff the most recent probe failed.

    Returns ``False`` before any probe has run, so callers don't get
    false negatives during cold start.  The InteractionManager queries
    this to decide whether to invoke the coordinator's INTENT call at all
    when a new user message arrives — short-circuiting saves ~the SDK's
    per-service timeout (multiple seconds) per failed evaluation.
    """
    return _probe_status is False


def _service_target(services: List[LLMService]) -> Optional[Tuple[str, int]]:
    """Pick a (host, port) probe target from the first service's base_url.

    Returns None when no service exposes ``_base_url`` — in that case the
    pool can't tell whether the network is down, so it falls back to the
    pre-existing "raise the last exception" behaviour.
    """
    for s in services:
        url = getattr(s, "_base_url", None)
        if not url:
            continue
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
        except Exception:
            continue
        host = parsed.hostname
        if not host:
            continue
        port = parsed.port or (443 if parsed.scheme != "http" else 80)
        return host, port
    return None


async def _probe(target: Tuple[str, int], *, force: bool = False) -> bool:
    """Return True iff *target* accepts a TCP connection.

    The result is cached across callers for ``_PROBE_TTL`` seconds — a
    coordinator call and an agent call hitting the same outage at the
    same instant share one probe rather than racing.  ``force=True``
    bypasses the cache, used by the wait loop so each retry round gets a
    fresh reading.
    """
    global _probe_status, _probe_at
    async with _probe_lock:
        now = time.monotonic()
        if (
            not force
            and _probe_status is not None
            and (now - _probe_at) < _PROBE_TTL
        ):
            return _probe_status

        host, port = target
        ok = False
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_PROBE_TIMEOUT,
            )
            ok = True
        except Exception:
            ok = False
        finally:
            if writer is not None:
                try:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                except Exception:
                    pass

        _probe_status = ok
        _probe_at = now
        if not ok:
            _logger.warning(
                f"llm_pool probe FAILED: {host}:{port} unreachable",
                component="llm_pool",
            )
        return ok


def _emit_network_event(
    cb: Optional[Callable[[str, int, int], None]],
    state: str,
    attempt: int,
    sleep_secs: int,
) -> None:
    """Invoke *cb* and the module-level notifier defensively.

    UI hooks must never abort the retry loop, so every call is wrapped in
    try/except.  Both the per-call callback *cb* and the module-level
    ``_network_event_notifier`` (wired by the bridge) are fired so that
    all roles (Orchestrator, Agent) surface network events to the
    renderer without each caller having to pass its own callback.
    """
    for hook in (cb, _network_event_notifier):
        if hook is None:
            continue
        try:
            hook(state, attempt, sleep_secs)
        except Exception:
            _logger.debug(
                f"network_event hook raised for state={state!r}; ignoring",
                component="llm_pool",
            )


async def _wait_for_network(
    target: Tuple[str, int],
    *,
    on_network_event: Optional[Callable[[str, int, int], None]],
) -> None:
    """Block until *target* is reachable, walking ``_WAIT_CYCLE`` forever.

    Each cycle climbs short → long → short
    (30s, 300s, 600s, 1800s, 3600s, 1800s, 600s, 300s, 30s) so the
    descending half periodically re-introduces fast probes — recovery
    detection stays bounded even during multi-hour outages.

    :class:`asyncio.CancelledError` propagates through ``asyncio.sleep``,
    so cancelling the task (e.g. ``:new`` tearing down the old
    FlowController) ends the wait cleanly without any explicit interrupt
    mechanism.
    """
    attempt = 0
    while True:
        for interval in _WAIT_CYCLE:
            attempt += 1
            _emit_network_event(on_network_event, "waiting", attempt, interval)
            await asyncio.sleep(interval)
            if await _probe(target, force=True):
                return


def _is_network_error(services: List[LLMService], exc: BaseException) -> bool:
    """True iff the first service classifies *exc* as a connectivity failure.

    ``_is_likely_network_error`` is defined on the base class and identical
    across adapters, so picking the first service is sufficient.
    """
    if not services:
        return False
    return services[0]._is_likely_network_error(exc)


async def _handle_network_failure(
    services: List[LLMService],
    exc: BaseException,
    *,
    wait_on_network_down: bool,
    on_network_event: Optional[Callable[[str, int, int], None]],
) -> None:
    """Run the probe-and-(maybe-wait) sequence after fallback exhausted.

    Returns when the caller should retry from ``services[0]``.  Raises
    in every other case (wrong error class, no probe target, network is
    fine but service-side failed, or wait was disabled and network is
    down).
    """
    if not _is_network_error(services, exc):
        raise exc
    target = _service_target(services)
    if target is None:
        # No way to distinguish local outage from service outage — fail
        # fast as the original wrapper would.
        raise exc
    # Use the cached probe (5s TTL) here so a stampede of N concurrent
    # callers all hitting the same outage shares ONE TCP probe rather
    # than launching N. Within the wait loop below we deliberately
    # force=True per round because each round IS the next observation.
    if await _probe(target):
        # Local network reachable → service-side problem; waiting won't help.
        raise exc

    _logger.warning(
        f"llm_pool: local network appears down ({type(exc).__name__})",
        component="llm_pool",
    )
    _emit_network_event(on_network_event, "down", 0, 0)

    if not wait_on_network_down:
        raise NetworkUnavailableError(
            "Local network unavailable (LLM endpoint unreachable)"
        ) from exc

    await _wait_for_network(target, on_network_event=on_network_event)
    _logger.info("llm_pool: network restored, retrying", component="llm_pool")
    _emit_network_event(on_network_event, "restored", 0, 0)


# ── Fallback wrappers ───────────────────────────────────────────────────────


def _next_live_service(
    services: List[LLMService], after: int,
) -> Optional[Tuple[int, LLMService]]:
    """Index+service of the first non-exhausted service after *after*.

    ``on_fallback`` / ``_fallback_notifier`` previously always announced
    ``services[i + 1]`` as the destination, which became wrong the moment
    exhausted services were actually being skipped — the UI would name a model
    that never got a request. Returns None when nothing usable remains.
    """
    for j in range(after + 1, len(services)):
        if not services[j].is_exhausted():
            return j, services[j]
    return None


async def _try_all_services(
    services: List[LLMService],
    chat_kwargs: dict,
    on_fallback: Optional[Callable[[int, Exception], None]],
    on_service_selected: Optional[Callable[["LLMService"], None]] = None,
) -> LLMChatResult:
    """One pass through every service.  Returns the first success;
    raises the last error on full exhaustion; fast-fails on 4xx.
    """
    from .anthropic_streaming_service import StreamDoneEvent  # noqa: PLC0415

    if not services:
        raise ValueError("call_with_fallback: services list is empty")

    last_exc: Optional[Exception] = None
    for i, service in enumerate(services):
        if service.is_exhausted():
            _logger.debug(
                f"call_with_fallback: skipping session-exhausted service "
                f"index {i} ({service.model})"
            )
            continue
        try:
            result: Optional[LLMChatResult] = None
            async for event in service.chat_stream(**chat_kwargs):
                if isinstance(event, StreamDoneEvent):
                    result = event.result
            if result is None:
                raise RuntimeError("chat_stream ended without a StreamDoneEvent")
            if on_service_selected is not None:
                try:
                    on_service_selected(service)
                except Exception:
                    pass
            return result
        except Exception as exc:
            last_exc = exc
            if service._is_client_error(exc):
                _logger.warning(
                    f"call_with_fallback: non-retryable client error from "
                    f"service index {i}, not trying remaining services: {exc}",
                )
                raise
            nxt = _next_live_service(services, i)
            if nxt is not None:
                next_i, next_svc = nxt
                if on_fallback is not None:
                    try:
                        on_fallback(next_i, exc)
                    except Exception:
                        pass
                if _fallback_notifier is not None:
                    try:
                        _fallback_notifier(service.model, next_svc.model, exc)
                    except Exception:
                        pass
            else:
                _logger.warning(
                    f"call_with_fallback: all {len(services)} service(s) failed; "
                    f"last error: {exc}",
                )

    if last_exc is None:
        raise RuntimeError(
            "All configured LLM services are session-exhausted (rate limit)"
        )
    raise last_exc


async def call_with_fallback(
    services: List[LLMService],
    chat_kwargs: dict,
    on_fallback: Optional[Callable[[int, Exception], None]] = None,
    *,
    on_service_selected: Optional[Callable[["LLMService"], None]] = None,
    wait_on_network_down: bool = True,
    on_network_event: Optional[Callable[[str, int, int], None]] = None,
) -> LLMChatResult:
    """Try each service in order; on full network-class exhaustion, probe
    the LLM host and either pause-retry or fail fast based on
    ``wait_on_network_down``.

    Args:
        services:        Pre-sliced list of LLM services (priority order).
        chat_kwargs:     Forwarded verbatim to ``service.chat_stream``.
        on_fallback:     ``(next_index, error) -> None`` callback fired
                         before each within-pass fallback step.
        on_service_selected: called with the service instance once a
                         result is actually produced. Mirrors
                         :func:`call_with_fallback_stream`'s callback of the
                         same name — lets callers learn which service in
                         the fallback chain actually served this call
                         (e.g. for per-model token accounting).
        wait_on_network_down: When True (default), the wrapper sleeps
                         with exponential backoff until the LLM host
                         comes back, then retries from ``services[0]``.
                         When False, raises
                         :class:`NetworkUnavailableError` so the caller
                         can short-circuit.
        on_network_event: ``(state, attempt, sleep_secs) -> None`` UI hook
                         where ``state ∈ {"down", "waiting", "restored"}``.

    Raises:
        :class:`NetworkUnavailableError`: only when
            ``wait_on_network_down=False`` and the LLM host is unreachable.
        4xx client errors: re-raised immediately without fallback.
        The last underlying error: when failure is service-side, not
            network-side.
        :class:`asyncio.CancelledError`: propagates through the wait.
    """
    while True:
        try:
            return await _try_all_services(services, chat_kwargs, on_fallback, on_service_selected)
        except Exception as exc:
            await _handle_network_failure(
                services, exc,
                wait_on_network_down=wait_on_network_down,
                on_network_event=on_network_event,
            )
            # _handle_network_failure either raises or returns ("retry").
            # loop and retry from services[0]


async def call_with_fallback_stream(
    services: List[LLMService],
    chat_kwargs: dict,
    on_fallback: Optional[Callable[[int, Exception], None]] = None,
    *,
    on_service_selected: Optional[Callable[["LLMService"], None]] = None,
    wait_on_network_down: bool = True,
    on_network_event: Optional[Callable[[str, int, int], None]] = None,
) -> AsyncGenerator[Any, None]:
    """Streaming counterpart to :func:`call_with_fallback`.

    Network-aware pause-retry triggers ONLY before the stream opens.
    Once the first event has been yielded the stream is "live" and any
    mid-stream error propagates unchanged — replaying half a stream onto
    the caller would corrupt event ordering and token accounting.

    Args/Raises mirror :func:`call_with_fallback`.

    on_service_selected: called with the service instance once the stream
        opens successfully. Allows callers to adapt (e.g. update context
        budget) based on which model is actually serving.
    """
    if not services:
        raise ValueError("call_with_fallback_stream: services list is empty")

    while True:
        last_exc: Optional[Exception] = None
        first_event: Any = None
        gen = None

        for i, service in enumerate(services):
            if service.is_exhausted():
                _logger.debug(
                    f"call_with_fallback_stream: skipping session-exhausted "
                    f"service index {i} ({service.model})"
                )
                continue
            try:
                gen = service.chat_stream(**chat_kwargs)
                first_event = await gen.__anext__()
            except StopAsyncIteration:
                # Empty stream — treat as success with no events.
                if on_service_selected is not None:
                    try:
                        on_service_selected(service)
                    except Exception:
                        pass
                return
            except Exception as exc:
                last_exc = exc
                gen = None
                if service._is_client_error(exc):
                    _logger.warning(
                        f"call_with_fallback_stream: non-retryable client error "
                        f"from service index {i}, not trying remaining services: {exc}",
                    )
                    raise
                nxt = _next_live_service(services, i)
                if nxt is not None:
                    next_i, next_svc = nxt
                    if on_fallback is not None:
                        try:
                            on_fallback(next_i, exc)
                        except Exception:
                            pass
                    # The streaming path never fired the bridge's fallback
                    # notifier, so the renderer's "↪ X failed; trying Y" bubble
                    # only ever appeared for non-streaming calls — i.e. never
                    # for the agent's own think/act loop, which is exactly where
                    # the user needs to see a model switch.
                    if _fallback_notifier is not None:
                        try:
                            _fallback_notifier(service.model, next_svc.model, exc)
                        except Exception:
                            pass
                    continue
                _logger.warning(
                    f"call_with_fallback_stream: all {len(services)} service(s) "
                    f"failed to open stream; last error: {exc}",
                )
                # All services failed at open — drop out of the loop so the
                # network handler runs.
                break
            else:
                # Stream opened successfully.
                if on_service_selected is not None:
                    try:
                        on_service_selected(service)
                    except Exception:
                        pass
                break

        if gen is not None and first_event is not None:
            yield first_event
            async for event in gen:
                yield event
            return

        # Every service failed before yielding any event.  Run the
        # network handler; on retry, restart from services[0].
        if last_exc is None:
            raise RuntimeError(
                "All configured LLM services are session-exhausted (rate limit)"
            )
        await _handle_network_failure(
            services, last_exc,
            wait_on_network_down=wait_on_network_down,
            on_network_event=on_network_event,
        )
        # _handle_network_failure either raised (propagated up) or returned (retry).


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
