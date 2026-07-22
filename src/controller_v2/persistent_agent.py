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
import re
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


MAX_ITEM_ITERATIONS = 999


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
        self._obs_budget_chars: int = resolve_obs_budget(self._services[0].context_window)

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
        self._conversation_summary: Optional[str] = None

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
                if err_msg.startswith(LLM_API_ERROR_TAG):
                    raw_error = err_msg[len(LLM_API_ERROR_TAG) + 1:].strip()

                    if raw_error.startswith("PTL:"):
                        min_budget = resolve_obs_budget(
                            min(svc.context_window for svc in self._services)
                        )
                        if self._obs_budget_chars > min_budget:
                            self.logger.info(
                                f"[{iteration}] PTL — shrinking obs budget "
                                f"{self._obs_budget_chars:,} -> {min_budget:,}",
                                component="PersistentAgent",
                            )
                            self._obs_budget_chars = min_budget

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
            # QGenie/Bedrock gateway silently suppresses thinking_delta events
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
                try:
                    self._interaction_manager.display_error(
                        f"LLM stream error: {type(_stream_error).__name__}: {_stream_error}"
                    )
                except Exception:
                    pass
                self.logger.warning(
                    f"[{self.current_iteration}][ThinkStream] Stream error: {_stream_error}",
                    component="PersistentAgent",
                )
                for _, task in running_tasks:
                    task.cancel()

                if self._services[0]._is_prompt_too_long_error(_stream_error):
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
                result = ToolResult(success=False, output=None, error="Tool execution was cancelled", tool_name=tc.tool_name, tool_parameters=tc.parameters)
            except Exception as exc:
                result = ToolResult(success=False, output=None, error=f"Tool execution error: {exc}", tool_name=tc.tool_name, tool_parameters=tc.parameters)
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
        if _asst_msg is not None and (_asst_msg.get("tool_calls") or tool_results):
            self._turns.append(ConversationTurn(assistant_message=_asst_msg, observations=tool_results))
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
            [user: session context]  summary + cross-item boundary
            [assistant/tool turns ]  append-only conversation trace
            [user: current item   ]  item block + LTM + host hint + reminder + action

        The per-item instruction sits at the BOTTOM so the model's freshest
        context is the current task — not the previous item's trailing tool
        results — and so the growing turn trace stays a cacheable prefix instead
        of being pushed below an instruction block that changes every item.
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
        if self._conversation_summary:
            top_parts.append(
                f"---\n[Earlier session progress]\n{self._conversation_summary}\n---"
            )
        boundary = self._task_channel.get_recent_results_for_agent(limit=10)
        if boundary:
            top_parts.append(boundary)
        # Anthropic requires a user message before the first turn in the trace.
        # Guard also fires when skill_messages exist (last skill msg is assistant),
        # so we always need a user message bridging to the turn trace.
        if turns and not top_parts:
            top_parts.append(self._current_item_block or f"[Current Task]\n{instruction}")
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
        for turn in turns:
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

        # Bottom instruction message — placed last so it is the model's freshest
        # context. On the first turn of an item, include full task block + LTM +
        # host context. On subsequent turns, abbreviate to avoid repeating static
        # content the model has already reasoned about.
        #
        # User's verbatim latest message is prepended on every turn (not just
        # iter 0). Bundling user-msg + item block at the high-attention bottom
        # position keeps the agent grounded across long items (100+ iter) where
        # the trace above may have drifted from the user's original intent.
        # `_latest_user_message` is last-write-wins: mid-flight instructions
        # ("also do X", "actually only on gv") overwrite the initial message,
        # but the initial content is already captured in the mechanically
        # enqueued item.instruction, so overwriting is safe.
        bottom_parts: List[str] = []
        user_msg = self._task_channel.get_latest_user_message()
        if user_msg:
            bottom_parts.append(
                "[User Directive — verbatim; honor this over any paraphrased "
                "reformulation]\n"
                f'"{user_msg}"'
            )
        if self._current_item_turn_count == 0:
            bottom_parts.append(
                self._current_item_block or f"[Current Task]\n{instruction}"
            )
            if self._current_ltm_block:
                bottom_parts.append(self._current_ltm_block)
        else:
            bottom_parts.append(f"[Continuing: {instruction[:120]}]")
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
        return max(self._obs_budget_chars - overhead, 100_000)

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
    _MICROCOMPACT_TOOLS: frozenset = frozenset({
        "read", "grep", "glob", "shell", "bash", "web_search",
    })
    # Only elide bodies larger than this (chars) — small results are not worth
    # the traceability loss.
    _MICROCOMPACT_MIN_CHARS: int = 600
    # Keep the most recent N turns fully intact — microcompact never touches
    # them (the agent is still actively reasoning over recent output).
    _MICROCOMPACT_KEEP_RECENT_TURNS: int = 4
    # Budget gate: microcompact only elides when total observation bytes exceed
    # this fraction of the effective obs budget. Below it, EVERYTHING stays full
    # — CC-aligned "no pressure → no compression". Kept lower than the
    # LLM-summary triggers (ITEM_BOUNDARY 0.80 / _compact_conversation 0.95) so
    # this cheap, lossless-in-capability layer relieves pressure first.
    _MICROCOMPACT_RATIO: float = 0.60

    def _microcompact_old_outputs(self, turns: List[ConversationTurn]) -> List[Dict[str, Any]]:
        """Sole observation-elision layer (CC-aligned microcompact).

        Under BUDGET PRESSURE only, replace old, large, re-derivable tool
        RESULTS (read/grep/glob/shell/web_search) with a one-line re-read hint
        via ``superseded_note``. tool_use blocks are never touched. The agent
        can re-run the tool if it needs the content again — lossless in
        capability, only trading a re-read for tokens. `superseded_note` is
        one-way (never cleared), so a settled turn's rendered bytes never change
        again → prompt-cache prefix stays stable.

        Budget-gated (``_MICROCOMPACT_RATIO``): when observation bytes are under
        the fraction of the effective budget, NOTHING is elided — everything
        stays full. This is the CC "no pressure → no compression" behaviour, and
        the fix for the case where a weak model lost its own recent edit to an
        age-based tier drop while the context was nowhere near full.

        Returns a list of elision events (one per newly-elided obs) for the
        ExecutionRecorder's per-turn trace. Idempotent: an already-elided obs is
        skipped, so a settled elision is reported once, not every build.
        """
        events: List[Dict[str, Any]] = []
        if len(turns) <= self._MICROCOMPACT_KEEP_RECENT_TURNS:
            return events
        # Budget gate — below the ratio, keep everything full (CC-aligned).
        total_chars = sum(t.total_obs_chars() for t in turns)
        if total_chars <= self._effective_obs_budget() * self._MICROCOMPACT_RATIO:
            return events
        cutoff = len(turns) - self._MICROCOMPACT_KEEP_RECENT_TURNS
        for turn in turns[:cutoff]:
            for obs in turn.observations:
                if obs.superseded_note is not None:
                    continue
                if not obs.success:
                    continue  # keep errors — they explain failures, small anyway
                if (obs.tool_name or "") not in self._MICROCOMPACT_TOOLS:
                    continue
                out = obs.output if isinstance(obs.output, dict) else {}
                if (obs.tool_name or "") in ("shell", "bash") and "task_id" in out:
                    # Background-task launch/status results carry an
                    # irreproducible task_id — re-running a launch mints a NEW
                    # task, so "re-run if needed" is actively wrong advice.
                    # Live-confirmed 2026-07-14: eliding these caused repeated
                    # relaunches chasing a lost task_id.
                    continue
                try:
                    body_len = len(obs.to_obs_json(1))
                except Exception:
                    continue
                if body_len < self._MICROCOMPACT_MIN_CHARS:
                    continue
                params = obs.tool_parameters or {}
                target = (
                    params.get("path")
                    or params.get("pattern")
                    or params.get("command")
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
                # DEBUG observability (moved here from the deleted tier-2
                # compressor): every elide decision is grep-able as
                # `[Microcompact]` on a session log, reconstructing the
                # compression history without the full trace file.
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
        """
        seen_signatures: set = set()
        for turn in reversed(turns):
            for obs in reversed(turn.observations):
                sig = obs.tool_name
                if sig not in SUPERSEDABLE_TOOL_ACTIONS:
                    continue
                if obs.superseded_note is not None:
                    seen_signatures.add(sig)
                    continue
                if sig not in seen_signatures:
                    seen_signatures.add(sig)  # newest occurrence — keep intact
                    continue
                obs.superseded_note = (
                    f"[superseded by newer {obs.tool_name}; "
                    f"result elided to save tokens]"
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
        """Callback: update observation budget when a different service is selected."""
        new_budget = resolve_obs_budget(service.context_window)
        if new_budget != self._obs_budget_chars:
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
        new_summary = await self._llm_compress(trace_text, compress_count)
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

        budget = self._effective_obs_budget()
        total_chars = sum(t.total_obs_chars() for t in self._turns)
        if total_chars <= budget * budget_ratio:
            return

        compress_count = len(self._turns) - self.KEEP_RECENT_TURNS
        if compress_count <= 0:
            return

        self.logger.info(
            f"[{self.current_iteration}][Compact] Budget at "
            f"{total_chars / budget:.0%}; compressing {compress_count} turns.",
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
        new_summary = await self._llm_compress(trace_text, compress_count)
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

                output = str(obs.output or obs.error or "")
                if len(output) > 500:
                    output = output[:200] + "\n...[truncated]...\n" + output[-200:]

                lines.append(f"  {tool_name}({param_desc}) → {status}: {output}")

            lines.append("")

        return "\n".join(lines)

    async def _llm_compress(self, trace_text: str, turn_count: int) -> str:
        """Call LLM for semantic compression; fall back to rule-based on failure."""
        summary_prompt = COMPACT_CONVERSATION_PROMPT.format(trace_text=trace_text)
        try:
            result = await call_with_fallback(
                self._services,
                dict(messages=[{"role": "user", "content": summary_prompt}], json_mode=False),
            )
            if result.content and result.content.strip():
                return self._strip_analysis_scratch(result.content.strip())
        except Exception as e:
            self.logger.warning(
                f"[{self.current_iteration}][Compact] LLM call failed: {e}; using fallback",
                component="PersistentAgent",
            )

        return self._rule_based_fallback_summary(turn_count)

    @staticmethod
    def _strip_analysis_scratch(summary: str) -> str:
        """Drop the COMPACT_CONVERSATION_PROMPT's <analysis> scratch block.

        The prompt asks the model to think through the trace inside
        <analysis> tags before writing the actual summary — that block is
        disposable reasoning, not part of the summary we want to spend the
        MAX_SUMMARY_CHARS budget on.
        """
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
                + "\n\nWhen the current task matches one of the skills above, "
                "call read_skill(name) to load its full instructions before you "
                "act — like reading a file you need."
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
        if release:
            self._hidden_tools.update(release)
        # Always regenerate — cheap, and covers the "release-only" /
        # "re-claim hidden tool" paths where the callback chain doesn't fire.
        self._regenerate_api_tools()
