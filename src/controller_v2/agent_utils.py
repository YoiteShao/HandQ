"""
Agent utility functions and classes.

All functions are pure / stateless (except IterationAdvisor which is a
lightweight in-memory tracker).
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Union, TYPE_CHECKING

from ..infrastructure.utils import try_parse_json_with_repair_flag
from ..tools.base_tool import ToolResult

if TYPE_CHECKING:
    # Annotation-only: shared_checklist imports this module, so importing
    # ItemResult at runtime would create a cycle.
    from .shared_checklist import ItemResult


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


@dataclass
class TurnDigest:
    """Mechanically-derived per-turn summary written to the bus each turn.

    NOT a self-report — every field is computed from the turn's tool results
    and the IterationAdvisor's counters, so it cannot be gamed by an agent that
    claims progress it did not make. Read by the planner (in-flight item view)
    and by the Tier-1 progress watcher.
    """
    item_id: str
    iteration: int
    tool_names: List[str]
    success_count: int
    fail_count: int
    produced_new_artifact: bool
    goal_signal_hit: bool
    no_progress_streak: int


@dataclass
class ProgressConcern:
    """Tier-1 watcher verdict — watcher → planner only, never shown to agent.

    last-write-wins single slot on the bus. ``verdict`` is one of
    "diverging" | "false_progress" | "ok"; only the first two are ever stored
    (an "ok" verdict simply leaves the slot untouched).
    """
    item_id: str
    verdict: str
    rationale: str
    suggest_replan: bool = False
    suggest_interrupt: bool = False


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


# ── Acceptance-loop cross-round info delta ────────────────────────────────────
#
# Pure, ungameable pre-gate for the acceptance loop — the mechanical "hard-coded
# sense" tier, analogous to IterationAdvisor.turns_since_artifact for the in-item
# loop. Measures the information a freshly-completed acceptance round added over
# the UNION of all prior acceptance rounds. total_new == 0 ⇒ the candidate is
# spinning in place (re-attempting with zero new information per round).

_WS_RE = re.compile(r'\s+')
_PUNCT_RE = re.compile(r'[^\w\s]')


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Collapses reworded-identical findings/issues so a blocker restated with
    different surface text does not read as genuinely new information.
    """
    s = _PUNCT_RE.sub(' ', s.lower())
    return _WS_RE.sub(' ', s).strip()


def acceptance_info_delta(
    latest: "ItemResult",
    prior: List["ItemResult"],
) -> Dict[str, Any]:
    """Set-difference of the latest acceptance round vs the UNION of prior rounds.

    Each of artifacts / key_findings / issues / factual_outcome is normalized and
    diffed against the pooled prior rounds. factual_outcome is the primary novelty
    channel for artifact-free tasks (e.g. SSH status reads). ``total_new`` == 0
    means the latest round surfaced nothing the candidate had not already
    produced — the ungameable signal that the loop is spinning.
    """
    fields = ("artifacts", "key_findings", "issues", "factual_outcome")

    def _norm_set(result: "ItemResult", field_name: str) -> Set[str]:
        out: Set[str] = set()
        for v in getattr(result, field_name, None) or []:
            n = _normalize(str(v))
            if n:
                out.add(n)
        return out

    delta: Dict[str, Any] = {}
    total_new = 0
    for f in fields:
        pooled: Set[str] = set()
        for r in prior:
            pooled |= _norm_set(r, f)
        new = sorted(_norm_set(latest, f) - pooled)
        delta[f] = new
        total_new += len(new)
    delta["total_new"] = total_new
    return delta


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

# No-progress detection (Tier 0 progress-sense): fires when tool calls keep
# SUCCEEDING but the item makes no headway — no new artifact AND no tool output
# matching the item's expected_outcomes — for this many turns. Distinct from
# stagnation, which keys off failures. Threshold is small (early, soft signal);
# cooldown stops it nagging every turn while stuck.
_NOPROGRESS_ARTIFACT_THRESHOLD = 3
_NOPROGRESS_GOAL_THRESHOLD = 3
_NOPROGRESS_COOLDOWN = 5
# Only treat "no progress" as meaningful when most calls are succeeding — a low
# success rate is already covered by the stagnation/anti-repeat channels.
_NOPROGRESS_MIN_SUCCESS_RATE = 0.7

