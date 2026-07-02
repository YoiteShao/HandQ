"""
Logger - Leveled logging system (DEBUG/INFO/WARNING/ERROR/CRITICAL).
"""
import io
import logging
import logging.handlers
import os
import sys
import inspect
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

# Per-async-task agent identifier.  Set via set_agent_context() inside each
# _execute_step coroutine so that all log calls within a parallel agent are
# automatically tagged with the agent's ID, making interleaved logs from
# concurrent agents easy to distinguish.
_agent_id_var: ContextVar[str] = ContextVar("agent_id", default="")

# Per-async-task session identifier. Set via set_session_context() at the top
# of each session's dispatch task in the bridge so that every log record
# emitted while serving that session (and all child tasks it spawns — asyncio
# copies the context per Task) is attributable to one session. Per-session
# file handlers (see add_root_file_handler) filter on this so concurrent
# sessions don't cross-contaminate each other's handq-engine.log.
_session_id_var: ContextVar[str] = ContextVar("session_id", default="")


class LogLevel(Enum):
    """Log level enumeration."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler with graceful failure handling for Windows and network shares.

    Standard RotatingFileHandler silently stops writing logs in two scenarios:

    1. **Rotation failure on Windows** — doRollover() closes the stream
       (self.stream = None) then calls os.rename().  On Windows, if any other
       process (VS Code, a log viewer, etc.) holds the file open, the rename
       raises PermissionError.  Because self.stream = self._open() is only
       reached *after* the rename, the stream stays None.  On the next emit(),
       shouldRollover() reopens the file, sees it is still ≥ maxBytes, returns
       1, doRollover() closes it again — an infinite loop of failed rotations
       that writes zero bytes.

    2. **Stale handle on a network share** — when the UNC share connection
       drops briefly, the open file handle becomes invalid.  All subsequent
       writes fail silently (caught by the base-class handleError()).
       ExecutionRecorder is immune because it opens the file fresh on every
       write; RotatingFileHandler is not.

    This subclass recovers from both scenarios:
    - Rotation failure: reopens the current file and continues writing (the
      file may exceed maxBytes, but no log entries are lost).
    - Stale handle / write failure: closes the bad handle, reopens the file,
      and retries the write once before falling back to handleError().
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rotation_failed: bool = False

    def doRollover(self) -> None:
        try:
            super().doRollover()
            self._rotation_failed = False
        except Exception:
            # Rotation failed (file locked on Windows, network hiccup, etc.).
            # Keep writing to the current file rather than silently dropping logs.
            self._rotation_failed = True
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    pass  # emit() will call handleError() if stream stays None

    def shouldRollover(self, record: logging.LogRecord) -> int:
        # After a rotation failure, skip the size check to avoid an infinite
        # loop of failed rotations.  The file will exceed maxBytes, but log
        # entries will continue to be written.
        if self._rotation_failed:
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    pass
            return 0
        try:
            return super().shouldRollover(record)
        except Exception:
            # shouldRollover() can fail if the file handle is stale
            # (e.g. network share reconnected).  Reopen and skip rotation.
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None  # type: ignore[assignment]
            try:
                self.stream = self._open()
            except Exception:
                pass
            return 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            logging.FileHandler.emit(self, record)
        except Exception:
            # First attempt failed (stale handle, rotation error, etc.).
            # Reopen the file and retry once before giving up.
            try:
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = None  # type: ignore[assignment]
                self.stream = self._open()
                logging.FileHandler.emit(self, record)
            except Exception:
                self.handleError(record)


class MultiLineFormatter(logging.Formatter):
    """
    Formatter that correctly indents continuation lines for multi-line messages.
    When a message contains \\n, subsequent lines are indented to align with
    the start of the message text.
    """

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)

        if '\n' not in formatted:
            return formatted

        lines = formatted.split('\n')
        first_line = lines[0]

        # Locate the message start in the first line to determine indent width.
        # Using the message content avoids mismatching on [agent_id]/[component]
        # bracket prefixes that could shift the indent incorrectly.
        msg_first_line = record.getMessage().split('\n')[0]
        if msg_first_line and msg_first_line in first_line:
            prefix_len = first_line.index(msg_first_line)
        else:
            last_bracket_end = first_line.rfind('] ')
            prefix_len = (last_bracket_end + 2) if last_bracket_end >= 0 else 2

        indent = ' ' * prefix_len
        return first_line + '\n' + '\n'.join(indent + line for line in lines[1:])


class HandQLogger:
    """HandQ logging system."""

    def __init__(
        self,
        name: str = "HandQ",
        level: LogLevel = LogLevel.INFO,
        log_file: Optional[str] = None,
        log_dir: str = "logs",
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5
    ):
        """
        Initialize the logger.

        Args:
            name:         Logger name.
            level:        Log level.
            log_file:     Log filename (optional; enables file output when set).
            log_dir:      Directory for the log file.
            max_bytes:    Maximum size in bytes for each log file (default: 10MB).
                          When reached, the log file is rotated.
            backup_count: Number of backup log files to keep (default: 5).
                          Old files are named as: filename.1, filename.2, etc.
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.value))
        # Detach any TextIOWrapper streams before clearing handlers.
        # io.TextIOWrapper.close() closes the underlying buffer (e.g.
        # sys.stdout.buffer), which would break sys.stdout for the rest of
        # the process.  detach() severs the ownership without closing it.
        for _h in list(self.logger.handlers):
            _stream = getattr(_h, 'stream', None)
            if isinstance(_stream, io.TextIOWrapper):
                try:
                    _stream.detach()
                except Exception:
                    pass
        self.logger.handlers.clear()
        self.level = level

        # Formatter with line numbers (DEBUG level and file output).
        formatter_with_line = MultiLineFormatter(
            '[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        # Formatter without line numbers (INFO level console output).
        formatter_without_line = MultiLineFormatter(
            '[%(asctime)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        try:
            stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
            # Verify the stream is actually writable (background child has stdout=DEVNULL)
            stdout_utf8.write("")
            stdout_utf8.flush()
        except Exception:
            stdout_utf8 = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
        console_handler = logging.StreamHandler(stdout_utf8)
        if level == LogLevel.DEBUG:
            console_handler.setFormatter(formatter_with_line)
        else:
            console_handler.setFormatter(formatter_without_line)
        console_handler.setLevel(getattr(logging, level.value))
        self.logger.addHandler(console_handler)

        if log_file:
            log_dir_path = Path(log_dir)
            log_dir_path.mkdir(parents=True, exist_ok=True)
            file_handler = SafeRotatingFileHandler(
                log_dir_path / log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding='utf-8'
            )
            file_handler.setFormatter(formatter_with_line)
            # Respect the configured level. Hardcoding DEBUG here used to leak
            # full third-party DEBUG payloads (openai request bodies, httpx
            # tracebacks, PIL chunk dumps) into the log file even when the
            # user set log_level=INFO in handq_config.yaml.
            file_handler.setLevel(getattr(logging, level.value))
            self.logger.addHandler(file_handler)

    def debug(self, message: str, component: str = "") -> None:
        """Log a DEBUG message."""
        agent_id = _agent_id_var.get()
        if agent_id:
            message = f"[{agent_id}] {message}"
        if component:
            message = f"[{component}] {message}"

        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            filename = Path(caller_frame.f_code.co_filename).name
            lineno = caller_frame.f_lineno
            record = self.logger.makeRecord(
                self.logger.name, logging.DEBUG, filename, lineno,
                message, (), None
            )
            self.logger.handle(record)
        else:
            self.logger.debug(message)

    def info(self, message: str, component: str = "") -> None:
        """Log an INFO message."""
        agent_id = _agent_id_var.get()
        if agent_id:
            message = f"[{agent_id}] {message}"
        if component:
            message = f"[{component}] {message}"

        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            filename = Path(caller_frame.f_code.co_filename).name
            lineno = caller_frame.f_lineno
            record = self.logger.makeRecord(
                self.logger.name, logging.INFO, filename, lineno,
                message, (), None
            )
            self.logger.handle(record)
        else:
            self.logger.info(message)

    def warning(self, message: str, component: str = "") -> None:
        """Log a WARNING message."""
        agent_id = _agent_id_var.get()
        if agent_id:
            message = f"[{agent_id}] {message}"
        if component:
            message = f"[{component}] {message}"

        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            filename = Path(caller_frame.f_code.co_filename).name
            lineno = caller_frame.f_lineno
            record = self.logger.makeRecord(
                self.logger.name, logging.WARNING, filename, lineno,
                message, (), None
            )
            self.logger.handle(record)
        else:
            self.logger.warning(message)

    def error(self, message: str, component: str = "", exc_info: bool = False) -> None:
        """Log an ERROR message."""
        agent_id = _agent_id_var.get()
        if agent_id:
            message = f"[{agent_id}] {message}"
        if component:
            message = f"[{component}] {message}"

        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            filename = Path(caller_frame.f_code.co_filename).name
            lineno = caller_frame.f_lineno
            record = self.logger.makeRecord(
                self.logger.name, logging.ERROR, filename, lineno,
                message, (), sys.exc_info() if exc_info else None
            )
            self.logger.handle(record)
        else:
            self.logger.error(message, exc_info=exc_info)

    def critical(self, message: str, component: str = "", exc_info: bool = False) -> None:
        """Log a CRITICAL message."""
        agent_id = _agent_id_var.get()
        if agent_id:
            message = f"[{agent_id}] {message}"
        if component:
            message = f"[{component}] {message}"

        frame = inspect.currentframe()
        if frame and frame.f_back:
            caller_frame = frame.f_back
            filename = Path(caller_frame.f_code.co_filename).name
            lineno = caller_frame.f_lineno
            record = self.logger.makeRecord(
                self.logger.name, logging.CRITICAL, filename, lineno,
                message, (), sys.exc_info() if exc_info else None
            )
            self.logger.handle(record)
        else:
            self.logger.critical(message, exc_info=exc_info)

    def set_level(self, level: LogLevel) -> None:
        """Set the log level."""
        self.level = level
        self.logger.setLevel(getattr(logging, level.value))


# Global logger instance
_global_logger: Optional[HandQLogger] = None


def initialize_logger(
    name: str = "HandQ",
    level: LogLevel = LogLevel.INFO,
    log_file: Optional[str] = None,
    log_dir: str = "logs",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5
) -> HandQLogger:
    """
    Initialize the global logger (call once at application startup).

    Args:
        name:         Logger name.
        level:        Log level.
        log_file:     Log filename (optional).
        log_dir:      Directory for the log file.
        max_bytes:    Maximum size in bytes for each log file (default: 10MB).
        backup_count: Number of backup log files to keep (default: 5).

    Returns:
        HandQLogger instance.
    """
    global _global_logger
    _global_logger = HandQLogger(name, level, log_file, log_dir, max_bytes, backup_count)
    return _global_logger


def set_agent_context(agent_id: str) -> None:
    """
    Bind *agent_id* to the current async-task context.

    Because asyncio copies the context for every new Task, calling this inside
    a coroutine only affects that coroutine and everything it awaits — parallel
    tasks running in the same event loop are completely unaffected.

    Args:
        agent_id: The agent identifier to attach to subsequent log messages.
                  Pass an empty string to clear the binding.
    """
    _agent_id_var.set(agent_id)


def get_logger() -> HandQLogger:
    """
    Return the global logger instance.

    If not yet initialized, creates one with default settings (INFO level,
    no log file).  Call initialize_logger() at startup to configure it.

    Returns:
        HandQLogger instance.
    """
    global _global_logger
    if _global_logger is None:
        _global_logger = HandQLogger(name="HandQ", level=LogLevel.INFO, log_file=None)
    return _global_logger


def set_session_context(session_id: str) -> None:
    """Bind *session_id* to the current async-task context.

    asyncio copies the context for every new Task, so calling this at the top
    of a session's dispatch coroutine tags that coroutine and everything it
    awaits (including child agent tasks) with the session id, while other
    sessions running concurrently on the same loop are unaffected. Per-session
    file handlers added via :func:`add_root_file_handler` filter on this so a
    record only reaches the engine.log of the session whose task emitted it.

    Pass an empty string to clear the binding.
    """
    _session_id_var.set(session_id)


class _SessionLogFilter(logging.Filter):
    """Pass a record only when the current task's session context matches.

    Attached to a per-session root file handler so concurrent sessions don't
    cross-contaminate. Records emitted outside any session task (boot,
    bridge-meta, scheduler setup before a session is bound) carry an empty
    session id and are dropped by every per-session handler — they belong in
    handq-bridge.log, not in any one session's engine.log.
    """

    _HANDQ_PREFIXES = ("handq", "HandQ", "src", "__main__")

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not record.name.startswith(self._HANDQ_PREFIXES):
            return False
        return _session_id_var.get() == self._session_id


def add_root_file_handler(
    log_path: str,
    level: LogLevel = LogLevel.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    session_id: Optional[str] = None,
) -> logging.Handler:
    """
    Attach a session-scoped file handler to the ROOT logger and return it.

    initialize_logger()'s file handler is bound to the "HandQ" logger name, so
    it only captures get_logger() calls. This handler sits on the root logger
    instead, so it records everything that propagates to root during the
    session: the HandQ tree PLUS every stdlib logging.getLogger(__name__)
    module (shell_tool / session_tool / session_context / ...). The
    background-daemon trees that set propagate=False (handq.ltm /
    handq.personality / handq.activity / handq.scheduler — diverted to
    .dia/internal-trace.log by bridge_main.py) are deliberately excluded.

    The bridge calls this once a session directory is allocated and pairs it
    with remove_root_file_handler() at session teardown, making
    handq-engine.log a clean "everything that happened in this session" view.

    Multi-session isolation: when *session_id* is given, the handler is fitted
    with a :class:`_SessionLogFilter` so it only records lines emitted within
    that session's dispatch-task context (see set_session_context). Without it,
    every concurrent session's records would land in every session's file,
    because all the handlers share the one root logger.

    Args:
        log_path:     Absolute path of the per-session log file.
        level:        Minimum level the handler records.
        max_bytes:    Rotation threshold per file (default: 10MB).
        backup_count: Number of rotated backups to keep (default: 5).
        session_id:   When set, restrict the handler to this session's records.
    """
    log_path_obj = Path(log_path)
    log_path_obj.parent.mkdir(parents=True, exist_ok=True)
    handler = SafeRotatingFileHandler(
        log_path_obj,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(MultiLineFormatter(
        '[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    handler.setLevel(getattr(logging, level.value))
    if session_id:
        handler.addFilter(_SessionLogFilter(session_id))
    logging.getLogger().addHandler(handler)
    return handler


def remove_root_file_handler(handler: Optional[logging.Handler]) -> None:
    """
    Detach and close a handler returned by add_root_file_handler().

    Safe to call with None or with a handler that is no longer attached.
    Closing flushes and releases the file so the next session can open a fresh
    handq-engine.log without a lingering Windows file lock.
    """
    if handler is None:
        return
    try:
        logging.getLogger().removeHandler(handler)
    finally:
        try:
            handler.close()
        except Exception:
            pass
