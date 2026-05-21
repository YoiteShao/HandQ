"""
Bash Tool - Execute shell commands (cross-platform: Linux / macOS / Windows).
"""
import asyncio
import locale
import logging
import os
import re
import shutil
import signal
import sys
import time
from typing import Optional

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# ── Platform constants ────────────────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# ── Timeout ───────────────────────────────────────────────────────────────────
_DEFAULT_TIMEOUT_SECONDS: int = 120

# ── Output truncation ─────────────────────────────────────────────────────────
# If combined stdout+stderr exceeds this, keep first 10 000 + last 5 000 chars.
_TRUNCATION_TOTAL_CHARS: int = 30_000
_TRUNCATION_HEAD_CHARS: int = 10_000
_TRUNCATION_TAIL_CHARS: int = 5_000

# ── Shell metacharacter injection detection ───────────────────────────────────
# Only flag genuinely dangerous patterns that are almost never intentional:
# - Backtick command substitution (legacy, rarely needed in modern scripts)
# - Null-byte injection (always malicious)
# $() and ; are intentionally excluded — they appear in normal shell commands.
_INJECTION_PATTERN = re.compile(
    r"""(?x)
    # Backtick command substitution
    `[^`]+`
    |
    # Null-byte injection
    \x00
    """
)


def _check_injection(command: str) -> None:
    """Log a warning if shell metacharacters are detected in unexpected positions."""
    if _INJECTION_PATTERN.search(command):
        logger.warning(
            "BashTool: possible shell metacharacters detected in command — "
            "verify this is intentional. Command (truncated): %.200s",
            command,
        )


# ── Dangerous command blocking ────────────────────────────────────────────────
# Hard-blocks commands that are almost certainly catastrophic mistakes.
# Each entry is (pattern, reason, suggestion).
# Patterns are matched against the full command string (after stripping).
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Recursive delete of filesystem root or home directory
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
    # find with destructive actions starting at root or home
    # Pattern: find <root> ... -delete  or  find <root> ... -exec rm
    # Root is / or ~ immediately after 'find' (with optional flags before path).
    # Note: \b does not work before '-' (non-word char); use (?<!\w) instead.
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
    # dd writing to raw block devices
    (
        re.compile(r'\bdd\b.*\bof=/dev/(?:sd|hd|vd|nvme|xvd|mmcblk)[a-z0-9]'),
        "dd to a raw block device would overwrite disk data",
        "use dd only with file paths, not raw device nodes",
    ),
    # mkfs — formats a filesystem
    (
        re.compile(r'\bmkfs(?:\.[a-z0-9]+)?\b'),
        "mkfs formats a filesystem and destroys all data on the target device",
        "do not format devices; use file-level operations instead",
    ),
    # Fork bomb
    (
        re.compile(r':\(\)\s*\{.*:\|:.*\}'),
        "fork bomb detected — would exhaust system process table",
        "do not run fork bombs",
    ),
    # chmod/chown on root or system auth files (with or without -R)
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
    # Overwrite /etc/passwd or /etc/shadow
    (
        re.compile(r'>\s*/etc/(?:passwd|shadow|sudoers)'),
        "overwriting /etc/passwd, /etc/shadow, or /etc/sudoers would break authentication",
        "do not overwrite system authentication files",
    ),
]

# Windows-specific dangerous patterns (only checked on Windows)
_DANGEROUS_PATTERNS_WIN: list[tuple[re.Pattern, str, str]] = [
    # Recursive delete of system drive root or user profile root
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
    # format command
    (
        re.compile(r'\bformat\b\s+[A-Za-z]:'),
        "format would destroy all data on the target drive",
        "do not format drives; use file-level operations instead",
    ),
    # Remove-Item -Recurse on drive root (PowerShell)
    (
        re.compile(r'Remove-Item\b.*?-Recurse.*?["\']?[A-Za-z]:\\["\']?\s', re.IGNORECASE),
        "recursive Remove-Item on drive root would destroy the filesystem",
        "scope to a specific subdirectory",
    ),
]


