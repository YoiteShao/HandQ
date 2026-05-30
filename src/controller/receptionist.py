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

Skill activation (orthogonal to intent):
  Receptionist additionally returns ``activated_skills`` — names of skills that
  semantically match the user's request (LLM-judged) or were explicitly invoked
  via ``@skill-name`` in the message text. FlowController merges this list into
  the session-level active skill set; Planner then sees the activated skills'
  full bodies on its next observe_and_plan call. Activation is silent (no
  confirmation modal) — the receptionist mentions the activation in
  ``response_to_user`` but does not block on user confirmation.

Note: SAVE_SESSION and LIST_TEMPLATES are handled via CLI (handq --save / handq --list),
not through the receptionist.
"""
import asyncio
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, cast

from ..infrastructure.gep_template import list_templates_summary
from ..infrastructure.json_key_streamer import JsonKeyStreamer
from ..infrastructure.llm_pool import (
    NetworkUnavailableError,
    call_with_fallback,
    call_with_fallback_stream,
)
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.long_term_memory import LongTermMemory
from ..infrastructure.long_term_memory.candidates import submit_user_turn
from ..infrastructure.skills import SkillRegistry
from ..infrastructure.anthropic_streaming_service import StreamTextDeltaEvent, StreamDoneEvent
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


# ── @skill-name prescan ───────────────────────────────────────────────────────
# Lookbehind avoids matching @user (email-style), @property (decorator), or
# /@foo (path component). The pattern is intentionally narrow: anything after
# the @ sign that isn't a registered skill name is silently dropped, so this
# regex is a *filter* not an *extractor*.
_SKILL_MENTION_RE = re.compile(r"(?<![\w/.])@([a-zA-Z0-9_\-]{1,64})")


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

    Note: there is intentionally no NETWORK_ERROR intent. When the LLM
    is unreachable the receptionist's evaluate_* methods raise
    :class:`NetworkUnavailableError`; the InteractionManager dispatcher
    silent-skips that case so the user message is neither displayed nor
    enqueued. The frontend shows the "server unreachable" indicator
    based on the on_network_event signal coming from the LLM pool.
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
    deferred_actions    — concise items the receptionist committed to in
                          response_to_user. Non-empty list forces intent=REPLAN
                          so the planner sees every promised future action;
                          empty list means the reply made no commitments.
    activated_skills    — skill names the receptionist (or the user via
                          @skill-name) is asking the FlowController to add to
                          the session-level active set. Orthogonal to intent —
                          a skill activation can accompany ANY intent value.
                          The list is already filtered against SkillRegistry,
                          so unknown names never reach the FlowController.
    """
    intent: UserMessageIntent
    response_to_user: str          # always required
    reasoning: str = ""
    context_for_planner: str = ""  # enriched message forwarded to Planner
    gep_suggested: bool = False
    matched_template_id: str = ""
    deferred_actions: List[str] = field(default_factory=list)
    activated_skills: List[str] = field(default_factory=list)


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


# ── Skill prompt block builder ────────────────────────────────────────────────

