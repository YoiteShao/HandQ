"""Agent-facing scheduling tools — create / list / delete scheduled prompts.

These are the HandQ counterpart to Claude Code's ``CronCreate`` / ``CronList``
/ ``CronDelete`` tools: they let the AGENT (not just the Electron UI) pin a
prompt to a cadence and have the bridge fire it automatically. They are thin
wrappers over the process-global :class:`Scheduler` service (the same one the
``cron_*`` IPC handlers drive), reached via ``ctx.scheduler``.

Design notes
------------
- ``on_demand=True`` — they only enter the LLM tool schema once the agent
  claims them (``claim_tool: ["schedule_create"]``), like ssh/browser/email.
- Each scheduled fire runs in a FRESH ``sched-{uuid}`` session minted by the
  bridge (``accept_scheduled_task``), independent of the session that created
  it. This is the "fixed cadence" paradigm — for a self-paced loop that keeps
  the CURRENT session's context, use ``schedule_wakeup`` instead.
- ``durable`` defaults to **False** on ``schedule_create`` (session-only,
  vanishes on bridge restart), matching Claude Code's ``CronCreate`` default.
  Pass ``durable=True`` to persist across restarts (writes scheduled_tasks.json).
- When ``ctx.scheduler`` is None (offline tests / no bridge) every tool returns
  a clean ``success=False`` "scheduler unavailable" result rather than raising.
"""
from __future__ import annotations

import time
from typing import Any, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


_NO_SCHEDULER_MSG = (
    "Scheduler is not available in this environment (no bridge running). "
    "Scheduled tasks require the HandQ desktop bridge."
)


def _scheduler_from(ctx: Optional["SessionContext"]) -> Optional[Any]:
    """Return the live Scheduler off the ctx, or None."""
    if ctx is None:
        return None
    return getattr(ctx, "scheduler", None)


