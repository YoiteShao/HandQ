# -*- coding: utf-8 -*-
"""AskHumanContextProvider — emit restraint guidance when the planner
declares the ``ask_human`` tool.

The hint is intentionally short and stern: the agent should default to
deciding silently. The full hint fires on first activation per task; a brief
reminder fires on subsequent activations so token cost stays low without
losing the message.

Memory storage reuses ``Memory._browser_contexts`` under the key
``"ask_human"`` (the same trick ``WebSearchContextProvider`` uses) so no
schema change is required.

Windows-only — registered alongside browser/desktop/email/web_search in
``FlowController._register_default_providers``, and gated by the
``tool_ask_human.enabled`` interaction switch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


def _build_full_hint() -> str:
    return (
        "[Ask-Human Context — first activation in this task]\n"
        "The 'ask_human' tool opens a modal that interrupts the user and waits\n"
        "for their text reply. They will see exactly the string you pass as\n"
        "'question'. The reply comes back as the tool output.\n"
        "\n"
        "STRICT RESTRAINT — read before every call:\n"
        "  - Default to deciding silently from context. The user is busy;\n"
        "    each call to this tool steals their attention.\n"
        "  - Only call when the task literally cannot proceed without\n"
        "    information you (a) do not have, AND (b) cannot derive by\n"
        "    reading the project, asking the planner via your reasoning,\n"
        "    or making a reasonable default choice that's easy to revert.\n"
        "  - NEVER call to confirm a choice the user already made, to\n"
        "    second-guess your own plan, to surface intermediate decisions\n"
        "    you can make yourself, or to pick between cosmetic options.\n"
        "  - Phrase the question as the literal sentence the user will see.\n"
        "    No 'I need to ask…', no chain-of-thought leakage, no options\n"
        "    list — one short sentence, answerable in one sentence.\n"
        "  - One question per call. If you genuinely need two pieces of\n"
        "    information, ask the most important one first; you can ask\n"
        "    again later if their answer makes the second one necessary.\n"
        "  - If the user dismisses the dialog (empty reply), DO NOT re-ask\n"
        "    — they declined; pick a default and proceed.\n"
    )


def _build_brief_hint() -> str:
    return (
        "[Ask-Human Context] 'ask_human' is available — but use it sparingly.\n"
        "Default to deciding silently. Only call when you genuinely cannot\n"
        "proceed without information that you cannot derive. One short\n"
        "sentence per call; no options, no chain-of-thought."
    )


class AskHumanContextProvider(StepContextProvider):
    """Activate when the Planner declares ``ask_human`` in tools_required."""

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "ask_human"

    def planner_description(self) -> str:
        return (
            "`ask_human` | "
            "Ask the user ONE clarifying question via a modal dialog; blocks until they reply. "
            "Use ONLY when the step cannot proceed without a specific value that cannot be derived "
            "from context or safely defaulted (e.g. target environment, recipient address). "
            "Routing: `[\"ask_human\"]`. | "
            "Step needs a single value from the user that is not inferrable and cannot be guessed safely"
        )

    def planner_routing_rule(self) -> str:
        return (
            "Step requires one specific value from the user that cannot be inferred "
            "→ `tools_required: [\"ask_human\"]`"
        )

    def planner_antipatterns(self) -> list:
        return [
            '`["ask_human"]` to confirm choices the user already made, or for decisions you '
            "can make yourself — use it only when a value is genuinely unknown and cannot be safely defaulted",
        ]

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        cached = memory.get_browser_context("ask_human")
        if cached and cached.get("prepared"):
            return _build_brief_hint()
        memory.set_browser_context("ask_human", {"prepared": True})
        return _build_full_hint()
