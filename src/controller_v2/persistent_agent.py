"""
PersistentAgent — long-lived agent loop driven by SharedCheckList.

Implements the Observe-Think-Act loop directly with streaming tool dispatch.

Feature set:
  - Full PTL recovery (semantic compaction → hard half-drop → retry)
  - IterationAdvisor (anti-repeat guard + parallelism nudge + stagnation detection)
  - Execution recorder (agent_start / write_iteration / agent_end per item)
  - Progressive tool loading via checklist callbacks
  - Interrupt handling at iteration level
  - Per-item static context: item description + provider hint + LTM recall
    (persistent fields, rebuilt into instruction every turn)
  - Effective obs budget (subtracts prelude/summary/item-static overhead)
  - Cross-item boundary view via SharedCheckList.get_recent_results_for_agent
  - User confirmation via InteractionManager (risk + tool-specific)
  - Stale-snapshot supersession
"""
import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Set, Union

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

from .interaction_manager import InteractionManager
from .shared_checklist import SharedCheckList, CheckListItem, ItemResult, INTERRUPTED_BY_PLANNER
from .risk_check import is_high_risk, get_risk_description, is_path_within_working_dir
from .user_confirmation import UserConfirmation

if TYPE_CHECKING:
    from .session_context import SessionContext
from .agent_utils import (
    LLM_API_ERROR_TAG,
    INFRA_TOOL_NAMES,
    TOOL_NAME_ALIASES,
    ToolCall,
    TurnOutcome,
    ConversationTurn,
    IterationAdvisor,
    format_tool_entry,
    resolve_obs_budget,
    SUPERSEDABLE_TOOL_ACTIONS,
)
from .agent_prompts import AGENT_SYSTEM_PROMPT, COMPACT_CONVERSATION_PROMPT, get_platform_context

# Desktop confirmation helpers — imported eagerly so packaging tools can
# statically discover the dependency and any breakage surfaces at startup.
from ..tools.desktop_tool import (
    is_task_approved as _desktop_is_task_approved,
    mark_task_approved as _desktop_mark_task_approved,
    was_user_rescinded as _desktop_was_user_rescinded,
)


MAX_ITEM_ITERATIONS = 999

PreItemHintProvider = Callable[[CheckListItem], Union[str, Awaitable[str]]]


