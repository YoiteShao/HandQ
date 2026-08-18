"""
PersistentAgent — long-lived agent loop driven by TaskChannel.

Implements the Observe-Think-Act loop directly with streaming tool dispatch.

Feature set:
  - Full PTL recovery (semantic compaction → hard half-drop → retry)
  - IterationAdvisor (anti-repeat guard + parallelism nudge + stagnation detection)
  - Execution recorder (agent_start / write_turn / agent_end per item)
  - Progressive tool loading via task-channel callbacks
  - Interrupt handling at iteration level
  - Per-item static context: item description + provider hint + LTM recall
    (persistent fields, rebuilt into instruction every turn)
  - Effective obs budget (subtracts prelude/summary/item-static overhead)
  - Cross-item boundary view via TaskChannel.get_recent_results_for_agent
  - User confirmation via InteractionManager (risk + tool-specific)
  - Stale-snapshot supersession
"""
import asyncio
import json
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ..infrastructure.anthropic_streaming_service import (
    StreamDoneEvent,
    StreamToolCallEvent,
)
from ..infrastructure.llm_pool import call_with_fallback, call_with_fallback_stream
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.execution_recorder import ExecutionRecorder
from ..models.token_usage import TokenUsage
from ..tools.base_tool import BaseTool, ToolResult
from ..tools.tool_registry import ToolRegistry
from ..tools.shell_tool import looks_read_only

from .interaction_manager import InteractionManager
from .task_channel import TaskChannel, TaskSpec, TaskResult, INTERRUPTED_BY_COORDINATOR
from .risk_check import is_high_risk, get_risk_description, is_path_within_working_dir
from .user_confirmation import UserConfirmation

if TYPE_CHECKING:
    from .session_context import SessionContext
from .agent_utils import (
    LLM_API_ERROR_TAG,
    INFRA_TOOL_NAMES,
    BOOKKEEPING_ONLY_TOOLS,
    TOOL_NAME_ALIASES,
    ToolCall,
    TurnOutcome,
    ConversationTurn,
    TurnDigest,
    ProgressConcern,
    IterationAdvisor,
    CompletionAudit,
    format_tool_entry,
    resolve_obs_budget,
    SUPERSEDABLE_TOOL_ACTIONS,
    extract_self_extension_fields,
)
from ..infrastructure.utils import try_parse_json_with_repair_flag
from .agent_prompts import (
    AGENT_SYSTEM_PROMPT,
    COMPACT_CONVERSATION_PROMPT,
    get_platform_context,
)

# Desktop confirmation helpers — imported eagerly so packaging tools can
# statically discover the dependency and any breakage surfaces at startup.
from ..tools.desktop_tool import (
    is_task_approved as _desktop_is_task_approved,
    mark_task_approved as _desktop_mark_task_approved,
    was_user_rescinded as _desktop_was_user_rescinded,
)


MAX_ITEM_ITERATIONS = 400

# Sentinel returned as TurnOutcome.error from _think_streaming when the user
# interrupts mid-stream. It defeats is_completion (which would otherwise let
# _item_loop treat "user pressed stop" as "task done, final_answer='stream
# interrupted'"), and it's routed to the interrupt handler in _item_loop
# rather than the generic LLM-API-error branch so the item ends as
# "Interrupted by user", not "LLM API error".
INTERRUPTED_BY_USER_MID_STREAM = "INTERRUPTED_BY_USER_MID_STREAM"


