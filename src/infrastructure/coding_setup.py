# -*- coding: utf-8 -*-
"""
CodingContextProvider — injects coding-mode discipline into effective_goal.

Activates when the Planner declares "coding" in step.tools_required.  Unlike
ssh / browser / desktop / session providers, "coding" does not correspond to
a registered on-demand tool — it is a *hint-only* provider.  The matching
tool name is silently filtered out by ToolRegistry.create_all_tool_instances
because no tool with on_demand=True is named "coding", so the agent's tool
schema is unaffected.

The hint covers ONLY behavioral / semantic rules that tool usage_guides
cannot teach (scope discipline, comment philosophy, run-the-build
verification, code-level security, git rules, honest reporting).
Mechanical contracts (edit exact-match, read-before-write, dangerous-
command refusal) already live in the relevant tool descriptions and are
NOT duplicated here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .step_context_provider import StepContextProvider
from ..agent.runtime_agent_prompts import CODING_HINT

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from ..infrastructure.memory import Memory
    from ..models.plan import Step


class CodingContextProvider(StepContextProvider):
    """Append CODING_HINT to effective_goal when the planner declares 'coding'."""

    @property
    def tool_name(self) -> str:
        return "coding"

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        return CODING_HINT
