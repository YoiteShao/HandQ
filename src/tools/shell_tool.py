"""
Shell Tool - Cross-platform shell command execution with background support.

Replaces the legacy bash_tool with full support for:
- PowerShell (Windows default) and sh/bash (Linux default)
- Background execution with task management
- CWD persistence across calls within a session
- Extended timeouts (up to 600s)
- Kill-at-any-time for background tasks
"""
import asyncio
import atexit
import logging
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# ── Platform constants ────────────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# ── Timeout ───────────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT_SECONDS: int = 120
_MAX_TIMEOUT_SECONDS: int = 600

# Cap on how long we wait for a killed process to actually exit before giving
# up on the wait (the kill signal itself is unconditional and already sent by
# this point). On Windows, a process blocked in kernel-mode I/O against an
# unresponsive UNC/SMB share can survive `taskkill /F` / TerminateProcess
# until that I/O itself resolves — which can take minutes, unrelated to any
# tool-level timeout. Without this cap, the caller (PersistentAgent's
# iteration loop, awaiting the tool call) blocks indefinitely, which in turn
# never reaches `acknowledge_interrupt()` — permanently starving every
# subsequent `TaskChannel.interrupt_agent()` call for the rest of the session.
# Confirmed live 2026-07-30: a hung UNC `Get-ChildItem` survived two user
# interrupts; the session had to be destroyed. We give up WAITING, not
# killing — the signal was already sent.
_KILL_WAIT_TIMEOUT_SECONDS: float = 10.0

# ── Output truncation ─────────────────────────────────────────────────────────
_TRUNCATION_TOTAL_CHARS: int = 15_000
_TRUNCATION_HEAD_CHARS: int = 5_000
_TRUNCATION_TAIL_CHARS: int = 5_000

# ── Concurrency-safety heuristic (server-side fallback for concurrent_safe) ───
#
# The model can declare `concurrent_safe: true` explicitly (see the tool
# schema's description in tool_registry.py, which lists these exact commands
# as examples) — this heuristic exists for the common case where it forgets
# to, so read-only commands don't get needlessly serialized behind unrelated
# in-flight tool calls (see PersistentAgent._is_concurrency_safe_call). It is
# intentionally conservative: false negatives (a safe command left
# unmarked) just cost a little concurrency; false positives (an unsafe
# command wrongly marked safe) would be a real correctness bug, so anything
# ambiguous is treated as unsafe. An explicit `concurrent_safe: false` from
# the model always wins — this heuristic only fills the gap when the model
# didn't say anything.
_READONLY_COMMAND_PREFIXES = frozenset({
    "ls", "find", "grep", "wc", "cat", "head", "tail", "which", "type",
    "pwd", "echo", "env", "printenv", "whoami", "hostname", "date",
    "get-childitem", "test-path", "select-string", "get-content",
    "get-location", "get-item", "get-process",
})
# Multi-word prefixes (subcommands) checked against the first TWO tokens.
_READONLY_SUBCOMMAND_PREFIXES = frozenset({
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
    ("git", "branch"), ("git", "remote"), ("git", "blame"),
    ("test", "-f"), ("test", "-d"), ("test", "-e"),
})
# Any of these appearing as the second token is a read-only probe regardless
# of the leading command (e.g. `python --version`, `node --version`).
_VERSION_PROBE_FLAGS = frozenset({"--version", "-v", "-V", "version"})


