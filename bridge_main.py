"""Top-level entry script for the Electron renderer to spawn.

Usage (from Electron main.js):
    child_process.spawn(pythonExe, ['bridge_main.py'], {stdio: ['pipe','pipe','pipe']})
"""
import io
import os
import sys

# Reserve the real stdin/stdout for the JSON IPC channel BEFORE any other
# module can grab them:
#   - logger.py:210 captures sys.stdout.buffer at logger-init time.
#   - interaction_manager.py:107 spawns a daemon thread that reads sys.stdin
#     line-by-line; left alone it would steal every IPC line we send.
#
# Strategy: dup fd 0 and fd 1 to private fds (exposed via env vars for the
# bridge), then replace fd 0 with /dev/null and fd 1 with fd 2, so any code
# that touches sys.stdin sees immediate EOF and any code that prints to
# sys.stdout lands on stderr instead.
_real_stdout_fd = os.dup(1)
_real_stdin_fd = os.dup(0)
os.dup2(2, 1)
_devnull_fd = os.open(os.devnull, os.O_RDONLY)
os.dup2(_devnull_fd, 0)
os.close(_devnull_fd)

sys.stdout = io.TextIOWrapper(
    os.fdopen(2, "wb", closefd=False),
    encoding="utf-8",
    errors="replace",
    line_buffering=True,
)
sys.stdin = io.TextIOWrapper(
    os.fdopen(0, "rb", closefd=False),
    encoding="utf-8",
    errors="replace",
)
os.environ["HANDQ_BRIDGE_STDOUT_FD"] = str(_real_stdout_fd)
os.environ["HANDQ_BRIDGE_STDIN_FD"] = str(_real_stdin_fd)

# Make 'src' importable when this script is launched from the project root.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# Self-locating install directory + config path resolution.
#
# The bridge MUST NOT rely on cwd to locate handq_config.yaml. Cwd is
# unpredictable: when launched by Electron from a desktop shortcut, the Start
# menu, or the command line, it can be anything. We resolve paths relative
# to the bridge entry point itself, so the same logic works for:
#
#   * dev mode  — running `python bridge_main.py`. __file__ points at this
#                 script; its parent is the repo root.
#   * Nuitka standalone — `bridge_main.exe` lives in <project>.dist/. Nuitka
#                 sets the module-level attribute __compiled__; sys.executable
#                 points at the .exe.
#   * PyInstaller — sets sys.frozen = True; sys.executable points at the .exe.
#
# Config search order (first hit wins):
#   1. HANDQ_CONFIG env var               — explicit override (CI, portable mode)
#   2. %LOCALAPPDATA%\HandQ\handq_config.yaml — per-user override (Program Files
#                                            is read-only for unprivileged users)
#   3. <install_dir>\handq_config.yaml    — default that ships with the build
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False) or "__compiled__" in globals():
    _INSTALL_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _INSTALL_DIR = _ROOT


def _resolve_config_path() -> str:
    env_override = os.environ.get("HANDQ_CONFIG")
    if env_override:
        return os.path.abspath(env_override)
    user_dir = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    user_cfg = os.path.join(user_dir, "HandQ", "handq_config.yaml")
    if os.path.exists(user_cfg):
        return user_cfg
    return os.path.join(_INSTALL_DIR, "handq_config.yaml")


_HANDQ_CONFIG = _resolve_config_path()
os.environ["HANDQ_CONFIG"] = _HANDQ_CONFIG

# ---------------------------------------------------------------------------
# Logging bootstrap — MUST happen before any src/ import so that module-level
# `logger = logging.getLogger(...)` calls inherit the configured handlers.
#
# Invariant: NO StreamHandler may target sys.stdout. stdout is the JSON IPC
# channel. We attach exactly two handlers:
#   - StreamHandler(sys.stderr) at INFO   — human-readable diagnostics
#   - RotatingFileHandler(<log_dir>/handq-bridge.log) at DEBUG — full trace
# Root level is DEBUG so the file handler can see every record; the stderr
# handler filters to INFO+ on its own.
#
# Log directory selection:
#   - If HANDQ_LOG_DIR is set in the environment (Electron passes the
#     per-launch directory it created), use it verbatim.
#   - Otherwise (standalone python run / smoke test), generate a fresh
#     <repo>/logs/<YYYYMMDD-HHMMSS>/ directory so each invocation is isolated.
# ---------------------------------------------------------------------------
import logging  # noqa: E402
from datetime import datetime  # noqa: E402
from logging import StreamHandler  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402
from pathlib import Path  # noqa: E402

_env_log_dir = os.environ.get("HANDQ_LOG_DIR")
if _env_log_dir:
    _LOG_DIR = Path(_env_log_dir)
else:
    _LOG_DIR = Path(_ROOT) / "logs" / datetime.now().strftime("%Y%m%d-%H%M%S")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "handq-bridge.log"

_LOG_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_formatter = logging.Formatter(_LOG_FMT)

_stderr_handler = StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.INFO)
_stderr_handler.setFormatter(_formatter)

_file_handler = RotatingFileHandler(
    str(_LOG_FILE),
    maxBytes=2_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_formatter)

logging.basicConfig(
    force=True,
    level=logging.DEBUG,
    format=_LOG_FMT,
    handlers=[_stderr_handler, _file_handler],
)

_boot_logger = logging.getLogger("handq.bridge.boot")
_boot_logger.info(
    "bridge boot: python=%s executable=%s",
    sys.version.replace("\n", " "),
    sys.executable,
)
_boot_logger.info(
    "fd redirection: real_stdout_fd=%d real_stdin_fd=%d; sys.stdout->fd2; sys.stdin->/dev/null",
    _real_stdout_fd,
    _real_stdin_fd,
)
_boot_logger.info(
    "env PYTHONUTF8=%r PYTHONIOENCODING=%r",
    os.environ.get("PYTHONUTF8"),
    os.environ.get("PYTHONIOENCODING"),
)
_boot_logger.info("log file: %s", _LOG_FILE)
_boot_logger.info(
    "install_dir=%s frozen=%s config=%s",
    _INSTALL_DIR,
    bool(getattr(sys, "frozen", False) or "__compiled__" in globals()),
    _HANDQ_CONFIG,
)

import asyncio  # noqa: E402
from src.bridge import stdio_bridge  # noqa: E402

_boot_logger.info("import phase complete; src.bridge.stdio_bridge loaded")


if __name__ == "__main__":
    _boot_logger.info("bridge entrypoint: starting asyncio loop")
    try:
        asyncio.run(stdio_bridge.run())
    except (KeyboardInterrupt, SystemExit):
        _boot_logger.info("bridge interrupted by KeyboardInterrupt/SystemExit")
        raise
    except BaseException:
        _boot_logger.exception("bridge crashed before run() returned")
        raise
    finally:
        _boot_logger.info("bridge shutdown banner: asyncio.run() returned; exiting")
