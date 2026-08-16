"""
Orchestrator — the Coordinator. INTENT classification + mechanical queueing.

Every user message runs through INTENT (chat / queue / interrupt). chat is
answered directly. queue / interrupt mechanically enqueue the request as a
TaskSpec — no second LLM call, no item-splitting, no LLM-authored expected
outcomes. The agent decomposes, chooses tools, and discovers its own targets
(e.g. ssh_target) at tool-call time.

Completion is detected mechanically: when `TaskChannel.mark_current_done`
leaves nothing in flight and nothing pending, the Orchestrator composes the
final reply from the agent's last verified structured facts (skeleton-first,
no LLM re-judges it) — see `_compose_completion_reply`.

Skill awareness (the former ReceptionistMixin) is inlined here: the INTENT
system prompt is built with the live [Available Skills] menu + standing skill
bodies, rendered fresh from SkillRegistry on every call so panel toggles take
effect immediately.

Architectural decisions:
  - IDLE = task channel is empty. Once the first item is appended the system is
    permanently in ACTIVE; "completion" is detected when nothing is pending and
    no in-progress item remains.
  - Progress concerns (mechanical hard-stall detection on the agent side) are
    stored on TaskChannel and surfaced passively: INTENT already receives the
    `[Current Plan]` block (which renders any concern), so a user asking "how's
    it going?" gets an accurate answer without any proactive push.
  - Conversation history is per-session, append-only, and re-rendered into the
    user prompt every turn.
"""
import asyncio
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Set, cast

from ..infrastructure.json_key_streamer import JsonKeyStreamer
from ..infrastructure.llm_pool import (
    NetworkUnavailableError,
    call_with_fallback,
    call_with_fallback_stream,
)
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.anthropic_streaming_service import (
    StreamTextDeltaEvent,
    StreamDoneEvent,
)
from ..infrastructure.logger import get_logger
from ..infrastructure.long_term_memory._constants import RecallTier
from ..infrastructure.utils import try_parse_json
from .mention_preprocessing import preprocess_mentions
from .session_context import GoalState
from .task_channel import (
    TaskChannel,
    TaskSpec,
    TaskResult,
)
from .intent_prompts import INTENT_SYSTEM_PROMPT, INTENT_TEMPLATE

if TYPE_CHECKING:
    from .session_context import SessionContext


_EMPTY_PLAN_MARKER = "(empty — no active task)"

# LTM recall block cache TTL. Aligned with the LTM mutation floor
# (DREAM_INTERVAL_MIN_SEC / IDENTITY_CACHE_TTL_SEC = 60s): an accepted entry
# can't land in under one dream tick, so a ≤60s-stale recall block is within
# the subsystem's existing worst-case staleness contract.
_LTM_BLOCK_CACHE_TTL_SEC: float = 60.0

# Safety valve on standing-goal check-in loops: after this many consecutive
# unsatisfied judge verdicts for the same goal, stop re-queuing silently and
# hand control back to the user instead of burning API calls indefinitely on
# a goal the judge (or the agent) may never be able to satisfy.
_GOAL_MAX_ITERATIONS = 20


@dataclass
class _GoalVerdict:
    """One adversarial goal-verifier verdict.

    ``blocking`` distinguishes a plain not-yet-satisfied (retry may help) from a
    "needs a human" state: ``contradiction`` (evidence contradicts the
    objective) or ``unverifiable`` (can't be checked from available evidence).
    Both short-circuit the re-queue loop so the goal doesn't burn attempts up
    to the cap on something retrying can't fix.
    """

    satisfied: bool
    blocking: str = "none"   # none | contradiction | unverifiable
    rationale: str = ""

_GOAL_JUDGE_SYSTEM_PROMPT = """You are a skeptical verifier. Decide whether a \
STANDING CONDITION holds, given the objective, the REAL file changes made since \
the goal was declared (CHANGED_FILES + DIFF — ground truth, captured by the \
harness, not self-reported), and the agent's own REPORTED outcomes across every \
task since. This is NOT "did the most recent task succeed" — a task can complete \
successfully while the condition still does not hold (e.g. "tell me when CPU \
exceeds 90%" — a check task always "succeeds" by running the check; the CONDITION \
holds only when the check actually observed CPU > 90%). Conversely the condition \
may already hold even if the latest task failed or was abandoned.

Your default is REFUTED: if you are not convinced the condition holds, \
satisfied=false. Continuing costs only more work; a false "satisfied" stops real \
work prematurely.

But do NOT manufacture reasons to refute. Guard against these failure modes:

- ANTI-RATCHET. The bar does NOT rise between check-ins. Attempt #5 is judged \
against the SAME objective as attempt #1 — never a stricter one. Do not invent \
new requirements the objective never stated (missing edge cases, extra tests, \
stylistic preferences, "could be more robust") as grounds to refute. If the \
objective as written is met, satisfied=true even on the first attempt.

- AUDIT, DON'T AUTHOR. Judge the evidence the agent actually produced against the \
objective. You are auditing, not redesigning. "I would have done it differently" \
is not a refutation.

- HONESTY CHECK. If a REPORTED outcome claims a file was created/modified but that \
file is absent from CHANGED_FILES, the claim is unsubstantiated — that is grounds \
to refute (the agent may be reporting work it did not do). Trust CHANGED_FILES/DIFF \
over prose when they conflict.

Also classify whether this is even retry-able:
- blocking="none":         a normal not-yet-satisfied — retrying may help.
- blocking="contradiction": the evidence CONTRADICTS the objective in a way retry \
  won't fix (e.g. the objective is based on a false premise, or the world state \
  makes it impossible). Needs the user, not another attempt.
- blocking="unverifiable": the condition cannot be checked from the available \
  evidence (e.g. it depends on external state nobody captured). Needs the user.

Output JSON only:
{"satisfied": bool, "blocking": "none"|"contradiction"|"unverifiable", \
"rationale": "<one sentence citing the specific evidence (a changed file, a diff \
hunk, or a reported outcome) that decides it>"}"""