class PersistentAgent:
    """V2 persistent agent — fully self-contained, no RuntimeAgent inheritance.

    Session lifecycle:
      - Created once at session start
      - run_loop() blocks indefinitely, processing items from the TaskChannel
      - Between items, blocks on wait_for_current_item()
      - Loop only exits via CancelledError (flow teardown)
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        task_channel: TaskChannel,
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        max_item_iterations: int = MAX_ITEM_ITERATIONS,
        config_manager: Optional[ConfigManager] = None,
        execution_recorder: Optional[ExecutionRecorder] = None,
        interaction_manager: InteractionManager = None,  # required, see check below
        ctx: Optional["SessionContext"] = None,
        expose_session_storage_in_prompt: bool = True,
    ):
        if not llm_services:
            raise ValueError("llm_services must contain at least one LLMService")
        if interaction_manager is None:
            raise ValueError(
                "interaction_manager is required. The agent's confirmation flow "
                "(read/grep/glob/edit/write/shell) routes through it; without one, "
                "every fallback branch returned UserConfirmation.no() and silently "
                "rejected legitimate tool calls. Pass an InteractionManager — use "
                "the production default `InteractionManager()` (no delegate → all "
                "confirmations refuse, which is at least loud) or wire a real one."
            )

        # SessionContext — when supplied, tools pull
        # IM / file_state / ssh_pool / browser_session / session_registry /
        # desktop_state / interrupt_event from it via DI rather than from
        # module-level globals. Callers without a session (tests) pass
        # ``None`` and tools fall back to the module-level state.
        self._ctx: Optional["SessionContext"] = ctx

        # LLM services
        self._services: List[LLMService] = list(llm_services)
        # Size the observation budget to the SMALLEST context window in the
        # fallback chain, not services[0]'s. Any service in the pool may end up
        # serving the request, and the budget is only corrected downward after a
        # stream OPENS successfully (`_update_obs_budget_for_service` via
        # `on_service_selected`) — which never happens when the request is
        # rejected at open. Sizing off services[0] was therefore
        # "shoot first, resize later":
        #
        #   2026-08-03 flash-meta run — agent_models[0] was
        #   `claude-4-6-sonnet:1M` (1M window → budget 2,945,250 chars), so the
        #   agent legitimately grew history to ~1.11M chars. The moment
        #   claude-4-6-sonnet's tokens_per_day quota died, traffic moved to a
        #   200k sibling and every request came back
        #   "Input is too long for requested model" — the payload was 5x the
        #   serving model's window and nothing had ever measured it against
        #   that window. Taking the min makes the first request already fit
        #   every hop, so the 400 cannot happen at all.
        self._obs_budget_chars: int = resolve_obs_budget(
            min(svc.context_window for svc in self._services)
        )
        # Hard ceiling for the life of the agent. `_turns` is state SHARED
        # across every hop of the fallback chain, so the budget may never be
        # raised above what the smallest hop can accept — even while a
        # large-window service is happily serving. Without this ceiling
        # `_update_obs_budget_for_service` would push the budget back up to
        # the 1M model's 2.9M chars on its first successful stream and
        # re-arm the exact failure above.
        self._obs_budget_ceiling: int = self._obs_budget_chars
        # PTL-recovery bookkeeping (see the ladder in the item loop).
        # `_skip_next_compaction` grants exactly one iteration of reprieve so
        # the free request trim can be tried before semantic compaction;
        # `_ptl_backoffs` bounds the budget-backoff arm per item.
        self._skip_next_compaction: bool = False
        self._ptl_backoffs: int = 0

        # Token-based compaction trigger (CC parity). `_last_input_tokens` is
        # the most recent REAL token count from the API response's `usage`
        # (input_tokens + cache_read); updated every turn by _log_context_pressure.
        # Compaction checks this against `_compact_threshold_tokens` (derived from
        # the CC formula: effectiveWindow − outputReserve − margin). Using real
        # token counts instead of char estimates eliminates the instability that
        # chars introduce (chars/token varies by content type — code ≈1.4, prose ≈1.8,
        # JSON ≈2.0 — so a char threshold oscillates depending on what the agent
        # happens to be reading).
        self._context_window_tokens: int = min(
            svc.context_window for svc in self._services
        )
        self._last_input_tokens: int = 0
        # CC formula: compactThreshold = window − outputReserve − 13000
        _output_reserve = min(20_000, self._context_window_tokens // 5)
        self._compact_threshold_tokens: int = (
            self._context_window_tokens - _output_reserve - 13_000
        )
        # Item-boundary fires earlier (80% of compact threshold).
        self._item_boundary_threshold_tokens: int = int(
            self._compact_threshold_tokens * 0.84
        )

        # Conversation state (replaces old _observations/_assistant_messages/_obs_group_sizes).
        # Out-of-band events (background-task completions, context-truncation
        # notices) are persisted into _turns via _persist_event_observations
        # rather than a transient buffer, so they are never shown once and lost.
        self._turns: List[ConversationTurn] = []

        # Tools — when ctx is supplied each tool's __init__ pulls per-session
        # dependencies from it (ShellTool / SessionTool grab interrupt_event;
        # ssh / browser / desktop tools grab their pools and IM ref). Old
        # callers without ctx still work — tools fall back to module globals.
        self.tools: Dict[str, BaseTool] = ToolRegistry.create_all_tool_instances(ctx=ctx)
        self._api_tools: List[Dict[str, Any]] = ToolRegistry.generate_tools_for_api()

        # Wire the spawn_agent / fan_out_agents tools' runtime: both reuse
        # THIS agent's LLM fallback chain to run isolated sub-loops, so their
        # reads/writes never enter this agent's own _turns. Bound here
        # (post-construction) because the tools need live references, not
        # registry metadata.
        #
        # The tool_instances dict passed in is NOT simply self.tools: a
        # sub-agent's fixed tool list (_SUBAGENT_TOOLS) includes tools like
        # `ssh` that are on_demand=True and therefore may not have an
        # instance in self.tools yet if THIS agent hasn't claimed them for
        # itself. A sub-agent's capability must not depend on whether the
        # parent happens to have claimed the same tool — so any
        # _SUBAGENT_TOOLS member missing from self.tools is instantiated
        # here and merged in, once, for the sub-agents' exclusive use. This
        # never adds anything to self.tools / self._api_tools — the PARENT's
        # own visible tool list is untouched.
        from ..tools.spawn_agent_tool import _SUBAGENT_TOOLS
        _sub_tool_instances = dict(self.tools)
        _missing_for_subagents = [
            n for n in _SUBAGENT_TOOLS if n not in _sub_tool_instances
        ]
        if _missing_for_subagents:
            try:
                _sub_tool_instances.update(
                    ToolRegistry.create_all_tool_instances(
                        ctx=ctx, extra_tool_names=_missing_for_subagents,
                    )
                )
            except Exception:
                pass
        for _sub_name in ("spawn_agent", "fan_out_agents"):
            _sub = self.tools.get(_sub_name)
            if _sub is not None and hasattr(_sub, "bind_runtime"):
                try:
                    _sub.bind_runtime(self._services, _sub_tool_instances)
                except Exception:
                    pass
            # bind_context wires PROVIDERS (zero-arg callables), not frozen
            # values — _max_item_iterations / _conversation_summary /
            # _current_item_block all change over this agent's lifetime
            # (new item, compaction), so a sub-agent spawned an hour into a
            # long session must see what the parent knows NOW, not whatever
            # was true at construction time.
            if _sub is not None and hasattr(_sub, "bind_context"):
                try:
                    _sub.bind_context(
                        lambda: self._max_item_iterations,
                        self._render_subagent_context_block,
                    )
                except Exception:
                    pass

        # Legacy interrupt-event post-injection — kept ONLY for ctx=None paths.
        # When ctx is supplied the tools already have the event from their
        # own __init__ pulling ctx.interrupt_event.
        if ctx is None:
            interrupt_event = task_channel._interrupt_event
            if interrupt_event is not None:
                for tool_name in (
                    "shell",
                    "live_shell_open", "live_shell_exec", "live_shell_write",
                    "live_shell_read", "live_shell_list", "live_shell_close",
                ):
                    tool = self.tools.get(tool_name)
                    if tool is not None:
                        tool.interrupt_event = interrupt_event  # type: ignore[attr-defined]

        # Reset file-read tracking for fresh session — only relevant when
        # ctx is None (the module-level FileState singleton survives across
        # sessions). When ctx is supplied, the per-session ``ctx.file_state``
        # IS the fresh tracker — no global reset needed.
        if ctx is None:
            from src.tools.file_state import FileState
            FileState.reset_for_session()

        # Progress / anti-repeat (merged into IterationAdvisor)
        self._advisor = IterationAdvisor()
        # Mechanical completion audit: unresolved background work + verification
        # bullets citing values no tool ever emitted (the 2026-08-01 fabricated
        # flash delivery). Fed alongside _advisor, plus the out-of-band
        # background completion observations in _poll_completed_background_tasks.
        self._completion_audit = CompletionAudit()
        self._conversation_summary: Optional[str] = None
        # CC-parity compaction circuit breakers (claude.exe §3.6).
        #   * consecutive LLM-compaction failures → after N, stop trying the LLM
        #     summarizer for the rest of the session (fall straight to the
        #     rule-based summary), so a persistently-failing summarizer can't burn
        #     a model call on every compaction.
        #   * thrashing → if a compaction is followed within a few turns by
        #     another budget breach, the working set is too large for summarizing
        #     to help; after N such episodes, surface it to the user instead of
        #     silently re-summarizing forever.
        self._compact_consecutive_failures: int = 0
        self._compact_llm_disabled: bool = False
        self._compact_iteration_history: List[int] = []
        self._compact_thrash_episodes: int = 0
        self._compact_thrash_warned: bool = False

        # Session-resume banner (see session_digest.py / flow_controller.py
        # resume path). Set once by the caller right after a resumed session
        # restores its state; consumed and cleared by the very next
        # _build_messages call so it appears exactly once at the top of the
        # first post-resume turn, not on every subsequent turn.
        self._resume_banner: Optional[str] = None

        # Per-item static context — set in _execute_item, consumed by every
        # _build_messages call until the next item begins. Keeps the task
        # instruction / LTM hints visible for the whole multi-iteration item
        # instead of being shown only on the first turn.
        self._current_item_block: Optional[str] = None
        self._current_ltm_block: Optional[str] = None

        # Safety (stateless — no RiskGuard instance needed)
        self.config_manager = config_manager or ConfigManager()
        self.working_directory: Optional[str] = working_directory
        self.storage_directory: str = storage_directory or working_directory or "."
        # Windows GUI mode passes False here so the prompt's environment block
        # only mentions the agent's workspace path. The agent's mental model
        # collapses to "workspace = my world"; the session-storage root
        # (where handq-engine.log / executions_logs/ live) is invisible.
        # Linux CLI mode keeps True — the agent sees both the user's cwd
        # (working_directory) and the session storage as before.
        self._expose_session_storage_in_prompt: bool = expose_session_storage_in_prompt

        # Metadata
        # Session-wide monotonic turn counter (NOT per-item). Incremented once
        # per LLM turn for the lifetime of the agent. Used for:
        #   - log line disambiguation across items ([42][Act] ...)
        #   - ExecutionRecorder.write_turn's `turn` field, paired with
        #     step_id so records are unique within a session
        #   - InteractionManager turn IDs visible to UI
        # Per-item iteration count lives in `_item_loop`'s local `iteration`.
        self.current_iteration: int = 0
        self._current_item_turn_count: int = 0
        self.logger = get_logger()

        # Per-build scratch for the ExecutionRecorder's incremental trace:
        # `_last_build_retiered` carries the observation-elision events from
        # this build's _microcompact_old_outputs pass (what got compressed
        # this turn); `_last_build_totals` carries the total context size.
        # Both read at the write_turn call site. Reset each build.
        self._last_build_retiered: List[Dict[str, Any]] = []
        self._last_build_totals: Dict[str, Any] = {}
        # Fires once per session: the first turn's full, untruncated message
        # list (system + skill prelude + turn trace + tools) is snapshotted
        # to ExecutionRecorder before any content could have been elided or
        # prefix-cached away — the one moment the "complete picture" the
        # incremental per-turn `appended` records don't carry on their own.
        self._first_request_logged: bool = False

        # Recorder / UI / TaskChannel
        self.execution_recorder: Optional[ExecutionRecorder] = execution_recorder
        self._interaction_manager: InteractionManager = interaction_manager

        self._task_channel = task_channel
        self._max_item_iterations = max_item_iterations
        # Item counters split by outcome so logs/metrics distinguish
        # "we ran N items" from "M of them succeeded".
        self._total_items_processed = 0
        self._total_items_succeeded = 0
        self._total_items_failed = 0

        # Skills / tools injection tracking
        self._loaded_tools: Set[str] = set()
        # Tools the agent has explicitly released for visibility purposes.
        # Loaded resources stay warm — see _apply_self_extension /
        # _regenerate_api_tools. Re-claiming a hidden tool clears it from
        # this set immediately when the claim is processed (right after the
        # turn that issued it finishes) — no re-instantiation needed, just a
        # set removal — but the tool only reappears in self._api_tools, and
        # so becomes callable, starting the NEXT turn's request.
        self._hidden_tools: Set[str] = set()

        # Bundled ``*-workflow`` skills already reverse-pushed this session
        # (see _apply_self_extension). Push-once guard: claiming more tools of
        # a family whose workflow skill is already in context must not re-inject
        # the body. Keyed by skill name.
        self._pushed_workflow_skills: Set[str] = set()

        # Skills follow the progressive-disclosure model (mirrors Claude Code):
        # there is no per-session skill injection state here. Every turn,
        # _build_messages renders the [Available Skills] menu + [Standing
        # Skills] bodies LIVE from the SkillRegistry singleton (see
        # _render_skill_prelude), so panel toggles take effect immediately and
        # the agent pulls non-standing skill bodies on demand via read_skill.

        # Subscribe to task-channel state changes (tools only — skills are no
        # longer pushed; see _render_skill_prelude).
        self._task_channel.on_tools_changed(self._handle_tools_added)

        # Build system prompt with environment info (static for session lifetime)
        env_parts = []
        if self.working_directory:
            env_parts.append(f"Working directory: {self.working_directory}")
        if self._expose_session_storage_in_prompt:
            env_parts.append(f"Session storage directory: {self.storage_directory}")
        else:
            # Windows GUI mode: agent only knows its workspace path. The
            # session-storage root and the History\<session>\ parent never
            # appear in the prompt. Bias the agent to keep all artifacts
            # inside the named workspace.
            env_parts.append(
                "All file deliverables you produce belong inside Working "
                "directory. Do not write outside it unless the user message "
                "explicitly names an absolute path. The one exception is the "
                "short-lived scratch/cache files described under 'Cache "
                "Discovery Results', which may go to your OS temp directory."
            )
        env_parts.append(get_platform_context())
        # Input-injection capability, probed ONCE at session start. See
        # desktop_tool.probe_input_injection for why this is worth a line of
        # prompt: without it the agent can spend an hour optimizing pointer
        # coordinates on a session that has no interactive desktop at all.
        try:
            from ..tools.desktop_tool import probe_input_injection
            _probe = probe_input_injection()
            if not _probe.get("available", True):
                env_parts.append(
                    "GUI INPUT INJECTION IS UNAVAILABLE in this session: "
                    f"{_probe.get('detail', 'no foreground window')}. "
                    "Mouse/keyboard injection (desktop_click_at, "
                    "desktop_find_and_click, desktop_type_text, drag, scroll, "
                    "hotkey, and any SendInput / PostMessage / "
                    "SetForegroundWindow trick you might write yourself) will "
                    "NOT work here, and no amount of re-aiming, maximizing or "
                    "re-trying will change that — it is a property of the "
                    "session, not of your coordinates. Do not spend turns on "
                    "it. Reach the application another way: its own automation "
                    "API or SDK, a CLI, a config file, Chrome DevTools Protocol "
                    "for Electron/Chromium apps, or ask the user to perform the "
                    "one click you cannot. desktop_screenshot and "
                    "desktop_list_windows still work for OBSERVATION."
                )
            else:
                env_parts.append(
                    f"GUI input injection: available ({_probe.get('detail', '')})."
                )
        except Exception:
            pass
        if self._services:
            env_parts.append(f"You are powered by the model: {self._services[0].model}")
        # Sent as TWO separate system blocks (not one concatenated string) so
        # the send layer (_build_api_kwargs) can cache-anchor the stable
        # identity/behavior block independently of the Environment block,
        # which is session-static but changes more often across deployments
        # than the core prompt does. Mirrors Claude Code's practice of
        # keeping the system prompt as named, independently-cacheable
        # sections rather than one opaque string.
        self._system_prompt_core = AGENT_SYSTEM_PROMPT
        self._system_prompt_env = "## Environment\n\n" + "\n".join(env_parts)

    # ── Public API ───────────────────────────────────────────────────────────

    def export_summary(self) -> Optional[str]:
        """Return the agent's current compaction summary, for a SessionDigest
        checkpoint. Read-only — does not clear or mutate anything."""
        return self._conversation_summary

    def restore_summary(self, summary: Optional[str]) -> None:
        """Reinstate a previously exported summary on a resumed session.

        This is the one piece of "belief" a resume carries over (see
        docs/session_resume_design.md §6.5) — it's the same summary the
        agent was already operating on at close time, not a fresh
        re-narration, so its provenance/quality is unchanged.
        """
        self._conversation_summary = summary

    def set_resume_banner(self, text: str) -> None:
        """Arm a one-shot banner to be woven into the very next
        _build_messages call, then cleared (see _resume_banner docstring
        at __init__)."""
        self._resume_banner = text

    async def run_loop(self) -> None:
        """Run the persistent loop. Blocks forever until CancelledError."""
        self.logger.info("Persistent agent loop started", component="PersistentAgent")
        try:
            while True:
                item = await self._task_channel.wait_for_current_item()
                await self._execute_item(item)
        except asyncio.CancelledError:
            self.logger.info(
                f"Persistent agent loop cancelled. "
                f"Items: processed={self._total_items_processed}, "
                f"succeeded={self._total_items_succeeded}, "
                f"failed={self._total_items_failed}",
                component="PersistentAgent",
            )
            raise

    # ── Observation helpers ──────────────────────────────────────────────────

    def _log_observations(self, results: List[ToolResult]) -> None:
        """Log tool results for diagnostics."""
        for obs in results:
            # On failure, .output is normally None and the real diagnostic
            # text lives in .error — logging str(obs.output) alone renders
            # every failure as the same uninformative "Output(len=4): None"
            # regardless of the actual cause. Confirmed to cost real
            # debugging time (had to be re-derived from ExecutionRecorder
            # traces instead of these logs) during live E2E benchmarking.
            payload = obs.output if obs.success else (obs.error or obs.output)
            output_str = str(payload)
            self.logger.info(
                f"[{self.current_iteration}][Observe] Tool={obs.tool_name}, "
                f"Success={obs.success}, Output(len={len(output_str)}): {output_str[:500]}...",
                component="PersistentAgent",
            )

    # ── Per-item execution ───────────────────────────────────────────────────

    async def _execute_item(self, item: TaskSpec) -> None:
        """Execute a single task item with full feature parity."""
        self._advisor.reset_for_item()
        # Seed the audit's sourced-token set with the item instruction so
        # identifiers the USER supplied stay quotable in verification.
        self._completion_audit.reset_for_item(item.instruction or "")
        self._current_item_turn_count = 0

        # Item-static context — persisted until the next item begins.
        # _build_messages reads these every turn so the agent never loses
        # its task instruction or LTM recall partway through a multi-iteration item.
        self._current_item_block = item.to_agent_message()
        # LTM block is precomputed by the Coordinator (PRECISE tier,
        # rerank=True) concurrently with the INTENT call that queued this
        # item — see Orchestrator._build_precise_long_term_block /
        # TaskSpec.ltm_block. The Agent does not run its own recall: the
        # instruction here is near-identical to what the Coordinator already
        # reranked against, so a second recall would just repeat that work
        # on the Agent's own critical path for no new information.
        self._current_ltm_block = item.ltm_block
        if self._current_ltm_block:
            self._emit_recall_summary(self._current_ltm_block)

        if self.execution_recorder:
            self.execution_recorder.write_agent_start(
                step_id=item.item_id,
                goal=item.instruction,
                active_tools=sorted(self._loaded_tools),
                skills_required=self._standing_skill_names(),
            )

        # Open a file-checkpoint window for this item so write/edit captures
        # land under item.item_id and a user can later undo this task's file
        # changes (RewindStore, Tier-1.3). No-op without a session store.
        rewind = getattr(self._ctx, "rewind_store", None) if self._ctx else None
        if rewind is not None:
            try:
                rewind.begin_item(item.item_id)
            except Exception:
                pass

        try:
            self._interaction_manager.notify_state_changed("executing")
        except Exception:
            pass

        item_result = await self._item_loop(item)

        # Close the checkpoint window: snapshot the post-item state of every
        # touched path for later external-modification detection at undo time.
        if rewind is not None:
            try:
                rewind.end_item()
            except Exception:
                pass

        # Item boundary: drop any stale progress concern so a judgment from THIS
        # item cannot bleed into the next one.
        self._task_channel.clear_progress_concern()
        self._total_items_processed += 1
        if item_result.success:
            self._total_items_succeeded += 1
        else:
            self._total_items_failed += 1

        # Record agent end
        if self.execution_recorder:
            self.execution_recorder.write_agent_end(
                step_id=item.item_id,
                success=item_result.success,
                goal=item.instruction,
                verification=item_result.verification,
                artifacts=item_result.artifacts,
                key_findings=item_result.key_findings,
                issues=item_result.issues,
                final_answer=item_result.final_answer,
            )

        self._task_channel.mark_current_done(item_result)
        self.logger.info(
            f"[PersistentAgent] Item '{item.item_id}' done "
            f"(success={item_result.success}, iters={item_result.iterations})",
            component="PersistentAgent",
        )
        # Item boundary: the completed item's raw turns are now dead weight
        # (ItemResult carries everything cross-item), so fold them into the
        # rolling summary instead of letting _turns grow until byte pressure.
        await self._compact_item_boundary()

    async def _item_loop(self, item: TaskSpec) -> TaskResult:
        """Per-item OTA loop."""
        instruction = item.instruction
        # Persist the task verbatim before doing anything with it, so the exact
        # original wording survives compaction and is recoverable by `read`.
        # Skip mechanical re-queues (standing-goal check-ins, schedule_wakeup
        # ticks) — they replay the SAME instruction text the user never
        # re-typed, so logging them here would fill this "verbatim user
        # instructions" log with dozens of duplicate TASK entries over a long
        # check-in/wakeup loop instead of the one real user message that
        # started it.
        if item.goal_iteration is None and item.wakeup_iteration is None:
            self._append_instruction_log(
                instruction, kind="TASK", item_id=item.item_id,
            )
        iteration = 0
        _tools_used: list = []
        # Bookkeeping-only tools (planning, reading a recipe, adjusting the tool
        # list) never touch the world and must not satisfy the speculative-
        # completion guard below. Canonical set lives in agent_utils
        # (BOOKKEEPING_ONLY_TOOLS) so the online guard and the offline laziness
        # analyzer stay in lock-step. Confirmed live (2026-07-14): an item that
        # called claim_tool + 3x todo_write and NOTHING else was accepted as
        # success — and a 2026-07-17 trace repeated it via read_skill+claim_tool
        # because claim_tool/release_tool were originally missing from this set.
        _grounding_tools_used: list = []
        # Absolute paths of files actually written/edited this item, collected
        # from the write/edit tool OUTPUTS (str(path.absolute())) — NOT from the
        # LLM's self-reported `artifacts`, which it tends to fill with bare
        # relative names the user can't locate. This is the authoritative
        # artifact list surfaced in the completion summary.
        _produced_paths: list = []
        _produced_seen: set = set()
        _token_usage = TokenUsage()

        # Fail-open backstop for the completion guards below. Each time a
        # completion is REJECTED (format violation or speculative/no-grounding)
        # we increment this; past the cap we stop rejecting and let the item
        # end as failed. Rationale: a guard that returns the SAME rejection to
        # the SAME model state can loop until the 999-iteration cap — the
        # 2026-07-23 "stop now" trace spun 186 times / ~79 min this way, because
        # an interrupt-spawned "stop now" item has no world-work to satisfy the
        # guard and the model just re-emits its summary forever. This cap makes
        # any such no-exit loop terminate in seconds regardless of WHY the item
        # is unsatisfiable (deferred hallucination, bad intent, future paths) —
        # it is a safety net, independent of upstream correctness.
        _completion_guard_rejections = 0
        _COMPLETION_GUARD_MAX_REJECTIONS = 3
        # Fresh budget-backoff allowance per item: a shrink forced by one
        # oversized item should not deny the next item its own cheap retries.
        self._ptl_backoffs = 0

        while iteration < self._max_item_iterations:
            iteration += 1
            self.current_iteration += 1

            # ── 0. Interrupt check ───────────────────────────────────────────
            if self._task_channel.check_interrupt():
                interrupt_reason = self._task_channel.acknowledge_interrupt()
                self.logger.info(
                    f"[{iteration}][Interrupt] Coordinator interrupt for item={item.item_id}"
                    f"{f': {interrupt_reason}' if interrupt_reason else ''}",
                    component="PersistentAgent",
                )
                issue_msg = INTERRUPTED_BY_COORDINATOR
                if interrupt_reason:
                    issue_msg = f"{INTERRUPTED_BY_COORDINATOR}: {interrupt_reason}"
                return TaskResult(
                    item_id=item.item_id,
                    success=False,
                    issues=[issue_msg],
                    artifacts=list(_produced_paths),
                    iterations=iteration,
                    token_usage=_token_usage,
                )

            # ── 1. Background + Compact ─────────────────────────────────────
            self._poll_completed_background_tasks()
            self._drain_pending_file_notices()
            # One-iteration reprieve granted by the PTL ladder below: it just
            # shrank the budget and wants the FREE, non-destructive request
            # trim (_budget_enforced_turns) to get its chance before semantic
            # compaction is allowed to collapse _turns.
            if self._skip_next_compaction:
                self._skip_next_compaction = False
            else:
                await self._compact_conversation()

            # ── 2. Reminders (via IterationAdvisor) ────────────────────────
            reminder = self._advisor.get_reminder()

            # ── 3. Think + Act ───────────────────────────────────────────────
            turn_result, tool_results, _iter_token_usage = await self._think_streaming(
                instruction, reminder
            )
            _token_usage += _iter_token_usage

            # Apply agent-driven self-extension (claim_tool / release_tool)
            # BEFORE the per-turn branching. Claim makes the tool available
            # for THIS iteration's tool calls when re-claiming an already-
            # loaded hidden tool (instance is warm); release hides on the
            # NEXT prompt build. Brand-new claims arrive on the next turn.
            if turn_result.claim_tool or turn_result.release_tool:
                self._apply_self_extension(
                    turn_result.claim_tool, turn_result.release_tool,
                )

            # ── 4. Error handling (PTL recovery) ─────────────────────────────
            if turn_result.is_error:
                err_msg = turn_result.error or ""
                # Mid-stream user interrupt — surface it as an interrupt exit,
                # not as an LLM API error. The stream handler sets this
                # sentinel INSTEAD of leaving error=None (which would trip
                # is_completion and mark the item succeeded with
                # final_answer='LLM stream interrupted'). Ack the coordinator
                # flag exactly the way the step-0 interrupt check does, then
                # return the same INTERRUPTED_BY_COORDINATOR TaskResult shape.
                if err_msg == INTERRUPTED_BY_USER_MID_STREAM:
                    interrupt_reason = self._task_channel.acknowledge_interrupt()
                    self.logger.info(
                        f"[{iteration}][Interrupt] Mid-stream interrupt for item={item.item_id}"
                        f"{f': {interrupt_reason}' if interrupt_reason else ''}",
                        component="PersistentAgent",
                    )
                    issue_msg = INTERRUPTED_BY_COORDINATOR
                    if interrupt_reason:
                        issue_msg = f"{INTERRUPTED_BY_COORDINATOR}: {interrupt_reason}"
                    return TaskResult(
                        item_id=item.item_id,
                        success=False,
                        issues=[issue_msg],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )
                if err_msg.startswith(LLM_API_ERROR_TAG):
                    raw_error = err_msg[len(LLM_API_ERROR_TAG) + 1:].strip()

                    if raw_error.startswith("PTL:"):
                        # Step 0 — CHEAP, NON-DESTRUCTIVE, and tried first.
                        # Back the observation budget off by a step and retry.
                        # `_build_messages` already runs
                        # `_budget_enforced_turns()`, which drops oldest turns
                        # from THIS REQUEST while `_turns` stays intact:
                        # milliseconds, no LLM call, no history lost.
                        #
                        # Reaching a PTL at all now means the char budget
                        # under-measured the real token cost (chars/token
                        # miscalibration, unmeasured tool-schema bulk, a grown
                        # summary/prelude) — so stepping the budget down is
                        # exactly the right correction, and it is the one the
                        # ladder previously skipped.
                        #
                        # 2026-08-03: the ladder went straight to
                        # `_compact_conversation()` and spent 95s collapsing
                        # 234 turns -> 5 (summary capped at 11,325 chars) at
                        # turn 234 of a 248-turn run.
                        if (
                            self._ptl_backoffs < self._PTL_MAX_BACKOFFS
                            and self._obs_budget_chars > self._PTL_BUDGET_FLOOR_CHARS
                        ):
                            new_budget = max(
                                int(self._obs_budget_chars * self._PTL_BACKOFF_FACTOR),
                                self._PTL_BUDGET_FLOOR_CHARS,
                            )
                            self._ptl_backoffs += 1
                            self.logger.info(
                                f"[{iteration}] PTL — obs budget backoff "
                                f"{self._ptl_backoffs}/{self._PTL_MAX_BACKOFFS}: "
                                f"{self._obs_budget_chars:,} -> {new_budget:,} chars; "
                                f"retrying with a free request trim (history kept).",
                                component="PersistentAgent",
                            )
                            self._obs_budget_chars = new_budget
                            self._skip_next_compaction = True
                            continue

                        turns_before = len(self._turns)
                        await self._compact_conversation()
                        if len(self._turns) < turns_before:
                            self.logger.info(
                                f"[{iteration}] Semantic compaction "
                                f"{turns_before} -> {len(self._turns)} turns; retrying.",
                                component="PersistentAgent",
                            )
                            continue

                        # Hard drop by bytes with summary preservation
                        dropped = self._hard_drop_turns()
                        if dropped > 0:
                            self.logger.warning(
                                f"[{iteration}] Hard-drop: {dropped} turns removed.",
                                component="PersistentAgent",
                            )
                            continue

                        # Drop LTM block (regenerable, lowest-value context)
                        if self._drop_current_ltm_block() > 0:
                            continue

                        # Last resort: elide the oldest turn's observations
                        # in place (never drop list entries — that desyncs
                        # from assistant_message["tool_calls"] and produces
                        # mismatched/missing tool_results, a 400 with the
                        # Anthropic API). Falls through to give up once
                        # nothing new gets superseded.
                        turn0 = self._turns[0] if self._turns else None
                        if turn0 and len(turn0.observations) > 1:
                            newly_superseded = 0
                            for obs in turn0.observations[:-1]:
                                if obs.superseded_note is None:
                                    obs.superseded_note = (
                                        "[content dropped — prompt was too "
                                        "long even after compaction]"
                                    )
                                    newly_superseded += 1
                            if newly_superseded > 0:
                                continue

                        raw_error = raw_error[len("PTL:"):].strip()

                    self.logger.error(
                        f"[{iteration}] LLM API error: {raw_error[:200]}",
                        component="PersistentAgent",
                    )
                    return TaskResult(
                        item_id=item.item_id,
                        success=False,
                        issues=[f"LLM API error: {raw_error[:300]}"],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )

                self.logger.error(
                    f"[{iteration}] Turn error: {turn_result.error}",
                    component="PersistentAgent",
                )
                err_text = turn_result.error or ""
                return TaskResult(
                    item_id=item.item_id,
                    success=False,
                    issues=[f"Error: {err_text[:300]}"],
                    plan_feedback=turn_result.plan_feedback or "",
                    iterations=iteration,
                    token_usage=_token_usage,
                )

            # ── 5. Completion (no tool calls) ────────────────────────────────
            if turn_result.is_completion:
                # Fail-open backstop: if the completion guards below have
                # already rejected this item too many times, stop rejecting.
                # A guard that keeps returning the same rejection to the same
                # model state never converges (the 2026-07-23 "stop now" spin);
                # ending the item as failed here bounds that to a few turns
                # instead of ~999. Independent of WHY the item is
                # unsatisfiable, so it also covers future no-exit paths.
                if _completion_guard_rejections >= _COMPLETION_GUARD_MAX_REJECTIONS:
                    self.logger.warning(
                        f"[{iteration}] Completion guard rejected this item "
                        f"{_completion_guard_rejections}x — failing open to break "
                        f"the retry loop.",
                        component="PersistentAgent",
                    )
                    return TaskResult(
                        item_id=item.item_id,
                        success=False,
                        issues=[
                            "Could not complete with tool-grounded evidence "
                            f"after {_completion_guard_rejections} completion-guard "
                            "rejections — ending to avoid a no-progress loop. This "
                            "usually means the item had no actionable world-work "
                            "(e.g. a bare stop/cancel directive)."
                        ],
                        final_answer=(turn_result.final_answer or turn_result.reasoning or "").strip(),
                        iterations=iteration,
                        token_usage=_token_usage,
                    )
                if tool_results:
                    stream_errors = [tr.error for tr in tool_results if tr.error]
                    error_summary = "; ".join(stream_errors) if stream_errors else "LLM stream interrupted"
                    self.logger.warning(
                        f"[{iteration}] Stream error masked as completion: {error_summary}",
                        component="PersistentAgent",
                    )
                    return TaskResult(
                        item_id=item.item_id,
                        success=False,
                        issues=[f"Stream error: {error_summary[:300]}"],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )

                # Completion-format guard: the LLM ignored the JSON contract
                # and returned pure prose (no dict with `reasoning`). Reject
                # like the speculative-completion guard below: inject a
                # corrective observation reminding the LLM of the schema and
                # of the "user-facing content → `answer`, not free prose"
                # rule, then loop. Empirically this happens after an
                # interrupt reframes the item as "just summarise what you
                # have" and the LLM dumps markdown; the corrective turn lets
                # it re-emit a proper JSON completion.
                if turn_result.format_violation:
                    self.logger.warning(
                        f"[{iteration}] Completion format violation rejected: "
                        f"LLM returned non-JSON prose. Forcing retry.",
                        component="PersistentAgent",
                    )
                    # Synthetic tool-result carrying the guard's rejection
                    # reason — used both to record this turn in the JSONL trace
                    # (so a rejected iter3-style completion is not invisible)
                    # and to seed the model's next-turn prompt via
                    # _persist_event_observations. The `reasoning` on
                    # turn_result already carries the full raw markdown the
                    # model attempted (from_completion_text no longer
                    # truncates), so the JSONL turn record captures what the
                    # model actually wrote.
                    guard_obs = ToolResult(
                        success=False, output=None,
                        tool_name="completion_guard", tool_parameters={},
                        error=(
                            "Completion output was not a JSON object with a "
                            "`reasoning` key. Re-emit the completion in the "
                            "documented envelope: "
                            "{\"reasoning\": \"internal thought\", "
                            "\"final_answer\": \"the user-facing answer in markdown\", "
                            "\"verification\": [\"short mechanical bullets\"], "
                            "\"artifacts\": [\"file paths — only if the user "
                            "asked for a file\"], "
                            "\"key_findings\": [\"discrete facts\"]}. "
                            "The prose you just emitted is the ANSWER — it "
                            "belongs in `final_answer`, not as raw markdown. "
                            "`verification` bullets are <30 words each and "
                            "describe what tools verified. `artifacts` is "
                            "ONLY for files the user explicitly asked for."
                        ),
                    )
                    if self.execution_recorder:
                        self.execution_recorder.write_turn(
                            turn=self.current_iteration,
                            step_id=item.item_id,
                            decision=turn_result,
                            tool_results=[guard_obs],
                            token_usage=_iter_token_usage,
                            retiered=self._last_build_retiered,
                            totals=self._last_build_totals,
                        )
                    self._persist_event_observations(
                        [guard_obs],
                        note=(
                            "(My last completion was raw markdown, not the "
                            "JSON envelope. The user-facing content belongs "
                            "in the `final_answer` field, not as free prose. "
                            "Re-emitting with the correct schema now.)"
                        ),
                    )
                    _completion_guard_rejections += 1
                    continue

                # Speculative-completion guard: an item that claims success
                # without ever dispatching a WORLD-TOUCHING tool is almost
                # always the LLM composing prose in lieu of action — the
                # Coordinator only queues genuine world-work, so a completion
                # backed by nothing but planning/reading (todo_write,
                # read_skill) needs the same rejection as zero tool calls.
                # This also catches "claimed a tool, never called it" — the
                # exact failure confirmed live for schedule_wakeup and
                # live_shell_open/exec/close (claim_tool + todo_write only,
                # then a completion claiming the claimed tool's job was done).
                if not _grounding_tools_used:
                    self.logger.warning(
                        f"[{iteration}] Speculative completion rejected: "
                        f"{len(_tools_used)} tool call(s), none world-touching. "
                        f"Forcing retry.",
                        component="PersistentAgent",
                    )
                    # Same JSONL-capture pattern as the format_violation guard
                    # above — record the rejected turn so the trace shows the
                    # model's attempt + why it was rejected, before continuing.
                    guard_obs = ToolResult(
                        success=False, output=None,
                        tool_name="completion_guard", tool_parameters={},
                        error=(
                            "Item declared complete without calling any tool "
                            "that actually does the task's work — claim_tool / "
                            "todo_write / read_skill alone are not evidence. "
                            "If you claimed a tool, this is the turn to actually "
                            "call it. Completion requires tool-grounded evidence. "
                            "If a required tool is unavailable in your tool list, "
                            "return an error JSON naming the missing tool — do "
                            "not synthesise a free-form `final_answer` or "
                            "`verification`."
                        ),
                    )
                    if self.execution_recorder:
                        self.execution_recorder.write_turn(
                            turn=self.current_iteration,
                            step_id=item.item_id,
                            decision=turn_result,
                            tool_results=[guard_obs],
                            token_usage=_iter_token_usage,
                            retiered=self._last_build_retiered,
                            totals=self._last_build_totals,
                        )
                    self._persist_event_observations(
                        [guard_obs],
                        note=(
                            "(I declared the item complete without calling any "
                            "tool that does real work — the completion-guard "
                            "rejected this. If I claimed a tool, I must call it "
                            "now, not just report having claimed it.)"
                        ),
                    )
                    _completion_guard_rejections += 1
                    continue

                # Completion-audit guard: the completion is well-formed AND
                # backed by real tool calls, but its CONTENT is not observable.
                # Two mechanical checks (see agent_utils.CompletionAudit):
                #   * a background task launched this item was never once seen
                #     in a terminal state — "still running" is not "succeeded";
                #   * a `verification` bullet quotes a percentage / timestamp /
                #     SCREAMING_SNAKE status that appears in no tool output and
                #     in nothing the user supplied — i.e. it was invented.
                # The 2026-08-01 flash trace passed both existing guards and
                # shipped "DEVICE_NO_ERROR (0), progress 4.96%->99.94% at
                # 13:37:37" with ten background tasks still marked running.
                # Bounded by the same _COMPLETION_GUARD_MAX_REJECTIONS cap, so
                # a false positive costs a few corrective turns, never a spin.
                _audit_failure = self._completion_audit.audit(
                    turn_result.verification
                )
                if _audit_failure:
                    self.logger.warning(
                        f"[{iteration}] Completion audit rejected: "
                        f"{_audit_failure[:300]}",
                        component="PersistentAgent",
                    )
                    guard_obs = ToolResult(
                        success=False, output=None,
                        tool_name="completion_guard", tool_parameters={},
                        error=_audit_failure,
                    )
                    if self.execution_recorder:
                        self.execution_recorder.write_turn(
                            turn=self.current_iteration,
                            step_id=item.item_id,
                            decision=turn_result,
                            tool_results=[guard_obs],
                            token_usage=_iter_token_usage,
                            retiered=self._last_build_retiered,
                            totals=self._last_build_totals,
                        )
                    self._persist_event_observations(
                        [guard_obs],
                        note=(
                            "(My completion was rejected by the audit: I either "
                            "left background work unobserved or cited a value no "
                            "tool actually reported. I must observe the real "
                            "result or drop the unsourced claim — not restate an "
                            "inference as an observation.)"
                        ),
                    )
                    _completion_guard_rejections += 1
                    continue

                self.logger.info(
                    f"[{iteration}] Item complete: {turn_result.reasoning[:100]}",
                    component="PersistentAgent",
                )
                _issues: List[str] = []
                if turn_result.truncation_note:
                    self.logger.warning(
                        f"[{iteration}] Completion truncation detected: "
                        f"{turn_result.truncation_note}",
                        component="PersistentAgent",
                    )
                    _issues.append(turn_result.truncation_note)
                return TaskResult(
                    item_id=item.item_id,
                    success=True,
                    verification=list(turn_result.verification or []),
                    artifacts=self._reconcile_artifacts(
                        _produced_paths, turn_result.artifacts
                    ),
                    key_findings=list(turn_result.key_findings or []),
                    # Defensive fallback: if the model emitted a valid completion
                    # JSON but left `final_answer` empty, use `reasoning` so the
                    # chat bubble isn't blank. A well-formed completion should
                    # always fill `final_answer` per the prompt, but this belt-
                    # and-suspenders keeps the UX from silently degrading when
                    # it doesn't.
                    final_answer=(
                        (turn_result.final_answer or "").strip()
                        or (turn_result.reasoning or "").strip()
                    ),
                    issues=_issues,
                    plan_feedback=turn_result.plan_feedback or "",
                    iterations=iteration,
                    token_usage=_token_usage,
                )

            # ── 6. Record tool results ───────────────────────────────────────
            prev_artifacts = len(_produced_paths)
            for tr in tool_results:
                if tr.tool_name:
                    params = tr.tool_parameters or {}
                    tool_nm = tr.tool_name
                    if tool_nm in ("bash", "shell"):
                        primary_input = params.get("command")
                    elif tool_nm in ("read", "write", "edit"):
                        primary_input = params.get("path")
                    elif tool_nm == "ssh":
                        action = params.get("action", "")
                        command = params.get("command", "")
                        remote_path = params.get("remote_path", "")
                        if command:
                            _detail = command[:100] + "..." if len(command) > 100 else command
                            primary_input = f"{action}: {_detail}" if action else _detail
                        elif remote_path:
                            _detail = remote_path[:100] + "..." if len(remote_path) > 100 else remote_path
                            primary_input = f"{action}: {_detail}" if action else _detail
                        else:
                            primary_input = action or (next(iter(params.values()), None) if params else None)
                    else:
                        primary_input = next(iter(params.values()), None) if params else None
                    _tools_used.append(format_tool_entry(tool_nm, primary_input))
                    if tool_nm not in BOOKKEEPING_ONLY_TOOLS:
                        _grounding_tools_used.append(tool_nm)

                    # Capture the authoritative absolute path from successful
                    # write/edit outputs (str(path.absolute())) for the artifact
                    # list, instead of trusting the LLM's self-reported names.
                    if (
                        tool_nm in ("write", "edit")
                        and tr.success
                        and isinstance(tr.output, dict)
                    ):
                        abs_path = tr.output.get("path")
                        if abs_path and abs_path not in _produced_seen:
                            _produced_paths.append(abs_path)
                            _produced_seen.add(abs_path)

            if self.execution_recorder and tool_results:
                # One incremental record per turn (not per tool_result): the
                # assistant message + all observations of this turn, rendered
                # as sent to the LLM, plus the tier changes and context totals
                # captured during this turn's _build_messages.
                self.execution_recorder.write_turn(
                    turn=self.current_iteration,
                    step_id=item.item_id,
                    decision=turn_result,
                    tool_results=tool_results,
                    token_usage=_iter_token_usage,
                    retiered=self._last_build_retiered,
                    totals=self._last_build_totals,
                )

            # ── 7. USER_NEW_INSTRUCTION propagation ──────────────────────────
            for tr in tool_results:
                if tr.error == "USER_NEW_INSTRUCTION":
                    return TaskResult(
                        item_id=item.item_id,
                        success=False,
                        issues=["User new instruction"],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )

            # ── 7b. Tool-call format violation (blank reasoning) ─────────────
            # Structural mirror of the completion format_violation guard above.
            # Tools already executed (their results are real facts, kept as-is
            # — discarding them would hide genuine environment feedback), but a
            # blank `reasoning` on a tool-call turn breaks the response-format
            # contract, so it's flagged the same way: log + a corrective note
            # for the model's next turn. No content requirement — the model
            # picks what to say.
            if turn_result.format_violation and turn_result.tool_calls:
                self.logger.warning(
                    f"[{iteration}] Tool-call format violation: blank "
                    f"reasoning. Corrective note injected.",
                    component="PersistentAgent",
                )
                self._persist_event_observations(
                    [ToolResult(
                        success=False, output=None,
                        tool_name="completion_guard", tool_parameters={},
                        error=(
                            "Format violation: the previous turn called tool(s) "
                            "with no reasoning text. Every turn must state why "
                            "before or alongside its tool call(s)."
                        ),
                    )],
                    note="(My last turn called tools with blank reasoning — noted for next turn.)",
                )

            # ── 8. Update advisor + per-turn progress signal ─────────────────
            has_wait_interval = False
            for tr in tool_results:
                self._advisor.record_tool_result(tr)
                self._completion_audit.record_tool_result(tr)
                if tr.tool_name == "wait_interval" and tr.success:
                    has_wait_interval = True
            info_gain = self._advisor.record_progress_signal(
                has_wait_interval=has_wait_interval,
            )

            # Mechanical progress-concern recording (the incident fix): when
            # either mechanical signal crosses its HARD threshold, record a
            # concern DIRECTLY on the task-channel bus — no LLM in the path,
            # no dependency on a helper pool. hard_stall() is cooldown-gated
            # so this fires at most once per stall episode. Guarded by the
            # item_id staleness check for parity with the watcher path.
            if self._advisor.hard_stall():
                current = self._task_channel.get_current_item()
                if current is not None and current.item_id == item.item_id:
                    self._task_channel.set_progress_concern(ProgressConcern(
                        item_id=item.item_id,
                        verdict="stalled",
                        rationale=(
                            "mechanical hard stall — crossed the no-progress / "
                            "consecutive-failure / redundant-repeat threshold "
                            "without adding new information; approach likely "
                            "needs re-planning or the item is already done"
                        ),
                        suggest_replan=True,
                    ))

            # ── 9. Mechanical digest ────────────────────────────────────────
            # Write one bus digest per turn (cheap, synchronous) so the
            # coordinator has an in-flight view of this running item.
            self._task_channel.append_turn_digest(TurnDigest(
                item_id=item.item_id,
                iteration=iteration,
                tool_names=[tr.tool_name for tr in tool_results if tr.tool_name],
                success_count=sum(1 for tr in tool_results if tr.success),
                fail_count=sum(1 for tr in tool_results if not tr.success),
                produced_new_artifact=len(_produced_paths) > prev_artifacts,
                info_gain=info_gain,
                no_progress_streak=self._advisor.no_info_gain_streak,
            ))

        advisor_summary = self._advisor.get_summary()
        self.logger.warning(
            f"Item '{item.item_id}' hit iteration cap ({self._max_item_iterations}). "
            f"Success rate: {advisor_summary['success_rate']:.1%}",
            component="PersistentAgent",
        )
        return TaskResult(
            item_id=item.item_id,
            success=False,
            issues=[f"Reached per-item iteration cap ({self._max_item_iterations})"],
            iterations=iteration,
            token_usage=_token_usage,
        )

    # ── LLM interaction ──────────────────────────────────────────────────────

    async def _think_streaming(
        self,
        instruction: str,
        reminder: Optional[str],
    ) -> tuple[TurnOutcome, List[ToolResult], TokenUsage]:
        """Streaming Think+Act: open stream, dispatch tools as they arrive."""
        messages = self._build_messages(instruction, reminder)

        if not self._first_request_logged and self.execution_recorder:
            self._first_request_logged = True
            self.execution_recorder.write_first_request_snapshot(
                messages=messages, tools=self._api_tools,
            )

        try:
            self._interaction_manager.notify_state_changed("thinking")
        except Exception:
            pass

        chat_kwargs = dict(
            messages=messages, tools=self._api_tools, json_mode=False,
            effort="xhigh",
            # Pair effort with an explicit thinking budget: without this, the
            # YOUR-AI-ENDPOINT/Bedrock gateway silently suppresses thinking_delta events
            # in streaming mode (verified 2026-07-16 — same request returns a
            # thinking block non-streaming but 0 thinking_deltas via
            # chat_stream). Effect: agent loop cannot see its own reasoning
            # from turn to turn, producing "blank rounds". Both
            # {"type":"enabled", "budget_tokens":N} and {"type":"adaptive"}
            # fix this and stream thinking equally well on this gateway; we
            # use budget_tokens=N here because chat_stream already exposes
            # that parameter and _build_api_kwargs already maps it to
            # {"type":"enabled", "budget_tokens":N} — one-line change with no
            # new plumbing. Coordinator (orchestrator.py) intentionally does
            # NOT set this — its one-shot JSON classifications don't consume
            # inter-turn reasoning, so the extra token cost is pure waste.
            thinking_budget_tokens=4096,
        )

        async def _run_after(tc: ToolCall, prereqs: List[asyncio.Task]) -> ToolResult:
            if prereqs:
                await asyncio.gather(*prereqs, return_exceptions=True)
            return await self._execute_one(tc)

        service_offset = 0

        while True:
            services_slice = self._services[service_offset:]
            if not services_slice:
                _err = "All LLM services exhausted (mid-stream retries failed)"
                self.logger.error(f"[{self.current_iteration}][ThinkStream] {_err}", component="PersistentAgent")
                return TurnOutcome(reasoning="All LLM services failed.", error=f"{LLM_API_ERROR_TAG}: {_err}"), [], TokenUsage()

            stream_gen = call_with_fallback_stream(
                services_slice,
                chat_kwargs,
                on_fallback=lambda idx, e: self.logger.warning(
                    f"[{self.current_iteration}][ThinkStream] fallback to service {service_offset + idx}: {e}",
                    component="PersistentAgent",
                ),
                on_service_selected=self._update_obs_budget_for_service,
                on_network_event=self._on_network_event,
            )

            running_tasks: List[tuple] = []
            ordered_tasks: List[tuple] = []
            dispatched_write_paths: set = set()
            stream_tool_calls: List[ToolCall] = []
            api_tool_calls_for_msg: List[Dict[str, Any]] = []
            turn_outcome: Optional[TurnOutcome] = None
            _asst_msg: Optional[Dict[str, Any]] = None
            llm_result: Optional[LLMChatResult] = None

            _stream_error: Optional[Exception] = None
            try:
                async for event in stream_gen:
                    # ── Mid-stream interrupt check ──────────────────────────────
                    # The outer iteration loop only checks at step 0; an LLM
                    # stream can run for minutes.  Peek at the flag each chunk so
                    # user-initiated interrupts land promptly.
                    if self._task_channel.check_interrupt():
                        # Cancel any tool tasks already dispatched on this turn —
                        # both sibling error-return paths (stream_error at
                        # ~L1459, no-done-event at ~L1484) do the same. Without
                        # this, in-flight write/edit/shell coroutines keep
                        # running detached, their results never collected.
                        for _, task in running_tasks:
                            task.cancel()
                        try:
                            # Best-effort: close the async generator to release
                            # the HTTP connection back to the pool immediately.
                            await stream_gen.aclose()
                        except Exception:
                            pass
                        # CRITICAL: this MUST NOT be TurnOutcome.is_completion,
                        # or _item_loop turns "user pressed stop" into "task
                        # succeeded, final_answer='LLM stream interrupted'".
                        # is_completion = not tool_calls and not error, and the
                        # per-item speculative-completion guard doesn't help
                        # (grounding_tools_used is per-item, so any prior
                        # grounding call satisfies it). Setting `error` bypasses
                        # is_completion; the outer loop's step-0 interrupt
                        # check on the next iteration then acknowledges the
                        # interrupt properly via the coordinator path.
                        return TurnOutcome(
                            reasoning="LLM stream interrupted by user.",
                            error=INTERRUPTED_BY_USER_MID_STREAM,
                        ), [], TokenUsage()

                    if isinstance(event, StreamToolCallEvent):
                        tc = ToolCall(call_id=event.call_id, tool_name=event.tool_name, parameters=event.args)
                        stream_tool_calls.append(tc)
                        api_tool_calls_for_msg.append({
                            "id": event.call_id,
                            "function": {"name": event.tool_name, "arguments": json.dumps(event.args)},
                        })

                        is_safe = self._is_concurrency_safe_call(tc)
                        if tc.tool_name in ("write", "edit"):
                            path = tc.parameters.get("path", "")
                            if path and path in dispatched_write_paths:
                                is_safe = False
                            else:
                                is_safe = True
                                if path:
                                    dispatched_write_paths.add(path)

                        prereqs: List[asyncio.Task] = [] if is_safe else [t for _, t in running_tasks if not t.done()]
                        task = asyncio.create_task(_run_after(tc, prereqs))
                        running_tasks.append((tc, task))
                        ordered_tasks.append((tc, task))

                        self.logger.info(
                            f"[{self.current_iteration}][ThinkStream] Dispatched '{tc.tool_name}' (safe={is_safe})",
                            component="PersistentAgent",
                        )

                    elif isinstance(event, StreamDoneEvent):
                        llm_result = event.result
                        reasoning = llm_result.content or ""
                        _thinking_blocks = llm_result.thinking_blocks or []
                        self._log_context_pressure(llm_result)

                        if stream_tool_calls:
                            _asst_msg = {
                                "role": "assistant",
                                "content": reasoning,
                                "tool_calls": api_tool_calls_for_msg,
                            }
                            if _thinking_blocks:
                                _asst_msg["thinking_blocks"] = _thinking_blocks
                            # A tool-call turn's `reasoning` is free-form text, not
                            # the completion JSON schema — but the prompt promises
                            # claim_tool/release_tool can appear "in one response"
                            # alongside a tool call. Best-effort scan: if the model
                            # embedded a JSON object in its reasoning, pull the
                            # fields out; absent/unparseable is the normal case and
                            # yields empty lists.
                            _claim, _release = [], []
                            if reasoning:
                                _parsed, _ = try_parse_json_with_repair_flag(reasoning)
                                if isinstance(_parsed, dict):
                                    _claim, _release = extract_self_extension_fields(_parsed)
                            turn_outcome = TurnOutcome(
                                reasoning=reasoning, tool_calls=stream_tool_calls,
                                claim_tool=_claim, release_tool=_release,
                                # Structural check, not a content requirement: the
                                # model must reason SOMEWHERE before acting — either
                                # in the visible `reasoning` text OR in an extended
                                # thinking block. What it says is entirely its own
                                # judgment; this only catches a truly blank turn
                                # (no text AND no thinking), mirroring the completion
                                # format_violation guard below. Post 2026-07-17 when
                                # PersistentAgent enabled `thinking_budget_tokens`,
                                # the model routinely puts full reasoning into a
                                # thinking block and leaves visible content empty —
                                # that's not a violation, it's the streamed thinking
                                # channel doing its job.
                                format_violation=(
                                    not reasoning.strip()
                                    and not _thinking_blocks
                                ),
                                thinking_text=llm_result.reasoning_content,
                                stop_reason=getattr(llm_result, "stop_reason", None),
                            )
                        else:
                            _asst_msg = {"role": "assistant", "content": reasoning}
                            if _thinking_blocks:
                                _asst_msg["thinking_blocks"] = _thinking_blocks
                            turn_outcome = TurnOutcome.from_completion_text(reasoning)
                            turn_outcome.thinking_text = llm_result.reasoning_content
                            turn_outcome.stop_reason = getattr(llm_result, "stop_reason", None)
                            # Truncation diagnostics: log stop_reason + output tokens on
                            # every completion turn, and stamp a truncation_note when
                            # stop_reason=max_tokens so ITEM_END surfaces the partial.
                            _sr = getattr(llm_result, "stop_reason", None)
                            self.logger.info(
                                f"[{self.current_iteration}][ThinkStream] Completion "
                                f"stop_reason={_sr} out_tokens={llm_result.output_tokens} "
                                f"raw_len={len(reasoning)}",
                                component="PersistentAgent",
                            )
                            # json_repair salvage is a benign format signal, NOT
                            # truncation — keep it as a DEBUG breadcrumb only, never
                            # in truncation_note/issues (that mislabeled every clean
                            # completion as "truncated mid-stream").
                            if turn_outcome.completion_needed_repair:
                                self.logger.debug(
                                    f"[{self.current_iteration}][ThinkStream] Completion "
                                    f"JSON needed json_repair salvage (non-strict but "
                                    f"complete); stop_reason={_sr} raw_len={len(reasoning)}",
                                    component="PersistentAgent",
                                )
                            if _sr == "max_tokens":
                                _mt_note = (
                                    f"LLM stopped at max_tokens after "
                                    f"{llm_result.output_tokens} output tokens; "
                                    f"completion is truncated."
                                )
                                turn_outcome.truncation_note = (
                                    f"{turn_outcome.truncation_note}; {_mt_note}"
                                    if turn_outcome.truncation_note else _mt_note
                                )

                        if turn_outcome.reasoning:
                            try:
                                self._interaction_manager.notify_decision_made(
                                    self.current_iteration, turn_outcome.reasoning,
                                    llm_result.total_tokens,
                                )
                            except Exception:
                                pass

            except Exception as e:
                _stream_error = e

            # Restore UI state (both happy-path and error-path)
            try:
                self._interaction_manager.notify_state_changed("executing")
            except Exception:
                pass

            if _stream_error is not None:
                # Classify BEFORE telling the user anything. A prompt-too-long
                # is a routine, self-healing condition — the PTL ladder in
                # `_item_loop` backs the budget off and retries, usually
                # transparently. Announcing it via `display_error` first painted
                # a red fatal "LLM stream error: BadRequestError ..." bubble for
                # an error that was about to be recovered: on 2026-08-03 the
                # user reported the agent "无法补救" while the log shows the very
                # next lines recovering and the run continuing for 14 more
                # turns. Surface it as progress, not failure.
                _is_ptl = self._services[0]._is_prompt_too_long_error(_stream_error)
                if _is_ptl:
                    try:
                        self._interaction_manager.notify_inline_event(
                            "context",
                            "Prompt exceeded the model's context window — "
                            "trimming context and retrying automatically.",
                        )
                    except Exception:
                        pass
                else:
                    try:
                        self._interaction_manager.display_error(
                            f"LLM stream error: {type(_stream_error).__name__}: {_stream_error}"
                        )
                    except Exception:
                        pass
                self.logger.warning(
                    f"[{self.current_iteration}][ThinkStream] Stream error"
                    f"{' (PTL — recoverable)' if _is_ptl else ''}: {_stream_error}",
                    component="PersistentAgent",
                )
                for _, task in running_tasks:
                    task.cancel()

                if _is_ptl:
                    return TurnOutcome(reasoning="Prompt too long.", error=f"{LLM_API_ERROR_TAG}:PTL: {_stream_error}"), [], TokenUsage()

                if not stream_tool_calls:
                    next_offset = service_offset + 1
                    if next_offset < len(self._services):
                        self.logger.warning(
                            f"[{self.current_iteration}][ThinkStream] Mid-stream retry with service {next_offset}",
                            component="PersistentAgent",
                        )
                        service_offset = next_offset
                        continue

                return TurnOutcome(
                    reasoning="LLM stream interrupted.",
                    error=f"{LLM_API_ERROR_TAG}: LLM stream error: {type(_stream_error).__name__}: {_stream_error}",
                ), [], TokenUsage()

            break

        if turn_outcome is None:
            self.logger.warning(f"[{self.current_iteration}][ThinkStream] Stream ended without StreamDoneEvent", component="PersistentAgent")
            for _, task in running_tasks:
                task.cancel()
            return TurnOutcome(reasoning="LLM stream incomplete."), [
                ToolResult(success=False, output=None, error="LLM stream ended without a completion event", tool_name="llm_stream", tool_parameters={})
            ], TokenUsage()

        # Collect results
        tool_results: List[ToolResult] = []
        for tc, task in ordered_tasks:
            try:
                result = await task
            except asyncio.CancelledError:
                result = ToolResult(success=False, output=None, error="Tool execution was cancelled",
                                    tool_name=tc.tool_name, tool_parameters=tc.parameters)
            except Exception as exc:
                result = ToolResult(success=False, output=None,
                                    error=f"Tool execution error: {exc}", tool_name=tc.tool_name, tool_parameters=tc.parameters)
            tool_results.append(result)

        # Drain claim_tool/release_tool intent recorded by the real
        # ClaimToolTool/ReleaseToolTool (self_extension_tool.py) via
        # ctx.pending_claim_tool/pending_release_tool, merging it into this
        # turn's TurnOutcome alongside whatever the legacy reasoning-JSON
        # scan above already found. This keeps _item_loop's existing,
        # unchanged call — `self._apply_self_extension(turn_result.claim_tool,
        # turn_result.release_tool)` — as the single application point; only
        # how intent gets INTO turn_outcome changed. Draining (not peeking)
        # means a name is consumed exactly once even if ctx is reused.
        if self._ctx is not None and turn_outcome is not None:
            _pc = getattr(self._ctx, "pending_claim_tool", None)
            if _pc:
                turn_outcome.claim_tool = list(dict.fromkeys(turn_outcome.claim_tool + _pc))
                _pc.clear()
            _pr = getattr(self._ctx, "pending_release_tool", None)
            if _pr:
                turn_outcome.release_tool = list(dict.fromkeys(turn_outcome.release_tool + _pr))
                _pr.clear()

        # Store turn in conversation history.
        # Skip obs-less completion turns: a turn with no tool_calls and no
        # observations contributes only a bare assistant message, which both
        # risks two consecutive assistant messages at the API (Anthropic 400)
        # and carries nothing not already captured in the ItemResult / summary.
        #
        # Capture any pending user message into this turn's interjection field.
        # It is consumed (cleared + logged) ONLY when a turn actually lands to
        # carry it: the guard below skips obs-less completion turns, and clearing
        # unconditionally would drop the user's message on the floor — neither in
        # the channel for the next turn nor in any ConversationTurn. Leaving it
        # in the channel means the next turn that does land picks it up.
        # Capture any pending user messages into this turn's interjection
        # field. They are consumed (cleared + logged) ONLY when a turn
        # actually lands to carry them: the guard below skips obs-less
        # completion turns, and clearing unconditionally would drop the
        # user's message on the floor — neither in the channel for the next
        # turn nor in any ConversationTurn. Leaving them in the channel means
        # the next turn that does land picks them up. Peek/clear (not a
        # single atomic drain) so a turn that fails to land leaves the queue
        # untouched — see TaskChannel.peek_pending_user_messages.
        _pending_user_msgs = self._task_channel.peek_pending_user_messages()

        if _asst_msg is not None and (_asst_msg.get("tool_calls") or tool_results):
            if _pending_user_msgs:
                # Drain the whole queue now — every peeked message is
                # accounted for by _drain_pending_interjections (carried as a
                # true interjection, or recognised as this item's own
                # instruction and dropped). Reassign _pending_user_msgs so the
                # ConversationTurn below carries only the kept ones.
                _pending_user_msgs = self._drain_pending_interjections(
                    _pending_user_msgs
                )
            self._turns.append(ConversationTurn(
                assistant_message=_asst_msg,
                observations=tool_results,
                user_interjection=list(_pending_user_msgs),
            ))
            self._current_item_turn_count += 1
            self._log_observations(tool_results)

        _in_tok = llm_result.input_tokens if llm_result is not None else 0
        _out_tok = llm_result.output_tokens if llm_result is not None else 0
        _cc_tok = llm_result.cache_creation_input_tokens if llm_result is not None else 0
        _cr_tok = llm_result.cache_read_input_tokens if llm_result is not None else 0
        return turn_outcome, tool_results, TokenUsage(
            input_tokens=_in_tok, output_tokens=_out_tok,
            cache_creation_tokens=_cc_tok, cache_read_tokens=_cr_tok,
        )

    def _build_messages(
        self,
        instruction: str,
        reminder: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build the LLM message list from ConversationTurns.

        Layout (stable prefix → volatile suffix, for prefix-cache friendliness):

            [system]
            [skill prelude ...]      append-only, last message cache-anchored
            [user: task + context]   TASK + summary + cross-item boundary
            [assistant/tool turns ]  append-only conversation trace
            [user: volatile      ]   LTM (first turn) + todo + reminder

        CC-form (Claude Code parity): the task is the FIRST user message of the
        conversation and stays there — CC's ``messages[0]`` is the user's request
        and the array is append-only, so the task is never moved and never
        restated. HandQ previously re-rendered the instruction at the BOTTOM of
        every turn (`[Continuing: ...]`), which cost the full instruction once
        per turn and read like a fresh directive. Now it sits once in the
        cache-anchored top block, byte-stable for the whole item.

        Mid-task user messages ride in the turn trace at their arrival position
        (``ConversationTurn.user_interjection``), also CC-form. Every instruction
        is additionally appended verbatim to the on-disk instruction log so it
        survives compaction — see ``_append_instruction_log``.
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt_core},
            {"role": "system", "content": self._system_prompt_env},
        ]

        # Skill prelude — rendered LIVE from the SkillRegistry every turn
        # (progressive disclosure): standing skill bodies + the [Available
        # Skills] menu. The last message gets tagged with `_cache_anchor`,
        # which `_convert_messages_to_anthropic` turns into a cache_control
        # breakpoint so the prelude is served from prefix cache on subsequent
        # turns. Content is byte-stable across turns unless the user toggles a
        # skill in the panel, so the cache keeps hitting.
        prelude = self._render_skill_prelude()
        if prelude:
            messages.extend(prelude[:-1])
            anchored = {**prelude[-1], "_cache_anchor": True}
            messages.append(anchored)

        # Budget-enforced turns (drop oldest if over budget) + supersede stale
        # snapshots in place. Computed before assembling the surrounding
        # messages so the first-message-user guard below can see whether a turn
        # trace will actually be emitted.
        turns = self._budget_enforced_turns()
        self._supersede_stale(turns)
        self._dedup_identical_outputs(turns)
        # Sole observation-elision layer (CC-aligned microcompact): under
        # budget pressure, replace old re-derivable tool RESULTS with a
        # one-line re-read hint via `superseded_note`. tool_use blocks are
        # never touched. Returns the elision events for this build so the
        # execution trace can record what was compressed this turn.
        self._last_build_retiered = self._microcompact_old_outputs(turns)

        # Top session-context message — earlier-session summary + cross-item
        # boundary history. Both change slowly (summary on compaction, boundary
        # on item completion), so this stays stable across a single item's many
        # iterations and keeps the turn trace below it cache-resident.
        #
        # Note: the user's verbatim original request is NOT injected here — it
        # rides in the bottom item block instead, adjacent to the current-task
        # instruction. LLMs pay markedly more attention to the very top and very
        # bottom of the context; bundling user-msg + item at the bottom gives
        # the agent grounded orientation on every turn (not just iter 0) at the
        # position where attention is strongest.
        top_parts: List[str] = []
        if self._resume_banner:
            top_parts.append(self._resume_banner)
            self._resume_banner = None
        # CC-form: the task is the FIRST user message of the conversation and it
        # STAYS there — Claude Code's messages[0] is the user's request and the
        # array is append-only, so the task never moves and is never restated.
        # It lives in this cache-anchored top block (byte-stable for the whole
        # item) rather than being re-rendered at the bottom every turn, which is
        # what the old `[Continuing: ...]` line did.
        top_parts.append(
            self._current_item_block or f"[Task]\n{instruction}"
        )
        if self._conversation_summary:
            top_parts.append(f"---\n{self._render_compaction_wrapper()}\n---")
        boundary = self._task_channel.get_recent_results_for_agent(limit=10)
        if boundary:
            top_parts.append(boundary)
        if top_parts:
            content = "\n\n".join(top_parts)
            # Cache breakpoint #4 (of Anthropic's 4-per-request max — the
            # other 3 are system[-2], tools[-1], and the skill-prelude
            # anchor). This block is stable across every iteration of a
            # single item (only changes on compaction or at the next item's
            # boundary), and it sits immediately after the skill-prelude
            # anchor and before the turn trace — anchoring it extends the
            # cached prefix through the LAST byte-stable point before the
            # trace begins, which itself is NOT byte-stable across turns
            # (see the three-tier rendering below) and so cannot itself be
            # anchored.
            messages.append({"role": "user", "content": content, "_cache_anchor": True})

        # Conversation trace — uniform rendering, CC-aligned.
        #
        # Every turn renders the SAME way regardless of age: the assistant
        # message in full (tool_calls NEVER stripped — the record of what the
        # model called, especially edit/write diffs, is irreproducible state)
        # followed by each observation via ToolResult.to_tool_result_json(),
        # which already renders `superseded_note` when set. The ONLY thing that
        # ever shrinks an old turn is `_microcompact_old_outputs` stamping
        # `superseded_note` on a re-derivable tool RESULT under budget pressure
        # (mirrors Claude Code microcompact: keep tool_use forever, only clear
        # old tool_results). superseded_note is one-way, so a settled turn's
        # bytes never change again → prompt-cache prefix stays stable.
        #
        # This replaces the former turn-distance three-tier loop, whose tier-3
        # stripped tool_calls purely by age (not budget) — live-confirmed to
        # make a weak model lose its own edit and loop hunting a phantom bug.
        # Budget-truncation notice: if _budget_enforced_turns dropped N turns
        # off the head this build, tell the model so — otherwise it reads the
        # remaining trace as if it were the complete history and re-does work
        # or contradicts settled state. Mirrors the explicit notice the PTL
        # recovery path already emits via _hard_drop_turns; the difference is
        # that this path is silent AND runs every turn under budget pressure,
        # not just as a last resort. No persistence: we don't mutate _turns
        # (this function is a read-only view of it), so we render the notice
        # inline every build until the turns come back under budget. The turn
        # trace below is not byte-stable across builds anyway (microcompact
        # rewrites it), so no cache-anchor invariant is broken.
        _dropped_count = len(self._turns) - len(turns)
        if _dropped_count > 0:
            _dropped_chars = sum(
                t.total_obs_chars() for t in self._turns[:_dropped_count]
            )
            messages.append({
                "role": "user",
                "content": (
                    f"[Context truncation: {_dropped_count} earlier turn(s) "
                    f"dropped from this request to fit the observation budget "
                    f"({_dropped_chars:,} chars). The trace below starts mid-"
                    f"item; earlier reasoning and observations are not visible "
                    f"this turn. Do not re-run work whose results you can only "
                    f"cite from the summary above; if a fact was in a dropped "
                    f"turn and you need it, re-observe rather than guess.]"
                ),
            })

        for turn in turns:
            # If this turn had user interjection(s), render them as a single
            # user message BEFORE the assistant response (they arrived before
            # the LLM was called). Multiple messages that arrived between
            # turns are newline-joined, each still individually logged.
            if turn.user_interjection:
                _interjection_text = "\n".join(turn.user_interjection)
                messages.append({"role": "user", "content": f"[User]: {_interjection_text}"})
            messages.append(turn.assistant_message)
            if turn.has_tool_calls:
                tc_list = turn.assistant_message.get("tool_calls", [])
                for i, obs in enumerate(turn.observations):
                    call_id = tc_list[i]["id"] if i < len(tc_list) else f"call_{i}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": obs.to_tool_result_json(),
                    })
            elif turn.observations:
                combined = "\n\n".join(
                    obs.to_tool_result_json() for obs in turn.observations
                )
                messages.append({"role": "user", "content": combined})
        # Bottom block — volatile only. The task itself now lives in the
        # cache-anchored TOP block (CC-form: task at position 0, never
        # restated), so nothing here repeats the instruction. What remains is
        # genuinely per-turn: the item's LTM recall on its first turn, plus the
        # agent's own todo and the advisor reminder.
        bottom_parts: List[str] = []
        if self._current_item_turn_count == 0 and self._current_ltm_block:
            bottom_parts.append(self._current_ltm_block)
        # Reminder section (advisor reminder + the agent's own todo).
        reminder_section_parts: List[str] = []
        # Agent-owned todo — the agent's OWN plan written via `todo_write`, fed
        # back every turn so it can see and update its list. Without this the
        # agent writes to ctx.agent_todo but never reads it back, defeating the
        # point of having a todo. Rendered as a compact status block near the
        # bottom (high-attention region) so it survives conversation growth.
        todo_block = self._render_agent_todo_block()
        if todo_block:
            reminder_section_parts.append(todo_block)
        if reminder:
            reminder_section_parts.append(reminder)
        if reminder_section_parts:
            bottom_parts.append("\n\n".join(reminder_section_parts))
        # Only emit the bottom message when there is something volatile to say.
        # Since the task moved to the TOP block, this can now legitimately be
        # empty (no LTM on a later turn, no todo, no reminder) — appending an
        # empty-content user message would be rejected by the API.
        if bottom_parts:
            messages.append({"role": "user", "content": "\n\n".join(bottom_parts)})

        # Record the total context size this build so the ExecutionRecorder's
        # turn record can track context growth. (`_last_build_retiered` was set
        # above from _microcompact_old_outputs — the elision events this build.)
        self._last_build_totals = {
            "messages": len(messages),
            "est_chars": sum(len(str(m.get("content", ""))) for m in messages),
        }

        return messages

    def _render_subagent_context_block(self) -> Optional[str]:
        """Assemble the same session-progress/current-task/LTM text
        ``_build_messages`` would show THIS agent right now, for seeding a
        spawn_agent/fan_out_agents sub-task's first message (see
        SpawnAgentTool.bind_context). Read fresh on every call — never
        cached — so a sub-agent spawned late in a long session sees the
        CURRENT compacted summary/task/LTM, not a stale snapshot from
        whenever bind_context happened to be wired.

        Mirrors _build_messages's top+bottom block assembly (persistent_
        agent.py's own "[Earlier session progress]" / "[Current Task]" /
        LTM parts) but flattened into one block — a sub-agent has no turn
        trace of its own to split a "stable prefix" from, so there's no
        prefix-cache reason to keep top and bottom separate the way the
        parent's own messages do.

        Returns None when there is nothing to inherit (fresh session, no
        item started yet) so callers can skip adding an empty message.
        """
        parts: List[str] = []
        if self._conversation_summary:
            parts.append(f"[Earlier session progress]\n{self._conversation_summary}")
        if self._current_item_block:
            parts.append(self._current_item_block)
        if self._current_ltm_block:
            parts.append(self._current_ltm_block)
        if not parts:
            return None
        return (
            "[Inherited from the agent that spawned you — this is the SAME "
            "session context it currently has, not a separate briefing]\n\n"
            + "\n\n".join(parts)
        )

    # ── User-instruction log (durable read-back after compaction) ────────────
    #
    # CC parity: Claude Code's post-compaction wrapper carries a pointer to the
    # full transcript on disk ("If you need specific details from before
    # compaction, read the full transcript at: {path}"). This is HandQ's
    # equivalent — an append-only, verbatim, tagged log of every instruction the
    # user gave. Compaction summarizes prose and MAY paraphrase or drop a
    # mid-task correction; this file leaves the original recoverable with a
    # plain `read`. The agent is TOLD the path (in the post-compaction wrapper)
    # but is never required to read it — that is its own call.
    _INSTRUCTION_LOG_NAME = "user_instructions.md"

    def _instruction_log_path(self) -> Optional[str]:
        base = self.storage_directory or self.working_directory
        if not base:
            return None
        return os.path.join(base, self._INSTRUCTION_LOG_NAME)

    def _append_instruction_log(
        self,
        text: str,
        *,
        kind: str,
        item_id: str = "",
        turn: Optional[int] = None,
    ) -> None:
        """Append one verbatim user instruction to the on-disk log.

        ``kind`` is ``"TASK"`` (an item instruction) or ``"INTERJECTION"`` (a
        mid-task message). Best-effort: any IO failure is swallowed — the log is
        a convenience for the agent, never a correctness dependency.
        """
        path = self._instruction_log_path()
        if not path or not (text or "").strip():
            return
        try:
            new_file = not os.path.exists(path)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            where = f" — item {item_id}" if item_id else ""
            if turn is not None:
                where += f", turn {turn}" if where else f" — turn {turn}"
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                if new_file:
                    fh.write(
                        "# User Instructions Log\n\n"
                        "Every instruction the user gave in this session, verbatim, "
                        "in the order received.\n"
                        "Read this if you need the exact original wording after a "
                        "context compaction.\n"
                    )
                fh.write(f"\n## [{stamp}] {kind}{where}\n\n{text.rstrip()}\n")
        except Exception:
            pass

    def _drain_pending_interjections(self, pending: List[str]) -> List[str]:
        """Consume the pending-user-message queue, returning true interjections.

        Clears the channel queue, filters out any message that is really the
        CURRENT item's own instruction, logs the survivors as INTERJECTION,
        and returns them (for the ConversationTurn.user_interjection field).

        Why the filter: the Orchestrator appends EVERY task-lane message to
        the pending queue (so genuine mid-task follow-ups like "also do B"
        reach the running agent) AND separately enqueues it as the item's
        TaskSpec. So the very message that STARTED an item lands in BOTH
        places. That message is already delivered to the LLM via
        _current_item_block (messages[0]) and logged as TASK at item start —
        carrying it here too would double-render the task in the turn trace
        and write a duplicate INTERJECTION entry in user_instructions.md
        immediately after the TASK entry (item_id + text identical). Only
        messages whose text differs from the running item's instruction are
        real interjections.

        Must be called only when the turn actually lands (see the peek/clear
        split in TaskChannel) — clearing here consumes the queue.
        """
        self._task_channel.clear_pending_user_messages()
        _cur = self._task_channel.get_current_item()
        _cur_instr = (_cur.instruction if _cur is not None else "").strip()
        _cur_id = _cur.item_id if _cur is not None else ""
        kept = [msg for msg in pending if msg.strip() != _cur_instr]
        for msg in kept:
            self._append_instruction_log(
                msg,
                kind="INTERJECTION",
                item_id=_cur_id,
                turn=self.current_iteration,
            )
        return kept

    def _render_compaction_wrapper(self) -> str:
        """Wrap the conversation summary CC-style, with a read-back pointer.

        CC parity — Claude Code injects the summary inside a fixed envelope:
        "This session is being continued from a previous conversation that ran
        out of context…", the summary body, a pointer to the full transcript on
        disk ("If you need specific details from before compaction…, read the
        full transcript at: {path}"), a note when recent messages were kept
        verbatim, and a closing "Resume directly — do not acknowledge the
        summary".

        Two deliberate deviations from CC's wording, recorded here so they don't
        later read as drift:
          * The pointer targets HandQ's verbatim instruction log rather than a
            raw transcript — it is the small, high-signal artifact (exactly what
            the user said, character-for-character) instead of a multi-MB JSONL
            the agent would burn context reading.
          * CC's envelope says "without asking the user any further questions".
            HandQ runs autonomously and has a real ask/notify channel, so
            suppressing it outright would be a regression; we keep the
            resume-directly intent and drop the don't-ask clause.
        """
        parts = [
            "This session is being continued from a previous conversation that "
            "ran out of context. The summary below covers the earlier portion of "
            "the conversation.",
            "",
            self._conversation_summary,
            "",
            "Recent messages are preserved verbatim below.",
        ]
        log_path = self._instruction_log_path()
        if log_path and os.path.exists(log_path):
            parts.append(
                "If you need the user's exact original wording from before "
                "compaction, every instruction is recorded verbatim at: "
                f"{log_path}"
            )
        parts.append(
            "Continue the work from where it left off. Resume directly — do not "
            "acknowledge this summary."
        )
        return "\n".join(parts)

    def _render_agent_todo_block(self) -> str:
        """Render the agent's own todo (written via `todo_write`) as a status block.

        Kept small: one line per item with a status glyph, ordered as the agent
        wrote them. Empty when no todo exists (initial state / task without a
        multi-step plan). Read from ctx.agent_todo (last-write-wins) — this is
        the SAME slot the tool writes to, so the agent always sees its latest
        plan next turn without a separate read.
        """
        todos = getattr(self._ctx, "agent_todo", None) if self._ctx else None
        if not todos:
            return ""
        glyph = {"pending": "☐", "in_progress": "▶", "completed": "✓"}
        lines = ["[Your Todo — your own plan, written via todo_write]"]
        for t in todos:
            g = glyph.get(t.get("status", "pending"), "☐")
            content = str(t.get("content", "")).strip()[:200]
            lines.append(f"  {g} {content}")
        return "\n".join(lines)

    def _log_context_pressure(self, llm_result: Any) -> None:
        """Log REAL token usage next to the char-based estimate every turn.

        The 2026-08-03 flash-meta run produced a 763-line engine log containing
        exactly ZERO token counts — `prompt_tokens` and `input_tokens` appear 0
        times. Every budget decision the agent makes is denominated in chars, so
        when the request was rejected as "Input is too long" there was no way,
        from the log alone, to see how far off the char estimate had been or
        which model's window had been exceeded. `LLMChatResult.token_usage` has
        carried the real numbers all along; nothing was reading them.

        Emitting `chars_per_token` here makes `resolve_obs_budget`'s constant
        empirically auditable instead of a guess: if this line consistently
        reports e.g. 1.4 while the constant says 2.0, the budget is over-issuing
        and the constant should come down.
        """
        try:
            usage = getattr(llm_result, "token_usage", None)
            if usage is None:
                return
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_tokens", 0) or 0)
            total_in = in_tok + cache_read
            # Feed the token-based compaction trigger.
            self._last_input_tokens = total_in
            window = min(svc.context_window for svc in self._services)
            turn_chars = sum(t.total_obs_chars() for t in self._turns)
            ratio = f"{turn_chars / total_in:.2f}" if total_in > 0 else "n/a"
            self.logger.info(
                f"[{self.current_iteration}][Context] input={total_in:,} tok "
                f"(fresh={in_tok:,} cached={cache_read:,}) / window={window:,} "
                f"= {(total_in / window * 100) if window else 0:.0f}% | "
                f"turns={len(self._turns)} obs_chars={turn_chars:,} "
                f"budget={self._obs_budget_chars:,} "
                f"(eff={self._effective_obs_budget():,}) | "
                f"measured chars_per_token={ratio}",
                component="PersistentAgent",
            )
        except Exception:
            # Observability must never break the turn.
            pass

    def _effective_obs_budget(self) -> int:
        """obs budget after subtracting prelude / summary / item-static overhead.

        Captures real prompt-level pressure: skill prelude, conversation
        summary, and per-item static blocks (item description, host hint,
        LTM recall) all consume tokens that the raw _obs_budget_chars
        (which only sized turns) ignored. Without this, a session with
        growing skills + LTM blocks would PTL repeatedly before turns
        even reached the old limit. Floors at 100k chars so the agent
        always has some room for fresh observations.
        """
        overhead = (
            sum(len(m.get("content", "")) for m in self._render_skill_prelude())
            + len(self._conversation_summary or "")
            + len(self._current_item_block or "")
            + len(self._current_ltm_block or "")
        )
        # Reserve a PROPORTION of the budget for fresh observations, not a fixed
        # 100k. A fixed floor larger than the budget itself silently hands back
        # more room than the window has — the same class of bug as the absolute
        # floor removed from `resolve_obs_budget`. A quarter of the budget is
        # always representable within the window by construction.
        return max(self._obs_budget_chars - overhead,
                   int(self._obs_budget_chars * 0.25))

    def _budget_enforced_turns(self) -> List[ConversationTurn]:
        """Return turns that fit within the effective observation budget."""
        if not self._turns:
            return []
        budget = self._effective_obs_budget()
        total_chars = sum(t.total_obs_chars() for t in self._turns)
        if total_chars <= budget:
            return list(self._turns)
        # Drop from front until under budget (keep at least 2 turns)
        turns = list(self._turns)
        while total_chars > budget and len(turns) > 2:
            dropped = turns.pop(0)
            total_chars -= dropped.total_obs_chars()
        return turns

    # Tools whose output is re-derivable on demand: the agent can just call the
    # tool again to get it back. Microcompact elides these once they are old,
    # replacing the body with a re-read hint. Write/edit are excluded (their
    # output is a diff/path, small and not re-derivable the same way); desktop/
    # browser snapshots are handled by _supersede_stale instead.
    #
    # SHELL/BASH ARE DELIBERATELY EXCLUDED (2026-08-05 review). CC's
    # microcompact clears "Read-class" tool results — a category defined by
    # semantics (idempotent, re-runnable reads of stable content), not by tool
    # name. HandQ's `shell` is a general escape hatch: the agent uses it for
    # both idempotent reads (`grep`, `cat`, `dir`) AND real-time physical-world
    # probes (`Get-PnpDevice`, CDP scripts reading current UI state,
    # `adb devices`). Those probes are TIME-STAMPED SNAPSHOTS — re-running
    # returns "the state NOW", not "the state THEN". Blanket-eliding all shell
    # results with a "re-run if needed" hint invites the agent to re-observe a
    # world that has since changed. Tools with narrow, idempotent semantics
    # (`read`, `grep`, `glob`, `web_search`) remain in the whitelist because
    # their re-run genuinely returns the same content (subject to the file
    # not being edited between the two reads, which is a separate concern
    # already handled by the edit-tool's staleness check).
    _MICROCOMPACT_TOOLS: frozenset = frozenset({
        "read", "grep", "glob", "web_search",
    })
    # CC parity: microcompact keeps the most recent N TURNS fully intact.
    # CC uses "5 tool uses" but that's suited for CC's 1-2-call-per-turn
    # pattern; HandQ does parallel dispatch (3-5 tool calls per turn), so
    # "5 tool uses" here would protect only 1-2 turns and lose the paired
    # reasoning. Turns are the atomic think→act unit — protecting 5 of them
    # gives the same practical coverage as CC's 5-tool-use budget while
    # preserving reasoning continuity for an autonomous long-running agent.
    _MICROCOMPACT_KEEP_RECENT_TURNS: int = 5
    # Budget gate — TWO gates AND'd:
    #   (a) obs bytes exceed this fraction of the effective budget
    #       (existing HandQ semantics — no-pressure-no-compression)
    #   (b) the elision would actually save >= this many chars
    #       (CC parity: `keepRecent=5` + "predicted savings >= 20000 tokens").
    #       Char-denominated because HandQ's budget is char-denominated;
    #       ~4 chars/token → 20k tokens ≈ 80k chars.
    # Below either gate, nothing is elided.
    _MICROCOMPACT_RATIO: float = 0.60
    _MICROCOMPACT_MIN_SAVINGS_CHARS: int = 80_000
    # Only elide bodies larger than this (chars) — small results aren't worth
    # the traceability loss.
    _MICROCOMPACT_MIN_CHARS: int = 600
    _SUPERSEDE_BUDGET_RATIO: float = 0.75

    def _microcompact_old_outputs(self, turns: List[ConversationTurn]) -> List[Dict[str, Any]]:
        """Sole observation-elision layer (CC-aligned microcompact).

        Under BUDGET PRESSURE, replace old, large, re-derivable tool RESULTS
        (read/grep/glob/web_search) with a one-line re-read hint via
        ``superseded_note``. tool_use blocks are never touched.

        CC parity (claude.exe §3.3 microcompact):
          * KEEP the most recent 5 TOOL USES intact (not turns — a turn may
            contain multiple parallel tool calls).
          * Elide only when PREDICTED SAVINGS >= ``_MICROCOMPACT_MIN_SAVINGS_CHARS``
            (~20k tokens in CC), AND obs bytes are already over
            ``_MICROCOMPACT_RATIO`` of the budget.
          * Elide `read`-class tools only. `shell`/`bash` are excluded because
            their semantics are a superset — the agent uses shell both for
            idempotent reads (`grep`, `cat`) and for real-time world probes
            (`Get-PnpDevice`, CDP UI state, `adb devices`). Blanket "re-run if
            needed" advice is wrong for the second class: re-running observes
            the world NOW, not the world THEN. See _MICROCOMPACT_TOOLS.
          * `superseded_note` is one-way (never cleared), so a settled turn's
            rendered bytes never change again → prompt-cache prefix stays
            stable.

        Returns a list of elision events (one per newly-elided obs) for the
        ExecutionRecorder's per-turn trace. Idempotent.
        """
        events: List[Dict[str, Any]] = []
        # Budget gate — below the ratio, keep everything full (CC-aligned).
        total_chars = sum(t.total_obs_chars() for t in turns)
        if total_chars <= self._effective_obs_budget() * self._MICROCOMPACT_RATIO:
            return events

        # Turn-based retention: the most recent 5 turns are fully protected.
        if len(turns) <= self._MICROCOMPACT_KEEP_RECENT_TURNS:
            return events
        cutoff = len(turns) - self._MICROCOMPACT_KEEP_RECENT_TURNS

        # First pass: identify eligible candidates and predict total savings.
        candidates: List[tuple] = []
        potential_savings = 0
        for turn in turns[:cutoff]:
            for obs in turn.observations:
                if obs.superseded_note is not None:
                    continue
                if not obs.success:
                    continue  # keep dead-path evidence intact
                if (obs.tool_name or "") not in self._MICROCOMPACT_TOOLS:
                    continue
                try:
                    body_len = len(obs.to_obs_json(1))
                except Exception:
                    continue
                if body_len < self._MICROCOMPACT_MIN_CHARS:
                    continue
                candidates.append((obs, body_len))
                potential_savings += body_len

        if potential_savings < self._MICROCOMPACT_MIN_SAVINGS_CHARS:
            return events

        # Second pass: elide.
        for obs, body_len in candidates:
            params = obs.tool_parameters or {}
            target = (
                params.get("path")
                or params.get("pattern")
                or params.get("query")
                or ""
            )
            target_hint = f" ({str(target)[:80]})" if target else ""
            obs.superseded_note = (
                f"[old {obs.tool_name} output{target_hint} elided to save "
                f"context; re-run the tool if you need it again]"
            )
            events.append({
                "tool": obs.tool_name or "?",
                "decision": "elided",
                "chars_saved": body_len,
            })
            self.logger.debug(
                f"[Microcompact] tool={obs.tool_name or '?'} elided "
                f"body_len={body_len}",
                component="PersistentAgent",
            )
        return events

    def _supersede_stale(self, turns: List[ConversationTurn]) -> None:
        """Elide older supersedable snapshots in-place; keep the newest of each.

        Screenshot/snapshot-style observations are only useful at their latest
        value — older ones are dead weight. Walk newest→oldest and, for each
        atomic tool in SUPERSEDABLE_TOOL_ACTIONS (post-2.1: keyed on tool_name
        alone since each atomic tool IS one action), keep the first (newest)
        occurrence intact while stamping every earlier one with a
        `superseded_note`. The stamp mutates the shared ToolResult objects, so
        it survives into every future _build_messages call and also shrinks
        total_obs_chars() (budget + compaction benefit too).

        Safe because supersession is monotonic: turns are append-only and
        budget/compaction trims only the oldest, so the newest occurrence of a
        signature is never the one dropped — an obs that is not-newest now can
        never become newest later.
        Failures are NEVER superseded (mirrors _microcompact_old_outputs's
        `if not obs.success: continue`). A failed observation is not a stale
        snapshot — it is the record of a dead path, and eliding it is what lets
        an agent re-walk the same wall. The 2026-08-01 Alpaca trace hit the
        identical capture failure 12 times; all but the newest were stamped
        "superseded by newer desktop_find_and_click", which reads as "there is
        a fresher value for this" rather than "this failed".

        The signature includes the TARGET, not just the tool name. Keying on
        tool_name alone asserted a false equivalence: a failed
        find_and_click("Connect") got marked superseded by a later
        find_and_click("Boot MD EDL"), two calls that share nothing but a tool.
        """
        # Budget gate — no pressure, no supersession. Mirrors _microcompact's
        # CC-aligned "no pressure → no compression" principle. Without this,
        # hover observations at different (x,y) coords get erased even when
        # context is nowhere near full, preventing the agent from building a
        # spatial map of the UI (the 2026-08-04 gear-icon-hunt failure).
        total_chars = sum(t.total_obs_chars() for t in turns)
        if total_chars <= self._effective_obs_budget() * self._SUPERSEDE_BUDGET_RATIO:
            return
        seen_signatures: set = set()
        for turn in reversed(turns):
            for obs in reversed(turn.observations):
                if obs.tool_name not in SUPERSEDABLE_TOOL_ACTIONS:
                    continue
                if not obs.success:
                    continue  # keep dead-path evidence intact
                sig = self._supersede_signature(obs)
                if obs.superseded_note is not None:
                    seen_signatures.add(sig)
                    continue
                if sig not in seen_signatures:
                    seen_signatures.add(sig)  # newest occurrence — keep intact
                    continue
                obs.superseded_note = (
                    f"[superseded by a newer {sig}; "
                    f"result elided to save tokens]"
                )

    @staticmethod
    def _supersede_signature(obs: ToolResult) -> str:
        """Identity of the surface an observation describes.

        Tool name plus the target it was pointed at, so only genuinely
        equivalent re-observations supersede each other. ``description`` covers
        find_element / find_and_click, ``hwnd`` + ``region`` cover
        screenshot / snapshot (a window capture and a fullscreen capture are
        different surfaces), and ``selector`` covers the browser pair.
        """
        params = obs.tool_parameters or {}
        parts = [obs.tool_name or ""]
        for key in ("description", "selector", "hwnd", "region", "x", "y"):
            val = params.get(key)
            if val not in (None, ""):
                parts.append(f"{key}={val}")
        return ":".join(parts)

    def _dedup_identical_outputs(self, turns: List[ConversationTurn]) -> None:
        """Stamp older observations whose output is byte-identical to a newer
        one FROM THE SAME TOOL.

        Complementary to _supersede_stale (which keys on tool-name+target) and
        _microcompact (which keys on tool-type+age). This catches the pattern
        where the SAME tool at the SAME target returns the SAME bytes across
        many turns — e.g. 62 identical GetWindowRect results — which neither of
        the other two layers can see.

        Keyed on ``(tool_name, output-hash)``, not on output-hash alone: a
        `read` and a `shell cat` of the same file, or two different UIA-tree
        tools that happen to serialize the same dialog, are NOT duplicates —
        they describe different worlds and their equivalence isn't safe to
        assert here. This matches the docstring's "same tool at the same
        target" invariant.

        The stamp names the SURVIVOR (the newer occurrence's tool name), not
        the older observation being stamped — so the agent is told which
        already-present result to trust, not which tool it just called
        (harmless when both are the same tool, but wrong if the survivor's
        tool ever differed).

        Budget-gated (same ratio as _supersede_stale) and one-way
        (idempotent, cache-safe). Only deduplicates SUCCESSFUL observations
        >= 200 chars to avoid false-positives on trivial short outputs.
        """
        total_chars = sum(t.total_obs_chars() for t in turns)
        if total_chars <= self._effective_obs_budget() * self._SUPERSEDE_BUDGET_RATIO:
            return

        import hashlib as _hl
        # newest -> oldest: first occurrence (newest) wins; older dupes get
        # stamped with a reference back to it. Key is (tool_name, hash) so
        # cross-tool identical outputs are NOT merged.
        seen: Dict[tuple, str] = {}  # (tool, hash) -> tool name of newest
        for turn_idx in range(len(turns) - 1, -1, -1):
            turn = turns[turn_idx]
            for obs in turn.observations:
                if obs.superseded_note is not None:
                    continue
                if not obs.success:
                    continue
                try:
                    # Hash the OUTPUT only (not params/step) — two calls with
                    # different commands that return identical results ARE
                    # dupes when they're from the same tool.
                    body = str(obs.output) if obs.output is not None else ""
                except Exception:
                    continue
                if len(body) < 200:
                    continue
                tool = obs.tool_name or ""
                h = _hl.blake2b(body.encode("utf-8", "replace"), digest_size=12).hexdigest()
                key = (tool, h)
                if key not in seen:
                    seen[key] = tool
                else:
                    obs.superseded_note = (
                        f"[identical to a newer {seen[key] or 'tool'} result; "
                        f"elided to save tokens]"
                    )

    # ── Tool execution ───────────────────────────────────────────────────────

    async def _execute_tool_with_write_lock(
        self, tool: BaseTool, tool_name: str, parameters: Dict[str, Any],
    ) -> ToolResult:
        """Run ``tool.execute(**parameters)``, serializing writes to the same
        path across EVERY concurrent writer in this session — not just the
        parent agent's own in-flight tool calls (that narrower dedup is
        ``dispatched_write_paths`` in ``_think_streaming``), but also any
        ``fan_out_agents``/``spawn_agent`` sub-task writing under the same
        ``SessionContext`` (``_run_one_tool`` in spawn_agent_tool.py takes the
        same lock via ``ctx.write_lock_for``). Two writers to DIFFERENT paths
        never contend — only same-path writers serialize. No-op for
        non-write/edit tools or when ctx is unavailable (test fixtures).
        """
        if tool_name not in ("write", "edit") or self._ctx is None:
            return await tool.execute(**parameters)
        path = parameters.get("path", "")
        if not path:
            return await tool.execute(**parameters)
        async with self._ctx.write_lock_for(path):
            return await tool.execute(**parameters)

    async def _execute_one(self, tc: ToolCall) -> ToolResult:
        """Execute a single ToolCall: check → validate → run → notify UI."""
        tool_name = tc.tool_name
        parameters = tc.parameters

        if tool_name in TOOL_NAME_ALIASES:
            canonical = TOOL_NAME_ALIASES[tool_name]
            self.logger.info(f"[{self.current_iteration}][Act] Tool alias '{tool_name}' -> '{canonical}'", component="PersistentAgent")
            tool_name = canonical
            tc = ToolCall(call_id=tc.call_id, tool_name=canonical, parameters=parameters)

        confirmation = await self._check_before_act(tc)
        if confirmation:
            if confirmation.is_approved():
                pass
            elif confirmation.is_rejected():
                return ToolResult(success=False, output=None, error="User rejected operation", tool_name=tool_name, tool_parameters=parameters)
            elif confirmation.is_risk_guidance():
                return ToolResult(
                    success=False, output=None,
                    error=f"High-risk operation was not executed. User guidance: {confirmation.message}",
                    tool_name=tool_name, tool_parameters=parameters,
                )
            else:
                return ToolResult(success=False, output=confirmation.message, error="USER_NEW_INSTRUCTION", tool_name=tool_name, tool_parameters=parameters)

        if not tool_name or tool_name not in self.tools:
            # Defensive auto-load, NOT "same-turn activation" of a claim: the
            # model can only emit a tool_use for a name it received a schema
            # for THIS turn (self._api_tools, frozen before the request was
            # sent — see _think_streaming), so a genuinely brand-new claim
            # can never be called in the response that claims it. This branch
            # exists for dispatch-layer callers that bypass that constraint
            # (e.g. a ToolCall constructed directly, not from a real
            # streamed tool_use) — it loads the instance on demand instead of
            # erroring "Unknown tool" when self.tools doesn't have it yet.
            if tool_name and tool_name in ToolRegistry.get_tool_names():
                try:
                    new_tools = ToolRegistry.create_all_tool_instances(
                        ctx=self._ctx, extra_tool_names=[tool_name],
                    )
                    if tool_name in new_tools:
                        self.tools[tool_name] = new_tools[tool_name]
                        self._loaded_tools.add(tool_name)
                        self._task_channel.activate_tools([tool_name])
                        self._regenerate_api_tools()
                except Exception:
                    pass  # fall through to error below

            if tool_name not in self.tools:
                available = sorted(self.tools.keys())
                error_msg = f"Unknown tool: '{tool_name}'. Available tools: {available}"
                self.logger.error(f"[{self.current_iteration}][Act] {error_msg}", component="PersistentAgent")
                return ToolResult(success=False, output=None, error=error_msg, tool_name=tool_name or "unknown", tool_parameters=parameters)

        param_error = self._validate_tool_parameters(tool_name, parameters)
        if param_error:
            self.logger.warning(f"[{self.current_iteration}][Act] Param error for '{tool_name}': {param_error[:200]}", component="PersistentAgent")
            return ToolResult(success=False, output=None, error=param_error, tool_name=tool_name, tool_parameters=parameters)

        tool = self.tools[tool_name]

        # Activity-scoped desktop overlay: keep the takeover border visible
        # while the agent is driving the desktop, and hide it (after a short
        # grace) when it switches to a non-desktop tool. Both calls self-
        # short-circuit when the desktop isn't approved/armed, so this is a
        # no-op for tasks that never touch the desktop. Pure visual/timing —
        # no effect on the ownership lock, approval gate, or revoke path.
        ds = self._ctx.desktop_state if self._ctx is not None else None
        if ds is not None:
            try:
                if tool_name.startswith("desktop_"):
                    ds.keep_alive()
                else:
                    ds.schedule_idle_hide()
            except Exception:
                pass

        try:
            truncated_params = (
                {k: (str(v)[:2000] + "..." if len(str(v)) > 2000 else str(v)) for k, v in parameters.items()}
                if parameters else None
            )
            self._interaction_manager.notify_tool_execution_started(self.current_iteration, tool_name, truncated_params, None)
        except Exception:
            pass

        result: ToolResult = await self._execute_tool_with_write_lock(tool, tool_name, parameters)

        if not result.tool_name:
            result.tool_name = tool_name
        if result.tool_parameters is None:
            result.tool_parameters = parameters

        # Egress filter: redact known plaintext secrets (keyring passwords)
        # from the tool output BEFORE it lands in the conversation history,
        # the session log, or the UI notification below. This is the single
        # boundary where every tool result passes through.
        from ..infrastructure.secret_redactor import SecretRedactor
        SecretRedactor.get().redact_tool_result(result)

        try:
            output = None
            if result.output:
                output = {
                    k: (str(v)[:2000] + "..." if len(str(v)) > 2000 else str(v))
                    for k, v in result.output.items()
                    if not (tool_name in ("bash", "shell") and k == "command")
                } if isinstance(result.output, dict) else None
            self._interaction_manager.notify_tool_execution_started(self.current_iteration, tool_name, None, output)
        except Exception:
            pass

        return result

    async def _check_before_act(self, tc: ToolCall) -> Optional[UserConfirmation]:
        """Check if confirmation is needed before executing a tool call.

        Uses InteractionManager for confirmation dialogs (both risk and tool-specific).
        ``self._interaction_manager`` is guaranteed non-None by the constructor —
        every branch that previously fell back to ``UserConfirmation.no()`` when
        IM was missing has been removed (that fallback silently auto-rejected
        legitimate tool calls and was the source of "all methods rejected by
        environment" errors when the agent was driven without a wired IM).
        """
        tool_name = tc.tool_name
        wd = self.working_directory or self.storage_directory

        # High-risk operation
        if is_high_risk(tool_name, tc.parameters or {}, wd, self.config_manager):
            self.logger.warning(f"[{self.current_iteration}][BeforeAct] High-risk operation detected", component="PersistentAgent")
            if self.config_manager.is_auto_approve_enabled("high_risk"):
                return None
            desc = get_risk_description(tool_name, tc.parameters or {}, wd, self.config_manager)
            return await self._interaction_manager.request_risk_confirmation(desc)

        # Write/edit inside working dir → auto-approve
        if tool_name in ("write", "edit"):
            file_path = tc.parameters.get("path", "")
            if file_path and is_path_within_working_dir(file_path, wd):
                return None

        # Desktop task-scoped approval — pull through SessionContext when
        # supplied (per-session DesktopState); fall back to module-level
        # helpers for ctx=None paths (test fixtures).
        # Post-2.1: matches every atomic ``desktop_*`` tool (family prefix);
        # approving ONE desktop_* action approves the whole family for that
        # task (DesktopState is session-wide, not per-tool).
        if tool_name.startswith("desktop_"):
            ds = self._ctx.desktop_state if self._ctx is not None else None
            is_approved = ds.is_task_approved() if ds is not None else _desktop_is_task_approved()
            was_rescinded = ds.was_user_rescinded() if ds is not None else _desktop_was_user_rescinded()
            mark_approved = ds.mark_task_approved if ds is not None else _desktop_mark_task_approved
            if is_approved:
                return None
            if (not was_rescinded
                    and self.config_manager.is_auto_approve_enabled("tool_desktop")):
                mark_approved()
                return None
            result = await self._interaction_manager.request_tool_confirmation(
                tool_name, tc.parameters,
                "Desktop tool requires confirmation",
            )
            if result.is_approved():
                mark_approved()
            return result

        # Config-driven approval — interaction switches are the single source
        # of truth. A tool needs confirmation ONLY when a switch explicitly
        # governs it with auto_approve=false. Two ways to run free:
        #   • no switch entry governs the tool (absence = no gate), or
        #   • its switch is present with auto_approve=true.
        # So teams / email / web_search / read / ssh / session / … run without
        # a prompt (no switch governs them); browser / desktop carry an explicit
        # auto_approve=false switch and still ask. Dangerous bash/shell commands
        # are caught earlier by is_high_risk regardless of this gate.
        switches = self.config_manager.get_interaction_switches_config()
        switch_name = "tool_bash" if tool_name == "shell" else f"tool_{tool_name}"
        if switch_name not in switches:
            return None  # no switch governs this tool → auto-approve
        if self.config_manager.is_auto_approve_enabled(switch_name):
            return None
        return await self._interaction_manager.request_tool_confirmation(
            tool_name, tc.parameters, f"Confirm execution of tool '{tool_name}'"
        )

    def _validate_tool_parameters(self, tool_name: str, parameters: Dict[str, Any]) -> Optional[str]:
        """Validate parameters against the tool's registered schema."""
        try:
            metadata = ToolRegistry.get_tool_metadata(tool_name)
        except KeyError:
            return None
        schema = metadata.parameter_schema
        allowed: set = set(schema.get("properties", {}).keys())
        required: set = set(schema.get("required", []))

        extra = set(parameters.keys()) - allowed
        if extra:
            extra_list = ", ".join(f"'{p}'" for p in sorted(extra))
            allowed_list = ", ".join(f"'{p}'" for p in sorted(allowed))
            required_list = ", ".join(f"'{p}'" for p in sorted(required))
            return (
                f"Parameter error for tool '{tool_name}': "
                f"unexpected parameter(s): {extra_list}.\n"
                f"This tool ONLY accepts: {allowed_list} (required: {required_list}).\n"
                f"ALL content must be placed inside the correct parameter(s)."
            )

        missing = required - set(parameters.keys())
        if missing:
            missing_list = ", ".join(f"'{p}'" for p in sorted(missing))
            return f"Parameter error for tool '{tool_name}': missing required: {missing_list}."
        return None

    def _is_concurrency_safe_call(self, tc: ToolCall) -> bool:
        """Return True if this tool call is safe to run concurrently."""
        tool = self.tools.get(tc.tool_name)
        if tool is None:
            return False
        if tc.tool_name in ("bash", "shell"):
            declared = tc.parameters.get("concurrent_safe")
            if declared is not None:
                # Explicit declaration (true or false) always wins over the
                # heuristic — the model gets the final say either way.
                return bool(declared)
            # Model didn't declare — fall back to a conservative server-side
            # heuristic so an obviously read-only command (ls, git status,
            # --version probes, ...) doesn't get needlessly serialized just
            # because the model forgot to set the flag.
            return looks_read_only(tc.parameters.get("command", ""))
        return bool(getattr(tool, "is_concurrency_safe", False))

    # ── Context management ───────────────────────────────────────────────────

    def _persist_event_observations(
        self, observations: List[ToolResult], note: str
    ) -> None:
        """Persist out-of-band observations as their own ConversationTurn.

        Background-task completions and context-truncation notices arrive
        outside a normal LLM turn. Recording them as a dedicated turn (a
        synthetic assistant note + the observations) keeps them in `_turns`, so
        they survive across iterations and participate in budget/compaction —
        instead of being shown once and dropped. The synthetic note keeps
        user/assistant alternation valid (mirrors the skill-prelude "Acknowledged"
        pattern); the observations render as the following user message because
        the turn carries no tool_calls.
        """
        if not observations:
            return
        self._turns.append(ConversationTurn(
            assistant_message={"role": "assistant", "content": note},
            observations=list(observations),
        ))

    def _drain_pending_file_notices(self) -> None:
        """Surface user-driven file-undo notices as an observation (Tier-1.3).

        A user undo runs on the bridge task and appends a faithful notice to
        ``ctx.pending_file_notices`` rather than touching ``_turns`` (which only
        this agent task may mutate — a cross-task append could desync
        tool_calls/tool_results and 400 the API). We drain it here at a safe
        loop point and persist each notice as its own observation turn so the
        model's history stays faithful to the world: it learns a file it edited
        was reverted and that its in-context copy is void. Drain-not-read means
        each notice surfaces exactly once.
        """
        notices = getattr(self._ctx, "pending_file_notices", None) if self._ctx else None
        if not notices:
            return
        drained = list(notices)
        notices.clear()
        observations = [
            ToolResult(
                success=True, output={"message": text},
                tool_name="file_undo", tool_parameters={},
            )
            for text in drained
        ]
        self._persist_event_observations(
            observations,
            note="(The user reverted some of my file changes — noted; I must "
            "re-read those files before relying on their contents.)",
        )

    def _poll_completed_background_tasks(self) -> None:
        """Check shell tool for completed background tasks, persist as a turn."""
        shell = self.tools.get("shell")
        get_completed = getattr(shell, "get_completed_tasks", None) if shell else None
        if get_completed is None:
            return
        completed = get_completed()
        bg_observations: List[ToolResult] = []
        for task in completed:
            stdout = task.stdout_data or ""
            stderr = task.stderr_data or ""
            obs = ToolResult(
                success=(task.exit_code == 0) if task.exit_code is not None else False,
                output={
                    "task_id": task.task_id, "command": task.command,
                    "description": task.description, "exit_code": task.exit_code,
                    "stdout": stdout, "stderr": stderr,
                    "background": True, "status": task.status,
                },
                error=stderr if task.exit_code != 0 else None,
                exit_code=task.exit_code,
                tool_name="shell",
                tool_parameters={"task_id": task.task_id, "command": task.command, "run_in_background": True},
            )
            bg_observations.append(obs)
            # Mark the task resolved in the completion audit. This observation
            # never passes through the per-turn advisor loop, so without this
            # line a task that genuinely FINISHED would still read as
            # unresolved and the audit guard would block a valid completion.
            self._completion_audit.record_tool_result(obs)
            self.logger.info(
                f"[{self.current_iteration}][Background] Task '{task.task_id}' completed "
                f"(exit_code={task.exit_code}): {(task.command or '')[:80]}",
                component="PersistentAgent",
            )
        if bg_observations:
            ids = ", ".join(
                (o.tool_parameters or {}).get("task_id", "") for o in bg_observations
            )
            self._persist_event_observations(
                bg_observations, f"(Background task results received: {ids}.)"
            )

    def _update_obs_budget_for_service(self, service: LLMService) -> None:
        """Callback: shrink the observation budget when a smaller model serves.

        DOWNWARD ONLY, clamped to ``_obs_budget_ceiling``. The ceiling is
        already the chain's smallest window, so a large-window service serving
        this turn must not raise the budget: `_turns` outlives the hop, and
        history grown to fit a 1M model cannot fall back to a 200k one. Kept as
        a real callback rather than deleted because a service may still report a
        window SMALLER than what config resolved (explicit override, gateway
        downgrade), and that shrink is worth honouring immediately.
        """
        new_budget = min(resolve_obs_budget(service.context_window),
                         self._obs_budget_ceiling)
        if new_budget < self._obs_budget_chars:
            self.logger.info(
                f"[{self.current_iteration}][Budget] Model {service.model} selected, "
                f"obs budget {self._obs_budget_chars:,} -> {new_budget:,} chars.",
                component="PersistentAgent",
            )
            self._obs_budget_chars = new_budget

    def _on_network_event(self, state: str, attempt: int, sleep_secs: int) -> None:
        """UI hook for network-aware fallback wrappers."""
        try:
            if state == "down":
                self._interaction_manager.notify_inline_event(
                    "network", f"Network appears down — pausing iteration {self.current_iteration}",
                )
            elif state == "restored":
                self._interaction_manager.notify_inline_event("network", "Network restored — resuming")
        except Exception:
            pass

    # ── Conversation compaction ────────────────────────────────────────────

    KEEP_RECENT_TURNS = 5
    # Hard ceiling on _conversation_summary length. It is fed by repeated
    # appends (_hard_drop_turns) and re-compressions (_compact_*); without a cap
    # it grows unbounded across a long multi-item session. Keep the most recent
    # chars — older narrative is the least relevant.
    MAX_SUMMARY_CHARS = 16_000
    # Item-boundary compaction only fires once obs pressure crosses this fraction
    # of the effective budget. Unconditional per-item compaction (the old
    # behavior) was a skeleton-first violation: it replaced verified raw tool
    # output with LLM prose on every item even when there was zero context
    # pressure, introducing one hallucination surface per item for no benefit.
    # Compaction is now purely budget-driven, decoupled from item structure.
    ITEM_BOUNDARY_COMPACT_RATIO = 0.80
    # CC circuit breakers (claude.exe §3.6). After this many consecutive LLM
    # summarizer failures, stop calling the LLM summarizer for the rest of the
    # session (rule-based summary only). If a compaction is followed within
    # _COMPACT_THRASH_WINDOW turns by another budget breach that many times,
    # summarizing isn't helping — warn the user once.
    _COMPACT_MAX_CONSECUTIVE_FAILURES = 3
    _COMPACT_THRASH_WINDOW = 3
    _COMPACT_THRASH_MAX_EPISODES = 3

    # ── PTL recovery tuning ────────────────────────────────────────────────
    # Step 0 of the PTL ladder walks the observation budget DOWN and retries
    # with a free, non-destructive request trim before any history is
    # destroyed. Bounded so a genuinely un-shrinkable prompt still falls
    # through to compaction / hard-drop / elision instead of spinning here.
    _PTL_MAX_BACKOFFS: int = 3
    _PTL_BACKOFF_FACTOR: float = 0.7
    # Below this the agent has no room left for fresh observations, so further
    # backoff is pointless — escalate to the destructive steps instead.
    _PTL_BUDGET_FLOOR_CHARS: int = 60_000

    async def _compact_item_boundary(self) -> None:
        """Compress old turns at an item boundary ONLY under obs-budget pressure.

        ``_turns`` is a single rolling buffer that never resets across items.
        A completed item's raw turns are cheap to keep while the buffer fits the
        budget, and keeping them preserves verified tool output (paths, exit
        codes, stdout) that the next item may reference far more reliably than a
        re-summarized narrative. So this fires on the SAME budget signal as
        :meth:`_compact_conversation`, just at a lower ratio (0.80 vs 0.95) so an
        item boundary is a natural early checkpoint to relieve pressure — never
        an unconditional summarize step.
        """
        if len(self._turns) <= self.KEEP_RECENT_TURNS:
            return

        # Token-based trigger (CC parity): use the real token count from the
        # most recent API response, not a char estimate. Falls back to
        # char-based if no measurement yet (first turn of session).
        if self._last_input_tokens > 0:
            if self._last_input_tokens < self._item_boundary_threshold_tokens:
                return
        else:
            budget = self._effective_obs_budget()
            total_chars = sum(t.total_obs_chars() for t in self._turns)
            if total_chars <= budget * self.ITEM_BOUNDARY_COMPACT_RATIO:
                return

        compress_count = len(self._turns) - self.KEEP_RECENT_TURNS
        if compress_count <= 0:
            return

        trace_text = self._build_trace_for_compaction(compress_count)
        if self._conversation_summary:
            trace_text = (
                f"[Previous summary to re-compress]\n{self._conversation_summary}\n\n"
                f"[New turns]\n{trace_text}"
            )

        # Extract verified structured facts BEFORE the turns are trimmed, so
        # they can be pinned after the prose summary (skeleton-first).
        verified = self._extract_verified_facts(compress_count)
        new_summary, _llm_ok = await self._llm_compress(trace_text, compress_count)
        self._note_compaction_outcome(_llm_ok)
        if verified:
            new_summary = f"{new_summary}\n\n{verified}"

        self._turns = self._turns[-self.KEEP_RECENT_TURNS:]
        self._conversation_summary = new_summary[-self.MAX_SUMMARY_CHARS:]

        self.logger.info(
            f"[{self.current_iteration}][Compact] Item boundary at "
            f"{total_chars / budget:.0%} budget: {compress_count} "
            f"turns -> summary ({len(self._conversation_summary)} chars), "
            f"{len(self._turns)} turns kept.",
            component="PersistentAgent",
        )

    def _extract_verified_facts(self, compress_count: int) -> str:
        """Deterministic structured-fact block from the turns being compacted.

        Post-compact restoration (skeleton-first): the LLM summary is prose and
        may drop or reword the exact paths/commands the agent will need later.
        This walks the turns about to be folded away and pulls VERIFIED facts
        straight from tool outputs — files written/edited (authoritative
        ``output['path']``), skills read, and remote hosts touched — and returns
        them as a compact block appended AFTER the prose summary. Every line
        traces to a real tool result, never to the summarizer's narration, so
        an artifact path can't be hallucinated or lost here.

        Returns "" when there is nothing worth pinning.
        """
        files: List[str] = []
        seen_files = set()
        skills: List[str] = []
        seen_skills = set()
        hosts: List[str] = []
        seen_hosts = set()

        for turn in self._turns[:compress_count]:
            for obs in turn.observations:
                if not obs.success:
                    continue
                tool = obs.tool_name or ""
                out = obs.output if isinstance(obs.output, dict) else {}
                params = obs.tool_parameters or {}
                if tool in ("write", "edit"):
                    p = out.get("path")
                    if p and p not in seen_files:
                        seen_files.add(p)
                        files.append(p)
                elif tool == "read_skill":
                    nm = out.get("name") or params.get("name")
                    if nm and nm not in seen_skills:
                        seen_skills.add(nm)
                        skills.append(nm)
                elif tool in ("ssh", "remote_handq"):
                    host = params.get("ssh_target") or params.get("host") or ""
                    if host and host not in seen_hosts:
                        seen_hosts.add(host)
                        hosts.append(host)

        if not (files or skills or hosts):
            return ""
        lines = ["[Verified facts carried past compaction]"]
        if files:
            lines.append("Files written/edited: " + "; ".join(files[:20]))
        if skills:
            lines.append("Skills read: " + ", ".join(skills[:10]))
        if hosts:
            lines.append("Remote hosts touched: " + ", ".join(hosts[:10]))
        return "\n".join(lines)

    async def _compact_conversation(self, budget_ratio: float = 0.95) -> None:
        """Compress old conversation turns into a narrative summary."""
        if len(self._turns) <= self.KEEP_RECENT_TURNS + 2:
            return

        # Token-based trigger (CC parity). Falls back to char-estimate only
        # when no real token measurement is available yet.
        if self._last_input_tokens > 0:
            if self._last_input_tokens < self._compact_threshold_tokens:
                return
        else:
            budget = self._effective_obs_budget()
            total_chars = sum(t.total_obs_chars() for t in self._turns)
            if total_chars <= budget * budget_ratio:
                return

        compress_count = len(self._turns) - self.KEEP_RECENT_TURNS
        if compress_count <= 0:
            return

        self.logger.info(
            f"[{self.current_iteration}][Compact] Token pressure "
            f"({self._last_input_tokens:,}/{self._compact_threshold_tokens:,} tok); "
            f"compressing {compress_count} turns.",
            component="PersistentAgent",
        )

        # Build trace text from old turns; always fold existing summary in so
        # the summary never grows unboundedly via repeated appends.
        trace_text = self._build_trace_for_compaction(compress_count)
        if self._conversation_summary:
            trace_text = (
                f"[Previous summary to re-compress]\n{self._conversation_summary}\n\n"
                f"[New turns]\n{trace_text}"
            )

        # LLM compression with fallback
        verified = self._extract_verified_facts(compress_count)
        new_summary, _llm_ok = await self._llm_compress(trace_text, compress_count)
        self._note_compaction_outcome(_llm_ok)
        if verified:
            new_summary = f"{new_summary}\n\n{verified}"

        # Update state — keep only recent turns; replace summary entirely
        # (old summary was already folded into trace_text above).
        self._turns = self._turns[-self.KEEP_RECENT_TURNS:]
        self._conversation_summary = new_summary[-self.MAX_SUMMARY_CHARS:]

        self.logger.info(
            f"[{self.current_iteration}][Compact] Done: {compress_count} turns -> summary "
            f"({len(new_summary)} chars), {len(self._turns)} turns kept.",
            component="PersistentAgent",
        )

    def _build_trace_for_compaction(self, compress_count: int) -> str:
        """Format old turns as a readable trace for LLM compression."""
        lines: List[str] = []

        for turn_idx in range(compress_count):
            turn = self._turns[turn_idx]
            reasoning = turn.assistant_message.get("content", "")
            if reasoning:
                reasoning = reasoning[:300]

            lines.append(f"[Turn {turn_idx + 1}]")
            if turn.user_interjection:
                lines.append(f"User said: \"{chr(10).join(turn.user_interjection)}\"")
            if reasoning.strip():
                lines.append(f"Reasoning: {reasoning}")

            for obs in turn.observations:
                status = "OK" if obs.success else "FAIL"
                params = obs.tool_parameters or {}
                tool_name = obs.tool_name or "unknown"

                if tool_name in ("bash", "shell"):
                    param_desc = params.get("command", "")[:120]
                elif tool_name in ("read", "write", "edit"):
                    param_desc = params.get("path", "")
                elif tool_name == "ssh":
                    action = params.get("action", "")
                    cmd = params.get("command", "")[:80]
                    param_desc = f"{action}: {cmd}" if cmd else action
                else:
                    first_val = next(iter(params.values()), "") if params else ""
                    param_desc = str(first_val)[:80]

                # Concatenate BOTH output and error — the old `output or error`
                # short-circuit dropped every structured failure's diagnostic
                # (shell exits non-zero with a non-empty stdout dict, so
                # obs.error never reached the compaction input, and the summary
                # read as if the call had merely returned that stdout with no
                # note of failure). Losing the failure signal at compaction
                # turns dead paths back into "looks feasible next" — the exact
                # rehash the summary is supposed to prevent.
                out_str = str(obs.output) if obs.output is not None else ""
                err_str = str(obs.error) if obs.error else ""
                if out_str and err_str:
                    output = f"{out_str}\n[error] {err_str}"
                else:
                    output = out_str or err_str
                if len(output) > 500:
                    output = output[:200] + "\n...[truncated]...\n" + output[-200:]

                lines.append(f"  {tool_name}({param_desc}) → {status}: {output}")

            lines.append("")

        return "\n".join(lines)

    async def _llm_compress(self, trace_text: str, turn_count: int) -> tuple[str, bool]:
        """Semantic compression via LLM; rule-based fallback on failure.

        Returns ``(summary, llm_ok)``. ``llm_ok`` is False when the LLM
        summarizer was skipped (circuit-breaker disabled) or failed and the
        rule-based fallback was used — the caller folds that into the
        consecutive-failure breaker. The compaction call passes NO tools (CC
        parity: ``canUseTool`` is force-denied during compaction — a summary is
        a single-shot text generation, never a tool-using turn).
        """
        if self._compact_llm_disabled:
            return self._rule_based_fallback_summary(turn_count), False
        summary_prompt = COMPACT_CONVERSATION_PROMPT.format(trace_text=trace_text)
        try:
            result = await call_with_fallback(
                self._services,
                dict(messages=[{"role": "user", "content": summary_prompt}], json_mode=False),
            )
            if result.content and result.content.strip():
                return self._strip_analysis_scratch(result.content.strip()), True
        except Exception as e:
            self.logger.warning(
                f"[{self.current_iteration}][Compact] LLM call failed: {e}; using fallback",
                component="PersistentAgent",
            )
        return self._rule_based_fallback_summary(turn_count), False

    def _note_compaction_outcome(self, llm_ok: bool) -> None:
        """Fold one compaction's result into the CC failure/thrash breakers.

        Failure breaker: N consecutive rule-based fallbacks disable the LLM
        summarizer for the session. Thrash breaker: record the iteration; if the
        gap since the previous compaction is under the thrash window, count an
        episode and warn the user once when episodes cross the cap.
        """
        if llm_ok:
            self._compact_consecutive_failures = 0
        else:
            self._compact_consecutive_failures += 1
            if (not self._compact_llm_disabled
                    and self._compact_consecutive_failures >= self._COMPACT_MAX_CONSECUTIVE_FAILURES):
                self._compact_llm_disabled = True
                self.logger.warning(
                    f"[{self.current_iteration}][Compact] LLM summarizer failed "
                    f"{self._compact_consecutive_failures}x consecutively — disabling "
                    f"it for the rest of this session (rule-based summary only).",
                    component="PersistentAgent",
                )

        prev = self._compact_iteration_history[-1] if self._compact_iteration_history else None
        self._compact_iteration_history.append(self.current_iteration)
        if prev is not None and (self.current_iteration - prev) <= self._COMPACT_THRASH_WINDOW:
            self._compact_thrash_episodes += 1
            if (not self._compact_thrash_warned
                    and self._compact_thrash_episodes >= self._COMPACT_THRASH_MAX_EPISODES):
                self._compact_thrash_warned = True
                self.logger.warning(
                    f"[{self.current_iteration}][Compact] Thrashing — compaction "
                    f"repeatedly followed within {self._COMPACT_THRASH_WINDOW} turns by "
                    f"another budget breach. A single tool output is likely too large "
                    f"for summarizing to help.",
                    component="PersistentAgent",
                )
                try:
                    self._interaction_manager.notify_inline_event(
                        "compact",
                        "Context keeps refilling right after compaction — a single "
                        "large file or tool output is likely the cause. Consider "
                        "reading it in smaller chunks.",
                    )
                except Exception:
                    pass

    @staticmethod
    def _strip_analysis_scratch(summary: str) -> str:
        """Extract the summary body from the compaction model's output.

        CC parity: Claude Code pulls the content of ``<summary>...</summary>``
        with a regex and, when the tag is absent, uses the whole raw output as
        the summary. We do the same, then drop any ``<analysis>`` scratch block
        that survived (the prompt asks the model to think in there first, and
        that reasoning is disposable — it must not eat the MAX_SUMMARY_CHARS
        budget).
        """
        m = re.search(r"<summary>(.*?)</summary>", summary, flags=re.DOTALL)
        if m:
            body = m.group(1).strip()
            if body:
                return body
        stripped = re.sub(r"<analysis>.*?</analysis>", "", summary, flags=re.DOTALL).strip()
        return stripped or summary

    def _rule_based_fallback_summary(self, compress_count: int) -> str:
        """Deterministic fallback when LLM compression fails."""
        lines: List[str] = []
        for turn_idx in range(min(compress_count, len(self._turns))):
            turn = self._turns[turn_idx]
            for obs in turn.observations:
                status = "OK" if obs.success else "FAIL"
                params = obs.tool_parameters or {}
                tool_name = obs.tool_name or "?"
                if tool_name in ("bash", "shell"):
                    detail = params.get("command", "")[:80]
                elif tool_name in ("read", "write", "edit"):
                    detail = params.get("path", "")
                elif tool_name == "ssh":
                    detail = f"{params.get('action', '')}: {params.get('command', '')[:60]}"
                else:
                    detail = str(next(iter(params.values()), ""))[:60] if params else ""
                lines.append(f"- {tool_name}({detail}) → {status}")
        return "\n".join(lines) if lines else f"({compress_count} turns compressed; details unavailable)"

    def _hard_drop_turns(self) -> int:
        """Drop oldest turns by bytes until under 60% of effective budget.

        Dropped content gets a rule-based summary appended to _conversation_summary.
        Returns number of turns dropped.
        """
        target = int(self._effective_obs_budget() * 0.60)
        total = sum(t.total_obs_chars() for t in self._turns)
        if total <= target:
            return 0

        drop_count = 0
        dropped_chars = 0
        for t in self._turns:
            if total - dropped_chars <= target:
                break
            dropped_chars += t.total_obs_chars()
            drop_count += 1

        if drop_count == 0:
            return 0

        # Summarize what's being dropped
        dropped_turns = self._turns[:drop_count]
        summary_lines: List[str] = []
        for turn in dropped_turns:
            for obs in turn.observations:
                status = "OK" if obs.success else "FAIL"
                params = obs.tool_parameters or {}
                tool_name = obs.tool_name or "?"
                if tool_name in ("bash", "shell"):
                    detail = params.get("command", "")[:80]
                elif tool_name in ("read", "write", "edit"):
                    detail = params.get("path", "")
                elif tool_name == "ssh":
                    detail = f"{params.get('action', '')}: {params.get('command', '')[:60]}"
                else:
                    detail = str(next(iter(params.values()), ""))[:60] if params else ""
                summary_lines.append(f"- {tool_name}({detail}) → {status}")

        summary = "\n".join(summary_lines) if summary_lines else f"({drop_count} turns dropped)"
        if self._conversation_summary:
            self._conversation_summary += "\n\n[Hard-drop summary]\n" + summary
        else:
            self._conversation_summary = "[Hard-drop summary]\n" + summary
        # Cap: this append is the unbounded-growth point across a long session.
        self._conversation_summary = self._conversation_summary[-self.MAX_SUMMARY_CHARS:]

        self._turns = self._turns[drop_count:]

        self._persist_event_observations(
            [ToolResult(
                success=True,
                output=(
                    f"[Context truncation: {drop_count} earlier turn(s) dropped "
                    f"({dropped_chars:,} chars freed). Summary preserved in session progress.]"
                ),
                tool_name="context_truncation_notice",
                tool_parameters={},
            )],
            "(Context truncated to fit the window.)",
        )
        return drop_count

    def _render_skill_prelude(self) -> List[Dict[str, Any]]:
        """Render the live skill prelude: standing bodies + available menu.

        Progressive disclosure (mirrors Claude Code's Skills). Pulled fresh
        from the SkillRegistry singleton every turn so panel toggles (enable /
        standing / CRUD) take effect immediately without an event pipeline:

          - Standing skill bodies are injected as transparent prompt text —
            the agent sees them as plain instructions, not as "skills".
          - [Available Skills]: name + description of every enabled non-standing
            skill for the agent to pull on demand via ``read_skill``.

        Returns a two-message [user, assistant "Acknowledged."] pair, or ``[]``
        when no enabled skills exist (keeps the prelude slot empty and the
        prefix stable). Content is byte-stable across turns unless the user
        toggles a skill, so the prefix cache keeps hitting.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            registry = SkillRegistry.get()
        except Exception:
            return []
        standing = registry.render_standing_block()
        menu = registry.render_menu_block(exclude=registry.standing_names())
        if not standing and not menu:
            return []
        parts: List[str] = []
        if standing:
            parts.append(standing)
        if menu:
            parts.append(
                menu
                + "\n\nThese skills are recipes for doing the work correctly. "
                "When the current task matches one of the skills above, calling "
                "read_skill(name) BEFORE you act is a hard requirement, not an "
                "option — load its full instructions and follow them, the same "
                "as reading a file you cannot do the task without. A task that "
                "looks simple is exactly when a skipped recipe costs the most. "
                "(If you claim a tool family that has a workflow skill, that "
                "skill's body is delivered to you automatically — you do not "
                "need to read_skill it again; just follow it.)"
            )
        content = "\n\n".join(parts)
        return [
            {"role": "user", "content": content},
            {"role": "assistant", "content": "Acknowledged."},
        ]

    def _standing_skill_names(self) -> List[str]:
        """Names of enabled+standing skills (always-in-effect for this agent).

        Recorded as ``skills_required`` in the execution trace — the standing
        set is the only body-injected skill state in the progressive-disclosure
        model; on-demand ``read_skill`` calls show up in the turn trace instead.
        """
        try:
            from ..infrastructure.skills import SkillRegistry
            return SkillRegistry.get().standing_names()
        except Exception:
            return []

    def _drop_current_ltm_block(self) -> int:
        """Drop _current_ltm_block as a PTL-recovery primitive. Returns chars freed.

        LTM is a nice-to-have hint queryable from disk; losing it mid-item is
        cheaper than losing turns, skill bodies, item description, or host
        credentials. Set to None (not empty string) so _build_messages skips
        the block entirely rather than emitting a stub.
        """
        if not self._current_ltm_block:
            return 0
        freed = len(self._current_ltm_block)
        self._current_ltm_block = None
        self.logger.warning(
            f"[{self.current_iteration}][PTL] Dropped _current_ltm_block "
            f"({freed:,} chars freed).",
            component="PersistentAgent",
        )
        return freed

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _reconcile_artifacts(
        produced_paths: list, llm_artifacts: Optional[list],
    ) -> List[str]:
        """Authoritative artifact list for a completed item.

        ``produced_paths`` are the absolute paths captured from successful
        write/edit tool outputs — the only trustworthy source, since the LLM's
        self-reported ``artifacts`` routinely contain bare relative names the
        user cannot locate. When we actually wrote files, those absolute paths
        ARE the artifacts. When we wrote none (read-only / shell-only items),
        fall back to the LLM's list so non-file deliverables still surface.
        """
        if produced_paths:
            return list(produced_paths)
        return list(llm_artifacts or [])

    # Pull the memory/knowledge sections out of a rendered LTM block, then
    # the per-entry summary text inside them. The block is XML-tagged
    # (see recall.format_memory_block / format_knowledge_block): the two
    # context wrappers hold one child element per recalled entry whose inner
    # text is the (XML-escaped) summary. Identity / known-entities blocks use
    # a different shape and are intentionally not surfaced here.
    _RECALL_SECTION_RE = re.compile(
        r"<(?:memory|knowledge)-context>(.*?)</(?:memory|knowledge)-context>",
        re.DOTALL,
    )
    _RECALL_ENTRY_RE = re.compile(r"<[\w-]+(?:\s[^>]*)?>(.*?)</[\w-]+>", re.DOTALL)

    def _emit_recall_summary(self, block: str) -> None:
        """Surface a compact one-line trace of what LTM recall pulled in.

        Best-effort, display-only: parses entry summaries out of the rendered
        block and emits a single inline event so the user sees that recall
        fired and roughly what it found. Any failure is swallowed — recall
        injection must not depend on the UI trace succeeding.
        """
        try:
            import html

            summaries: List[str] = []
            for section in self._RECALL_SECTION_RE.findall(block):
                for raw in self._RECALL_ENTRY_RE.findall(section):
                    text = html.unescape(raw).strip().replace("\n", " ")
                    if text:
                        summaries.append(text)
            if not summaries:
                return

            shown = "; ".join(s[:70] for s in summaries[:2])
            if len(shown) > 140:
                shown = shown[:139] + "…"
            count = len(summaries)
            noun = "memory" if count == 1 else "memories"
            self._interaction_manager.notify_inline_event(
                "🧠", f"Recalled {count} {noun}: {shown}",
            )
        except Exception:
            pass

    # ── TaskChannel callbacks ────────────────────────────────────────────────

    def _handle_tools_added(self, names: List[str]) -> None:
        """Callback: tools activated on the task channel — load implementations."""
        delta = [n for n in names if n and n not in self._loaded_tools]
        if not delta:
            return
        try:
            new_tools = ToolRegistry.create_all_tool_instances(
                ctx=self._ctx, extra_tool_names=delta,
            )
        except Exception as exc:
            self.logger.warning(f"[PersistentAgent] Failed to load tools {delta}: {exc}", component="PersistentAgent")
            return
        for name, tool in new_tools.items():
            if name not in self.tools:
                self.tools[name] = tool
        # Mark only names that actually resolved to a tool instance. An unknown
        # name (typo / not-yet-registered) is silently absent from new_tools —
        # create_all_tool_instances does not raise — so updating _loaded_tools
        # with the raw delta would permanently swallow a later genuine
        # activation of that name.
        loaded = [n for n in delta if n in new_tools]
        if not loaded:
            self.logger.warning(
                f"[PersistentAgent] No tools resolved from activation request "
                f"{delta} — unknown name(s) ignored.",
                component="PersistentAgent",
            )
            return
        self._loaded_tools.update(loaded)
        self._regenerate_api_tools()
        self.logger.info(f"[PersistentAgent] Tools loaded: +{loaded}", component="PersistentAgent")

    def _regenerate_api_tools(self) -> None:
        """Rebuild self._api_tools from currently-loaded, non-hidden tools.

        Called after task-channel activation (via _handle_tools_added) and
        after agent self-extension (via _apply_self_extension). Hidden tools are
        excluded from the LLM's visible tool list but their instances stay
        loaded — re-claiming is 0 ms.
        """
        base = ("read", "write", "edit", "shell", "glob", "grep")
        extra = [
            n for n in self.tools.keys()
            if n not in base and n not in self._hidden_tools
        ]
        try:
            self._api_tools = ToolRegistry.generate_tools_for_api(extra_tool_names=extra)
        except Exception:
            pass

    def _apply_self_extension(self, claim: List[str], release: List[str]) -> None:
        """Apply agent-driven claim_tool / release_tool fields from a turn.

        - claim: append to task-channel active tools (loads via the same callback
          chain skill activation uses) and remove from hidden if present.
        - release: add to hidden set; resource stays loaded for fast re-claim.

        Unknown names are silently dropped — operators see them at debug
        level. Self-extension never errors back to the agent.
        """
        if not claim and not release:
            return
        if claim:
            valid = [n for n in claim if n in ToolRegistry.get_tool_names()]
            if valid:
                # Re-claiming a hidden tool: clear it from _hidden_tools so
                # _regenerate_api_tools puts it back in the visible list.
                # activate_tools is idempotent; the on_tools_changed callback
                # fires only for genuinely-new names.
                self._hidden_tools.difference_update(valid)
                self._task_channel.activate_tools(valid)
            dropped = [n for n in claim if n not in valid]
            if dropped:
                self.logger.debug(
                    f"[PersistentAgent] claim_tool ignored unknown name(s): {dropped}",
                    component="PersistentAgent",
                )
            # Reverse-push: deliver the family's bundled workflow skill WITH
            # the tools, so its usage knowledge doesn't depend on the agent
            # choosing to read_skill (the exact gap behind the 2026-07-23
            # desktop stall — the tools were claimed, desktop-workflow never
            # read). Runs on the tool-claim path AND the reasoning-JSON path
            # since both funnel through here.
            #
            # Crucially, this runs on the FULL claim list (valid + dropped), not
            # just the valid names. A claim of a fuzzy/wrong name like
            # "desktop_click" (real name: desktop_click_at) fails the exact-name
            # match above, but the agent's INTENT — "I want to drive the desktop"
            # — is already unambiguous, and that is exactly the moment its
            # workflow recipe (with the correct tool names + action hierarchy)
            # is most valuable. workflow_skill_for_tool() matches by family
            # prefix, so "desktop_click" → "desktop_" → desktop-workflow still
            # resolves. The 2026-07-25 6-hour flash-meta stall was caused by this
            # gap: the agent claimed desktop_click/desktop_type (all wrong names),
            # got no skill and no correction, concluded desktop tools were
            # unavailable, and hand-rolled screenshot/click/OCR for the rest of
            # the task. Push-once per skill + origin==bundled gating make it safe
            # to run on the unfiltered list.
            self._reverse_push_workflow_skills(claim)
        if release:
            self._hidden_tools.update(release)
        # Always regenerate — cheap, and covers the "release-only" /
        # "re-claim hidden tool" paths where the callback chain doesn't fire.
        self._regenerate_api_tools()

    def _reverse_push_workflow_skills(self, claimed_tools: List[str]) -> None:
        """Inject the bundled ``*-workflow`` skill body for any claimed tool
        family, ONCE per session, as an out-of-band observation.

        ``claimed_tools`` is the RAW claim list — it may contain names that
        failed the exact-name match in the caller (e.g. a fuzzy "desktop_click"
        for the real "desktop_click_at"). That is intentional:
        ``workflow_skill_for_tool`` resolves by family PREFIX, so a wrong leaf
        name in the right family still maps to the correct workflow skill, and
        a wrong name is precisely when the agent most needs the recipe (it
        reveals the intended family but not the real tool names). Names in no
        workflow-backed family resolve to None and are skipped.

        This is the push half of the progressive-disclosure model: read_skill
        is the pull (agent asks), this is the push (claiming a workflow-backed
        tool family delivers its recipe unprompted). Gated hard on
        ``origin == bundled`` — product-shipped skills are trusted for
        unconditional injection; user/auto skills are NOT pushed and stay on
        the pull model (a user-edited copy of a bundled skill flips to
        origin=user and thus silently drops out — fail-safe: revert to pull,
        never inject unvetted text). Push-once via ``_pushed_workflow_skills``.
        """
        # Resolve claimed tools → distinct workflow skill names, skipping any
        # already pushed this session.
        wanted: List[str] = []
        seen: Set[str] = set()
        for tool_name in claimed_tools:
            skill_name = ToolRegistry.workflow_skill_for_tool(tool_name)
            if (
                skill_name
                and skill_name not in self._pushed_workflow_skills
                and skill_name not in seen
            ):
                seen.add(skill_name)
                wanted.append(skill_name)
        if not wanted:
            return

        try:
            from ..infrastructure.skills import SkillRegistry, SKILL_ORIGIN_BUNDLED
            registry = SkillRegistry.get()
        except Exception:
            return

        for skill_name in wanted:
            # Mark pushed up front so a resolve/inject failure never retries
            # every claim for the rest of the session.
            self._pushed_workflow_skills.add(skill_name)
            try:
                entry = registry.get_skill(skill_name)  # enabled-only
            except Exception:
                continue
            if entry is None or entry.origin != SKILL_ORIGIN_BUNDLED:
                continue
            body = (entry.body or "").rstrip()
            if not body:
                continue
            obs = ToolResult(
                success=True,
                output={
                    "skill": entry.name,
                    "description": entry.description,
                    "body": body,
                    "auto_delivered": True,
                },
                tool_name="read_skill",
                tool_parameters={"name": entry.name},
            )
            self._persist_event_observations(
                [obs],
                note=(
                    f"(I just claimed tools from the '{entry.name}' family. Its "
                    "workflow skill is delivered below — I will follow it before "
                    "acting, the same as if I had read_skill'd it.)"
                ),
            )
            self.logger.info(
                f"[PersistentAgent] Reverse-pushed workflow skill "
                f"'{entry.name}' on tool claim.",
                component="PersistentAgent",
            )
