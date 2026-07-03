"""
Interactive Session Tool — spawn and control long-lived subprocesses across
multiple tool calls.

Supports: adb shell, Python REPL, telnet, any interactive CLI.

Actions:
  open   — spawn a subprocess, return session_id.
            Pass alias="name" to reuse an existing alive session with
            the same alias instead of opening a new one.
  exec   — send a command and wait for completion (delimiter or prompt)
  write  — send raw stdin bytes without waiting
  read   — return buffered output (non-blocking or with short wait)
  list   — list active sessions
  close  — kill the subprocess tree and clean up
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# ── Buffer limits ────────────────────────────────────────────────────────────
_BUFFER_MAX_BYTES = 2_048_000
_BUFFER_EVICT_RATIO = 0.5
_RETURN_MAX_CHARS = 80_000
_RETURN_HEAD_CHARS = 30_000
_RETURN_TAIL_CHARS = 20_000

# ── Session limits ───────────────────────────────────────────────────────────
_MAX_SESSIONS = 4
_OPEN_INITIAL_WAIT = 0.8  # seconds to collect initial output on open
_OPEN_SSH_WAIT = 2.0  # extra time for SSH handshake + remote shell startup
_EXEC_DEFAULT_TIMEOUT = 30
_CLOSE_GRACE_TIMEOUT = 2.0

# ── Real-time UI streaming ──────────────────────────────────────────────────
_DATA_EMIT_INTERVAL = 0.016  # ~60 fps cap for session_data events
_DATA_EMIT_MAX_CHARS = 4096  # max chars per session_data event


def _get_output_encoding() -> str:
    if _IS_WINDOWS:
        import locale
        enc = locale.getpreferredencoding(False)
        return enc if enc else "utf-8"
    return "utf-8"


def _truncate_output(text: str) -> tuple[str, bool]:
    if len(text) <= _RETURN_MAX_CHARS:
        return text, False
    omitted = len(text) - _RETURN_HEAD_CHARS - _RETURN_TAIL_CHARS
    notice = (
        f"\n\n... [output truncated: {omitted:,} characters omitted "
        f"({len(text):,} total)] ...\n\n"
    )
    return text[:_RETURN_HEAD_CHARS] + notice + text[-_RETURN_TAIL_CHARS:], True


# ── Session data model ───────────────────────────────────────────────────────

@dataclass
class InteractiveSession:
    session_id: str
    command: str
    description: str
    process: asyncio.subprocess.Process
    pid: int
    start_time: float
    status: str = "alive"  # "alive" | "dead" | "killed"
    exit_code: Optional[int] = None
    alias: Optional[str] = None

    # I/O
    _stdout_buffer: Deque[str] = field(default_factory=collections.deque)
    _buffer_bytes: int = 0
    _reader_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _encoding: str = "utf-8"

    # Exec synchronisation
    _pending_delimiter: Optional[str] = field(default=None, repr=False)
    _delimiter_output: str = field(default="", repr=False)
    _delimiter_seen: Optional[asyncio.Event] = field(default=None, repr=False)

    # Prompt pattern (for REPLs)
    prompt_pattern: Optional[re.Pattern] = field(default=None, repr=False)
    _prompt_output: str = field(default="", repr=False)
    _prompt_seen: Optional[asyncio.Event] = field(default=None, repr=False)

    # Output activity tracking — updated by _append_to_buffer on every
    # chunk received from the subprocess stdout. Consumers (list/read actions)
    # expose this as idle_seconds for hang detection.
    last_output_ts: float = field(default_factory=time.time)

    # Real-time UI streaming throttle
    _data_emit_buf: str = field(default="", repr=False)
    _data_emit_last_ts: float = field(default=0.0, repr=False)

    # UI bus for lifecycle events. Stamped by InteractiveSessionTool._action_open
    # from ctx.interaction_manager so the module-level reader task / buffer
    # helpers (which only have the session in scope) can emit through it.
    _im: Optional[Any] = field(default=None, repr=False)


# ── Buffer helpers ───────────────────────────────────────────────────────────

def _append_to_buffer(session: InteractiveSession, text: str) -> None:
    session._stdout_buffer.append(text)
    byte_len = len(text.encode("utf-8", errors="replace"))
    session._buffer_bytes += byte_len
    session.last_output_ts = time.time()

    # Real-time UI streaming: throttle to avoid flooding IPC
    session._data_emit_buf += text
    now = time.time()
    elapsed = now - session._data_emit_last_ts
    if (elapsed >= _DATA_EMIT_INTERVAL
            or len(session._data_emit_buf) >= _DATA_EMIT_MAX_CHARS):
        chunk = session._data_emit_buf[:_DATA_EMIT_MAX_CHARS]
        session._data_emit_buf = session._data_emit_buf[_DATA_EMIT_MAX_CHARS:]
        session._data_emit_last_ts = now
        _emit_session_event(session, "session_data", {
            "session_id": session.session_id,
            "text": chunk,
        })

    # Accumulate into delimiter/prompt output if exec is pending
    if session._pending_delimiter is not None:
        session._delimiter_output += text
        # Only fire when the delimiter appears on its OWN line (the output of
        # "echo __DELIM__"), not when it appears inside the echoed command line.
        # Check: delimiter must be preceded by a newline (or be at the start).
        delim = session._pending_delimiter
        idx = session._delimiter_output.find("\n" + delim)
        if idx == -1 and session._delimiter_output.startswith(delim):
            idx = 0
        if idx >= 0:
            if session._delimiter_seen:
                session._delimiter_seen.set()
    if session.prompt_pattern is not None and session._prompt_seen is not None:
        session._prompt_output += text
        lines = session._prompt_output.split("\n")
        if lines:
            last_line = lines[-1]
            if session.prompt_pattern.search(last_line):
                session._prompt_seen.set()

    # Evict oldest chunks if over limit
    while session._buffer_bytes > _BUFFER_MAX_BYTES and session._stdout_buffer:
        evicted = session._stdout_buffer.popleft()
        session._buffer_bytes -= len(evicted.encode("utf-8", errors="replace"))


def _drain_buffer(session: InteractiveSession) -> str:
    text = "".join(session._stdout_buffer)
    session._stdout_buffer.clear()
    session._buffer_bytes = 0
    return text


# ── Reader coroutine ─────────────────────────────────────────────────────────

async def _session_reader(session: InteractiveSession) -> None:
    CHUNK_SIZE = 4096
    try:
        while True:
            assert session.process.stdout is not None
            data = await session.process.stdout.read(CHUNK_SIZE)
            if not data:
                session.status = "dead"
                session.exit_code = session.process.returncode
                break
            text = data.decode(session._encoding, errors="replace")
            if _IS_WINDOWS:
                text = text.replace("\r\n", "\n")
            _append_to_buffer(session, text)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("session reader %s crashed: %s", session.session_id, exc)
        session.status = "dead"


# ── Process kill ─────────────────────────────────────────────────────────────

async def _kill_session_process(process: asyncio.subprocess.Process) -> None:
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
            _killpg = getattr(os, "killpg", None)
            _getpgid = getattr(os, "getpgid", None)
            _SIGKILL = getattr(signal, "SIGKILL", None)
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
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except Exception:
        pass


# ── Session registry (per-session; one instance lives on each SessionContext) ─

class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: Dict[str, InteractiveSession] = {}
        self._counter: int = 0

    def create_id(self) -> str:
        self._counter += 1
        return f"sess_{self._counter}"

    def count(self) -> int:
        return len(self._sessions)

    def register(self, session: InteractiveSession) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Optional[InteractiveSession]:
        return self._sessions.get(session_id)

    def find_by_alias(self, alias: str) -> Optional[InteractiveSession]:
        for s in self._sessions.values():
            if s.alias == alias and s.status == "alive":
                return s
        return None

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_sessions(self) -> List[Dict[str, Any]]:
        result = []
        now = time.time()
        for s in self._sessions.values():
            elapsed = now - s.start_time
            idle = now - s.last_output_ts
            result.append({
                "session_id": s.session_id,
                "alias": s.alias,
                "command": s.command,
                "description": s.description,
                "status": s.status,
                "pid": s.pid,
                "elapsed_seconds": round(elapsed, 1),
                "idle_seconds": round(idle, 1),
                "bytes_buffered": s._buffer_bytes,
            })
        return result

    async def kill_all(self) -> int:
        count = 0
        for s in list(self._sessions.values()):
            await _close_session(s)
            count += 1
        self._sessions.clear()
        self._counter = 0
        return count

    async def close_all(self) -> Optional[str]:
        """Per-session-end cleanup: kill every session in this registry, return
        a one-line summary (or ``None`` when empty). Used by
        :meth:`SessionContext.close` so each ``SessionRegistry`` instance can be
        torn down independently."""
        n = await self.kill_all()
        if n:
            return f"{n} interactive session(s) closed"
        return None


# ctx=None fallback only (bare ``InteractiveSessionTool()`` in unit tests). The
# live flow routes through ``ctx.session_registry`` and never touches this.
_registry = SessionRegistry()


async def _close_session(session: InteractiveSession) -> Optional[str]:
    """Close a single session: cancel reader, close stdin, kill process."""
    final_output = _drain_buffer(session)

    if session._reader_task and not session._reader_task.done():
        session._reader_task.cancel()
        try:
            await session._reader_task
        except (asyncio.CancelledError, Exception):
            pass

    if session.process.stdin and not session.process.stdin.is_closing():
        try:
            session.process.stdin.close()
        except Exception:
            pass

    if session.process.returncode is None:
        try:
            await asyncio.wait_for(session.process.wait(), timeout=_CLOSE_GRACE_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_session_process(session.process)
        except Exception:
            await _kill_session_process(session.process)

    session.status = "killed"
    session.exit_code = session.process.returncode
    return final_output


# ── InteractiveSessionTool ───────────────────────────────────────────────────

class InteractiveSessionTool(BaseTool):
    """Spawn and control long-lived interactive subprocesses."""

    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, ctx=None) -> None:
        super().__init__("session", ctx=ctx)
        # Pull interrupt_event from the SessionContext when supplied so the
        # asyncio.wait([communicate, interrupt]) race in _action_exec wakes up
        # on session-end.
        self.interrupt_event: Optional[asyncio.Event] = (
            ctx.interrupt_event if ctx is not None else None
        )
        # Per-session registry from the SessionContext. ctx=None test fixtures
        # fall back to the module-level singleton so bare ``InteractiveSessionTool()``
        # still works.
        self.registry: SessionRegistry = (
            ctx.session_registry if ctx is not None else _registry
        )
        # UI bus, stamped onto each opened session so module-level helpers can
        # emit lifecycle events. None under ctx=None test fixtures.
        self.im = ctx.interaction_manager if ctx is not None else None

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "")
        start_time = time.time()

        dispatch = {
            "open":  self._action_open,
            "exec":  self._action_exec,
            "write": self._action_write,
            "read":  self._action_read,
            "list":  self._action_list,
            "close": self._action_close,
        }

        handler = dispatch.get(action)
        if not handler:
            return ToolResult(
                success=False, output=None,
                error=f"Unknown action '{action}'. Must be one of: {', '.join(dispatch.keys())}",
                tool_name=self.name,
                tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        try:
            return await handler(start_time=start_time, **kwargs)
        except Exception as exc:
            logger.exception("session tool action=%s failed", action)
            return ToolResult(
                success=False, output=None,
                error=f"session.{action} failed: {exc}",
                tool_name=self.name,
                tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

    # ── open ─────────────────────────────────────────────────────────────────

    async def _action_open(self, start_time: float, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command", "")
        alias = kwargs.get("alias")

        # Alias reuse: if an alive session with this alias exists, return it
        if alias:
            existing = self.registry.find_by_alias(alias)
            if existing:
                buffered = _drain_buffer(existing)
                return ToolResult(
                    success=True,
                    output={
                        "session_id": existing.session_id,
                        "pid": existing.pid,
                        "status": existing.status,
                        "reused": True,
                        "alias": alias,
                        "buffered_output": buffered[:_RETURN_MAX_CHARS] if buffered else "",
                        "message": f"Reused existing session '{existing.session_id}' (alias='{alias}').",
                    },
                    tool_name=self.name, tool_parameters=kwargs,
                    execution_time=time.time() - start_time,
                )

        if not command:
            return ToolResult(
                success=False, output=None,
                error="'command' is required for action='open'.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        if self.registry.count() >= _MAX_SESSIONS:
            return ToolResult(
                success=False, output=None,
                error=f"Maximum {_MAX_SESSIONS} concurrent sessions reached. Close one first.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        description = kwargs.get("description", command[:60])
        cwd = kwargs.get("cwd")
        prompt_pattern_str = kwargs.get("prompt_pattern")
        merge_stderr = kwargs.get("merge_stderr", True)

        # Parse prompt pattern
        prompt_pattern: Optional[re.Pattern] = None
        if prompt_pattern_str:
            try:
                prompt_pattern = re.compile(prompt_pattern_str)
            except re.error as exc:
                return ToolResult(
                    success=False, output=None,
                    error=f"Invalid prompt_pattern regex: {exc}",
                    tool_name=self.name, tool_parameters=kwargs,
                    execution_time=time.time() - start_time,
                )

        # Force PTY allocation for SSH commands.  Without -tt, SSH won't
        # allocate a remote PTY when local stdin is a pipe, causing the remote
        # shell to run non-interactively — stdin forwarding stalls and exec
        # times out with 0 output.
        is_ssh = bool(re.match(r"^ssh\b", command))
        if is_ssh and "-tt" not in command:
            command = re.sub(r"^ssh\b", "ssh -tt", command)

        # SSH credentials are pre-established by ssh_setup.py (key auth or
        # keyring) BEFORE this tool runs. The subprocess only needs a working
        # ssh.exe in PATH and inherited environment — we never inject passwords
        # here.

        # Build argv — spawn the command directly (no shell wrapper).
        # Interactive programs (adb shell, python -i) must own stdin/stdout
        # directly; wrapping in cmd.exe /c would add an extra parent process
        # that swallows stdin and complicates kill.
        if _IS_WINDOWS:
            # shlex.split doesn't handle Windows paths well; use a simple split
            # but respect quotes.
            import shlex
            try:
                argv = shlex.split(command, posix=False)
            except ValueError:
                argv = command.split()
        else:
            import shlex
            argv = shlex.split(command)

        # Subprocess creation
        kwargs_proc: Dict[str, Any] = {}
        if _IS_WINDOWS:
            import subprocess as _sp
            kwargs_proc["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs_proc["start_new_session"] = True

        # A relative cwd is resolved against the per-session workspace, so
        # neither the isdir check nor the subprocess base depends on the
        # process cwd (no longer mutated via os.chdir — see concurrency work).
        resolved_cwd = self.resolve_in_workspace(cwd) if cwd else None
        effective_cwd = resolved_cwd if (resolved_cwd and os.path.isdir(resolved_cwd)) else (
            self.ctx.working_directory if (self.ctx and self.ctx.working_directory) else None
        )
        stderr_target = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_target,
                cwd=effective_cwd,
                **kwargs_proc,
            )
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=f"Failed to spawn process: {exc}",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        session_id = self.registry.create_id()
        session = InteractiveSession(
            session_id=session_id,
            command=command,
            description=description,
            process=process,
            pid=process.pid,
            start_time=time.time(),
            _encoding=_get_output_encoding(),
            prompt_pattern=prompt_pattern,
            alias=alias,
            _im=self.im,
        )

        self.registry.register(session)

        # Emit session_opened BEFORE starting reader so the UI creates the
        # terminal before any session_data events arrive.
        _emit_session_event(session, "session_opened", {
            "session_id": session_id,
            "command": command,
            "description": description,
            "pid": process.pid,
        })

        # Start reader
        session._reader_task = asyncio.create_task(
            _session_reader(session),
            name=f"session-reader-{session_id}",
        )

        # SSH connections need extra time for handshake + remote shell startup.
        # Credentials are pre-established by ssh_setup.py (key auth or keyring),
        # so this branch covers handshake only — no password prompt to fill.
        if is_ssh:
            await asyncio.sleep(_OPEN_SSH_WAIT)
        else:
            await asyncio.sleep(_OPEN_INITIAL_WAIT)

        # Flush any remaining throttled data to the UI
        if session._data_emit_buf:
            _emit_session_event(session, "session_data", {
                "session_id": session_id,
                "text": session._data_emit_buf,
            })
            session._data_emit_buf = ""
            session._data_emit_last_ts = time.time()
        initial_output = _drain_buffer(session)
        initial_output, _ = _truncate_output(initial_output)

        # Reject one-shot commands: if the process has already exited within
        # the settle window, the agent is using session for something the
        # shell tool should do (e.g. ``python script.py``, a malformed
        # ``python -c "..."`` that crashed on syntax). Without this guard
        # the open returns success=True with status='dead', the agent
        # doesn't notice, then ``read`` / ``exec`` fail later with confusing
        # "session is dead" errors several iterations downstream.
        if session.status == "dead":
            self.registry.remove(session_id)
            return ToolResult(
                success=False,
                output={
                    "session_id": session_id,
                    "exit_code": session.exit_code,
                    "initial_output": initial_output,
                },
                error=(
                    f"Session command exited within "
                    f"{_OPEN_SSH_WAIT if is_ssh else _OPEN_INITIAL_WAIT:g}s "
                    f"of launch (exit_code={session.exit_code}). The session "
                    f"tool is for long-lived interactive processes — for "
                    f"one-shot commands use the shell tool instead. For an "
                    f"interactive REPL try python -i / pwsh / bash / "
                    f"ssh -tt host. Initial output (if any) is in the "
                    f"output payload."
                ),
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        return ToolResult(
            success=True,
            output={
                "session_id": session_id,
                "pid": process.pid,
                "status": session.status,
                "alias": alias,
                "initial_output": initial_output,
                "message": f"Session '{session_id}' opened. Use session_id='{session_id}' for subsequent calls.",
            },
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )

    # ── exec ─────────────────────────────────────────────────────────────────

    async def _action_exec(self, start_time: float, **kwargs: Any) -> ToolResult:
        session_id = kwargs.get("session_id", "")
        command = kwargs.get("command", "")
        timeout = float(kwargs.get("timeout", _EXEC_DEFAULT_TIMEOUT))

        session = self.registry.get(session_id)
        if not session:
            return ToolResult(
                success=False, output=None,
                error=f"Session '{session_id}' not found.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )
        if session.status != "alive":
            remaining = _drain_buffer(session)
            return ToolResult(
                success=False,
                output={"status": session.status, "exit_code": session.exit_code,
                        "remaining_output": remaining},
                error=f"Session '{session_id}' is {session.status}.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )
        if not command:
            return ToolResult(
                success=False, output=None,
                error="'command' is required for action='exec'.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        # Drain any buffered output from previous activity
        _drain_buffer(session)

        # Determine completion strategy
        use_prompt = (session.prompt_pattern is not None)

        if use_prompt:
            # REPL mode: wait for prompt to reappear
            session._prompt_output = ""
            session._prompt_seen = asyncio.Event()
            input_text = command + "\n"
        else:
            # Shell mode: inject delimiter
            delimiter = f"__HANDQ_DONE_{secrets.token_hex(4)}__"
            session._pending_delimiter = delimiter
            session._delimiter_output = ""
            session._delimiter_seen = asyncio.Event()
            # Ensure delimiter always echoes regardless of command exit code.
            # Use platform-appropriate command separator:
            #   - POSIX shells (sh, bash, adb shell): ";"
            #   - Windows cmd.exe: "&"
            # We detect cmd.exe by checking the open command.
            cmd_lower = session.command.lower()
            is_cmd_exe = ("cmd.exe" in cmd_lower or "cmd /k" in cmd_lower
                          or "cmd /q" in cmd_lower)
            sep = " & " if is_cmd_exe else "; "
            input_text = f"{command}{sep}echo {delimiter}\n"

        # Write to stdin
        assert session.process.stdin is not None
        try:
            session.process.stdin.write(input_text.encode(session._encoding, errors="replace"))
            await session.process.stdin.drain()
        except Exception as exc:
            session._pending_delimiter = None
            session._delimiter_seen = None
            session._prompt_seen = None
            return ToolResult(
                success=False, output=None,
                error=f"Failed to write to session stdin: {exc}",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        # Notify UI of the agent's input
        _emit_session_event(session, "session_input", {
            "session_id": session_id,
            "text": command,
        })

        # Wait for completion signal
        wait_event = session._prompt_seen if use_prompt else session._delimiter_seen
        assert wait_event is not None

        timed_out = False
        interrupted = False

        if self.interrupt_event is not None:
            wait_task = asyncio.create_task(wait_event.wait())
            interrupt_task = asyncio.create_task(self.interrupt_event.wait())
            done, pending = await asyncio.wait(
                [wait_task, interrupt_task],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if not done:
                timed_out = True
            elif interrupt_task in done:
                interrupted = True
        else:
            try:
                await asyncio.wait_for(wait_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True

        # Extract output
        if use_prompt:
            raw_output = session._prompt_output
            session._prompt_output = ""
            session._prompt_seen = None
            # Strip the command echo (first line) if it matches
            lines = raw_output.split("\n", 1)
            if lines and lines[0].strip() == command.strip():
                output_text = lines[1] if len(lines) > 1 else ""
            else:
                output_text = raw_output
            # Strip trailing prompt line
            if session.prompt_pattern and output_text:
                out_lines = output_text.rsplit("\n", 1)
                if len(out_lines) == 2 and session.prompt_pattern.search(out_lines[1]):
                    output_text = out_lines[0]
        else:
            raw_output = session._delimiter_output
            # Clear exec state
            delim_token = session._pending_delimiter or ""
            session._pending_delimiter = None
            session._delimiter_output = ""
            session._delimiter_seen = None
            # The delimiter appears twice in cmd.exe output:
            #   1. Inside the echoed command line: "...& echo __DELIM__"
            #   2. On its own line (actual echo output): "\n__DELIM__\n"
            # Split on the LAST occurrence to capture everything before the
            # actual delimiter output line.
            if delim_token and delim_token in raw_output:
                # rsplit ensures we get everything before the LAST delimiter
                output_text = raw_output.rsplit(delim_token, 1)[0]
            else:
                output_text = raw_output
            # Strip the echoed input line (contains command + delimiter echo).
            # Interactive shells echo the typed line back. We detect it by
            # checking if the user's command text appears in the first line.
            lines = output_text.split("\n", 1)
            if lines and command.strip() in lines[0]:
                output_text = lines[1] if len(lines) > 1 else ""
            # Also strip any line that contains "echo <delimiter>" (shell echo
            # of our injected delimiter command). Handle both ";" and "&" separators.
            if output_text and delim_token:
                out_lines = output_text.split("\n")
                out_lines = [l for l in out_lines
                             if f"echo {delim_token}" not in l]
                output_text = "\n".join(out_lines)

        # Clean up leading/trailing whitespace
        output_text = output_text.strip()

        # Drain the buffer so subsequent read() calls only see genuinely
        # new content (not leftover prompt/delimiter fragments from this exec).
        _drain_buffer(session)

        # Truncate if needed
        output_text, truncated = _truncate_output(output_text)

        # Emit visualization
        _emit_session_event(session, "session_exec_done", {
            "session_id": session_id,
            "command": command,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "output_chars": len(output_text),
        })

        result_data: Dict[str, Any] = {
            "session_id": session_id,
            "output": output_text,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "truncated": truncated,
            "status": session.status,
        }

        return ToolResult(
            success=not timed_out and not interrupted,
            output=result_data,
            error="Command timed out" if timed_out else ("Interrupted" if interrupted else None),
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )

    # ── write ────────────────────────────────────────────────────────────────

    async def _action_write(self, start_time: float, **kwargs: Any) -> ToolResult:
        session_id = kwargs.get("session_id", "")
        input_text = kwargs.get("input", "")
        append_newline = kwargs.get("append_newline", True)

        session = self.registry.get(session_id)
        if not session:
            return ToolResult(
                success=False, output=None,
                error=f"Session '{session_id}' not found.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )
        if session.status != "alive":
            return ToolResult(
                success=False, output=None,
                error=f"Session '{session_id}' is {session.status}.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        payload = input_text
        if append_newline and not payload.endswith("\n"):
            payload += "\n"

        assert session.process.stdin is not None
        try:
            session.process.stdin.write(payload.encode(session._encoding, errors="replace"))
            await session.process.stdin.drain()
        except Exception as exc:
            return ToolResult(
                success=False, output=None,
                error=f"Failed to write to session stdin: {exc}",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        # Notify UI of the agent's input
        _emit_session_event(session, "session_input", {
            "session_id": session_id,
            "text": input_text,
        })

        return ToolResult(
            success=True,
            output={
                "session_id": session_id,
                "bytes_written": len(payload),
                "status": session.status,
            },
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )

    # ── read ─────────────────────────────────────────────────────────────────

    async def _action_read(self, start_time: float, **kwargs: Any) -> ToolResult:
        session_id = kwargs.get("session_id", "")
        timeout = float(kwargs.get("timeout", 0))

        session = self.registry.get(session_id)
        if not session:
            return ToolResult(
                success=False, output=None,
                error=f"Session '{session_id}' not found.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        # If timeout > 0, wait a bit for new data to arrive
        if timeout > 0 and session.status == "alive":
            initial_bytes = session._buffer_bytes
            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.1)
                if session._buffer_bytes > initial_bytes:
                    # Got new data — wait a tiny bit more for the burst to finish
                    await asyncio.sleep(0.2)
                    break
                if session.status != "alive":
                    break

        text = _drain_buffer(session)
        text, truncated = _truncate_output(text)
        idle = time.time() - session.last_output_ts

        return ToolResult(
            success=True,
            output={
                "session_id": session_id,
                "output": text,
                "truncated": truncated,
                "status": session.status,
                "alive": session.status == "alive",
                "idle_seconds": round(idle, 1),
            },
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )

    # ── list ─────────────────────────────────────────────────────────────────

    async def _action_list(self, start_time: float, **kwargs: Any) -> ToolResult:
        sessions = self.registry.list_sessions()
        return ToolResult(
            success=True,
            output={"sessions": sessions, "count": len(sessions)},
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )

    # ── close ────────────────────────────────────────────────────────────────

    async def _action_close(self, start_time: float, **kwargs: Any) -> ToolResult:
        session_id = kwargs.get("session_id", "")

        session = self.registry.get(session_id)
        if not session:
            return ToolResult(
                success=False, output=None,
                error=f"Session '{session_id}' not found.",
                tool_name=self.name, tool_parameters=kwargs,
                execution_time=time.time() - start_time,
            )

        final_output = await _close_session(session)
        self.registry.remove(session_id)

        final_output = final_output or ""
        final_output, _ = _truncate_output(final_output)

        _emit_session_event(session, "session_closed", {
            "session_id": session_id,
            "exit_code": session.exit_code,
        })

        return ToolResult(
            success=True,
            output={
                "session_id": session_id,
                "final_output": final_output,
                "exit_code": session.exit_code,
                "status": "closed",
            },
            tool_name=self.name, tool_parameters=kwargs,
            execution_time=time.time() - start_time,
        )


# ── Visualization events ─────────────────────────────────────────────────────
#
# Emit status events through the InteractionManager's UI delegate so the
# Electron renderer can show a live session monitor panel. If no UI is
# attached (e.g. unit testing), events are silently dropped.

def _emit_session_event(
    session: Optional[InteractiveSession], event_name: str, data: Dict[str, Any]
) -> None:
    """Best-effort emit a session lifecycle event through the InteractionManager
    stamped on the session.

    The IM is carried on the ``InteractiveSession`` (``session._im``) rather than
    passed explicitly because the emitters live in module-level helpers (the
    reader task, the buffer flusher) that only have the session in scope. When no
    IM is wired (unit tests, ctx=None fixtures) the event is silently dropped.
    """
    if session is None or session._im is None:
        return
    try:
        session._im.notify_session_event(event_name, data)
    except Exception:
        pass
