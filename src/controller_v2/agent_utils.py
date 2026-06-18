"""
Agent utility functions and classes.

All functions are pure / stateless (except IterationAdvisor which is a
lightweight in-memory tracker).
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ..infrastructure.utils import try_parse_json
from ..tools.base_tool import ToolResult


# ── ToolCall ────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """A single tool call within a Decision (for parallel execution)."""
    call_id: str
    tool_name: str
    parameters: Dict[str, Any]


# ── Constants ────────────────────────────────────────────────────────────────

LLM_API_ERROR_TAG = "LLM_API_ERROR"

INFRA_TOOL_NAMES: frozenset = frozenset({
    "llm_stream", "context_truncation_notice",
})

TOOL_NAME_ALIASES: Dict[str, str] = {
    "write_file": "write",
    "read_file": "read",
    "search": "grep",
    "find_files": "glob",
    "list_files": "glob",
    "bash": "shell",
}

SUPERSEDABLE_TOOL_ACTIONS: frozenset = frozenset({
    ("desktop", "screenshot"),
    ("desktop", "snapshot"),
    ("desktop", "hover_at"),
    ("desktop", "find_element"),
    ("desktop", "find_and_click"),
    ("browser", "screenshot"),
    ("browser", "snapshot"),
})


# ── ConversationTurn ────────────────────────────────────────────────────────

@dataclass
class ConversationTurn:
    """One LLM turn: assistant response paired with its tool results."""
    assistant_message: Dict[str, Any]
    observations: List[ToolResult] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return "tool_calls" in self.assistant_message

    def total_obs_chars(self) -> int:
        return sum(len(obs.to_obs_json(i + 1)) for i, obs in enumerate(self.observations))


# ── Observation budget ───────────────────────────────────────────────────────

def resolve_obs_budget(context_window: int) -> int:
    """Scale observation budget to the model's context window capacity.

    chars_per_token = 3.5 reflects real mixed content (text + code + JSON).
    utilization = 0.85 leaves 15% headroom.
    """
    chars_per_token = 3.5
    utilization = 0.85
    fixed_overhead_tokens = 10_000
    available_tokens = int((context_window - fixed_overhead_tokens) * utilization)
    budget = int(available_tokens * chars_per_token)
    return max(budget, 300_000)


# ── Formatting helpers ───────────────────────────────────────────────────────

def format_tool_entry(tool_name: str, tool_input: Any, max_len: int = 200) -> str:
    """Format a tools_used entry as '<tool_name>: <truncated_input>'."""
    if tool_input is None:
        return tool_name
    input_str = str(tool_input).strip()
    if not input_str:
        return tool_name
    if len(input_str) > max_len:
        input_str = input_str[:max_len] + "..."
    return f"{tool_name}: {input_str}"


# ── Failed-approach signature ────────────────────────────────────────────────

_NUMERIC_NORMALIZE_RE = re.compile(r'\d[\d.]*\d|\d+')


def _normalize_numeric(text: str) -> str:
    """Replace digit sequences (incl version numbers 2.1.0) with '#'."""
    return _NUMERIC_NORMALIZE_RE.sub('#', text)


def _smart_truncate(text: str, limit: int = 120) -> str:
    """Head+tail signature: first half + '...' + last half if over limit."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}...{text[-half:]}"


def failed_approach_signature(tr: ToolResult) -> Optional[str]:
    """Return a compact, stable signature for a failed ToolResult.

    Used to detect when the same approach is retried after already failing.
    Returns None for results that should not be tracked.
    """
    params = tr.tool_parameters or {}
    name = tr.tool_name or ""
    if name == "bash" or name == "shell":
        cmd = _normalize_numeric(params.get("command", "").strip())
        return f"bash:{_smart_truncate(cmd)}" if cmd else None
    if name == "ssh":
        action = params.get("action", "")
        command = _normalize_numeric(params.get("command", "").strip())
        script = _normalize_numeric(params.get("script_content", "").strip())
        key = command or script
        sig = _smart_truncate(key, 120)
        return f"ssh:{action}:{sig}" if sig else (f"ssh:{action}" if action else None)
    if name in ("read", "write", "edit"):
        path = params.get("path", "")
        return f"{name}:{path}" if path else None
    if name in ("glob", "grep"):
        pattern = _normalize_numeric(params.get("pattern", ""))
        return f"{name}:{_smart_truncate(pattern)}" if pattern else None
    return None


# ── Stale-snapshot supersession ──────────────────────────────────────────────
#
# In-place supersession is implemented in PersistentAgent._supersede_stale,
# which operates on ConversationTurn.observations. The earlier paired-tuple
# variant has been removed.


# ── IterationAdvisor ────────────────────────────────────────────────────────

_MODERATE_STAGNATION = 3
_SEVERE_STAGNATION = 5
_LTM_REFRESH_THRESHOLD = 3
_LTM_REFRESH_COOLDOWN = 5

