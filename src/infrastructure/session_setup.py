"""
Session Setup — Context provider that prepares the interactive session tool.

Activation is purely Planner-driven: when "session" appears in
step.tools_required, FlowController invokes prepare() to inject a usage
hint into effective_goal. There is no keyword scan — if the Planner did
not declare session, this provider does nothing.

The session tool itself spawns local subprocesses directly; no credential
or resource preparation is needed. prepare() exists solely to remind the
agent which of the 4 irreplaceable scenarios applies and how to use alias
for cross-step session reuse.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .step_context_provider import StepContextProvider
from .logger import get_logger

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


class SessionContextProvider(StepContextProvider):
    """Inject the session-tool usage hint when the Planner declares it.

    Responsibility narrowed to hint injection — there is no credential
    prep or resource initialisation. The session tool's runtime registry
    handles subprocess lifecycle directly.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "session"

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        return (
            "[Session activated] Confirm ONE of the 4 scenarios in the session "
            "tool description applies (state persistence / watch+interject / "
            "tty-bound / user asked to watch). Otherwise use shell or ssh.\n"
            "If a previous step already opened a session for this device/host, "
            "pass alias='<stable-name>' on open() (e.g., alias='adb_main' or "
            "alias='ssh_<host>') so the registry reuses the live session "
            "instead of spawning a new one."
        )
