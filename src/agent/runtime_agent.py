"""
Runtime Agent - Runtime Agent Implementation
Implements the Observe-Think-Act closed-loop logic
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, cast

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from ..infrastructure.anthropic_streaming_service import (
    AnthropicStreamingService,
    StreamDoneEvent,
    StreamToolCallEvent,
)
from ..infrastructure.llm_pool import call_with_fallback, call_with_fallback_stream
from ..infrastructure.llm_service import LLMChatResult, LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.progress_checker import ProgressStatus, ProgressAnalyzerBase
from ..models.plan import Step
from ..models.agent_result import AgentResult
from ..models.decision import Decision, ToolCall
from ..models.state import UserConfirmation
from ..models.token_usage import TokenUsage
from ..tools.base_tool import BaseTool, ToolResult
from ..tools.tool_registry import ToolRegistry
from ..infrastructure.execution_recorder import ExecutionRecorder
from ..controller.interaction_manager import InteractionManager
from .runtime_agent_prompts import SYSTEM_PROMPT, COMPACT_OBSERVATION_PROMPT, get_platform_context
from .risk_guard import RiskGuard

def _format_tool_entry(tool_name: str, tool_input: Any, max_len: int = 200) -> str:
    """
    Format a tools_used entry as '<tool_name>: <truncated_input>'.

    The input is stringified and truncated to *max_len* characters.
    A trailing '...' is appended when truncation occurs.
    Falls back to just the tool name when *tool_input* is None/empty.

    Args:
        tool_name:  Canonical tool name (e.g. 'bash', 'read', 'write').
        tool_input: Primary input value for the tool call (str, dict, etc.).
                    For bash this is the command string; for read/write/edit
                    it is the file path.
        max_len:    Maximum number of characters for the input portion.

    Returns:
        A string like ``'bash: ls -la /tmp'`` or
        ``'read: /very/long/path/to/fi...'``.
    """
    if tool_input is None:
        return tool_name
    input_str = str(tool_input).strip()
    if not input_str:
        return tool_name
    if len(input_str) > max_len:
        input_str = input_str[:max_len] + "..."
    return f"{tool_name}: {input_str}"


def _failed_approach_signature(tr: "ToolResult") -> Optional[str]:
    """
    Return a compact, stable signature for a failed ToolResult.

    Used by RuntimeAgent to detect when the same approach is retried after
    already failing.  Returns None for results that should not be tracked
    (infrastructure errors, streaming failures, etc.).

    The signature is intentionally coarse — it captures the tool + primary
    input truncated to 120 chars so that cosmetically different but
    semantically identical commands collide correctly.
    """
    params = tr.tool_parameters or {}
    name = tr.tool_name or ""
    if name == "bash":
        cmd = params.get("command", "").strip()
        return f"bash:{cmd[:120]}" if cmd else None
    if name == "ssh":
        action = params.get("action", "")
        command = params.get("command", "").strip()
        script  = params.get("script_content", "").strip()
        key = command or script[:80]
        return f"ssh:{action}:{key}" if key else (f"ssh:{action}" if action else None)
    if name in ("read", "write", "edit"):
        path = params.get("path", "")
        return f"{name}:{path}" if path else None
    return None



# Checked in RuntimeAgent.run_streaming() to trigger error-analysis recovery.
_LLM_API_ERROR_TAG = "LLM_API_ERROR"

# Tool names that represent infrastructure/meta results rather than retryable
# agent actions.  Excluded from the failed-approach registry in run_streaming()
# so that LLM-level or compaction errors never pollute the anti-repeat guard.
_INFRA_TOOL_NAMES: frozenset = frozenset({
    "llm_stream", "observation_summary", "context_truncation_notice",
})

# Maximum total size (chars) of all serialised observations passed to the LLM.
#
# Budget derivation (model context limit: 200K tokens):
#   Fixed overhead  : ~10K tokens  (system prompt ~3K + tools schema ~5K +
#                                    goal message ~0.5K + next-action prompt ~0.5K +
#                                    assistant reasoning per turn ~1K)
#   Available        : ~190K tokens
#   chars/token      : ~2.5  (conservative for mixed Chinese/code content;
#                             English-only is ~4, Chinese is ~1-2)
#   → 190K × 2.5 = 475K chars  → rounded to 480K
#
# Compaction triggers:
#   _compact_old_observations() (budget_ratio=0.8) : triggered every iteration
#       when total observation chars > 384K (80% of budget).
#   _compact_old_observations() (budget_ratio=0.3) : triggered proactively
#       every 50 iterations when chars > 144K (30%), to prevent gradual growth
#       in long-running tasks before the 80% threshold is reached.
# _build_messages_from_observations() drops oldest pairs as a hard-drop fallback
#       when this limit is exceeded at serialisation time.
_OBS_BUDGET_CHARS: int = 480_000

# Aliases for tool names the LLM may emit instead of the canonical registry names.
# Normalised to the canonical name at the start of act() so that all downstream
# logic (_check_before_act, parameter validation, tool lookup) is unaffected.
_TOOL_NAME_ALIASES: Dict[str, str] = {
    "write_file": "write",
    "read_file": "read",
}


class RuntimeAgent:
    """Runtime Agent - Executes Observe-Think-Act loop"""

    def __init__(
        self,
        llm_services: List[LLMService],
        step: Step,
        from_data_services: Optional[List[LLMService]] = None,
        working_directory: str = ".",
        storage_directory: Optional[str] = None,
        max_iterations: int = 100,
        progress_config: Optional[Dict[str, Any]] = None,
        config_manager: Optional[ConfigManager] = None,
        confirmation_callback: Optional[Callable[[Decision, str], UserConfirmation]] = None,
        check_interrupt_callback: Optional[Callable[[], Optional[str]]] = None,
        execution_recorder: Optional[ExecutionRecorder] = None,
        agent_id: str = "",
        venv_path: Optional[str] = None,
        interaction_manager: Optional[InteractionManager] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        extra_tool_names: Optional[List[str]] = None,
    ):
        if not llm_services:
            raise ValueError("RuntimeAgent requires at least one LLMService in llm_services")
        self._services: List[LLMService] = list(llm_services)

        # from_data services: for mechanical secondary tasks (Decision.from_data JSON
        # extraction, _get_llm_error_explanation). Compaction uses _services directly
        # because summary quality directly affects context window management.
        self._from_data_services: List[LLMService] = (
            from_data_services if from_data_services is not None
            else (self._services[2:] if len(self._services) > 2 else self._services[-1:])
        )

        self.step = step
        self.working_directory = working_directory
        self.storage_directory = storage_directory or working_directory
        self.max_iterations = max_iterations
        self.logger = get_logger()
        self.current_iteration = 0
        self.last_logged_obs_count = 0
        # Full assistant messages (role="assistant") stored for conversation history.
        # Each entry is either:
        #   • a tool-call message  → {"role": "assistant", "content": ..., "tool_calls": [...]}
        #   • a completion message → {"role": "assistant", "content": "..."}
        # Indexed in parallel with observations: _assistant_messages[i] is the
        # decision that produced observations[i].
        self._assistant_messages: List[Dict[str, Any]] = []
        # Tracks how many observations each assistant message produced.
        # For single-tool turns: 1. For parallel turns: N (number of concurrent tools).
        # len(_assistant_messages) == len(_obs_group_sizes) always.
        self._obs_group_sizes: List[int] = []

        self.tools: Dict[str, BaseTool] = ToolRegistry.create_all_tool_instances(
            venv_path=venv_path, extra_tool_names=extra_tool_names
        )
        # Reset file-read tracking so stale-file checks start fresh for this step.
        # FileState is a process-level singleton; without this reset, read records
        # from a previous step would incorrectly satisfy the read-before-write guard.
        from src.tools.file_state import FileState
        FileState.reset_for_session()
        # Inject interrupt_event into BashTool so long-running commands can be
        # killed immediately when the Planner fires an interrupt signal, without
        # waiting for the current tool call to finish naturally.
        if interrupt_event is not None:
            bash = self.tools.get("bash")
            if bash is not None:
                bash.interrupt_event = interrupt_event  # type: ignore[attr-defined]
        # Pre-build the tools list in OpenAI function-calling format once at init time.
        # Passed to every chat_stream() call so the model uses
        # native function-calling instead of JSON-in-content.
        self._api_tools: List[Dict[str, Any]] = ToolRegistry.generate_tools_for_api(
            extra_tool_names=extra_tool_names
        )
        self.progress_analyzer = SuccessPatternAnalyzer(config=progress_config)
        self.config_manager = config_manager or ConfigManager()
        self.risk_guard = RiskGuard(self.config_manager, working_directory=self.working_directory)
        self.confirmation_callback = confirmation_callback
        # Called at the start of every Observe-Think-Act iteration (non-blocking).
        # If it returns a non-empty string the agent aborts immediately and
        # propagates USER_NEW_INSTRUCTION so the FlowController can replan.
        self.check_interrupt_callback = check_interrupt_callback

        # Execution record persistence.  Optional — when None, recording is skipped.
        self.execution_recorder = execution_recorder
        # agent_id used for recorder labels; falls back to step_id for sequential steps.
        self.agent_id = agent_id or step.step_id

        # UI interaction manager — used to forward tool output to the TUI.
        # Falls back to the singleton if not explicitly provided.
        try:
            self._interaction_manager: Optional[InteractionManager] = (
                interaction_manager or InteractionManager.get_instance()
            )
        except RuntimeError:
            # No singleton registered (e.g. in tests without a full app setup).
            self._interaction_manager = interaction_manager

        self.logger.info("RuntimeAgent initialized successfully", component="RuntimeAgent")

        # Failed-approach registry: maps approach signature → failure count.
        # Populated in run_streaming() after each tool call.  Used to inject
        # an ANTI-REPEAT GUARD reminder when the agent repeats a failing approach.
        self._failed_approaches: Dict[str, int] = {}

        # In-flight tool calls: tracks currently executing tools so that
        # get_progress_summary() can report what is running RIGHT NOW, even
        # before the tool completes and an observation is recorded.
        self._in_flight_tools: List[Dict[str, Any]] = []

    def get_progress_summary(self, max_chars: int = 5000) -> str:
        """
        Return a compact summary of the agent's current execution state.

        Called by FlowController during replan so the Planner can see what the
        in-flight agent has already tried — preventing blind replanning.

        The summary is intentionally brief (bounded by max_chars) to avoid
        bloating the Planner's context window.  It covers:
          • iteration count and step description
          • currently executing tool calls (in-flight, not yet completed)
          • last few completed tool calls with their success/failure status

        This method is read-only and thread-safe: it only reads existing
        observation data that the agent loop has already written.
        """
        observations = self.step.get_all_observations()
        in_flight = self._in_flight_tools

        if not observations and not in_flight:
            return (
                f"[In-flight step '{self.step.description}'] "
                f"No tool calls executed yet (iteration {self.current_iteration})."
            )

        lines = [
            f"[In-flight step '{self.step.description}'] "
            f"Iteration {self.current_iteration}, "
            f"{len(observations)} tool call(s) completed so far."
        ]

        # Show currently executing tools first (most relevant for progress queries)
        if in_flight:
            lines.append("  Currently executing:")
            for entry in in_flight:
                tool = entry.get("tool_name", "unknown")
                snippet = entry.get("snippet", "")
                lines.append(f"    ⟳ {tool}: {snippet}")

        # Show last 5 completed tool calls
        if observations:
            recent = observations[-5:]
            lines.append("  Recent completed:")
            for obs in recent:
                status = "OK" if obs.success else "FAIL"
                tool = obs.tool_name or "unknown"
                if obs.success:
                    out = str(obs.output or "")
                    snippet = out[:120].replace("\n", " ")
                    if len(out) > 120:
                        snippet += "..."
                    lines.append(f"    {status} {tool}: {snippet}")
                else:
                    err = str(obs.error or obs.output or "")
                    snippet = err[:120].replace("\n", " ")
                    if len(err) > 120:
                        snippet += "..."
                    lines.append(f"    {status} {tool}: {snippet}")

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n  ...(truncated)"
        return result

    @staticmethod
    def _tool_call_snippet(tc) -> str:
        """Build a short human-readable snippet for an in-flight tool call."""
        params = tc.parameters or {}
        name = tc.tool_name
        if name == "bash":
            cmd = params.get("command", "")
            return cmd[:150] if cmd else "(no command)"
        if name == "ssh":
            action = params.get("action", "")
            command = params.get("command", "")
            return f"{action}: {command[:100]}" if command else action
        if name in ("read", "write", "edit"):
            path = params.get("path", "")
            return path[:150] if path else "(no path)"
        if name == "glob":
            return params.get("pattern", "")[:100]
        if name == "grep":
            return params.get("pattern", "")[:100]
        # Generic: show first param value
        first_val = next(iter(params.values()), "") if params else ""
        return str(first_val)[:100]

    def observe(self, tool_result: ToolResult | None = None) -> List[ToolResult]:
        """
        Store a ToolResult in the step and return all current observations.
        """
        if tool_result is not None:
            self.step.add_observation(tool_result)

        observations = self.step.get_all_observations()

        new_observations = observations[self.last_logged_obs_count:]
        if new_observations:
            for idx, obs in enumerate(new_observations, self.last_logged_obs_count + 1):
                output_str = str(obs.output)
                self.logger.info(
                    f"[{self.current_iteration}][Observe] #{idx} Tool={obs.tool_name}, "
                    f"Success={obs.success}, Output(len={len(output_str)}): {output_str[:500]}...",
                    component="RuntimeAgent"
                )
            self.last_logged_obs_count = len(observations)

        return observations

    async def _get_llm_error_explanation(self, error_msg: str) -> Optional[str]:
        """
        Ask the LLM to explain an error from a previous failed LLM call.
        Uses plain-text mode (json_mode=False) to avoid the same failure mode.
        Returns the explanation, or None if this call also fails.
        """
        # Notify UI: waiting for LLM response (shows animation)
        if self._interaction_manager:
            try:
                self._interaction_manager.notify_state_changed("thinking")
            except Exception:
                pass

        try:
            result = cast(LLMChatResult, await call_with_fallback(
                self._from_data_services,
                dict(
                    messages=[{
                        "role": "user",
                        "content": (
                            "An error occurred when I submitted a request to you:\n\n"
                            f"Error: {error_msg}\n\n"
                            "Briefly explain what this error means and what likely caused it."
                        ),
                    }],
                    json_mode=False,
                ),
            ))
            # Notify UI: LLM response received, back to executing
            if self._interaction_manager:
                try:
                    self._interaction_manager.notify_state_changed("executing")
                except Exception:
                    pass
            return result.content
        except Exception as explain_err:
            # Notify UI: LLM response received (error), back to executing
            if self._interaction_manager:
                try:
                    self._interaction_manager.notify_state_changed("executing")
                except Exception:
                    pass
            self.logger.warning(
                f"[{self.current_iteration}][LLMErrorAnalysis] "
                f"Could not obtain explanation: {explain_err}",
                component="RuntimeAgent",
            )
            return None

    async def _compact_old_observations(self, budget_ratio: float = 0.8) -> None:
        """Summarise old observations when the budget exceeds *budget_ratio*.

        Passing a lower budget_ratio (e.g. 0.3) from the proactive periodic
        compact in run_streaming() forces early compaction for long-running
        tasks, preventing the context from silently growing large over hundreds
        of iterations before the normal 80% trigger fires.

        The minimum observation count guard (KEEP_RECENT + 5) ensures we never
        compact a nearly-empty history regardless of budget_ratio.
        """
        observations = self.step.get_all_observations()
        if not observations:
            return

        total_chars = sum(
            len(obs.to_obs_json(i + 1))
            for i, obs in enumerate(observations)
        )
        # Never compact a nearly-empty history — not worth the LLM call overhead.
        KEEP_RECENT = 10
        if len(observations) <= KEEP_RECENT + 5:
            return
        if total_chars <= _OBS_BUDGET_CHARS * budget_ratio:
            return
        old_obs = observations[:-KEEP_RECENT] if len(observations) > KEEP_RECENT else []
        if not old_obs:
            return

        self.logger.info(
            f"[{self.current_iteration}][Compact] Observation budget at "
            f"{total_chars / _OBS_BUDGET_CHARS:.0%}; summarising "
            f"{len(old_obs)} old observation(s).",
            component="RuntimeAgent",
        )

        # Build a structured representation that includes tool parameters so the
        # LLM can identify duplicate / superseded observations (e.g. two reads of
        # the same file, or a read that was later overwritten).
        obs_entries: list[str] = []
        for seq, obs in enumerate(old_obs, 1):
            status = "OK" if obs.success else "FAIL"
            params = obs.tool_parameters or {}
            # Derive a compact parameter annotation
            if obs.tool_name == "bash":
                cmd = params.get("command", "").strip()
                param_ann = f" cmd={cmd[:120]!r}" if cmd else ""
            elif obs.tool_name in ("read", "write", "edit"):
                path = params.get("path", "")
                param_ann = f" path={path!r}" if path else ""
            elif obs.tool_name == "ssh":
                action = params.get("action", "")
                cmd = params.get("command", "").strip()
                param_ann = f" action={action!r} cmd={cmd[:80]!r}" if cmd else (f" action={action!r}" if action else "")
            else:
                param_ann = ""
            content = (str(obs.output)[:600] if obs.success else str(obs.error)[:600])
            obs_entries.append(f"[{seq}] {obs.tool_name}{param_ann} → {status}:\n{content}")

        obs_text = "\n\n".join(obs_entries)
        summary_prompt = COMPACT_OBSERVATION_PROMPT.format(obs_text=obs_text)

        summary_text: Optional[str] = None
        try:
            result = cast(LLMChatResult, await call_with_fallback(
                self._services,
                dict(
                    messages=[{"role": "user", "content": summary_prompt}],
                    json_mode=False,
                ),
            ))
            summary_text = result.content
        except Exception as e:
            self.logger.warning(
                f"[{self.current_iteration}][Compact] Summary LLM call failed: {e}; "
                "falling back to dropping old observations.",
                component="RuntimeAgent",
            )

        summary_obs = ToolResult(
            success=True,
            output=(
                f"[Compressed observation history ({len(old_obs)} earlier steps merged)]\n"
                + (summary_text or "(summary unavailable — observations dropped)")
            ),
            tool_name="observation_summary",
            tool_parameters={},
        )

        # Replace old observations with the summary in the step's observation list.
        # We rebuild _assistant_messages and _obs_group_sizes to stay aligned.
        keep_count = min(KEEP_RECENT, len(observations))
        kept_obs = observations[-keep_count:]

        # Clear and re-add: summary first, then kept observations
        self.step.clear_observations()
        self.step.add_observation(summary_obs)
        for obs in kept_obs:
            self.step.add_observation(obs)

        # Trim parallel tracking lists to match kept assistant messages.
        # Slice by group count (not observation count) so that parallel turns
        # — which contribute more than one observation per group — stay aligned.
        # Walk from the end, accumulating group sizes until keep_count obs are covered.
        accum = 0
        kept_groups = 0
        for gs in reversed(self._obs_group_sizes):
            accum += max(gs, 1)  # group_size=0 counts as 1 obs (summary placeholder)
            kept_groups += 1
            if accum >= keep_count:
                break
        self._assistant_messages = self._assistant_messages[-kept_groups:]
        self._obs_group_sizes = self._obs_group_sizes[-kept_groups:]
        # Prepend a dummy entry for the summary observation (plain-text, group_size=0
        # so _build_messages_from_observations treats it as a user-role message).
        self._assistant_messages.insert(0, {
            "role": "assistant",
            "content": "[Compacted earlier observations into summary below.]",
        })
        self._obs_group_sizes.insert(0, 0)

        self.last_logged_obs_count = 0  # reset so new observations are logged
        self.logger.info(
            f"[{self.current_iteration}][Compact] Done: {len(old_obs)} obs → 1 summary + "
            f"{keep_count} recent obs kept.",
            component="RuntimeAgent",
        )

    def _build_messages_from_observations(
        self,
        goal: str,
        observations: List[ToolResult],
        reminder: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Build the LLM message list for the OpenAI function-calling multi-turn format.

        Layout (indices are stable across iterations):
          [0]        system    : SYSTEM_PROMPT
                                 — never changes; always a cache hit after the first call.
          [1]        user      : "Goal: …\n\nWorking directory: …"
                                 — fixed for the lifetime of this RuntimeAgent instance.
          [2 .. N-1] assistant : stored assistant message from the previous iteration
                                 (either a tool-call message or a plain content message)
                     tool/user : paired observation result
                                 — tool role when the assistant issued a tool call,
                                   user role otherwise (plain-text completion path).
                                 — append-only pairs; existing messages are never mutated,
                                   so every previously-seen prefix remains a cache hit.
          [N]        user      : next-action prompt (optionally prefixed with reminder)
                                 — always the last message; the only one that may vary.

        KV-cache guarantees:
          • No timestamps anywhere in the serialised form.
          • Observation JSON keys are in a fixed order (step→tool→params→ok→out|err).
          • params sub-keys are sorted alphabetically.
          • Large string values are truncated to a fixed maximum length so the
            serialised form does not grow unboundedly.
        """
        # ── [0] System prompt — stable prefix ────────────────────────────────
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # ── [1] Goal + working directory — stable for this step ──────────────
        goal_content = (
            f"Goal: {goal}\n\n"
            f"Working directory: {self.working_directory}\n"
            f"Session storage directory: {self.storage_directory}\n\n"
            f"{get_platform_context()}"
        )
        if self.step.expected_outcomes:
            items = "\n".join(f"  - {e}" for e in self.step.expected_outcomes)
            goal_content += (
                f"\n\nPlanner's expected outcomes (reference only — goal takes priority):\n"
                f"{items}\n"
                f"These are the planner's predictions of what success looks like. "
                f"Focus on completing the goal above. If execution reveals that an "
                f"expectation is wrong or inapplicable, proceed with what actually works "
                f"and report the deviation with an explanation in key_findings."
            )
        messages.append({
            "role": "user",
            "content": goal_content,
        })

        # ── [2..N-1] Alternating assistant / tool-or-user pairs ──────────────
        # self._assistant_messages[i] is the decision that produced the
        # observation group starting at the offset tracked by _obs_group_sizes.
        #
        # For single-tool turns: group_size=1 → one tool-result message.
        # For parallel turns:    group_size=N → N tool-result messages (one per call_id).
        # For plain-text turns:  group_size=0 → one user-role message (no tool call).
        #
        # Context budget guard: if the total serialised size of all pairs exceeds
        # _OBS_BUDGET_CHARS, the oldest pairs are dropped together to preserve alignment.
        # Dropping (not truncating) preserves the integrity of each remaining pair.

        # Build a flat list of (obs_idx, full_json, slim_json) aligned with
        # assistant messages, respecting group sizes.
        obs_list = list(enumerate(observations, 1))  # (1-based-idx, obs)
        group_sizes = self._obs_group_sizes[:len(self._assistant_messages)]
        assistant_msgs = self._assistant_messages[:len(group_sizes)]

        # Compute per-assistant-message observation slices
        # Each entry: (asst_msg, [(obs_idx, full_json, slim_json), ...])
        paired: List[tuple] = []
        obs_cursor = 0
        for i, (asst_msg, gsize) in enumerate(zip(assistant_msgs, group_sizes)):
            if gsize == 0:
                # Plain-text turn: no observation consumed
                paired.append((asst_msg, []))
            else:
                group_obs = obs_list[obs_cursor:obs_cursor + gsize]
                obs_cursor += gsize
                paired.append((asst_msg, [
                    (idx, obs.to_obs_json(idx), obs.to_tool_result_json())
                    for idx, obs in group_obs
                ]))

        # Budget: sum all serialised sizes of history pairs.
        #
        # We do NOT include the fixed overhead (system prompt, tools schema, goal
        # message, next-action prompt) in this sum because those are constant and
        # already accounted for in the _OBS_BUDGET_CHARS derivation (see constant
        # definition above).  The budget here covers only the variable history.
        total_obs_chars = sum(
            sum(len(slim_j if asst_msg.get("tool_calls") else full_j)
                for _, full_j, slim_j in obs_group)
            + len(json.dumps(asst_msg))
            for asst_msg, obs_group in paired
        )

        dropped_count = 0
        if total_obs_chars > _OBS_BUDGET_CHARS and len(paired) > 1:
            # Drop oldest pairs until we are below 70% of the budget.
            # Dropping to 70% (rather than just below 100%) gives headroom for
            # the current iteration's new observations before the next compaction.
            target = int(_OBS_BUDGET_CHARS * 0.70)
            running = total_obs_chars
            while running > target and dropped_count < len(paired) - 1:
                _, obs_group = paired[dropped_count]
                pair_size = (
                    sum(len(slim_j if paired[dropped_count][0].get("tool_calls") else full_j)
                        for _, full_j, slim_j in obs_group)
                    + len(json.dumps(paired[dropped_count][0]))
                )
                running -= pair_size
                dropped_count += 1
            paired = paired[dropped_count:]

        if dropped_count > 0:
            # Find the first kept observation index for the notice message
            first_kept_obs_idx = 1
            for _, obs_group in paired:
                if obs_group:
                    first_kept_obs_idx = obs_group[0][0]
                    break
            self.logger.warning(
                f"[{self.current_iteration}][BuildMessages] Observation budget exceeded: "
                f"dropped oldest {dropped_count} pair(s) "
                f"(steps 1\u2013{first_kept_obs_idx - 1}), keeping {len(paired)}.",
                component="RuntimeAgent",
            )
            messages.append({
                "role": "user",
                "content": (
                    f"[Context budget notice: oldest {dropped_count} step(s) "
                    f"(steps 1\u2013{first_kept_obs_idx - 1}) were dropped to stay within "
                    f"the context limit. "
                    f"Only the most recent {len(paired)} step(s) are shown.]"
                ),
            })

        for asst_msg, obs_group in paired:
            messages.append(asst_msg)
            if asst_msg.get("tool_calls"):
                if len(obs_group) == 0:
                    # Tool call with no observation yet — inject a synthetic error
                    # tool_result for every tool_use so the API never sees an unpaired
                    # tool_use block (Bedrock rejects such histories with 400).
                    for tc in asst_msg["tool_calls"]:
                        tc_id = tc.get("id", "")
                        messages.append({
                            "role": "tool",
                            "content": "Tool execution was interrupted before a result was produced.",
                            "tool_call_id": tc_id,
                        })
                elif len(obs_group) == 1:
                    # Single tool call — standard path
                    _, _, slim_json_str = obs_group[0]
                    tool_call_id = asst_msg["tool_calls"][0].get("id", "")
                    messages.append({
                        "role": "tool",
                        "content": slim_json_str,
                        "tool_call_id": tool_call_id,
                    })
                else:
                    # Parallel tool calls — emit one tool-result message per call
                    api_tool_calls = asst_msg["tool_calls"]
                    for j, (_, _, slim_json_str) in enumerate(obs_group):
                        call_id = api_tool_calls[j].get("id", "") if j < len(api_tool_calls) else ""
                        messages.append({
                            "role": "tool",
                            "content": slim_json_str,
                            "tool_call_id": call_id,
                        })
            else:
                # Plain-text assistant message — emit user-role observation
                if obs_group:
                    _, full_json_str, _ = obs_group[0]
                    messages.append({
                        "role": "user",
                        "content": full_json_str,
                    })
                # If no observation (group_size=0), nothing to append

        # ── [N] Next-action prompt — always last ──────────────────────────────
        next_action = "What is the next action?"
        if reminder:
            next_action = f"{reminder}\n\n{next_action}"

        # Guard against consecutive user messages.
        #
        # On the first iteration there are no observation pairs yet, so the
        # message list ends with the goal message [1] — a user message.
        # Appending another user message ("What is the next action?") creates
        # two back-to-back user messages with no assistant turn in between.
        # When using function-calling, the last message after a tool result is
        # a "tool" role message, so we always append a fresh user message then.
        last_role = messages[-1]["role"]
        if last_role == "user":
            messages[-1] = {
                **messages[-1],
                "content": messages[-1]["content"] + "\n\n" + next_action,
            }
        else:
            messages.append({"role": "user", "content": next_action})

        return messages

    def _check_before_act(self, tc: ToolCall) -> Optional[UserConfirmation]:
        """
        Check if confirmation is needed before executing a single ToolCall.

        Accepts a ToolCall (not a Decision) so it can be called per-tool
        when act() executes multiple tools concurrently.

        Unified confirmation_callback contract
        ---------------------------------------
        A single confirmation_callback handles both high-risk and tool-specific
        confirmations.  Signature: (decision, context_str) -> UserConfirmation,
        where context_str is the risk description for high-risk operations and
        the tool name for tool-specific operations.

        "Other input" handling (user typed something other than yes/no):
          • High-risk path  → converted to UserConfirmation.risk_guidance() here
            so the agent re-thinks within the current step using the guidance.
          • Tool-specific path → the FlowController wrapper converts it to
            UC.no() and queues the message for Planner evaluation; the agent
            re-thinks within the step via the rejection result.
        """
        tool_name = tc.tool_name
        # Build a minimal Decision for the confirmation_callback contract
        # (callback only reads tool_name and parameters from it).
        _decision_proxy = Decision(
            reasoning="",
            tool_calls=[tc],
        )

        # Check 1: High-risk operation (highest priority)
        if self.risk_guard.is_high_risk(_decision_proxy):
            self.logger.warning(
                f"[{self.current_iteration}][BeforeAct] High-risk operation detected",
                component="RuntimeAgent"
            )
            if self.config_manager.is_auto_approve_enabled("high_risk"):
                return None
            if self.confirmation_callback:
                risk_description = self.risk_guard.get_risk_description(_decision_proxy)
                confirmation = self.confirmation_callback(_decision_proxy, risk_description)
                if confirmation.has_new_message():
                    guidance = confirmation.message or ""
                    self.logger.info(
                        f"[{self.current_iteration}][BeforeAct] Risk dialog: user provided "
                        f"guidance (will handle within step): {guidance[:80]}...",
                        component="RuntimeAgent",
                    )
                    return UserConfirmation.risk_guidance(guidance)
                return confirmation
            else:
                self.logger.warning(
                    f"[{self.current_iteration}][BeforeAct] No confirmation callback, auto-rejecting",
                    component="RuntimeAgent"
                )
                return UserConfirmation.no()

        # Check 1.5: Auto-approve write/edit when the target path is inside the
        # working directory.
        if tool_name in ("write", "edit"):
            file_path = tc.parameters.get("path", "")
            if file_path and self.risk_guard.is_path_within_working_dir(file_path):
                self.logger.debug(
                    f"[{self.current_iteration}][BeforeAct] {tool_name} path inside "
                    f"working dir, auto-approved: {file_path}",
                    component="RuntimeAgent",
                )
                return None

        # Check 2-4: Tool-specific switches (write/edit/bash)
        tool_switch_map = {"write": "tool_write", "edit": "tool_edit", "bash": "tool_bash"}
        if tool_name in tool_switch_map:
            switch_name = tool_switch_map[tool_name]
            if self.config_manager.is_auto_approve_enabled(switch_name):
                return None
            if self.confirmation_callback:
                return self.confirmation_callback(_decision_proxy, tool_name)
            else:
                self.logger.warning(
                    f"[{self.current_iteration}][BeforeAct] No confirmation callback, auto-rejecting",
                    component="RuntimeAgent"
                )
                return UserConfirmation.no()

        return None

    def _validate_tool_parameters(
        self, tool_name: str, parameters: Dict[str, Any]
    ) -> Optional[str]:
        """
        Validate *parameters* against the tool's registered JSON schema.

        Returns an actionable error string if unexpected parameters are present,
        or None when the parameters are valid.

        This is the primary guard against the silent-truncation bug where the
        LLM splits file content across multiple keyword arguments (e.g.
        "严重程度", "建议", "影响" …).  Without this check those extra kwargs
        are absorbed by **kwargs in tool.execute() and silently ignored, so the
        tool returns success=True while writing only a tiny fragment of the
        intended content.
        """
        try:
            metadata = ToolRegistry.get_tool_metadata(tool_name)
        except KeyError:
            return None  # Unknown tool — handled separately in act()

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
                f"unexpected parameter(s) received: {extra_list}.\n"
                f"This tool ONLY accepts: {allowed_list} "
                f"(required: {required_list}).\n"
                f"ALL content must be placed inside the correct parameter(s). "
                f"Do NOT pass content sections as extra parameters — "
                f"combine everything into a single value for the appropriate parameter "
                f"and retry."
            )

        missing = required - set(parameters.keys())
        if missing:
            missing_list = ", ".join(f"'{p}'" for p in sorted(missing))
            return (
                f"Parameter error for tool '{tool_name}': "
                f"missing required parameter(s): {missing_list}."
            )

        return None

    async def _execute_one(self, tc: ToolCall) -> ToolResult:
        """
        Execute a single ToolCall: check → validate → run tool → notify UI.

        Returns a ToolResult in all cases (never raises).
        """
        tool_name = tc.tool_name
        parameters = tc.parameters

        # Normalise tool name aliases
        if tool_name in _TOOL_NAME_ALIASES:
            canonical = _TOOL_NAME_ALIASES[tool_name]
            self.logger.info(
                f"[{self.current_iteration}][Act] Tool alias '{tool_name}' → '{canonical}'",
                component="RuntimeAgent",
            )
            tool_name = canonical
            tc = ToolCall(call_id=tc.call_id, tool_name=canonical, parameters=parameters)

        # Confirmation check
        confirmation = self._check_before_act(tc)
        if confirmation:
            if confirmation.is_approved():
                pass
            elif confirmation.is_rejected():
                return ToolResult(
                    success=False, output=None, error="User rejected operation",
                    tool_name=tool_name, tool_parameters=parameters,
                )
            elif confirmation.is_risk_guidance():
                self.logger.info(
                    f"[{self.current_iteration}][Act] Risk guidance injected as observation.",
                    component="RuntimeAgent",
                )
                return ToolResult(
                    success=False, output=None,
                    error=f"High-risk operation was not executed. User guidance: {confirmation.message}",
                    tool_name=tool_name, tool_parameters=parameters,
                )
            else:  # has_new_message
                return ToolResult(
                    success=False, output=confirmation.message,
                    error="USER_NEW_INSTRUCTION",
                    tool_name=tool_name, tool_parameters=parameters,
                )

        if not tool_name or tool_name not in self.tools:
            available = sorted(self.tools.keys())
            error_msg = (
                f"Unknown tool: '{tool_name}'. "
                f"Available tools: {available}"
            )
            self.logger.error(f"[{self.current_iteration}][Act] {error_msg}", component="RuntimeAgent")
            return ToolResult(success=False, output=None, error=error_msg,
                              tool_name=tool_name or "unknown", tool_parameters=parameters)

        param_error = self._validate_tool_parameters(tool_name, parameters)
        if param_error:
            self.logger.warning(
                f"[{self.current_iteration}][Act] Param validation failed for '{tool_name}': {param_error[:200]}",
                component="RuntimeAgent",
            )
            return ToolResult(success=False, output=None, error=param_error,
                              tool_name=tool_name, tool_parameters=parameters)

        tool = self.tools[tool_name]

        # Notify UI: before execution
        try:
            if self._interaction_manager is not None:
                truncated_params: Optional[Dict[str, Any]] = (
                    {k: (str(v)[:100] + "..." if len(str(v)) > 100 else str(v)) for k, v in parameters.items()}
                    if parameters else None
                )
                self._interaction_manager.notify_tool_execution_started(
                    self.current_iteration, tool_name, truncated_params, None)
        except Exception as e:
            self.logger.error(f"[{self.current_iteration}][Act] Pre-exec UI notify error: {e}",
                              component="RuntimeAgent", exc_info=True)

        result: ToolResult = await tool.execute(**parameters)

        # Backfill context fields
        if not result.tool_name:
            result.tool_name = tool_name
        if result.tool_parameters is None:
            result.tool_parameters = parameters

        # Notify UI: after execution
        try:
            if self._interaction_manager is not None:
                output: Optional[Dict[str, Any]] = None
                if result.output:
                    output = {
                        k: (str(v)[:100] + "..." if len(str(v)) > 100 else str(v))
                        for k, v in result.output.items()
                        if not (tool_name.lower() == "bash" and k == "command")
                    }
                self._interaction_manager.notify_tool_execution_started(
                    self.current_iteration, None, None, output)
        except Exception as e:
            self.logger.error(f"[{self.current_iteration}][Act] Post-exec UI notify error: {e}",
                              component="RuntimeAgent", exc_info=True)

        return result

    async def act(self, decision: Decision) -> List[ToolResult]:
        """
        Execute all tool calls in the decision, respecting concurrency safety.

        Single tool call  → runs directly, returns [result].
        Multiple tool calls → partitioned into concurrent-safe batches and
                              serial batches; concurrent batches run via
                              asyncio.gather, serial batches run sequentially.

        Always returns a list so run() has a single unified code path.
        """
        if not decision.tool_calls:
            return []
        if len(decision.tool_calls) == 1:
            return [await self._execute_one(decision.tool_calls[0])]

        batches = self._partition_tool_calls(decision.tool_calls)
        # Log only when there are multiple batches (i.e. mixed safe/unsafe calls)
        if len(batches) > 1:
            self.logger.info(
                f"[{self.current_iteration}][Act] Partitioned into {len(batches)} batch(es): "
                + ", ".join(
                    f"[{'+'.join(tc.tool_name for tc in b['calls'])}]"
                    f"({'concurrent' if b['concurrent'] else 'serial'})"
                    for b in batches
                ),
                component="RuntimeAgent",
            )
        else:
            self.logger.info(
                f"[{self.current_iteration}][Act] Parallel: {[tc.tool_name for tc in decision.tool_calls]}",
                component="RuntimeAgent",
            )

        all_results: List[ToolResult] = []
        for batch in batches:
            if batch["concurrent"]:
                results = list(await asyncio.gather(
                    *[self._execute_one(tc) for tc in batch["calls"]],
                    return_exceptions=False,
                ))
            else:
                results = []
                for tc in batch["calls"]:
                    results.append(await self._execute_one(tc))
            all_results.extend(results)
        return all_results

    def _partition_tool_calls(self, tool_calls: List[ToolCall]) -> List[dict]:
        """Partition tool calls into concurrent-safe and serial batches.

        Rules:
        - Consecutive concurrency-safe calls are merged into one concurrent batch.
        - Any non-safe call starts a new serial batch (size 1).
        - write/edit calls targeting different files are treated as concurrency-safe
          (they do not share state); write/edit calls targeting the same file are
          serialised (second call waits for the first to finish).

        This mirrors Claude Code's partitionToolCalls() logic.
        """
        batches: List[dict] = []
        current_batch: dict = {"concurrent": True, "calls": []}
        # Track paths already claimed by write/edit in the current concurrent batch.
        current_batch_write_paths: set = set()

        for tc in tool_calls:
            is_safe = self._is_concurrency_safe_call(tc)

            # write/edit: safe unless the same path is already in the current batch
            if tc.tool_name in ("write", "edit"):
                path = tc.parameters.get("path", "")
                if path and path in current_batch_write_paths:
                    # Path conflict — flush current batch and start a serial one
                    if current_batch["calls"]:
                        batches.append(current_batch)
                    current_batch = {"concurrent": False, "calls": [tc]}
                    current_batch_write_paths = {path} if path else set()
                    continue
                # Different path — treat as safe
                is_safe = True

            if is_safe and current_batch["concurrent"]:
                current_batch["calls"].append(tc)
                if tc.tool_name in ("write", "edit"):
                    path = tc.parameters.get("path", "")
                    if path:
                        current_batch_write_paths.add(path)
            else:
                if current_batch["calls"]:
                    batches.append(current_batch)
                current_batch = {"concurrent": is_safe, "calls": [tc]}
                current_batch_write_paths = set()
                if is_safe and tc.tool_name in ("write", "edit"):
                    path = tc.parameters.get("path", "")
                    if path:
                        current_batch_write_paths.add(path)

        if current_batch["calls"]:
            batches.append(current_batch)

        return batches

    def _is_concurrency_safe_call(self, tc: ToolCall) -> bool:
        """Return True if this tool call is safe to run concurrently with other safe calls.

        - bash: reads the model-annotated ``concurrent_safe`` parameter (bool, default False).
          The model sets this to True when it knows the command is read-only (grep, find, etc.).
        - write/edit: dynamically safe when the target path differs from all other write/edit
          calls in the same batch (compared by the caller via _partition_tool_calls).
          Returns True here; path-collision detection happens in _partition_tool_calls.
        - All other tools: delegates to the tool's ``is_concurrency_safe`` class attribute.
        """
        tool = self.tools.get(tc.tool_name)
        if tool is None:
            return False
        if tc.tool_name == "bash":
            # Model-annotated: True only when the model explicitly marks the command safe.
            return bool(tc.parameters.get("concurrent_safe", False))
        return bool(getattr(tool, "is_concurrency_safe", False))

    def _record_agent_end(self, agent_result: "AgentResult", tools_used: Optional[List[str]] = None) -> None:
        """Write the step-end record to the execution recorder (if active).

        Uses agent_result directly rather than self.step because step.factual_outcome /
        step.artifacts / step.key_findings are assigned by FlowController AFTER
        run_streaming() returns — they are empty at the point this method is called.
        """
        if self.execution_recorder:
            self.execution_recorder.write_agent_end(
                agent_id=self.agent_id,
                step_id=self.step.step_id,
                success=agent_result.success,
                goal=self.step.goal,
                factual_outcome=list(agent_result.factual_outcome),
                artifacts=list(agent_result.artifacts),
                key_findings=list(agent_result.key_findings),
                issues=[agent_result.error] if agent_result.error else [],
                tools_used=tools_used or [],
            )

    async def _think_streaming(
        self,
        goal: str,
        observations: List[ToolResult],
        reminder: Optional[str],
    ) -> tuple[Decision, List[ToolResult], TokenUsage]:
        """
        Internal streaming Think+Act step used by run_streaming().

        Opens a streaming request via call_with_fallback_stream() (which tries
        each service in priority order and falls back before the stream starts),
        dispatches each tool call as an asyncio.Task the moment its
        content_block_stop fires, then awaits all tasks and returns
        (decision, tool_results).

        Concurrency rules mirror _partition_tool_calls() / act():
          - concurrent-safe tools start immediately (asyncio.Task, no prereqs)
          - non-concurrent tools wait for ALL currently running tasks first

        Fallback / error policy
        -----------------------
        - Open-stream failure (service.chat() raises before any event):
            call_with_fallback_stream() tries the next service automatically.
            PTL and 4xx errors fast-fail as usual.
        - Mid-stream error (exception while iterating events):
            Returned as an error ToolResult so run_streaming() can pass it as
            an observation to the next iteration.  No service switch happens
            because events have already been partially delivered.
        - No StreamDoneEvent (malformed stream):
            Same as mid-stream error — returned as an error ToolResult.
        """
        messages = self._build_messages_from_observations(goal, observations, reminder)

        if self._interaction_manager:
            try:
                self._interaction_manager.notify_state_changed("thinking")
            except Exception:
                pass

        # ── Open the stream with automatic pre-stream fallback ────────────────
        chat_kwargs = dict(
            messages=messages,
            tools=self._api_tools,
            json_mode=False,
        )

        async def _run_after(tc: ToolCall, prereqs: List[asyncio.Task]) -> ToolResult:
            """Wait for *prereqs* to finish, then execute *tc*."""
            if prereqs:
                await asyncio.gather(*prereqs, return_exceptions=True)
            return await self._execute_one(tc)

        # ── Mid-stream fallback retry loop ────────────────────────────────────
        # service_offset: how many leading services to skip on the next attempt.
        # Incremented when a mid-stream error occurs before any tool is dispatched
        # (safe to retry because no partial state has been committed).
        service_offset = 0

        while True:
            services_slice = self._services[service_offset:]
            if not services_slice:
                # All services exhausted by mid-stream retries.
                _err = "All LLM services exhausted (mid-stream retries failed)"
                self.logger.error(
                    f"[{self.current_iteration}][ThinkStream] {_err}",
                    component="RuntimeAgent",
                )
                return Decision(
                    reasoning="All LLM services failed.",
                    error=f"{_LLM_API_ERROR_TAG}: {_err}",
                ), [], TokenUsage()

            stream_gen = call_with_fallback_stream(
                services_slice,
                chat_kwargs,
                on_fallback=lambda idx, e: self.logger.warning(
                    f"[{self.current_iteration}][ThinkStream] open-stream fallback "
                    f"to service index {service_offset + idx}: {type(e).__name__}: {e}",
                    component="RuntimeAgent",
                ),
            )

            # ── Streaming dispatch state (reset per attempt) ──────────────────
            running_tasks: List[tuple] = []   # (ToolCall, asyncio.Task)
            ordered_tasks: List[tuple] = []
            dispatched_write_paths: set = set()
            stream_tool_calls: List[ToolCall] = []
            api_tool_calls_for_msg: List[Dict[str, Any]] = []
            decision: Optional[Decision] = None

            # ── Consume stream events ─────────────────────────────────────────
            _stream_error: Optional[Exception] = None
            try:
                async for event in stream_gen:
                    if isinstance(event, StreamToolCallEvent):
                        tc = ToolCall(
                            call_id=event.call_id,
                            tool_name=event.tool_name,
                            parameters=event.args,
                        )
                        stream_tool_calls.append(tc)
                        api_tool_calls_for_msg.append({
                            "id": event.call_id,
                            "function": {
                                "name": event.tool_name,
                                "arguments": json.dumps(event.args),
                            },
                        })

                        # Determine concurrency safety (mirrors _partition_tool_calls)
                        is_safe = self._is_concurrency_safe_call(tc)
                        if tc.tool_name in ("write", "edit"):
                            path = tc.parameters.get("path", "")
                            if path and path in dispatched_write_paths:
                                # Same-path conflict → must wait for all running tasks
                                is_safe = False
                            else:
                                is_safe = True
                                if path:
                                    dispatched_write_paths.add(path)

                        if is_safe:
                            prereqs: List[asyncio.Task] = []
                        else:
                            # Non-concurrent: wait for every currently running task
                            prereqs = [t for _, t in running_tasks if not t.done()]

                        task = asyncio.create_task(_run_after(tc, prereqs))
                        running_tasks.append((tc, task))
                        ordered_tasks.append((tc, task))

                        # Track in-flight for real-time progress visibility
                        _snippet = self._tool_call_snippet(tc)
                        self._in_flight_tools.append({
                            "tool_name": tc.tool_name,
                            "snippet": _snippet,
                            "call_id": event.call_id,
                        })

                        # Write dispatch record for real-time log visibility
                        if self.execution_recorder:
                            self.execution_recorder.write_tool_dispatch(
                                step_id=self.step.step_id,
                                agent_id=self.agent_id,
                                iteration=self.current_iteration,
                                tool_name=tc.tool_name,
                                snippet=_snippet,
                            )

                        self.logger.info(
                            f"[{self.current_iteration}][ThinkStream] "
                            f"Dispatched '{tc.tool_name}' "
                            f"(safe={is_safe}, id={event.call_id})",
                            component="RuntimeAgent",
                        )

                    elif isinstance(event, StreamDoneEvent):
                        llm_result = event.result
                        reasoning = llm_result.content or ""

                        if stream_tool_calls:
                            self._assistant_messages.append({
                                "role": "assistant",
                                "content": reasoning,
                                "tool_calls": api_tool_calls_for_msg,
                            })
                            self._obs_group_sizes.append(len(stream_tool_calls))
                            decision = Decision(
                                reasoning=reasoning,
                                tool_calls=stream_tool_calls,
                            )
                        else:
                            # Plain-text completion — no tool calls
                            decision = await Decision.from_data(
                                raw_content=reasoning,
                                llm_services=self._from_data_services,
                            )
                            self._assistant_messages.append({
                                "role": "assistant",
                                "content": reasoning,
                            })
                            self._obs_group_sizes.append(0)

                        # Log decision (mirrors think())
                        _params_log = None
                        if decision.parameters:
                            _params_log = {
                                k: (str(v)[:200] + "..." if len(str(v)) > 200 else v)
                                for k, v in decision.parameters.items()
                            }
                        self.logger.info(
                            f"[{self.current_iteration}][ThinkStream] Decision: "
                            + (
                                f"parallel_tools={[tc.tool_name for tc in decision.tool_calls]}, "
                                if decision.is_parallel else
                                f"tool={decision.tool_name}, params={_params_log}, "
                            )
                            + f"reasoning='{decision.reasoning}'"
                            + (f", error='{decision.error}'" if decision.error else ""),
                            component="RuntimeAgent",
                        )

                        if self._interaction_manager and decision.reasoning:
                            try:
                                self._interaction_manager.notify_decision_made(
                                    self.current_iteration, decision.reasoning,
                                    llm_result.total_tokens,
                                )
                            except Exception:
                                pass

            except Exception as e:
                _stream_error = e

            # ── Handle stream error or advance ────────────────────────────────
            if _stream_error is not None:
                if self._interaction_manager:
                    try:
                        self._interaction_manager.notify_state_changed("executing")
                        self._interaction_manager.display_error(
                            f"LLM stream error: {type(_stream_error).__name__}: {_stream_error}"
                        )
                    except Exception:
                        pass
                self.logger.warning(
                    f"[{self.current_iteration}][ThinkStream] Stream error: "
                    f"{type(_stream_error).__name__}: {_stream_error}",
                    component="RuntimeAgent",
                )
                for _, task in running_tasks:
                    task.cancel()
                self._in_flight_tools.clear()

                if self._services[0]._is_prompt_too_long_error(_stream_error):
                    return Decision(
                        reasoning="Prompt too long.",
                        error=f"{_LLM_API_ERROR_TAG}:PTL: {_stream_error}",
                    ), [], TokenUsage()

                # Mid-stream fallback: safe only when no tool calls dispatched yet.
                if not stream_tool_calls:
                    next_offset = service_offset + len(services_slice)
                    if next_offset < len(self._services):
                        self.logger.warning(
                            f"[{self.current_iteration}][ThinkStream] Mid-stream error before "
                            f"any tool dispatch — retrying with service index {next_offset}: "
                            f"{type(_stream_error).__name__}",
                            component="RuntimeAgent",
                        )
                        service_offset = next_offset
                        continue  # retry with next service

                # Tools already dispatched, or all services exhausted.
                return Decision(
                    reasoning="LLM stream interrupted.",
                    error=f"{_LLM_API_ERROR_TAG}: LLM stream error: "
                          f"{type(_stream_error).__name__}: {_stream_error}",
                ), [], TokenUsage()

            # Stream consumed successfully — exit the retry loop.
            break

        if self._interaction_manager:
            try:
                self._interaction_manager.notify_state_changed("executing")
            except Exception:
                pass

        # decision must be set by StreamDoneEvent; guard against malformed streams
        if decision is None:
            self.logger.warning(
                f"[{self.current_iteration}][ThinkStream] Stream ended without StreamDoneEvent",
                component="RuntimeAgent",
            )
            for _, task in running_tasks:
                task.cancel()
            self._in_flight_tools.clear()
            return Decision(reasoning="LLM stream incomplete."), [
                ToolResult(
                    success=False,
                    output=None,
                    error="LLM stream ended without a completion event",
                    tool_name="llm_stream",
                    tool_parameters={},
                )
            ], TokenUsage()

        # ── Collect results in dispatch order ─────────────────────────────────
        tool_results: List[ToolResult] = []
        for tc, task in ordered_tasks:
            try:
                result = await task
            except asyncio.CancelledError:
                result = ToolResult(
                    success=False, output=None,
                    error="Tool execution was cancelled",
                    tool_name=tc.tool_name, tool_parameters=tc.parameters,
                )
            except Exception as exc:
                result = ToolResult(
                    success=False, output=None,
                    error=f"Tool execution error: {exc}",
                    tool_name=tc.tool_name, tool_parameters=tc.parameters,
                )
            tool_results.append(result)

        # All tools finished — clear in-flight tracking
        self._in_flight_tools.clear()

        _in_tok = llm_result.input_tokens if llm_result is not None else 0
        _out_tok = llm_result.output_tokens if llm_result is not None else 0
        _cc_tok = llm_result.cache_creation_input_tokens if llm_result is not None else 0
        _cr_tok = llm_result.cache_read_input_tokens if llm_result is not None else 0
        return decision, tool_results, TokenUsage(
            input_tokens=_in_tok,
            output_tokens=_out_tok,
            cache_creation_tokens=_cc_tok,
            cache_read_tokens=_cr_tok,
        )

    async def run_streaming(self, goal: str) -> AgentResult:
        """Run the Observe-Think-Act loop using streaming tool dispatch.

        Identical to run() in every respect except that the Think step uses
        streaming so that each tool call is dispatched as an asyncio.Task the
        moment its content_block_stop event fires — before the full LLM
        response has finished streaming.

        Works with any LLMService implementation (QGenieLLMService,
        UniversalLLMService, AnthropicStreamingService, etc.) because
        _think_streaming() calls call_with_fallback_stream() which uses the
        unified chat_stream() interface.

        All error-handling paths from run() are preserved:
          • User interrupt check before every iteration
          • Prompt-too-long: semantic compaction → hard half-drop → fail
          • LLM API error: return failure AgentResult to Planner
          • USER_NEW_INSTRUCTION propagation from tool results
          • Execution recorder (agent_start / write_iteration / agent_end)
          • Progress analyzer reminders
          • Maximum iterations guard
        """
        self.logger.info(
            f"Starting Agent Runtime (streaming) goal: {goal[:150]}",
            component="RuntimeAgent",
        )
        self.logger.debug(
            f"Starting Agent Runtime (streaming) goal: {goal}",
            component="RuntimeAgent",
        )

        if self.execution_recorder:
            self.execution_recorder.write_agent_start(
                agent_id=self.agent_id,
                step_id=self.step.step_id,
                description=self.step.description,
                goal=self.step.goal,
                planner_reasoning=self.step.planner_reasoning,
                expected_outcomes=self.step.expected_outcomes,
            )

        iteration = 0
        _agent_result: Optional[AgentResult] = None
        _tools_used: list = []
        _token_usage = TokenUsage()

        while iteration < self.max_iterations:
            iteration += 1
            self.current_iteration = iteration

            # 0. User interrupt check
            if self.check_interrupt_callback:
                user_msg = self.check_interrupt_callback()
                if user_msg:
                    self.logger.info(
                        f"[{iteration}][Interrupt] User message detected before Think: "
                        f"{user_msg[:80]}",
                        component="RuntimeAgent",
                    )
                    _agent_result = AgentResult.create_result(
                        success=False,
                        error="USER_NEW_INSTRUCTION",
                        reasoning=user_msg,
                        iterations=iteration,
                        tools_used=_tools_used,
                        token_usage=_token_usage,
                    )
                    self._record_agent_end(_agent_result, _tools_used)
                    return _agent_result

            # 1. Observe
            observations = self.observe()

            # 1b. Compact old observations:
            #   • Every 50 iterations: proactive maintenance (30% threshold) to
            #     prevent gradual context growth in long-running tasks before
            #     the normal 80% trigger fires.
            #   • Every iteration: standard budget-threshold compact (80%).
            _compact_ratio = 0.3 if (iteration % 50 == 0 and iteration > 0) else 0.8
            await self._compact_old_observations(budget_ratio=_compact_ratio)
            observations = self.observe()

            # 2. Analyze progress
            progress_status = self.progress_analyzer.analyze()

            # 3. Think (streaming) + Act (early dispatch)
            reminder = (
                progress_status.reminder_message
                if progress_status.should_add_reminder else None
            )
            # Anti-repeat guard: if any approach has failed 2+ times, prepend
            # a structured reminder so the model avoids verbatim retries.
            _repeat_offenders = sorted(
                [(sig, cnt) for sig, cnt in self._failed_approaches.items() if cnt >= 2],
                key=lambda x: -x[1],
            )[:5]
            if _repeat_offenders:
                _lines = [f"  • ({cnt}× failed) {sig}" for sig, cnt in _repeat_offenders]
                _anti_repeat = (
                    "ANTI-REPEAT GUARD — these approaches have already failed "
                    "multiple times. Do NOT retry them; use a structurally different "
                    "tool, command, or decomposition:\n" + "\n".join(_lines)
                )
                reminder = (_anti_repeat + "\n\n" + reminder) if reminder else _anti_repeat
            decision, tool_results, _iter_token_usage = await self._think_streaming(
                goal, observations, reminder
            )
            _token_usage += _iter_token_usage

            # ── Error handling (mirrors run() exactly) ────────────────────────
            if decision.error:
                if decision.error.startswith(_LLM_API_ERROR_TAG):
                    raw_error = decision.error[len(_LLM_API_ERROR_TAG) + 1:].strip()

                    # Prompt-too-long recovery
                    if raw_error.startswith("PTL:"):
                        self.logger.warning(
                            f"[{iteration}][ThinkStream] Prompt too long — "
                            "attempting semantic compaction.",
                            component="RuntimeAgent",
                        )
                        obs_before = len(self.step.get_all_observations())
                        await self._compact_old_observations()
                        obs_after = len(self.step.get_all_observations())
                        if obs_after < obs_before:
                            self.logger.info(
                                f"[{iteration}][ThinkStream] Semantic compaction "
                                f"{obs_before} → {obs_after}; retrying.",
                                component="RuntimeAgent",
                            )
                            continue  # retry with compacted context

                        # Semantic compaction made no progress → hard half-drop
                        self.logger.warning(
                            f"[{iteration}][ThinkStream] Semantic compaction "
                            "insufficient — falling back to hard half-drop.",
                            component="RuntimeAgent",
                        )
                        all_obs = self.step.get_all_observations()
                        half = len(all_obs) // 2
                        if half > 0:
                            kept = all_obs[half:]
                            self.step.clear_observations()
                            for obs in kept:
                                self.step.add_observation(obs)
                            half_groups = len(self._obs_group_sizes) // 2
                            self._assistant_messages = self._assistant_messages[half_groups:]
                            self._obs_group_sizes = self._obs_group_sizes[half_groups:]
                            self.last_logged_obs_count = 0
                            self.observe(ToolResult(
                                success=True,
                                output=(
                                    f"[Context truncation notice: {half} earlier "
                                    "observation(s) were dropped to stay within "
                                    "the context limit.]"
                                ),
                                tool_name="context_truncation_notice",
                                tool_parameters={},
                            ))
                            continue  # retry with reduced context
                        raw_error = raw_error[len("PTL:"):].strip()

                    # All LLM services failed
                    _agent_result = AgentResult.create_result(
                        success=False,
                        reasoning=(
                            f"The LLM client is currently unavailable "
                            f"(error: {raw_error}). "
                            "This is NOT an agent execution failure — "
                            "the step could not proceed because the LLM API "
                            "is unreachable. Please retry or adjust the plan."
                        ),
                        error=decision.error,
                        iterations=iteration,
                        tools_used=_tools_used,
                        token_usage=_token_usage,
                    )
                    self._record_agent_end(_agent_result, _tools_used)
                    return _agent_result

                self.logger.error(
                    f"[{iteration}][ThinkStream] Error: {decision.error}",
                    component="RuntimeAgent",
                )
                _agent_result = AgentResult.create_result(
                    success=False,
                    reasoning=f"Error in Think step: {decision.reasoning}",
                    error=decision.error,
                    iterations=iteration,
                    tools_used=_tools_used,
                    token_usage=_token_usage,
                )
                self._record_agent_end(_agent_result, _tools_used)
                return _agent_result

            # Completion signal: no tool calls
            if not decision.tool_calls:
                # Guard: if _think_streaming returned error tool_results (stream
                # error path), this is NOT a real completion — treat it as an
                # LLM API failure so the step fails instead of exiting "successfully".
                if tool_results:
                    stream_errors = [tr.error for tr in tool_results if tr.error]
                    error_summary = "; ".join(stream_errors) if stream_errors else "LLM stream interrupted"
                    self.logger.warning(
                        f"[{iteration}][ThinkStream] Stream error masked as completion — "
                        f"failing step: {error_summary}",
                        component="RuntimeAgent",
                    )
                    _agent_result = AgentResult.create_result(
                        success=False,
                        reasoning=(
                            f"The LLM stream failed before a decision could be made "
                            f"(error: {error_summary}). "
                            "This is NOT an agent execution failure — "
                            "the step could not proceed because the LLM stream was interrupted. "
                            "Please retry or adjust the plan."
                        ),
                        error=f"{_LLM_API_ERROR_TAG}: {error_summary}",
                        iterations=iteration,
                        tools_used=_tools_used,
                        token_usage=_token_usage,
                    )
                    self._record_agent_end(_agent_result, _tools_used)
                    return _agent_result

                self.logger.info(
                    f"[{iteration}][ThinkStream] Goal achieved (no tool_calls) — "
                    f"{decision.reasoning}",
                    component="RuntimeAgent",
                )
                _agent_result = AgentResult.create_result(
                    success=True,
                    reasoning=decision.reasoning,
                    factual_outcome=list(decision.factual_outcome or []),
                    artifacts=list(decision.artifacts or []),
                    key_findings=list(decision.key_findings or []),
                    iterations=iteration,
                    tools_used=_tools_used,
                    token_usage=_token_usage,
                )
                self._record_agent_end(_agent_result, _tools_used)
                return _agent_result

            # 4. tool_results already collected by _think_streaming()

            # 5. Record iteration
            for tr in tool_results:
                if tr.tool_name:
                    params = tr.tool_parameters or {}
                    tool_nm = tr.tool_name
                    if tool_nm == "bash":
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
                    _tools_used.append(_format_tool_entry(tool_nm, primary_input))

            if self.execution_recorder and tool_results:
                is_parallel = len(tool_results) > 1
                for idx, tr in enumerate(tool_results):
                    self.execution_recorder.write_iteration(
                        tool_result=tr,
                        decision=decision,
                        iteration=iteration,
                        agent_id=self.agent_id,
                        step_id=self.step.step_id,
                        parallel_index=idx if is_parallel else None,
                        token_usage=_iter_token_usage,
                    )

            # 7. Propagate USER_NEW_INSTRUCTION
            for tr in tool_results:
                if tr.error == "USER_NEW_INSTRUCTION":
                    _agent_result = AgentResult.create_result(
                        success=False,
                        error="USER_NEW_INSTRUCTION",
                        reasoning=tr.output or "",
                        iterations=iteration,
                        tools_used=_tools_used,
                        token_usage=_token_usage,
                    )
                    self._record_agent_end(_agent_result, _tools_used)
                    return _agent_result

            # 8. Record progress and store observations
            for tr in tool_results:
                self.progress_analyzer.add_result(tr)
                self.observe(tr)

            # 8b. Update failed-approach registry for anti-repeat guard.
            # Skip infra-level results that don't represent retryable agent actions.
            for tr in tool_results:
                if (not tr.success
                        and tr.tool_name
                        and tr.tool_name not in _INFRA_TOOL_NAMES):
                    _sig = _failed_approach_signature(tr)
                    if _sig:
                        self._failed_approaches[_sig] = (
                            self._failed_approaches.get(_sig, 0) + 1
                        )

        # Reached maximum iterations
        progress_summary = self.progress_analyzer.get_summary()
        self.logger.warning(
            f"Task incomplete: Reached maximum iterations ({self.max_iterations}). "
            f"Final success rate: {progress_summary['success_rate']:.1%}",
            component="RuntimeAgent",
        )
        _agent_result = AgentResult.create_result(
            success=False,
            reasoning="Reached maximum iterations without completing goal",
            error=f"Maximum iterations ({self.max_iterations}) reached",
            iterations=iteration,
            tools_used=_tools_used,
            token_usage=_token_usage,
        )
        self._record_agent_end(_agent_result, _tools_used)
        return _agent_result