_WRITE_PARAM_ERROR_REMINDER: str = (
    "Write Tool Parameter Error — your previous write call failed "
    "because the file content was split across multiple JSON parameters "
    "instead of being placed entirely inside the 'content' string.\n\n"
    "ROOT CAUSE: The write tool ONLY accepts three parameters:\n"
    "  - \"path\"    (required) — destination file path\n"
    "  - \"content\" (required) — ALL text as one single string\n"
    "  - \"append\"  (optional bool) — true to append, false to overwrite\n\n"
    "HOW TO FIX — Write in chunks using append mode:\n"
    "  1. First chunk  -> {\"path\": \"file.md\", \"content\": \"...part 1...\", \"append\": false}\n"
    "  2. Next chunks  -> {\"path\": \"file.md\", \"content\": \"...part 2...\", \"append\": true}\n\n"
    "CRITICAL: Do NOT put any content section as a separate parameter key. "
    "Every byte of the file must live inside the single 'content' value."
)


class IterationAdvisor:
    """Unified iteration health tracker for PersistentAgent.

    Two output channels:
      A. get_reminder() — text reminder injected into the agent's per-turn
         instruction message. Merges three signals:
           1. Anti-repeat guard (failed_approaches tracking)
           2. Parallelism nudge (turn tool-count tracking)
           3. Progress stagnation (consecutive failure detection +
              write_param_error)
      B. should_refresh_ltm() — boolean signal that drives a fresh LTM
         recall in PersistentAgent (cooldown-gated). Result is shown as an
         independent block alongside (A); see get_recent_failure_signatures()
         for the query-enrichment helper.
    """

    def __init__(self) -> None:
        self._success_history: List[bool] = []
        self._last_error_hint: Optional[str] = None
        self._failed_approaches: Dict[str, int] = {}
        self._iteration_tool_counts: List[int] = []
        self._parallelism_cooldown: int = 0
        # Cooldown for stagnation-triggered LTM refresh, decremented per turn
        # so the agent doesn't re-query LTM on every turn while stuck.
        self._ltm_refresh_cooldown: int = 0

    def reset_for_item(self) -> None:
        """Call at the start of each item."""
        self._success_history.clear()
        self._last_error_hint = None
        self._failed_approaches.clear()
        self._iteration_tool_counts.clear()
        self._parallelism_cooldown = 0
        self._ltm_refresh_cooldown = 0

    def record_tool_result(self, tr: ToolResult) -> None:
        """Record outcome of a tool execution."""
        self._success_history.append(tr.success)

        if not tr.success:
            if (tr.tool_name == "write"
                    and "Parameter error for tool 'write'" in (tr.error or "")):
                self._last_error_hint = "write_param_error"
            if tr.tool_name and tr.tool_name not in INFRA_TOOL_NAMES:
                sig = failed_approach_signature(tr)
                if sig:
                    self._failed_approaches[sig] = self._failed_approaches.get(sig, 0) + 1
        else:
            self._last_error_hint = None

    def record_turn_tool_count(self, count: int) -> None:
        """Record how many tool calls were issued in one LLM turn."""
        self._iteration_tool_counts.append(count)
        if len(self._iteration_tool_counts) > 16:
            self._iteration_tool_counts = self._iteration_tool_counts[-16:]
        if self._parallelism_cooldown > 0:
            self._parallelism_cooldown -= 1
        if self._ltm_refresh_cooldown > 0:
            self._ltm_refresh_cooldown -= 1

    def get_reminder(self) -> Optional[str]:
        """Generate combined reminder string, or None if nothing to say."""
        parts: List[str] = []

        # 1. Anti-repeat guard (highest priority — specific dead paths)
        repeat_offenders = sorted(
            [(sig, cnt) for sig, cnt in self._failed_approaches.items() if cnt >= 2],
            key=lambda x: -x[1],
        )[:5]
        if repeat_offenders:
            lines = [f"  ({cnt}x failed) {sig}" for sig, cnt in repeat_offenders]
            parts.append(
                "ANTI-REPEAT GUARD — these approaches have already failed "
                "multiple times. Do NOT retry them; use a structurally different "
                "tool, command, or decomposition:\n" + "\n".join(lines)
            )

        # 2. Parallelism nudge (with cooldown)
        if self._parallelism_cooldown <= 0 and len(self._iteration_tool_counts) >= 5:
            recent = self._iteration_tool_counts[-5:]
            if all(c == 1 for c in recent):
                parts.append(
                    "PARALLELISM CHECK — the last 5 turns each issued one tool call. "
                    "Before sending the next response, ask yourself: were those calls "
                    "genuinely sequential, or could some have run in parallel? "
                    "Batch independent operations in one response."
                )
                self._parallelism_cooldown = 5

        # 3. Progress stagnation / write_param_error
        stagnation_msg = self._check_stagnation()
        if stagnation_msg:
            parts.append(stagnation_msg)

        if not parts:
            return None
        return "\n\n".join(parts)

    def get_summary(self) -> dict:
        """Snapshot for logging at iteration cap."""
        return {
            "success_rate": self._get_success_rate(),
            "consecutive_failures": self._count_consecutive_failures(),
            "failed_approaches_count": len(self._failed_approaches),
        }

    def should_refresh_ltm(self) -> bool:
        """Return True when stagnation justifies a fresh LTM recall.

        Fires once per stagnation episode (cooldown applied) so a stuck agent
        gets one LTM refresh, not one per turn. Threshold is identical to
        moderate stagnation (3 consecutive failures): the per-item LTM was
        chosen for the happy-path query at item start, so once the agent has
        failed three times the original recall is likely the wrong angle.

        Caller is expected to query LTM with a query enriched by
        get_recent_failure_signatures(). The returned block goes into the
        bottom instruction area as an independent reminder, NOT replacing the
        per-item static block (preserves the prefix-cache anchor and lets the
        original LTM stay visible too).
        """
        if self._ltm_refresh_cooldown > 0:
            return False
        if self._count_consecutive_failures() >= _LTM_REFRESH_THRESHOLD:
            self._ltm_refresh_cooldown = _LTM_REFRESH_COOLDOWN
            return True
        return False

    def get_recent_failure_signatures(self, limit: int = 3) -> List[str]:
        """Return the top-N most-frequent failed-approach signatures.

        Used to enrich the LTM refresh query so recall favors entries about
        overcoming the actual blocker, not the item's happy-path approach.
        """
        return [
            sig for sig, _ in sorted(
                self._failed_approaches.items(),
                key=lambda x: -x[1],
            )[:limit]
        ]

    # ── Internal ────────────────────────────────────────────────────────────

    def _count_consecutive_failures(self) -> int:
        count = 0
        for success in reversed(self._success_history):
            if not success:
                count += 1
            else:
                break
        return count

    def _get_success_rate(self) -> float:
        if not self._success_history:
            return 1.0
        window = self._success_history[-10:]
        return sum(window) / len(window)

    def _check_stagnation(self) -> Optional[str]:
        if self._last_error_hint == "write_param_error":
            return _WRITE_PARAM_ERROR_REMINDER

        consecutive = self._count_consecutive_failures()
        if consecutive < _MODERATE_STAGNATION:
            return None

        rate = self._get_success_rate()
        if consecutive >= _SEVERE_STAGNATION:
            return (
                f"Significant Challenge Detected: {consecutive} consecutive "
                f"failures observed (success rate: {rate:.1%}).\n\n"
                f"The current approach may need fundamental reconsideration:\n"
                f"  - Is the current strategy addressing the root problem?\n"
                f"  - Are there alternative tools or methods?\n"
                f"  - Could the instruction be approached from a completely different angle?\n"
                f"  - Is this instruction achievable with available tools?\n\n"
                f"Options:\n"
                f"  - Continue with a radically different strategy\n"
                f"  - Use the \"error\" field if the instruction is truly unachievable"
            )
        return (
            f"Progress Note: Recent operations show {consecutive} consecutive "
            f"failures (success rate: {rate:.1%}).\n\n"
            f"Consider:\n"
            f"  - Reflect on what's not working and why\n"
            f"  - Consider alternative approaches or strategies\n"
            f"  - Avoid repeating the same failed operation\n"
            f"  - Think creatively about solving the problem differently\n\n"
            f"The \"error\" field is available if you determine the instruction is "
            f"fundamentally unachievable."
        )


