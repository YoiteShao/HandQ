"""JSON-backed persistence for scheduled tasks.

Tiny store: the file is loaded into memory at startup and rewritten
atomically every mutation. We don't expect users to have hundreds of
scheduled prompts — even a thousand entries serialise in <10ms.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from ..long_term_memory.models import ScheduledTask, SchedulerTaskStatus
from .schedule import is_one_shot, jittered_next_fire, next_fire, parse_schedule

_logger = logging.getLogger("handq.scheduler.store")


class ScheduleStore:
    """Thread-safe, async-friendly wrapper around a JSON file.

    The Scheduler service is single-task asyncio, so we serialise all
    writes through one ``asyncio.Lock`` and dispatch the actual I/O to
    a thread to avoid blocking the loop on slow disks (Windows AV,
    OneDrive sync, etc.).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._tasks: Dict[str, ScheduledTask] = {}

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def load(self) -> None:
        async with self._lock:
            try:
                if not self.path.exists():
                    self._tasks = {}
                    return
                raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                data = json.loads(raw or "[]")
            except FileNotFoundError:
                self._tasks = {}
                return
            except Exception:
                _logger.exception(
                    "scheduler: failed to read %s; backing up and resetting",
                    self.path,
                )
                # Corruption: rename and start fresh so the bridge boots.
                try:
                    backup = self.path.with_suffix(
                        f".broken-{int(time.time())}.json",
                    )
                    await asyncio.to_thread(os.replace, self.path, backup)
                except Exception:
                    pass
                self._tasks = {}
                return
            tasks: Dict[str, ScheduledTask] = {}
            if isinstance(data, list):
                for d in data:
                    try:
                        t = ScheduledTask.from_dict(d)
                        tasks[t.id] = t
                    except Exception:
                        _logger.warning("scheduler: dropping malformed entry")
            self._tasks = tasks
            _logger.info("scheduler: loaded %d tasks from %s",
                         len(self._tasks), self.path)

    async def _flush_locked(self) -> None:
        # MUST be called with self._lock held.
        # Session-only (durable=False) tasks are kept in memory and scheduled
        # normally, but never written to disk — so they vanish on restart.
        data = [
            t.to_dict() for t in self._tasks.values() if t.durable
        ]
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        def _write() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace — never leave a partially-written file.
            fd, tmp = tempfile.mkstemp(
                prefix=".scheduled_tasks-", suffix=".json",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        await asyncio.to_thread(_write)

    # ── Read API ────────────────────────────────────────────────────────────

    def list(self) -> List[ScheduledTask]:
        return sorted(
            self._tasks.values(),
            key=lambda t: (not t.enabled, t.next_run_at or 9_999_999_999),
        )

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        return self._tasks.get(task_id)

    # ── Mutations ───────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        dispatch_prompt: str = "",
        durable: bool = True,
    ) -> ScheduledTask:
        # Validate the schedule first so the parse error surfaces in
        # the IPC response instead of a rejected create that left a
        # half-formed row behind.
        parse_schedule(schedule)
        async with self._lock:
            task_id = str(uuid.uuid4())
            t = ScheduledTask(
                id=task_id,
                name=(name or "").strip() or "(unnamed task)",
                prompt=prompt,
                schedule=schedule,
                enabled=True,
                last_run_at=0,
                next_run_at=jittered_next_fire(
                    schedule, last_run_at=0, seed=task_id,
                ),
                dispatch_prompt=dispatch_prompt or "",
                durable=durable,
            )
            self._tasks[t.id] = t
            await self._flush_locked()
        return t

    async def update(
        self,
        task_id: str,
        *,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        schedule: Optional[str] = None,
    ) -> Optional[ScheduledTask]:
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return None
            if name is not None:
                t.name = name
            if prompt is not None:
                t.prompt = prompt
            if schedule is not None:
                parse_schedule(schedule)
                t.schedule = schedule
                t.next_run_at = jittered_next_fire(
                    schedule, last_run_at=t.last_run_at, seed=t.id,
                )
            t.updated_at = int(time.time())
            await self._flush_locked()
        return t

    async def delete(self, task_id: str) -> bool:
        async with self._lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
            await self._flush_locked()
        return True

    async def set_enabled(self, task_id: str, enabled: bool) -> Optional[ScheduledTask]:
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return None
            t.enabled = bool(enabled)
            t.updated_at = int(time.time())
            if enabled and t.last_run_at:
                # When re-enabling, recompute the next fire from now so
                # we don't immediately replay every interval the task
                # missed while disabled.
                t.next_run_at = jittered_next_fire(
                    t.schedule, last_run_at=int(time.time()), seed=t.id,
                )
            elif enabled:
                t.next_run_at = jittered_next_fire(
                    t.schedule, last_run_at=0, seed=t.id,
                )
            await self._flush_locked()
        return t

    async def mark_running(self, task_id: str) -> None:
        """Mark *task_id* as in-flight and advance ``next_run_at`` to
        the SCHEDULED next fire time, not ``now + interval``.

        The base for the new ``next_run_at`` is the CURRENT
        ``next_run_at`` (the trigger we're firing right now), plus one
        interval. This is what makes serial catch-up actually catch up:

          * If the task ran long and missed N triggers, each
            mark_running advances next_run_at by exactly one interval.
          * After mark_finished, ``next_run_at`` may STILL be in the
            past, so _scan_and_fire fires again and we catch up one
            more missed trigger. Repeat until wall-clock catches up.
          * Compare to anchoring on ``now``: that would silently
            collapse all N missed triggers into a single catch-up,
            which is exactly the "skip" semantics the user rejected.

        For daily/weekly schedules ``next_fire`` rolls forward to the
        next future anchor, so they don't pile up missed days. That's
        intentional — replaying yesterday's "summarise today's PRs"
        prompt is rarely useful.
        """
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            now = int(time.time())
            base = t.next_run_at if t.next_run_at else now
            t.last_status = SchedulerTaskStatus.RUNNING
            t.last_run_at = now
            if is_one_shot(t.schedule):
                # One-shot: no future fire. Leaving a stale next_run_at
                # in the past would re-fire on every wakeup until
                # mark_finished arrives — clear it.
                t.next_run_at = 0
            else:
                from ..long_term_memory import _constants as C
                if now - t.created_at >= C.SCHEDULER_RECURRING_MAX_AGE_SEC:
                    # 7-day cap: this is the final fire — disable so it never
                    # fires again (mirrors Claude Code's recurring auto-expiry).
                    t.enabled = False
                    t.next_run_at = 0
                    _logger.info(
                        "scheduler: recurring task %s auto-expired after %d days",
                        task_id, C.SCHEDULER_RECURRING_MAX_AGE_SEC // 86400,
                    )
                else:
                    t.next_run_at = jittered_next_fire(
                        t.schedule, last_run_at=base, seed=t.id,
                    )
            t.updated_at = now
            await self._flush_locked()

    async def mark_pending(
        self, task_id: str, *, restore_next_run_at: Optional[int] = None,
    ) -> None:
        """Bridge was busy when this task came due, OR (new call site) the
        bridge refused a fire we had already optimistically mark_running'd.

        ``restore_next_run_at``, when given, resets ``next_run_at`` back to
        the value it held BEFORE the caller's ``mark_running`` mutated it
        (one-shot: zeroed; recurring: advanced one interval) — otherwise a
        refused dispatch would leave the trigger permanently skipped/lost
        instead of retried on the next idle wakeup. ``None`` preserves the
        original "leave next_run_at untouched" behaviour for the plain
        busy-refusal call site, where mark_running was never called.

        Idempotent — if the task is already PENDING (we hit it on a
        previous wakeup of the same idle gap) this is a no-op so we don't
        churn the JSON file (restore_next_run_at is still honoured on the
        first call that sets PENDING, before the idempotence check would
        matter — see below).
        """
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            if t.last_status == SchedulerTaskStatus.PENDING:
                return
            t.last_status = SchedulerTaskStatus.PENDING
            if restore_next_run_at is not None:
                t.next_run_at = restore_next_run_at
            t.updated_at = int(time.time())
            await self._flush_locked()

    async def reset_stale_running(self, task_id: str) -> Optional[ScheduledTask]:
        """Recover a task wedged in RUNNING because its fire never reported
        back (bridge crash / flow-setup exception after dispatch was accepted).

        Flips the status out of RUNNING and makes the task due NOW so the next
        scan re-fires it, rather than skipping it forever. For one-shots that
        already had next_run_at cleared by mark_running, we set next_run_at to
        the current time so the due-check picks it up; recurring tasks whose
        next_run_at is already in the future are re-fired immediately as a
        catch-up for the lost run. Records the recovery in last_error so it is
        visible in the UI.
        """
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t or t.last_status != SchedulerTaskStatus.RUNNING:
                return t
            now = int(time.time())
            t.last_status = SchedulerTaskStatus.FAILED
            t.last_error = "previous fire never reported completion (timed out); auto-recovered"
            # Make it due now so _scan_and_fire re-fires on this pass.
            t.next_run_at = now
            t.updated_at = now
            await self._flush_locked()
        return t

    async def mark_finished(
        self,
        task_id: str,
        *,
        ok: bool,
        error: str = "",
        count_as_failure: bool = True,
    ) -> Optional[ScheduledTask]:
        """Record the outcome of an in-flight fire.

        NOTE: does NOT touch ``last_run_at`` or ``next_run_at``.
        ``mark_running`` already pinned them at fire-start time.
        Re-anchoring here would silently swallow missed triggers and
        re-introduce the skip semantics we explicitly removed."""
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return None
            now = int(time.time())
            t.run_count += 1
            if ok:
                t.failure_count = 0
                t.last_status = SchedulerTaskStatus.OK
                t.last_error = ""
            elif not count_as_failure:
                # User-cancelled (e.g. New Session). Don't pollute the
                # auto-disable counter — these aren't real failures.
                t.last_status = SchedulerTaskStatus.CANCELLED
                t.last_error = (error or "")[:500]
            else:
                t.failure_count += 1
                t.last_status = SchedulerTaskStatus.FAILED
                t.last_error = (error or "")[:500]
                from ..long_term_memory import _constants as C
                if t.failure_count >= C.SCHEDULER_MAX_FAILURES_BEFORE_DISABLE:
                    t.enabled = False
                    _logger.warning(
                        "scheduler: auto-disabled task %s after %d failures",
                        task_id, t.failure_count,
                    )
            t.updated_at = now
            if is_one_shot(t.schedule):
                # Disable one-shot tasks after they fire, regardless of
                # outcome. Re-enabling via the UI re-fires immediately
                # (set_enabled recomputes next_run_at from the same
                # absolute timestamp, which is now in the past).
                t.enabled = False
            await self._flush_locked()
        return t
