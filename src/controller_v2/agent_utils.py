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
#
# notify_user is here for the same reason: TELLING the user to press a button is
# not the button being pressed. The tool's own output says so explicitly, but
# the guard must not be able to accept "I informed the user" as evidence the
# world changed.
BOOKKEEPING_ONLY_TOOLS: frozenset = frozenset({
    "todo_write", "read_skill", "claim_tool", "release_tool", "notify_user",
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
    # User messages received BEFORE this turn (mid-task instructions/
    # corrections), oldest first. Rendered as one role:"user" message in
    # _build_messages at its natural position (joined if more than one
    # arrived between turns). Participates in compression normally (no
    # special immunity).
    user_interjection: List[str] = field(default_factory=list)

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
        total = obs_chars + asst_chars
        for msg in self.user_interjection:
            total += len(msg)
        return total


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

    ``chars_per_token = 2.0`` is calibrated for the mixed CJK content HandQ
    actually carries. The previous 3.5 was an English-text figure: Chinese runs
    roughly 1–1.5 chars per token, so a Chinese-heavy history (HandQ's own
    prompts, Chinese task instructions, Chinese tool output) costs ~2-3x the
    tokens the old estimate predicted. That gap is invisible until the request
    is rejected, because every budget check in the agent is denominated in
    chars. 2.0 stays slightly optimistic for pure CJK and slightly pessimistic
    for pure ASCII — deliberately, since over-estimating capacity is the
    failure mode that costs a turn and under-estimating only costs some
    history depth.

    NOTE there is deliberately NO absolute floor. A floor larger than what the
    window can hold is not a safety net, it is a guaranteed rejection: the old
    ``max(budget, 300_000)`` would hand a hypothetical 100k-token model a
    300,000-char budget it could never satisfy. The computed value already IS
    the floor. The 20k guard below only rejects degenerate/absurd inputs.
    """
    chars_per_token = 2.0
    utilization = 0.85
    fixed_overhead_tokens = 10_000
    available_tokens = int((context_window - fixed_overhead_tokens) * utilization)
    budget = int(available_tokens * chars_per_token)
    return max(budget, 20_000)


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


# ── GUI approach signatures (desktop_* / browser_*) ─────────────────────────
#
# Why these tools need their own branch: failed_approach_signature returned
# None for every name outside its whitelist, so ``_failed_approaches`` never
# recorded a single desktop_* / browser_* failure and the ANTI-REPEAT GUARD was
# structurally blind to GUI dead ends. The 2026-08-01 Alpaca trace hit the
# IDENTICAL PyAutoGUI-failsafe error 7 times across 48 minutes, the IDENTICAL
# capture error 12 times across 46 minutes, and re-issued
# desktop_click_at(x=328, y=605) with byte-identical params on two consecutive
# turns — and the guard never said a word. Note the asymmetry this fixes:
# repeated_call_signature below already has a generic fallback, so GUI
# SUCCESSES were tracked while GUI FAILURES were not — exactly backwards.

# Parameters that select a capture / injection PATH rather than an aim point.
# These MUST stay in the signature: region='foreground' and region='fullscreen'
# are structurally different approaches, and collapsing them would make the
# guard say "stop using this tool" when the real fix was to switch this one
# argument — which is precisely how that trace finally escaped its 14-minute
# capture-failure loop.
_GUI_PATH_KEYS: tuple = ("region", "use_uia_pattern")

# Parameters naming WHAT the call aimed at, in priority order. The first one
# present becomes the signature's target, so "find Connect" and "find Boot MD
# EDL" stay distinct dead paths instead of merging into one.
_GUI_TARGET_KEYS: tuple = ("description", "selector", "url", "text", "keys")


def _gui_approach_signature(name: str, params: Dict[str, Any]) -> str:
    """Return a signature for a failed ``desktop_*`` / ``browser_*`` call.

    Coordinates are deliberately EXCLUDED. Every hard error these tools raise
    — PyAutoGUI failsafe, "cannot determine foreground window", a capture /
    BitBlt failure, a refused sensitive-window guard — is independent of the
    exact pixel aimed at, so keying on x/y would let one dead approach
    re-register as novel on every click and defeat the point of tracking it.
    """
    bits: List[str] = [name]
    action = str(params.get("action") or "")
    if action and not name.endswith(action):
        bits.append(action)
    if params.get("hwnd"):
        # Presence, not value: the hwnd path runs through PrintWindow instead
        # of mss, and that is the structural difference worth keying on. Exact
        # handles churn — they change every time the target app restarts — and
        # would fragment one dead path into many.
        bits.append("hwnd=set")
    for key in _GUI_PATH_KEYS:
        val = params.get(key)
        if val not in (None, ""):
            bits.append(f"{key}={val}")
    for key in _GUI_TARGET_KEYS:
        val = params.get(key)
        if val not in (None, ""):
            bits.append(_smart_truncate(_normalize_numeric(str(val)), 80))
            break
    return ":".join(bits)


def failed_approach_signature(
    tr: ToolResult, script_index: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Return a compact, stable signature for a failed ToolResult.

    Used to detect when the same approach is retried after already failing.
    Returns None for results that should not be tracked.

    ``script_index`` maps a written script's basename → the semantic
    fingerprint of its CONTENT (see :func:`script_semantic_fingerprint`). When a
    shell command runs an indexed script, the signature keys on that fingerprint
    instead of the command text — see the module comment above
    ``script_semantic_fingerprint`` for why filenames are worthless here.
    """
    params = tr.tool_parameters or {}
    name = tr.tool_name or ""
    if name == "bash" or name == "shell":
        raw = params.get("command", "").strip()
        script_sig = _script_run_signature(raw, script_index)
        if script_sig:
            return script_sig
        cmd = _normalize_numeric(raw)
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
    if name.startswith(("desktop_", "browser_")) or name in ("desktop", "browser"):
        return _gui_approach_signature(name, params)
    return None


# ── Generated-script identity ────────────────────────────────────────────────
#
# A shell signature keyed on the command text is blind to the dominant loop
# shape in agent-written automation: write a script, run it once, read a partial
# failure, write a NEW script with a NEW NAME, run that once, repeat.
#
# 2026-08-03 flash-meta run: 55 files written, 33 of them `cdp_*.py`, each
# executed exactly once. Nine near-duplicate SPINOR drivers
# (`cdp_spinor_download` → `_proper` → `_debug` → `_final` → `_reconnect`), five
# UFS variants, four reboot-verify variants. Every turn therefore carried a
# brand-new action signature, the ANTI-REPEAT GUARD saw novelty forever, and it
# never fired again after turn 140 — while the last 60 turns changed nothing.
#
# Two scripts are "the same approach" when they act on the same things, so the
# fingerprint is taken over the distinctive STRING LITERALS in the body (CSS
# selectors, control names, API methods, paths) rather than the filename or the
# surrounding code. Rewording a print() or renaming the file does not move the
# fingerprint; genuinely targeting a different control does.
#
# The literal must CONTAIN a letter (so pure punctuation / numbers are skipped)
# but may START with a selector sigil — ``.p-treetable-toggler`` and
# ``#device-list`` are precisely the tokens worth keying on, and requiring an
# alpha first char would drop every CSS class/id selector.
_SCRIPT_LITERAL_RE = re.compile(r"""['"]([\w.#][\w \-./#\[\]=:*]{2,60})['"]""")

# Literals that appear in almost every generated script and carry no information
# about WHAT it targets. Left in, they would pull unrelated scripts together.
_SCRIPT_NOISE_LITERALS: frozenset = frozenset({
    "utf-8", "utf8", "ascii", "replace", "ignore", "strict",
    "localhost", "http", "https", "get", "post", "json", "text/plain",
    "true", "false", "none", "null", "error", "warning", "info", "debug",
    "windows-1252", "cp1252", "\\n", "\\r\\n",
})

_SCRIPT_SUFFIXES: tuple = (".py", ".ps1", ".psm1", ".sh", ".bash", ".js", ".bat", ".cmd")
# How many literals feed the hash. Enough to characterise a script, few enough
# that appending one more probe line does not move the fingerprint.
_SCRIPT_FINGERPRINT_LITERALS: int = 24
# Below this a script has too little distinctive content to fingerprint
# meaningfully; fall back to the command text rather than collapse unrelated
# one-liners together.
_SCRIPT_FINGERPRINT_MIN_LITERALS: int = 3


def _is_structural_literal(lit: str) -> bool:
    """True for literals that identify WHAT a script targets, not free prose.

    Structural = a selector/path/identifier: it carries a sigil
    (``. # / \\ [ ] : =``) or is a single whitespace-free token. Multi-word
    prose ("DONE v2", "Clicking the gear", a status message) is excluded, which
    is exactly what makes "rewording a print() does not move the fingerprint"
    true — the tweak-and-rerun loop rewords prints and prose constantly while
    the CSS selectors, API methods and paths it targets stay put.
    """
    if any(sig in lit for sig in (".", "#", "/", "\\", "[", "]", ":", "=")):
        return True
    return " " not in lit and "\t" not in lit


def script_semantic_fingerprint(content: str) -> str:
    """Fingerprint a generated script by WHAT IT TARGETS, not what it is called.

    Returns '' when the body has too few distinctive literals to characterise —
    the caller then keeps the command-text signature rather than merging
    unrelated scripts.
    """
    if not content:
        return ""
    lits = set()
    for m in _SCRIPT_LITERAL_RE.finditer(content):
        lit = _normalize_numeric(m.group(1)).strip().lower()
        # Require a letter (skip "===", pure numbers), reject known noise, and
        # keep only structural target literals so prose churn is ignored.
        if (lit and lit not in _SCRIPT_NOISE_LITERALS
                and any(c.isalpha() for c in lit)
                and _is_structural_literal(lit)):
            lits.add(lit)
    if len(lits) < _SCRIPT_FINGERPRINT_MIN_LITERALS:
        return ""
    top = sorted(lits)[:_SCRIPT_FINGERPRINT_LITERALS]
    return hashlib.blake2b(
        "|".join(top).encode("utf-8", "replace"), digest_size=8,
    ).hexdigest()


def indexable_script_path(tr: ToolResult) -> Optional[str]:
    """Basename of the script a ``write`` created, or None if not a script."""
    if (tr.tool_name or "") not in ("write", "edit"):
        return None
    path = str((tr.tool_parameters or {}).get("path") or "")
    if not path:
        return None
    base = path.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return base if base.endswith(_SCRIPT_SUFFIXES) else None


def _script_run_signature(
    command: str, script_index: Optional[Dict[str, str]],
) -> Optional[str]:
    """``script:<fingerprint>`` when *command* runs a script we have indexed."""
    if not command or not script_index:
        return None
    lowered = command.replace("\\", "/").lower()
    for base, fingerprint in script_index.items():
        if base and fingerprint and base in lowered:
            return f"script:{fingerprint}"
    return None


# ── Shared preconditions that whole tool families depend on ──────────────────
#
# The ANTI-REPEAT GUARD keys on the TOOL, so a dead dependency shared by several
# tools reads as several independent, still-untried approaches.
#
# 2026-08-03, turns 38-42, verbatim from the agent's own reasoning:
#   "The ANTI-REPEAT GUARD blocks `desktop_click_at:use_uia_pattern=False`."
#   "`desktop_find_and_click` is NOT blocked - this is a different tool"
#   "`desktop_find_and_click` also relies on PyAutoGUI internally for mouse
#    clicks, so it might hit the same failsafe issue"
#   "Since the ANTI-REPEAT GUARD only blocks the specific `desktop_click_at`
#    variant, I have a few paths forward..."
# It wrote down that the substitute shares the broken dependency, and used it
# anyway, because the guard's unit of blocking was a name. Modelling the
# DEPENDENCY as the thing that fails closes that: one failure greys out every
# tool that needs it.
_CAPABILITY_SIGNATURES: Dict[str, tuple] = {
    "pointer_injection": (
        "failsafe", "pyautogui", "sendinput", "cursor_at_exception",
    ),
    "foreground_window": (
        "cannot determine foreground window", "getforegroundwindow",
        "no foreground window",
    ),
    "uia_tree": (
        "uia blacklist", "snapshot enumerate exceeded", "uia call",
        "not uia-visible",
    ),
}

# Which tools each capability gates. Prefixes match by startswith.
_CAPABILITY_DEPENDENTS: Dict[str, tuple] = {
    "pointer_injection": (
        "desktop_click_at", "desktop_find_and_click", "desktop_drag",
        "desktop_scroll", "desktop_hover_at", "desktop_type_text",
        "desktop_hotkey", "desktop_key_press",
    ),
    "foreground_window": (
        "desktop_click_at", "desktop_find_and_click", "desktop_find_element",
        "desktop_type_text", "desktop_screenshot", "desktop_snapshot",
        "desktop_hotkey", "desktop_key_press", "desktop_scroll", "desktop_drag",
    ),
    "uia_tree": (
        "desktop_snapshot", "desktop_find_element", "desktop_find_and_click",
    ),
}

_CAPABILITY_ADVICE: Dict[str, str] = {
    "pointer_injection": (
        "Synthetic pointer/keyboard input is not reaching this desktop. Every "
        "tool above shares that one dependency, and hand-rolled SendInput / "
        "PostMessage / SetForegroundWindow in a script shares it too — swapping "
        "between them cannot help. Drive the app through a non-input channel: "
        "its own automation API or SDK, a CLI, a config file, or CDP for "
        "Electron/Chromium apps. If a human press is genuinely required, use "
        "notify_user to ask for it."
    ),
    "foreground_window": (
        "This session has no foreground window (RDP / VNC / service session). "
        "Activating, maximizing or re-aiming will not create one. Anything that "
        "needs focus or screen coordinates is unavailable for the whole "
        "session — do not spend further turns on it."
    ),
    "uia_tree": (
        "This window exposes no usable UIA tree (Electron/Chromium or a "
        "custom-rendered surface). Element lookups against it will keep "
        "failing, and 'none_detected' from it is uninformative rather than "
        "negative. Use CDP for Chromium-based apps, or the app's own API."
    ),
}


def failed_capabilities(tr: ToolResult) -> List[str]:
    """Capabilities implicated by a failed ToolResult's error text."""
    if tr.success:
        return []
    blob = f"{tr.error or ''} {tr.output if tr.output is not None else ''}".lower()
    if not blob.strip():
        return []
    return [
        cap for cap, needles in _CAPABILITY_SIGNATURES.items()
        if any(n in blob for n in needles)
    ]


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


def repeated_call_signature(
    tr: ToolResult, script_index: Optional[Dict[str, str]] = None,
) -> Optional[str]:
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
        raw = params.get("command", "").strip()
        # Same reasoning as in failed_approach_signature: a re-run of an
        # equivalent generated script is a repeat even under a new filename.
        script_sig = _script_run_signature(raw, script_index)
        if script_sig:
            return script_sig
        cmd = _normalize_numeric(raw)
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

        # basename → semantic fingerprint of every script the agent has WRITTEN
        # this item, so a `shell` call that runs one is signed by what the script
        # targets rather than by its (freshly-invented) filename. See
        # script_semantic_fingerprint for the 33-one-shot-scripts loop this
        # exists to make visible.
        self._script_index: Dict[str, str] = {}

        # capability → number of failures attributed to it this item. A dead
        # SHARED DEPENDENCY (pointer injection, foreground window, UIA tree) is
        # the unit the agent kept routing around by switching tool names; see
        # _CAPABILITY_SIGNATURES.
        self._failed_capabilities: Dict[str, int] = {}

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
        self._script_index.clear()
        self._failed_capabilities.clear()
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

        # Index every script the agent writes BEFORE any signature is computed,
        # so the very first run of a freshly-written script is already keyed on
        # its content rather than its name.
        script_base = indexable_script_path(tr)
        if script_base:
            fingerprint = script_semantic_fingerprint(
                str((tr.tool_parameters or {}).get("content") or "")
            )
            if fingerprint:
                self._script_index[script_base] = fingerprint

        self._success_history.append(tr.success)

        if not tr.success:
            if (tr.tool_name == "write"
                    and "Parameter error for tool 'write'" in (tr.error or "")):
                self._last_error_hint = "write_param_error"
            # Attribute the failure to any shared dependency it implicates, so
            # switching to a sibling tool that needs the same thing is no longer
            # invisible to the guard.
            for cap in failed_capabilities(tr):
                self._failed_capabilities[cap] = self._failed_capabilities.get(cap, 0) + 1
            if tr.tool_name and tr.tool_name not in INFRA_TOOL_NAMES:
                sig = failed_approach_signature(tr, self._script_index)
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
        repeat_sig = repeated_call_signature(tr, self._script_index)
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
            # write/edit is authoring, not observing — producing a new script
            # variant is not information gain (running it and getting novel
            # output IS). Without this, "write cdp_vN.py (new content hash) +
            # run it (same output)" resets the stall streak every cycle.
            if not is_redundant_repeat and tr.tool_name not in ("write", "edit"):
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

        # 1b. Dead-capability guard — the fix for switching tool NAMES to route
        #     around a broken shared dependency. When a capability (pointer
        #     injection, foreground window, UIA tree) has failed 2+ times, name
        #     EVERY tool that depends on it, so the agent cannot treat a sibling
        #     that needs the same thing as an untried path. This is the guard the
        #     2026-08-03 run defeated by hopping desktop_click_at →
        #     desktop_find_and_click while noting they share PyAutoGUI.
        dead_caps = sorted(
            [(cap, cnt) for cap, cnt in self._failed_capabilities.items() if cnt >= 2],
            key=lambda x: -x[1],
        )
        if dead_caps:
            cap_lines: List[str] = []
            for cap, cnt in dead_caps:
                dependents = ", ".join(_CAPABILITY_DEPENDENTS.get(cap, ()))
                advice = _CAPABILITY_ADVICE.get(cap, "")
                cap_lines.append(
                    f"  [{cap}] failed {cnt}x. This blocks ALL of: {dependents}. "
                    f"{advice}"
                )
            parts.append(
                "DEAD-CAPABILITY GUARD — a shared dependency is failing, not just "
                "one tool. Switching to another tool in the SAME list below will "
                "hit the identical wall (including any SendInput / PostMessage / "
                "CDP-over-shell workaround you might write that leans on it):\n"
                + "\n".join(cap_lines)
            )

        # 1c. Redundant-repeat signatures (successful calls repeated 3+×)
        redundant = sorted(
            [(sig, cnt) for sig, cnt in self._successful_call_counts.items() if cnt >= _REDUNDANT_REPEAT_HARD],
            key=lambda x: -x[1],
        )[:3]
        if redundant:
            sigs = ", ".join(f"{sig} ({cnt}×)" for sig, cnt in redundant)
            parts.append(f"⚠ Redundant: {sigs} — switch approach.")

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
            "dead_capabilities": sorted(
                c for c, n in self._failed_capabilities.items() if n >= 2
            ),
            "scripts_indexed": len(self._script_index),
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


# ── Completion audit ────────────────────────────────────────────────────────
#
# Why this exists: the 2026-08-01 flash-meta trace SHIPPED a fabricated
# delivery. Ten background shell tasks were launched (bg_1..bg_10) and not one
# was ever observed in a terminal state — every poll came back
# ``status="running", exit_code=null``. The agent then wrote item_end
# verification bullets quoting a downloader's exact output:
#     "xPCAT UFS flat build download: DEVICE_NO_ERROR (0),
#      progress 4.96%->99.94%->100% at 13:37:37"
# Those strings appear NOWHERE in any tool output from that session. The only
# real input behind them was "bg_10 has been running 105 seconds". Ten minutes
# later the agent contradicted its own delivery ("boot_a … not correctly
# written") — but by then the item was already recorded as success.
#
# The two existing completion guards structurally cannot catch this. They check
# the SHAPE of the completion turn — was it valid JSON, did any world-touching
# tool run — and both were satisfied: plenty of real shell calls ran. What was
# wrong was the CONTENT of the claim relative to the observation history, and no
# component was keeping that history. This class keeps it.
#
# Two independent mechanical checks (no LLM in the trigger path):
#   1. unresolved background work — a task launched this item and never once
#      observed done/killed cannot back a completion. "Still running" is not
#      "succeeded".
#   2. unsourced claims — a distinctive token in a `verification` bullet that
#      appeared in no tool output AND in nothing the user supplied was not
#      observed; it was invented.

# Token classes distinctive enough that quoting one you never observed is
# fabrication rather than coincidence. Deliberately NOT bare integers: "0",
# "1", "exit 0" occur in nearly every output and would false-positive on every
# honest completion.
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")              # 4.96%, 99.94 %
_SNAKE_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b")  # DEVICE_NO_ERROR
# Clock times, with an optional AM/PM suffix. A 12-hour reading is registered
# under BOTH its literal and its 24-hour equivalent: an agent that reads
# "1:37:37 PM" out of a log and writes "13:37:37" in a verification bullet is
# reformatting, not fabricating, and must not trip the guard. A timestamp
# matching neither form was genuinely never observed.
_CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})(?:\s*([AaPp])[.\s]?[Mm])?")