def _check_dangerous_command(command: str) -> str | None:
    """Return an error string if the command matches a dangerous pattern.

    Returns None when the command is safe to execute.
    This is a BLOCKING check — callers must not execute the command when a
    non-None value is returned.
    """
    stripped = command.strip()
    for pattern, reason, suggestion in _DANGEROUS_PATTERNS:
        if pattern.search(stripped):
            return (
                f"BLOCKED: {reason}. "
                f"Suggestion: {suggestion}"
            )
    if _IS_WINDOWS:
        for pattern, reason, suggestion in _DANGEROUS_PATTERNS_WIN:
            if pattern.search(stripped):
                return (
                    f"BLOCKED: {reason}. "
                    f"Suggestion: {suggestion}"
                )
    return None


# ── Wide-search path detection ────────────────────────────────────────────────
# Matches: grep/find/rg/ripgrep followed by flags and then an absolute path arg
# that is NOT the working directory or a subdirectory of it.
# We look for the search-target argument: the first non-flag token that is an
# absolute path (starts with /).
_WIDE_SEARCH_CMDS = re.compile(r'\b(grep|find|rg|ripgrep)\b')
_ABS_PATH_ARG = re.compile(r'(?:^|\s)(\/[^\s]*)')


def _check_wide_search(command: str, working_dir: str) -> str | None:
    """Return a warning string if the command searches outside *working_dir*.

    Detects patterns like ``grep -rn "pattern" /some/parent/dir`` where the
    search root is an ancestor of or unrelated to *working_dir*.  Returns None
    when the command looks safe.

    Only checks grep / find / rg / ripgrep — other commands are ignored.
    Non-blocking: callers append the warning to the tool output rather than
    rejecting the command, because there are legitimate cross-directory uses.
    """
    if not _WIDE_SEARCH_CMDS.search(command):
        return None

    working_dir_real = os.path.realpath(working_dir)

    for match in _ABS_PATH_ARG.finditer(command):
        candidate = match.group(1).rstrip('"\';)')
        # Skip common output-redirect targets and /dev/null
        if candidate in ('/dev/null', '/dev/stdin', '/dev/stdout', '/dev/stderr'):
            continue
        try:
            candidate_real = os.path.realpath(candidate)
        except (ValueError, OSError):
            continue
        # Flag if candidate is NOT equal to or a subdirectory of working_dir
        if not (candidate_real == working_dir_real or
                candidate_real.startswith(working_dir_real + os.sep)):
            return (
                f"\n⚠ SCOPE WARNING: search path '{candidate}' is outside the "
                f"working directory '{working_dir_real}'. "
                f"Scope searches to the working directory (use '.' or a subdirectory) "
                f"to avoid scanning unrelated files and hanging on large directory trees."
            )
    return None


def _truncate_stream(text: str, head: int, tail: int, stream: str) -> tuple[str, bool]:
    """
    Truncate a single stream using head+tail strategy.

    Returns:
        (truncated_text, was_truncated)
    """
    if len(text) <= head + tail:
        return text, False
    omitted = len(text) - head - tail
    notice = (
        f"\n\n... [{stream} truncated: {omitted:,} characters omitted "
        f"({len(text):,} total)] ...\n\n"
    )
    return text[:head] + notice + text[-tail:], True


def _apply_truncation(
    stdout: str, stderr: str
) -> tuple[str, str, bool]:
    """
    Apply truncation when combined stdout+stderr exceeds _TRUNCATION_TOTAL_CHARS.

    Keeps first _TRUNCATION_HEAD_CHARS + last _TRUNCATION_TAIL_CHARS of the
    combined output.  Each stream is truncated proportionally.

    Returns:
        (stdout_out, stderr_out, truncated_bool)
    """
    combined_len = len(stdout) + len(stderr)
    if combined_len <= _TRUNCATION_TOTAL_CHARS:
        return stdout, stderr, False

    # Truncate each stream proportionally to its share of the total.
    total_keep = _TRUNCATION_HEAD_CHARS + _TRUNCATION_TAIL_CHARS
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


