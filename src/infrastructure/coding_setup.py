# -*- coding: utf-8 -*-
"""
CodingContextProvider — injects coding-mode discipline as a per-item hint.

Activates when the Planner declares ``coding`` in the active tool set. Unlike
ssh / browser / desktop / session providers, ``coding`` does not correspond
to a registered on-demand tool — it is a hint-only provider. The matching
tool name is silently filtered out by ``ToolRegistry.create_all_tool_instances``
because no tool with ``on_demand=True`` is named ``coding``, so the agent's
tool schema is unaffected.

The hint covers behavioural / semantic rules that tool usage_guides cannot
teach (scope discipline, comment philosophy, run-the-build verification,
code-level security, git rules, honest reporting). Mechanical contracts
(edit exact-match, read-before-write, dangerous-command refusal) live in
the relevant tool descriptions and are not duplicated here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..controller_v2.coding_hint import CODING_HINT
from ..controller_v2.context import ContextProvider, ItemContext, ProviderCache

if TYPE_CHECKING:
    from ..controller_v2.interaction_manager import InteractionManager


class CodingContextProvider(ContextProvider):
    """Append ``CODING_HINT`` to every item's host-context block when active."""

    @property
    def tool_name(self) -> str:
        return "coding"

    async def before_item(
        self,
        ctx: ItemContext,
        im: "InteractionManager",
        cache: ProviderCache,
    ) -> Optional[str]:
        return CODING_HINT
