"""Scheduler service — async loop that fires due tasks.

Lifecycle::

    sched = Scheduler(store_path=..., dispatch=...)
    await sched.start()
    ...
    await sched.shutdown()

The ``dispatch`` argument is a callable provided by the bridge:

    async def dispatch(task: ScheduledTask) -> None:
        # The bridge mints a fresh sched-{uuid} session, builds a
        # FlowControllerV2, runs on_user_message, and returns when
        # the coordinator's reply is complete (NOT fire-and-forget).
        ...

Note: dispatch blocks for the full reply duration. The scheduler's _fire
awaits it, so concurrent cron fires for different schedules serialize behind
the running one. If finer-grained concurrency is needed later, the bridge
can wrap its dispatch in a background task and return immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from ..long_term_memory import _constants as C
from ..long_term_memory.models import ScheduledTask, SchedulerTaskStatus
from .store import ScheduleStore

_logger = logging.getLogger("handq.scheduler")

DispatchFn = Callable[[ScheduledTask], Awaitable[bool]]


class Scheduler:
    """JSON-backed scheduler.

    See module docstring for the contract. Public methods are all
    async-safe; the underlying store serialises mutations.
    """

    def __init__(
        self,
        *,
        store_path: Path,
        dispatch: DispatchFn,
    ) -> None:
        self._store = ScheduleStore(store_path)
        self._dispatch = dispatch
        self._task: Optional[asyncio.Task] = None
        self._wakeup = asyncio.Event()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._store.load()
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="scheduler")
        _logger.info("scheduler started; tasks=%d", len(self._store.list()))

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger.exception("scheduler shutdown raised")
        _logger.info("scheduler shut down")

    # ── Public mutations (called from bridge IPC handlers) ──────────────────

    async def list_tasks(self) -> List[Dict]:
        return [t.to_dict() for t in self._store.list()]

    async def create_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        dispatch_prompt: str = "",
        durable: bool = True,
    ) -> Dict:
        t = await self._store.create(
            name=name, prompt=prompt, schedule=schedule,
            dispatch_prompt=dispatch_prompt, durable=durable,
        )
        self._wakeup.set()
        return t.to_dict()

    async def update_task(
        self,
        task_id: str,
        *,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        schedule: Optional[str] = None,
    ) -> Optional[Dict]:
        t = await self._store.update(
            task_id,
            name=name, prompt=prompt,
            schedule=schedule,
        )
        if t:
            self._wakeup.set()
        return t.to_dict() if t else None

    async def delete_task(self, task_id: str) -> bool:
        ok = await self._store.delete(task_id)
        if ok:
            self._wakeup.set()
        return ok

    async def set_enabled(self, task_id: str, enabled: bool) -> Optional[Dict]:
        t = await self._store.set_enabled(task_id, enabled)
        if t:
            self._wakeup.set()
        return t.to_dict() if t else None

    async def run_now(self, task_id: str) -> Optional[Dict]:
        """Force a manual fire. Subject to the same busy guard as
        scheduled fires (the bridge's ``dispatch`` decides)."""
        t = self._store.get(task_id)
        if not t:
            return None
        await self._fire(t, manual=True)
        return self._store.get(task_id).to_dict() if self._store.get(task_id) else None

    async def notify_task_finished(
        self, task_id: str, *, ok: bool, error: str = "",
        count_as_failure: bool = True,
    ) -> None:
        await self._store.mark_finished(
            task_id, ok=ok, error=error, count_as_failure=count_as_failure,
        )

    # ── Main loop ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while True:
                next_due = self._next_due_seconds()
                # asyncio.Event.wait doesn't take a max — wrap in wait_for.
                # Whichever fires first (timeout OR a mutation poking us)
                # wakes the loop.
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=next_due)
                except asyncio.TimeoutError:
                    pass
                self._wakeup.clear()
                try:
                    await self._scan_and_fire()
                except Exception:
                    _logger.exception("scheduler scan crashed; sleeping")
                    await asyncio.sleep(C.SCHEDULER_TICK_SEC)
        except asyncio.CancelledError:
            return

    def _next_due_seconds(self) -> float:
        """Compute how long the main loop should sleep before the next
        wake-up. Uses an adaptive cap that grows with the gap to the
        next due fire so we don't burn CPU waking every 2 minutes when
        the next task is 12 hours away.

        Tiered cap (gap = soonest - now):
          gap >= 1 hour  → cap at 1 hour   (long-tail tasks)
          gap >= 10 min  → cap at 10 min   (medium-tail)
          else           → exact wait      (about to fire — be precise)

        Even with a long cap, mutations (create / update / delete /
        set_enabled / run_now) and the bridge's _after_flow_done both
        call ``_wakeup.set()``, which makes ``wait_for`` return early
        and re-runs ``_next_due_seconds`` against the fresh task list.
        So the long cap never delays a freshly-added task."""
        now = time.time()
        upcoming = [
            t.next_run_at for t in self._store.list()
            if t.enabled and t.next_run_at
        ]
        if not upcoming:
            # No enabled tasks: sleep on the long cap until something
            # is added (mutation will wake us via _wakeup).
            return 3600.0
        soonest = min(upcoming)
        delta = max(1.0, soonest - now)
        if delta >= 3600.0:
            return 3600.0
        if delta >= 600.0:
            return 600.0
        return delta

    async def _scan_and_fire(self) -> None:
        now = int(time.time())
        for t in self._store.list():
            if not t.enabled:
                continue
            if t.last_status == SchedulerTaskStatus.RUNNING:
                # Normally the bridge still has this fire in flight; skip until
                # it reports back via notify_task_finished. BUT if the bridge
                # died / crashed / never reported (e.g. flow setup threw after
                # dispatch was accepted), the task would stay RUNNING forever
                # and be skipped on every scan — a permanent zombie. Backstop:
                # if it has been RUNNING longer than the task timeout, presume
                # the fire is dead, reset it, and let this scan re-fire it.
                stale_for = now - (t.last_run_at or 0)
                if stale_for < C.SCHEDULER_TASK_TIMEOUT_SEC:
                    continue
                _logger.warning(
                    "scheduler: task %s stuck RUNNING for %ds (> %ds timeout); "
                    "resetting so it can fire again",
                    t.id[:8], stale_for, C.SCHEDULER_TASK_TIMEOUT_SEC,
                )
                await self._store.reset_stale_running(t.id)
                t = self._store.get(t.id)
                if t is None or not t.enabled:
                    continue
                # fall through to the due-check below
            if not t.next_run_at or t.next_run_at > now:
                continue
            await self._fire(t, manual=False)

    async def _fire(self, t: ScheduledTask, *, manual: bool) -> None:
        # Scheduler firing is decoupled from "is any session idle?" — in the
        # multi-session bridge model each fire generates a fresh
        # ``sched-{uuid}`` session id and the bridge spawns a brand-new
        # FlowControllerV2 to run it. Fires don't block on interactive
        # sessions being busy; however _fire itself awaits dispatch completion
        # (the full on_user_message round-trip), so overlapping schedules
        # serialize behind one slow reply. The only refusal path is bridge
        # shutdown (``_shutdown_requested=True``), in which case dispatch
        # returns ``False`` below and we leave the task PENDING so the next
        # live scheduler tick (or the next process boot) picks it up.
        _logger.info(
            "scheduler firing task=%s name=%r manual=%s",
            t.id[:8], t.name, manual,
        )
        # Mark RUNNING BEFORE dispatch, not after. ``self._dispatch`` (the
        # bridge's accept_scheduled_task) blocks for the FULL session
        # round-trip — it awaits on_user_message to completion and calls
        # notify_task_finished (→ mark_finished, setting last_status to
        # ok/failed) BEFORE returning True. Marking RUNNING only after
        # `await self._dispatch(t)` returns therefore ran AFTER
        # mark_finished had already recorded the real outcome, silently
        # overwriting ok/failed back to a permanent-looking "running" —
        # exactly the state a user would see stuck in the UI even though the
        # task genuinely completed. If the bridge refuses (busy / shutting
        # down), we correct back to PENDING below — mark_pending
        # unconditionally overwrites last_status, so this can never leave a
        # stale RUNNING behind.
        original_next_run_at = t.next_run_at
        await self._store.mark_running(t.id)
        try:
            accepted = await self._dispatch(t)
        except Exception as exc:
            _logger.exception("scheduler dispatch crashed task=%s", t.id[:8])
            await self._store.mark_finished(
                t.id, ok=False, error=f"dispatch crashed: {exc}",
            )
            return
        if accepted:
            # The bridge already called notify_task_finished (→ mark_finished)
            # synchronously as part of this dispatch call. Nothing left to do.
            pass
        else:
            # Bridge was busy/shutting down and refused BEFORE running
            # anything. Correct the RUNNING mark we set above back to
            # PENDING, and restore the pre-mark_running next_run_at (which
            # mark_running already advanced/zeroed) so this trigger is
            # retried on the next idle wakeup instead of being lost.
            await self._store.mark_pending(
                t.id, restore_next_run_at=original_next_run_at,
            )
