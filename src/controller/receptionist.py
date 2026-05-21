"""
Receptionist - User-message classification and routing

Role
----
The Receptionist is the sole entry point for all user messages. It:
  • Classifies the initial user message as PLAN (task) or CHAT (FR-2).
  • While the Planner is busy, classifies incoming messages as REPLAN or RESPOND_ONLY (FR-2).
  • Maintains a full session-scoped conversation history — ALL user messages AND all
    Receptionist responses, including RESPOND_ONLY replies (FR-3).
    This is a superset of what the Planner sees (Planner only receives REPLAN messages).
  • Has its own independent LLM service (FR-4).
  • Reads the same context as the Planner (goal, current step, lookahead, task context) (FR-1).

GEP-aware intents (in addition to RESPOND_ONLY / REPLAN):
  GEP_CONFIRM    — user explicitly confirms using a specific GEP template.
  GEP_DECLINE    — user explicitly declines a pending GEP suggestion.

Note: SAVE_SESSION and LIST_TEMPLATES are handled via CLI (handq --save / handq --list),
not through the receptionist.
"""
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, cast

from ..infrastructure.gep_template import list_templates_summary
from ..infrastructure.llm_pool import call_with_fallback
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.utils import try_parse_json
from .receptionist_prompts import (
    CLASSIFY_INITIAL_GOAL_SYSTEM_PROMPT,
    CLASSIFY_INITIAL_GOAL_TEMPLATE,
    EVALUATE_USER_MESSAGE_SYSTEM_PROMPT,
    EVALUATE_USER_MESSAGE_TEMPLATE,
    GEP_CONFIRMATION_WINDOW_SYSTEM_PROMPT,
    GEP_CONFIRMATION_WINDOW_TEMPLATE,
)


# ── User-message intent types ─────────────────────────────────────────────────

class UserMessageIntent(Enum):
    """
    How the Receptionist classifies an incoming user message.

    RESPOND_ONLY   — query or chat; respond immediately, task continues uninterrupted.
    REPLAN         — any plan change (including stop/cancel requests);
                     triggers observe_and_plan which decides whether to
                     adjust the lookahead, pivot, or set should_terminate.

    Note: termination is NOT decided here.  observe_and_plan has full
    execution context and is the sole authority on whether to stop via
    Plan.should_terminate.
    """
    RESPOND_ONLY   = "respond_only"
    REPLAN         = "replan"
    GEP_CONFIRM    = "gep_confirm"
    GEP_DECLINE    = "gep_decline"


@dataclass
class UserMessageEvaluation:
    """
    Result of Receptionist.evaluate_user_message() or classify_initial_goal().

    intent              — routing decision (see UserMessageIntent).
    response_to_user    — always present; shown to the user immediately via
                          InteractionManager so they know their message was
                          received and what will happen next.
    reasoning           — one-sentence explanation (for logging / debugging).
    context_for_planner — when intent=REPLAN, contains the recent conversation
                          history prepended to the raw message so the Planner
                          has full context (e.g. prior RESPOND_ONLY answers that
                          the user is now referencing).  Equals the raw message
                          when there is no prior conversation context to add.
    gep_suggested       — True when the LLM matched a GEP template to the request.
    matched_template_id — template id when intent=GEP_CONFIRM; empty string otherwise.
    """
    intent: UserMessageIntent
    response_to_user: str          # always required
    reasoning: str = ""
    context_for_planner: str = ""  # enriched message forwarded to Planner
    gep_suggested: bool = False
    matched_template_id: str = ""


# ── Intent string → enum mapping ──────────────────────────────────────────────
# "task" and "chat" are aliases used by the initial-goal prompt; direct enum
# values ("replan", "respond_only", etc.) are used by the evaluate prompt.
# Unknown strings fall back to the caller-supplied default.