class ScheduleCreateTool(BaseTool):
    """Pin a prompt to a cadence so the bridge fires it automatically."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("schedule_create", ctx=ctx)

    async def execute(
        self,
        prompt: str = "",
        schedule: str = "",
        name: str = "",
        durable: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {
            "prompt": prompt, "schedule": schedule,
            "name": name, "durable": durable,
        }

        def _fail(msg: str) -> ToolResult:
            return ToolResult(
                success=False, output=None, error=msg,
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )

        if not prompt or not prompt.strip():
            return _fail("schedule_create requires a non-empty 'prompt'.")

        scheduler = _scheduler_from(self.ctx)
        if scheduler is None:
            return _fail(_NO_SCHEDULER_MSG)

        # Resolve the schedule string. If the caller gave an explicit grammar /
        # cron expression, validate it directly; otherwise (or on failure) fall
        # back to LLM inference over the prompt so natural-language cadences
        # ("every morning", "in 10 minutes") still work — same pipeline the
        # cron_create IPC handler uses.
        from ..infrastructure.scheduler.schedule import (
            ScheduleSyntaxError, normalize_schedule, parse_schedule,
        )

        resolved_schedule = ""
        dispatch_prompt = ""
        sched_in = (schedule or "").strip()
        if sched_in:
            try:
                normalized = normalize_schedule(sched_in)
                parse_schedule(normalized)
                resolved_schedule = normalized
            except ScheduleSyntaxError as exc:
                return _fail(
                    f"invalid schedule {sched_in!r}: {exc}. Examples: "
                    "'every 5 minutes', 'daily 09:00', 'weekly mon 09:00', "
                    "'once at 2026-06-02 14:30', or cron '*/5 * * * *'."
                )
        else:
            # No explicit schedule — infer cadence + cleaned prompt from the
            # prompt text via the scheduler's LLM inferer.
            try:
                cfg = self.ctx.config_manager.get_config() if self.ctx else {}
            except Exception:
                cfg = {}
            from ..infrastructure.scheduler.inferer import infer_schedule
            inferred = await infer_schedule(prompt.strip(), cfg)
            resolved_schedule = inferred.schedule
            dispatch_prompt = inferred.prompt

        try:
            task = await scheduler.create_task(
                name=name or "",
                prompt=prompt.strip(),
                schedule=resolved_schedule,
                dispatch_prompt=dispatch_prompt,
                durable=bool(durable),
            )
        except ScheduleSyntaxError as exc:
            return _fail(f"invalid schedule: {exc}")
        except Exception as exc:
            return _fail(f"failed to create scheduled task: {exc}")

        return ToolResult(
            success=True,
            output={
                "created": True,
                "id": task.get("id"),
                "name": task.get("name"),
                "schedule": task.get("schedule"),
                "durable": task.get("durable"),
                "next_run_at": task.get("next_run_at"),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {
            "prompt": {
                "type": "string",
                "description": (
                    "The prompt to fire on the cadence, phrased as 'do this NOW' "
                    "(no relative-time words — the schedule already absorbs them)."
                ),
            },
            "schedule": {
                "type": "string",
                "description": (
                    "When to fire. Accepts a friendly form ('every 5 minutes', "
                    "'daily 09:00', 'weekly mon 09:00', 'once at 2026-06-02 14:30', "
                    "'once in 10 minutes') OR standard 5-field cron ('*/5 * * * *', "
                    "'0 9 * * 1-5'). If omitted, the cadence is inferred from the "
                    "prompt's natural language."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional human-readable label shown in the UI.",
            },
            "durable": {
                "type": "boolean",
                "description": (
                    "False (default) = session-only, vanishes when the app "
                    "restarts. True = persisted across restarts. Only set True "
                    "when the user explicitly wants the task to survive restarts."
                ),
            },
        }


class ScheduleListTool(BaseTool):
    """List all scheduled tasks (durable + session-only)."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("schedule_list", ctx=ctx)

    async def execute(self, **kwargs: Any) -> ToolResult:
        start = time.time()
        scheduler = _scheduler_from(self.ctx)
        if scheduler is None:
            return ToolResult(
                success=False, output=None, error=_NO_SCHEDULER_MSG,
                tool_name=self.name, tool_parameters={},
                execution_time=time.time() - start,
            )
        try:
            tasks = await scheduler.list_tasks()
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=f"failed to list scheduled tasks: {exc}",
                tool_name=self.name, tool_parameters={},
                execution_time=time.time() - start,
            )
        # Compact projection — the agent rarely needs every internal field.
        rows = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "schedule": t.get("schedule"),
                "enabled": t.get("enabled"),
                "durable": t.get("durable"),
                "next_run_at": t.get("next_run_at"),
                "last_status": t.get("last_status"),
                "run_count": t.get("run_count"),
            }
            for t in (tasks or [])
        ]
        return ToolResult(
            success=True,
            output={"count": len(rows), "tasks": rows},
            tool_name=self.name, tool_parameters={},
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {}


class ScheduleDeleteTool(BaseTool):
    """Delete a scheduled task by id."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("schedule_delete", ctx=ctx)

    async def execute(self, task_id: str = "", **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"task_id": task_id}
        scheduler = _scheduler_from(self.ctx)
        if scheduler is None:
            return ToolResult(
                success=False, output=None, error=_NO_SCHEDULER_MSG,
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )
        tid = (task_id or "").strip()
        if not tid:
            return ToolResult(
                success=False, output=None,
                error="schedule_delete requires a 'task_id' (from schedule_list).",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )
        try:
            ok = await scheduler.delete_task(tid)
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=f"failed to delete scheduled task: {exc}",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )
        if not ok:
            return ToolResult(
                success=False, output=None,
                error=f"no scheduled task with id {tid!r} (already deleted?).",
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )
        return ToolResult(
            success=True, output={"deleted": True, "id": tid},
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {
            "task_id": {
                "type": "string",
                "description": "The id of the task to delete (from schedule_list).",
            },
        }
