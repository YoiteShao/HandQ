"""schedule_wakeup — self-paced loop primitive (Claude Code ScheduleWakeup parity).

Lets the agent end its turn and be re-woken after a delay to continue a
self-paced loop, WITHOUT spawning a fresh session — the wakeup re-queues a
prompt onto the current session's TaskChannel, so the agent's conversation
history (and thus context) is preserved across the tick.

Contrast with the two neighbours:
  - ``wait_interval`` blocks the current item (session stays busy) — for short
    waits where you resume the same in-task tool loop.
  - ``schedule_create`` pins a prompt to a cadence and fires it in a FRESH
    scheduled session — for durable/recurring cron-style tasks.
  - ``schedule_wakeup`` (this tool) releases the turn and resumes THIS session
    later — for longer self-paced loops where holding the session open wastes
    resources but you still want your accumulated context.

Delegates to :meth:`SessionContext.schedule_wakeup`, which owns the timer,
the clamp ([60, 3600] s), and the re-queue. This tool is read-only in the
side-effect sense: it only registers future work, it doesn't act now.
"""
from __future__ import annotations

import time
from typing import Any, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


class ScheduleWakeupTool(BaseTool):
    """Register an in-session wakeup to continue a self-paced loop after a delay."""

    is_read_only = True          # only registers future work; no immediate effect
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("schedule_wakeup", ctx=ctx)

    async def execute(
        self,
        delay_seconds: Any = 300,
        prompt: str = "",
        reason: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {
            "delay_seconds": delay_seconds, "prompt": prompt, "reason": reason,
        }

        def _fail(msg: str) -> ToolResult:
            return ToolResult(
                success=False, output=None, error=msg,
                tool_name=self.name, tool_parameters=params,
                execution_time=time.time() - start,
            )

        if not prompt or not prompt.strip():
            return _fail(
                "schedule_wakeup requires a non-empty 'prompt' (what to resume "
                "with when you wake up)."
            )
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError):
            return _fail("'delay_seconds' must be an integer number of seconds.")

        if self.ctx is None:
            return _fail(
                "schedule_wakeup is unavailable without a live session context."
            )

        clamped = self.ctx.schedule_wakeup(
            delay_seconds=delay, prompt=prompt.strip(), reason=reason,
        )
        return ToolResult(
            success=True,
            output={
                "scheduled": True,
                "delay_seconds": clamped,
                "reason": reason or "",
                "note": (
                    f"Will wake up and resume in {clamped}s. This turn can now "
                    "end. To end the loop, simply don't call schedule_wakeup again."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {
            "delay_seconds": {
                "type": "integer",
                "description": (
                    "Seconds until you wake up, clamped to [60, 3600]. Avoid 300 "
                    "(worst for prompt cache); prefer <270 to stay cache-warm or "
                    ">=1200 for idle waits."
                ),
            },
            "prompt": {
                "type": "string",
                "description": "The instruction to resume with when you wake up.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence on what you're waiting for and why this "
                    "delay (shown to the user)."
                ),
            },
        }
