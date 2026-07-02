"""
Orchestrator — INTENT classification + unified PLAN_MODIFY (sync + async).

Channels:
  1. on_user_message() — every user message goes through Stage 1 (INTENT) and,
     when intent=task, the unified planner via _run_planner().
  2. run_planner_loop() — background loop that re-runs the planner after every
     item completion (mark_done event), keeping the post-current item list
     fresh based on the latest agent results.

Both channels share `_run_planner` and the `_planner_lock` so their mutations
are serialized.

Architectural decisions (V2):
  - IDLE = checklist is empty. Once the first item is appended the system is
    permanently in ACTIVE; "completion" is detected by Orchestrator when
    post_current_items is empty and no in-progress item remains, and is
    signalled by emitting a final reply derived from the agent's last
    factual_outcome (no LLM call required).
  - Stage 1 INTENT does NOT extract a goal — the original user message is
    forwarded verbatim to the planner.
  - Stage 1 INTENT receives the same context (LTM, history, checklist)
    as the planner so it can answer status questions accurately.
  - The planner emits a single op shape: replace_post_current(items). Plus
    optional interrupt_current to abort the in-flight item.
  - Skills and tools are session-level append-only state on SharedCheckList.
    Planner declares activations at the top level of its output; agent picks
    up the diff via on_skills_changed / on_tools_changed callbacks.
  - Conversation history is per-session, append-only, and re-rendered into the
    user prompt every turn.
"""
import asyncio
import json
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, cast

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
from .planner_mixin import PlannerMixin
from .receptionist import ReceptionistMixin
from .shared_checklist import (
    SharedCheckList,
    CheckListItem,
    ItemResult,
)
from .agent_utils import ProgressConcern, acceptance_info_delta
from .planner_prompts import (
    INTENT_SYSTEM_PROMPT,
    INTENT_TEMPLATE,
    PLAN_MODIFY_TEMPLATE,
    build_plan_modify_system_prompt,
)


_EMPTY_CHECKLIST_MARKER = "(empty — no active task)"

# Planner-tier LTM recall block cache TTL. Aligned with the LTM mutation
# floor (DREAM_INTERVAL_MIN_SEC / IDENTITY_CACHE_TTL_SEC = 60s): an accepted
# entry can't land in under one dream tick, so a ≤60s-stale recall block is
# within the subsystem's existing worst-case staleness contract.
_LTM_BLOCK_CACHE_TTL_SEC: float = 60.0


