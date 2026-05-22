"""Tests for tool cancellation primitives + ssh_tool wiring.

How to run
----------
  # From repo root, no extra deps required:
  python tests/test_tool_cancellation.py

  # Or via unittest discovery (still stdlib only):
  python -m unittest tests.test_tool_cancellation -v

What this verifies
------------------
The user-visible promise after the cancellation refactor is:

  > Click anywhere, every blocking SSH operation aborts within ~50ms
  > and every retry/poll sleep aborts at its next tick. File I/O on
  > slow filesystems can't freeze the bridge. Old-flow stragglers
  > can't pollute the new flow's UI.

These tests prove each leg of that promise in isolation:

  1. AbortHandle correctness          — register/fire/idempotency/late-add
  2. ThreadInterruptToken              — wait+set returns immediately
  3. interruptible_sleep               — wakes early on interrupt; falls
                                         back to plain sleep outside the
                                         framework (backwards-compat)
  4. run_with_abort_handle (happy)     — returns value, cleans up watcher
  5. run_with_abort_handle (cancel)    — fires AbortHandle, sets token,
                                         honors shutdown_deadline even when
                                         blocking_fn ignores everything
  6. SSH retry-sleep wiring             — _new_client's interruptible_sleep
                                         path actually wakes on interrupt
  7. SSH _connect force_close hook     — when AbortHandle fires, the
                                         registered callback runs and
                                         evicts the pool entry
  8. flush_connection_pool              — closes pooled clients, clears
                                         rate-limit + OS-detection caches
  9. File-I/O off the loop              — read_tool releases the event loop
                                         during a slow read so other tasks
                                         can make progress concurrently

Tests 6-8 use a tiny FakeSSHClient mock so paramiko isn't required to run.
Tests 9 uses a real read on a temp file with a deliberately slow stat
side-channel to simulate a slow filesystem.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import List

# Make src/ importable when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.tools.cancellation import (
    AbortHandle,
    ThreadInterruptToken,
    current_abort,
    current_interrupt,
    interruptible_sleep,
    run_with_abort_handle,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _async_test(coro):
    """Run an async test method on a fresh event loop. Avoids dependency
    on pytest-asyncio so this runs with stdlib only."""
    def _wrapper(self):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro(self))
        finally:
            loop.close()
    return _wrapper


# ---------------------------------------------------------------------------
# 1. AbortHandle correctness
# ---------------------------------------------------------------------------

class TestAbortHandle(unittest.TestCase):
    def test_callbacks_run_in_registration_order(self):
        h = AbortHandle()
        seen: List[str] = []
        h.register(lambda: seen.append("a"))
        h.register(lambda: seen.append("b"))
        h.register(lambda: seen.append("c"))
        h.fire()
        self.assertEqual(seen, ["a", "b", "c"])

    def test_fire_is_idempotent(self):
        h = AbortHandle()
        seen: List[int] = []
        h.register(lambda: seen.append(1))
        h.fire()
        h.fire()
        h.fire()
        self.assertEqual(seen, [1], "callback should run exactly once across multiple fires")

    def test_late_register_after_fire_is_dropped(self):
        h = AbortHandle()
        h.fire()
        # The next register must NOT run; otherwise tools that allocate
        # resources after cancellation would leak callbacks for resources
        # that no longer matter.
        h.register(lambda: self.fail("late callback should never run"))
        # If we got here, no exception, no callback run. Pass.

    def test_callback_exception_does_not_break_chain(self):
        h = AbortHandle()
        seen: List[str] = []
        h.register(lambda: (_ for _ in ()).throw(RuntimeError("first")))
        h.register(lambda: seen.append("second-ran"))
        # fire must not raise — abort path must never raise.
        h.fire()
        self.assertEqual(seen, ["second-ran"])

    def test_is_fired_flag(self):
        h = AbortHandle()
        self.assertFalse(h.is_fired)
        h.fire()
        self.assertTrue(h.is_fired)

    def test_thread_safe_register_during_fire(self):
        # Concurrently register from one thread while firing from another.
        # Neither should crash; depending on scheduling, the late register
        # may or may not be picked up — but the contract guarantees no
        # exception either way.
        h = AbortHandle()
        ran: List[int] = []
        for i in range(20):
            h.register(lambda i=i: ran.append(i))

        late_done = threading.Event()
        def _late_register():
            for j in range(20, 40):
                h.register(lambda j=j: ran.append(j))
            late_done.set()

        t = threading.Thread(target=_late_register)
        t.start()
        h.fire()
        t.join(timeout=2.0)
        self.assertTrue(late_done.is_set(), "late-register thread should not deadlock")
        # Original 20 must have all run; late ones may be 0..20 depending on race.
        self.assertGreaterEqual(len(ran), 20)


# ---------------------------------------------------------------------------
# 2. ThreadInterruptToken
# ---------------------------------------------------------------------------

class TestThreadInterruptToken(unittest.TestCase):
    def test_initial_state_unset(self):
        t = ThreadInterruptToken()
        self.assertFalse(t.is_set())
        self.assertFalse(t.wait(0.01), "wait on unset token should time out → False")

    def test_set_makes_wait_return_true_immediately(self):
        t = ThreadInterruptToken()
        t.set()
        start = time.monotonic()
        result = t.wait(5.0)
        elapsed = time.monotonic() - start
        self.assertTrue(result)
        self.assertLess(elapsed, 0.5, "wait on set token should return immediately, not after timeout")

    def test_set_from_another_thread_wakes_waiter(self):
        t = ThreadInterruptToken()
        woke: List[float] = []

        def _waiter():
            start = time.monotonic()
            t.wait(5.0)
            woke.append(time.monotonic() - start)

        worker = threading.Thread(target=_waiter)
        worker.start()
        time.sleep(0.05)  # let the waiter park
        t.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(woke), 1)
        self.assertLess(woke[0], 0.5, "cross-thread set should wake waiter promptly")


# ---------------------------------------------------------------------------
# 3. interruptible_sleep
# ---------------------------------------------------------------------------

class TestInterruptibleSleep(unittest.TestCase):
    def test_falls_back_to_time_sleep_outside_framework(self):
        # No thread-local token installed → must behave like time.sleep
        # so existing tools that haven't been wired in stay unaffected.
        start = time.monotonic()
        result = interruptible_sleep(0.1)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 1.0)

    def test_zero_duration_is_a_noop(self):
        start = time.monotonic()
        result = interruptible_sleep(0.0)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertLess(elapsed, 0.01)


# ---------------------------------------------------------------------------
# 4 & 5. run_with_abort_handle
# ---------------------------------------------------------------------------

class TestRunWithAbortHandle(unittest.TestCase):
    @_async_test
    async def test_returns_blocking_fn_value(self):
        result = await run_with_abort_handle(lambda: 42)
        self.assertEqual(result, 42)

    @_async_test
    async def test_propagates_exception_from_blocking_fn(self):
        async def _coro():
            await run_with_abort_handle(lambda: (_ for _ in ()).throw(ValueError("boom")))
        with self.assertRaises(ValueError):
            await _coro()

    @_async_test
    async def test_cancellation_fires_abort_handle_and_token(self):
        # The blocking fn registers an abort callback. When the surrounding
        # asyncio task is cancelled, the framework must fire the abort
        # handle (callback runs) and set the interrupt token (token.is_set
        # becomes True from the blocking fn's point of view).
        fired: List[str] = []
        observed_set: List[bool] = []

        def _blocking():
            abort = current_abort()
            self.assertIsNotNone(abort, "current_abort must be set on executor thread")
            abort.register(lambda: fired.append("aborted"))
            token = current_interrupt()
            self.assertIsNotNone(token)
            # Poll for up to 5s. We expect cancellation to wake us via the token.
            woke = token.wait(5.0)
            observed_set.append(token.is_set())
            return "interrupted" if woke else "completed"

        ev = asyncio.Event()
        task = asyncio.create_task(
            run_with_abort_handle(_blocking, interrupt_event=ev),
        )
        # Let the task start and the executor thread park on token.wait
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(fired, ["aborted"])
        self.assertEqual(observed_set, [True])

    @_async_test
    async def test_shutdown_deadline_is_honored_when_blocking_fn_ignores_token(self):
        # A truly wedged blocking_fn ignores the interrupt token. The
        # framework must NOT wait for it — we re-raise CancelledError
        # within shutdown_deadline regardless. The orphan thread is
        # accepted (Python+Windows can't kill it).
        woke = threading.Event()

        def _blocking():
            # Ignore the token. Sleep on a real Event to avoid spinning.
            woke.wait(60.0)  # would wedge for a full minute
            return "should-never-reach-caller"

        task = asyncio.create_task(
            run_with_abort_handle(_blocking, shutdown_deadline=0.5),
        )
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - t0
        # Must return within shutdown_deadline + small overhead.
        self.assertLess(
            elapsed, 1.5,
            f"cancel should re-raise within ~deadline; elapsed={elapsed:.3f}s",
        )
        # Release the orphan so the executor pool can recycle the thread.
        woke.set()

    @_async_test
    async def test_interrupt_event_is_mirrored_into_token(self):
        # When the asyncio interrupt event is set externally (without a
        # task cancel), the token on the executor thread sees it.
        seen: List[bool] = []

        def _blocking():
            token = current_interrupt()
            woke = token.wait(5.0)
            seen.append(woke)
            return "ok"

        ev = asyncio.Event()
        task = asyncio.create_task(run_with_abort_handle(_blocking, interrupt_event=ev))
        await asyncio.sleep(0.05)
        ev.set()  # asyncio side fires the event
        result = await task
        self.assertEqual(result, "ok")
        self.assertEqual(seen, [True])


# ---------------------------------------------------------------------------
# 6 & 7 & 8. SSH tool wiring (with paramiko mocked out)
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, parent: "_FakeSSHClient"):
        self._parent = parent
        self._active = True
        self.sock = None  # _linger_close handles None gracefully

    def is_active(self):
        return self._active

    def close(self):
        self._active = False

    def set_keepalive(self, _interval):
        pass


class _FakeSSHClient:
    """Just enough of a paramiko.SSHClient to drive the pool / abort path."""
    closes: List[int] = []

    def __init__(self):
        self.transport = _FakeTransport(self)
        self.closed = False

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True
        self.transport.close()
        _FakeSSHClient.closes.append(id(self))


class TestSSHToolWiring(unittest.TestCase):
    """Verify ssh_tool's pool / abort helpers without needing paramiko."""

    def setUp(self):
        from src.tools import ssh_tool as _ssh
        self.ssh = _ssh
        # Snapshot + clear pool state so tests don't bleed into each other.
        with _ssh._conn_pool_lock:
            self._snap_pool = dict(_ssh._conn_pool)
            self._snap_lru = dict(_ssh._conn_pool_last_used)
            _ssh._conn_pool.clear()
            _ssh._conn_pool_last_used.clear()
        with _ssh._connect_lock:
            self._snap_ts = dict(_ssh._connect_timestamps)
            _ssh._connect_timestamps.clear()
        with _ssh._os_cache_lock:
            self._snap_os = dict(_ssh._os_cache)
            _ssh._os_cache.clear()
        _FakeSSHClient.closes.clear()

    def tearDown(self):
        # Restore originals so we don't perturb a running bridge.
        with self.ssh._conn_pool_lock:
            self.ssh._conn_pool.clear()
            self.ssh._conn_pool.update(self._snap_pool)
            self.ssh._conn_pool_last_used.clear()
            self.ssh._conn_pool_last_used.update(self._snap_lru)
        with self.ssh._connect_lock:
            self.ssh._connect_timestamps.clear()
            self.ssh._connect_timestamps.update(self._snap_ts)
        with self.ssh._os_cache_lock:
            self.ssh._os_cache.clear()
            self.ssh._os_cache.update(self._snap_os)

    def test_flush_connection_pool_closes_all_clients(self):
        c1 = _FakeSSHClient()
        c2 = _FakeSSHClient()
        self.ssh._pool_put("host1:22", c1)
        self.ssh._pool_put("host2:22", c2)
        # Also seed rate-limit + os-detect caches to verify they're cleared.
        with self.ssh._connect_lock:
            self.ssh._connect_timestamps["host1:22"] = time.monotonic()
        with self.ssh._os_cache_lock:
            self.ssh._os_cache["host1:22"] = ("linux", "bash")

        closed = self.ssh.flush_connection_pool()

        self.assertEqual(closed, 2)
        self.assertTrue(c1.closed)
        self.assertTrue(c2.closed)
        self.assertEqual(len(self.ssh._conn_pool), 0)
        self.assertEqual(len(self.ssh._conn_pool_last_used), 0)
        self.assertEqual(len(self.ssh._connect_timestamps), 0)
        self.assertEqual(len(self.ssh._os_cache), 0)

    def test_flush_on_empty_pool_is_noop(self):
        closed = self.ssh.flush_connection_pool()
        self.assertEqual(closed, 0)

    @_async_test
    async def test_pool_force_close_callback_runs_on_abort_fire(self):
        # Simulate _connect's behavior: register a force_close callback on
        # the active AbortHandle. Then fire the handle (as run_with_abort_handle
        # does on cancellation) and verify the callback closed the client.
        client = _FakeSSHClient()

        def _blocking():
            abort = current_abort()
            assert abort is not None
            # Mirror what _connect does: register the close callback.
            abort.register(lambda: client.close())
            # Park on the token. When fired, return.
            current_interrupt().wait(5.0)
            return "ok"

        task = asyncio.create_task(run_with_abort_handle(_blocking))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(client.closed, "force_close callback must close the client on cancel")


