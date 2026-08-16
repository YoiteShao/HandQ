"""
Notify User Tool — the agent's only path to reach the user mid-item.

Why this exists
---------------
Before this tool, an agent that learned something the user urgently needed to
know had nowhere to put it. It could ask a blocking question
(``request_user_form``), or it could write to stdout and hope. In the
2026-08-03 flash-meta run it did the latter, and the run failed because of it:

  * Turn 242, the agent correctly diagnosed the outer loop that had blocked the
    task for ~45 minutes — the USER's own repeated "Boot SS EDL" clicks in
    Alpaca were intercepting the firehose reboot and pulling the device back
    into 9008.
  * It wrote ``"⚠️  CRITICAL: Please do NOT click 'Boot SS EDL' for the next
    120 seconds"`` into ``final_reboot_verify.py``'s stdout — a stream no human
    was watching.
  * Turn 247 it named the gap in its own reasoning ("This is a communication
    gap where I need to clarify…") and then called ``wait_interval(60)``.

``notify_user`` is fire-and-forget by design. A blocking question is the wrong
shape for "stop doing X" or "FYI, the thing you asked about is actually Y" —
the agent should keep working, and the user should see the message immediately.
When the agent genuinely needs an answer before continuing, that is
``request_user_form`` (via the confirmation path), not this.
"""
import time
from typing import Any, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext

_MAX_MESSAGE_CHARS = 1200


class NotifyUserTool(BaseTool):
    """Push a prominent, non-blocking message from the agent to the user."""

    # Pure outbound communication: touches no files, no processes, no device.
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("notify_user", ctx=ctx)

    async def execute(
        self,
        message: Optional[str] = None,
        urgent: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {"message": message, "urgent": urgent}

        text = (message or "").strip()
        if not text:
            return ToolResult(
                success=False,
                output=None,
                error="notify_user requires a non-empty 'message'.",
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )
        truncated = len(text) > _MAX_MESSAGE_CHARS
        if truncated:
            text = text[:_MAX_MESSAGE_CHARS] + "…"

        im = getattr(self.ctx, "interaction_manager", None) if self.ctx else None
        if im is None:
            # No UI attached (tests / headless). Report honestly rather than
            # claiming the user was told — a false "delivered" here would be
            # exactly the class of lie this tool exists to prevent.
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "No interaction manager attached to this session, so the "
                    "message could NOT be delivered to the user. Do not treat "
                    "the user as informed."
                ),
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )
        try:
            im.notify_user_notice(text, urgent=bool(urgent))
        except Exception as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"Failed to deliver the message to the user: {exc}",
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )

        return ToolResult(
            success=True,
            output={
                "delivered": True,
                "urgent": bool(urgent),
                "message": text,
                "truncated": truncated,
                "note": (
                    "Shown to the user as a standalone bubble. This is "
                    "one-way — the user may or may not act on it, and you have "
                    "NOT been told they read it. If the task cannot proceed "
                    "without their action, say so here and then verify the "
                    "world state yourself rather than assuming compliance."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {
            "message": {
                "type": "string",
                "description": (
                    "What to tell the user, in their language. Use this the "
                    "moment you learn something they need to know and cannot "
                    "see from their side. The three cases that matter most: "
                    "(1) the user is actively doing something that is BREAKING "
                    "the task — say so immediately and say what to stop; "
                    "(2) an instruction they gave turns out to be wrong or "
                    "impossible in this environment, so they should stop "
                    "expecting that path to work; (3) you need a physical "
                    "action only they can take (press a button, replug a "
                    "cable) — name the exact action. "
                    "Do NOT use it for progress narration; the activity trace "
                    "already shows that."
                ),
            },
            "urgent": {
                "type": "boolean",
                "description": (
                    "True renders the message with a warning marker. Reserve it "
                    "for cases where the user acting (or not acting) in the next "
                    "minute changes the outcome. Default: false."
                ),
            },
        }