def _resolve_shell(shell: Optional[str]) -> list[str]:
    """
    Resolve a shell name (or None) to an executable path + invocation prefix.

    Return examples:
      ["cmd.exe", "/c"]
      ["powershell.exe", "-NoProfile", "-Command"]
      ["/bin/bash", "-c"]
      ["/bin/sh", "-c"]

    Args:
        shell: Shell name requested by the caller, or None for the platform default.
               Accepted values: None / "cmd" / "powershell" / "bash" / "sh" / "zsh" / …

    Returns:
        list[str]: [shell_executable, *invoke_args]
                   Callers append the command string: [*result, command_string]
    """
    if shell is None:
        # Platform default
        if _IS_WINDOWS:
            return ["cmd.exe", "/c"]
        else:
            return ["/bin/sh", "-c"]

    name = shell.strip().lower()

    # ---- Windows built-in shells ----
    if name in ("cmd", "cmd.exe"):
        return ["cmd.exe", "/c"]

    if name in ("powershell", "pwsh"):
        # Prefer cross-platform pwsh (PowerShell 7+), fall back to built-in powershell
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-Command"]

    # ---- POSIX-style shells (available on Windows via Git Bash / WSL) ----
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

    # Not found — use the name as-is and let the OS report the error
    return [shell, "-c"]


def _get_output_encoding() -> str:
    """
    Return the most appropriate encoding for decoding command output.

    - Windows: prefer UTF-8 (when chcp 65001 or PYTHONUTF8=1 is active),
               fall back to the system OEM code page (e.g. cp936).
    - Other platforms: UTF-8.
    """
    if _IS_WINDOWS:
        enc = locale.getpreferredencoding(False)
        return enc if enc else "utf-8"
    return "utf-8"


