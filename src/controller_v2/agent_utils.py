"""
Agent utility functions and classes.

All functions are pure / stateless (except IterationAdvisor which is a
lightweight in-memory tracker).
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING

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

    ``info_gain`` is the unified per-turn novelty signal (a novel content
    artifact, a previously-unseen tool observation, a newly-learned failure
    signature, or an intentional wait_interval). ``no_progress_streak`` is the
    count of consecutive turns with ``info_gain == False``.
    """
    item_id: str
    iteration: int
    tool_names: List[str]
    success_count: int
    fail_count: int
    produced_new_artifact: bool
    info_gain: bool
    no_progress_streak: int


@dataclass
class ProgressConcern:
    """Tier-1 watcher verdict — planner-facing only, never shown to agent.

    last-write-wins single slot on the bus. ``verdict`` is one of
    "diverging" | "false_progress" | "stalled" | "ok". "diverging" and
    "false_progress" come from the LLM watcher; "stalled" is emitted
    mechanically by the agent when a hard stall threshold is crossed (no LLM in
    the path). Only the non-"ok" verdicts are ever stored (an "ok" verdict
    simply leaves the slot untouched).
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
# sense" tier, analogous to IterationAdvisor.no_info_gain_streak for the in-item
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

_LTM_REFRESH_THRESHOLD = 3
_LTM_REFRESH_COOLDOWN = 5

# Stall detection (unified mechanical progress-sense). A "stall" is N consecutive
# turns that added zero new information (no_info_gain_streak) OR N consecutive
# tool failures. Two tiers:
#   - SOFT: surface a single bail-out reminder to the agent (cooldown-gated).
#   - HARD: flip the planner trigger mechanically (no LLM in the path) via the
#     checklist bus — the incident fix. Higher threshold than SOFT so the agent
#     gets a chance to self-correct before the planner is woken.
# _STALL_COOLDOWN gates both so neither fires every turn while stuck.
_SOFT_STALL_STREAK = 3
_HARD_STALL_STREAK = 5
_HARD_FAIL_STREAK = 6
_STALL_COOLDOWN = 5

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
         instruction message. At most three blocks (down from six):
           0. Directives (planner's advisory constraints, always visible)
           1. Anti-repeat guard (specific dead-path signatures)
           2. One stall block — the specific write_param_error fix OR the
              generic soft-stall bail-out (mutually exclusive).
      B. should_refresh_ltm() — boolean signal that drives a fresh LTM
         recall in PersistentAgent (cooldown-gated). Result is shown as an
         independent block alongside (A); see get_recent_failure_signatures()
         for the query-enrichment helper.

    Progress is measured by a single mechanical primitive, info_gain, recorded
    per turn. A turn shows info gain iff it wrote a novel content artifact, a
    tool observation returned previously-unseen (numeric-normalized) bytes, a
    new failure signature appeared, or it contained an intentional
    wait_interval. N consecutive turns without info gain (no_info_gain_streak)
    — or N consecutive tool failures — constitute a stall. hard_stall() reports
    when a HARD threshold is crossed so PersistentAgent can wake the planner
    mechanically (no LLM in the trigger path — the incident fix).
    """

    def __init__(self) -> None:
        self._success_history: List[bool] = []
        self._last_error_hint: Optional[str] = None
        self._failed_approaches: Dict[str, int] = {}
        # Cooldown for stagnation-triggered LTM refresh, decremented per turn
        # so the agent doesn't re-query LTM on every turn while stuck.
        self._ltm_refresh_cooldown: int = 0

        # Unified progress-sense (replaces the keyword goal-proxy). Every turn
        # is classified as info-gaining or not; _no_info_gain_streak counts
        # consecutive turns that added nothing. _seen_obs_hashes is the per-item
        # observation-novelty ledger (numeric-normalized payload hashes);
        # _turn_saw_info_gain is the per-turn flag set by record_tool_result and
        # consumed (then reset) by record_progress_signal.
        self._seen_obs_hashes: Set[str] = set()
        self._no_info_gain_streak: int = 0
        self._turn_saw_info_gain: bool = False

        # Stall-block cooldowns (soft agent reminder + hard mechanical planner
        # wake), so neither fires every turn while stuck. Decremented once per
        # turn in record_progress_signal, and reset when info gain ends an
        # episode so a fresh stall can fire cleanly.
        self._soft_stall_cooldown: int = 0
        self._hard_stall_cooldown: int = 0

        # Directives (Q4): advisory constraints from the planner shown to the
        # agent each turn via get_reminder(). Weak — agent may violate but is
        # expected to bail out via plan_feedback rather than silently push
        # through. Populated on reset_for_item, cleared at item boundary.
        self._directives: List[str] = []

    def reset_for_item(
        self,
        directives: Optional[List[str]] = None,
    ) -> None:
        """Call at the start of each item."""
        self._success_history.clear()
        self._last_error_hint = None
        self._failed_approaches.clear()
        self._ltm_refresh_cooldown = 0
        self._seen_obs_hashes.clear()
        self._no_info_gain_streak = 0
        self._turn_saw_info_gain = False
        self._soft_stall_cooldown = 0
        self._hard_stall_cooldown = 0
        self._directives = list(directives) if directives else []

    def record_tool_result(self, tr: ToolResult) -> None:
        """Record outcome of a tool execution.

        Besides success/failure and failed-approach signatures, this sets the
        per-turn info-gain flag when the result adds new information:
          - a SUCCESSFUL tool returned a payload whose numeric-normalized hash
            was not seen before this item (a novel content artifact or a novel
            observation), OR
          - a NEW failure signature appeared (learning a fresh dead path is
            information; re-hitting a known one is not).
        The payload is selected PER TOOL TYPE (see _info_payload): for
        write/edit it is the content written, NEVER tr.output — tr.output is the
        destination path and changes on every rename, so trusting it would let
        probe1.sh → probe2.sh with identical bodies inflate novelty forever
        (the real cause of the item-2 170-turn spin). For every other tool it is
        the returned output bytes.

        wait_interval is infrastructure (intentional yield between observation
        cycles) — it is excluded from success_history so it does not break
        consecutive-failure tracking, and from failed_approaches so repeated
        waits are never flagged as retried failures. Its info-gain contribution
        is handled in record_progress_signal (has_wait_interval).
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
                    prev = self._failed_approaches.get(sig, 0)
                    self._failed_approaches[sig] = prev + 1
                    if prev == 0:
                        self._turn_saw_info_gain = True
            return

        self._last_error_hint = None
        if tr.tool_name in INFRA_TOOL_NAMES:
            return
        payload = self._info_payload(tr)
        if not payload:
            return
        h = hashlib.blake2b(
            _normalize_numeric(payload).encode("utf-8", "replace"),
            digest_size=16,
        ).hexdigest()
        if h not in self._seen_obs_hashes:
            self._seen_obs_hashes.add(h)
            self._turn_saw_info_gain = True

    @staticmethod
    def _info_payload(tr: ToolResult) -> str:
        """Substantive payload for novelty hashing, selected per tool type.

        write/edit → the content written (tr.tool_parameters['content']); NOT
        tr.output, which is the destination path. read/bash/ssh/others →
        str(tr.output). Returns '' when there is nothing substantive to hash.
        """
        params = tr.tool_parameters or {}
        if tr.tool_name in ("write", "edit"):
            content = params.get("content")
            return str(content) if content is not None else ""
        return str(tr.output) if tr.output is not None else ""

    def record_progress_signal(
        self, *, has_wait_interval: bool = False,
    ) -> bool:
        """Fold this turn's info-gain flag into the no-info-gain streak.

        Call exactly once per turn, AFTER every tool result has been fed to
        record_tool_result. Returns this turn's info_gain (for the digest). Also
        the single per-turn clock: decrements the LTM-refresh and both stall
        cooldowns.

        info_gain is True iff the turn saw a novel observation / content
        artifact / new failure signature (tracked in record_tool_result) OR the
        turn contained an intentional wait_interval — the agent's explicit "I
        confirmed the state and will re-check later" monitoring signal, which is
        real progress through a monitoring duty even though it produces no
        artifact. A stall is a run of consecutive turns with info_gain == False;
        an info-gaining turn ends the episode (streak → 0) and clears the stall
        cooldowns so a subsequent stall can fire cleanly.
        """
        if self._ltm_refresh_cooldown > 0:
            self._ltm_refresh_cooldown -= 1
        if self._soft_stall_cooldown > 0:
            self._soft_stall_cooldown -= 1
        if self._hard_stall_cooldown > 0:
            self._hard_stall_cooldown -= 1

        info_gain = self._turn_saw_info_gain or has_wait_interval
        if info_gain:
            self._no_info_gain_streak = 0
            self._soft_stall_cooldown = 0
            self._hard_stall_cooldown = 0
        else:
            self._no_info_gain_streak += 1

        self._turn_saw_info_gain = False
        return info_gain

    @property
    def no_info_gain_streak(self) -> int:
        """Consecutive turns that added zero new information."""
        return self._no_info_gain_streak

    def hard_stall(self) -> bool:
        """True once per stall episode when a HARD threshold is crossed.

        Crossed when no_info_gain_streak >= _HARD_STALL_STREAK OR consecutive
        tool failures >= _HARD_FAIL_STREAK. Cooldown-gated (_STALL_COOLDOWN) so
        a persistent stall re-wakes the planner periodically rather than every
        turn. PersistentAgent calls this after record_progress_signal and, when
        True, flips the planner trigger mechanically via set_progress_concern —
        NO LLM in the path (the incident fix).
        """
        if self._hard_stall_cooldown > 0:
            return False
        if (self._no_info_gain_streak >= _HARD_STALL_STREAK
                or self._count_consecutive_failures() >= _HARD_FAIL_STREAK):
            self._hard_stall_cooldown = _STALL_COOLDOWN
            return True
        return False

    def get_reminder(self) -> Optional[str]:
        """Generate combined reminder string, or None if nothing to say."""
        parts: List[str] = []

        # 0. Directives — planner's advisory constraints, always visible while
        # active ("警钟长鸣"). Placed first so the agent re-reads them every
        # turn. Weak constraint: agent may violate but is expected to bail out
        # via plan_feedback rather than silently push through — see the
        # "Directive Conflicts" section of the agent system prompt.
        directives_msg = self._format_directives()
        if directives_msg:
            parts.append(directives_msg)

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

        # 2. Stall block — exactly one of two mutually-exclusive messages.
        #    The specific write_param_error fix takes precedence (immediately
        #    actionable, ungated) over the generic soft-stall bail-out. The
        #    soft-stall block fires when no_info_gain_streak OR consecutive
        #    failures cross _SOFT_STALL_STREAK, cooldown-gated so we don't nag
        #    every turn. This single block replaces the former parallelism
        #    nudge, stagnation prose, replan advisory, and no-progress prose.
        if self._last_error_hint == "write_param_error":
            parts.append(_WRITE_PARAM_ERROR_REMINDER)
        else:
            consecutive_fails = self._count_consecutive_failures()
            if (self._soft_stall_cooldown <= 0
                    and (self._no_info_gain_streak >= _SOFT_STALL_STREAK
                         or consecutive_fails >= _SOFT_STALL_STREAK)):
                if consecutive_fails >= _SOFT_STALL_STREAK:
                    fact = f"{consecutive_fails} consecutive tool failures"
                else:
                    fact = (
                        f"{self._no_info_gain_streak} consecutive turns added no "
                        f"new information (same observations, no new artifact, no "
                        f"new failure mode)"
                    )
                parts.append(
                    f"STALL CHECK — {fact}. If the current approach is "
                    f"structurally wrong (wrong assumption, tool, or target), stop "
                    f"tweaking and bail out to the planner: emit completion or "
                    f"error JSON with `plan_feedback` describing what you tried, "
                    f"what kept failing, and what you now suspect the real blocker "
                    f"is. If the expected outcomes are already met, complete the "
                    f"item. Bailing out when the approach is structurally wrong is "
                    f"the correct action — not a failure."
                )
                self._soft_stall_cooldown = _STALL_COOLDOWN

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
        """Return True when a run of failures justifies a fresh LTM recall.

        Fires once per failure episode (cooldown applied) so a stuck agent
        gets one LTM refresh, not one per turn. Threshold is
        _LTM_REFRESH_THRESHOLD (3) consecutive failures: the per-item LTM was
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

    def _format_directives(self) -> Optional[str]:
        """Render the current item's directives as a per-turn reminder block.

        Called by get_reminder(). Directives are the planner's advisory
        constraints — derived from user directives, prior lessons, and skill
        non-negotiables. They stay visible every turn ("警钟长鸣") until the
        item boundary; agent may violate but is expected to bail out via
        plan_feedback rather than silently push through.
        """
        if not self._directives:
            return None
        lines = [
            "🔔 [Directives — planner guidance; follow by default. "
            "If a directive contradicts observed reality, bail out with "
            "plan_feedback rather than silently violate — see \"Directive "
            "Conflicts\" in your system prompt.]"
        ]
        lines.extend(f"  • {d}" for d in self._directives)
        return "\n".join(lines)


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
