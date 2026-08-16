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
    # Pure waiting with no side effects — safe to abort the instant the user
    # redirects. The execute() body already wakes on interrupt_event; this flag
    # lets the coordinator treat a hard_task as immediately actionable when the
    # only in-flight tool is a wait.
    interrupt_behavior = "cancel"

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("wait_interval", ctx=ctx)

    async def execute(
        self,
        seconds: int = 300,
        user_message: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Sleep for the specified duration, interruptible by user messages.

        Args:
            seconds: Duration to wait (1-7200). Default 300 (5 minutes).
            user_message: Delivered to the user as a prominent bubble BEFORE
                the wait begins. Set it whenever the thing being waited on is
                a HUMAN action — see the note below.

        Returns:
            ToolResult with output indicating whether the wait completed
            normally ("elapsed:Ns") or was interrupted ("interrupted").
        """
        start = time.time()
        params = {"seconds": seconds}
        if user_message:
            params["user_message"] = user_message

        seconds = max(_MIN_SECONDS, min(int(seconds), _MAX_SECONDS))

        # Deliver the human-facing message BEFORE sleeping — announcing it
        # afterwards would defeat the point. Waiting on a person who was never
        # told what to do cannot terminate: the 2026-08-03 flash-meta run spent
        # its last ~20 turns in a poll → wait_interval(60) → poll loop, while
        # the agent's own reasoning already knew the user needed to stop
        # pressing a button. It never told them.
        notice_delivered: Optional[bool] = None
        if user_message and user_message.strip():
            notice_delivered = False
            im = getattr(self.ctx, "interaction_manager", None) if self.ctx else None
            if im is not None:
                try:
                    im.notify_user_notice(user_message.strip(), urgent=True)
                    notice_delivered = True
                except Exception:
                    notice_delivered = False

        def _out(body: str) -> Any:
            if notice_delivered is None:
                return body
            return {
                "wait": body,
                "user_message_delivered": notice_delivered,
                "note": (
                    "The user was shown your message before this wait started."
                    if notice_delivered else
                    "YOUR MESSAGE WAS NOT DELIVERED (no UI attached). The user "
                    "does not know what you are waiting for — do not assume the "
                    "action you asked for will happen."
                ),
            }

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
                    output=_out(f"interrupted (after {elapsed:.0f}s)"),
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=elapsed,
                )
            except asyncio.TimeoutError:
                elapsed = time.time() - start
                return ToolResult(
                    success=True,
                    output=_out(f"elapsed:{seconds}s"),
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=elapsed,
                )
        else:
            await asyncio.sleep(seconds)
            elapsed = time.time() - start
            return ToolResult(
                success=True,
                output=_out(f"elapsed:{seconds}s"),
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
            "user_message": {
                "type": "string",
                "description": (
                    "REQUIRED when you are waiting on a HUMAN action rather than a "
                    "machine one. Shown to the user prominently before the wait "
                    "starts. Waiting silently for a person who was never told what "
                    "you need cannot succeed. Omit for ordinary machine polling."
                ),
            },
        }