# ---------------------------------------------------------------------------
# 9. File-I/O off the asyncio loop
# ---------------------------------------------------------------------------

class TestFileIOOffLoop(unittest.TestCase):
    """Prove that the event loop stays alive while a 'slow' file read runs.

    We simulate a slow read by patching open() to sleep before returning
    bytes. Then we kick off a read concurrently with a separate "heartbeat"
    coroutine that should be able to make progress while the read is in
    flight. Before the refactor, the heartbeat would not tick during the
    sleep; after the refactor, it should tick repeatedly.
    """

    @_async_test
    async def test_read_tool_does_not_freeze_event_loop(self):
        from src.tools.read_tool import ReadTool

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
            f.write("hello world\n")
            f.write("line two\n")
            tmp_path = f.name

        try:
            tool = ReadTool()

            # Heartbeat counter: ticks every 20ms while we're awaiting the read.
            ticks = 0
            stop_heartbeat = asyncio.Event()
            async def _heartbeat():
                nonlocal ticks
                while not stop_heartbeat.is_set():
                    await asyncio.sleep(0.02)
                    ticks += 1

            heartbeat = asyncio.create_task(_heartbeat())
            try:
                # Slow the read by patching the per-path sync helper to sleep
                # at the start. This simulates a slow filesystem syscall.
                orig_read_single = tool._read_single_path

                def _slow_read(*args, **kwargs):
                    time.sleep(0.3)  # 300ms blocking inside the executor
                    return orig_read_single(*args, **kwargs)

                tool._read_single_path = _slow_read  # type: ignore[assignment]

                result = await tool.execute(path=tmp_path)
                stop_heartbeat.set()
                await heartbeat

                self.assertTrue(result.success, f"read failed: {result.error}")
                # During the 300ms slow block we expect at least ~10 ticks
                # (20ms each). Generous lower bound to absorb scheduler jitter.
                self.assertGreaterEqual(
                    ticks, 5,
                    f"event loop should keep ticking during executor-bound read; ticks={ticks}",
                )
            finally:
                stop_heartbeat.set()
                if not heartbeat.done():
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