def _build_skills_section(active: Iterable[str]) -> str:
    """
    Emit the entire skill-related prompt block, or "" when no skill is
    installed. Both the [Active / Available Skills] sections AND the teaching
    that tells the LLM to populate ``activated_skills`` live here, so that a
    user with no skills installed pays zero token cost — the receptionist's
    base system prompt and JSON schema stay clean.

    Strengthened language follows Anthropic's Claude Code skill prompt:
      • BLOCKING REQUIREMENT on match — list the skill in activated_skills.
      • NEVER mention a skill in response_to_user without listing it.
      • Never invent skill names not in [Available Skills].
      • Don't re-list skills already in [Active Skills].
    """
    try:
        registry = SkillRegistry.get()
    except Exception:
        return ""
    all_names = registry.names()
    if not all_names:
        return ""

    active_set = {n for n in active if registry.has(n)}
    inactive = [n for n in all_names if n not in active_set]

    lines: List[str] = ["[Skills — additional methodology you may activate]"]

    if active_set:
        lines.append("\nActive Skills (already on for this session — do NOT re-list):")
        for name in sorted(active_set):
            entry = registry.get_skill(name)
            if entry is None:
                continue
            lines.append(f"  - {entry.name}: {entry.description}")

    if inactive:
        lines.append("\nAvailable Skills (off — list in `activated_skills` to turn on):")
        for name in inactive:
            entry = registry.get_skill(name)
            if entry is None:
                continue
            lines.append(f"  - {entry.name}: {entry.description}")

    lines.append(
        "\nRules:\n"
        "  - When the user's request semantically matches an Available Skill "
        "(same task type, same domain — match on meaning, not keywords), this "
        "is a BLOCKING REQUIREMENT: list the skill name in `activated_skills`. "
        "If multiple match, list them all.\n"
        "  - NEVER mention a skill in `response_to_user` without also listing "
        "it in `activated_skills` — the planner only sees what the JSON field "
        "carries; promising the user \"I'll apply X skill\" without activating "
        "it is a leak.\n"
        "  - Never invent skill names. Only names that appear above are valid.\n"
        "  - Skills in [Active Skills] are already on — do NOT re-list them.\n"
        "  - Skill activation is ORTHOGONAL to intent: chat / task / "
        "respond_only / replan / gep_confirm can all carry skill activation.\n"
        "  - Explicit @skill-name mentions in the user's message are activated "
        "automatically — you don't need to echo them.\n"
        "  - When in doubt, leave `activated_skills` empty. Over-activation "
        "pollutes the planner's context."
    )
    lines.append(
        "\nJSON output extension: in addition to your normal response fields, "
        "include `\"activated_skills\": [\"name1\", \"name2\"]` (or `[]` when "
        "no Available Skill matches)."
    )

    return "\n".join(lines).rstrip() + "\n"


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
        shell_history_path: Optional[str] = None,
        initial_task_context: Optional[str] = None,
    ):
        if not llm_services:
            raise ValueError("Receptionist requires at least one LLMService in llm_services")
        self._services: List[LLMService] = list(llm_services)

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

        # Session-level active skills (names). Set by FlowController via
        # set_active_skills(). Used to render the [Active Skills] block and
        # to exclude already-active entries from the [Available Skills] menu.
        self._active_skills: Set[str] = set()

        self.logger.info("Receptionist initialized", component="Receptionist")

    # ── Skill activation passthrough ──────────────────────────────────────────

    def set_active_skills(self, names: Iterable[str]) -> None:
        """Set the session-level active skill names.

        Called by FlowController after every successful skill activation so
        that the receptionist's prompt blocks reflect the current active
        set on the next user message. The set is taken as authoritative —
        callers are responsible for filtering against SkillRegistry first.
        """
        self._active_skills = {str(n).strip() for n in names if str(n).strip()}

    def _extract_at_mentions(self, text: str) -> Set[str]:
        """Return the registered skill names mentioned via ``@name`` in *text*.

        Unknown @mentions (decorator names, email handles, paths, made-up
        skills) are filtered out — only names that resolve in SkillRegistry
        survive. This makes the prescan a defense-in-depth filter, not a
        hostile name extractor.
        """
        if not text or "@" not in text:
            return set()
        try:
            registry = SkillRegistry.get()
        except Exception:
            return set()
        found = set()
        for match in _SKILL_MENTION_RE.findall(text):
            if registry.has(match):
                found.add(match)
        return found

    # ── Conversation context helper ───────────────────────────────────────────

    def _format_conversation_history(self) -> str:
        """
        Format conversation_history as a readable string for prompt injection.

        Excludes the current turn (last 2 entries: user + assistant). Returns
        the full prior history without truncation: a sliding window would
        invalidate the prefix every time it shifts and break KV-cache for the
        rest of the prompt. The message processor commits one turn per request,
        so the prior history is append-only across consecutive calls — perfect
        for cache reuse. Compress older turns (planner-style) only when token
        cost actually warrants it.
        """
        prior = self.conversation_history[:-2] if len(self.conversation_history) >= 2 else []
        if not prior:
            return "(none)"
        lines = []
        for turn in prior:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        return "\n".join(lines)

    @staticmethod
    def _build_context_for_planner(
        raw_message: str,
        context_summary: str,
        deferred_actions: Optional[List[str]] = None,
    ) -> str:
        deferred_actions = deferred_actions or []
        if not context_summary and not deferred_actions:
            return raw_message
        parts: List[str] = []
        if context_summary:
            parts.append(f"[Context from prior conversation]\n{context_summary}")
        if deferred_actions:
            parts.append(
                "[Deferred actions promised to user]\n"
                + "\n".join(f"  - {a}" for a in deferred_actions)
            )
        parts.append(f"[Current request]\n{raw_message}")
        return "\n\n".join(parts)

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

    # ── Long-term memory helper ───────────────────────────────────────────────

    async def _build_long_term_block(self, query: str) -> str:
        """Recall memory + knowledge for *query* and format as one XML block.

        Thin wrapper around :meth:`LongTermMemory.format_context_block`.

        Latency note: receptionist runs on the per-message hot path, so we
        opt OUT of stage-3 LLM rerank (~3s) and rely on the BM25 + dense
        + RRF fused order. Empirically that's enough precision for intent
        classification — the receptionist's own LLM call already filters
        out low-relevance noise. The 3s saved per message is far more
        valuable than the marginal precision gain from rerank.

        Falls back to "" on any error so receptionist replies are never
        blocked by LTM problems.
        """
        try:
            ltm = LongTermMemory.get()
        except Exception:
            return ""
        try:
            return await ltm.format_context_block(query=query, rerank=False)
        except Exception:
            self.logger.debug(
                "LongTermMemory.format_context_block failed",
                component="Receptionist",
            )
            return ""

    def _submit_user_turn_candidate(
        self, raw_message: str, current_goal: Optional[str] = None,
    ) -> None:
        """Fire-and-forget submission of the user's raw message as a triage
        candidate. Failures are swallowed — long-term memory must never block
        the user-facing reply path.
        """
        try:
            ltm = LongTermMemory.get()
        except Exception:
            return
        try:
            asyncio.create_task(
                submit_user_turn(
                    ltm=ltm,
                    msg_id=str(uuid.uuid4()),
                    user_message=raw_message,
                    current_goal=current_goal,
                ),
                name="ltm-submit-user-turn",
            )
        except RuntimeError:
            # No running loop — extremely rare in receptionist context, but
            # makes the helper safe to call from sync code paths.
            self.logger.debug(
                "submit_user_turn skipped (no running loop)",
                component="Receptionist",
            )
        except Exception:
            self.logger.debug(
                "submit_user_turn dispatch failed",
                component="Receptionist",
            )

    # ── Context message builder ───────────────────────────────────────────────

    async def _build_context_messages(
        self,
        *,
        query_for_long_term: str = "",
        include_long_term_memory: bool = True,
        include_shell_history: bool = True,
        include_conversation_history: bool = True,
        include_templates: bool = True,
        include_skills: bool = True,
    ) -> List[Dict[str, str]]:
        """
        Build interleaved user/assistant message pairs for context injection.

        Each non-empty context block is sent as a standalone user message
        immediately acknowledged by a brief assistant reply, keeping the
        final user turn (the actual request) clean.

        Order (most-static → most-volatile) for KV cache hit rate:
          1. long-term memory  — survives across sessions, recalled per query.
          2. templates          — changes only when a GEP template is saved.
          3. skills             — append-only within a session (active set
                                  grows but never shrinks), so the prefix
                                  stays stable for early messages.
          4. conversation       — append-only within a session (no truncation,
                                  so the prefix stays stable).
          5. shell history      — sliding window (Linux only).
        """
        extra: List[Dict[str, str]] = []

        if include_long_term_memory:
            ltm = await self._build_long_term_block(query_for_long_term)
            if ltm:
                extra.append({"role": "user",      "content": ltm})
                extra.append({"role": "assistant", "content": "Noted."})

        if include_templates:
            templates = _build_templates_section()
            if templates:
                extra.append({"role": "user",      "content": templates})
                extra.append({"role": "assistant",  "content": "Noted."})

        if include_skills:
            skills = _build_skills_section(self._active_skills)
            if skills:
                extra.append({"role": "user",      "content": skills})
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

        Passes ``wait_on_network_down=False`` so that — if the pool's TCP
        probe confirms the LLM endpoint is unreachable — the wrapper
        raises :class:`NetworkUnavailableError` instead of waiting.
        Receptionist runs on the user-facing hot path; pausing
        indefinitely would leave the user staring at a frozen cursor.
        The InteractionManager catches the exception and silent-skips.
        """
        raw = cast(LLMChatResult, await call_with_fallback(
            self._services,
            dict(messages=messages),
            on_fallback=lambda idx, e: self.logger.warning(
                f"Receptionist {log_context} fallback to index {idx}: {e}",
                component="Receptionist",
            ),
            wait_on_network_down=False,
        ))
        parsed = try_parse_json(raw.content or "")
        return parsed if isinstance(parsed, dict) else None

    async def _call_and_parse_streaming(
        self,
        messages: List[Dict[str, str]],
        log_context: str,
        on_response_chunk: Callable[[str], None],
    ) -> Optional[Dict]:
        """
        Streaming variant of _call_and_parse(). Forwards response_to_user
        chunks to on_response_chunk as they arrive, then returns the full
        parsed JSON dict.

        Like :meth:`_call_and_parse`, opts OUT of pause-and-retry. A
        ``NetworkUnavailableError`` raised before any chunk arrives
        propagates to the caller; mid-stream errors fall through to the
        non-streaming retry path.
        """
        streamer = JsonKeyStreamer("response_to_user")
        accumulated = []

        try:
            async for event in call_with_fallback_stream(
                self._services,
                dict(messages=messages),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Receptionist {log_context} stream fallback to index {idx}: {e}",
                    component="Receptionist",
                ),
                wait_on_network_down=False,
            ):
                if isinstance(event, StreamTextDeltaEvent):
                    accumulated.append(event.text)
                    if not streamer.done:
                        for fragment in streamer.feed(event.text):
                            on_response_chunk(fragment)
                elif isinstance(event, StreamDoneEvent):
                    break

            full_text = "".join(accumulated)
            parsed = try_parse_json(full_text)
            return parsed if isinstance(parsed, dict) else None
        except NetworkUnavailableError:
            # Don't degrade to the non-streaming retry — that wrapper is
            # also network-aware and will hit the same wall. Let the
            # caller propagate this so InteractionManager can silent-skip.
            raise
        except Exception as e:
            self.logger.warning(
                f"Receptionist {log_context} streaming failed: {e} — falling back to non-streaming",
                component="Receptionist",
            )
            return await self._call_and_parse(messages, log_context)

    def _build_evaluation_result(
        self,
        parsed: Dict,
        raw_message: str,
        default_intent: UserMessageIntent,
        prescan_skills: Optional[Set[str]] = None,
    ) -> UserMessageEvaluation:
        """
        Build a UserMessageEvaluation from a parsed LLM response dict.

        Resolves the intent string via _INTENT_MAP (handles "task"/"chat" aliases
        and direct enum values), appends the assistant turn to conversation_history,
        and enriches context_for_planner for REPLAN/GEP_CONFIRM intents.

        Commitment-leak guard: if the LLM populates deferred_actions but leaves
        intent as RESPOND_ONLY (or chat), the intent is force-upgraded to REPLAN
        and the deferred actions are embedded into context_for_planner. This
        means even when the LLM mis-routes the intent, any explicit promise of
        future work still reaches the planner.

        ``prescan_skills`` is the set of @-mentions extracted from the raw
        message before the LLM call. Merged with the LLM's ``activated_skills``
        and filtered against SkillRegistry; the result is attached to the
        evaluation so the FlowController can union it into the session active
        set. Activation is independent of intent — a skill mention can ride
        on a chat / replan / gep_confirm intent equally.
        """
        intent_str = parsed.get("intent", default_intent.value)
        response = parsed.get("response_to_user") or ""
        reasoning = parsed.get("reasoning", "")
        context_summary = parsed.get("context_summary", "") or ""
        matched_template_id = parsed.get("matched_template_id", "") or ""

        deferred_raw = parsed.get("deferred_actions") or []
        if not isinstance(deferred_raw, list):
            deferred_raw = [deferred_raw]
        deferred_actions: List[str] = [
            str(a).strip() for a in deferred_raw if str(a).strip()
        ]

        # ── Skill activation ──────────────────────────────────────────────────
        # LLM-suggested + prescan @-mentions, both filtered against the
        # registry so an unknown name from either source is silently dropped.
        try:
            registry = SkillRegistry.get()
            registry_has = registry.has
        except Exception:
            registry_has = lambda _n: False  # noqa: E731

        skills_raw = parsed.get("activated_skills") or []
        if not isinstance(skills_raw, list):
            skills_raw = [skills_raw]
        llm_skills = {str(s).strip() for s in skills_raw if str(s).strip()}
        unknown = {s for s in llm_skills if not registry_has(s)}
        if unknown:
            self.logger.warning(
                f"LLM returned unknown skill names {sorted(unknown)} — dropping",
                component="Receptionist",
            )
            llm_skills -= unknown
        merged_skills = sorted((prescan_skills or set()) | llm_skills)

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

        # Commitment-leak guard: deferred_actions present but routed as
        # RESPOND_ONLY → force REPLAN so the planner sees the promised work.
        if deferred_actions and intent == UserMessageIntent.RESPOND_ONLY:
            self.logger.warning(
                f"Receptionist returned respond_only with deferred_actions={deferred_actions} "
                f"— forcing intent=replan to avoid commitment leak",
                component="Receptionist",
            )
            intent = UserMessageIntent.REPLAN

        if not response:
            response = self._default_response(intent, raw_message)

        # FR-3: record assistant turn
        self.conversation_history.append({"role": "assistant", "content": response})

        if intent in (UserMessageIntent.REPLAN, UserMessageIntent.GEP_CONFIRM):
            context_for_planner = self._build_context_for_planner(
                raw_message, context_summary, deferred_actions
            )
        else:
            context_for_planner = raw_message

        return UserMessageEvaluation(
            intent=intent,
            response_to_user=response,
            reasoning=reasoning,
            context_for_planner=context_for_planner,
            gep_suggested=(intent == UserMessageIntent.GEP_CONFIRM),
            matched_template_id=matched_template_id,
            deferred_actions=deferred_actions,
            activated_skills=merged_skills,
        )

    # ── Initial goal classification ───────────────────────────────────────────

    async def classify_initial_goal(
        self,
        user_input: str,
        on_response_chunk: Optional[Callable[[str], None]] = None,
    ) -> UserMessageEvaluation:
        """
        Classify the user's initial message.

        The LLM decides the intent (task / chat / gep_confirm / gep_decline) —
        no hardcoded keyword overrides. Template matching is driven by the
        available GEP template list injected into the system prompt.

        Falls back to REPLAN (task) on any LLM failure.
        """
        # FR-3: record user turn
        self.conversation_history.append({"role": "user", "content": user_input})

        # Submit a long-term-memory candidate so durable preferences in the
        # initial goal are captured even if the user never sends a follow-up.
        self._submit_user_turn_candidate(user_input)

        # @-prescan — extracted ONCE before the LLM call so it survives
        # network failures and parse failures further down. Filtered against
        # SkillRegistry by _extract_at_mentions; unknown @names drop silently.
        prescan_skills = self._extract_at_mentions(user_input)

        messages = [
            {"role": "system", "content": CLASSIFY_INITIAL_GOAL_SYSTEM_PROMPT},
            *(await self._build_context_messages(query_for_long_term=user_input)),
            {"role": "user", "content": CLASSIFY_INITIAL_GOAL_TEMPLATE.format(user_input=user_input)},
        ]

        try:
            if on_response_chunk is not None:
                parsed = await self._call_and_parse_streaming(
                    messages, "classify_initial_goal", on_response_chunk
                )
            else:
                parsed = await self._call_and_parse(messages, "classify_initial_goal")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, user_input,
                    default_intent=UserMessageIntent.REPLAN,
                    prescan_skills=prescan_skills,
                )
                if on_response_chunk is not None:
                    evaluation._streamed = True  # type: ignore[attr-defined]
                self.logger.info(
                    f"Initial goal classification: intent={evaluation.intent.value}, "
                    f"matched_template_id={evaluation.matched_template_id!r}, "
                    f"activated_skills={evaluation.activated_skills}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except NetworkUnavailableError:
            self._rewind_user_turn()
            raise
        except Exception as e:
            self.logger.warning(
                f"classify_initial_goal LLM call failed: {e} — defaulting to task",
                component="Receptionist",
            )

        # Safe fallback: treat as task. Even on LLM failure we still honour
        # explicit @-mentions because the user typed them deliberately.
        fallback_response = "Got it, I'll start working on that."
        self.conversation_history.append({"role": "assistant", "content": fallback_response})
        return UserMessageEvaluation(
            intent=UserMessageIntent.REPLAN,
            response_to_user=fallback_response,
            reasoning="LLM classification failed; defaulting to task.",
            context_for_planner=user_input,
            activated_skills=sorted(prescan_skills),
        )

    # ── GEP confirmation window ───────────────────────────────────────────────

    async def evaluate_gep_confirmation(
        self,
        user_input: str,
        template_name: str,
        template_description: str,
        guide_steps_summary: str = "",
        on_response_chunk: Optional[Callable[[str], None]] = None,
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
        self._submit_user_turn_candidate(user_input)

        steps_section = (
            f"Steps:\n{guide_steps_summary}\n" if guide_steps_summary else ""
        )

        messages = [
            {"role": "system", "content": GEP_CONFIRMATION_WINDOW_SYSTEM_PROMPT},
            *(await self._build_context_messages(
                query_for_long_term=user_input,
                include_shell_history=False,
                include_templates=False,
            )),
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
            if on_response_chunk is not None:
                parsed = await self._call_and_parse_streaming(
                    messages, "evaluate_gep_confirmation", on_response_chunk
                )
            else:
                parsed = await self._call_and_parse(messages, "evaluate_gep_confirmation")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, user_input, default_intent=UserMessageIntent.RESPOND_ONLY
                )
                if on_response_chunk is not None:
                    evaluation._streamed = True  # type: ignore[attr-defined]
                self.logger.info(
                    f"GEP confirmation evaluation: intent={evaluation.intent.value}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except NetworkUnavailableError:
            self._rewind_user_turn()
            raise
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
        on_response_chunk: Optional[Callable[[str], None]] = None,
    ) -> UserMessageEvaluation:
        """
        Classify a user message while a task is executing (FR-1, FR-2, FR-3).

        Supports intents: respond_only | replan.
        GEP intents (gep_confirm / gep_decline) are not available mid-task.
        """
        # FR-3: record user turn
        self.conversation_history.append({"role": "user", "content": message})
        self._submit_user_turn_candidate(message, current_goal=goal)

        # @-prescan — same logic as classify_initial_goal: extract once,
        # filter against registry, and apply even if the LLM call fails.
        prescan_skills = self._extract_at_mentions(message)

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
            *(await self._build_context_messages(
                query_for_long_term=message, include_templates=False,
            )),
            {"role": "user", "content": final_user_content},
        ]

        try:
            if on_response_chunk is not None:
                parsed = await self._call_and_parse_streaming(
                    messages, "evaluate_user_message", on_response_chunk
                )
            else:
                parsed = await self._call_and_parse(messages, "evaluate_user_message")
            if parsed is not None:
                evaluation = self._build_evaluation_result(
                    parsed, message,
                    default_intent=UserMessageIntent.REPLAN,
                    prescan_skills=prescan_skills,
                )
                if on_response_chunk is not None:
                    evaluation._streamed = True  # type: ignore[attr-defined]
                self.logger.info(
                    f"Message evaluation: intent={evaluation.intent.value}, "
                    f"matched_template_id={evaluation.matched_template_id!r}, "
                    f"activated_skills={evaluation.activated_skills}, "
                    f"reasoning={evaluation.reasoning}",
                    component="Receptionist",
                )
                return evaluation
        except NetworkUnavailableError:
            self._rewind_user_turn()
            raise
        except Exception as e:
            self.logger.warning(
                f"evaluate_user_message LLM call failed: {e} — defaulting to REPLAN",
                component="Receptionist",
            )

        # Safe fallback — preserve any explicit @-mentions even when the LLM fails.
        fallback_response = "Got it — I'll incorporate your message into the plan on the next cycle."
        self.conversation_history.append({"role": "assistant", "content": fallback_response})
        return UserMessageEvaluation(
            intent=UserMessageIntent.REPLAN,
            response_to_user=fallback_response,
            reasoning="LLM evaluation failed; defaulting to replan.",
            context_for_planner=message,
            activated_skills=sorted(prescan_skills),
        )

    def _default_response(self, intent: UserMessageIntent, message: str) -> str:
        """Generate a minimal fallback response when the LLM omits response_to_user."""
        if intent == UserMessageIntent.RESPOND_ONLY:
            return "Noted."
        return "Got it — I'll adjust the plan on the next cycle."

    def _rewind_user_turn(self) -> None:
        """Pop the most-recently-pushed user turn from conversation_history.

        Called by evaluate_* methods when they re-raise
        :class:`NetworkUnavailableError`: the LLM never replied, so the
        history would otherwise be left with an orphaned user turn that
        misaligns the user/assistant pairs assumed by
        :meth:`_format_conversation_history`. Rewind keeps the history
        clean so that when the network comes back the next user message
        starts from a balanced state.
        """
        if (
            self.conversation_history
            and self.conversation_history[-1].get("role") == "user"
        ):
            self.conversation_history.pop()