_INTENT_MAP: Dict[str, UserMessageIntent] = {
    "task":         UserMessageIntent.REPLAN,
    "chat":         UserMessageIntent.RESPOND_ONLY,
    "respond_only": UserMessageIntent.RESPOND_ONLY,
    "replan":       UserMessageIntent.REPLAN,
    "gep_confirm":  UserMessageIntent.GEP_CONFIRM,
    "gep_decline":  UserMessageIntent.GEP_DECLINE,
}


# ── GEP template helper ───────────────────────────────────────────────────────

def _build_templates_section() -> str:
    """
    Build the [Available GEP Templates] section for injection into LLM prompts.
    Returns an empty string when no templates exist.
    """
    import json as _json
    raw = list_templates_summary()
    templates: list = _json.loads(raw)
    if not templates:
        return ""
    lines = ["[Available GEP Templates]"]
    for t in templates:
        lines.append(
            f'  id={t["id"][:8]}  name="{t["name"]}"  v{t["version"]}\n'
            f'    {t["description"]}'
        )
        if t.get("params"):
            lines.append(f'    Params: {", ".join(t["params"])}')
        if t.get("steps"):
            lines.append(f'    Steps: {" → ".join(t["steps"])}')
    lines.append(
        "\nIf the user's request semantically matches one of the templates above "
        "(same task type, same workflow pattern, same domain), "
        "set intent to 'gep_confirm' and matched_template_id to the full template id. "
        "Match on meaning, not keywords — read the description to judge fit. "
        "If multiple match, pick the best one. If none match clearly, use 'task'.\n"
    )
    return "\n".join(lines) + "\n"


def _format_templates_list() -> str:
    """Format templates for display to the user."""
    import json as _json
    raw = list_templates_summary()
    templates: list = _json.loads(raw)
    if not templates:
        return "No GEP templates found yet. Use 'save session' after completing a task to create one."
    lines = ["Available GEP experience templates:\n"]
    for t in templates:
        lines.append(
            f"  [{t['id'][:8]}] {t['name']} (v{t['version']})\n"
            f"    {t['description']}"
        )
    return "\n".join(lines)


# ── Receptionist ──────────────────────────────────────────────────────────────

