# -*- coding: utf-8 -*-
"""
StepContextProvider — Generic interface for dynamic step context injection.

FlowController holds a list of StepContextProvider instances.  Before each
step executes, every registered provider is asked:
  1. Does this step need my context?  (matches)
  2. If yes, prepare and return a hint string to append to effective_goal.  (prepare)

This decouples context-injection concerns (SSH credentials, DB connections,
API tokens, etc.) from the core orchestration loop.  SSH is the first
concrete implementation; future providers follow the same interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from ..infrastructure.memory import Memory
    from ..models.plan import Step


class StepContextProvider(ABC):
    """
    Abstract base class for dynamic step context providers.

    Implementations are registered with FlowController via
    ``register_step_context_provider()``.  For each step, the controller
    calls ``matches()`` first; only if it returns True does it call
    ``prepare()``.

    ``prepare()`` may have side effects (write credential files, store
    secrets in the OS keyring, update Memory) and must be idempotent —
    calling it multiple times for the same step should be safe.
    """

    @abstractmethod
    def matches(self, step: "Step") -> bool:
        """
        Return True if this step requires context from this provider.

        Implementations should be fast (keyword scan, attribute check) and
        must not perform I/O or network calls.
        """

    @abstractmethod
    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        """
        Prepare context for the step and return a hint string.

        The returned string is appended to ``effective_goal`` before the
        RuntimeAgent is created.  Return ``None`` to skip injection (e.g.
        context already present in memory, or preparation failed
        non-fatally).

        Implementations may:
          - Prompt the user for credentials via ``interaction_manager``
          - Write credential files to disk
          - Store secrets in the OS keyring
          - Update ``memory`` so subsequent steps reuse the same context

        Raises:
            Exception: If preparation fails fatally and the step should not
                       proceed.  The caller (FlowController) will surface the
                       error to the user.
        """

    def extra_tool_names(self) -> List[str]:
        """
        Return on-demand tool names to activate for this step.

        Called by FlowController after prepare() completes (only when
        matches() returned True).  The returned names are passed to
        RuntimeAgent so it includes those tools in the LLM call.

        Default: no extra tools.  Override to request on-demand tools
        (e.g. SSHContextProvider returns ["ssh"]).
        """
        return []
