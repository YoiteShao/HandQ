# -*- coding: utf-8 -*-
"""AskHumanTool — open a modal asking the user a clarifying question.

Restraint contract
------------------
This tool interrupts the user. The system prompt's usage_guide instructs the
agent to default to deciding silently from context and to call this tool ONLY
when the task literally cannot proceed without information that cannot be
derived. The runtime does not try to enforce that rule programmatically — it
trusts the agent to follow the guidance — but the result of every ask_human
call goes back to the agent verbatim, so spurious prompts produce visible UX
regressions and are easy to spot.

Runtime model
-------------
The tool delegates to ``InteractionManager.request_user_text(question)``:

  - In GUI mode (Electron bridge) the IM forwards a ``kind: "ask_human"``
    envelope to the renderer, which opens the same overlay used for the
    secret-input flow but without input masking. The user types their reply
    and clicks Send; the answer flows back through the confirmation queue.

  - In CLI / no-UI mode ``request_user_text`` falls back to a stderr prompt
    plus a blocking read on the confirmation queue. The fallback exists so
    importing the tool never crashes; callers get a low-friction experience
    when the GUI is wired up and a workable one when it isn't.

Concurrency
-----------
The V2 ``InteractionManager.request_user_text`` is a coroutine — it awaits a
bridge-side future that resolves when the user submits the modal — so
``execute()`` awaits it directly without an executor; the event loop stays
free while we wait.

Timeout
-------
``asyncio.wait_for()`` caps the wait at ``_ASK_HUMAN_TIMEOUT_S`` seconds.
On timeout the tool returns ``success=False`` with an explicit instruction to
proceed with a sensible default, so an unattended task never stalls forever.
The bridge drops the orphaned pending future on cancellation; the modal may
linger in the UI until the user dismisses it or the next prompt replaces it.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base_tool import BaseTool, ToolResult
from ..infrastructure.logger import get_logger

# Hard ceiling for blocking on user input.
# Prevents a long-running unattended task from stalling indefinitely if the
# agent calls ask_human while no one is watching.
_ASK_HUMAN_TIMEOUT_S: int = 1800  # 30 minutes


class AskHumanTool(BaseTool):
    """Ask the user a clarifying question and return their text reply."""

    is_read_only = True
    is_concurrency_safe = False

    parameter_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "What to ask the user. Keep it short, specific, and "
                    "answerable in one sentence. Phrase it as the actual "
                    "question the user will see in the modal — no prefix "
                    "like 'I need to ask:' and no chain-of-thought."
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None) -> None:
        super().__init__("ask_human", ctx=ctx)
        self.logger = get_logger()

    async def execute(self, **kwargs: Any) -> ToolResult:
        question = kwargs.get("question", "")
        if not isinstance(question, str) or not question.strip():
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error="ask_human requires a non-empty 'question' string.",
            )

        # ctx + IM are injected by the SessionContext wiring; the per-session
        # InteractionManager forwards to the renderer (GUI) or stderr (CLI).
        im = getattr(self.ctx, "interaction_manager", None) if self.ctx else None
        if im is None:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    "ask_human is unavailable (no interaction manager in this "
                    "session). Pick a sensible default and continue."
                ),
            )

        try:
            answer = await asyncio.wait_for(
                im.request_user_text(question.strip()),
                timeout=_ASK_HUMAN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    f"No answer within {_ASK_HUMAN_TIMEOUT_S}s — the user may be "
                    "away. Proceed with a sensible default."
                ),
            )

        answer = (answer or "").strip()
        if not answer:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=(
                    "The user dismissed the question without answering. "
                    "Proceed with a sensible default."
                ),
            )

        return ToolResult(
            success=True,
            output=answer,
            tool_name=self.name,
            tool_parameters=kwargs,
        )

