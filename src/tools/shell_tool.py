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
import locale
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

# ── Output truncation ─────────────────────────────────────────────────────────
_TRUNCATION_TOTAL_CHARS: int = 15_000
_TRUNCATION_HEAD_CHARS: int = 5_000
_TRUNCATION_TAIL_CHARS: int = 5_000

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


def _check_dangerous_command(command: str) -> str | None:
    stripped = command.strip()
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


def _get_output_encoding() -> str:
    if _IS_WINDOWS:
        enc = locale.getpreferredencoding(False)
        return enc if enc else "utf-8"
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

    def _build_venv_env(self) -> Optional[dict]:
        if not self.venv_path:
            return None
        env = dict(os.environ)
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
            kill_requested = (command and command.strip().lower() == "kill")
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
                            error=f"Interrupted by planner: {command}",
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
                    "returncode": process.returncode,
                    "cwd": effective_cwd,
                    "venv": self.venv_path,
                }

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

    try:
        await process.wait()
    except Exception:
        pass


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

    try:
        await process.wait()
    except Exception:
        pass
