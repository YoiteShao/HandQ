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
    # Annotation-only: task_channel imports this module, so importing
    # TaskResult at runtime would create a cycle.
    from .task_channel import TaskResult


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

# Bookkeeping tools that never touch the world — planning, reading a recipe, or
# adjusting the agent's own tool list. A completion "grounded" ONLY by these is
# not real evidence the task was done (see the speculative-completion guard in
# PersistentAgent._item_loop). Single source of truth: the online guard and the
# offline laziness analyzer (tools_v3/analyze_laziness.py) both key off this set,
# so a tool added here is treated as non-grounding everywhere at once.
#
# claim_tool / release_tool MUST be here: activating a tool is not using it. A
# real 2026-07-17 trace declared success after only read_skill + claim_tool
# ("SSH ... auto-probing verified") without ever calling ssh — the offline
# analyzer caught it; the online guard had missed claim_tool/release_tool.
BOOKKEEPING_ONLY_TOOLS: frozenset = frozenset({
    "todo_write", "read_skill", "claim_tool", "release_tool",
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
    # Post-2.1 split: each atomic tool has its own name; supersession keys on
    # tool_name alone (no more action-dispatch composites).
    "desktop_screenshot",
    "desktop_snapshot",
    "desktop_hover_at",
    "desktop_find_element",
    "desktop_find_and_click",
    "browser_screenshot",
    "browser_snapshot",
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
        """Char-cost of this turn as it actually rides in the request body.

        Historically summed ONLY ``observations`` (tool_results) — but the
        assistant message paired with them can be the larger half: its
        ``tool_calls[].function.arguments`` carries the FULL call payload
        (e.g. ``write``'s entire file content, a `shell` heredoc script),
        and ``thinking_blocks`` is replayed verbatim every subsequent turn
        per Anthropic's extended-thinking protocol. A turn dominated by
        several 10-40KB ``write`` calls could look tiny by this metric while
        actually being the biggest contributor to the request body — which
        is exactly what let the 2026-07-26 flash-meta session's PTL recovery
        (compact → hard-drop → LTM-drop → elide) shrink `_turns` repeatedly
        while the request kept getting rejected as "too long": every
        recovery step measures (and trims) only the half it can see.
        """
        obs_chars = sum(len(obs.to_obs_json(i + 1)) for i, obs in enumerate(self.observations))
        asst = self.assistant_message or {}
        asst_chars = len(str(asst.get("content") or ""))
        for tc in (asst.get("tool_calls") or []):
            fn = tc.get("function") or {}
            asst_chars += len(str(fn.get("arguments") or ""))
        for block in (asst.get("thinking_blocks") or []):
            asst_chars += len(str(block.get("thinking") or block.get("data") or ""))
        return obs_chars + asst_chars


@dataclass
class TurnDigest:
    """Mechanically-derived per-turn summary written to the bus each turn.

    NOT a self-report — every field is computed from the turn's tool results
    and the IterationAdvisor's counters, so it cannot be gamed by an agent that
    claims progress it did not make. Read by the Coordinator (in-flight item
    view) and by the Tier-1 progress watcher.

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
    """Tier-1 watcher verdict — Coordinator-facing only, never shown to agent.

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


# ── Tools whose repetition is bookkeeping/orientation, not task progress ─────
# A repeat of these is never a "redundant-work" stall: todo_write is the agent
# updating its own plan, read_skill re-reads a recipe. Excluded from the
# repeated-SUCCESS detector below so normal orientation isn't flagged.
_REPEAT_EXEMPT_TOOLS: frozenset = frozenset({
    "todo_write", "read_skill", "wait_interval",
})

# Observation-only tools (screenshot/snapshot/list_windows): their OUTPUT
# legitimately differs every call (fresh pixels, live window list), so the
# output-novelty stall detector never trips on a re-observe loop. Keyed on
# tool_name ALONE here so N repeats of the same observation count as a repeat
# regardless of the ever-changing bytes — this is what catches the Alma
# cold-start "43 screenshots, zero clicks" loop.
_OBSERVE_ONLY_TOOLS: frozenset = frozenset({
    "desktop_screenshot", "desktop_snapshot", "desktop_list_windows",
    "browser_screenshot", "browser_snapshot",
})


def repeated_call_signature(tr: ToolResult) -> Optional[str]:
    """Stable signature for a SUCCESSFUL tool call, for redundant-repeat detection.

    Mirrors ``failed_approach_signature`` but for successes, and keyed on the
    CALL (tool + normalized params) rather than the output — so a repeat is
    caught even when the output looks superficially novel every time
    (``schedule_create`` returns a fresh UUID, ``desktop_screenshot`` returns
    fresh pixels). This is the hole the output-novelty stall detector cannot
    see: re-running an already-succeeded, non-idempotent-looking call forever.

    Returns None for calls that should not be tracked (bookkeeping tools, or
    calls with no substantive key).
    """
    name = tr.tool_name or ""
    if name in _REPEAT_EXEMPT_TOOLS or name in INFRA_TOOL_NAMES:
        return None
    if name in _OBSERVE_ONLY_TOOLS:
        return name  # re-observing the same surface — key on tool alone
    params = tr.tool_parameters or {}
    if name in ("bash", "shell"):
        cmd = _normalize_numeric(params.get("command", "").strip())
        return f"bash:{_smart_truncate(cmd)}" if cmd else None
    if name == "ssh":
        action = params.get("action", "")
        key = _normalize_numeric(
            (params.get("command", "") or params.get("script_content", "")).strip()
        )
        sig = _smart_truncate(key, 120)
        return f"ssh:{action}:{sig}" if sig else (f"ssh:{action}" if action else None)
    if name in ("write", "edit"):
        # Key on content (like _info_payload), NOT the destination path — a
        # rename with identical body is still a repeat.
        content = params.get("content")
        if content is None:
            return None
        body = _smart_truncate(_normalize_numeric(str(content)), 120)
        return f"{name}:{body}"
    if name == "read":
        path = params.get("path", "")
        return f"read:{path}" if path else None
    if name in ("glob", "grep"):
        pattern = _normalize_numeric(params.get("pattern", ""))
        return f"{name}:{_smart_truncate(pattern)}" if pattern else None
    # Generic fallback for multi-action tools (schedule_*, remote_handq, etc.):
    # key on tool + action so re-issuing the same action counts as a repeat.
    action = params.get("action", "")
    return f"{name}:{action}" if action else name


# ── Stale-snapshot supersession ──────────────────────────────────────────────
#
# In-place supersession is implemented in PersistentAgent._supersede_stale,
# which operates on ConversationTurn.observations. The earlier paired-tuple
# variant has been removed.


# ── IterationAdvisor ────────────────────────────────────────────────────────

# Stall detection (unified mechanical progress-sense). A "stall" is N consecutive
# turns that added zero new information (no_info_gain_streak) OR N consecutive
# tool failures. Two tiers:
#   - SOFT: surface a single bail-out reminder to the agent (cooldown-gated).
#   - HARD: mechanically record a progress concern on the task channel (no LLM
#     in the path) — the incident fix. Higher threshold than SOFT so the agent
#     gets a chance to self-correct before the concern is recorded.
# _STALL_COOLDOWN gates both so neither fires every turn while stuck.
_SOFT_STALL_STREAK = 3
_HARD_STALL_STREAK = 5
_HARD_FAIL_STREAK = 6
_STALL_COOLDOWN = 5
# Redundant-repeat detection: the SAME successful call (tool + normalized
# params, see repeated_call_signature) repeated this many times is a redundant
# loop even when its OUTPUT looks novel every call — the exact hole the
# output-novelty streak cannot see (Pattern A: re-running an already-succeeded
# schedule_create/desktop_screenshot forever). Folded into hard_stall so the
# loop surfaces as a mechanical ProgressConcern to the coordinator; the
# model-facing fix is the standing prompt ("stop when done, don't repeat a
# succeeded call"), CC-aligned — no per-turn mechanical reminder.
_REDUNDANT_REPEAT_HARD = 3

# todo_write reminder (CC-aligned — mirrors Claude Code's
# getTodoReminderAttachments in attachments.ts: a pure turn-count nudge, no
# semantic stall judgment). Fires when neither threshold is a judgment call —
# just "N turns since the tool was last used" and "N turns since we last said
# so" — so it can't misfire on a task that's genuinely progressing without
# a todo list. Values match Claude Code's TODO_REMINDER_CONFIG verbatim.
_TODO_REMINDER_TURNS_SINCE_WRITE = 10
_TODO_REMINDER_TURNS_BETWEEN = 10

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

    Primary output channel:
      get_reminder() — text reminder injected into the agent's per-turn
      instruction message. At most three blocks:
        1. Anti-repeat guard (specific dead-path signatures)
        2. One stall block — the specific write_param_error fix OR the
           generic soft-stall bail-out (mutually exclusive).
        3. todo_write reminder (CC-aligned turn-count nudge, independent of
           the other two — a pure "haven't seen this tool in a while" check,
           no stall judgment).

    Progress is measured by a single mechanical primitive, info_gain, recorded
    per turn. A turn shows info gain iff it wrote a novel content artifact, a
    tool observation returned previously-unseen (numeric-normalized) bytes, a
    new failure signature appeared, or it contained an intentional
    wait_interval. N consecutive turns without info gain (no_info_gain_streak)
    — or N consecutive tool failures — constitute a stall. hard_stall() reports
    when a HARD threshold is crossed so PersistentAgent can record a progress
    concern mechanically (no LLM in the trigger path — the incident fix).
    """

    def __init__(self) -> None:
        self._success_history: List[bool] = []
        self._last_error_hint: Optional[str] = None
        self._failed_approaches: Dict[str, int] = {}

        # Unified progress-sense (replaces the keyword goal-proxy). Every turn
        # is classified as info-gaining or not; _no_info_gain_streak counts
        # consecutive turns that added nothing. _seen_obs_hashes is the per-item
        # observation-novelty ledger (numeric-normalized payload hashes);
        # _turn_saw_info_gain is the per-turn flag set by record_tool_result and
        # consumed (then reset) by record_progress_signal.
        self._seen_obs_hashes: Set[str] = set()
        self._no_info_gain_streak: int = 0
        self._turn_saw_info_gain: bool = False

        # Redundant-repeat ledger: per-item count of each SUCCESSFUL call
        # signature (repeated_call_signature). Distinct from _failed_approaches
        # (tracks failures) and _seen_obs_hashes (tracks output novelty) — this
        # catches re-running an already-succeeded call whose output looks novel
        # every time, so the loop shows up as a real no-info-gain stall.
        # Coordinator-facing (feeds hard_stall → ProgressConcern); NOT a
        # per-turn model reminder — the standing prompt's "stop when done, don't
        # repeat a succeeded call" guidance is the model-facing fix (CC-aligned:
        # trust the model + faithful history + prompt, not a mechanical nag).
        self._successful_call_counts: Dict[str, int] = {}

        # Stall-block cooldowns (soft agent reminder + hard mechanical concern
        # recording), so neither fires every turn while stuck. Decremented once
        # per turn in record_progress_signal, and reset when info gain ends an
        # episode so a fresh stall can fire cleanly.
        self._soft_stall_cooldown: int = 0
        self._hard_stall_cooldown: int = 0

        # todo_write reminder counters (CC-aligned, see _TODO_REMINDER_* above).
        # _turns_since_todo_write resets on a successful todo_write call;
        # _turns_since_todo_reminder resets only when the reminder itself is
        # actually emitted (get_reminder) — two independent clocks, mirroring
        # Claude Code's turnsSinceLastTodoWrite / turnsSinceLastReminder.
        # _turn_saw_todo_write is set by record_tool_result and consumed by
        # record_progress_signal, so the turn that calls todo_write ends at
        # count 0 rather than being reset then immediately incremented back
        # to 1 (the same flag-then-consume pattern as _turn_saw_info_gain).
        self._turns_since_todo_write: int = 0
        self._turns_since_todo_reminder: int = 0
        self._turn_saw_todo_write: bool = False

    def reset_for_item(self) -> None:
        """Call at the start of each item."""
        self._success_history.clear()
        self._last_error_hint = None
        self._failed_approaches.clear()
        self._seen_obs_hashes.clear()
        self._no_info_gain_streak = 0
        self._turn_saw_info_gain = False
        self._successful_call_counts.clear()
        self._turns_since_todo_write = 0
        self._turns_since_todo_reminder = 0
        self._turn_saw_todo_write = False
        self._soft_stall_cooldown = 0
        self._hard_stall_cooldown = 0

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
        if tr.tool_name == "todo_write" and tr.success:
            self._turn_saw_todo_write = True

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

        # Redundant-repeat tracking (keyed on the CALL, not the output). If
        # this exact successful call was already made this item, it is NOT
        # progress no matter how novel its output looks — suppress the
        # output-novelty info-gain below so the stall streak can build. This
        # is the fix for the "re-run an already-succeeded schedule_create /
        # desktop_screenshot forever" loop the output hash alone can't see.
        repeat_sig = repeated_call_signature(tr)
        is_redundant_repeat = False
        if repeat_sig is not None:
            prev = self._successful_call_counts.get(repeat_sig, 0)
            self._successful_call_counts[repeat_sig] = prev + 1
            if prev >= 1:
                is_redundant_repeat = True

        payload = self._info_payload(tr)
        if not payload:
            return
        h = hashlib.blake2b(
            _normalize_numeric(payload).encode("utf-8", "replace"),
            digest_size=16,
        ).hexdigest()
        if h not in self._seen_obs_hashes:
            self._seen_obs_hashes.add(h)
            if not is_redundant_repeat:
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
        if self._soft_stall_cooldown > 0:
            self._soft_stall_cooldown -= 1
        if self._hard_stall_cooldown > 0:
            self._hard_stall_cooldown -= 1

        if self._turn_saw_todo_write:
            self._turns_since_todo_write = 0
        else:
            self._turns_since_todo_write += 1
        self._turns_since_todo_reminder += 1
        self._turn_saw_todo_write = False

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

    def _max_repeat_count(self) -> int:
        """Highest repeat count among successful call signatures this item."""
        return max(self._successful_call_counts.values(), default=0)

    def hard_stall(self) -> bool:
        """True once per stall episode when a HARD threshold is crossed.

        Crossed when no_info_gain_streak >= _HARD_STALL_STREAK, consecutive
        tool failures >= _HARD_FAIL_STREAK, OR the same successful call was
        repeated >= _REDUNDANT_REPEAT_HARD times (the redundant-repeat loop the
        output-novelty streak can't see). Cooldown-gated (_STALL_COOLDOWN) so a
        persistent stall re-records the concern periodically rather than every
        turn. PersistentAgent calls this after record_progress_signal and, when
        True, records a progress concern mechanically via set_progress_concern —
        NO LLM in the path (the incident fix).
        """
        if self._hard_stall_cooldown > 0:
            return False
        if (self._no_info_gain_streak >= _HARD_STALL_STREAK
                or self._count_consecutive_failures() >= _HARD_FAIL_STREAK
                or self._max_repeat_count() >= _REDUNDANT_REPEAT_HARD):
            self._hard_stall_cooldown = _STALL_COOLDOWN
            return True
        return False

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
                    f"tweaking and bail out: emit completion or error JSON with "
                    f"`plan_feedback` describing what you tried, what kept failing, "
                    f"and what you now suspect the real blocker is. If the "
                    f"instruction is already satisfied, complete the item. Bailing "
                    f"out when the approach is structurally wrong is the correct "
                    f"action — not a failure."
                )
                self._soft_stall_cooldown = _STALL_COOLDOWN

        # 3. todo_write reminder (CC-aligned) — pure turn-count nudge, no
        #    semantic judgment of whether a stall is happening. Fires once
        #    both counters clear their threshold, then resets the
        #    between-reminders clock so it doesn't repeat every turn.
        if (self._turns_since_todo_write >= _TODO_REMINDER_TURNS_SINCE_WRITE
                and self._turns_since_todo_reminder >= _TODO_REMINDER_TURNS_BETWEEN):
            parts.append(
                "The todo_write tool hasn't been used recently. If this task "
                "has multiple distinct steps, consider calling todo_write to "
                "list them and track which are done — this also lets the "
                "user watch your progress. Ignore if the task is a single "
                "step or todo_write wouldn't add value."
            )
            self._turns_since_todo_reminder = 0

        if not parts:
            return None
        return "\n\n".join(parts)

    def get_summary(self) -> dict:
        """Snapshot for logging at iteration cap."""
        return {
            "success_rate": self._get_success_rate(),
            "consecutive_failures": self._count_consecutive_failures(),
            "failed_approaches_count": len(self._failed_approaches),
            "max_redundant_repeat": self._max_repeat_count(),
        }

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
    # User-facing answer body — markdown shown in the chat bubble. Only
    # populated on completion turns.
    final_answer: Optional[str] = None
    # Tool-grounded audit bullets — what tools verified this task.
    verification: Optional[List[str]] = None
    artifacts: Optional[List[str]] = None
    key_findings: Optional[List[str]] = None
    claim_tool: List[str] = field(default_factory=list)
    release_tool: List[str] = field(default_factory=list)
    # Optional agent→coordinator advisory. Set by the agent (on a completion OR
    # an error turn) when something it learned this item — most often a skill
    # body read via read_skill, or a discovered fact — means the coordinator's
    # REMAINING items should change. Carried onto ItemResult and rendered into
    # the coordinator's task-plan context at the item boundary; the coordinator
    # may then replace_post_current the pending tail. Distinct from
    # key_findings (facts) and from the watcher's ProgressConcern
    # (watcher→coordinator only).
    plan_feedback: Optional[str] = None
    # Non-fatal note attached when a completion turn has evidence of
    # truncation (stop_reason=max_tokens, or JSON that only parsed after
    # json_repair salvage). Populated by _think_streaming after inspecting
    # LLMChatResult.stop_reason and by from_completion_text when parsing
    # required repair. Surfaced to the log's ITEM_END `issues` field so a
    # partial-completion never looks like a clean pass.
    truncation_note: Optional[str] = None
    # True when the completion JSON only parsed after json_repair salvage
    # (stdlib json.loads failed on the de-fenced content). This is NOT a
    # truncation signal — it fires for benign, complete-but-non-strict output
    # (trailing comma, single quotes, a raw newline inside a string, or a
    # closing pleasantry after the JSON object), all of which json_repair
    # fixes cleanly. Kept purely as a diagnostic the caller can DEBUG-log to
    # notice "this model's completion format routinely needs repair"; it must
    # NOT feed truncation_note / issues (that conflated it with real
    # truncation and mislabeled every clean completion as "truncated").
    completion_needed_repair: bool = False
    # True when the LLM emitted a completion that violated the JSON schema
    # entirely — i.e. no dict with a `reasoning` key. This is a stronger
    # signal than truncation_note: the item_loop uses it to REJECT the
    # completion (mirroring speculative-completion guard) rather than
    # accept a preview. The corrective retry sends the schema back to the
    # LLM so subsequent turns can produce a properly-structured completion
    # with long output routed into artifacts / files.
    format_violation: bool = False
    # Plain-text extended-thinking content for this turn (Claude's internal
    # reasoning before the visible reply), when the model/service enabled
    # it. DEBUG-only — written to the execution log by ExecutionRecorder so
    # a human can inspect what the model was "thinking" on a given turn;
    # never re-sent to the model (the structured, verbatim thinking_blocks
    # on the assistant message are what gets round-tripped — see
    # PersistentAgent._think_streaming / _convert_messages_to_anthropic).
    thinking_text: Optional[str] = None
    # Anthropic's stop_reason for this turn's raw response (end_turn /
    # max_tokens / tool_use / stop_sequence / None). Populated by
    # _think_streaming from LLMChatResult.stop_reason on every turn
    # (tool-call and completion alike) and surfaced by
    # ExecutionRecorder.write_turn so a truncated/odd stop is visible in
    # the trace without cross-referencing the separate DEBUG log stream.
    stop_reason: Optional[str] = None

    @property
    def is_completion(self) -> bool:
        return not self.tool_calls and not self.error

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @classmethod
    def from_completion_text(cls, raw_content: str) -> 'TurnOutcome':
        """Parse non-tool LLM output into a completion TurnOutcome.

        Attempts JSON parsing for structured fields (``final_answer`` /
        ``verification`` / ``artifacts`` / ``key_findings``). When json_repair
        had to salvage the input (a strong signal of mid-stream truncation),
        the returned TurnOutcome carries a ``truncation_note`` so the caller
        can surface it in the item's issues. Non-JSON prose sets
        ``format_violation`` and the item_loop retries.
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
            _pf = parsed.get("plan_feedback")
            _fa = parsed.get("final_answer")
            final_answer = str(_fa).strip() if _fa is not None else None
            return cls(
                reasoning=parsed.get("reasoning", ""),
                error=parsed.get("error"),
                final_answer=final_answer or None,
                verification=_coerce_str_list(parsed.get("verification")),
                artifacts=_coerce_str_list(parsed.get("artifacts")),
                key_findings=_coerce_str_list(parsed.get("key_findings")),
                claim_tool=claim,
                release_tool=release,
                # repaired means json_repair salvaged a non-strict-but-complete
                # payload — a diagnostic, NOT truncation (see field docstring).
                # Real truncation is stamped from stop_reason==max_tokens by the
                # caller, independently of this.
                completion_needed_repair=repaired,
                plan_feedback=(str(_pf).strip() or None) if _pf is not None else None,
            )

        # Fallback: the LLM returned something that isn't a JSON object with
        # a `reasoning` key — most commonly pure markdown prose ignoring the
        # completion contract. Do NOT stash the raw content as `final_answer`
        # here (that would silently let a schema-violating completion succeed
        # and bypass the corrective retry). Instead flag ``format_violation``
        # and carry the FULL raw content back as `reasoning`. The item_loop
        # reads ``format_violation`` and drives a corrective retry (see
        # persistent_agent.py speculative-completion guard) — the LLM gets the
        # schema pointed out and re-emits a proper JSON completion, moving the
        # user-facing content into `final_answer`.
        #
        # We keep the raw content whole (no truncation here) so downstream
        # observers see what the model actually attempted: ExecutionRecorder
        # writes it into the JSONL turn record (its own MAX_OUTPUT_LEN cap
        # applies there), and notify_decision_made surfaces it to the UI so a
        # rejected summary is not silently lost.
        return cls(
            reasoning=raw_content if raw_content else "Failed to parse LLM response.",
            final_answer=None,
            verification=None,
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
