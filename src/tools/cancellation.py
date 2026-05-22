"""Cooperative cancellation primitives for tools.

Cross-platform: pure asyncio + threading. No platform-conditional code in
this module — every primitive maps to semantics that behave identically on
Windows and POSIX. Platform-specific cleanup (taskkill / killpg, paramiko
transport.close, etc.) lives in the tool that owns the resource.

Design principles
-----------------
* The user's promise is "click stop, every tool stops within shutdown_deadline
  seconds, regardless of how long the work was supposed to take." We never
  impose a wall-clock cap on legitimate long work — we only bound how long a
  tool gets to comply with a stop request.
* Python on Windows cannot kill a thread. Once a tool's blocking syscall
  starts, the only way to abort it is by side-effect: close the underlying
  fd / socket / process from another thread. Each tool registers its
  side-effect-abort callback via AbortHandle.register; the engine fires
  the handle to trigger every callback simultaneously.
* asyncio.Event is not thread-safe to await on from another thread. Tools
  that run blocking work on an executor thread therefore use a
  threading.Event mirror (ThreadInterruptToken). The bridge_asyncio_to_thread
  helper sets up the mirror and tears it down with the executor task.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, List, Optional


class AbortHandle:
    """Registry of side-effect-abort callbacks for one tool invocation.

    A tool registers cleanup functions during execute() (e.g.
    paramiko.SSHClient.close, subprocess.terminate). The engine calls
    .fire() to invoke every callback; this is intended to be called from
    a thread that is NOT the executor thread running the tool, so it can
    abort an in-flight blocking syscall from outside.

    Idempotent: .fire() is safe to call multiple times — subsequent calls
    are no-ops. Late registration (after fire) is silently ignored, which
    is the pragmatic choice: the tool registering late had clearly never
    held the resource anyway.

    Thread-safe in both directions: register() may be called from the
    executor thread while fire() runs concurrently from the asyncio
    thread. Each callback is invoked exactly once even under contention.
    """

    __slots__ = ("_callbacks", "_fired", "_lock")

    def __init__(self) -> None:
        self._callbacks: List[Callable[[], None]] = []
        self._fired: bool = False
        self._lock = threading.Lock()

    def register(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._fired:
                # We've already aborted; fn would never be invoked. Drop it
                # rather than queue it for a fire that will never happen.
                return
            self._callbacks.append(fn)

    def fire(self) -> None:
        with self._lock:
            if self._fired:
                return
            self._fired = True
            cbs = list(self._callbacks)
            self._callbacks.clear()
        # Run callbacks OUTSIDE the lock so a slow callback can't block
        # late registers from another thread (though they'd be rejected
        # by is_fired anyway).
        for cb in cbs:
            try:
                cb()
            except Exception:
                # Abort path must never raise — we're already in shutdown.
                pass

    @property
    def is_fired(self) -> bool:
        with self._lock:
            return self._fired


class ThreadInterruptToken:
    """A threading.Event-shaped cancellation token usable from blocking
    code on an executor thread.

    Use in place of time.sleep(N):

        if token.wait(retry_delay):
            raise InterruptedError("aborted")

    .wait(timeout) returns True if the token was set during the wait,
    False on full timeout. .is_set() is a non-blocking probe that's
    appropriate for inserting between operations of an interruptible
    chunked read.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float]) -> bool:
        return self._event.wait(timeout)


def bridge_asyncio_to_thread(
    asyncio_event: Optional[asyncio.Event],
) -> "tuple[ThreadInterruptToken, Optional[asyncio.Task]]":
    """Mirror an asyncio.Event into a thread-safe ThreadInterruptToken.

    Returns (token, watcher_task). The caller MUST cancel watcher_task
    (and ideally await it) when the executor work finishes, so the
    watcher does not outlive its purpose.

    If *asyncio_event* is None, returns (token, None) — the token is
    inert and .wait/.is_set will never report set, but the API remains
    uniform so callers don't need to branch.
    """
    token = ThreadInterruptToken()
    if asyncio_event is None:
        return token, None

    async def _bridge() -> None:
        try:
            await asyncio_event.wait()
        except asyncio.CancelledError:
            return
        token.set()

    return token, asyncio.create_task(_bridge(), name="interrupt-bridge")