class SuccessPatternAnalyzer(ProgressAnalyzerBase):
    """
    Agent-level pattern analyzer (tool-call granularity).

    Used by RuntimeAgent to track individual tool call outcomes and inject
    reminders into the agent's prompt when stagnation is detected.
    """

    def _default_config(self) -> dict:
        return {
            "window_size": 5,
            "min_success_for_progress": 2,
            "moderate_stagnation_threshold": 3,
            "severe_stagnation_threshold": 5,
            "early_termination_threshold": 6,
            "enable_reminders": True,
        }

    def add_result(self, tool_result: ToolResult) -> None:  # type: ignore[override]
        """Record the outcome of a tool call.

        Overrides the base-class ``add_result(success, error_hint)`` to accept
        a full :class:`ToolResult` so that error-hint detection logic lives
        here rather than in the caller.

        Currently recognised hints:
          ``"write_param_error"`` — the write tool failed because the LLM
          split file content across multiple JSON parameters.
        """
        error_hint: Optional[str] = None
        if (
            not tool_result.success
            and tool_result.tool_name == "write"
            and "Parameter error for tool 'write'" in (tool_result.error or "")
        ):
            error_hint = "write_param_error"
        super().add_result(tool_result.success, error_hint=error_hint)

    def analyze(self) -> ProgressStatus:
        """
        Analyse the current tool-call history.

        Returns:
            ProgressStatus with should_add_reminder and optional reminder_message.

        Priority order for reminders:
          1. ``last_error_hint == "write_param_error"`` → targeted write-tool
             reminder injected immediately on the very next Think call so the
             LLM knows exactly how to fix the issue without wasting iterations.
          2. Generic stagnation → warning / critical reminder based on
             consecutive failure count (existing behaviour).
        """
        # ── Priority 1: targeted write-parameter-error reminder ───────────────
        if self.last_error_hint == "write_param_error":
            return ProgressStatus(
                should_add_reminder=True,
                reminder_message=self._generate_write_param_error_reminder(),
            )

        # ── Priority 2: generic stagnation reminder ───────────────────────────
        consecutive_failures = self._count_consecutive_failures()
        success_rate = self._get_success_rate()

        should_add_reminder = self._should_add_reminder(consecutive_failures)
        reminder_message = None
        if should_add_reminder:
            reminder_message = self._generate_reminder(consecutive_failures, success_rate)

        return ProgressStatus(
            should_add_reminder=should_add_reminder,
            reminder_message=reminder_message,
        )

    @staticmethod
    def _generate_write_param_error_reminder() -> str:
        """
        Return a targeted, actionable reminder for the write-tool parameter
        error where the LLM splits file content across multiple JSON keys.

        Injected on the very next Think call after the failure so the LLM
        immediately understands the root cause and knows how to fix it.
        """
        return (
            "⚠️  Write Tool Parameter Error — your previous write call failed "
            "because the file content was split across multiple JSON parameters "
            "instead of being placed entirely inside the 'content' string.\n\n"
            "ROOT CAUSE: The write tool ONLY accepts three parameters:\n"
            "  • \"path\"    (required) — destination file path\n"
            "  • \"content\" (required) — ALL text as one single string\n"
            "  • \"append\"  (optional bool) — true to append, false to overwrite\n\n"
            "HOW TO FIX — choose one strategy:\n\n"
            "Strategy A — Write in chunks using append mode (recommended):\n"
            "  1. First chunk  → {\"path\": \"file.md\", \"content\": \"...part 1...\", \"append\": false}\n"
            "  2. Next chunks  → {\"path\": \"file.md\", \"content\": \"...part 2...\", \"append\": true}\n"
            "  3. More chunks  → {\"path\": \"file.md\", \"content\": \"...part 3...\", \"append\": true}\n\n"
            "Strategy B — Use bash to write content in parts:\n"
            "  printf '...part 1...' > file.md\n"
            "  printf '...part 2...' >> file.md\n\n"
            "CRITICAL: Do NOT put any content section as a separate parameter key. "
            "Every byte of the file must live inside the single 'content' value."
        )

    def _get_pattern(self, consecutive_failures: int) -> str:
        """
        Classify the current progress pattern.

        Returns one of: "progressing", "mild_stagnation",
                        "moderate_stagnation", "severe_stagnation"
        """
        window_size = self.config["window_size"]

        if len(self.success_history) < window_size:
            return "progressing"  # Early stage — assume progress

        recent = self.success_history[-window_size:]
        success_count = sum(recent)

        if consecutive_failures >= self.config["severe_stagnation_threshold"]:
            return "severe_stagnation"
        elif consecutive_failures >= self.config["moderate_stagnation_threshold"]:
            return "moderate_stagnation"
        elif success_count >= self.config["min_success_for_progress"]:
            return "progressing"
        else:
            return "mild_stagnation"

    def _generate_warning_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        return (
            f"📊 Progress Note: Recent operations show {consecutive_failures} consecutive "
            f"failures (success rate: {success_rate:.1%}).\n\n"
            f"This might be a good moment to:\n"
            f"  • Reflect on what's not working and why\n"
            f"  • Consider alternative approaches or strategies\n"
            f"  • Avoid repeating the same failed operation\n"
            f"  • Think creatively about solving the problem differently\n\n"
            f"Remember: You have the autonomy to decide the best path forward. "
            f"The \"error\" field is available if you determine the goal is "
            f"fundamentally unachievable."
        )

    def _generate_critical_reminder(
        self, consecutive_failures: int, success_rate: float
    ) -> str:
        return (
            f"⚠️ Significant Challenge Detected: {consecutive_failures} consecutive "
            f"failures observed (success rate: {success_rate:.1%}).\n\n"
            f"This pattern suggests the current approach may need fundamental "
            f"reconsideration:\n"
            f"  • Is the current strategy addressing the root problem?\n"
            f"  • Are there alternative tools or methods that might work better?\n"
            f"  • Could the goal be approached from a completely different angle?\n"
            f"  • Is this goal achievable with the currently available tools and "
            f"information?\n\n"
            f"You have full autonomy to decide:\n"
            f"  - Continue with a radically different strategy if you see a viable path\n"
            f"  - Use the \"error\" field if you determine the goal is truly unachievable\n\n"
            f"Trust your judgment on the best course of action."
        )

    # ── Convenience accessors (for RuntimeAgent) ─────────────────────────────

    def get_consecutive_failures(self) -> int:
        """Return the current consecutive failure count."""
        return self._count_consecutive_failures()

    def get_success_rate(self) -> float:
        """Return the current success rate."""
        return self._get_success_rate()