class Receptionist:
    """
    User-message classifier and router.

    FR-1: Reads the same context as the Planner.
    FR-2: Runs independently via InteractionManager's message processor.
    FR-3: Maintains a session-scoped conversation_history.
    FR-4: Uses its own independent LLM service.
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        working_directory: str = ".",
        shell_history_path: Optional[str] = None,
        initial_task_context: Optional[str] = None,
    ):
        if not llm_services:
            raise ValueError("Receptionist requires at least one LLMService in llm_services")
        self._services: List[LLMService] = list(llm_services)

        self.working_directory = working_directory
        self.shell_history_path: Optional[str] = shell_history_path
        self.logger = get_logger()

        # FR-3: session-scoped full conversation history.
        # If initial_task_context is provided (e.g. for a save/GEP flow), seed
        # the history so the receptionist starts aware of the task rather than
        # in an empty 'no task' state.
        if initial_task_context:
            self.conversation_history: List[Dict[str, str]] = [
                {"role": "user", "content": initial_task_context},
                {"role": "assistant", "content": "Understood. I will assist with this task."},
            ]
        else:
            self.conversation_history: List[Dict[str, str]] = []
        self.current_screen: str = ""

        self.logger.info("Receptionist initialized", component="Receptionist")

    # ── Conversation context helper ───────────────────────────────────────────

    def _format_conversation_history(self, max_turns: int = 30) -> str:
        """
        Format the last max_turns entries of conversation_history as a
        readable string for injection into the LLM prompt.

        Excludes the current turn (last 2 entries: user + assistant).
        Returns "(none)" when there is no prior history.
        """
        prior = self.conversation_history[:-2] if len(self.conversation_history) >= 2 else []
        if not prior:
            return "(none)"
        recent = prior[-(max_turns):]
        lines = []
        for turn in recent:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        return "\n".join(lines)

    @staticmethod
    def _build_context_for_planner(raw_message: str, context_summary: str) -> str:
        if not context_summary:
            return raw_message
        return (
            f"[Context from prior conversation]\n{context_summary}\n\n"
            f"[Current request]\n{raw_message}"
        )

    # ── Shell history helper ──────────────────────────────────────────────────

    def _read_shell_history(self) -> str:
        if not self.shell_history_path:
            return ""
        try:
            content = Path(self.shell_history_path).read_text(encoding="utf-8").strip()
            if not content:
                return "[Shell History]\nCurrent console command history is empty or unreadable\n"
            return "[Shell History]\n" + content + "\n"
        except Exception as exc:
            self.logger.debug(
                f"Could not read shell history from {self.shell_history_path!r}: {exc}",
                component="Receptionist",
            )
            return "[Shell History]\nCurrent console command history is empty or unreadable\n"

    # ── Context message builder ───────────────────────────────────────────────

    def _build_context_messages(
        self,
        *,
        include_shell_history: bool = True,
        include_conversation_history: bool = True,
        include_templates: bool = True,
    ) -> List[Dict[str, str]]:
        """
        Build interleaved user/assistant message pairs for context injection.

        Each non-empty context block is sent as a standalone user message
        immediately acknowledged by a brief assistant reply, keeping the
        final user turn (the actual request) clean.
        """
        extra: List[Dict[str, str]] = []

        # Order: static first → append-only next → sliding window last
        # This maximizes KV cache hit rate across consecutive calls.

        if include_templates:
            templates = _build_templates_section()
            if templates:
                extra.append({"role": "user",      "content": templates})
                extra.append({"role": "assistant",  "content": "Noted."})

        if include_conversation_history:
            conv = self._format_conversation_history()
            if conv != "(none)":
                extra.append({"role": "user",      "content": f"[Recent Conversation History]\n{conv}"})
                extra.append({"role": "assistant",  "content": "Noted."})

        if include_shell_history:
            shell_history = self._read_shell_history()
            if shell_history:
                extra.append({"role": "user",      "content": shell_history})
                extra.append({"role": "assistant",  "content": "Noted."})

        return extra

    # ── Shared private helpers ────────────────────────────────────────────────

    async def _call_and_parse(
        self,
        messages: List[Dict[str, str]],
        log_context: str,
    ) -> Optional[Dict]:
        """
        Call the LLM with fallback and return the parsed JSON dict.
        Returns None if the response cannot be parsed as a dict.
        Raises on LLM/network errors so callers can handle the fallback path.
        """
        raw = cast(LLMChatResult, await call_with_fallback(
            self._services,
            dict(messages=messages),
            on_fallback=lambda idx, e: self.logger.warning(
                f"Receptionist {log_context} fallback to index {idx}: {e}",
                component="Receptionist",
            ),
        ))
        parsed = try_parse_json(raw.content or "")
        return parsed if isinstance(parsed, dict) else None

    def _build_evaluation_result(
        self,
        parsed: Dict,
        raw_message: str,
        default_intent: UserMessageIntent,
    ) -> UserMessageEvaluation:
        """
        Build a UserMessageEvaluation from a parsed LLM response dict.

        Resolves the intent string via _INTENT_MAP (handles "task"/"chat" aliases
        and direct enum values), appends the assistant turn to conversation_history,
        and enriches context_for_planner for REPLAN/GEP_CONFIRM intents.
        """
        intent_str = parsed.get("intent", default_intent.value)
        response = parsed.get("response_to_user") or ""
        reasoning = parsed.get("reasoning", "")
        context_summary = parsed.get("context_summary", "") or ""
        matched_template_id = parsed.get("matched_template_id", "") or ""

        # Resolve intent: map lookup covers aliases; fallback tries direct enum value.
        lower = intent_str.lower()
        if lower in _INTENT_MAP:
            intent = _INTENT_MAP[lower]
        else:
            try:
                intent = UserMessageIntent(lower)
            except ValueError:
                self.logger.warning(
                    f"Unknown intent '{intent_str}' — defaulting to {default_intent.value}",
                    component="Receptionist",
                )
                intent = default_intent

        if not response:
            response = self._default_response(intent, raw_message)

        # FR-3: record assistant turn
        self.conversation_history.append({"role": "assistant", "content": response})

        context_for_planner = (
            self._build_context_for_planner(raw_message, context_summary)
            if intent in (UserMessageIntent.REPLAN, UserMessageIntent.GEP_CONFIRM)
            else raw_message
        )

        return UserMessageEvaluation(
            intent=intent,
            response_to_user=response,
            reasoning=reasoning,
            context_for_planner=context_for_planner,
            gep_suggested=(intent == UserMessageIntent.GEP_CONFIRM),
            matched_template_id=matched_template_id,
        )

    # ── Initial goal classification ───────────────────────────────────────────

    async def classify_initial_goal(self, user_input: str) -> UserMessageEvaluation:
        """
        Classify the user's initial message.

        The LLM decides the intent (task / chat / gep_confirm / gep_decline) —
        no hardcoded keyword overrides. Template matching is driven by the
        available GEP template list injected into the system prompt.

        Falls back to REPLAN (task) on any LLM failure.
        """
        # FR-3: record user turn
        self.conversation_history.append({"role": "user", "content": user_input})

        messages = [
            {"role": "system", "content": CLASSIFY_INITIAL_GOAL_SYSTEM_PROMPT},
            *self._build_context_messages(),
            {"role": "user", "content": CLASSIFY_INITIAL_GOAL_TEMPLATE.format(user_input=user_input)},
        ]

        try:
            parsed = await self._call_and_parse(messages, "classify_initial_goal")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, user_input, default_intent=UserMessageIntent.REPLAN
                )
                self.logger.info(
                    f"Initial goal classification: intent={evaluation.intent.value}, "
                    f"matched_template_id={evaluation.matched_template_id!r}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except Exception as e:
            self.logger.warning(
                f"classify_initial_goal LLM call failed: {e} — defaulting to task",
                component="Receptionist",
            )

        # Safe fallback: treat as task
        fallback_response = "Got it, I'll start working on that."
        self.conversation_history.append({"role": "assistant", "content": fallback_response})
        return UserMessageEvaluation(
            intent=UserMessageIntent.REPLAN,
            response_to_user=fallback_response,
            reasoning="LLM classification failed; defaulting to task.",
            context_for_planner=user_input,
        )

    # ── GEP confirmation window ───────────────────────────────────────────────

    async def evaluate_gep_confirmation(
        self,
        user_input: str,
        template_name: str,
        template_description: str,
        guide_steps_summary: str = "",
    ) -> UserMessageEvaluation:
        """
        Classify a user message during the GEP confirmation window.

        Unlike classify_initial_goal (which assumes no prior context) this
        method tells the LLM explicitly that a specific template was proposed
        and the user is responding to that proposal.

        Intents: gep_confirm | gep_decline | respond_only.
        Falls back to RESPOND_ONLY on LLM failure — never auto-confirms or
        auto-declines based on a failed call; the timeout handles the
        no-response case.
        """
        self.conversation_history.append({"role": "user", "content": user_input})

        steps_section = (
            f"Steps:\n{guide_steps_summary}\n" if guide_steps_summary else ""
        )

        messages = [
            {"role": "system", "content": GEP_CONFIRMATION_WINDOW_SYSTEM_PROMPT},
            *self._build_context_messages(
                include_shell_history=False,
                include_templates=False,
            ),
            {
                "role": "user",
                "content": GEP_CONFIRMATION_WINDOW_TEMPLATE.format(
                    template_name=template_name,
                    template_description=template_description,
                    steps_section=steps_section,
                    user_input=user_input,
                ),
            },
        ]

        try:
            parsed = await self._call_and_parse(messages, "evaluate_gep_confirmation")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, user_input, default_intent=UserMessageIntent.RESPOND_ONLY
                )
                self.logger.info(
                    f"GEP confirmation evaluation: intent={evaluation.intent.value}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except Exception as e:
            self.logger.warning(
                f"evaluate_gep_confirmation LLM call failed: {e} — defaulting to respond_only",
                component="Receptionist",
            )

        fallback_response = "I'm not sure I understood — could you confirm: should I use this template, skip it, or do you have a question?"
        self.conversation_history.append({"role": "assistant", "content": fallback_response})
        return UserMessageEvaluation(
            intent=UserMessageIntent.RESPOND_ONLY,
            response_to_user=fallback_response,
            reasoning="LLM evaluation failed; defaulting to respond_only.",
        )

    # ── User-message evaluation ───────────────────────────────────────────────

    async def evaluate_user_message(
        self,
        message: str,
        goal: str,
        current_step_description: Optional[str],
        lookahead_descriptions: List[str],
        task_context: str = "",
        accumulated_findings: str = "",
        agent_progress: str = "",
        completed_count: int = 0,
        remaining_count: int = 0,
    ) -> UserMessageEvaluation:
        """
        Classify a user message while a task is executing (FR-1, FR-2, FR-3).

        Supports intents: respond_only | replan.
        GEP intents (gep_confirm / gep_decline) are not available mid-task.
        """
        # FR-3: record user turn
        self.conversation_history.append({"role": "user", "content": message})

        current_step = current_step_description or "(no step currently executing)"
        lookahead = (
            "\n".join(f"  {i+1}. {d}" for i, d in enumerate(lookahead_descriptions))
            if lookahead_descriptions
            else "  (none)"
        )

        total = completed_count + remaining_count
        if total > 0:
            progress_section = f"Completed {completed_count} of ~{total} steps  |  {remaining_count} remaining in lookahead"
        else:
            progress_section = "(no steps recorded yet)"

        final_user_content = EVALUATE_USER_MESSAGE_TEMPLATE.format(
            goal=goal,
            progress_section=progress_section,
            current_step=current_step,
            lookahead=lookahead,
            task_context=task_context,
            accumulated_findings_section=(
                f"\n[Accumulated Findings & Failed Approaches]\n{accumulated_findings}\n"
                if accumulated_findings else ""
            ),
            agent_progress_section=(
                f"\n[In-flight Agent Status]\n{agent_progress}\n"
                if agent_progress else ""
            ),
            message=message,
        )

        messages = [
            {"role": "system", "content": EVALUATE_USER_MESSAGE_SYSTEM_PROMPT},
            *self._build_context_messages(include_templates=False),
            {"role": "user", "content": final_user_content},
        ]

        try:
            parsed = await self._call_and_parse(messages, "evaluate_user_message")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, message, default_intent=UserMessageIntent.REPLAN
                )
                self.logger.info(
                    f"Message evaluation: intent={evaluation.intent.value}, "
                    f"matched_template_id={evaluation.matched_template_id!r}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except Exception as e:
            self.logger.warning(
                f"evaluate_user_message LLM call failed: {e} — defaulting to REPLAN",
                component="Receptionist",
            )

        # Safe fallback
        fallback_response = "Got it — I'll incorporate your message into the plan on the next cycle."
        self.conversation_history.append({"role": "assistant", "content": fallback_response})
        return UserMessageEvaluation(
            intent=UserMessageIntent.REPLAN,
            response_to_user=fallback_response,
            reasoning="LLM evaluation failed; defaulting to replan.",
            context_for_planner=message,
        )

    def _default_response(self, intent: UserMessageIntent, message: str) -> str:
        """Generate a minimal fallback response when the LLM omits response_to_user."""
        if intent == UserMessageIntent.RESPOND_ONLY:
            return "Noted."
        return "Got it — I'll adjust the plan on the next cycle."
