"""
ContextProvider abstraction.

A ``ContextProvider`` is a per-tool setup hook that runs alongside the
``PersistentAgent``. When the planner activates an on-demand tool (browser,
ssh, desktop, ...), the matching provider's ``on_tool_activated`` runs once;
its ``before_item`` runs before each item the agent picks up while that tool
is active. ``before_item`` may return a hint string that is appended to the
agent's per-item context block (``[Host Context]`` segment in the bottom
user message).

Three classes:

* ``ItemContext`` — minimal per-item view (3 fields) handed to ``before_item``.
* ``ProviderCache`` — namespaced dict store shared across providers, holds
  per-tool caches (cred files, "already prepared" flags, etc).
* ``ContextProvider`` — abstract base. ``on_tool_activated`` for one-shot
  session setup; ``before_item`` for per-item hint. Subclasses override only
  what they need; eligibility for per-item dispatch is determined by whether
  ``before_item`` is overridden (default returns ``None``, which
  ``FlowControllerV2`` filters out when collecting hints).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .interaction_manager import InteractionManager
    from .shared_checklist import CheckListItem


# ── ItemContext ─────────────────────────────────────────────────────────────

@dataclass
class ItemContext:
    """Per-item context handed to ``ContextProvider.before_item()``.

    Decouples providers from ``CheckListItem`` — they see only the three fields
    they actually need:

      * ``item_id``     — opaque identifier for logging.
      * ``instruction`` — the ``CheckListItem.instruction`` text (canonical
        instruction string, no separate goal/description split).
      * ``ssh_target``  — empty for local items; ``"user@host"`` when the
        planner has marked the item for remote execution. Used by SSH and
        RemoteHandQ providers to look up per-host credentials.
    """
    item_id: str
    instruction: str
    ssh_target: str = ""

    @classmethod
    def from_item(cls, item: "CheckListItem") -> "ItemContext":
        return cls(
            item_id=item.item_id,
            instruction=item.instruction,
            ssh_target=item.ssh_target,
        )


# ── ProviderCache ───────────────────────────────────────────────────────────

class ProviderCache:
    """Namespaced dict store shared across providers.

    Each provider gets its own namespace (``"ssh"``, ``"browser"``, ``"desktop"``,
    ``"email"``, ``"teams"``, ``"remote_handq"``, ...). Within a namespace, keys
    are typically ``"default"`` for session-scoped providers or hostnames for
    per-host providers. Values are arbitrary dicts. Single generic
    ``get`` / ``set`` / ``clear`` surface — providers don't roll their own
    per-tool caches.
    """

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    def get(self, namespace: str, key: str = "default") -> Optional[Dict[str, Any]]:
        return self._store.get(namespace, {}).get(key)

    def set(self, namespace: str, key: str, value: Dict[str, Any]) -> None:
        self._store.setdefault(namespace, {})[key] = dict(value)

    def clear(self, namespace: str) -> None:
        self._store.pop(namespace, None)


# ── ContextProvider ─────────────────────────────────────────────────────────

class ContextProvider(ABC):
    """Abstract base for tool-scoped setup providers.

    Subclasses must declare ``tool_name`` and override one of two lifecycle
    hooks (or both):

      * ``on_tool_activated`` — runs once when the planner first activates this
        tool via ``tools_needed`` in the planner's output. Use for session-wide
        setup (warm up a browser, prepare a Teams session, populate a
        first-touch hint flag in the cache).

      * ``before_item`` — runs before each item the agent picks up while this
        tool is in ``checklist.active_tools``. Use for per-item setup that
        depends on item context (SSH credentials for the item's ``ssh_target``,
        remote HandQ discovery on a per-host basis).

    Both default to no-op so subclasses opt in to whichever lifecycle they
    need.

    Side effects are allowed (write credential files, set provider cache
    entries, prompt the user via ``InteractionManager``) and the methods
    must be idempotent — calling them multiple times for the same item /
    tool activation must be safe.
    """

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """The on-demand tool name this provider serves.

        Must match a tool registered in ``ToolRegistry`` with ``on_demand=True``
        (e.g. ``"ssh"``, ``"session"``, ``"browser"``, ``"desktop"``).
        ``FlowControllerV2`` invokes this provider's hooks iff the planner has
        added this name to the active tool set.
        """

    async def on_tool_activated(
        self,
        im: "InteractionManager",
        cache: ProviderCache,
    ) -> None:
        """One-shot session setup when the planner first activates this tool.

        Default: no-op. Override for session-scoped initialisation that does
        not depend on a specific item.
        """
        return None

    async def before_item(
        self,
        ctx: ItemContext,
        im: "InteractionManager",
        cache: ProviderCache,
    ) -> Optional[str]:
        """Per-item hint string. Return ``None`` to skip injection.

        Default: returns ``None``. Override to return a hint that
        ``FlowControllerV2`` will append to the agent's ``[Host Context]``
        block on the bottom user message for the current item.
        """
        return None

    # ── Planner table contributions ─────────────────────────────────────────

    def planner_description(self) -> str:
        """Markdown table row for the planner's tool-selection table.

        Format (pipe-delimited, no leading/trailing pipes):
            ``\\`tool_name\\` | Activate-when description | Decision signal``

        Empty string excludes this tool from the dynamic table.
        """
        return ""

    def planner_routing_rule(self) -> str:
        """Single routing-rule line for the planner prompt.

        Example::

            "Web page interaction → tools_required: ['browser']"

        Empty string skips. Numbered sequentially by ``FlowControllerV2``
        when the planner prompt is built.
        """
        return ""

    def planner_antipatterns(self) -> List[str]:
        """List of anti-pattern strings for this tool.

        Each string is the bullet body without the leading ``❌``;
        ``FlowControllerV2`` prefixes ``  ❌ `` when assembling the section.
        """
        return []