def looks_read_only(command: str) -> bool:
    """Heuristic: does this command look safe to run concurrently?

    Deliberately narrow — matches only the LEADING command of the (first)
    chained segment, never a substring anywhere in the string. A command
    that chains a read-only prefix into something else via ``&&``/``;``/
    ``|``/``|>`` etc. is NOT considered safe: e.g. ``ls && rm -rf x`` must
    not short-circuit on ``ls``. Only a command with nothing chained after
    the read-only leading word (aside from further pipe-safe read tools) is
    treated as safe. Empty/unparseable input is never safe.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    # Any of these chain/redirect operators mean "more than one command" —
    # bail to unsafe rather than try to prove every segment is read-only.
    for op in ("&&", "||", ";", ">", ">>", "<", "$(", "`"):
        if op in cmd:
            return False
    # A pipeline of read-only-looking segments is fine (e.g.
    # `Get-ChildItem | Select-String foo`) — check every segment.
    segments = [seg.strip() for seg in cmd.split("|") if seg.strip()]
    if not segments:
        return False
    return all(_segment_is_read_only(seg) for seg in segments)


def _segment_is_read_only(segment: str) -> bool:
    tokens = segment.split()
    if not tokens:
        return False
    head = tokens[0].lower().lstrip("./")
    for suffix in (".exe", ".ps1"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    if len(tokens) >= 2 and tokens[1].lower() in _VERSION_PROBE_FLAGS:
        return True
    if len(tokens) >= 2 and (head, tokens[1].lower()) in _READONLY_SUBCOMMAND_PREFIXES:
        return True
    if head in _READONLY_COMMAND_PREFIXES:
        return True
    return False

# ── Background task output buffer cap ─────────────────────────────────────────
_BG_BUFFER_MAX_BYTES: int = 15_000

# ── Shell metacharacter injection detection ───────────────────────────────────
_INJECTION_PATTERN = re.compile(
    r"""(?x)
    `[^`]+`
    |
    \x00
    """
)


def _check_injection(command: str) -> None:
    if _INJECTION_PATTERN.search(command):
        logger.warning(
            "ShellTool: possible shell metacharacters detected in command — "
            "verify this is intentional. Command (truncated): %.200s",
            command,
        )


# ── Dangerous command blocking ────────────────────────────────────────────────
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r'\brm\b.*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+[/~](?:\s|$)'),
        "recursive force-delete of / or ~ would destroy the filesystem",
        "scope the delete to a specific subdirectory, e.g. rm -rf ./tmp/",
    ),
    (
        re.compile(r'\brm\b.*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+[/~](?:\s|$)'),
        "recursive force-delete of / or ~ would destroy the filesystem",
        "scope the delete to a specific subdirectory, e.g. rm -rf ./tmp/",
    ),
    (
        re.compile(r'\bfind\b(?:\s+-\w+)*\s+[/~](?:\s|$).*(?<!\w)-delete\b'),
        "find -delete starting at / or ~ would destroy the filesystem",
        "scope find to a specific subdirectory, e.g. find ./tmp/ -delete",
    ),
    (
        re.compile(r'\bfind\b(?:\s+-\w+)*\s+[/~](?:\s|$).*(?<!\w)-exec\s+rm\b'),
        "find -exec rm starting at / or ~ would destroy the filesystem",
        "scope find to a specific subdirectory, e.g. find ./tmp/ -exec rm {} \\;",
    ),
    (
        re.compile(r'\bdd\b.*\bof=/dev/(?:sd|hd|vd|nvme|xvd|mmcblk)[a-z0-9]'),
        "dd to a raw block device would overwrite disk data",
        "use dd only with file paths, not raw device nodes",
    ),
    (
        re.compile(r'\bmkfs(?:\.[a-z0-9]+)?\b'),
        "mkfs formats a filesystem and destroys all data on the target device",
        "do not format devices; use file-level operations instead",
    ),
    (
        re.compile(r':\(\)\s*\{.*:\|:.*\}'),
        "fork bomb detected — would exhaust system process table",
        "do not run fork bombs",
    ),
    (
        re.compile(r'\b(?:chmod|chown)\b.*-[a-zA-Z]*R[a-zA-Z]*\s+[^\s]*\s+/(?:\s|$)'),
        "recursive permission change on / would corrupt system file permissions",
        "scope permission changes to a specific subdirectory",
    ),
    (
        re.compile(r'\bchmod\b\s+[0-7]{3,4}\s+/etc/(?:passwd|shadow|sudoers)\b'),
        "changing permissions on /etc/passwd, /etc/shadow, or /etc/sudoers would break authentication",
        "do not modify permissions on system authentication files",
    ),
    (
        re.compile(r'>\s*/etc/(?:passwd|shadow|sudoers)'),
        "overwriting /etc/passwd, /etc/shadow, or /etc/sudoers would break authentication",
        "do not overwrite system authentication files",
    ),
]

_DANGEROUS_PATTERNS_WIN: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r'\b(?:rmdir|rd)\b.*?/[sS]\s+["\']?[A-Za-z]:\\(?:\s|$|["\'])'),
        "recursive delete of drive root would destroy the filesystem",
        "scope the delete to a specific subdirectory, e.g. rmdir /s /q .\\tmp",
    ),
    (
        re.compile(r'\bdel\b.*?/[sS]\s+["\']?[A-Za-z]:\\(?:\s|$|["\'])'),
        "recursive delete of drive root would destroy the filesystem",
        "scope the delete to a specific subdirectory",
    ),
    (
        re.compile(r'\bformat\b\s+[A-Za-z]:'),
        "format would destroy all data on the target drive",
        "do not format drives; use file-level operations instead",
    ),
    (
        re.compile(r'Remove-Item\b.*?-Recurse.*?["\']?[A-Za-z]:\\["\']?\s', re.IGNORECASE),
        "recursive Remove-Item on drive root would destroy the filesystem",
        "scope to a specific subdirectory",
    ),
]


# ── Self-termination guard ────────────────────────────────────────────────────
# The agent runs *inside* the bridge process, so a shell command that kills the
# bridge's OWN PID terminates the agent mid-task. This is not hypothetical: an
# agent that recompiled the bridge ran `taskkill /F /PID <n>` to free the locked
# exe for a hot-swap — the PID was its own, and it killed itself. Only HandQ's
# own process is protected; killing any other process is left untouched.

_KILL_VERB = re.compile(
    r'\btaskkill\b|\bkill\b|\bpkill\b|\bkillall\b|\bskill\b'
    r'|Stop-Process|Remove-Process',
    re.IGNORECASE,
)
# A standalone run of digits (not glued to a word char and not a signal like
# "-9"). Over-capture is safe: a captured number only blocks when it equals the
# bridge's own PID, a specific, large runtime value.
_PID_TOKEN = re.compile(r'(?<![\w-])(\d+)(?![\w])')

# In a dev run the bridge is `python bridge_main.py`, so the executable basename
# is a generic interpreter; name-based blocking would then be far too broad (the
# agent may legitimately kill a python/node subprocess it spawned). Name-based
# self-kill blocking therefore applies only to a frozen host exe.
_INTERPRETER_STEMS = frozenset({
    "python", "pythonw", "python3", "node", "deno", "bun",
    "pwsh", "powershell", "cmd", "sh", "bash", "zsh", "dash",
})


def _check_self_kill(command: str) -> str | None:
    if not _KILL_VERB.search(command):
        return None
    own = os.getpid()
    for match in _PID_TOKEN.finditer(command):
        if int(match.group(1)) == own:
            return (
                f"BLOCKED: this command targets PID {own}, the HandQ bridge's own "
                f"process. Killing it would terminate the agent itself. "
                f"Suggestion: never kill HandQ's own PID — if a new build must "
                f"take effect, ask the user to restart HandQ rather than "
                f"hot-swapping the running process."
            )
    # Name-based image kill, frozen production host only.
    exe_stem = Path(sys.executable).stem.lower()
    if len(exe_stem) >= 4 and exe_stem not in _INTERPRETER_STEMS:
        if re.search(rf'\b{re.escape(exe_stem)}\b', command, re.IGNORECASE):
            return (
                f"BLOCKED: this command targets the image '{exe_stem}', the "
                f"running HandQ bridge. Killing it would terminate the agent "
                f"itself. Suggestion: ask the user to restart HandQ instead of "
                f"killing the bridge process."
            )
    return None


def _check_dangerous_command(command: str) -> str | None:
    stripped = command.strip()
    self_kill = _check_self_kill(stripped)
    if self_kill:
        return self_kill
    for pattern, reason, suggestion in _DANGEROUS_PATTERNS:
        if pattern.search(stripped):
            return f"BLOCKED: {reason}. Suggestion: {suggestion}"
    if _IS_WINDOWS:
        for pattern, reason, suggestion in _DANGEROUS_PATTERNS_WIN:
            if pattern.search(stripped):
                return f"BLOCKED: {reason}. Suggestion: {suggestion}"
    return None


# ── Wide-search path detection ────────────────────────────────────────────────
_WIDE_SEARCH_CMDS = re.compile(r'\b(grep|find|rg|ripgrep)\b')
_ABS_PATH_ARG = re.compile(r'(?:^|\s)(\/[^\s]*)')
_ABS_PATH_ARG_WIN = re.compile(r'(?:^|\s)([A-Za-z]:[\\\/][^\s]*)')


def _check_wide_search(command: str, working_dir: str) -> str | None:
    if not _WIDE_SEARCH_CMDS.search(command):
        return None
    working_dir_real = os.path.realpath(working_dir)
    candidates_raw: list[str] = []
    for match in _ABS_PATH_ARG.finditer(command):
        candidates_raw.append(match.group(1))
    if _IS_WINDOWS:
        for match in _ABS_PATH_ARG_WIN.finditer(command):
            candidates_raw.append(match.group(1))
    for candidate in candidates_raw:
        candidate = candidate.rstrip('"\';)')
        if candidate in ('/dev/null', '/dev/stdin', '/dev/stdout', '/dev/stderr'):
            continue
        try:
            candidate_real = os.path.realpath(candidate)
        except (ValueError, OSError):
            continue
        if not (candidate_real == working_dir_real or
                candidate_real.startswith(working_dir_real + os.sep)):
            return (
                f"\n⚠ SCOPE WARNING: search path '{candidate}' is outside the "
                f"working directory '{working_dir_real}'. "
                f"Scope searches to the working directory (use '.' or a subdirectory) "
                f"to avoid scanning unrelated files and hanging on large directory trees."
            )
    return None


# ── Output truncation ─────────────────────────────────────────────────────────

def _truncate_stream(text: str, head: int, tail: int, stream: str) -> tuple[str, bool]:
    if len(text) <= head + tail:
        return text, False
    omitted = len(text) - head - tail
    notice = (
        f"\n\n... [{stream} truncated: {omitted:,} characters omitted "
        f"({len(text):,} total)] ...\n\n"
    )
    return text[:head] + notice + text[-tail:], True


def _apply_truncation(stdout: str, stderr: str) -> tuple[str, str, bool]:
    combined_len = len(stdout) + len(stderr)
    if combined_len <= _TRUNCATION_TOTAL_CHARS:
        return stdout, stderr, False
    if combined_len > 0:
        stdout_head = int(_TRUNCATION_HEAD_CHARS * len(stdout) / combined_len)
        stdout_tail = int(_TRUNCATION_TAIL_CHARS * len(stdout) / combined_len)
        stderr_head = _TRUNCATION_HEAD_CHARS - stdout_head
        stderr_tail = _TRUNCATION_TAIL_CHARS - stdout_tail
    else:
        stdout_head = stdout_tail = stderr_head = stderr_tail = 0
    stdout_out, stdout_trunc = _truncate_stream(stdout, max(stdout_head, 1), max(stdout_tail, 1), "stdout")
    stderr_out, stderr_trunc = _truncate_stream(stderr, max(stderr_head, 1), max(stderr_tail, 1), "stderr")
    truncated = stdout_trunc or stderr_trunc
    return stdout_out, stderr_out, truncated


# ── Exit-code semantics ───────────────────────────────────────────────────────
#
# Mirrors claude-code's commandSemantics.ts: many commands use a non-zero exit
# code to convey information, NOT failure. Treating "exit != 0" as a blanket
# failure (and then burying the stdout in an err string) is exactly what misled
# the 2026-07-24 QPM agent — a Get-ChildItem that scanned several dirs, listed
# qpm-cli.exe successfully, but returned 1 because one subdir was inaccessible,
# was presented as {"ok": false, ...} with the CLI path folded into `err`.
#
# `_interpret_exit_code` returns (is_error, note): is_error drives the tool's
# success flag; note is an informational string appended to stdout (never
# replacing it) so the agent understands the non-zero code without treating it
# as failure.

# Base command → semantic. Each takes (code, stdout, stderr) and returns
# (is_error, note). Matched against the FIRST token of the (last) command
# segment, lowercased, with .exe/.ps1 stripped — same normalization as
# _segment_is_read_only.
def _sem_listing(code: int, stdout: str, _stderr: str) -> tuple[bool, Optional[str]]:
    # Get-ChildItem / ls / dir: code 1 with output = some paths were
    # inaccessible (permissions), not a failure. code 1 with NO output, or
    # code >= 2, is a real error (bad path, bad args).
    if code >= 2:
        return True, None
    if code == 1:
        if stdout.strip():
            return False, "some paths were inaccessible (partial listing)"
        return True, None
    return False, None


def _sem_search(code: int, _stdout: str, _stderr: str) -> tuple[bool, Optional[str]]:
    # Select-String / findstr / grep / rg: 0 = matches, 1 = no matches (not an
    # error), 2+ = real error.
    if code >= 2:
        return True, None
    if code == 1:
        return False, "no matches found"
    return False, None


def _sem_diff(code: int, _stdout: str, _stderr: str) -> tuple[bool, Optional[str]]:
    # fc / diff: 0 = identical, 1 = differences found (not an error), 2+ = error.
    if code >= 2:
        return True, None
    if code == 1:
        return False, "files differ"
    return False, None


_COMMAND_SEMANTICS: Dict[str, Any] = {
    "get-childitem": _sem_listing, "gci": _sem_listing, "ls": _sem_listing,
    "dir": _sem_listing, "gci.exe": _sem_listing,
    "select-string": _sem_search, "sls": _sem_search, "findstr": _sem_search,
    "grep": _sem_search, "rg": _sem_search, "egrep": _sem_search, "fgrep": _sem_search,
    "fc": _sem_diff, "diff": _sem_diff,
}


# ── Self-reported outcome scan (goal-level signal) ──────────────────────────
#
# `exit_code` answers "did the interpreter run?" — never "did the script achieve
# its goal?". That gap is the single biggest observability hole in HandQ, and it
# opens the moment an agent moves real work into `python script.py`.
#
# 2026-08-03 flash-meta run: the agent moved 100% of its GUI automation into
# `python cdp_*.py` (33 one-shot scripts). The whole desktop_* effect layer —
# content_changed / effect / effect_hint — became unreachable, and the only
# remaining success signal was each script's own unconditional print(). Turn
# 106 shipped this as ok=true / exit_code=0:
#
#     [A] Clicking XMLS button...   CLICKED_XMLS[0]: secondary p-button
#     [B] Inspecting XMLS dialog... NO_DIALOG_NO_PANEL. Open dialogs: 0
#     [C] Selecting XML files...    NO_DIALOG
#     [D] Clicking OK...            NO_DIALOG
#     [E] Check RAW PROGRAM state... RAW PROGRAM SECTION NOT FOUND
#
# Turn 190 was worse: `Connect: CONNECT_NOT_FOUND` sat one line above a
# "connection confirmed" reading of a 90-minute-stale CSS class, both under
# ok=true; the agent read only the second.
#
# 2026-08-04 flash run added the CDP-null-effect family below. Driving an
# Electron BROWSE button over CDP, every instrument reported the click had no
# effect — `No CDP fileChooserOpened event received`, `Device Programmer: None`,
# `invoke calls: []`, `remote.dialog intercepted: false` — because the file
# dialog is a native Win32 window CDP cannot see. All of that shipped as
# exit_code=0/ok=true, so the agent re-clicked and stacked a second dialog.
# Surfacing these "ran-but-null-effect" markers turns that silent loop into a
# visible reported_failures list, and the effect_hint below now points at
# desktop_fill_file_dialog for the specific fileChooser case.
#
# Deliberately CASE-SENSITIVE and uppercase-only for the UPPER_SNAKE tokens.
# Scripts print those as deliberate status tokens, so requiring caps keeps
# ordinary prose ("no errors found", "0 failed") from tripping the scan. The
# CDP-null-effect phrases are matched case-insensitively but are specific enough
# not to false-positive. This does NOT flip `success` — process semantics stay
# honest and a script legitimately reporting one NOT_FOUND among ten checks is
# not a failed command. It makes the negative lines impossible to skim past,
# which is the actual failure mode.
_SELF_REPORTED_FAILURE_RE = re.compile(
    r"(?m)^.*?(?:"
    r"\b(?:NOT_FOUND|NOT FOUND|NO_DIALOG(?:_NO_PANEL)?|NO_PANEL|NO_MATCH|"
    r"CONNECT_NOT_FOUND|FAILURE|FATAL|UNREACHABLE|REFUSED|TIMEOUT)\b"
    r"|Traceback \(most recent call last\)"
    r"|(?i:no\b.{0,20}fileChooserOpened)"
    r"|(?i:remote\.dialog (?:not found|intercepted: false))"
    r"|(?i:Device Programmer: None)"
    r").*$"
)
_MAX_REPORTED_LINES = 8
_MAX_REPORTED_LINE_CHARS = 240

# The fileChooser marker specifically means "a native OS dialog probably opened
# where CDP can't see it" — route the agent to the tool that CAN.
_FILECHOOSER_MARKER_RE = re.compile(r"(?i)fileChooserOpened|open dialog|openDialog")


def _scan_self_reported_failures(stdout: str) -> List[str]:
    """Return stdout lines that look like the script reporting its OWN failure."""
    if not stdout:
        return []
    hits: List[str] = []
    for m in _SELF_REPORTED_FAILURE_RE.finditer(stdout):
        line = m.group(0).strip()
        if not line:
            continue
        if len(line) > _MAX_REPORTED_LINE_CHARS:
            line = line[:_MAX_REPORTED_LINE_CHARS] + "…"
        hits.append(line)
        if len(hits) >= _MAX_REPORTED_LINES:
            break
    return hits


def _apply_outcome_patterns(
    stdout: str,
    stderr: str,
    success_pattern: Optional[str],
    failure_pattern: Optional[str],
) -> tuple[Optional[bool], Optional[str]]:
    """Evaluate agent-declared success/failure oracles against the output.

    Returns ``(verdict, note)`` where ``verdict`` is None when the agent
    declared no oracle (nothing to say), True/False when one matched. Unlike
    the advisory scan above, an explicit oracle IS authoritative and drives the
    tool's ``success`` flag — the agent asked for exactly this contract.

    ``failure_pattern`` is checked first: "did the thing I feared happen?" is a
    stronger statement than "did a string I hoped for appear?".
    """
    blob = f"{stdout}\n{stderr}"
    if failure_pattern:
        try:
            if re.search(failure_pattern, blob):
                return False, (
                    f"failure_pattern {failure_pattern!r} matched the output — "
                    f"the declared failure condition occurred."
                )
        except re.error as exc:
            return None, f"failure_pattern is not a valid regex ({exc}); ignored."
    if success_pattern:
        try:
            if re.search(success_pattern, blob):
                return True, (
                    f"success_pattern {success_pattern!r} matched the output."
                )
            return False, (
                f"success_pattern {success_pattern!r} did NOT match the output — "
                f"the command ran but did not report the outcome you declared as "
                f"success. Do not treat this as done."
            )
        except re.error as exc:
            return None, f"success_pattern is not a valid regex ({exc}); ignored."
    return None, None


def _base_command_token(command: str) -> str:
    """First token of the LAST pipeline/chain segment, normalized.

    The last command in a pipe/chain determines the process exit code, so its
    semantic is the one that applies. Best-effort — not for security decisions.
    """
    cmd = (command or "").strip()
    if not cmd:
        return ""
    # Split on the coarse chain/pipe operators; take the last non-empty segment.
    parts = re.split(r"\|\||&&|;|\||>>|>|<", cmd)
    seg = next((p.strip() for p in reversed(parts) if p.strip()), cmd)
    head = (seg.split() or [""])[0].lower().lstrip("./")
    for suffix in (".exe", ".ps1", ".cmd", ".bat"):
        if head.endswith(suffix):
            head = head[: -len(suffix)]
            break
    return head


def _interpret_exit_code(
    command: str, code: int, stdout: str, stderr: str
) -> tuple[bool, Optional[str]]:
    """Interpret a shell exit code by command semantics.

    Returns (is_error, note). Default (unknown command): is_error = code != 0.
    Known informational-exit commands (listing/search/diff) map code 1 to
    success-with-note. See module comment for rationale.
    """
    sem = _COMMAND_SEMANTICS.get(_base_command_token(command))
    if sem is None:
        return code != 0, None
    return sem(code, stdout, stderr)


# ── Shell resolution ──────────────────────────────────────────────────────────

def _resolve_shell(shell: Optional[str]) -> list[str]:
    """
    Resolve a shell name to an executable path + invocation prefix.

    Platform defaults:
      - Windows: PowerShell (pwsh or powershell.exe)
      - Linux/macOS: /bin/sh
    """
    if shell is None:
        if _IS_WINDOWS:
            exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
            return [exe, "-NoProfile", "-NonInteractive", "-Command"]
        else:
            return ["/bin/sh", "-c"]

    name = shell.strip().lower()

    if name in ("cmd", "cmd.exe"):
        return ["cmd.exe", "/c"]

    if name in ("powershell", "pwsh"):
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-Command"]

    posix_shells = {
        "bash": ["bash", "bash.exe"],
        "sh":   ["sh",   "sh.exe"],
        "zsh":  ["zsh",  "zsh.exe"],
        "fish": ["fish", "fish.exe"],
    }
    candidates = posix_shells.get(name, [name, name + ".exe"])
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return [found, "-c"]

    return [shell, "-c"]


def _shell_label(executable: str) -> str:
    """Return a stable, agent-friendly name for a resolved shell executable.

    ``_resolve_shell`` returns the full path to the interpreter, which is
    accurate but not what the agent needs when it's asking itself "was that
    PowerShell or bash?" — the path contains version numbers, install
    prefixes, and drive letters. Normalize to one of the shell family names
    the agent's tool description already talks about, so its next
    self-correction (e.g. picking Here-String vs single-quoted string
    for a multiline python -c) has an unambiguous signal to key off.
    """
    name = os.path.basename(executable).lower()
    if name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        return "powershell"
    if name in ("cmd", "cmd.exe"):
        return "cmd"
    for base in ("bash", "zsh", "sh", "fish", "dash"):
        if name.startswith(base):
            return base
    return name or "unknown"


def _get_output_encoding() -> str:
    # PowerShell 7 (pwsh) encodes piped/redirected stdout as UTF-8 regardless
    # of the console codepage, and _build_venv_env now forces every spawned
    # Python child to do the same (PYTHONIOENCODING=utf-8). Decoding with the
    # locale codepage here (the old behavior) mismatched that and produced
    # mojibake/UnicodeDecodeError for any non-ASCII byte actually emitted as
    # UTF-8 — always decode as UTF-8 to match what's actually on the wire.
    return "utf-8"


# ── Background task management ────────────────────────────────────────────────

@dataclass
class BackgroundTask:
    """State for a single background shell task."""
    task_id: str
    command: str
    description: str
    process: asyncio.subprocess.Process
    start_time: float
    status: str = "running"  # "running" | "done" | "killed"
    exit_code: Optional[int] = None
    stdout_data: str = ""
    stderr_data: str = ""
    _reader_task: Optional[asyncio.Task] = field(default=None, repr=False)


class BackgroundTaskRegistry:
    """Lightweight in-process job registry for background shell tasks."""

    def __init__(self):
        self._tasks: Dict[str, BackgroundTask] = {}
        self._counter: int = 0

    def create_task_id(self) -> str:
        self._counter += 1
        return f"bg_{self._counter}"

    def register(self, task: BackgroundTask) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> List[Dict[str, Any]]:
        result = []
        for t in self._tasks.values():
            elapsed = time.time() - t.start_time
            result.append({
                "task_id": t.task_id,
                "command": t.command[:100],
                "description": t.description,
                "status": t.status,
                "exit_code": t.exit_code,
                "elapsed_seconds": round(elapsed, 1),
            })
        return result

    def get_completed_and_clear(self) -> List[BackgroundTask]:
        """Return all completed/killed tasks and remove them from registry."""
        completed = [t for t in self._tasks.values() if t.status != "running"]
        for t in completed:
            del self._tasks[t.task_id]
        return completed

    async def kill(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status != "running":
            return True
        await _kill_process_tree(task.process)
        task.status = "killed"
        task.exit_code = -1
        if task._reader_task and not task._reader_task.done():
            task._reader_task.cancel()
            try:
                await task._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        return True

    async def kill_all(self) -> None:
        for task_id in list(self._tasks.keys()):
            await self.kill(task_id)


# ── ShellTool ─────────────────────────────────────────────────────────────────

# Directories a workspace-mtime scan skips outright — heavy VCS, build
# artefact, and cache trees where the agent's shell won't touch anything
# the user cares to see in the nebula, and which would blow the walk cost
# out of proportion on typical projects.
_WORKSPACE_SCAN_EXCLUDE_DIRS = frozenset([
    ".git", "node_modules", "__pycache__",
    ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
    ".idea", ".vscode", ".DS_Store",
])
# Guard rail: a walk that would iterate more files than this bails empty
# rather than emit a partial picture — quietly missing a file the shell
# actually touched is a worse signal than "we didn't scan this run".
_WORKSPACE_SCAN_MAX_FILES = 20_000


def _scan_workspace_changes(root: str, since_ts: float) -> List[str]:
    """Return absolute paths under *root* whose mtime is at or after
    *since_ts* — the shell tool's crude "which files did that command
    touch?" detector. 1-second slop is applied so a filesystem whose
    mtime resolution rounds down still reports fresh writes.

    Skips ``_WORKSPACE_SCAN_EXCLUDE_DIRS`` at every level. Returns an
    empty list on any walk error or if the tree exceeds
    ``_WORKSPACE_SCAN_MAX_FILES`` — see the const's docstring.
    """
    changed: List[str] = []
    total = 0
    threshold = since_ts - 1.0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _WORKSPACE_SCAN_EXCLUDE_DIRS]
            for fname in filenames:
                total += 1
                if total > _WORKSPACE_SCAN_MAX_FILES:
                    return []
                fp = os.path.join(dirpath, fname)
                try:
                    mt = os.stat(fp).st_mtime
                except OSError:
                    continue
                if mt >= threshold:
                    changed.append(fp)
    except OSError:
        pass
    return changed


def _augment_ssh_command(command: str) -> str:
    """Auto-add safety flags to bare ``ssh`` commands.

    OpenSSH without ``BatchMode=yes`` will silently wait on stdin for a password
    when key auth is unavailable; the wrapping shell tool only sees a hung
    process and ends up paying the full timeout for nothing actionable.
    Inject ``-o BatchMode=yes -o ConnectTimeout=15`` after the literal ``ssh``
    token so failures surface as ``Permission denied`` immediately, giving the
    LLM a real signal to switch credential strategies.

    Only triggers when:
      - the command's first token (post leading whitespace) is exactly ``ssh``
        (rejects ``sshfs``, ``ssh-add``, ``ssh-keygen``, etc.)
      - neither ``BatchMode=`` nor ``ConnectTimeout=`` already appear in the
        command — explicit user intent always wins
    """
    stripped = command.lstrip()
    if not stripped.startswith("ssh "):
        return command
    if "BatchMode=" in stripped or "ConnectTimeout=" in stripped:
        return command
    indent_len = len(command) - len(stripped)
    head = command[:indent_len]
    return f"{head}ssh -o BatchMode=yes -o ConnectTimeout=15 {stripped[4:]}"


class ShellTool(BaseTool):
    """Execute shell commands (cross-platform) with background support.

    Default shell: PowerShell on Windows, /bin/sh on Linux/macOS.
    Supports background execution, CWD persistence, and extended timeouts.
    """

    is_read_only = False
    is_concurrency_safe = False

    def __init__(
        self,
        ctx=None,
        venv_path: Optional[str] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ):
        super().__init__("shell", ctx=ctx)
        self.venv_path = venv_path
        # Prefer the explicitly-supplied event (test fixtures); otherwise pull
        # from the SessionContext so PersistentAgent doesn't have to do
        # post-construction injection.
        if interrupt_event is not None:
            self.interrupt_event: Optional[asyncio.Event] = interrupt_event
        elif ctx is not None:
            self.interrupt_event = ctx.interrupt_event
        else:
            self.interrupt_event = None
        self._registry = BackgroundTaskRegistry()

    def _build_venv_env(self) -> dict:
        env = dict(os.environ)
        if _IS_WINDOWS:
            # Python defaults stdout/stderr to the console codepage (cp1252 on
            # most en-US/zh-CN Windows installs) whenever they're piped rather
            # than a real console — which is exactly what happens here via
            # asyncio.subprocess.PIPE. Any non-ASCII character a generated
            # script prints (✓, →, em-dash, …) then raises UnicodeEncodeError
            # instead of running. Forcing UTF-8 here fixes it at the source
            # for every command this tool runs, instead of relying on each
            # generated script to remember PYTHONIOENCODING/encoding="utf-8".
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
        if not self.venv_path:
            return env
        bin_dir = os.path.join(
            self.venv_path,
            "Scripts" if _IS_WINDOWS else "bin"
        )
        existing_path = env.get("PATH", "")
        env["PATH"] = bin_dir + os.pathsep + existing_path if existing_path else bin_dir
        env["VIRTUAL_ENV"] = self.venv_path
        env.pop("PYTHONHOME", None)
        return env

    def get_completed_tasks(self) -> List[BackgroundTask]:
        """Called by runtime_agent to inject completion notifications."""
        return self._registry.get_completed_and_clear()

    async def execute(
        self,
        command: Optional[str] = None,
        run_in_background: bool = False,
        task_id: Optional[str] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        shell: Optional[str] = None,
        description: Optional[str] = None,
        success_pattern: Optional[str] = None,
        failure_pattern: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Execute a shell command or manage a background task.

        Dispatch logic:
        - task_id provided (no command): query/kill a background task
        - command + run_in_background=True: launch in background
        - command (default): execute in foreground with timeout
        """
        # Route: task management
        if task_id is not None:
            # bool() around the `and` — command may be None or empty, and
            # `and` short-circuits to those falsy values, not to False. Passing
            # "" or None where _task_action expects a bool typechecks wrong and
            # in principle could route through a truthy branch on an
            # unexpected value.
            kill_requested = bool(command and command.strip().lower() == "kill")
            return await self._task_action(task_id, kill=kill_requested)

        # Route: requires a command
        if not command:
            return ToolResult(
                success=False,
                output=None,
                error="Either 'command' or 'task_id' is required.",
                tool_name=self.name,
                tool_parameters={"command": command, "task_id": task_id},
            )

        command = _augment_ssh_command(command)

        if run_in_background:
            return await self._execute_background(
                command=command,
                shell=shell,
                cwd=cwd,
                description=description or command[:80],
            )
        else:
            return await self._execute_foreground(
                command=command,
                timeout=timeout,
                shell=shell,
                cwd=cwd,
                success_pattern=success_pattern,
                failure_pattern=failure_pattern,
                **kwargs,
            )

    async def _task_action(self, task_id: str, kill: bool = False) -> ToolResult:
        """Query status or kill a background task."""
        start_time = time.time()

        if kill:
            success = await self._registry.kill(task_id)
            if not success:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Background task '{task_id}' not found.",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={"task_id": task_id, "command": "kill"},
                )
            task = self._registry.get(task_id)
            return ToolResult(
                success=True,
                output={
                    "task_id": task_id,
                    "status": "killed",
                    "stdout": task.stdout_data if task else "",
                    "stderr": task.stderr_data if task else "",
                },
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"task_id": task_id, "command": "kill"},
            )

        task = self._registry.get(task_id)
        if task is None:
            return ToolResult(
                success=False,
                output=None,
                error=f"Background task '{task_id}' not found.",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"task_id": task_id},
            )

        elapsed = time.time() - task.start_time
        output: Dict[str, Any] = {
            "task_id": task_id,
            "command": task.command,
            "description": task.description,
            "status": task.status,
            "exit_code": task.exit_code,
            "elapsed_seconds": round(elapsed, 1),
        }
        if task.status != "running":
            output["stdout"] = task.stdout_data
            output["stderr"] = task.stderr_data

        return ToolResult(
            success=True,
            output=output,
            execution_time=time.time() - start_time,
            tool_name=self.name,
            tool_parameters={"task_id": task_id},
        )

    async def _execute_background(
        self,
        command: str,
        shell: Optional[str],
        cwd: Optional[str],
        description: str,
    ) -> ToolResult:
        """Launch a command in background, return task_id immediately."""
        start_time = time.time()

        _danger = _check_dangerous_command(command)
        if _danger:
            return ToolResult(
                success=False,
                output={"stdout": "", "stderr": _danger},
                error=_danger,
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"command": command, "run_in_background": True},
            )

        _check_injection(command)

        # No explicit cwd → run in the per-session workspace, not the process
        # cwd (no longer mutated via os.chdir — see concurrency work). A
        # relative cwd is resolved against the workspace too, so neither the
        # isdir check nor the subprocess base depends on the process cwd.
        resolved_cwd = self.resolve_in_workspace(cwd) if cwd else None
        effective_cwd = resolved_cwd if (resolved_cwd and os.path.isdir(resolved_cwd)) else (
            self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else None
        )

        shell_argv = _resolve_shell(shell)
        full_argv = [shell_argv[0], *shell_argv[1:], command]
        proc_env = self._build_venv_env()

        kwargs_proc: dict = {}
        if _IS_WINDOWS:
            import subprocess as _sp
            kwargs_proc["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs_proc["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                *full_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
                env=proc_env,
                **kwargs_proc,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Failed to start background command: {e}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={"command": command, "run_in_background": True},
            )

        task_id = self._registry.create_task_id()
        bg_task = BackgroundTask(
            task_id=task_id,
            command=command,
            description=description,
            process=process,
            start_time=time.time(),
        )

        async def _reader():
            """Read subprocess output into task buffers."""
            encoding = _get_output_encoding()
            try:
                stdout_bytes, stderr_bytes = await process.communicate()
                bg_task.stdout_data = stdout_bytes.decode(encoding, errors="replace")
                bg_task.stderr_data = stderr_bytes.decode(encoding, errors="replace")
                # Truncate if needed
                if len(bg_task.stdout_data) > _BG_BUFFER_MAX_BYTES:
                    bg_task.stdout_data = bg_task.stdout_data[:_BG_BUFFER_MAX_BYTES] + "\n...[truncated]"
                if len(bg_task.stderr_data) > _BG_BUFFER_MAX_BYTES:
                    bg_task.stderr_data = bg_task.stderr_data[:_BG_BUFFER_MAX_BYTES] + "\n...[truncated]"
                bg_task.exit_code = process.returncode
                bg_task.status = "done"
            except asyncio.CancelledError:
                bg_task.status = "killed"
            except Exception as e:
                bg_task.stderr_data = f"Reader error: {e}"
                bg_task.status = "done"
                bg_task.exit_code = -1

        reader_task = asyncio.create_task(_reader())
        bg_task._reader_task = reader_task
        self._registry.register(bg_task)

        return ToolResult(
            success=True,
            output={
                "task_id": task_id,
                "status": "running",
                "command": command,
                "description": description,
                "shell_used": _shell_label(shell_argv[0]),
                "message": f"Command launched in background. Use task_id='{task_id}' to check status or kill.",
            },
            execution_time=time.time() - start_time,
            tool_name=self.name,
            tool_parameters={"command": command, "run_in_background": True},
        )

    async def _execute_foreground(
        self,
        command: str,
        timeout: Optional[int] = None,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        success_pattern: Optional[str] = None,
        failure_pattern: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """Execute a command in foreground with timeout."""
        start_time = time.time()

        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT_SECONDS
        if effective_timeout > _MAX_TIMEOUT_SECONDS:
            effective_timeout = _MAX_TIMEOUT_SECONDS

        try:
            self.validate_params(["command"], {"command": command})

            # Working directory: explicit cwd, else the per-session workspace
            # (not the process cwd — no longer mutated via os.chdir). A relative
            # cwd is resolved against the workspace, so neither the isdir check
            # nor the subprocess base depends on the process cwd.
            effective_cwd = (self.resolve_in_workspace(cwd) if cwd else None) or (
                self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else None
            )
            if effective_cwd is not None and not os.path.isdir(effective_cwd):
                return ToolResult(
                    success=False,
                    output={
                        "exit_code": None,
                        "stdout": "",
                        "stderr": f"Working directory does not exist: {effective_cwd}",
                        "truncated": False,
                        "cwd_used": effective_cwd,
                        "command": command,
                        "shell": None,
                        "venv": self.venv_path,
                    },
                    error=f"Working directory does not exist: {effective_cwd}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

            _danger = _check_dangerous_command(command)
            if _danger:
                return ToolResult(
                    success=False,
                    output={
                        "exit_code": None,
                        "stdout": "",
                        "stderr": _danger,
                        "truncated": False,
                        "cwd_used": effective_cwd,
                        "command": command,
                        "shell": None,
                        "venv": self.venv_path,
                    },
                    error=_danger,
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

            _check_injection(command)

            # In production every ShellTool sees ``effective_cwd`` set via the
            # injected SessionContext (``ctx.working_directory``). The fallback
            # below only fires for ctx-less callers (test fixtures invoking
            # ShellTool directly without a SessionContext). We pick the user
            # home rather than ``os.getcwd()`` because the process cwd is
            # global state shared across every running session — picking it
            # up here would silently leak whatever directory the bridge was
            # launched from (or any other module's last ``os.chdir``) into
            # what should be a session-scoped wide-search heuristic.
            _scope_cwd = effective_cwd if effective_cwd is not None else str(Path.home())
            _scope_warning = _check_wide_search(command, _scope_cwd)

            shell_argv = _resolve_shell(shell)
            executable = shell_argv[0]

            full_argv = [executable, *shell_argv[1:], command]
            proc_env = self._build_venv_env()

            kwargs_proc: dict = {}
            if _IS_WINDOWS:
                import subprocess as _sp
                kwargs_proc["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs_proc["start_new_session"] = True

            process = await asyncio.create_subprocess_exec(
                *full_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
                env=proc_env,
                **kwargs_proc,
            )

            try:
                if self.interrupt_event is not None:
                    communicate_task = asyncio.create_task(process.communicate())
                    interrupt_task = asyncio.create_task(self.interrupt_event.wait())
                    done, pending = await asyncio.wait(
                        [communicate_task, interrupt_task],
                        timeout=effective_timeout if effective_timeout > 0 else None,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

                    if interrupt_task in done:
                        await _kill_process_tree(process)
                        return ToolResult(
                            success=False,
                            output={'stdout': '', 'stderr': 'interrupted by user', 'exit_code': -1, 'truncated': False},
                            error=f"Interrupted by coordinator: {command}",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={'command': command, 'timeout': timeout},
                        )

                    # Neither completed within the timeout window: the command
                    # is genuinely stuck. Without this guard the interrupt-aware
                    # branch waits forever (the non-interrupt branch already has
                    # wait_for). Kill the process tree and report a timeout.
                    if communicate_task not in done:
                        await _kill_process_tree_graceful(process)
                        return ToolResult(
                            success=False,
                            output={
                                "exit_code": None,
                                "stdout": "",
                                "stderr": f"Command timed out after {effective_timeout}s",
                                "truncated": False,
                                "cwd_used": effective_cwd,
                                "command": command,
                                "shell": executable,
                                "shell_used": _shell_label(executable),
                                "venv": self.venv_path,
                            },
                            error=f"Command execution timeout ({effective_timeout}s): {command}",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={'command': command, 'timeout': timeout},
                        )

                    stdout_bytes, stderr_bytes = communicate_task.result()

                else:
                    try:
                        stdout_bytes, stderr_bytes = await asyncio.wait_for(
                            process.communicate(),
                            timeout=effective_timeout if effective_timeout > 0 else None,
                        )
                    except asyncio.TimeoutError:
                        await _kill_process_tree_graceful(process)
                        return ToolResult(
                            success=False,
                            output={
                                "exit_code": None,
                                "stdout": "",
                                "stderr": f"Command timed out after {effective_timeout}s",
                                "truncated": False,
                                "cwd_used": effective_cwd,
                                "command": command,
                                "shell": executable,
                                "shell_used": _shell_label(executable),
                                "venv": self.venv_path,
                            },
                            error=f"Command execution timeout ({effective_timeout}s): {command}",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={'command': command, 'timeout': timeout},
                        )

                encoding = _get_output_encoding()
                stdout_text = stdout_bytes.decode(encoding, errors="replace")
                stderr_text = stderr_bytes.decode(encoding, errors="replace")

                # Truncation
                stdout_text, stderr_text, truncated = _apply_truncation(stdout_text, stderr_text)

                # Exit-code semantics: a non-zero code isn't always failure
                # (Get-ChildItem partial listing, Select-String no-match, diff
                # differs). is_error drives success; note is appended to stdout
                # so the agent understands the code without treating it as a
                # failure. See _interpret_exit_code.
                is_error, exit_note = _interpret_exit_code(
                    command, process.returncode, stdout_text, stderr_text
                )
                if exit_note:
                    stdout_text = (
                        stdout_text + f"\n[note: exit code {process.returncode} — {exit_note}]"
                    )

                # Scope warning
                if _scope_warning:
                    stdout_text = stdout_text + _scope_warning

                observation = {
                    "exit_code": process.returncode,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "truncated": truncated,
                    "cwd_used": effective_cwd,
                    "command": command,
                    "shell": executable,
                    "shell_used": _shell_label(executable),
                    "returncode": process.returncode,
                    "cwd": effective_cwd,
                    "venv": self.venv_path,
                }

                # ── Goal-level outcome signal ───────────────────────────────
                # Layer 1 (opt-in, AUTHORITATIVE): the agent declared its own
                # oracle, so honour it and let it drive `success`.
                _verdict, _verdict_note = _apply_outcome_patterns(
                    stdout_text, stderr_text, success_pattern, failure_pattern,
                )
                if _verdict is not None:
                    is_error = not _verdict
                    observation["outcome_check"] = _verdict_note
                elif _verdict_note:
                    # Malformed regex — say so instead of silently ignoring it.
                    observation["outcome_check"] = _verdict_note

                # Layer 2 (always on, ADVISORY but loud): lines where the
                # script reports its own failure. Never flips `success` — see
                # _SELF_REPORTED_FAILURE_RE for why — but is placed where it
                # cannot be skimmed past.
                _reported = _scan_self_reported_failures(stdout_text)
                if _reported:
                    observation["reported_failures"] = _reported
                    _hint = (
                        f"exit_code={process.returncode} only means the "
                        f"interpreter ran; it does NOT mean the script achieved "
                        f"its goal. This command's OWN output reports "
                        f"{len(_reported)} failed/not-found step(s), listed in "
                        f"`reported_failures`. Treat those lines as the outcome, "
                        f"not the exit code and not any success string the "
                        f"script printed alongside them. If you needed a "
                        f"machine-checkable verdict, re-run with "
                        f"`success_pattern` / `failure_pattern` so the tool "
                        f"decides instead of you."
                    )
                    # A fileChooser/openDialog null-effect almost always means a
                    # NATIVE OS file dialog opened where CDP cannot see it. Point
                    # at the tool that can, instead of letting the agent re-click
                    # the DOM button and stack another invisible dialog.
                    if _FILECHOOSER_MARKER_RE.search("\n".join(_reported)):
                        _hint += (
                            " NOTE: a 'fileChooser/openDialog' null result means "
                            "a BROWSE/Open button most likely opened a NATIVE "
                            "Windows file dialog, which is NOT in the DOM and "
                            "which CDP cannot see or fill. Do NOT re-click the "
                            "DOM button (that just stacks another hidden dialog) "
                            "and do NOT try to intercept it via CDP / "
                            "ipcRenderer / @electron/remote. Call "
                            "desktop_fill_file_dialog (no path) to confirm the "
                            "dialog is open, then again with path=<full path> to "
                            "select the file."
                        )
                    observation["effect_hint"] = _hint

                # Workspace-mtime scan: emit file_touch(edit) for every file
                # under effective_cwd whose mtime landed after this command
                # started. Only meaningful on a non-error result (a failed rm
                # didn't touch anything worth showing) and skipped for
                # demonstrably read-only commands (ls, grep) where the scan
                # would just burn CPU. Runs in the executor so a big tree
                # doesn't stall the event loop.
                if (not is_error
                        and effective_cwd
                        and not looks_read_only(command)):
                    try:
                        changed = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: _scan_workspace_changes(effective_cwd, start_time),
                        )
                        for fp in changed:
                            self.emit_file_touch(fp, "edit")
                    except Exception:
                        pass

                # When an agent-declared oracle is what failed, stderr is
                # typically EMPTY (the script ran fine, it just didn't achieve
                # the goal). Falling back to `stderr_text` alone would produce
                # success=False with error=None — the mirror image of the
                # partial-success erasure bug, and just as unreadable. Name the
                # real reason.
                _error_text: Optional[str] = None
                if is_error:
                    _error_text = stderr_text or observation.get("outcome_check") or (
                        f"command exited {process.returncode} with no stderr"
                    )

                return ToolResult(
                    success=not is_error,
                    output=observation,
                    error=_error_text,
                    execution_time=time.time() - start_time,
                    exit_code=process.returncode,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

            except asyncio.TimeoutError:
                await _kill_process_tree(process)
                return ToolResult(
                    success=False,
                    output={'stdout': '', 'stderr': 'command timed out', 'exit_code': -1, 'truncated': False},
                    error=f"Command execution timeout ({effective_timeout}s): {command}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

        except Exception as e:
            return ToolResult(
                success=False,
                output=None,
                error=f"Command execution failed: {str(e)}",
                execution_time=time.time() - start_time,
                tool_name=self.name,
                tool_parameters={'command': command, 'timeout': timeout},
            )


# ── Process termination utilities ─────────────────────────────────────────────

async def _kill_process_tree_graceful(process: asyncio.subprocess.Process) -> None:
    """SIGTERM → wait 5s → SIGKILL escalation. Windows: immediate force-kill."""
    if _IS_WINDOWS:
        await _kill_process_tree(process)
        return

    _killpg = getattr(os, "killpg", None)
    _getpgid = getattr(os, "getpgid", None)
    _SIGTERM = getattr(signal, "SIGTERM", None)
    _SIGKILL = getattr(signal, "SIGKILL", None)

    try:
        if _killpg and _getpgid and _SIGTERM:
            _killpg(_getpgid(process.pid), _SIGTERM)
        else:
            process.terminate()
    except Exception:
        pass

    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if _killpg and _getpgid and _SIGKILL:
            _killpg(_getpgid(process.pid), _SIGKILL)
        else:
            process.kill()
    except Exception:
        pass

    await _wait_after_kill(process)


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Force-terminate process and all children. Cross-platform."""
    if _IS_WINDOWS:
        try:
            os.system(f"taskkill /F /T /PID {process.pid} >nul 2>&1")
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
    else:
        try:
            import signal as _signal
            _killpg = getattr(os, "killpg", None)
            _getpgid = getattr(os, "getpgid", None)
            _SIGKILL = getattr(_signal, "SIGKILL", None)
            if _killpg and _getpgid and _SIGKILL:
                _killpg(_getpgid(process.pid), _SIGKILL)
            else:
                process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    await _wait_after_kill(process)


async def _wait_after_kill(process: asyncio.subprocess.Process) -> None:
    """Bounded wait for a process we just sent a kill signal to.

    The kill signal is unconditional and already sent by the time this runs —
    this only bounds how long we block waiting to OBSERVE the exit. A process
    wedged in uninterruptible kernel-mode I/O (Windows: an unresponsive
    UNC/SMB share; POSIX: a hard-mounted NFS path) can survive the kill for as
    long as that I/O takes to resolve — minutes, unrelated to the tool's own
    timeout. Giving up on WAITING (never on killing, which already happened)
    keeps the caller from blocking indefinitely.


    Confirmed live 2026-07-30: a hung UNC `Get-ChildItem` outlived two user
    interrupts and the session had to be destroyed — the unbounded
    ``await process.wait()`` that used to be here was the reason the agent's
    iteration loop never reached its next boundary, which in turn never
    called ``TaskChannel.acknowledge_interrupt()``, permanently starving
    every later ``interrupt_agent()`` call for the rest of the session.
    """
    try:
        await asyncio.wait_for(process.wait(), timeout=_KILL_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "ShellTool: process %s did not exit within %.0fs of being killed "
            "— giving up on the wait (kill signal was already sent); it may "
            "still be wedged in kernel-mode I/O.",
            process.pid, _KILL_WAIT_TIMEOUT_SECONDS,
        )
    except Exception:
        pass
