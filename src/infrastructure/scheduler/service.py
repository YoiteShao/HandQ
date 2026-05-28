"""Scheduler service — async loop that fires due tasks.

Lifecycle::

    sched = Scheduler(store_path=..., dispatch=...)
    await sched.start()
    ...
    await sched.shutdown()

The ``dispatch`` argument is a callable provided by the bridge:

    async def dispatch(task: ScheduledTask) -> None:
        # The bridge synthesises an inbound `request` envelope and
        # routes it through its own dispatcher. Returns when the
        # session has actually been kicked off (NOT when it finishes).
        ...

Why we don't await the actual flow completion: a long task could pin
the scheduler loop for hours, blocking every other due fire. Instead,
we mark the task as ``RUNNING`` before dispatch, and the bridge calls
``Scheduler.notify_task_finished(task_id, ok, error)`` from its
``_run_flow_session`` finally-block. That keeps the run-counter
accurate without coupling the scheduler to FlowController internals.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from ..long_term_memory import _constants as C
from ..long_term_memory.models import ScheduledTask, SchedulerTaskStatus
from .schedule import ScheduleSyntaxError, parse_schedule
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
    ) -> Dict:
        t = await self._store.create(
            name=name, prompt=prompt, schedule=schedule,
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

    # ── Validation hook for IPC ────────────────────────────────────────────

    @staticmethod
    def validate_schedule(spec: str) -> None:
        """Raise ``ScheduleSyntaxError`` if *spec* is invalid. The bridge
        IPC layer calls this directly on ``cron_create`` / ``cron_update``
        so the renderer surfaces the error message verbatim.
        """
        parse_schedule(spec)

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
                # Bridge still has the previous fire in flight; honor
                # the busy policy by skipping until it reports back.
                continue
            if not t.next_run_at or t.next_run_at > now:
                continue
            await self._fire(t, manual=False)

    async def _fire(self, t: ScheduledTask, *, manual: bool) -> None:
        _logger.info(
            "scheduler firing task=%s name=%r manual=%s",
            t.id[:8], t.name, manual,
        )
        # Don't pre-mark RUNNING — if the bridge refuses (busy), we'd
        # leave a stale RUNNING that _scan_and_fire skips forever.
        # Mark RUNNING only after dispatch is accepted.
        try:
            accepted = await self._dispatch(t)
        except Exception as exc:
            _logger.exception("scheduler dispatch crashed task=%s", t.id[:8])
            await self._store.mark_finished(
                t.id, ok=False, error=f"dispatch crashed: {exc}",
            )
            return
        if accepted:
            await self._store.mark_running(t.id)
            # The bridge will call notify_task_finished when its session
            # completes. We do nothing else here.
        else:
            # Bridge was busy and refused. Mark PENDING and leave
            # next_run_at unchanged so the next idle wakeup retries it.
            await self._store.mark_pending(t.id)
