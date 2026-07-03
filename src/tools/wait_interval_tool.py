"""
Wait Interval Tool — zero-resource async sleep for monitoring loops.

Allows the agent to yield execution for a specified duration without holding
any process, connection, or buffer resources. Respects the session's
interrupt_event so user messages can wake the agent mid-sleep.

Design rationale:
  This tool exists to make long-running monitoring tasks efficient. Without it,
  an agent doing periodic checks (e.g., "poll SSH status every 5 minutes for
  24 hours") would either busy-loop or abuse shell/session tools as sleep
  primitives. wait_interval provides a clean, interruptible, resource-free
  wait that integrates with the existing interrupt_event mechanism.

  Critically, wait_interval signals to the IterationAdvisor that the agent is
  in an intentional monitoring cycle — preventing false spinning detection.
"""
import asyncio
import time
from typing import Any, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext

_MAX_SECONDS = 7200  # 2 hours cap per single wait
_MIN_SECONDS = 1


class WaitIntervalTool(BaseTool):
    """Interruptible async sleep — yields execution without resource consumption."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("wait_interval", ctx=ctx)

    async def execute(self, seconds: int = 300, **kwargs: Any) -> ToolResult:
        """Sleep for the specified duration, interruptible by user messages.

        Args:
            seconds: Duration to wait (1-7200). Default 300 (5 minutes).

        Returns:
            ToolResult with output indicating whether the wait completed
            normally ("elapsed:Ns") or was interrupted ("interrupted").
        """
        start = time.time()
        params = {"seconds": seconds}

        seconds = max(_MIN_SECONDS, min(int(seconds), _MAX_SECONDS))

        interrupt_event = None
        if self.ctx:
            interrupt_event = getattr(self.ctx, "interrupt_event", None)

        if interrupt_event is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(interrupt_event.wait()),
                    timeout=seconds,
                )
                elapsed = time.time() - start
                return ToolResult(
                    success=True,
                    output=f"interrupted (after {elapsed:.0f}s)",
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=elapsed,
                )
            except asyncio.TimeoutError:
                elapsed = time.time() - start
                return ToolResult(
                    success=True,
                    output=f"elapsed:{seconds}s",
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=elapsed,
                )
        else:
            await asyncio.sleep(seconds)
            elapsed = time.time() - start
            return ToolResult(
                success=True,
                output=f"elapsed:{seconds}s",
                tool_name=self.name,
                tool_parameters=params,
                execution_time=elapsed,
            )

    @classmethod
    def get_schema(cls):
        return {
            "seconds": {
                "type": "integer",
                "description": (
                    "Duration to wait in seconds (1-7200). Use between observation "
                    "cycles in a monitoring loop. The wait is interruptible — if the "
                    "user sends a message, this returns immediately with 'interrupted'."
                ),
            },
        }