# Goal-keyword extraction (coarse mechanical proxy for "did this turn surface
# anything related to the goal"). ASCII tokens require >=4 chars; CJK runs are
# shingled into 2-grams so substring matching works without a CJK tokenizer.
# Errs toward over-matching (= fewer no-progress warnings), the safe direction.
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")
_GOAL_STOPWORDS: frozenset = frozenset({
    "with", "that", "this", "from", "into", "have", "been", "will", "your",
    "more", "than", "then", "when", "what", "which", "should", "could", "must",
    "each", "item", "task", "ensure", "verify", "confirm", "least", "using",
    "based", "there", "their", "about", "would", "shall", "value", "result",
})

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

        # No-progress tracking (Tier 0 progress-sense). Goal keywords are
        # extracted from the item's expected_outcomes at reset_for_item; the
        # two counters track consecutive turns without a new artifact / without
        # a goal-keyword hit. _noprogress_streak counts consecutive turns past
        # the small threshold (consumed by the Tier 1 watcher's hysteresis gate).
        self._goal_keywords: Set[str] = set()
        self._expected_outcomes: List[str] = []
        self._turns_since_artifact: int = 0
        self._turns_since_goal_signal: int = 0
        self._noprogress_cooldown: int = 0
        self._noprogress_streak: int = 0

    def reset_for_item(self, expected_outcomes: Optional[List[str]] = None) -> None:
        """Call at the start of each item."""
        self._success_history.clear()
        self._last_error_hint = None
        self._failed_approaches.clear()
        self._iteration_tool_counts.clear()
        self._parallelism_cooldown = 0
        self._ltm_refresh_cooldown = 0
        self._expected_outcomes = list(expected_outcomes) if expected_outcomes else []
        self._goal_keywords = self._extract_keywords(expected_outcomes or [])
        self._turns_since_artifact = 0
        self._turns_since_goal_signal = 0
        self._noprogress_cooldown = 0
        self._noprogress_streak = 0

    def record_tool_result(self, tr: ToolResult) -> None:
        """Record outcome of a tool execution.

        wait_interval is infrastructure (intentional yield between observation
        cycles) — it is excluded from success_history so it does not break
        consecutive-failure tracking, and from failed_approaches so repeated
        waits are never flagged as retried failures.
        """
        if tr.tool_name == "wait_interval":
            return

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

    def record_progress_signal(
        self, *, produced_new_artifact: bool, goal_signal_hit: bool,
        has_wait_interval: bool = False,
    ) -> None:
        """Record per-turn progress signals (call once per turn).

        Detects "active but not progressing": tool calls may keep succeeding
        while the item makes no headway toward its expected_outcomes. Unlike the
        failure-based stagnation check, this fires even when every tool call
        succeeds but produces no new artifact and surfaces no goal keyword.
        _noprogress_streak counts consecutive turns past the small threshold;
        it is read by the Tier 1 watcher's hysteresis gate.

        When has_wait_interval=True, the turn contained an intentional yield
        (wait_interval tool). This is the agent's explicit "I confirmed the
        state is normal and will check again later" signal — it resets the
        no-progress counters because the agent IS progressing through its
        monitoring duty, just not producing artifacts.
        """
        if self._noprogress_cooldown > 0:
            self._noprogress_cooldown -= 1

        if has_wait_interval:
            self._turns_since_artifact = 0
            self._turns_since_goal_signal = 0
            self._noprogress_streak = 0
            return

        self._turns_since_artifact = (
            0 if produced_new_artifact else self._turns_since_artifact + 1
        )
        self._turns_since_goal_signal = (
            0 if goal_signal_hit else self._turns_since_goal_signal + 1
        )

        if (self._turns_since_artifact >= _NOPROGRESS_ARTIFACT_THRESHOLD
                and self._turns_since_goal_signal >= _NOPROGRESS_GOAL_THRESHOLD):
            self._noprogress_streak += 1
        else:
            self._noprogress_streak = 0

    @property
    def noprogress_streak(self) -> int:
        """Consecutive turns past the no-progress threshold (Tier 1 gate)."""
        return self._noprogress_streak

    @property
    def turns_since_artifact(self) -> int:
        return self._turns_since_artifact

    def matches_goal(self, text: str) -> bool:
        """True if any expected-outcome keyword appears in `text`.

        Cheap substring scan. Returns False when the item provided no
        expected_outcomes (empty keyword set) so a goal-less item never reports
        spurious goal signals. ASCII keywords are lowercase (text is lowered to
        match); CJK 2-gram keywords match case-insensitively by construction.
        """
        if not self._goal_keywords or not text:
            return False
        low = text.lower()
        return any(kw in low for kw in self._goal_keywords)

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

        # 4. No-progress (active but not advancing toward expected_outcomes).
        #    Distinct from #3, which keys off FAILURES: this fires when tool
        #    calls keep SUCCEEDING but produce no new artifact and surface no
        #    goal-keyword signal. Soft fact — the smarter agent may override it.
        if (self._noprogress_cooldown <= 0
                and self._goal_keywords
                and self._turns_since_artifact >= _NOPROGRESS_ARTIFACT_THRESHOLD
                and self._turns_since_goal_signal >= _NOPROGRESS_GOAL_THRESHOLD
                and self._get_success_rate() >= _NOPROGRESS_MIN_SUCCESS_RATE):
            goal_anchor = ""
            if self._expected_outcomes:
                goals = "; ".join(self._expected_outcomes[:3])
                goal_anchor = (
                    f"\n\nGOAL RE-ANCHOR — your expected outcomes are: [{goals}]. "
                    f"Is your current line of work the most DIRECT path to these "
                    f"outcomes? If not, change approach now rather than continuing "
                    f"a tangent."
                )
            parts.append(
                f"PROGRESS CHECK — the last {self._turns_since_artifact} turns "
                f"completed without producing a new artifact, and the last "
                f"{self._turns_since_goal_signal} turns surfaced nothing matching "
                f"this item's expected outcomes, yet tool calls are succeeding. "
                f"You may be active without advancing. Re-read the expected "
                f"outcomes and ask: is the current line of work actually moving "
                f"toward them, or should the approach change? If the outcomes are "
                f"already met, complete the item; if truly blocked, use the "
                f"\"error\" field.{goal_anchor}"
            )
            self._noprogress_cooldown = _NOPROGRESS_COOLDOWN

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

    def _extract_keywords(self, outcomes: List[str]) -> Set[str]:
        """Build a keyword set from expected_outcomes for cheap substring
        matching against tool outputs.

        ASCII alphanumeric tokens require >=4 chars (and aren't stopwords); CJK
        runs are shingled into 2-grams so matching works without a CJK
        tokenizer. The result errs toward over-matching, which is the safe
        direction — a spurious goal-signal hit merely suppresses a soft
        no-progress note rather than over-warning the agent.
        """
        kws: Set[str] = set()
        for outcome in outcomes:
            if not outcome:
                continue
            lowered = outcome.lower()
            for tok in _ASCII_TOKEN_RE.findall(lowered):
                if tok not in _GOAL_STOPWORDS:
                    kws.add(tok)
            for run in _CJK_RUN_RE.findall(outcome):
                for i in range(len(run) - 1):
                    kws.add(run[i:i + 2])
        return kws

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
        goal_anchor = ""
        if self._expected_outcomes:
            goals = "; ".join(self._expected_outcomes[:3])
            goal_anchor = (
                f"\n\nGOAL RE-ANCHOR — your expected outcomes are: [{goals}]. "
                f"Evaluate whether your current approach is the most direct path "
                f"to these outcomes, or if you have drifted into solving a "
                f"different problem."
            )

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
                f"{goal_anchor}"
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
            f"{goal_anchor}"
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
    claim_tool: List[str] = field(default_factory=list)
    release_tool: List[str] = field(default_factory=list)
    # Optional agent→planner advisory. Set by the agent (on a completion OR an
    # error turn) when something it learned this item — most often a skill body
    # read via read_skill, or a discovered fact — means the planner's REMAINING
    # items should change. Carried onto ItemResult and rendered into the
    # planner's checklist context at the item boundary; the planner may then
    # replace_post_current the pending tail. Distinct from key_findings (facts)
    # and from the watcher's ProgressConcern (watcher→planner only).
    plan_feedback: Optional[str] = None
    # Non-fatal note attached when a completion turn has evidence of
    # truncation (stop_reason=max_tokens, or JSON that only parsed after
    # json_repair salvage). Populated by _think_streaming after inspecting
    # LLMChatResult.stop_reason and by from_completion_text when parsing
    # required repair. Surfaced to the log's ITEM_END `issues` field so a
    # partial-completion never looks like a clean pass.
    truncation_note: Optional[str] = None
    # True when the LLM emitted a completion that violated the JSON schema
    # entirely — i.e. no dict with a `reasoning` key. This is a stronger
    # signal than truncation_note: the item_loop uses it to REJECT the
    # completion (mirroring speculative-completion guard) rather than
    # accept a preview. The corrective retry sends the schema back to the
    # LLM so subsequent turns can produce a properly-structured completion
    # with long output routed into artifacts / files.
    format_violation: bool = False

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
        treating plain text as a factual_outcome summary. When json_repair
        had to salvage the input (a strong signal of mid-stream truncation),
        the returned TurnOutcome carries a ``truncation_note`` so the caller
        can surface it in the item's issues.
        """
        def _coerce_str_list(v: Any) -> Optional[List[str]]:
            if v is None:
                return None
            if isinstance(v, list):
                return [str(x) for x in v if x is not None]
            return [str(v)]

        parsed, repaired = try_parse_json_with_repair_flag(raw_content)

        if isinstance(parsed, dict) and "reasoning" in parsed:
            claim, release = extract_self_extension_fields(parsed)
            note = (
                f"Completion JSON was salvaged by json_repair (likely truncated "
                f"mid-stream); raw_len={len(raw_content)}"
                if repaired else None
            )
            _pf = parsed.get("plan_feedback")
            return cls(
                reasoning=parsed.get("reasoning", ""),
                error=parsed.get("error"),
                factual_outcome=_coerce_str_list(parsed.get("factual_outcome")),
                artifacts=_coerce_str_list(parsed.get("artifacts")),
                key_findings=_coerce_str_list(parsed.get("key_findings")),
                claim_tool=claim,
                release_tool=release,
                truncation_note=note,
                plan_feedback=(str(_pf).strip() or None) if _pf is not None else None,
            )

        # Fallback: the LLM returned something that isn't a JSON object with
        # a `reasoning` key — most commonly pure markdown prose ignoring the
        # completion contract. Do NOT stash the raw content as factual_outcome
        # (that field is for short structured facts; long content belongs in
        # artifacts). Instead flag ``format_violation`` and hand a short
        # preview back. The item_loop reads ``format_violation`` and drives a
        # corrective retry (see persistent_agent.py speculative-completion
        # guard) — the LLM gets the schema pointed out and can re-emit a
        # proper JSON completion, moving long text to artifacts.
        preview_len = 500
        preview = raw_content[:preview_len] if raw_content else ""
        return cls(
            reasoning=preview if raw_content else "Failed to parse LLM response.",
            factual_outcome=None,
            truncation_note=(
                "Completion output was not valid JSON with a `reasoning` key; "
                f"raw_len={len(raw_content)}; item_loop will request a retry."
                if raw_content else None
            ),
            format_violation=bool(raw_content),
        )


def extract_self_extension_fields(parsed_json: Any) -> tuple[List[str], List[str]]:
    """Pull optional `claim_tool` / `release_tool` lists from any turn's JSON.

    Used for self-extension: agent can request additional on-demand tools
    (`claim_tool`) or hide tools from its visible list (`release_tool`)
    on any turn — not only at completion. Both fields are optional; unknown
    or non-list shapes return empty lists silently.
    """
    if not isinstance(parsed_json, dict):
        return [], []
    raw_claim = parsed_json.get("claim_tool") or []
    raw_release = parsed_json.get("release_tool") or []
    claim = [str(x) for x in raw_claim if isinstance(x, str)] if isinstance(raw_claim, list) else []
    release = [str(x) for x in raw_release if isinstance(x, str)] if isinstance(raw_release, list) else []
    return claim, release