# ── TurnOutcome ────────────────────────────────────────────────────────────

@dataclass
class TurnOutcome:
    """Discriminated outcome of one LLM turn in PersistentAgent.

    Three mutually-exclusive states:
      - tool_calls non-empty → execute all tools, then loop
      - is_completion (no tool_calls, no error) → instruction achieved
      - is_error (error set) → instruction unachievable
    """

    reasoning: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    error: Optional[str] = None
    factual_outcome: Optional[List[str]] = None
    artifacts: Optional[List[str]] = None
    key_findings: Optional[List[str]] = None

    @property
    def is_completion(self) -> bool:
        return not self.tool_calls and not self.error

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @classmethod
    def from_completion_text(cls, raw_content: str) -> 'TurnOutcome':
        """Parse non-tool LLM output into a completion TurnOutcome.

        Attempts JSON parsing for structured fields. Falls back to
        treating plain text as a factual_outcome summary.
        """
        def _coerce_str_list(v: Any) -> Optional[List[str]]:
            if v is None:
                return None
            if isinstance(v, list):
                return [str(x) for x in v if x is not None]
            return [str(v)]

        parsed: Union[dict, str] = try_parse_json(raw_content)

        if isinstance(parsed, dict) and "reasoning" in parsed:
            return cls(
                reasoning=parsed.get("reasoning", ""),
                error=parsed.get("error"),
                factual_outcome=_coerce_str_list(parsed.get("factual_outcome")),
                artifacts=_coerce_str_list(parsed.get("artifacts")),
                key_findings=_coerce_str_list(parsed.get("key_findings")),
            )

        return cls(
            reasoning=raw_content[:500] if raw_content else "Failed to parse LLM response.",
            factual_outcome=[raw_content[:500]] if raw_content else None,
        )