# Background-task states that count as "observed to have finished". Mirrors
# shell_tool.BackgroundTask.status ("running" | "done" | "killed").
_BG_TERMINAL_STATES: frozenset = frozenset({"done", "killed"})


def _claim_tokens(text: str) -> Set[str]:
    """Extract the canonicalised distinctive tokens from *text*."""
    found: Set[str] = set()
    for rx in (_PERCENT_RE, _SNAKE_RE):
        for match in rx.findall(text or ""):
            found.add(re.sub(r"\s+", "", match).upper())
    for hh, mm, ss, meridiem in _CLOCK_RE.findall(text or ""):
        hour = int(hh)
        found.add(f"{hour:02d}:{mm}:{ss}")
        if meridiem:
            shifted = (hour % 12) + (12 if meridiem.lower() == "p" else 0)
            found.add(f"{shifted:02d}:{mm}:{ss}")
    return found


class CompletionAudit:
    """Per-item observation ledger backing the completion-audit guard.

    Fed the same ToolResults as IterationAdvisor, plus the background-task
    completion observations that arrive out-of-band via
    PersistentAgent._poll_completed_background_tasks (those never pass through
    the per-turn advisor loop, so wiring only one of the two would let a task
    that DID finish still read as unresolved).

    Every method is best-effort and never raises: a broken ledger must not be
    able to block a legitimate completion.
    """

    def __init__(self) -> None:
        self._bg_launched: Dict[str, str] = {}
        self._bg_resolved: Set[str] = set()
        self._sourced: Set[str] = set()

    def reset_for_item(self, instruction: str = "") -> None:
        """Start a fresh ledger for one item.

        The item's own instruction seeds the sourced set: identifiers the USER
        supplied (``MEMORY_TYPE_UFS``, a build id, a target time) are
        legitimately quotable in verification even though no tool emitted them.
        Without this seed the guard would false-positive on every task whose
        instruction names a constant.
        """
        self._bg_launched.clear()
        self._bg_resolved.clear()
        self._sourced = _claim_tokens(instruction)

    def record_tool_result(self, tr: ToolResult) -> None:
        """Fold one tool result into the ledger."""
        try:
            self._sourced |= _claim_tokens(self._result_text(tr))
            self._track_background(tr)
        except Exception:
            pass

    @staticmethod
    def _result_text(tr: ToolResult) -> str:
        parts: List[str] = []
        if tr.output is not None:
            parts.append(str(tr.output))
        if tr.error:
            parts.append(str(tr.error))
        if getattr(tr, "exit_code", None) is not None:
            parts.append(f"exit_code={tr.exit_code}")
        return "\n".join(parts)

    def _track_background(self, tr: ToolResult) -> None:
        """Record launches and terminal observations of background tasks.

        Keyed off the ``task_id`` present in every background-shaped shell
        output — the launch result, a status poll, a kill, the ``tasks`` list,
        and the out-of-band completion observation all carry it, so one branch
        covers all five shapes.
        """
        out = tr.output if isinstance(tr.output, dict) else {}
        records: List[Dict[str, Any]] = []
        if out.get("task_id"):
            records.append(out)
        listed = out.get("tasks")
        if isinstance(listed, list):
            records.extend(
                e for e in listed if isinstance(e, dict) and e.get("task_id")
            )
        for rec in records:
            task_id = str(rec.get("task_id"))
            status = str(rec.get("status") or "")
            if status in _BG_TERMINAL_STATES or rec.get("exit_code") is not None:
                self._bg_resolved.add(task_id)
            elif status == "running":
                self._bg_launched.setdefault(
                    task_id, str(rec.get("command") or "")[:70]
                )

    @property
    def unresolved_background(self) -> List[tuple]:
        """[(task_id, command)] launched this item, never seen finishing."""
        return sorted(
            (tid, cmd) for tid, cmd in self._bg_launched.items()
            if tid not in self._bg_resolved
        )

    def unsourced_claims(self, verification: Optional[List[str]]) -> List[tuple]:
        """[(claim, [missing_tokens])] for bullets quoting unobserved values."""
        hits: List[tuple] = []
        for claim in (verification or []):
            missing = sorted(_claim_tokens(claim) - self._sourced)
            if missing:
                hits.append((claim, missing))
        return hits

    def audit(self, verification: Optional[List[str]]) -> Optional[str]:
        """Return a rejection message when this completion is not observable.

        None means the completion may proceed. The caller (PersistentAgent's
        completion-guard block) is responsible for the fail-open cap, so a
        false positive here costs a few corrective turns, never an infinite
        loop.
        """
        problems: List[str] = []

        pending = self.unresolved_background
        if pending:
            listed = "; ".join(
                f"{tid} ({cmd})" if cmd else tid for tid, cmd in pending
            )
            problems.append(
                f"{len(pending)} background task(s) were launched this item and "
                f"never observed finishing: {listed}. A task last seen "
                f"status='running' has NOT succeeded — elapsed time is not an "
                f"exit code. Before completing, either poll it with "
                f"shell(task_id='<id>') until status is 'done'/'killed' and "
                f"read the exit_code, or kill it with "
                f"shell(task_id='<id>', kill=True), or return an error JSON "
                f"stating the work did not finish."
            )

        unsourced = self.unsourced_claims(verification)
        if unsourced:
            lines = "\n".join(
                f"  - {claim[:120]!r} cites {', '.join(toks)}"
                for claim, toks in unsourced[:5]
            )
            problems.append(
                "These verification bullets quote values that appear in NO tool "
                "output from this item and in nothing the user gave you, so they "
                f"were never observed:\n{lines}\n"
                "Cite the actual tool result that produced each value, or delete "
                "the claim. A number you inferred from elapsed time must not be "
                "restated as something a tool reported."
            )

        if not problems:
            return None
        return "Completion audit failed. " + " ".join(problems)


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
        # completion contract, but ALSO the empty-string case (all output
        # tokens landed in a thinking block, zero in the visible completion
        # text — confirmed live 2026-08-06, stop_reason=end_turn out_tokens>0
        # raw_len=0). Do NOT stash the raw content as `final_answer`
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
        #
        # format_violation is UNCONDITIONALLY True here — reaching this
        # return means parsing already failed to find a JSON dict with a
        # `reasoning` key, regardless of whether raw_content is empty or
        # non-empty prose. `bool(raw_content)` was wrong: an empty completion
        # (raw_len=0) evaluated to False, silently skipping the corrective
        # retry and letting a blank turn stand as the item's "final" result
        # (item_end recorded success=True with reasoning stuck at the
        # "Failed to parse LLM response." placeholder below, while the
        # session itself crashed) — confirmed live 2026-08-06.
        return cls(
            reasoning=raw_content if raw_content else "Failed to parse LLM response.",
            final_answer=None,
            verification=None,
            truncation_note=(
                "Completion output was not valid JSON with a `reasoning` key; "
                f"raw_len={len(raw_content)}; item_loop will request a retry."
                if raw_content else
                "Completion output was empty (all output tokens went into a "
                "thinking block, none into visible completion text); "
                "item_loop will request a retry."
            ),
            format_violation=True,
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