class PersistentAgent:
    """V2 persistent agent — fully self-contained, no RuntimeAgent inheritance.

    Session lifecycle:
      - Created once at session start
      - run_loop() blocks indefinitely, processing items from CheckList
      - Between items, blocks on wait_for_current_item()
      - Loop only exits via CancelledError (flow teardown)
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        checklist: SharedCheckList,
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        max_item_iterations: int = MAX_ITEM_ITERATIONS,
        config_manager: Optional[ConfigManager] = None,
        execution_recorder: Optional[ExecutionRecorder] = None,
        interaction_manager: InteractionManager = None,  # required, see check below
        pre_item_hint_provider: Optional[PreItemHintProvider] = None,
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

        # Legacy interrupt-event post-injection — kept ONLY for ctx=None paths.
        # When ctx is supplied the tools already have the event from their
        # own __init__ pulling ctx.interrupt_event.
        if ctx is None:
            interrupt_event = checklist._interrupt_event
            if interrupt_event is not None:
                for tool_name in ("shell", "session"):
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
        # _build_messages call until the next item begins. Keeps
        # expected_outcomes / SSH credentials / LTM hints visible for the whole
        # multi-iteration item instead of being shown only on the first turn.
        self._current_item_block: Optional[str] = None
        self._current_item_hint: Optional[str] = None
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
        #   - ExecutionRecorder.write_iteration's iteration field, paired with
        #     step_id so records are unique within a session
        #   - InteractionManager turn IDs visible to UI
        # Per-item iteration count lives in `_item_loop`'s local `iteration`.
        self.current_iteration: int = 0
        self.logger = get_logger()

        # Recorder / UI / Checklist
        self.execution_recorder: Optional[ExecutionRecorder] = execution_recorder
        self._interaction_manager: InteractionManager = interaction_manager

        self._checklist = checklist
        self._max_item_iterations = max_item_iterations
        # Item counters split by outcome so logs/metrics distinguish
        # "we ran N items" from "M of them succeeded".
        self._total_items_processed = 0
        self._total_items_succeeded = 0
        self._total_items_failed = 0
        self._total_token_usage = TokenUsage()

        # Pre-item hint provider
        self._pre_item_hint_provider: Optional[PreItemHintProvider] = pre_item_hint_provider

        # Skills / tools injection tracking
        self._injected_skills: Set[str] = set()
        self._loaded_tools: Set[str] = set()

        # Skill prelude — append-only ordered list, sits between system prompt
        # and the per-item instruction in _build_messages. Excluded from
        # compaction (parallel to _turns) and anchored as a cache breakpoint.
        # Each entry preserves the contributing skill names so PTL recovery
        # can log meaningfully on eviction:
        #   {"names": (skill_name_1, ...),
        #    "messages": [{"role": "user", "content": ...},
        #                 {"role": "assistant", "content": "Acknowledged."}]}
        self._skill_entries: List[Dict[str, Any]] = []

        # Subscribe to checklist state changes
        self._checklist.on_skills_changed(self._handle_skills_added)
        self._checklist.on_tools_changed(self._handle_tools_added)

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
        self._system_prompt = AGENT_SYSTEM_PROMPT + "\n\n---\n## Environment\n\n" + "\n".join(env_parts)

    # ── Public API ───────────────────────────────────────────────────────────

    async def run_loop(self) -> None:
        """Run the persistent loop. Blocks forever until CancelledError."""
        self.logger.info("Persistent agent loop started", component="PersistentAgent")
        try:
            while True:
                item = await self._checklist.wait_for_current_item()
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
            output_str = str(obs.output)
            self.logger.info(
                f"[{self.current_iteration}][Observe] Tool={obs.tool_name}, "
                f"Success={obs.success}, Output(len={len(output_str)}): {output_str[:500]}...",
                component="PersistentAgent",
            )

    # ── Per-item execution ───────────────────────────────────────────────────

    async def _execute_item(self, item: CheckListItem) -> None:
        """Execute a single CheckList item with full feature parity."""
        self._advisor.reset_for_item()

        # Item-static context — persisted until the next item begins.
        # _build_messages reads these every turn so the agent never loses
        # expected_outcomes, host credentials, or LTM recall partway through
        # a multi-iteration item.
        self._current_item_block = item.to_agent_message()
        self._current_item_hint  = await self._gather_pre_item_hint(item)
        self._current_ltm_block  = await self._gather_ltm_block(item)

        if self.execution_recorder:
            self.execution_recorder.write_agent_start(
                step_id=item.item_id,
                goal=item.instruction,
                planner_reasoning=item.planner_reasoning,
                expected_outcomes=item.expected_outcomes,
                active_tools=sorted(self._loaded_tools),
                ssh_target=item.ssh_target,
                skills_required=sorted(self._injected_skills),
            )

        try:
            self._interaction_manager.notify_state_changed("executing")
        except Exception:
            pass

        item_result = await self._item_loop(item)
        self._total_token_usage += item_result.token_usage or TokenUsage()
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
                factual_outcome=item_result.factual_outcome,
                artifacts=item_result.artifacts,
                key_findings=item_result.key_findings,
                issues=item_result.issues,
            )

        self._checklist.mark_current_done(item_result)
        self.logger.info(
            f"[PersistentAgent] Item '{item.item_id}' done "
            f"(success={item_result.success}, iters={item_result.iterations})",
            component="PersistentAgent",
        )

    async def _item_loop(self, item: CheckListItem) -> ItemResult:
        """Per-item OTA loop."""
        instruction = item.instruction
        iteration = 0
        _tools_used: list = []
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
            if self._checklist.check_interrupt():
                interrupt_reason = self._checklist.acknowledge_interrupt()
                self.logger.info(
                    f"[{iteration}][Interrupt] Planner interrupt for item={item.item_id}"
                    f"{f': {interrupt_reason}' if interrupt_reason else ''}",
                    component="PersistentAgent",
                )
                issue_msg = INTERRUPTED_BY_PLANNER
                if interrupt_reason:
                    issue_msg = f"{INTERRUPTED_BY_PLANNER}: {interrupt_reason}"
                return ItemResult(
                    item_id=item.item_id,
                    success=False,
                    issues=[issue_msg],
                    artifacts=list(_produced_paths),
                    iterations=iteration,
                    token_usage=_token_usage,
                )

            # ── 1. Background + Compact ─────────────────────────────────────
            self._poll_completed_background_tasks()
            await self._compact_conversation()

            # ── 2. Reminders (via IterationAdvisor) ────────────────────────
            reminder = self._advisor.get_reminder()

            # ── 2b. Stagnation-triggered LTM refresh ───────────────────────
            # When the advisor sees ≥3 consecutive failures (cooldown-gated so
            # we don't re-query every turn), run a fresh LTM recall whose query
            # is enriched with the recent failure signatures. Result is shown
            # as an independent block in the reminder area — _current_ltm_block
            # is NOT replaced, so the original happy-path recall stays visible
            # alongside and the per-item prefix-cache anchor is preserved.
            extra_ltm_block: Optional[str] = None
            if self._advisor.should_refresh_ltm():
                signatures = self._advisor.get_recent_failure_signatures()
                extra_ltm_block = await self._gather_stagnation_ltm_block(
                    item, signatures
                )

            # ── 3. Think + Act ───────────────────────────────────────────────
            turn_result, tool_results, _iter_token_usage = await self._think_streaming(
                instruction, reminder, extra_ltm_block
            )
            _token_usage += _iter_token_usage

            self._advisor.record_turn_tool_count(len(turn_result.tool_calls or []))

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

                        # Evict oldest skill prelude entry (loses skill body
                        # for the rest of the session; tradeoff vs PTL-stuck)
                        if self._evict_oldest_skill_entry() > 0:
                            continue

                        # Last resort: truncate oldest turn's observations
                        if self._turns and len(self._turns[0].observations) > 1:
                            self._turns[0].observations = self._turns[0].observations[-1:]
                            continue

                        raw_error = raw_error[len("PTL:"):].strip()

                    self.logger.error(
                        f"[{iteration}] LLM API error: {raw_error[:200]}",
                        component="PersistentAgent",
                    )
                    return ItemResult(
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
                return ItemResult(
                    item_id=item.item_id,
                    success=False,
                    issues=[f"Error: {err_text[:300]}"],
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
                    return ItemResult(
                        item_id=item.item_id,
                        success=False,
                        issues=[f"Stream error: {error_summary[:300]}"],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )

                self.logger.info(
                    f"[{iteration}] Item complete: {turn_result.reasoning[:100]}",
                    component="PersistentAgent",
                )
                return ItemResult(
                    item_id=item.item_id,
                    success=True,
                    factual_outcome=list(turn_result.factual_outcome or []),
                    artifacts=self._reconcile_artifacts(
                        _produced_paths, turn_result.artifacts
                    ),
                    key_findings=list(turn_result.key_findings or []),
                    iterations=iteration,
                    token_usage=_token_usage,
                )

            # ── 6. Record tool results ───────────────────────────────────────
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
                is_parallel = len(tool_results) > 1
                for idx, tr in enumerate(tool_results):
                    self.execution_recorder.write_iteration(
                        tool_result=tr,
                        decision=turn_result,
                        iteration=self.current_iteration,
                        step_id=item.item_id,
                        parallel_index=idx if is_parallel else None,
                        token_usage=_iter_token_usage,
                    )

            # ── 7. USER_NEW_INSTRUCTION propagation ──────────────────────────
            for tr in tool_results:
                if tr.error == "USER_NEW_INSTRUCTION":
                    return ItemResult(
                        item_id=item.item_id,
                        success=False,
                        issues=["User new instruction"],
                        iterations=iteration,
                        token_usage=_token_usage,
                    )

            # ── 8. Update advisor ────────────────────────────────────────────
            for tr in tool_results:
                self._advisor.record_tool_result(tr)

        # Reached per-item iteration cap
        advisor_summary = self._advisor.get_summary()
        self.logger.warning(
            f"Item '{item.item_id}' hit iteration cap ({self._max_item_iterations}). "
            f"Success rate: {advisor_summary['success_rate']:.1%}",
            component="PersistentAgent",
        )
        return ItemResult(
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
        extra_ltm_block: Optional[str] = None,
    ) -> tuple[TurnOutcome, List[ToolResult], TokenUsage]:
        """Streaming Think+Act: open stream, dispatch tools as they arrive."""
        messages = self._build_messages(instruction, reminder, extra_ltm_block)

        try:
            self._interaction_manager.notify_state_changed("thinking")
        except Exception:
            pass

        chat_kwargs = dict(messages=messages, tools=self._api_tools, json_mode=False)

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

                        if stream_tool_calls:
                            _asst_msg = {
                                "role": "assistant",
                                "content": reasoning,
                                "tool_calls": api_tool_calls_for_msg,
                            }
                            turn_outcome = TurnOutcome(reasoning=reasoning, tool_calls=stream_tool_calls)
                        else:
                            _asst_msg = {"role": "assistant", "content": reasoning}
                            turn_outcome = TurnOutcome.from_completion_text(reasoning)

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

        # Store turn in conversation history.
        # Skip obs-less completion turns: a turn with no tool_calls and no
        # observations contributes only a bare assistant message, which both
        # risks two consecutive assistant messages at the API (Anthropic 400)
        # and carries nothing not already captured in the ItemResult / summary.
        if _asst_msg is not None and (_asst_msg.get("tool_calls") or tool_results):
            self._turns.append(ConversationTurn(assistant_message=_asst_msg, observations=tool_results))
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
        extra_ltm_block: Optional[str] = None,
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
            {"role": "system", "content": self._system_prompt},
        ]

        # Skill prelude — append-only, sits after system. The last message gets
        # tagged with `_cache_anchor`, which `_convert_messages_to_anthropic`
        # turns into a cache_control breakpoint so the full prelude is served
        # from prefix cache on subsequent turns.
        if self._skill_entries:
            flat = self._flat_skill_messages()
            messages.extend(flat[:-1])
            anchored = {**flat[-1], "_cache_anchor": True}
            messages.append(anchored)

        # Budget-enforced turns (drop oldest if over budget) + supersede stale
        # snapshots in place. Computed before assembling the surrounding
        # messages so the first-message-user guard below can see whether a turn
        # trace will actually be emitted.
        turns = self._budget_enforced_turns()
        self._supersede_stale(turns)

        # Top session-context message — earlier-session summary + cross-item
        # boundary history. Both change slowly (summary on compaction, boundary
        # on item completion), so this stays stable across a single item's many
        # iterations and keeps the turn trace below it cache-resident.
        top_parts: List[str] = []
        if self._conversation_summary:
            top_parts.append(
                f"---\n[Earlier session progress]\n{self._conversation_summary}\n---"
            )
        boundary = self._checklist.get_recent_results_for_agent(limit=10)
        if boundary:
            top_parts.append(boundary)
        # Anthropic requires a user message before the first turn in the trace.
        # Guard also fires when skill_messages exist (last skill msg is assistant),
        # so we always need a user message bridging to the turn trace.
        if turns and not top_parts:
            top_parts.append(self._current_item_block or f"[Current Task]\n{instruction}")
        if top_parts:
            messages.append({"role": "user", "content": "\n\n".join(top_parts)})

        # Conversation trace.
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
                    obs.to_obs_json(i + 1) for i, obs in enumerate(turn.observations)
                )
                messages.append({"role": "user", "content": combined})

        # Bottom instruction message — fresh per item, rebuilt every turn so the
        # agent never loses expected_outcomes / host credentials / LTM recall,
        # with the per-turn reminder + action prompt appended. Placed last so it
        # is the model's freshest context. The `[Current Task]` fallback only
        # fires if _current_item_block is None (should not happen in normal
        # flow; set in _execute_item). Adjacent same-role messages (e.g. the
        # preceding tool-result user message) are coalesced by the converter.
        bottom_parts: List[str] = [
            self._current_item_block or f"[Current Task]\n{instruction}"
        ]
        if self._current_ltm_block:
            bottom_parts.append(self._current_ltm_block)
        if self._current_item_hint:
            bottom_parts.append(f"[Host Context]\n{self._current_item_hint}")
        next_action = (
            "Pick one: "
            "(a) tool calls — batch every independent call in this same turn; "
            "(b) completion JSON (no tool calls) when the instruction is fully achieved; "
            "(c) error JSON (no tool calls) only when the instruction is genuinely unachievable."
        )
        # Reminder section. extra_ltm_block (when present) is the
        # stagnation-triggered fresh LTM recall — kept here as an independent
        # block so it sits next to the advisor reminder that motivated it,
        # WITHOUT overwriting _current_ltm_block above (the original happy-path
        # recall stays visible, and the per-item prefix-cache anchor is intact).
        reminder_section_parts: List[str] = []
        if extra_ltm_block:
            reminder_section_parts.append(
                f"[Stagnation Recall — refreshed LTM for current blockers]\n"
                f"{extra_ltm_block}"
            )
        if reminder:
            reminder_section_parts.append(reminder)
        reminder_section_parts.append(next_action)
        bottom_parts.append("\n\n".join(reminder_section_parts))
        messages.append({"role": "user", "content": "\n\n".join(bottom_parts)})

        return messages

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
            sum(
                len(m.get("content", ""))
                for e in self._skill_entries
                for m in e["messages"]
            )
            + len(self._conversation_summary or "")
            + len(self._current_item_block or "")
            + len(self._current_item_hint or "")
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

    def _supersede_stale(self, turns: List[ConversationTurn]) -> None:
        """Elide older supersedable snapshots in-place; keep the newest of each.

        Screenshot/snapshot-style observations are only useful at their latest
        value — older ones are dead weight. Walk newest→oldest and, for each
        (tool, action) in SUPERSEDABLE_TOOL_ACTIONS, keep the first (newest)
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
                action = (obs.tool_parameters or {}).get("action")
                sig = (obs.tool_name, action)
                if sig not in SUPERSEDABLE_TOOL_ACTIONS:
                    continue
                if obs.superseded_note is not None:
                    seen_signatures.add(sig)
                    continue
                if sig not in seen_signatures:
                    seen_signatures.add(sig)  # newest occurrence — keep intact
                    continue
                obs.superseded_note = (
                    f"[superseded by newer {obs.tool_name}.{action}; "
                    f"result elided to save tokens]"
                )

    # ── Tool execution ───────────────────────────────────────────────────────

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

        result: ToolResult = await tool.execute(**parameters)

        if not result.tool_name:
            result.tool_name = tool_name
        if result.tool_parameters is None:
            result.tool_parameters = parameters

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
        if tool_name == "desktop":
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
            return bool(tc.parameters.get("concurrent_safe", False))
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

    async def _compact_conversation(self, budget_ratio: float = 0.8) -> None:
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
        new_summary = await self._llm_compress(trace_text, compress_count)

        # Update state — keep only recent turns; replace summary entirely
        # (old summary was already folded into trace_text above).
        self._turns = self._turns[-self.KEEP_RECENT_TURNS:]
        self._conversation_summary = new_summary

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
                return result.content.strip()
        except Exception as e:
            self.logger.warning(
                f"[{self.current_iteration}][Compact] LLM call failed: {e}; using fallback",
                component="PersistentAgent",
            )

        return self._rule_based_fallback_summary(turn_count)

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

    def _flat_skill_messages(self) -> List[Dict[str, Any]]:
        """Flatten _skill_entries into a single message list."""
        return [m for e in self._skill_entries for m in e["messages"]]

    def _evict_oldest_skill_entry(self) -> int:
        """Drop the oldest skill prelude entry. Returns chars freed.

        Used as a PTL-recovery primitive: when turn-level shrinking can't free
        enough room, the next-cheapest source of bytes is the skill prelude.
        Eviction is permanent for the session — there is no re-injection path
        — so this should only fire after turn-level recovery is exhausted.
        Tracks contributing skill names in the log so debugging a session that
        lost a skill body has a breadcrumb.
        """
        if not self._skill_entries:
            return 0
        oldest = self._skill_entries.pop(0)
        freed = sum(len(m.get("content", "")) for m in oldest["messages"])
        names = oldest.get("names", ())
        # _injected_skills intentionally NOT updated: re-activating the same
        # skill via the planner would re-trigger _handle_skills_added, which
        # would skip it (already injected). Removing from _injected_skills
        # here would let it be re-injected on next planner activation, which
        # is desirable in some cases but introduces unbounded re-injection
        # risk under thrashing. Leaving the bridge for future work.
        self.logger.warning(
            f"[{self.current_iteration}][PTL] Evicted oldest skill prelude entry "
            f"(skills={list(names)}, freed={freed:,} chars, "
            f"remaining_entries={len(self._skill_entries)})",
            component="PersistentAgent",
        )
        return freed

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

    async def _gather_pre_item_hint(self, item: CheckListItem) -> Optional[str]:
        """Invoke the FlowController's per-item hint provider for host context.

        Returns the hint string (stripped, non-empty) or None. Persisted by
        _execute_item into self._current_item_hint so it remains visible for
        the entire item lifetime, not just the first iteration.
        """
        if self._pre_item_hint_provider is None:
            return None
        try:
            result = self._pre_item_hint_provider(item)
            hint: str = result if isinstance(result, str) else await result
        except Exception as exc:
            self.logger.warning(
                f"[PersistentAgent] pre_item_hint_provider failed: {exc}",
                component="PersistentAgent",
            )
            return None
        return hint.strip() if hint and hint.strip() else None

    @staticmethod
    def _resolve_current_frame(item: CheckListItem) -> dict:
        """Map ``item.ssh_target`` to a frame dict for LTM recall.

        Empty ``ssh_target``  → ``{os:'windows', host:'local',  confidence:0.95}``
        ``user@host`` value   → ``{os:'linux',   host:<host>,   confidence:0.85}``

        Why ``confidence=0.85`` for SSH (not 0.95): the hostname overwhelmingly
        indicates a Linux remote, but a user could SSH to a Windows server or
        a non-Linux Unix. 0.85 stays above the 0.6 floor in
        :func:`recall._frame_compatible` so filtering is active, but a
        low-confidence wrong guess does NOT fully suppress signal — the
        permissive-on-uncertainty bias still applies.
        """
        target = (item.ssh_target or "").strip()
        if not target:
            return {"os": "windows", "host": "local", "confidence": 0.95}
        host = target.split("@", 1)[1] if "@" in target else target
        return {"os": "linux", "host": host or "unknown", "confidence": 0.85}

    async def _gather_ltm_block(self, item: CheckListItem) -> Optional[str]:
        """Recall LTM context for this item's instruction.

        Uses the same LongTermMemory.get() singleton + format_context_block
        API as Orchestrator, but queries with item.instruction (fine-grained)
        rather than the raw user message (coarse-grained). Returns the
        formatted block or None on empty/error so injection is a no-op.
        """
        try:
            from ..infrastructure.long_term_memory import LongTermMemory
            ltm = LongTermMemory.get()
        except Exception:
            return None
        # LTM 2.0 frame: derive from item.ssh_target — local items get
        # ``windows/local``; remote items get ``linux/<host>`` so SSH-captured
        # insights surface and Windows-only insights are filtered out. See
        # _resolve_current_frame for the confidence rationale.
        current_frame = self._resolve_current_frame(item)
        try:
            self._interaction_manager.notify_recall_started()
            block = await ltm.format_context_block(
                query=item.instruction, rerank=True,
                current_frame=current_frame,
            )
            if not (block and block.strip()):
                return None
            self._emit_recall_summary(block)
            return block
        except Exception as exc:
            self.logger.warning(
                f"[PersistentAgent] LTM recall failed: {exc}",
                component="PersistentAgent",
            )
            return None

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

    async def _gather_stagnation_ltm_block(
        self, item: CheckListItem, failure_signatures: List[str]
    ) -> Optional[str]:
        """Mid-item LTM recall focused on failure-recovery patterns.

        Triggered by IterationAdvisor.should_refresh_ltm() when the agent
        has stagnated (≥3 consecutive failures, cooldown-gated). This is a
        REMINDER injected on top of ``_current_ltm_block`` (which already
        carries IDENTITY + AGENTIC + instruction-aligned recall from
        item-start), so:
          - query: ``failure_signatures``-only — the instruction is
            already in ``_current_item_block`` and re-including it dilutes
            BM25 / dense focus on the actual blocker.
          - memory dimension = INSIGHT — AGENTIC preferences live in the
            item-start block already; INSIGHT is where past-failure /
            environment facts cluster.
          - include_identity=False — IDENTITY is in ``_current_ltm_block``;
            re-rendering it on every stagnation refresh wastes tokens.
          - k=3/3 — focused reminder, not full re-discovery.
        Returns None on empty/error so the caller's injection path becomes
        a no-op.
        """
        try:
            from ..infrastructure.long_term_memory import (
                LongTermMemory, MemoryDimension,
            )
            ltm = LongTermMemory.get()
        except Exception:
            return None
        if failure_signatures:
            sigs = "; ".join(failure_signatures)
            query = f"Recovery strategies for: {sigs}"
        else:
            # No signatures yet — advisor's should_refresh_ltm() only
            # fires after ≥3 consecutive failures, so this branch is
            # uncommon. Fall back to the instruction so we still issue a
            # sensible recall instead of an empty query.
            query = item.instruction
        try:
            self._interaction_manager.notify_recall_started()
            block = await ltm.format_context_block(
                query=query,
                memory_dimension=MemoryDimension.INSIGHT,
                memory_k=3,
                knowledge_k=3,
                include_identity=False,
                rerank=False,
                current_frame=self._resolve_current_frame(item),
            )
        except Exception as exc:
            self.logger.warning(
                f"[PersistentAgent] Stagnation LTM recall failed: {exc}",
                component="PersistentAgent",
            )
            return None
        if not (block and block.strip()):
            return None
        self.logger.info(
            f"[{self.current_iteration}][LTM] Stagnation refresh issued "
            f"({len(failure_signatures)} signatures, {len(block):,} chars)",
            component="PersistentAgent",
        )
        return block

    # ── Checklist callbacks ──────────────────────────────────────────────────

    def _handle_skills_added(self, names: List[str]) -> None:
        """Callback: planner activated new skills — inject their bodies.

        Appends a stable user/assistant pair to ``_skill_entries``. The pair
        lives outside the compaction pool (``_turns``), so the skill body
        remains visible to the LLM for the rest of the session. Append-only, so
        the prefix-cache anchor on the most recent ack keeps hitting as new
        skills accrue. ``_injected_skills`` is updated only when a body actually
        rendered, so a skill whose body was momentarily unavailable is not
        permanently marked injected. Each entry tracks the contributing skill
        names so PTL recovery can identify what is being evicted.
        """
        delta = [n for n in names if n and n not in self._injected_skills]
        if not delta:
            return
        try:
            from ..infrastructure.skills import SkillRegistry
            registry = SkillRegistry.get()
        except Exception:
            return
        block = registry.render_active_block(delta)
        if not block.strip():
            return
        self._skill_entries.append({
            "names": tuple(delta),
            "messages": [
                {"role": "user", "content": block},
                {"role": "assistant", "content": "Acknowledged."},
            ],
        })
        self._injected_skills.update(delta)
        prelude_msgs = sum(len(e["messages"]) for e in self._skill_entries)
        prelude_chars = sum(
            len(m["content"]) for e in self._skill_entries for m in e["messages"]
        )
        self.logger.info(
            f"[PersistentAgent] Injected skill bodies: {sorted(delta)} "
            f"(prelude: {prelude_msgs} msgs, {prelude_chars:,} chars)",
            component="PersistentAgent",
        )

    def _handle_tools_added(self, names: List[str]) -> None:
        """Callback: planner activated new tools — load implementations."""
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
        # name (planner typo / not-yet-registered) is silently absent from
        # new_tools — create_all_tool_instances does not raise — so updating
        # _loaded_tools with the raw delta would permanently swallow a later
        # genuine activation of that name. Mirrors the _injected_skills guard
        # in _handle_skills_added.
        loaded = [n for n in delta if n in new_tools]
        if not loaded:
            self.logger.warning(
                f"[PersistentAgent] No tools resolved from activation request "
                f"{delta} — unknown name(s) ignored.",
                component="PersistentAgent",
            )
            return
        self._loaded_tools.update(loaded)
        all_loaded_extra = [name for name in self.tools.keys() if name not in ("read", "write", "edit", "shell", "glob", "grep")]
        try:
            self._api_tools = ToolRegistry.generate_tools_for_api(extra_tool_names=all_loaded_extra)
        except Exception:
            pass
        self.logger.info(f"[PersistentAgent] Tools loaded: +{loaded}", component="PersistentAgent")
