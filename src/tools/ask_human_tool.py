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
``request_user_text`` is intentionally blocking — the user takes seconds to
answer. ``execute()`` runs it on the asyncio default executor so the event
loop is not stalled while we wait for the modal.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base_tool import BaseTool, ToolResult
from ..controller.interaction_manager import InteractionManager
from ..infrastructure.logger import get_logger


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

    def __init__(self) -> None:
        super().__init__("ask_human")
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

        try:
            im = InteractionManager.get_instance()
        except RuntimeError as exc:
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=f"ask_human requires a running InteractionManager: {exc}",
            )

        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(
                None, im.request_user_text, question.strip()
            )
        except Exception as exc:
            self.logger.warning(
                f"ask_human request failed: {exc}", component="AskHumanTool"
            )
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=kwargs,
                error=f"ask_human dialog failed: {exc}",
            )

        if isinstance(answer, str) and answer.strip():
            return ToolResult(
                success=True,
                output=answer,
                tool_name=self.name,
                tool_parameters=kwargs,
            )

        return ToolResult(
            success=False,
            output=None,
            tool_name=self.name,
            tool_parameters=kwargs,
            error=(
                "User dismissed the ask_human dialog without providing an "
                "answer. Proceed without their input or pick a sensible "
                "default; do not re-prompt."
            ),
        )
