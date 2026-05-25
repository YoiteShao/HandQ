# -*- coding: utf-8 -*-
"""
StepContextProvider — Resource setup for declared on-demand tools.

FlowController holds a list of StepContextProvider instances. For each step,
the controller checks each provider's ``tool_name`` against the Planner's
declaration in ``step.tools_required``. When the tool is declared, the
controller calls ``prepare()`` to set up resources (credentials, profiles,
etc.) and append the returned hint string to ``effective_goal``.

Tool ACTIVATION is NOT this layer's concern — it is purely driven by
``step.tools_required``. There is no keyword safety net. If the Planner
under-declares, the agent fails for lack of a tool, returns an error JSON,
and the next observe_and_plan() round corrects ``tools_required``. This
costs one wasted iteration; it preserves agent focus and avoids context
inflation from keyword false-positives.

This class only handles cross-cutting setup work (credential preparation,
hint injection) for tools the Planner has approved.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from ..infrastructure.memory import Memory
    from ..models.plan import Step


class StepContextProvider(ABC):
    """
    Abstract base for tool-scoped setup providers.

    Subclasses must:
      1. Declare ``tool_name`` — the on-demand tool name they serve
         (matches a tool registered in ToolRegistry with on_demand=True).
      2. Implement ``prepare()`` — set up resources for the tool and
         optionally return a hint string to inject into the agent's goal.

    ``prepare()`` may have side effects (write credential files, store
    secrets, update Memory) and must be idempotent — calling it multiple
    times for the same step (preflight + execution) must be safe.
    """

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """The on-demand tool name this provider serves.

        Must match a tool registered in ToolRegistry with on_demand=True
        (e.g. 'ssh', 'session', 'browser', 'desktop'). FlowController
        triggers prepare() iff this value appears in step.tools_required.
        """

    @abstractmethod
    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        """
        Prepare resources for the step and return an optional hint string.

        The returned string is appended to ``effective_goal`` before the
        RuntimeAgent is created. Return ``None`` to skip injection.

        Raises:
            Exception: If preparation fails fatally and the step should not
                       proceed. FlowController surfaces the error to the
                       agent's effective_goal so the agent can report it.
        """
