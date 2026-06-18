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
  - Stage 1 INTENT receives the same context (LTM, history, shell, checklist)
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, cast

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
from ..infrastructure.utils import try_parse_json
from .mention_preprocessing import preprocess_mentions
from .planner_mixin import PlannerMixin
from .receptionist import ReceptionistMixin
from .shared_checklist import (
    SharedCheckList,
    CheckListItem,
    ItemResult,
)
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
        shell_history_path: Optional[str] = None,
        session_dir: Optional[str] = None,
    ):
        if not llm_services:
            raise ValueError("Orchestrator requires at least one LLMService")
        self._services: List[LLMService] = list(llm_services)
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
        self.shell_history_path = shell_history_path
        self._session_dir = session_dir

        # Session-scoped conversation history (all user + assistant turns)
        self.conversation_history: List[Dict[str, str]] = []

        # Planner-tier LTM recall cache: (query, expiry_monotonic, block).
        # Single-slot — only one recall query is "active" at a time (frozen
        # during a task, replaced on the next user message). Collapses the
        # per-item-completion background re-plan recalls into one real call.
        # See _build_long_term_block for the freshness argument.
        self._ltm_block_cache: Optional[tuple] = None

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
        sections = await self._gather_context_sections()
        intent_context = self._format_for_intent(sections)

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
            if reply and self._on_reply_to_user and not self._last_response_streamed:
                self._on_reply_to_user(reply)
            return reply or "I'm here. Send me a task when you're ready."

        # Task path: run the unified planner under the lock so we don't race
        # with the background planner_loop's own call. Reuse the freshly
        # gathered sections — LTM recall is the same query and is the most
        # expensive section to rebuild.
        async with self._planner_lock:
            plan_reply = await self._run_planner(
                trigger="user_msg", precomputed_sections=sections,
            )
        if plan_reply:
            return plan_reply
        return reply or ""

    # ── Unified planner (used by user-msg path AND planner_loop) ─────────────

    async def _run_planner(
        self,
        trigger: str = "user_msg",
        precomputed_sections: Optional[Dict[str, str]] = None,
    ) -> str:
        """Single planner LLM call. Shared by Stage 2 and the background loop.

        Caller MUST hold `_planner_lock`. Builds the unified PLAN_MODIFY prompt
        from current checklist state, calls the LLM with json_mode, applies the
        output via `_apply_planner_output`, and emits the task-completion reply
        when the resulting state has nothing in flight.

        `trigger` is "user_msg" or "mark_done" — used only in logs.
        `precomputed_sections` lets the user-message path forward the sections
        gathered for INTENT so we don't recompute LTM recall.

        Returns the planner's `response_to_user` (may be empty). The completion
        reply (when emitted) goes out separately via _on_reply_to_user.
        """
        # Activity strip → "designing…": the planner is composing/revising the
        # checklist. Fires for both the user-message path and the post-item
        # background loop. The agent's own thinking/executing states (or the
        # idle transition in _emit_completion_reply) supersede it next.
        self._notify_state("planning")

        sections = precomputed_sections or await self._gather_context_sections()
        full_context_block = self._format_for_planner(sections)

        # Quality controls: loop detection + epistemic inventory preamble.
        completed_results = self._checklist.get_completed_results()
        loop_warning = self._detect_loops(completed_results)
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
            user_message=last_user,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # Streaming PLAN_MODIFY: response_to_user chunks → UI as JSON arrives.
        # Other fields (post_current_items, skills_needed, etc.) are
        # parsed from the accumulated full text after the stream completes.
        parsed = await self._call_and_parse_streaming(
            messages,
            "plan_modify",
            on_response_chunk=self._on_response_chunk,
            extra_kwargs={"json_mode": True},
        ) or {}
        # If the reply was actually streamed to the user (fragments pushed via
        # `_on_response_chunk`), suppress the batch emit in
        # `_apply_planner_output` below to avoid duplicating the same text. If
        # the stream fell back to non-streaming, `_last_response_streamed` is
        # False and a non-empty reply is still emitted there.
        streamed_reply_to_user = self._last_response_streamed

        # Structural guards (e.g. drop bad ops, validate item shape).
        parsed = self._apply_structural_guards(parsed)

        # Apply: skills + tools commit, optional interrupt, post-current
        # replacement, optional reply. Returns True iff the resulting state has
        # nothing in flight (task-complete candidate — still subject to
        # verification gate below).
        task_complete = await self._apply_planner_output(
            parsed, suppress_reply=streamed_reply_to_user,
        )

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
            # completion (e.g. it just replied to acknowledge a preference the
            # LTM triage will persist). No item will ever flip the strip to
            # "executing", and the completion path — the only other place that
            # emits "idle" — didn't fire, so settle the strip here. Without
            # this it stays stuck on "designing…" forever.
            self._notify_state("idle")

        reply = parsed.get("response_to_user", "") or ""
        self.logger.info(
            f"[Orchestrator] Planner({trigger}): "
            f"interrupt={parsed.get('interrupt_current')} "
            f"items={len(parsed.get('post_current_items') or [])} "
            f"task_complete={task_complete} reply_len={len(reply)}",
            component="Orchestrator",
        )
        return reply

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
            # Record whether we actually streamed the reply: drives the
            # caller's suppress_reply decision. A successful stream that
            # produced fragments → True; a silent (empty response_to_user)
            # run → False.
            self._last_response_streamed = streamed_any
            parsed = try_parse_json(full_text)
            return parsed if isinstance(parsed, dict) else None

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
    # Two consumers (INTENT and PLAN_MODIFY) both need the same 4 sections
    # (LTM, conversation, shell, CheckList) but want different ordering:
    #   - INTENT runs frequently (every user message). Within Anthropic's
    #     5-minute prefix-cache TTL, consecutive INTENT calls can hit cache,
    #     so we want append-only sections (Conversation, Shell) at the FRONT
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

    async def _gather_context_sections(self) -> Dict[str, str]:
        """Compute every context section ONCE.

        Returns a dict with keys:
          ltm           — formatted LTM recall block (may be "")
          conversation  — formatted prior-conversation block, header included
                          (may be "")
          shell         — formatted shell-history block (may be "")
          checklist     — `[Current CheckList]` block, always present;
                          contains `_EMPTY_CHECKLIST_MARKER` when idle.
        """
        ltm_block = await self._build_long_term_block(
            self.conversation_history[-1]["content"]
            if self.conversation_history else ""
        )
        conv_raw = self._format_conversation_history()
        conversation_block = (
            f"[Recent Conversation History]\n{conv_raw}\n" if conv_raw else ""
        )
        shell_block = self._read_shell_history()
        checklist_body = (
            self._checklist.get_checklist_context_for_planner()
            if self._checklist.total_items > 0
            else _EMPTY_CHECKLIST_MARKER
        )
        checklist_block = f"[Current CheckList]\n{checklist_body}\n"

        return {
            "ltm": ltm_block,
            "conversation": conversation_block,
            "shell": shell_block,
            "checklist": checklist_block,
        }

    def _format_for_intent(self, sections: Dict[str, str]) -> str:
        """Cache-friendly ordering: append-only sections first, volatile last.

        Conversation and shell histories grow by appending — their byte
        prefix stays stable across consecutive user messages within the cache
        TTL window. LTM is query-dependent so it can change between turns;
        placing it AFTER the append-only blocks keeps those blocks cacheable
        as a contiguous prefix. CheckList is the most volatile section
        (mutates within a turn on every mark_done) and goes last.
        """
        parts = [
            sections["conversation"],
            sections["shell"],
            sections["ltm"],
            sections["checklist"],
        ]
        return "\n".join(p for p in parts if p) + "\n"

    def _format_for_planner(self, sections: Dict[str, str]) -> str:
        """Semantic-importance ordering: background first, operating state last.

        The planner's attention should be most focused on the CheckList
        (what it is about to mutate) and the user's request (rendered by the
        prompt template AFTER this block). Push background context (LTM,
        shell, prior conversation) to the front so it informs but does not
        dominate. Cache rarely hits on this path, so order is purely an
        attention argument.
        """
        parts = [
            sections["ltm"],
            sections["shell"],
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

    async def _build_long_term_block(self, query: str) -> str:
        """Recall LTM context. Falls back to "" on any error.

        Orchestrator drives INTENT + PLAN, which is the high-stakes planner
        tier per ``_constants.py:62-64``. ``rerank=True`` activates the
        LLM cross-encoder rerank stage; ``dynamic_k=True`` activates score-
        gap trimming bounded by ``RECALL_PLANNER_MIN_K`` / ``MAX_K``.

        Result is cached for ``_LTM_BLOCK_CACHE_TTL_SEC`` keyed by ``query``.
        The background planner_loop re-plans after every item completion, but
        during autonomous execution no new user/assistant turn is appended to
        ``conversation_history`` — so the recall query is frozen for the
        duration of a task, and the LTM corpus is frozen too (triage is async
        with a 60s floor). The N background re-plans of one task therefore
        recompute the *identical* reranked block; the cache collapses those
        into a single real recall. A new user message changes the query →
        cache miss → fresh recall, so per-turn freshness is preserved.
        """
        cached = self._ltm_block_cache
        if (
            cached is not None
            and cached[0] == query
            and time.monotonic() < cached[1]
        ):
            return cached[2]
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
        try:
            self._notify_recall_started()
            block = await ltm.format_context_block(
                query=query, rerank=True, dynamic_k=True,
                current_frame=current_frame,
            )
        except Exception:
            self.logger.debug(
                "LTM format_context_block failed", component="Orchestrator"
            )
            return ""
        self._ltm_block_cache = (
            query, time.monotonic() + _LTM_BLOCK_CACHE_TTL_SEC, block,
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

    # ── Shell history ────────────────────────────────────────────────────────

    def _read_shell_history(self) -> str:
        """Read recent shell commands for context."""
        if not self.shell_history_path:
            return ""
        try:
            content = Path(self.shell_history_path).read_text(encoding="utf-8").strip()
            if not content:
                return ""
            return f"[Shell History]\n{content}\n"
        except Exception:
            return ""

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _on_item_done_sync(self, result: ItemResult) -> None:
        """Callback from CheckList — wake the planner_loop.

        Fires synchronously when agent calls mark_current_done. We just set
        the trigger event; the planner_loop wakes, acquires the planner lock,
        and runs `_run_planner` against the new state. No queue, no per-result
        eval object — the loop reads everything off the checklist.
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

    async def _apply_planner_output(self, parsed: dict, suppress_reply: bool = False) -> bool:
        """Apply a planner LLM response to the checklist.

        Schema:
          {
            "interrupt_current": bool,
            "interrupt_reason": str (optional, empty when no interrupt),
            "post_current_items": [item_dict, ...],
            "skills_needed": [name, ...],
            "tools_needed": [name, ...],
            "response_to_user": "..."
          }

        `suppress_reply=True` means the caller already streamed
        `response_to_user` to the user via `_on_response_chunk` and we should
        NOT batch-emit it here (would duplicate the same text in the UI).

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

        reply = parsed.get("response_to_user", "") or ""
        if reply and self._on_reply_to_user and not suppress_reply:
            try:
                self._on_reply_to_user(reply)
            except Exception:
                pass

        # "Task done" = nothing in flight AND nothing pending AND we have
        # already executed something (completed_count > 0). Empty initial
        # state shouldn't trigger completion.
        return (
            self._checklist.completed_count > 0
            and not self._checklist.has_pending
            and self._checklist.get_current_item() is None
        )

    async def _handle_task_complete_candidate(self) -> None:
        """Verification gate at task-complete candidate state.

        Called from `_run_planner` when `_apply_planner_output` returned True
        (post-current empty AND no in-progress AND completed_count > 0).

        Mechanical 5-verdict dispatcher. The verifier itself decides whether
        to skip (TRIVIAL), pass (PASS), inject more work (EXTEND/VALIDATE),
        or finalise with a noted gap (ACCEPT). The "stop looping" rule is
        enforced inside the verifier prompt by inspecting the completed list
        for items prefixed with `acceptance_` — no host-side round counter.

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

        if verdict.verdict in ("PASS", "TRIVIAL"):
            self._submit_session_to_ltm(success=True)
            self._emit_completion_reply()
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
            return

        if verdict.verdict in ("EXTEND", "VALIDATE"):
            if not verdict.items_to_inject:
                # Defensive: verdict said extend/validate but produced no
                # actionable item — finalise rather than stall.
                self._emit_completion_reply(
                    prefix=f"(verification {verdict.verdict.lower()} produced no item)\n"
                )
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