class BashTool(BaseTool):
    """Execute shell commands (cross-platform: Linux / macOS / Windows)."""

    # Bash concurrency safety is determined per-call by the model via the
    # optional ``concurrent_safe`` parameter in the tool call arguments.
    # The class-level flag is False (conservative default).
    is_read_only = False
    is_concurrency_safe = False

    def __init__(
        self,
        venv_path: Optional[str] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ):
        """
        Args:
            venv_path:       Optional virtual environment root (e.g. /path/to/venv).
                             When set, all commands run inside that venv:
                             - {venv_path}/bin (Linux/macOS) or {venv_path}/Scripts (Windows)
                               is prepended to PATH so python/pip resolve to the venv.
                             - VIRTUAL_ENV is set to venv_path.
                             - PYTHONHOME is cleared to avoid interfering with the venv.
                             Equivalent to sourcing activate before each command, but
                             subprocess-friendly (no interactive shell required).
            interrupt_event: Optional asyncio.Event.  When set, execute() monitors it
                             concurrently with the subprocess.  If the event fires before
                             the command finishes, the process tree is killed immediately
                             and a ToolResult with error="Interrupted by planner" is
                             returned.  When None (default), behaviour is unchanged.
        """
        super().__init__("bash")
        self.venv_path = venv_path
        self.interrupt_event: Optional[asyncio.Event] = interrupt_event

    def _build_venv_env(self) -> Optional[dict]:
        """
        Build an environment dict with the venv activated.

        Returns:
            Modified environment dict, or None if venv_path is not set
            (subprocess inherits the parent environment unchanged).
        """
        if not self.venv_path:
            return None

        env = dict(os.environ)

        # Linux/macOS: bin/  Windows: Scripts/
        bin_dir = os.path.join(
            self.venv_path,
            "Scripts" if _IS_WINDOWS else "bin"
        )
        # Prepend venv bin dir to PATH so python/pip resolve to the venv versions
        existing_path = env.get("PATH", "")
        env["PATH"] = bin_dir + os.pathsep + existing_path if existing_path else bin_dir

        env["VIRTUAL_ENV"] = self.venv_path

        # PYTHONHOME overrides sys.prefix and must be cleared for the venv to work
        env.pop("PYTHONHOME", None)

        return env

    async def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        shell: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Execute a shell command.

        Args:
            command: Command string to execute.
            timeout: Timeout in seconds; defaults to 120s.  None means no limit.
            cwd:     Working directory; None inherits the current directory.
                     If the path does not exist, an error is returned immediately.
            shell:   Shell to use.
                     - None (default): /bin/sh on Linux/macOS, cmd.exe on Windows.
                     - "bash":         bash (requires Git Bash or WSL on Windows).
                     - "sh":           /bin/sh (or sh.exe on Windows).
                     - "powershell":   PowerShell (native or cross-platform pwsh).
                     - "cmd":          cmd.exe (Windows only).
                     - other string:   looked up as an executable name.

        Returns:
            ToolResult with structured observation dict containing:
              exit_code, stdout, stderr, truncated, cwd_used, command, shell, venv.
        """
        start_time = time.time()

        # Apply default timeout
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT_SECONDS

        try:
            self.validate_params(["command"], {"command": command})

            # ── (4) Working directory validation ─────────────────────────────
            cwd_used = cwd
            if cwd is not None and not os.path.isdir(cwd):
                return ToolResult(
                    success=False,
                    output={
                        "exit_code": None,
                        "stdout": "",
                        "stderr": f"Working directory does not exist: {cwd}",
                        "truncated": False,
                        "cwd_used": cwd,
                        "command": command,
                        "shell": None,
                        "venv": self.venv_path,
                    },
                    error=f"Working directory does not exist: {cwd}",
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

            # ── Dangerous command blocking (hard block) ───────────────────────
            _danger = _check_dangerous_command(command)
            if _danger:
                return ToolResult(
                    success=False,
                    output={
                        "exit_code": None,
                        "stdout": "",
                        "stderr": _danger,
                        "truncated": False,
                        "cwd_used": cwd_used,
                        "command": command,
                        "shell": None,
                        "venv": self.venv_path,
                    },
                    error=_danger,
                    execution_time=time.time() - start_time,
                    tool_name=self.name,
                    tool_parameters={'command': command, 'timeout': timeout},
                )

            # ── (5) Command injection warning (non-blocking) ──────────────────
            _check_injection(command)

            # ── (5b) Wide-search scope warning (non-blocking) ─────────────────
            _effective_cwd = cwd if cwd is not None else os.getcwd()
            _scope_warning = _check_wide_search(command, _effective_cwd)

            shell_argv = _resolve_shell(shell)
            executable = shell_argv[0]
            invoke_args = shell_argv[1:]

            # Full argv: [shell_exe, *invoke_args, command]
            full_argv = [executable, *invoke_args, command]

            # Build subprocess env: activate venv if set, otherwise inherit parent env
            proc_env = self._build_venv_env()  # None means inherit

            # On Windows set CREATE_NEW_PROCESS_GROUP so the whole process tree
            # can be terminated via taskkill on timeout.
            # On Unix set start_new_session=True so the subprocess gets its own
            # process group — this lets _kill_process_tree use os.killpg to kill
            # the entire tree without sending SIGKILL to the parent process group.
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
                cwd=cwd,
                env=proc_env,
                **kwargs_proc,
            )

            try:
                if self.interrupt_event is not None:
                    # Monitor interrupt_event concurrently with the subprocess.
                    # Whichever finishes first wins; the other is cancelled.
                    communicate_task = asyncio.create_task(process.communicate())
                    interrupt_task   = asyncio.create_task(self.interrupt_event.wait())
                    done, pending = await asyncio.wait(
                        [communicate_task, interrupt_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

                    if interrupt_task in done:
                        # Interrupt fired before the command finished — kill it.
                        await _kill_process_tree(process)
                        return ToolResult(
                            success=False,
                            output={'stdout': '', 'stderr': 'interrupted by user', 'exit_code': -1, 'truncated': False},
                            error=f"Interrupted by planner: {command}",
                            execution_time=time.time() - start_time,
                            tool_name=self.name,
                            tool_parameters={'command': command, 'timeout': timeout},
                        )

                    # communicate_task finished first — normal path.
                    stdout_bytes, stderr_bytes = communicate_task.result()

                else:
                    # No interrupt_event — use configurable timeout with graceful
                    # SIGTERM → SIGKILL escalation on expiry.
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
                                "cwd_used": cwd_used,
                                "command": command,
                                "shell": executable,
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

                # ── (2) Output truncation ─────────────────────────────────────
                stdout_text, stderr_text, truncated = _apply_truncation(stdout_text, stderr_text)

                # ── (3) Separate stdout/stderr with clear labels ───────────────
                # ── (6) Structured fields in observation dict ─────────────────
                # Append scope warning (if any) to stdout so the LLM sees it
                # immediately in the tool result without needing to check stderr.
                if _scope_warning:
                    stdout_text = stdout_text + _scope_warning

                observation = {
                    "exit_code": process.returncode,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "truncated": truncated,
                    "cwd_used": cwd_used,
                    # Legacy fields preserved for backward compatibility
                    "command": command,
                    "shell": executable,
                    "returncode": process.returncode,
                    "cwd": cwd,
                    "venv": self.venv_path,
                }

                # ── FileState invalidation ────────────────────────────────────
                # A bash command that exits 0 may have written or modified files
                # outside the read/write/edit tools, leaving FileState stale.
                # Clear all recorded reads so subsequent edit/write calls are
                # forced to re-read before acting.
                # Exception: commands marked concurrent_safe=True are read-only
                # by contract (grep, find, ls, etc.) and must not clear state,
                # since they run concurrently with other steps.
                _concurrent_safe = kwargs.get("concurrent_safe", False)
                # Blanket clear() removed: too aggressive — wiped all read records
                # even for commands that never touched any file (echo, grep, etc.).
                # The agent must explicitly re-read files it expects a bash command
                # to have modified before issuing a subsequent edit/write.

                return ToolResult(
                    success=process.returncode == 0,
                    output=observation,
                    error=stderr_text if process.returncode != 0 else None,
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


async def _kill_process_tree_graceful(process: asyncio.subprocess.Process) -> None:
    """
    Terminate a process tree with SIGTERM → SIGKILL escalation (Unix).

    Sends SIGTERM first and waits up to 5 seconds for graceful shutdown.
    If the process is still alive, escalates to SIGKILL.
    On Windows, falls back to force-kill immediately (no SIGTERM concept).
    """
    if _IS_WINDOWS:
        await _kill_process_tree(process)
        return

    # Unix: SIGTERM first
    _killpg  = getattr(os, "killpg",  None)
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

    # Wait up to 5 seconds for graceful exit
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass

    # Escalate to SIGKILL
    try:
        if _killpg and _getpgid and _SIGKILL:
            _killpg(_getpgid(process.pid), _SIGKILL)
        else:
            process.kill()
    except Exception:
        pass

    try:
        await process.wait()
    except Exception:
        pass


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    Terminate a process and all its children, cross-platform.

    - Windows: taskkill /F /T to force-terminate the entire process tree.
    - Unix:    SIGKILL to the process group (if available), otherwise direct kill.
    """
    if _IS_WINDOWS:
        try:
            # /F force-terminate, /T include child processes
            os.system(f"taskkill /F /T /PID {process.pid} >nul 2>&1")
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
    else:
        try:
            # os.killpg / os.getpgid / signal.SIGKILL exist only on Unix;
            # accessed via getattr to avoid static-analysis errors on Windows.
            import signal as _signal
            _killpg  = getattr(os, "killpg",  None)
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

    try:
        await process.wait()
    except Exception:
        pass