class Orchestrator(PlannerMixin, ReceptionistMixin):
    """Two-stage user-message handler + background item evaluator."""

    def __init__(
        self,
        llm_services: List[LLMService],
        checklist: SharedCheckList,
        on_reply_to_user: Optional[Callable[[str], Any]] = None,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
        on_response_done: Optional[Callable[[], Any]] = None,
        on_state_changed: Optional[Callable[[str], Any]] = None,
        on_recall_started: Optional[Callable[[], Any]] = None,
        on_task_complete: Optional[Callable[[], Awaitable[None]]] = None,
        session_dir: Optional[str] = None,
        helper_services: Optional[List[LLMService]] = None,
    ):
        if not llm_services:
            raise ValueError("Orchestrator requires at least one LLMService")
        self._services: List[LLMService] = list(llm_services)
        # Cheap-model pool for the acceptance-spinning auditor (Tier-1 sense at
        # the acceptance-loop task boundary). Empty-degrades exactly like
        # PersistentAgent's watcher pool: no helpers → auditor is skipped and
        # the mechanical delta==0 signal alone decides termination.
        self._helper_services: List[LLMService] = (
            list(helper_services) if helper_services else []
        )
        self._checklist = checklist
        self.logger = get_logger()
        self._on_reply_to_user = on_reply_to_user
        self._on_response_chunk = on_response_chunk
        self._on_response_done = on_response_done
        # Activity-strip state hook. The agent emits "thinking"/"executing"
        # directly via the InteractionManager; the planner phase and the
        # task-settled transition are only visible here, so the orchestrator
        # surfaces "planning" (planner LLM call in flight) and "idle"
        # (task complete, final reply sent) through this callback.
        self._on_state_changed = on_state_changed
        # LTM-recall-in-flight hook. INTENT/PLAN recall is the slowest tier
        # (rerank + dynamic_k, ~3s); this surfaces a transient "recalling…"
        # label on the activity strip while it runs.
        self._on_recall_started = on_recall_started
        # Task-boundary cleanup hook. Fires when the acceptance gate reaches
        # a terminal verdict (PASS / TRIVIAL / ACCEPT) — i.e. the whole task
        # is really finished, not just an item boundary. FlowControllerV2 wires
        # this to the browser-holder close so Chromium doesn't linger between
        # tasks in the same session. EXTEND / VALIDATE do NOT fire it (more
        # work is queued). Optional; ``None`` = no cleanup callback.
        self._on_task_complete = on_task_complete
        self._session_dir = session_dir

        # Session-scoped conversation history (all user + assistant turns)
        self.conversation_history: List[Dict[str, str]] = []

        # Planner-tier LTM recall cache: {(query, tier_value, bare): (expiry_monotonic, block)}.
        # Two slots per query — one for fast-tier (INTENT / receptionist), one for
        # quality-tier (Planner / stagnation). Same query can carry both; a chat
        # turn does not evict the quality slot the Planner will later want.
        # The ``bare`` dimension differentiates full blocks (with header +
        # identity + known-entities) from bare blocks (memory/knowledge only)
        # used by the dual-recall extra path — see _build_long_term_block.
        self._ltm_block_cache: Dict[tuple[str, str, bool], tuple[float, str]] = {}

        # Task-root recall query — the user message that STARTED the current
        # task (INTENT classified it as "task"). Planner uses this instead of
        # ``conversation_history[-1]`` so mid-task chats ("wait, first make me
        # coffee") do NOT re-anchor recall on the chat text — the planner_loop
        # keeps recalling against the original task goal for the whole task
        # lifecycle. INTENT still uses conversation_history[-1] (each user
        # turn deserves its own fresh classification recall).
        # Set in _handle_user_message when intent=task; overwritten by every
        # subsequent task classification. Not cleared on task completion — the
        # next task's INTENT will overwrite, and while idle the field is
        # harmless (planner_loop only fires on mark_done which requires an
        # active checklist). None means "no active task; fall back to the last
        # user message" (identical to pre-fix behaviour, covers cold-start and
        # edge cases like planner tests).
        self._task_root_query: Optional[str] = None

        # Receptionist mixin owns no state — skills live in checklist.

        # Initialize planner mixin state (quality controls, compression)
        self._init_planner()

        # Single-flight lock: serializes Stage 2 (user-message path) and the
        # background planner_loop (mark_done path). First-come-first-served;
        # the second caller sees the first's mutations in its context.
        self._planner_lock = asyncio.Lock()

        # planner_loop trigger: agent flips this on mark_done so the loop
        # wakes up and runs another planner call against the new state.
        self._planner_trigger = asyncio.Event()
        self._checklist.on_item_done(self._on_item_done_sync)
        # Tier-1 progress watcher → planner. Symmetric to on_item_done: a
        # divergence verdict on the in-flight item wakes the planner loop the
        # same way an item completion does. The planner then reads the in-flight
        # digests + concern from the checklist context (no new pipeline needed)
        # and may replace_post_current / interrupt_agent. The watcher gets no
        # direct handle on the orchestrator — only this trigger.
        self._checklist.on_progress_concern(self._on_progress_concern_sync)

    # ── Channel 1: User Message (single entry point) ─────────────────────────

    async def on_user_message(
        self,
        message: str,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
    ) -> str:
        """Handle any user message. Returns the final reply string.

        Streaming hook:
          - on_response_chunk receives Stage 1 (and Stage 2) reply fragments
            as they arrive. The intent classification itself is NOT exposed
            via a separate callback — INTENT/TASK is routing internal to
            Orchestrator and not user-facing.
        """
        message, prescan = preprocess_mentions(message)
        self.conversation_history.append({"role": "user", "content": message})
        # Mirror the verbatim message into the checklist so PersistentAgent
        # can render an `[User Original Request]` grounding block in its
        # prompt — preserves user-side nuance (e.g. specific Chinese phrasing)
        # that the planner's item-instruction translation may flatten.
        self._checklist.set_latest_user_message(message)
        self._submit_user_turn_to_ltm_triage(message)

        # Apply prescan immediately so @-mentioned skills activate even on
        # chat-only paths where Stage 2 never runs.
        if prescan:
            self._activate_skills_in_checklist({"skills_needed": []}, prescan)

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
            fallback = "Got it — I'll incorporate your message."
            self.conversation_history.append({"role": "assistant", "content": fallback})
            if self._on_reply_to_user:
                self._on_reply_to_user(fallback)
            return fallback

    # ── Channel 2: Background Planner Loop ───────────────────────────────────

    async def run_planner_loop(self) -> None:
        """Background task: re-run planner after every item completion.

        Triggered by `_planner_trigger` (set in `_on_item_done_sync`). Acquires
        `_planner_lock` to serialize against the user-message path. Runs the
        same `_run_planner` as Stage 2; the only difference is the trigger
        source. Loops forever until cancelled.
        """
        while True:
            try:
                await self._planner_trigger.wait()
                self._planner_trigger.clear()
                async with self._planner_lock:
                    await self._run_planner(trigger="mark_done")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    f"[Orchestrator] planner_loop error: {e}",
                    component="Orchestrator",
                )

    # ── Stage 1 + Stage 2 dispatcher ─────────────────────────────────────────

    async def _handle_user_message(
        self,
        message: str,
        on_chunk: Optional[Callable[[str], Any]],
    ) -> str:
        """Run Stage 1 (INTENT). Forward to Stage 2 (PLAN_MODIFY) when intent=task."""
        sections = await self._gather_context_sections(RecallTier.FAST)
        intent_context = self._format_for_intent(sections)

        # Speculative QUALITY recall pre-warm: start the rerank-enabled recall
        # concurrently with the INTENT LLM streaming. If intent=task,
        # _run_planner's _gather_context_sections(QUALITY) will hit the
        # _ltm_block_cache — eliminating 3-8s of rerank latency from the
        # critical startup path. If intent=chat, the task is cancelled.
        quality_prefetch = asyncio.create_task(
            self._build_long_term_block(message, tier=RecallTier.QUALITY),
            name="quality-recall-prefetch",
        )

        intent_messages = [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": INTENT_TEMPLATE.format(
                full_context_block=intent_context,
                message=message,
            )},
        ]

        parsed = await self._call_and_parse_streaming(
            intent_messages, "intent", on_chunk,
        ) or {}

        intent = (parsed.get("intent") or "chat").strip().lower()
        reply = parsed.get("response_to_user", "")
        deferred = self._normalize_deferred_actions(parsed)

        # Commitment-leak guard: execution work declared but mislabeled chat
        # → force task. deferred_actions now means "operations the agent must
        # perform in the world" (set from the request, not from the reply's
        # tone), so a non-empty list while intent=chat is a genuine routing
        # miss — the planner must run. Preferences / acknowledgements carry an
        # empty list and correctly stay chat.
        if deferred and intent == "chat":
            self.logger.warning(
                f"[Orchestrator] chat intent with deferred_actions={deferred} "
                f"— forcing intent=task",
                component="Orchestrator",
            )
            intent = "task"

        self.conversation_history.append({"role": "assistant", "content": reply or "..."})

        self.logger.info(
            f"[Orchestrator] Intent: intent={intent} deferred={deferred}",
            component="Orchestrator",
        )

        if intent != "task":
            # Emit the reply only if it wasn't already streamed. When the
            # INTENT stream fell back to non-streaming (or no chunk hook was
            # set), `_last_response_streamed` is False and a non-empty reply
            # still needs to go out via the batch sink.
            quality_prefetch.cancel()
            if reply and self._on_reply_to_user and not self._last_response_streamed:
                self._on_reply_to_user(reply)
            return reply or "I'm here. Send me a task when you're ready."

        # Task path: ensure the speculative QUALITY recall has finished and
        # populated _ltm_block_cache before _run_planner reads it. The rerank
        # started in parallel with INTENT streaming, so at most we wait
        # (rerank_time - intent_time) — the same wait _run_planner would have
        # incurred anyway, just shifted earlier in the timeline.
        try:
            await quality_prefetch
        except (asyncio.CancelledError, Exception):
            pass  # _run_planner does its own recall on cache miss

        # Run the unified planner under the lock so we don't race with the
        # background planner_loop's own call.
        #
        # Freeze the task-root recall query ONLY when the checklist has
        # no in-flight work — either this is the first task of the session
        # (total_items == 0) OR the previous task fully completed (every item
        # marked done, so completed_count == total_items). Mid-task task-
        # modifier messages that get forcibly reclassified as task via
        # deferred_actions ("also handle concurrency", forced task because
        # deferred is non-empty) MUST NOT overwrite the anchor — otherwise
        # the modifier replaces "refactor foo.py" and subsequent item-
        # completion re-plans lose the original task's recall anchor.
        #
        # WHY NOT ``total_items == 0`` alone: total_items is monotonic — it
        # never shrinks after items are added, so once task 1 completes with
        # 10 items, total_items stays at 10 forever. Using == 0 would mean
        # only the very first task of the whole session gets a root query,
        # and every subsequent task inherits task 1's stale anchor. The
        # completed_count equality captures "checklist is idle" correctly
        # regardless of session history.
        _cl = self._checklist
        _task_idle = _cl.total_items == _cl.completed_count
        if self._task_root_query is None or _task_idle:
            self._task_root_query = message
        async with self._planner_lock:
            plan_reply = await self._run_planner(
                trigger="user_msg",
                fallback_actions=deferred,
            )
        if plan_reply:
            return plan_reply
        return reply or ""

    # ── Unified planner (used by user-msg path AND planner_loop) ─────────────

    async def _run_planner(
        self,
        trigger: str = "user_msg",
        precomputed_sections: Optional[Dict[str, str]] = None,
        fallback_actions: Optional[List[str]] = None,
    ) -> str:
        """Single planner LLM call. Shared by Stage 2 and the background loop.

        Caller MUST hold `_planner_lock`. Builds the unified PLAN_MODIFY prompt
        from current checklist state, calls the LLM with json_mode, applies the
        output via `_apply_planner_output`, and emits the task-completion reply
        when the resulting state has nothing in flight.

        `trigger` is "user_msg" or "mark_done" — used only in logs.
        `precomputed_sections` lets a caller forward pre-built sections when
        (and only when) they were gathered under ``RecallTier.QUALITY`` (the
        planner tier). Cross-tier reuse is unsafe — the sections would carry
        an unreranked LTM block. In practice both callers now pass None and
        this method re-gathers under QUALITY.
        `fallback_actions` is the intent-stage `deferred` plan. Used ONLY on the
        user_msg path as a safety net: if the planner produces no items (e.g. a
        JSON parse failure yielded an empty dict), these are materialized into
        checklist items so a triggered task never silently idles. The
        background `mark_done` loop passes None.

        Returns "" — the planner has no user-facing channel. The completion
        reply (when emitted) goes out separately via _on_reply_to_user, and
        the synchronous user-message reply comes from the INTENT stage.
        """
        # Activity strip → "designing…": the planner is composing/revising the
        # checklist. Fires for both the user-message path and the post-item
        # background loop. The agent's own thinking/executing states (or the
        # idle transition in _emit_completion_reply) supersede it next.
        self._notify_state("planning")

        sections = precomputed_sections or await self._gather_context_sections(
            RecallTier.QUALITY,
            query_override=self._task_root_query,
        )
        full_context_block = self._format_for_planner(sections)

        # Quality controls: loop detection + epistemic inventory preamble.
        completed_results = self._checklist.get_completed_results()
        loop_warning = self._detect_loops(completed_results)
        failure_tail_warning = self._build_failed_tail_warning(completed_results)
        last_user = self._last_user_message()
        epistemic_preamble = self._build_epistemic_inventory_warning(
            last_user, completed_results
        )

        # Skills section reflects current active state (read from checklist).
        skills_section = self._build_skills_section()

        system_prompt = build_plan_modify_system_prompt(
            on_demand_tools_table=self._on_demand_tools_table,
            on_demand_routing_rules=self._on_demand_routing_rules,
            on_demand_antipatterns=self._on_demand_antipatterns,
            skills_section=skills_section,
        )

        user_content = PLAN_MODIFY_TEMPLATE.format(
            full_context_block=full_context_block,
            epistemic_preamble=epistemic_preamble,
            loop_warning=loop_warning,
            failure_tail_warning=failure_tail_warning,
            user_message=last_user,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # PLAN_MODIFY has no user-facing channel — the planner schema no longer
        # carries a response_to_user field. We still use the streaming variant
        # for its json_mode parse + network-wait behaviour, but pass
        # on_response_chunk=None so nothing reaches the UI. Even if the model
        # emits a stray response_to_user, it is parsed into the dict and then
        # ignored (see _apply_planner_output) — never streamed, never surfaced.
        # The sole planner-originated user message is the completion summary.
        parsed = await self._call_and_parse_streaming(
            messages,
            "plan_modify",
            on_response_chunk=None,
            extra_kwargs={"json_mode": True},
        ) or {}

        # Apply: skills + tools commit, optional interrupt, post-current
        # replacement. Returns True iff the resulting state has nothing in
        # flight (task-complete candidate — still subject to verification
        # gate below).
        task_complete = await self._apply_planner_output(parsed)

        # Verification gate (B1): when the planner thinks the task is done,
        # decide whether to verify or skip, run goal-level acceptance synthesis
        # if needed, and either emit completion reply or inject a corrective
        # item to address gaps.
        if task_complete:
            await self._handle_task_complete_candidate()
        elif (
            self._checklist.get_current_item() is None
            and not self._checklist.has_pending
        ):
            # Planner left nothing for the agent to run and this isn't a
            # completion. On the background mark_done loop this is benign (the
            # re-plan simply found nothing left to do). But a freshly triggered
            # user task that yields no items is the silent-idle bug: e.g. a
            # JSON parse failure collapsed `parsed` to {} and the user's
            # request would vanish unexecuted. The planner has no
            # response_to_user channel any more, so a user_msg trigger with no
            # items ALWAYS needs recovery. Recover before settling the strip.
            recovered = False
            if trigger == "user_msg":
                if fallback_actions:
                    self.logger.warning(
                        f"[Orchestrator] Planner yielded no items/reply for a "
                        f"user task — materializing {len(fallback_actions)} "
                        f"deferred fallback action(s)",
                        component="Orchestrator",
                    )
                    await self._apply_planner_output(
                        {"post_current_items": [
                            {"instruction": a} for a in fallback_actions
                        ]},
                    )
                    recovered = (
                        self._checklist.get_current_item() is not None
                        or self._checklist.has_pending
                    )
                if not recovered:
                    self.logger.warning(
                        "[Orchestrator] Planner yielded nothing actionable for "
                        "a user task and no usable fallback was available — "
                        "asking the user to rephrase",
                        component="Orchestrator",
                    )
                    if self._on_reply_to_user:
                        try:
                            self._on_reply_to_user(
                                "I wasn't able to turn that into a concrete "
                                "plan. Could you rephrase or add a bit more "
                                "detail?"
                            )
                        except Exception:
                            pass
            if not recovered:
                # No item will flip the strip to "executing" and the completion
                # path didn't fire, so settle it here — otherwise it stays
                # stuck on "designing…" forever.
                self._notify_state("idle")

        self.logger.info(
            f"[Orchestrator] Planner({trigger}): "
            f"interrupt={parsed.get('interrupt_current')} "
            f"items={len(parsed.get('post_current_items') or [])} "
            f"task_complete={task_complete}",
            component="Orchestrator",
        )
        return ""

    async def _call_and_parse_streaming(
        self,
        messages: List[Dict[str, str]],
        log_context: str,
        on_response_chunk: Optional[Callable[[str], Any]] = None,
        extra_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """Call LLM with streaming and stream `response_to_user` chunks.

        Used by both INTENT and PLAN_MODIFY (PLAN passes
        `extra_kwargs={"json_mode": True}`).

        `response_to_user` is streamed chunk-by-chunk via `on_response_chunk`
        as the JSON arrives. The full response is parsed at the end and
        returned as a dict. Other fields (intent, post_current_items,
        skills_needed, etc.) are ONLY available after the stream completes
        — the stream is not used to dispatch any logic early, since the
        decisions that consume those fields all run after the stream is done.

        Falls back to non-streaming on error.
        """
        extra_kwargs = extra_kwargs or {}
        streamer_response = JsonKeyStreamer("response_to_user")
        accumulated: List[str] = []
        streamed_any = False
        # Reset the cross-call flag up front: if we fall back to non-streaming
        # below, no fragments are pushed and this stays False so callers know
        # the reply still needs a batch emit.
        self._last_response_streamed = False

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
            # True; a silent (empty response_to_user) run → False. The
            # plan_modify path passes on_response_chunk=None, so this stays
            # False there and the planner reply is never surfaced.
            self._last_response_streamed = streamed_any
            parsed = try_parse_json(full_text)
            if isinstance(parsed, dict):
                return parsed
            # Parse failure (model emitted prose under json_mode, or truncated
            # JSON). A silent None here propagates to `or {}` at the call site
            # and — for a fresh task — silently idles the planner. Retry ONCE
            # non-streaming before giving up; the retry usually returns clean
            # JSON and keeps the task alive.
            self.logger.warning(
                f"Orchestrator {log_context} stream produced non-JSON "
                f"({len(full_text)} chars) — one non-streaming retry",
                component="Orchestrator",
            )
            return await self._call_and_parse(messages, log_context, extra_kwargs=extra_kwargs)

        except NetworkUnavailableError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Orchestrator {log_context} streaming failed: {e} — non-streaming fallback",
                component="Orchestrator",
            )
            return await self._call_and_parse(messages, log_context, extra_kwargs=extra_kwargs)
        finally:
            # Seal the streamed reply bubble so the UI finalizes it. Only when
            # we actually pushed fragments — an empty response_to_user (silent
            # mid-task planner run) must not leave a dangling empty bubble.
            # In `finally` so a mid-stream error still closes whatever showed.
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
        extra_kwargs = extra_kwargs or {}
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
    #
    # Two consumers (INTENT and PLAN_MODIFY) both need the same 3 sections
    # (LTM, conversation, CheckList) but want different ordering:
    #   - INTENT runs frequently (every user message). Within Anthropic's
    #     5-minute prefix-cache TTL, consecutive INTENT calls can hit cache,
    #     so we want the append-only section (Conversation) at the FRONT
    #     and query-dependent / volatile sections (LTM, CheckList) at the BACK.
    #     This maximises the byte-stable prefix between calls.
    #   - PLAN_MODIFY's mark_done trigger cycle is often >5 min (one Agent
    #     item can take that long), so prefix cache rarely helps. Optimise
    #     for LLM attention instead: put background context first, the
    #     CheckList state (what the planner is operating on) last so it sits
    #     close to the user message.
    #
    # `_gather_context_sections` builds all 4 once; the two `_format_for_*`
    # functions just pick the order. The user-message path runs INTENT and
    # PLAN_MODIFY back-to-back and reuses the same gather (LTM recall is
    # the same query and is the most expensive section).

    async def _gather_context_sections(
        self,
        tier: RecallTier,
        *,
        query_override: Optional[str] = None,
    ) -> Dict[str, str]:
        """Compute every context section ONCE.

        ``tier`` selects the LTM recall lane:
          * ``RecallTier.FAST``    — no rerank, chat / INTENT hot path.
          * ``RecallTier.QUALITY`` — rerank on, Planner / dynamic-K path.
        Cache is keyed by (query, tier.value) so both lanes can coexist for the
        same query without evicting each other. Callers MUST NOT share sections
        gathered under one tier with a stage that expects the other tier.

        ``query_override`` overrides the default recall query
        (``conversation_history[-1]``). Planner passes ``self._task_root_query``
        so item-completion re-plans keep anchoring on the original task goal
        instead of the latest utterance.

        Dual-recall: if ``query_override`` is provided AND the latest user
        message diverged from it (mid-task chat / task-modifier), we ALSO
        recall against the latest message and concatenate the results. This
        keeps the planner's LTM context anchored on the task goal (primary
        recall) while surfacing memory relevant to whatever the user just
        added (extra recall) — so "use the DNS-retry approach we discussed"
        can still pull the DNS-retry memory even mid-task. Each recall is
        cached at its own (query, tier) key; the second call is free on
        repeat within the 60s TTL.

        Returns a dict with keys:
          ltm           — formatted LTM recall block (may be "")
          conversation  — formatted prior-conversation block, header included
                          (may be "")
          checklist     — `[Current CheckList]` block, always present;
                          contains `_EMPTY_CHECKLIST_MARKER` when idle.
        """
        default_query = (
            self.conversation_history[-1]["content"]
            if self.conversation_history else ""
        )
        primary_query = (
            query_override if query_override is not None else default_query
        )
        ltm_block = await self._build_long_term_block(primary_query, tier=tier)

        # Dual-recall condition: planner supplied a root query AND the current
        # tail message is a DIFFERENT string (i.e. user chatted / added a
        # modifier since task start). No extra recall on the first task turn
        # (root == tail) or on pure INTENT calls (query_override is None).
        if (
            query_override is not None
            and default_query
            and default_query != primary_query
        ):
            extra_block = await self._build_long_term_block(
                default_query, tier=tier, bare=True,
            )
            if extra_block and extra_block.strip():
                divider = (
                    "\n<!-- Additional recall for latest user message: "
                    f"{default_query[:80]!r} -->\n"
                )
                ltm_block = (ltm_block or "") + divider + extra_block
        conv_raw = self._format_conversation_history()
        conversation_block = (
            f"[Recent Conversation History]\n{conv_raw}\n" if conv_raw else ""
        )
        checklist_body = (
            self._checklist.get_checklist_context_for_planner()
            if self._checklist.total_items > 0
            else _EMPTY_CHECKLIST_MARKER
        )
        checklist_block = f"[Current CheckList]\n{checklist_body}\n"

        return {
            "ltm": ltm_block,
            "conversation": conversation_block,
            "checklist": checklist_block,
        }

    def _format_for_intent(self, sections: Dict[str, str]) -> str:
        """Cache-friendly ordering: append-only sections first, volatile last.

        Conversation history grows by appending — its byte prefix stays
        stable across consecutive user messages within the cache TTL window.
        LTM is query-dependent so it can change between turns; placing it
        AFTER the append-only block keeps that block cacheable as a
        contiguous prefix. CheckList is the most volatile section (mutates
        within a turn on every mark_done) and goes last.
        """
        parts = [
            sections["conversation"],
            sections["ltm"],
            sections["checklist"],
        ]
        return "\n".join(p for p in parts if p) + "\n"

    def _format_for_planner(self, sections: Dict[str, str]) -> str:
        """Semantic-importance ordering: background first, operating state last.

        The planner's attention should be most focused on the CheckList
        (what it is about to mutate) and the user's request (rendered by the
        prompt template AFTER this block). Push background context (LTM,
        prior conversation) to the front so it informs but does not
        dominate. Cache rarely hits on this path, so order is purely an
        attention argument.
        """
        parts = [
            sections["ltm"],
            sections["conversation"],
            sections["checklist"],
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

    async def _build_long_term_block(
        self, query: str, *, tier: RecallTier, bare: bool = False,
    ) -> str:
        """Recall LTM context for *query* at the requested *tier*. Falls back to "" on any error.

        Tier semantics (see ``_constants.py`` §2b):
          * ``RecallTier.FAST``    — ``rerank=False, dynamic_k=False``. Sub-second
            latency, RRF+recency ordering only. Used on the chat-turn hot path
            (INTENT stage) where the user is actively waiting.
          * ``RecallTier.QUALITY`` — ``rerank=True, dynamic_k=True``. LLM cross-
            encoder rerank + score-gap trimming bounded by
            ``RECALL_PLANNER_MIN_K`` / ``MAX_K``. Used for Planner and other
            paths where the downstream decision has high stakes.

        ``bare=True`` skips the ``[Long-Term Context]`` header + description
        + identity + known-entities blocks and returns ONLY the
        ``<memory-context>`` / ``<knowledge-context>`` tags for *query*. Used
        by the dual-recall path in ``_gather_context_sections`` where the
        primary recall already carries the shared prelude — concatenating two
        full blocks would produce duplicate headers and repeat identity
        directives, wasting prompt tokens.

        Cache is keyed by ``(query, tier.value, bare)`` so the bare / full
        variants coexist without evicting each other. During a task, the
        background planner_loop re-plans after every item completion — but the
        recall query stays frozen (task_root_query) and the LTM corpus is
        frozen too (triage has a 60s floor). The N background re-plans of one
        task therefore all hit the QUALITY cache entry. A new user message
        changes the query → cache miss → fresh recall, so per-turn freshness
        is preserved.
        """
        cache_key = (query, tier.value, bare)
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
        # ``/local/mnt/wine/...`` (Linux-SSH-observed) insights to the planner
        # and trigger the wine-bug class of UNC→SSH mistranslation.
        current_frame = {"os": "windows", "host": "local", "confidence": 0.95}
        rerank = (tier == RecallTier.QUALITY)
        dynamic_k = (tier == RecallTier.QUALITY)
        # Fast tier bypasses rerank, and RECALL_RERANK_MIN_SCORE is the
        # authoritative relevance cutoff — without rerank, top-K by RRF would
        # surface activity-snapshot noise (cosine band 0.34-0.41) as "least
        # bad" matches on chat turns even when the query has no real hit.
        # Raise the dense-branch floor so noise-band rows are pre-filtered out.
        # Quality tier keeps the standard floor because rerank still runs.
        from ..infrastructure.long_term_memory._constants import (
            RECALL_MIN_SCORE,
            RECALL_MIN_SCORE_FAST,
        )
        min_score = RECALL_MIN_SCORE if rerank else RECALL_MIN_SCORE_FAST
        try:
            self._notify_recall_started()
            block = await ltm.format_context_block(
                query=query,
                rerank=rerank,
                dynamic_k=dynamic_k,
                min_score=min_score,
                include_identity=not bare,
                include_known_entities=not bare,
                include_header=not bare,
                current_frame=current_frame,
            )
        except Exception:
            self.logger.debug(
                "LTM format_context_block failed", component="Orchestrator"
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
        been processed by INTENT and possibly PLAN_MODIFY.
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

        Called only from the terminal verdict branches of
        `_handle_task_complete_candidate` — i.e. exactly when a completion
        reply is emitted. ``success`` is derived from the acceptance verdict
        (PASS/TRIVIAL → True; ACCEPT-with-gap / unknown → False). Like the
        user-turn submitter, we don't await: triage runs for seconds to
        minutes and must not block the planner.
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
        last_steps = self._checklist.get_completed_results()
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

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _on_item_done_sync(self, result: ItemResult) -> None:
        """Callback from CheckList — wake the planner_loop.

        Fires synchronously when agent calls mark_current_done. We just set
        the trigger event; the planner_loop wakes, acquires the planner lock,
        and runs `_run_planner` against the new state. No queue, no per-result
        eval object — the loop reads everything off the checklist.
        """
        self._planner_trigger.set()

    def _on_progress_concern_sync(self, concern: ProgressConcern) -> None:
        """Callback from CheckList — wake the planner_loop on a watcher verdict.

        Identical mechanism to _on_item_done_sync: set the trigger and let the
        planner_loop read the concern (and the in-flight digests) off the
        checklist context. Runs synchronously in the agent coroutine via the
        bus callback, so it does no real work beyond flipping the event.
        """
        self._planner_trigger.set()

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

    def _activate_tools_in_checklist(self, parsed: Dict) -> List[str]:
        """Activate tools listed in parsed['tools_needed'] into the checklist.

        Declarative schema: planner declares all tools the remaining items
        need. The system diffs against active state and only activates the
        delta. Re-listing already-active tools is safe (no-op).
        """
        raw = parsed.get("tools_needed") or []
        if not isinstance(raw, list):
            raw = [raw]
        names = [str(n).strip() for n in raw if str(n).strip()]
        if not names:
            return []
        new = self._checklist.activate_tools(names)
        if new:
            self.logger.info(
                f"[Orchestrator] Tools activated: {new}",
                component="Orchestrator",
            )
        return new

    def _last_user_message(self) -> str:
        """Return the most recent user turn from conversation_history.

        Used as the semantic anchor for downstream consumers that need a
        "current goal" string (background eval, LTM triage, acceptance
        synthesis). Captures the latest task framing so it stays accurate
        across multi-turn refinement.
        """
        for turn in reversed(self.conversation_history):
            if turn.get("role") == "user":
                return turn.get("content", "")
        return ""

    async def _apply_planner_output(self, parsed: dict) -> bool:
        """Apply a planner LLM response to the checklist.

        Schema:
          {
            "interrupt_current": bool,
            "interrupt_reason": str (optional, empty when no interrupt),
            "post_current_items": [item_dict, ...],
            "skills_needed": [name, ...],
            "tools_needed": [name, ...]
          }

        The planner has no user-facing channel — there is no response_to_user
        field. The only planner-originated user message is the completion
        summary composed in `_emit_completion_reply`.

        Returns True iff the resulting checklist has nothing in flight (no
        current item AND no pending) — this is the trigger for emitting the
        task-completion summary.
        """
        # Skills + tools first — independent from items, can fail without
        # affecting the rest of the operation.
        try:
            self._activate_skills_in_checklist(parsed)
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] _activate_skills_in_checklist failed: {e}",
                component="Orchestrator",
            )
        try:
            self._activate_tools_in_checklist(parsed)
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] _activate_tools_in_checklist failed: {e}",
                component="Orchestrator",
            )

        interrupt = bool(parsed.get("interrupt_current"))
        interrupt_reason = str(parsed.get("interrupt_reason") or "").strip()
        raw_items = parsed.get("post_current_items") or []
        if not isinstance(raw_items, list):
            raw_items = []

        new_items: List[CheckListItem] = []
        for data in raw_items:
            if not isinstance(data, dict):
                continue
            item = CheckListItem.from_planner_dict(data)
            if item.instruction and len(item.instruction.strip()) > 20:
                new_items.append(item)

        # Order matters: write the new post-current tail FIRST, then send
        # the interrupt. If we interrupted first, a fast agent could process
        # the interrupt, advance current_index past the in-flight item, and
        # pick up whatever was in the OLD pending tail before
        # replace_post_current ran — replace can no longer touch that item
        # because it has already become the new "current". By writing the
        # tail first, the agent's next pickup after the interrupt is
        # guaranteed to be the new head.
        await self._checklist.replace_post_current(new_items)

        if interrupt:
            try:
                await self._checklist.interrupt_agent(reason=interrupt_reason)
            except Exception as e:
                self.logger.warning(
                    f"[Orchestrator] interrupt_agent failed: {e}",
                    component="Orchestrator",
                )

        # "Task done" = nothing in flight AND nothing pending AND we have
        # already executed something (completed_count > 0). Empty initial
        # state shouldn't trigger completion.
        return (
            self._checklist.completed_count > 0
            and not self._checklist.has_pending
            and self._checklist.get_current_item() is None
        )

    async def _audit_acceptance_spinning(
        self,
        latest: ItemResult,
        prior: List[ItemResult],
    ) -> ProgressConcern:
        """Cheap-LLM semantic check: is the acceptance loop truly spinning?

        Called ONLY after the mechanical delta is already 0 (see
        `_handle_task_complete_candidate`). Judges whether the latest
        acceptance round repeated an approach already tried — a reworded but
        identical blocker is STILL spinning — or pursued a materially different
        angle the set-difference could not detect.

        Awaited (once per round at a task boundary, latency is fine), unlike
        the fire-and-forget in-item progress watcher. Fail-toward-terminate:
        an empty helper pool, an LLM error, or an unparseable reply all yield
        `false_progress`, because the mechanical signal already says "no new
        info" — when in doubt, stop rather than spin.
        """
        fallback = ProgressConcern(
            item_id=latest.item_id,
            verdict="false_progress",
            rationale="auditor unavailable; mechanical delta==0 ⇒ terminate",
        )
        if not self._helper_services:
            return fallback

        from .agent_prompts import ACCEPTANCE_SPINNING_PROMPT

        def _render(r: ItemResult) -> str:
            parts: List[str] = []
            if r.factual_outcome:
                parts.append(f"outcome: {'; '.join(r.factual_outcome)}")
            if r.key_findings:
                parts.append(f"findings: {'; '.join(r.key_findings)}")
            if r.artifacts:
                parts.append(f"artifacts: {', '.join(r.artifacts)}")
            if r.issues:
                parts.append(f"issues: {'; '.join(r.issues)}")
            body = " | ".join(parts) if parts else "(no structured output)"
            return f"[{r.item_id}] {body}"

        prompt = ACCEPTANCE_SPINNING_PROMPT.format(
            prior_rounds="\n".join(_render(r) for r in prior) or "(none)",
            latest_round=_render(latest),
        )

        try:
            result = await call_with_fallback(
                self._helper_services,
                dict(
                    messages=[{"role": "user", "content": prompt}],
                    json_mode=True,
                    max_tokens=400,
                ),
            )
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] Acceptance auditor LLM call failed "
                f"({type(e).__name__}: {e}) — defaulting to false_progress.",
                component="Orchestrator",
            )
            return fallback

        parsed = try_parse_json(result.content or "")
        if not isinstance(parsed, dict):
            return fallback
        verdict = str(parsed.get("verdict", "false_progress")).strip().lower()
        if verdict not in ("false_progress", "ok"):
            verdict = "false_progress"
        return ProgressConcern(
            item_id=latest.item_id,
            verdict=verdict,
            rationale=str(parsed.get("rationale", ""))[:500],
        )

    async def _handle_task_complete_candidate(self) -> None:
        """Verification gate at task-complete candidate state.

        Called from `_run_planner` when `_apply_planner_output` returned True
        (post-current empty AND no in-progress AND completed_count > 0).

        Mechanical 5-verdict dispatcher. The verifier itself decides whether
        to skip (TRIVIAL), pass (PASS), inject more work (EXTEND/VALIDATE),
        or finalise with a noted gap (ACCEPT). The verifier prompt is asked to
        ACCEPT once `acceptance_*` items are visible in the completed list, but
        that is advisory. The hard stop is **information-gain based**, not a
        round count — it mirrors the in-item control loop (planner decides →
        agent executes → Tier-0 mechanical sense → Tier-1 cheap-LLM observation
        → planner intervenes):

          - Tier-0 (mechanical, ungameable): `acceptance_info_delta` measures
            what the latest acceptance round added over the union of all prior
            completed results. `total_new > 0` ⇒ genuine exploration, EXTEND is
            honored. `total_new == 0` ⇒ candidate produced nothing new.
          - Tier-1 (cheap semantic auditor): fires ONLY when delta == 0, to
            catch reworded-identical spinning the mechanical diff would miss.
            `false_progress` ⇒ terminal ACCEPT (the persistent blocker becomes
            the gap); `ok` ⇒ honor EXTEND (mechanical missed real novelty).
          - Seatbelt (`_ACCEPTANCE_SEATBELT_ROUNDS`): defense-in-depth ceiling
            for pathological novelty signals (e.g. timestamped artifact paths);
            logged at WARNING and never the deciding mechanism in normal runs.

        Action map:
          PASS / TRIVIAL → emit completion reply, no further work.
          EXTEND / VALIDATE → replace_post_current(items_to_inject); the
                              agent runs them, mark_done re-enters this gate.
          ACCEPT → emit completion reply with gap_summary prefix.
          unknown → treat as ACCEPT (defensive).
        """
        completed = self._checklist.get_completed_results()
        if not completed:
            return  # defensive: gate triggered with no completed results

        try:
            verdict = await self.synthesize_acceptance(
                conversation_history=self.conversation_history,
                completed_results=completed,
                checklist_items=self._checklist.items,
            )
        except Exception as e:
            self.logger.warning(
                f"[Orchestrator] synthesize_acceptance raised "
                f"({type(e).__name__}: {e}) — fallback ACCEPT",
                component="Orchestrator",
            )
            from .planner_mixin import AcceptanceVerdict
            verdict = AcceptanceVerdict(
                verdict="ACCEPT",
                gap_summary="Verification synthesis failed.",
                fallback=True,
            )

        self.logger.info(
            f"[Orchestrator] Verdict: {verdict.verdict} "
            f"items_to_inject={len(verdict.items_to_inject)} "
            f"gap={verdict.gap_summary[:80]!r}",
            component="Orchestrator",
        )

        # First-round-ACCEPT guard: a gap-bearing ACCEPT before any
        # acceptance_* item has run is almost always the verifier giving up
        # too early. Snap to EXTEND with a corrective acceptance_* item; the
        # prompt's "don't loop after acceptance_* ran" rule still bounds
        # termination on subsequent rounds. Skipped on fallback (synthesis
        # crashed — no real verification signal to act on).
        acceptance_results = [
            r for r in completed
            if r.item_id.startswith(self._ACCEPTANCE_PREFIX)
        ]
        acceptance_count = len(acceptance_results)
        if (
            verdict.verdict == "ACCEPT"
            and verdict.gap_summary
            and acceptance_count == 0
            and not verdict.fallback
        ):
            self.logger.warning(
                f"[Orchestrator] First-round ACCEPT with non-empty gap "
                f"({verdict.gap_summary[:80]!r}) — snapping to EXTEND.",
                component="Orchestrator",
            )
            corrective = CheckListItem(
                item_id=f"acceptance_{str(_uuid.uuid4())[:6]}",
                instruction=(
                    f"Verification gap: {verdict.gap_summary} "
                    "Re-attempt the missing observable evidence using whichever "
                    "tool applies (SSH / browser / email / web_search). If a "
                    "required tool is genuinely unavailable, document the named "
                    "blocker explicitly rather than producing a prose summary."
                ),
                expected_outcomes=[
                    "Observable evidence for the gap is collected, OR the "
                    "blocker is explicitly documented with the missing "
                    "tool/credential named.",
                ],
                planner_reasoning=(
                    "Acceptance guard: first-round ACCEPT with non-empty gap "
                    "rejected; force one EXTEND round before terminal ACCEPT."
                ),
            )
            from .planner_mixin import AcceptanceVerdict
            verdict = AcceptanceVerdict(
                verdict="EXTEND",
                gap_summary=verdict.gap_summary,
                items_to_inject=[corrective],
            )

        # Information-gain termination (replaces the old fixed round cap).
        # Fires once at least one acceptance_* round has completed and the
        # verifier still wants more work. The first-round guard above can only
        # fire at acceptance_count == 0, so it never collides with this block.
        # Mirrors the in-item control loop: the Tier-0 mechanical delta gates
        # whether the Tier-1 cheap auditor runs, and only their combination
        # terminates — so genuine exploration is never killed and pure spinning
        # is stopped on the first round that adds nothing.
        if (
            verdict.verdict in ("EXTEND", "VALIDATE")
            and acceptance_count >= 1
        ):
            from .planner_mixin import AcceptanceVerdict

            if acceptance_count >= self._ACCEPTANCE_SEATBELT_ROUNDS:
                # Defense-in-depth only — must not be the normal exit. Logged
                # loud so a spurious trip (e.g. timestamped artifact paths that
                # always read as "new", defeating the set-difference) is
                # diagnosable from a single WARNING line.
                self.logger.warning(
                    f"[Orchestrator] Acceptance seatbelt engaged "
                    f"({acceptance_count} rounds) — forcing "
                    f"{verdict.verdict}→ACCEPT. Novelty signal likely defeated; "
                    f"investigate if this fires in normal operation.",
                    component="Orchestrator",
                )
                verdict = AcceptanceVerdict(
                    verdict="ACCEPT",
                    gap_summary=verdict.gap_summary or (
                        "Acceptance seatbelt reached with an unresolved gap; "
                        "finalising."
                    ),
                )
            else:
                latest = acceptance_results[-1]
                prior = [r for r in completed if r is not latest]
                delta = acceptance_info_delta(latest, prior)
                total_new = delta["total_new"]
                if total_new == 0:
                    # Tier-0 says the round added nothing new. Ask the cheap
                    # auditor whether that is true semantic spinning or merely
                    # reworded novelty the set-difference cannot see.
                    concern = await self._audit_acceptance_spinning(latest, prior)
                    if concern.verdict == "false_progress":
                        self.logger.info(
                            f"[Orchestrator] Acceptance spinning confirmed "
                            f"(delta=0, auditor=false_progress: "
                            f"{concern.rationale[:80]!r}) — terminal ACCEPT.",
                            component="Orchestrator",
                        )
                        verdict = AcceptanceVerdict(
                            verdict="ACCEPT",
                            gap_summary=verdict.gap_summary or (
                                "Acceptance loop made no new progress against a "
                                "persistent blocker; finalising."
                            ),
                        )
                    else:
                        # Auditor sees genuine novelty the diff missed — honor
                        # the EXTEND/VALIDATE and let the agent continue.
                        self.logger.info(
                            f"[Orchestrator] Acceptance delta=0 but auditor "
                            f"judged novel ({concern.rationale[:80]!r}) — "
                            f"honoring {verdict.verdict}.",
                            component="Orchestrator",
                        )
                else:
                    self.logger.info(
                        f"[Orchestrator] Acceptance round produced new info "
                        f"(total_new={total_new}) — honoring {verdict.verdict}.",
                        component="Orchestrator",
                    )

        if verdict.verdict in ("PASS", "TRIVIAL"):
            self._submit_session_to_ltm(success=True)
            self._emit_completion_reply()
            await self._run_task_complete_cleanup()
            return

        if verdict.verdict == "ACCEPT":
            # Skip the LTM write on a fallback ACCEPT (synthesis crashed →
            # no real verification signal). A real gap_summary is mined as
            # a lesson via the SESSION_FAILED path (success=False).
            if not verdict.fallback:
                self._submit_session_to_ltm(success=False)
            prefix = (
                f"(verification: {verdict.gap_summary})\n"
                if verdict.gap_summary else ""
            )
            self._emit_completion_reply(prefix=prefix)
            await self._run_task_complete_cleanup()
            return

        if verdict.verdict in ("EXTEND", "VALIDATE"):
            if not verdict.items_to_inject:
                # Defensive: verdict said extend/validate but produced no
                # actionable item — finalise rather than stall.
                self._emit_completion_reply(
                    prefix=f"(verification {verdict.verdict.lower()} produced no item)\n"
                )
                await self._run_task_complete_cleanup()
                return
            await self._checklist.replace_post_current(verdict.items_to_inject)
            # No completion reply emitted: agent will run injected items →
            # mark_done → planner_loop → re-enter this gate.
            return

        # Unknown verdict (AcceptanceVerdict.from_dict already snaps unknowns
        # to ACCEPT, but be defensive in case future code paths bypass it).
        self._submit_session_to_ltm(success=False)
        self._emit_completion_reply(
            prefix=f"(unknown verdict {verdict.verdict!r} — finalising)\n"
        )
        await self._run_task_complete_cleanup()

    async def _run_task_complete_cleanup(self) -> None:
        """Invoke the task-complete cleanup callback, swallowing any error.

        Fires at every terminal verdict path (PASS / TRIVIAL / ACCEPT /
        VALIDATE-with-empty-items / unknown), but NOT at EXTEND / VALIDATE
        with more work queued — those flow back into the acceptance loop.
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

        Defensive: a misbehaving UI callback must never derail planning.
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
        """Compose + send the final task-completion reply (private helper)."""
        # Reaching here means a terminal verdict (PASS / TRIVIAL / ACCEPT /
        # unknown) — the checklist has nothing in flight. Settle the activity
        # strip to idle before the summary goes out; covers the no-body early
        # return below too, so the strip never stays stuck on "designing…".
        self._notify_state("idle")
        body = self._compose_completion_reply()
        if not body:
            return
        # Separate the verification/finalisation prefix from the markdown body
        # with a blank line so the prefix renders as its own paragraph above
        # the `## ` header rather than being glued onto it.
        prefix = prefix.strip()
        full = f"{prefix}\n\n{body}" if prefix else body
        if self._on_reply_to_user:
            try:
                self._on_reply_to_user(full)
            except Exception:
                pass

    def _compose_completion_reply(self) -> str:
        """Stitch a final task-completion reply from the agent's results.

        Called when planner output leaves the checklist with no pending and
        no in-progress item. Renders structured markdown (no extra LLM call):
        a `## ` header, the last completed item's factual_outcome and
        key_findings as bullet lists, and artifacts aggregated + deduped
        across every completed item (artifacts accrue over the whole task,
        not just the final step).
        """
        completed = self._checklist.get_completed_results()
        if not completed:
            return ""
        last = completed[-1]

        # Artifacts span the whole task — collect across every completed item,
        # deduping while preserving first-seen order.
        artifacts: List[str] = []
        seen: Set[str] = set()
        for r in completed:
            for a in r.artifacts:
                if a and a not in seen:
                    seen.add(a)
                    artifacts.append(a)

        sections: List[str] = ["## Task complete"]
        if last.factual_outcome:
            sections.append(
                "**Outcomes**\n"
                + "\n".join(f"- {o}" for o in last.factual_outcome)
            )
        if last.key_findings:
            sections.append(
                "**Key findings**\n"
                + "\n".join(f"- {f}" for f in last.key_findings)
            )
        if artifacts:
            sections.append(
                "**Artifacts**\n"
                + "\n".join(f"- {a}" for a in artifacts)
            )
        return "\n\n".join(sections)