class Orchestrator:
    """The Coordinator: INTENT triage + mechanical queueing + completion relay."""

    def __init__(
        self,
        llm_services: List[LLMService],
        task_channel: TaskChannel,
        on_reply_to_user: Optional[Callable[[str], Any]] = None,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
        on_response_done: Optional[Callable[[], Any]] = None,
        on_state_changed: Optional[Callable[[str], Any]] = None,
        on_recall_started: Optional[Callable[[], Any]] = None,
        on_task_complete: Optional[Callable[[], Awaitable[None]]] = None,
        on_intent_classified: Optional[Callable[[str], Any]] = None,
        session_dir: Optional[str] = None,
        session_ctx: Optional["SessionContext"] = None,
    ):
        if not llm_services:
            raise ValueError("Orchestrator requires at least one LLMService")
        self._services: List[LLMService] = list(llm_services)
        self._task_channel = task_channel
        self.logger = get_logger()
        self._on_reply_to_user = on_reply_to_user
        self._on_response_chunk = on_response_chunk
        self._on_response_done = on_response_done
        # Activity-strip state hook. The agent emits "thinking"/"executing"
        # directly via the InteractionManager; the task-settled transition is
        # only visible here, so the orchestrator surfaces "idle" (task
        # complete, final reply sent) through this callback.
        self._on_state_changed = on_state_changed
        # LTM-recall-in-flight hook — surfaces a transient "recalling…" label
        # on the activity strip while INTENT's recall runs.
        self._on_recall_started = on_recall_started
        # Task-boundary cleanup hook. Fires when the task is finalized — i.e.
        # the task channel is empty and the whole task is really finished, not just
        # an item boundary. FlowControllerV2 wires this to the browser-holder
        # close so Chromium doesn't linger between tasks in the same session.
        # Optional; ``None`` = no cleanup callback.
        self._on_task_complete = on_task_complete
        # Fires once per on_user_message call, right after this turn's FINAL
        # intent lane is settled (chat/queue/interrupt — after the
        # commitment-leak guard has had its say, never the raw pre-guard LLM
        # value). Session-resume's bridge-side gate is the reason this
        # exists: bridge needs to know the instant a turn resolves to a real
        # task ("queue") so it can permanently stop searching + withdraw
        # whatever candidate card is showing, without waiting for
        # on_user_message's return value (which is just a reply string and
        # carries no lane information). "chat"/"interrupt" are deliberately
        # inert here — see the callback's bridge-side consumer for why.
        self._on_intent_classified = on_intent_classified
        self._session_dir = session_dir
        # Holds SessionContext.active_goal — the standing-goal check-in state.
        # Optional so tests/fixtures that don't need goal support can omit it;
        # every goal-related code path treats a ``None`` session_ctx as
        # "no standing goal ever active".
        self._session_ctx = session_ctx

        # Session-scoped conversation history (all user + assistant turns)
        self.conversation_history: List[Dict[str, str]] = []

        # LTM recall cache: {(query, tier_value): (expiry_monotonic, block)}.
        self._ltm_block_cache: Dict[tuple[str, str], tuple[float, str]] = {}

        # Tracks whether the last _call_and_parse_streaming actually pushed
        # response_to_user fragments to the UI. False when the call fell back
        # to non-streaming (no fragments emitted) — callers use this to decide
        # whether a non-empty reply still needs a batch emit.
        self._last_response_streamed: bool = False

        # Diagnostic detail for the most recent LLM-call failure inside
        # _call_and_parse_streaming (the caught exception, or the raw
        # non-JSON output that survived both the streaming attempt and the
        # non-streaming retry). Reset to None at the top of every
        # _call_and_parse_streaming call — surfaced verbatim in the
        # "no usable reply" fallback so the user sees the real cause instead
        # of a placeholder sentence.
        self._last_llm_error: Optional[str] = None

        self._task_channel.on_item_done(self._on_item_done_sync)
        # Mechanical hard-stall concerns are stored on the channel and read
        # passively by INTENT (via render_state_for_coordinator) — no
        # proactive push, no re-plan trigger. Deliberately no callback is
        # registered via on_progress_concern: there is nothing for the
        # Coordinator to DO when a concern arrives, only something for it to
        # SHOW when asked later.

    # ── Session-resume restore (session_digest.py) ────────────────────────────

    def restore_conversation(self, history: List[Dict[str, str]]) -> None:
        """Reinstate a digest's verbatim conversation on a resumed session.

        Plain replace — history is verbatim user/assistant text (no live
        state), and _format_conversation_history (below) is a pure reader
        that works the same whether conversation_history was built up turn
        by turn or injected all at once here.
        """
        self.conversation_history = list(history)

    # ── User message (single entry point) ────────────────────────────────────

    async def on_user_message(
        self,
        message: str,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Handle any user message. Returns the final reply string.

        Streaming hook:
          - on_response_chunk receives INTENT reply fragments as they arrive.
            The intent classification itself is NOT exposed via a separate
            callback — it's routing internal to Orchestrator, not user-facing.
        """
        # Normalize @-mentions (quote/UNC handling); skill @-mentions are left
        # inline. Under progressive disclosure a @skill is no longer
        # force-activated — the normalized text rides along to the agent, which
        # sees the [Available Skills] menu and decides whether to read_skill it.
        message = preprocess_mentions(message)
        self.conversation_history.append({"role": "user", "content": message})
        self._submit_user_turn_to_ltm_triage(message)

        chunk_cb = on_response_chunk or self._on_response_chunk

        try:
            return await self._handle_user_message(message, chunk_cb)
        except NetworkUnavailableError:
            self._rewind_user_turn()
            raise
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] on_user_message error: {e} — fallback",
                component="Orchestrator",
            )
            fallback = f"⚠ Failed to process that message: {type(e).__name__}: {e}"
            self.conversation_history.append({"role": "assistant", "content": fallback})
            if self._on_reply_to_user:
                self._on_reply_to_user(fallback)
            return fallback

    # ── INTENT ─────────────────────────────────────────────────────────────────

    async def _handle_user_message(
        self,
        message: str,
        on_chunk: Optional[Callable[[str], Any]],
    ) -> str:
        """Run INTENT classification and mechanically act on the result."""
        # Kick off the PRECISE-tier (rerank=True) recall CONCURRENTLY with the
        # rest of this turn — never awaited on the user-facing critical path.
        # INTENT itself only ever sees the FAST-tier block (via
        # _gather_context_sections → _build_long_term_block, rerank=False).
        # This task is only awaited below if intent resolves to "queue" —
        # by then it has had the full INTENT round-trip to finish, so the
        # common case pays no additional latency at all.
        precise_ltm_task = asyncio.create_task(
            self._build_precise_long_term_block(message),
            name="precise-ltm-recall",
        )
        try:
            sections = await self._gather_context_sections()
            intent_context = self._format_for_intent(sections)

            # Skill awareness (single-turn, cannot read_skill mid-turn): the
            # enabled menu (reference only) + standing bodies (drive
            # response_to_user style). Rendered live so panel toggles take effect
            # immediately. Each non-empty block is appended after the base prompt.
            menu_block = self._build_skills_menu_block()
            standing_block = self._build_standing_block()
            intent_system = INTENT_SYSTEM_PROMPT
            if standing_block:
                intent_system += "\n\n" + standing_block
            if menu_block:
                intent_system += "\n\n" + menu_block

            intent_messages = [
                {"role": "system", "content": intent_system},
                {"role": "user", "content": INTENT_TEMPLATE.format(
                    full_context_block=intent_context,
                    message=message,
                )},
            ]

            parsed = await self._call_and_parse_streaming(
                intent_messages, "intent", on_chunk,
            ) or {}
        except BaseException:
            precise_ltm_task.cancel()
            raise

        intent = (parsed.get("intent") or "chat").strip().lower()
        reply = parsed.get("response_to_user", "")
        deferred = self._normalize_deferred_actions(parsed)

        # The Coordinator emits exactly three lanes: chat / queue / interrupt.
        # Anything else (a stray/garbled value) is treated as chat below unless
        # the commitment-leak guard promotes it — no legacy-value mapping,
        # because INTENT is a fresh LLM call every turn against the current
        # prompt; there is no persisted old value to stay compatible with.

        # Commitment-leak guard: execution work declared but mislabeled chat
        # → force queue. deferred_actions means "operations the agent must
        # perform in the world" (set from the request, not the reply's tone),
        # so a non-empty list while intent != a task lane is a genuine routing
        # miss — the agent must run. Preferences / acknowledgements carry an
        # empty list and correctly stay chat.
        if deferred and intent not in ("queue", "interrupt"):
            self.logger.warning(
                f"[Orchestrator] non-task intent {intent!r} with "
                f"deferred_actions={deferred} — forcing intent=queue",
                component="Orchestrator",
            )
            intent = "queue"

        # intent is now FINAL for this turn (guard above can no longer touch
        # it) — this is the one point callers outside the lane-routing logic
        # below should learn what actually happened. See __init__'s
        # _on_intent_classified docstring for why this exists and why it
        # fires here rather than at the raw parsed.get("intent") value.
        if self._on_intent_classified:
            try:
                self._on_intent_classified(intent)
            except Exception as e:
                self.logger.warning(
                    f"[Orchestrator] on_intent_classified callback raised: {e!r}",
                    component="Orchestrator",
                )

        is_task = intent in ("queue", "interrupt")

        # Standing-goal declaration/cancellation is orthogonal to the intent
        # lane — a "set" or "clear" can ride along with chat, queue, or
        # interrupt. Applied unconditionally (not gated on is_task) so a pure
        # cancellation ("never mind that goal, drop it", classified chat
        # since it implies no new world-work) still clears state.
        self._apply_goal_action(parsed)

        # Mirror the verbatim message into the task channel so PersistentAgent
        # can render a `[User Directive]` grounding block in its prompt — only
        # for task lanes. INTENT is the sole task-relevance filter; writing
        # this unconditionally (before intent was known) let a pure "chat"
        # message (no task implication at all) overwrite the slot and get
        # echoed to the running agent as a directive on its next turn.
        if is_task:
            self._task_channel.append_user_message(message)

        self.conversation_history.append({"role": "assistant", "content": reply or "..."})

        self.logger.info(
            f"[Orchestrator] Intent: intent={intent} deferred={deferred}",
            component="Orchestrator",
        )

        if not is_task:
            precise_ltm_task.cancel()
            # Emit the reply if it wasn't already streamed. When the INTENT
            # stream fell back to non-streaming (or no chunk hook was set),
            # `_last_response_streamed` is False and the reply still needs to
            # go out via the batch sink. When `reply` is empty (e.g. both the
            # streaming parse AND the one non-streaming retry in
            # `_call_and_parse_streaming` failed to produce JSON), fall back
            # to a diagnostic sentence instead of a silent placeholder — some
            # callers (e.g. the stdio bridge's `user_input` path) discard this
            # function's return value entirely and rely solely on the
            # `_on_reply_to_user` callback, so an empty `reply` must not
            # silently skip the emit.
            if not reply and parsed:
                # JSON parsed fine (parsed is non-empty) but response_to_user
                # itself was empty/missing — distinct from a total parse
                # failure (parsed == {}), where `_last_llm_error` below
                # carries the actual failure detail instead. Only the
                # "non-JSON — one non-streaming retry" WARNING hints at this
                # from the logs otherwise; this makes the swallow itself
                # visible.
                self.logger.warning(
                    f"[Orchestrator] INTENT parsed successfully but "
                    f"response_to_user was empty (intent={intent!r}) — "
                    f"falling back to a diagnostic sentence; model's real "
                    f"answer was lost",
                    component="Orchestrator",
                )
            if reply:
                final_reply = reply
            elif parsed:
                # Parsed fine, response_to_user just wasn't populated — show
                # what the model DID return rather than a generic sentence.
                final_reply = (
                    f"⚠ LLM response was missing 'response_to_user' — "
                    f"parsed fields: {parsed!r}"
                )
            else:
                # Total failure: `_last_llm_error` carries the real cause
                # (caught exception, or the non-JSON text that survived both
                # attempts). Only falls back to the generic sentence if that
                # slot is somehow empty too (defensive; should not happen).
                final_reply = (
                    self._last_llm_error
                    or "⚠ LLM service returned no usable response."
                )
            if self._on_reply_to_user and not self._last_response_streamed:
                self._on_reply_to_user(final_reply)
            return final_reply

        # Pure halt: an interrupt with NO deferred actions is "stop what you're
        # doing", not "do this instead". Enqueuing it as a TaskSpec (the old
        # `else message` path below) created an item with no world-work, which
        # the completion_guard then rejected on every turn — the 2026-07-23
        # "stop now" spin (~79 min / 186 rejections). Here we abort without
        # ever creating that item: clear the pending tail and interrupt the
        # in-flight item if one exists; if the agent is already idle, this is a
        # no-op and we just acknowledge. deferred-non-empty interrupts fall
        # through to the redirect path below (abandon current + queue the new
        # work), which is the correct "do this instead" behaviour.
        if intent == "interrupt" and not deferred:
            precise_ltm_task.cancel()
            current = self._task_channel.get_current_item()
            if current is not None:
                await self._task_channel.replace_post_current([])
                try:
                    await self._task_channel.interrupt_agent(
                        reason="User halted current task"
                    )
                except Exception as e:
                    self.logger.warning(
                        f"[Orchestrator] interrupt_agent (halt) failed: {e}",
                        component="Orchestrator",
                    )
            else:
                self.logger.info(
                    "[Orchestrator] Pure-halt interrupt while idle — no in-flight "
                    "item to stop; acknowledging without enqueuing.",
                    component="Orchestrator",
                )
            return reply or ""

        # queue / interrupt: mechanically enqueue the request verbatim. The
        # agent owns decomposition, tool selection, and target discovery —
        # the Coordinator's only job is to get the work into the channel and,
        # for interrupt, abort the in-flight item after the new tail lands.
        instruction = message
        force_interrupt = (
            intent == "interrupt"
            and self._task_channel.get_current_item() is not None
        )
        # Only a plain queue gets the precomputed PRECISE-tier block — an
        # interrupt is the user abandoning/redirecting the current plan, a
        # more time-sensitive path where waiting on rerank (even a mostly-
        # finished one) is not worth it; the Agent's item-start LTM context
        # for an interrupt-driven task is left at "none" rather than adding
        # a wait here.
        if force_interrupt:
            precise_ltm_task.cancel()
            ltm_block = None
        else:
            try:
                ltm_block = await precise_ltm_task
            except Exception as exc:
                self.logger.debug(
                    f"[Orchestrator] precise LTM recall task raised: {exc}",
                    component="Orchestrator",
                )
                ltm_block = None
        await self._enqueue_task(
            instruction,
            interrupt=force_interrupt,
            interrupt_reason="User redirected mid-task" if force_interrupt else "",
            ltm_block=ltm_block or None,
        )
        return reply or ""

    async def _enqueue_task(
        self, instruction: str, *, interrupt: bool, interrupt_reason: str = "",
        ltm_block: Optional[str] = None,
    ) -> None:
        """Mechanically queue *instruction* as a new TaskSpec.

        Non-interrupt (queue): APPENDS after whatever is already pending, so
        two quick non-interrupt messages both survive (e.g. "check X" then
        "also check Y" while the agent is still on the current item) — a
        straight replace would silently drop the still-unstarted first one.
        Interrupt: replaces the pending tail outright — the user is
        explicitly asking to abandon whatever was queued, not just the
        in-flight item, in favor of this new instruction.

        Order matters: write the new post-current tail FIRST, then send the
        interrupt. If we interrupted first, a fast agent could process the
        interrupt, advance current_index past the in-flight item, and pick up
        whatever was in the OLD pending tail before replace_post_current ran
        — replace can no longer touch that item because it has already
        become the new "current". By writing the tail first, the agent's
        next pickup after the interrupt is guaranteed to be the new head.
        """
        instruction = instruction.strip()
        if not instruction:
            return
        item = TaskSpec(item_id=str(_uuid.uuid4()), instruction=instruction, ltm_block=ltm_block)
        new_tail = [item] if interrupt else self._task_channel.get_pending_items() + [item]
        await self._task_channel.replace_post_current(new_tail)
        if interrupt:
            try:
                await self._task_channel.interrupt_agent(reason=interrupt_reason)
            except Exception as e:
                self.logger.warning(
                    f"[Orchestrator] interrupt_agent failed: {e}",
                    component="Orchestrator",
                )

    async def _call_and_parse_streaming(
        self,
        messages: List[Dict[str, str]],
        log_context: str,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
        extra_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """Call LLM with streaming and stream `response_to_user` chunks.

        `response_to_user` is streamed chunk-by-chunk via `on_response_chunk`
        as the JSON arrives. The full response is parsed at the end and
        returned as a dict. Other fields (intent, deferred_actions, etc.)
        are ONLY available after the stream completes — the stream is not
        used to dispatch any logic early, since the decisions that consume
        those fields all run after the stream is done.

        Falls back to non-streaming on error.
        """
        extra_kwargs = {"effort": "high", **(extra_kwargs or {})}
        streamer_response = JsonKeyStreamer("response_to_user")
        accumulated: List[str] = []
        streamed_any = False
        # Reset the cross-call flag up front: if we fall back to non-streaming
        # below, no fragments are pushed and this stays False so callers know
        # the reply still needs a batch emit.
        self._last_response_streamed = False
        # Reset the failure-diagnostic slot too — only set below if THIS call
        # actually fails; a clean run must not leak a stale message from a
        # previous turn.
        self._last_llm_error = None

        try:
            async for event in call_with_fallback_stream(
                self._services,
                dict(messages=messages, **extra_kwargs),
                on_fallback=lambda idx, e: self.logger.warning(
                    f"Orchestrator {log_context} stream fallback to index {idx}: {e}",
                    component="Orchestrator",
                ),
                wait_on_network_down=True,
            ):
                if isinstance(event, StreamTextDeltaEvent):
                    accumulated.append(event.text)

                    # Stream response_to_user chunks (the only field that
                    # surfaces to the UI in real-time).
                    if not streamer_response.done:
                        for fragment in streamer_response.feed(event.text):
                            if on_response_chunk and fragment:
                                streamed_any = True
                                try:
                                    on_response_chunk(fragment)
                                except Exception:
                                    pass

                elif isinstance(event, StreamDoneEvent):
                    break

            full_text = "".join(accumulated)
            # Record whether we actually streamed the reply: drives the INTENT
            # path's batch-emit decision (only emit the reply if it wasn't
            # already streamed). A successful stream that produced fragments →
            # True; a silent (empty response_to_user) run → False.
            self._last_response_streamed = streamed_any
            parsed = try_parse_json(full_text)
            if isinstance(parsed, dict):
                return parsed
            # Parse failure (model emitted prose under json_mode, or truncated
            # JSON). A silent None here propagates to `or {}` at the call site.
            # Capture the actual offending text before retrying, so a total
            # failure can show the user what the model really said instead of
            # a generic placeholder.
            snippet = full_text.strip()
            self._last_llm_error = (
                f"model returned non-JSON output ({len(snippet)} chars): "
                f"{snippet[:300]!r}"
            ) if snippet else "model returned an empty response"
            self.logger.warning(
                f"Orchestrator {log_context} stream produced non-JSON "
                f"({len(full_text)} chars) — one non-streaming retry",
                component="Orchestrator",
            )
            return await self._call_and_parse(messages, log_context, extra_kwargs=extra_kwargs)

        except NetworkUnavailableError:
            raise
        except Exception as e:
            self._last_llm_error = f"{type(e).__name__}: {e}"
            self.logger.warning(
                f"Orchestrator {log_context} streaming failed: {e} — non-streaming fallback",
                component="Orchestrator",
            )
            return await self._call_and_parse(messages, log_context, extra_kwargs=extra_kwargs)
        finally:
            # Seal the streamed reply bubble so the UI finalizes it. Only when
            # we actually pushed fragments — an empty response_to_user must not
            # leave a dangling empty bubble. In `finally` so a mid-stream error
            # still closes whatever showed.
            if streamed_any and self._on_response_done:
                try:
                    self._on_response_done()
                except Exception:
                    pass

    async def _call_and_parse(
        self,
        messages: List[Dict[str, str]],
        log_context: str,
        extra_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """Non-streaming LLM call with JSON parse."""
        extra_kwargs = {"effort": "high", **(extra_kwargs or {})}
        raw = cast(LLMChatResult, await call_with_fallback(
            self._services,
            dict(messages=messages, **extra_kwargs),
            on_fallback=lambda idx, e: self.logger.warning(
                f"Orchestrator {log_context} fallback to index {idx}: {e}",
                component="Orchestrator",
            ),
            wait_on_network_down=True,
        ))
        parsed = try_parse_json(raw.content or "")
        return parsed if isinstance(parsed, dict) else None

    # ── Context building ─────────────────────────────────────────────────────

    async def _gather_context_sections(self) -> Dict[str, str]:
        """Compute every context section INTENT needs.

        Returns a dict with keys:
          ltm           — formatted LTM recall block (may be "")
          conversation  — formatted prior-conversation block, header included
                          (may be "")
          plan          — `[Current Plan]` block, always present;
                          contains `_EMPTY_PLAN_MARKER` when idle.
        """
        query = (
            self.conversation_history[-1]["content"]
            if self.conversation_history else ""
        )
        ltm_block = await self._build_long_term_block(query)
        conv_raw = self._format_conversation_history()
        conversation_block = (
            f"[Recent Conversation History]\n{conv_raw}\n" if conv_raw else ""
        )
        plan_body = (
            self._task_channel.render_state_for_coordinator()
            if self._task_channel.total_items > 0
            else _EMPTY_PLAN_MARKER
        )
        plan_block = f"[Current Plan]\n{plan_body}\n"

        return {
            "ltm": ltm_block,
            "conversation": conversation_block,
            "plan": plan_block,
        }

    def _format_for_intent(self, sections: Dict[str, str]) -> str:
        """Cache-friendly ordering: append-only sections first, volatile last.

        Conversation history grows by appending — its byte prefix stays
        stable across consecutive user messages within the cache TTL window.
        LTM is query-dependent so it can change between turns; placing it
        AFTER the append-only block keeps that block cacheable as a
        contiguous prefix. Task plan is the most volatile section (mutates
        within a turn on every mark_done) and goes last.
        """
        parts = [
            sections["conversation"],
            sections["ltm"],
            sections["plan"],
        ]
        return "\n".join(p for p in parts if p) + "\n"

    def _format_conversation_history(self) -> str:
        """Format prior conversation turns (excludes the current message).

        The current user message is rendered separately by the prompt template
        — we slice it off here to avoid duplication. No length truncation:
        long-context models handle the full history fine, and trimming creates
        gaps that can mislead the LLM.
        """
        prior = self.conversation_history[:-1] if self.conversation_history else []
        if not prior:
            return ""
        lines = []
        for turn in prior:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        return "\n".join(lines)

    # ── Long-term memory ─────────────────────────────────────────────────────

    async def _build_long_term_block(self, query: str) -> str:
        """Recall LTM context for *query* at FAST tier. Falls back to "" on any error.

        FAST tier: ``rerank=False``. Sub-second latency,
        RRF+recency ordering only — appropriate for the chat-turn hot path
        where the user is actively waiting.
        """
        cache_key = (query, RecallTier.FAST.value)
        cached = self._ltm_block_cache.get(cache_key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        try:
            from ..infrastructure.long_term_memory import LongTermMemory
            ltm = LongTermMemory.get()
        except Exception:
            return ""
        # LTM 2.0 frame context — HandQ bridge runs on the user's local Windows
        # machine. Without this, recall would happily surface
        # ``/local/mnt/wine/...`` (Linux-SSH-observed) insights to INTENT and
        # trigger the wine-bug class of UNC→SSH mistranslation.
        current_frame = {"os": "windows", "host": "local", "confidence": 0.95}
        # FAST tier bypasses rerank, and RECALL_MIN_SCORE_FAST is the
        # authoritative relevance cutoff — without rerank, top-K by RRF would
        # surface activity-snapshot noise (cosine band 0.34-0.41) as "least
        # bad" matches on chat turns even when the query has no real hit.
        from ..infrastructure.long_term_memory._constants import (
            RECALL_ITEM_TIMEOUT_SECONDS,
            RECALL_MIN_SCORE_FAST,
        )
        try:
            self._notify_recall_started()
            block = await asyncio.wait_for(
                ltm.format_context_block(
                    query=query,
                    rerank=False,
                    min_score=RECALL_MIN_SCORE_FAST,
                    current_frame=current_frame,
                ),
                timeout=RECALL_ITEM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.logger.debug(
                f"LTM format_context_block timed out after "
                f"{RECALL_ITEM_TIMEOUT_SECONDS}s", component="Orchestrator"
            )
            return ""
        except Exception:
            self.logger.debug(
                "LTM format_context_block failed", component="Orchestrator"
            )
            return ""
        self._ltm_block_cache[cache_key] = (
            time.monotonic() + _LTM_BLOCK_CACHE_TTL_SEC, block,
        )
        return block

    async def _build_precise_long_term_block(self, query: str) -> str:
        """Recall LTM context for *query* at PRECISE tier (``rerank=True``).

        Started concurrently with the FAST-tier INTENT call (see
        ``_handle_user_message``) — never on the user-facing critical path.
        Only consumed when INTENT resolves to ``queue``: its result becomes
        the queued ``TaskSpec.ltm_block``, so the Agent executing that task
        starts with a rerank-quality LTM block instead of running its own
        recall. Falls back to "" on any error, same as the FAST tier.
        """
        cache_key = (query, RecallTier.PRECISE.value)
        cached = self._ltm_block_cache.get(cache_key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        try:
            from ..infrastructure.long_term_memory import LongTermMemory
            ltm = LongTermMemory.get()
        except Exception:
            return ""
        current_frame = {"os": "windows", "host": "local", "confidence": 0.95}
        from ..infrastructure.long_term_memory._constants import (
            RECALL_ITEM_TIMEOUT_SECONDS,
            RECALL_MIN_SCORE,
        )
        try:
            block = await asyncio.wait_for(
                ltm.format_context_block(
                    query=query,
                    rerank=True,
                    min_score=RECALL_MIN_SCORE,
                    current_frame=current_frame,
                ),
                timeout=RECALL_ITEM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.logger.debug(
                f"LTM precise recall timed out after "
                f"{RECALL_ITEM_TIMEOUT_SECONDS}s", component="Orchestrator"
            )
            return ""
        except Exception:
            self.logger.debug(
                "LTM precise recall failed", component="Orchestrator"
            )
            return ""
        self._ltm_block_cache[cache_key] = (
            time.monotonic() + _LTM_BLOCK_CACHE_TTL_SEC, block,
        )
        return block

    def _submit_user_turn_to_ltm_triage(self, message: str) -> None:
        """Fire-and-forget: submit user message to LTM triage queue.

        LTM (long-term memory) decides asynchronously whether the user turn
        is worth remembering across sessions. We don't await — by the time
        triage finishes (seconds to minutes), the user message has already
        been processed by INTENT.
        """
        try:
            from ..infrastructure.long_term_memory import LongTermMemory
            from ..infrastructure.long_term_memory.candidates import submit_user_turn
            ltm = LongTermMemory.get()
        except Exception:
            return
        try:
            asyncio.create_task(
                submit_user_turn(
                    ltm=ltm,
                    msg_id=str(_uuid.uuid4()),
                    user_message=message,
                    current_goal=message,
                ),
                name="ltm-submit-user-turn",
            )
        except Exception:
            pass

    def _submit_session_to_ltm(self, *, success: bool) -> None:
        """Fire-and-forget: submit the just-completed session trajectory to
        LTM triage as a SESSION_COMPLETE (success) or SESSION_FAILED candidate.

        Called only from `_handle_task_complete_candidate` — i.e. exactly when
        a completion reply is emitted. Like the user-turn submitter, we don't
        await: triage runs for seconds to minutes and must not block the
        Coordinator.
        """
        if not self._session_dir:
            return
        try:
            from ..infrastructure.long_term_memory import LongTermMemory
            from ..infrastructure.long_term_memory.candidates import (
                submit_session_complete,
            )
            ltm = LongTermMemory.get()
        except Exception:
            return
        goal = self._last_user_message()
        summary = self._compose_completion_reply()
        last_steps = self._task_channel.get_completed_results()
        try:
            asyncio.create_task(
                submit_session_complete(
                    ltm=ltm,
                    session_dir=self._session_dir,
                    goal=goal,
                    summary=summary,
                    last_steps=last_steps,
                    success=success,
                ),
                name="ltm-submit-session",
            )
        except Exception:
            pass

    # ── Skill awareness (formerly ReceptionistMixin) ─────────────────────────

    def _build_skills_menu_block(self) -> str:
        """Render the [Available Skills] menu for INTENT.

        The menu (name + description of every enabled non-standing skill) is
        rendered live from the SkillRegistry. Standing skills are excluded
        (their bodies are already injected as transparent prompt text).
        Wrapped with an instruction that this is for reference only — pulling
        a skill body and executing it is the agent's job, not the
        Coordinator's.

        Returns "" when no enabled skills exist or on any error.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            reg = SkillRegistry.get()
            menu = reg.render_menu_block(exclude=reg.standing_names())
        except Exception:
            return ""
        if not menu:
            return ""
        return (
            menu
            + "\n\nThese skills are available for reference only — they inform "
            "how you talk about what the system can do. Actually reading a "
            "skill's instructions and executing it is the agent's job, not "
            "yours; do not attempt to apply them here."
        )

    def _build_standing_block(self) -> str:
        """Render standing skill bodies as transparent style instructions.

        Standing skills define the Coordinator's communication style and must
        be applied to ``response_to_user``. They do NOT change task/chat
        classification — style only. The body is plain prompt text with no
        skill attribution.

        Returns "" when no enabled+standing skills exist or on any error.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            standing = SkillRegistry.get().render_standing_block()
        except Exception:
            return ""
        if not standing:
            return ""
        return (
            standing
            + "\n\nThe instructions above define your communication style. "
            "Apply them to `response_to_user`. They do NOT change your task/chat "
            "classification — style only."
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _on_item_done_sync(self, result: TaskResult) -> None:
        """Callback from TaskChannel — mechanically check for task completion.

        Fires synchronously when the agent calls mark_current_done. If nothing
        is left in flight or pending, the task is done — finalize it. No
        LLM call, no background loop: the channel already has everything
        needed to decide.
        """
        if (
            self._task_channel.completed_count > 0
            and not self._task_channel.has_pending
            and self._task_channel.get_current_item() is None
        ):
            asyncio.create_task(
                self._handle_task_complete_candidate(),
                name="task-complete-candidate",
            )

    def _rewind_user_turn(self) -> None:
        """Pop last user turn from history on network failure."""
        if (
            self.conversation_history
            and self.conversation_history[-1].get("role") == "user"
        ):
            self.conversation_history.pop()

    def _normalize_deferred_actions(self, parsed: Dict) -> List[str]:
        """Coerce parsed['deferred_actions'] into a clean list of strings."""
        raw = parsed.get("deferred_actions") or []
        if not isinstance(raw, list):
            raw = [raw]
        return [str(a).strip() for a in raw if str(a).strip()]

    def _apply_goal_action(self, parsed: Dict) -> None:
        """Set/clear SessionContext.active_goal from INTENT's goal_action field.

        No-op when there is no session_ctx to write to (e.g. a bare
        Orchestrator constructed without one in tests that don't exercise
        goal support).
        """
        if self._session_ctx is None:
            return
        action = (parsed.get("goal_action") or "none").strip().lower()
        if action == "set":
            condition = (parsed.get("goal_condition") or "").strip()
            if not condition:
                return
            self._session_ctx.active_goal = GoalState(
                condition=condition,
                baseline_result_count=self._task_channel.completed_count,
            )
            self.logger.info(
                f"[Orchestrator] Standing goal set: {condition!r}",
                component="Orchestrator",
            )
        elif action == "clear":
            if self._session_ctx.active_goal is not None:
                self.logger.info(
                    f"[Orchestrator] Standing goal cleared: "
                    f"{self._session_ctx.active_goal.condition!r}",
                    component="Orchestrator",
                )
            self._session_ctx.active_goal = None

    def _last_user_message(self) -> str:
        """Return the most recent user turn from conversation_history.

        Used as the semantic anchor for downstream consumers that need a
        "current goal" string (LTM triage). Captures the latest task framing
        so it stays accurate across multi-turn refinement.
        """
        for turn in reversed(self.conversation_history):
            if turn.get("role") == "user":
                return turn.get("content", "")
        return ""

    async def _handle_task_complete_candidate(self) -> None:
        """Finalize the task mechanically when the task channel has nothing in flight.

        Called from `_on_item_done_sync` when nothing is pending and nothing
        is in progress. Completion is purely mechanical: the task channel is
        empty ⇒ the task is done. The completion reply is assembled
        deterministically from the agent's verified structured facts
        (skeleton-first, see `_compose_completion_reply`); no LLM re-judges it.

        A failed tail is surfaced to the user via the completion reply's issue
        list, not hidden behind a second LLM's verdict.

        When a standing goal is active, this is also the ONLY point where its
        condition is re-checked — a single judge call against the accumulated
        evidence since the goal was declared, not a re-judge of every item.
        If the condition doesn't yet hold, the goal is mechanically re-queued
        instead of finalizing — see `_check_standing_goal`.
        """
        completed = self._task_channel.get_completed_results()
        if not completed:
            return  # defensive: gate triggered with no completed results

        last = completed[-1]
        success = last.success and not last.issues
        self._submit_session_to_ltm(success=success)

        goal = self._session_ctx.active_goal if self._session_ctx else None
        if goal is not None:
            await self._check_standing_goal(goal, completed)
            return

        # Self-paced loop fold: if a schedule_wakeup timer is pending, the agent
        # is mid-loop and will be re-woken shortly — suppress the verbose
        # completion reply (the loop tick shows in the task panel instead), the
        # same way Claude Code folds consecutive noop wakeups into one entry.
        # We still run LTM submission (above) and cleanup so state stays sane.
        if self._session_ctx is not None and getattr(
            self._session_ctx, "_wakeup_tasks", None,
        ):
            await self._run_task_complete_cleanup()
            return

        self._emit_completion_reply()
        await self._run_task_complete_cleanup()

    async def _check_standing_goal(
        self, goal: "GoalState", completed: List[TaskResult],
    ) -> None:
        """Judge the standing goal against evidence accumulated since it was
        declared, then either re-queue it or finalize the session.

        This is the sole point of difference from the ordinary completion
        path: the judge call answers "does the CONDITION hold now" — a
        question about accumulated world-state across every item since the
        goal was set — not "did the last item succeed", which the Agent's own
        per-item loop already answers on its own. See GoalState's docstring.
        """
        # Narrow for the type checker: this method is only ever reached via
        # finalize_completion's `goal = session_ctx.active_goal if session_ctx
        # else None` short-circuit, so a non-None goal implies a non-None ctx.
        # Pyright cannot see across that call boundary, and every
        # `self._session_ctx.active_goal = None` in this method reads as
        # "attribute access on Optional[SessionContext]" without the assert.
        assert self._session_ctx is not None
        evidence = completed[goal.baseline_result_count:]
        verdict = await self._judge_goal_satisfaction(goal, evidence)

        if verdict.satisfied:
            self._session_ctx.active_goal = None
            self._emit_completion_reply(prefix=f"✓ Goal achieved: {verdict.rationale}")
            await self._run_task_complete_cleanup()
            return

        # Blocking verdict: the evidence contradicts the objective, or the
        # condition can't be checked from what's available. Retrying won't fix
        # either — hand back to the user instead of burning attempts up to the
        # cap. This is the adversarial verifier's "needs a human, not another
        # loop" signal (borrowed from Grok's goal_classifier `blocking` field).
        if verdict.blocking in ("contradiction", "unverifiable"):
            self._session_ctx.active_goal = None
            label = (
                "Goal contradicts the current state"
                if verdict.blocking == "contradiction"
                else "Goal cannot be verified from the available evidence"
            )
            self._emit_completion_reply(
                prefix=(
                    f"⚠ {label} — retrying won't resolve it, so I'm stopping: "
                    f"{verdict.rationale}"
                )
            )
            await self._run_task_complete_cleanup()
            return

        if goal.iterations < _GOAL_MAX_ITERATIONS:
            goal.iterations += 1
            await self._requeue_goal(goal)
            return  # still running — no completion reply, no cleanup

        # Safety valve: stop re-queuing silently, hand back to the user.
        self._session_ctx.active_goal = None
        self._emit_completion_reply(
            prefix=(
                f"⚠ Goal \"{goal.condition}\" is still unmet after "
                f"{goal.iterations} consecutive attempts — stopping here; "
                f"please confirm whether to keep going."
            )
        )
        await self._run_task_complete_cleanup()

    async def _judge_goal_satisfaction(
        self, goal: "GoalState", evidence: List[TaskResult],
    ) -> "_GoalVerdict":
        """Adversarially verify whether *goal*'s standing condition holds.

        Two upgrades over the old single-prose-judge:

        1. **Ground-truth evidence.** Alongside the agent's self-reported
           outcomes, we feed the REAL file changes since the goal was declared
           (RewindStore.capture_diff → CHANGED_FILES + truncated DIFF). The
           judge audits what the files ACTUALLY became against the objective,
           so it catches "reported writing config.yaml but nothing changed" —
           the class of false-completion the mechanical channel-empty check and
           the speculative-completion guard both miss (they verify a
           world-touching tool was CALLED, not that its output met the goal).

        2. **Optional skeptic panel.** ``goal_verifier.voters`` (config,
           default 1) runs N independent judges; approval needs a strict
           majority AND every abstention/parse-failure degrades to refuted
           (bias-to-fail, Grok's cold-panel aggregation). Default 1 keeps the
           haiku hot path at exactly one call — same cost as before, just a
           sharper prompt + real diff. Opus deployments can raise it.

        Still fed EVERY TaskResult since the goal was declared: the condition
        can be false after an item "succeeds" and true after one fails. Reuses
        `_call_and_parse` — same model pool as INTENT/the Agent, no new plumbing.
        """
        reported_block = "\n\n".join(
            f"--- reported outcome #{i + 1} (item={r.item_id}) ---\n"
            f"success={r.success}\n"
            f"final_answer={r.final_answer}\n"
            f"verification={r.verification}\n"
            f"key_findings={r.key_findings}\n"
            f"issues={r.issues}"
            for i, r in enumerate(evidence)
        ) or "(no reported outcomes)"

        # Ground-truth file changes since the goal's baseline. Best-effort:
        # no store (tests) or empty capture → an explicit "no file changes"
        # block so the judge distinguishes "nothing changed" from "unavailable".
        changes_block = self._render_goal_file_changes(goal, evidence)

        user_content = (
            f"OBJECTIVE:\n{goal.condition}\n\n"
            f"{changes_block}\n\n"
            f"REPORTED_OUTCOMES ({len(evidence)} task(s) since goal declared):\n"
            f"{reported_block}\n"
        )
        messages = [
            {"role": "system", "content": _GOAL_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        voters = self._goal_verifier_voters()
        if voters <= 1:
            return await self._single_goal_vote(messages)

        results = await asyncio.gather(
            *[self._single_goal_vote(messages) for _ in range(voters)],
            return_exceptions=True,
        )
        votes: List["_GoalVerdict"] = []
        for r in results:
            # An exception (transport/parse) degrades to a refuted vote —
            # bias-to-fail keeps a missing verdict from counting as approval.
            votes.append(r if isinstance(r, _GoalVerdict) else _GoalVerdict(
                satisfied=False, blocking="none", rationale="verifier error",
            ))
        return self._aggregate_goal_votes(votes)

    def _goal_verifier_voters(self) -> int:
        """Read goal_verifier.voters from config (default 1, clamped 1..5)."""
        try:
            cm = self._session_ctx.config_manager if self._session_ctx else None
            if cm is None:
                return 1
            raw = cm.get_section("goal_verifier").get("voters", 1)
            return max(1, min(int(raw), 5))
        except Exception:
            return 1

    async def _single_goal_vote(self, messages: List[Dict[str, str]]) -> "_GoalVerdict":
        parsed = await self._call_and_parse(messages, "goal-judge") or {}
        blocking = str(parsed.get("blocking") or "none").strip().lower()
        if blocking not in ("none", "contradiction", "unverifiable"):
            blocking = "none"
        return _GoalVerdict(
            satisfied=bool(parsed.get("satisfied")),
            blocking=blocking,
            rationale=str(parsed.get("rationale") or ""),
        )

    def _aggregate_goal_votes(self, votes: List["_GoalVerdict"]) -> "_GoalVerdict":
        """Strict-majority approval; any blocking verdict from any voter wins.

        - A single voter flagging `contradiction`/`unverifiable` surfaces that
          (it's a "needs a human" signal — err toward escalation, not looping).
        - satisfied requires a STRICT majority of voters (n//2 + 1). Ties and
          minorities stay refuted (bias-to-fail).
        - rationale = the first refuting/blocking rationale (the actionable one)
          when not satisfied, else the first approving one.
        """
        n = len(votes)
        for v in votes:
            if v.blocking in ("contradiction", "unverifiable"):
                return v
        approvals = sum(1 for v in votes if v.satisfied)
        if approvals >= (n // 2 + 1):
            first_yes = next((v for v in votes if v.satisfied), votes[0])
            return _GoalVerdict(satisfied=True, blocking="none",
                                rationale=first_yes.rationale)
        first_no = next((v for v in votes if not v.satisfied), votes[0])
        return _GoalVerdict(satisfied=False, blocking="none",
                            rationale=first_no.rationale)

    def _render_goal_file_changes(
        self, goal: "GoalState", evidence: List[TaskResult],
    ) -> str:
        """Render CHANGED_FILES + DIFF for the items since the goal's baseline,
        from the session RewindStore. Falls back to an explicit no-evidence
        block on any error / absent store."""
        try:
            store = self._session_ctx.rewind_store if self._session_ctx else None
            if store is None:
                return "CHANGED_FILES: (file-change capture unavailable this session)"
            item_ids = [r.item_id for r in evidence if r.item_id]
            diff_ev = store.capture_diff(item_ids or None)
            return diff_ev.render()
        except Exception:
            return "CHANGED_FILES: (file-change capture failed)"

    async def _requeue_goal(self, goal: "GoalState") -> None:
        """Mechanically re-queue the goal's verbatim condition as a new item.

        No LLM-authored "next step" — same philosophy as ordinary task
        queueing (the Coordinator never plans; the Agent decomposes). The
        Agent sees `[Task Boundary History]` (TaskChannel.
        get_recent_results_for_agent, read every turn) and the `(goal
        check-in #N)` prefix on this item, so it has full visibility into
        prior attempts without the Coordinator needing to summarize them.
        """
        item = TaskSpec(
            item_id=str(_uuid.uuid4()),
            instruction=goal.condition,
            ltm_block=None,
            goal_iteration=goal.iterations,
        )
        await self._task_channel.replace_post_current([item])

    async def _run_task_complete_cleanup(self) -> None:
        """Invoke the task-complete cleanup callback, swallowing any error.

        A misbehaving callback must never derail completion delivery; the
        completion reply has already been emitted at the call site.
        """
        if self._on_task_complete is None:
            return
        try:
            await self._on_task_complete()
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] task-complete cleanup callback raised "
                f"({type(e).__name__}: {e}); ignoring.",
                component="Orchestrator",
            )

    def _notify_state(self, state: str) -> None:
        """Push an activity-strip state through the optional UI hook.

        Defensive: a misbehaving UI callback must never derail the Coordinator.
        """
        if self._on_state_changed is None:
            return
        try:
            self._on_state_changed(state)
        except Exception:
            pass

    def _notify_recall_started(self) -> None:
        """Signal LTM recall is in flight so the activity strip shows
        ``recalling…``. Defensive: never let a UI callback derail recall."""
        if self._on_recall_started is None:
            return
        try:
            self._on_recall_started()
        except Exception:
            pass

    def _emit_completion_reply(self, prefix: str = "") -> None:
        """Compose + send the final task-completion reply (private helper).

        The reply is assembled deterministically from the agent's structured
        completion (final_answer / verification / artifacts / key_findings).
        The `final_answer` field carries the user-facing content directly;
        `verification` is the mechanical audit trail (rendered as a details /
        folded section); `artifacts` surfaces only when the user explicitly
        asked for files. There is no SECOND LLM call layer — the composer
        stitches only what the agent already produced.
        """
        # Reaching here means the task channel has nothing in flight. Settle the
        # activity strip to idle before the summary goes out; covers the no-body
        # early return below too, so the strip never stays stuck on a stale state.
        self._notify_state("idle")
        body = self._compose_completion_reply()
        if not body:
            return
        # Separate the finalisation prefix from the markdown body with a blank
        # line so the prefix renders as its own paragraph above the `## `
        # header rather than being glued onto it.
        prefix = prefix.strip()
        full = f"{prefix}\n\n{body}" if prefix else body
        if self._on_reply_to_user:
            try:
                self._on_reply_to_user(full)
            except Exception:
                pass

    def _compose_completion_reply(self) -> str:
        """Stitch a final task-completion reply from the agent's results.

        Called when the task channel has no pending and no in-progress item.
        Renders structured markdown (no LLM call).

        Layout, top → bottom:

        1. **`final_answer`** — the user-facing content the agent authored.
           This is the headline: whatever the user asked for, in whatever
           shape fits (paragraph, table, list, code block). If empty (agent
           didn't emit an answer body), we fall through so at least *something*
           renders.

        2. **Files** — only when `artifacts` is non-empty, i.e. the user
           explicitly asked for files. Verified paths (tool-output ground
           truth, not LLM self-report).

        3. **Verification / Key findings** — audit-trail bullets under an
           `### Audit trail` H3 subheading. These are the mechanical evidence
           and discrete facts; they're not the answer, they're the audit line,
           so they render as a section below the headline. (An earlier draft
           folded this into `<details>`, but the Electron renderer HTML-
           escapes every tag except fenced code, so the fold surfaced as
           literal `<details>`/`<summary>` text.)

        4. **Unresolved** — a failed tail's blockers, always surfaced as their
           own section.
        """
        completed = self._task_channel.get_completed_results()
        if not completed:
            return ""
        last = completed[-1]

        # Artifacts span the whole task — collect across every completed item,
        # deduping while preserving first-seen order. These are the grounded
        # facts (tool output paths).
        artifacts: List[str] = []
        seen: Set[str] = set()
        for r in completed:
            for a in r.artifacts:
                if a and a not in seen:
                    seen.add(a)
                    artifacts.append(a)

        sections: List[str] = []
        header = "## Task complete" if (last.success and not last.issues) else "## Task ended"
        sections.append(header)

        # 1. FINAL ANSWER first — the user's actual answer, headline position.
        if last.final_answer:
            sections.append(last.final_answer)

        # 2. Files ONLY when the user asked for them (artifacts non-empty).
        if artifacts:
            sections.append(
                "**Files created / modified** (verified from tool output)\n"
                + "\n".join(f"- {a}" for a in artifacts)
            )

        # 3. Audit trail under an H3 subheading. verification (mechanical, tool-
        #    grounded bullets) and key_findings (discrete facts) live here.
        #    Nested under the "## Task complete" / "## Task ended" header at
        #    the top so the visual hierarchy reads as headline → detail; the
        #    Electron renderer's Markdown parser (electron/renderer/renderer.js
        #    `renderMarkdown`) HTML-escapes every tag except fenced code, so a
        #    `<details>`/`<summary>` fold surfaces as literal text — this UI
        #    just doesn't do collapsible sections.
        audit_parts: List[str] = []
        if last.verification:
            audit_parts.append(
                "**Verification**\n"
                + "\n".join(f"- {v}" for v in last.verification)
            )
        if last.key_findings:
            audit_parts.append(
                "**Key findings**\n"
                + "\n".join(f"- {f}" for f in last.key_findings)
            )
        if audit_parts:
            sections.append("### Audit trail\n\n" + "\n\n".join(audit_parts))

        # 4. A failed tail's blockers are always surfaced, never folded.
        if not last.success and last.issues:
            sections.append(
                "**Unresolved**\n"
                + "\n".join(f"- {i}" for i in last.issues)
            )
        return "\n\n".join(sections)

