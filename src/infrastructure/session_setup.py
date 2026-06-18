"""
Session Setup — Context provider that emits the interactive-session usage hint.

The session tool spawns local subprocesses directly; no credential or resource
preparation is needed. ``before_item`` exists solely to remind the agent which
of the four irreplaceable scenarios applies and how to use ``alias`` for
cross-step session reuse.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .logger import get_logger
from ..controller_v2.context import ContextProvider, ItemContext, ProviderCache

if TYPE_CHECKING:
    from ..controller_v2.interaction_manager import InteractionManager


class SessionContextProvider(ContextProvider):
    """Inject the session-tool usage hint when the Planner activates ``session``.

    Hint is identical for every item — the session tool's runtime registry
    handles subprocess lifecycle directly so there is no first-vs-subsequent
    distinction.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "session"

    async def before_item(
        self,
        ctx: ItemContext,
        im: "InteractionManager",
        cache: ProviderCache,
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