async def run_with_abort(
    blocking_fn: Callable[[], Any],
    *,
    interrupt_event: Optional[asyncio.Event] = None,
    abort: Optional[AbortHandle] = None,
    shutdown_deadline: float = 5.0,
) -> Any:
    """Run *blocking_fn* on the default executor, with cancellation safety.

    Behavior:
      * Starts *blocking_fn* in a thread (loop.run_in_executor).
      * Mirrors *interrupt_event* into a ThreadInterruptToken so blocking
        code can poll it. The token is exposed by setting it on a
        thread-local that helpers in the calling tool can read; tools
        that don't use the helpers simply ignore it.
      * If the surrounding asyncio task is cancelled (e.g. by
        new_session in the bridge), this coroutine:
          1. Fires *abort* (if provided) so registered cleanup callbacks
             side-effect-abort the blocking work — the only way to wake
             a thread parked in a blocking syscall on Windows.
          2. Sets the interrupt token so the blocking code, if it polls,
             can self-abort at its next check point.
          3. Re-raises CancelledError after a bounded grace window. The
             executor thread is NOT killed (Python cannot do that on any
             platform); it eventually returns and is reaped by the
             default executor's thread pool. The asyncio side does not
             wait for it.

    The result of *blocking_fn* is returned on success. On hard timeout
    after a fired abort, asyncio.TimeoutError is raised; on cancellation,
    asyncio.CancelledError is raised.
    """
    loop = asyncio.get_event_loop()
    token, watcher = bridge_asyncio_to_thread(interrupt_event)

    # Stash the interrupt token on a thread-local so helpers like
    # interruptible_sleep can find it without changing every tool's
    # function signature.
    fut = loop.run_in_executor(
        None, _run_blocking_with_token, blocking_fn, token,
    )
    try:
        # asyncio.shield is required: a bare `await fut` would propagate
        # a cancel from the surrounding task into fut, marking it
        # cancelled before our abort/token cleanup runs. The executor
        # thread keeps running but we never see its result. shield
        # decouples the wait from fut's lifetime — the cancel raises
        # CancelledError out of the wait, fut stays pending, and we
        # can drive the proper shutdown sequence below.
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # Engine asked us to stop. Side-effect-abort first (closes
        # sockets / fds / kills procs); the executor thread will then
        # error out and the future will resolve. We bound how long we
        # wait for it.
        if abort is not None:
            try:
                abort.fire()
            except Exception:
                pass
        token.set()
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=shutdown_deadline)
        except (asyncio.TimeoutError, Exception):
            # Orphan: the executor thread is still alive but we're done
            # waiting. Caller is responsible for any further isolation
            # (generation tag in the bridge, etc.).
            pass
        raise
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()


# ---------------------------------------------------------------------------
# Thread-local interrupt token — lets sync helpers (interruptible_sleep, etc.)
# pick up the current invocation's token without changing every tool function
# signature. Tools that prefer to pass the token explicitly may do so; the
# thread-local is purely a convenience.
# ---------------------------------------------------------------------------

_threadlocal = threading.local()


def _run_blocking_with_token(fn: Callable[[], Any], token: ThreadInterruptToken) -> Any:
    """Runner planted on the executor thread that installs *token* on the
    thread-local before calling *fn*, and removes it after."""
    prev = getattr(_threadlocal, "interrupt", None)
    _threadlocal.interrupt = token
    try:
        return fn()
    finally:
        _threadlocal.interrupt = prev


def current_interrupt() -> Optional[ThreadInterruptToken]:
    """Return the ThreadInterruptToken installed on this executor thread,
    or None if the calling thread is not currently running under
    run_with_abort. Helpers like interruptible_sleep call this.
    """
    return getattr(_threadlocal, "interrupt", None)


def interruptible_sleep(seconds: float) -> bool:
    """Sleep up to *seconds*, returning early if the current thread's
    interrupt token is set.

    Returns True if interrupted, False if the full duration elapsed. The
    return value mirrors threading.Event.wait so callers can write:

        if interruptible_sleep(retry_delay):
            raise InterruptedError("aborted during retry backoff")

    If the current thread has no token (i.e. the caller didn't go through
    run_with_abort), this falls back to a plain time.sleep — fully
    backwards-compatible for tools that haven't been wired up yet.
    """
    if seconds <= 0:
        return False
    token = current_interrupt()
    if token is None:
        # Slow path: not running under run_with_abort. Use the bare sleep
        # so existing call sites don't change behavior.
        import time as _time
        _time.sleep(seconds)
        return False
    return token.wait(seconds)


def current_abort() -> Optional[AbortHandle]:
    """Return the AbortHandle installed on this executor thread, or None.

    Tools that allocate cancellable resources (paramiko clients, sockets)
    register a close callback here so the bridge can side-effect-abort
    the blocking syscall from another thread.
    """
    return getattr(_threadlocal, "abort", None)


def _run_blocking_with_token_and_abort(
    fn: Callable[[], Any],
    token: ThreadInterruptToken,
    abort: AbortHandle,
) -> Any:
    """Like _run_blocking_with_token but also installs an AbortHandle."""
    prev_int = getattr(_threadlocal, "interrupt", None)
    prev_abort = getattr(_threadlocal, "abort", None)
    _threadlocal.interrupt = token
    _threadlocal.abort = abort
    try:
        return fn()
    finally:
        _threadlocal.interrupt = prev_int
        _threadlocal.abort = prev_abort


async def run_with_abort_handle(
    blocking_fn: Callable[[], Any],
    *,
    interrupt_event: Optional[asyncio.Event] = None,
    shutdown_deadline: float = 5.0,
) -> Any:
    """Variant of run_with_abort that allocates a fresh AbortHandle and
    exposes it to the blocking function via current_abort() on the
    executor thread.

    This is the variant tools that use side-effect-abort (ssh_tool's
    client.close registration) should call — they don't need to manage
    the AbortHandle's lifetime themselves.
    """
    loop = asyncio.get_event_loop()
    token, watcher = bridge_asyncio_to_thread(interrupt_event)
    abort = AbortHandle()

    fut = loop.run_in_executor(
        None, _run_blocking_with_token_and_abort, blocking_fn, token, abort,
    )
    try:
        # asyncio.shield required — see run_with_abort for the full
        # explanation. Without shield, a task cancel from the engine
        # would race with our cleanup and either drop the result of
        # blocking_fn or skip the abort.fire entirely.
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        try:
            abort.fire()
        except Exception:
            pass
        token.set()
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=shutdown_deadline)
        except (asyncio.TimeoutError, Exception):
            pass
        raise
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
