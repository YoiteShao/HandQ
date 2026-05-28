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
#   1. HANDQ_CONFIG env var                  — explicit override (CI, portable mode)
#   2. %USERPROFILE%\HandQ\handq_config.yaml — per-user config; lives next to the
#                                              session History\ directory so the
#                                              user has one place for everything
#                                              they own (config + history)
#   3. <install_dir>\handq_config.yaml       — default that ships with the build,
#                                              copied to (2) on first launch
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False) or "__compiled__" in globals():
    _INSTALL_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _INSTALL_DIR = _ROOT


def _user_handq_root() -> str:
    """Return %USERPROFILE%\\HandQ (or ~/HandQ on non-Windows / when USERPROFILE
    is missing). This is the single per-user root for config + session history.
    Logs deliberately live elsewhere (%LOCALAPPDATA%\\HandQ\\logs)."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "HandQ")


def _resolve_config_path() -> str:
    env_override = os.environ.get("HANDQ_CONFIG")
    if env_override:
        return os.path.abspath(env_override)
    user_cfg = os.path.join(_user_handq_root(), "handq_config.yaml")
    if os.path.exists(user_cfg):
        return user_cfg
    return os.path.join(_INSTALL_DIR, "handq_config.yaml")


def _ensure_user_config_present() -> None:
    """First-run: copy the shipped default to %USERPROFILE%\\HandQ\\.

    Frozen-only: in dev mode the install dir IS the repo root, so a
    copy here would create a parallel user file that silently shadows
    the dev's repo-side edits (priority 2 beats priority 3 in
    _resolve_config_path). Skipping the copy in dev keeps `python
    bridge_main.py` reading the repo yaml directly. A dev who wants
    packaged-style behaviour can `set HANDQ_CONFIG=...` (priority 1)
    or hand-create the user copy.

    Best-effort: failures (perms / disk / AV lock) are swallowed —
    _resolve_config_path falls back to the install default and the
    bridge still boots; we just won't have a writable user copy yet.
    The boot logger is not yet configured at this point in the file,
    so there's nowhere useful to log a copy failure.
    """
    is_frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if not is_frozen:
        return
    user_root = _user_handq_root()
    user_cfg = os.path.join(user_root, "handq_config.yaml")
    if os.path.exists(user_cfg):
        return
    install_cfg = os.path.join(_INSTALL_DIR, "handq_config.yaml")
    if not os.path.exists(install_cfg):
        return
    try:
        os.makedirs(user_root, exist_ok=True)
        import shutil
        shutil.copy2(install_cfg, user_cfg)
    except OSError:
        pass


_ensure_user_config_present()
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
# Write back so stdio_bridge (and any other src/ module) can find the launch
# log dir without re-deriving it. Safe to overwrite: if Electron passed
# HANDQ_LOG_DIR it equals _LOG_DIR; if not, we just set it for the first time.
os.environ["HANDQ_LOG_DIR"] = str(_LOG_DIR)
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

# ---------------------------------------------------------------------------
# Internal-trace log (LTM / activity / scheduler).
#
# Production debugging needs deep visibility into the long-term memory and
# activity-monitor subsystems WITHOUT cluttering the main bridge log.
# This handler attaches to specific logger trees and writes to a separate
# file in a deliberately non-obvious location: it's NOT named with "ltm"
# or "memory" or "activity", and the directory is "diag" (not "logs"),
# so a casual user poking through %LOCALAPPDATA% won't immediately see
# what's there. We're not hiding from a determined investigator — we're
# just keeping the visible-debug-surface small.
#
# Rotation: 1 MB per file, 5 files; comfortably covers a multi-hour
# session even at DEBUG verbosity.
try:
    _diag_dir_env = os.environ.get("HANDQ_DIAG_DIR")
    if _diag_dir_env:
        _DIAG_DIR = Path(_diag_dir_env)
    else:
        _local_appdata = os.environ.get("LOCALAPPDATA") or str(Path.home())
        _DIAG_DIR = Path(_local_appdata) / "HandQ" / "diag"
    _DIAG_DIR.mkdir(parents=True, exist_ok=True)
    _DIAG_FILE = _DIAG_DIR / "internal-trace.log"
    _diag_handler = RotatingFileHandler(
        str(_DIAG_FILE),
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    _diag_handler.setLevel(logging.DEBUG)
    _diag_handler.setFormatter(_formatter)
    # Attach to the LTM / activity / scheduler logger trees only —
    # the main bridge logs stay in the main file. Logger propagation
    # means the root handler (handq-bridge.log) ALSO gets these
    # records, which is fine: the diag log is an extra copy, not a
    # diversion.
    for _name in ("handq.ltm", "handq.personality", "handq.scheduler"):
        logging.getLogger(_name).addHandler(_diag_handler)
    _boot_logger = logging.getLogger("handq.bridge.boot")
    _boot_logger.info("internal-trace log: %s", _DIAG_FILE)
except Exception:
    # If we can't write the diag log (perms / disk full / AV) just
    # carry on with the main log — diag is supplementary.
    logging.getLogger("handq.bridge.boot").exception(
        "could not initialise internal-trace log; main log only",
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
# Windows is the only supported production target. Non-Windows is allowed
# for dev / test (LTM core is portable: pure Python + SQLite + httpx),
# but the personality monitor (which wraps Win32 APIs through ctypes) will
# refuse to start on other platforms — see PersonalityMonitor.start().
if sys.platform != "win32":
    _boot_logger.warning(
        "non-Windows platform detected (%s); the bridge will boot but "
        "PersonalityMonitor will refuse to start. Production target is "
        "Windows only.", sys.platform,
    )

import asyncio  # noqa: E402
from src.bridge import stdio_bridge  # noqa: E402
from src.infrastructure.long_term_memory import LongTermMemory  # noqa: E402
from src.infrastructure.personality import PersonalityMonitor  # noqa: E402
from src.infrastructure.scheduler import Scheduler  # noqa: E402

_boot_logger.info("import phase complete; src.bridge.stdio_bridge loaded")


async def _run_with_long_term_memory() -> None:
    """Initialise LongTermMemory + PersonalityMonitor + Scheduler before the
    bridge starts and tear them down on exit. All three live for the
    lifetime of the bridge process; a clean shutdown lets WAL flush, the
    personality buffer drain, and the scheduler's JSON state reach disk.

    The init failure path is non-fatal for each subsystem: a null LTM
    instance keeps the bridge available even when the SQLite db is
    locked / corrupted; a PersonalityMonitor that fails to enumerate
    monitors simply does nothing; a scheduler with a corrupt store
    file backs it up and starts fresh. The bridge stays up so the user
    can fix or delete files under %USERPROFILE%\\HandQ\\ without
    losing core flows.
    """
    user_root = Path(_user_handq_root())
    # ── Personality data root ─────────────────────────────────────────
    # Per ARCHITECTURE.md §1.5, every "what HandQ has learned about
    # me" artifact lives under %USERPROFILE%\HandQ\personality\:
    #   memory.db, memory_notes\, ephemeral\
    # Created lazily on first boot. We are pre-release, so there is no
    # legacy layout to migrate from.
    from src.infrastructure.long_term_memory import _constants as _ltm_consts
    personality_root = user_root / _ltm_consts.PERSONALITY_DATA_DIR
    personality_root.mkdir(parents=True, exist_ok=True)
    db_path = personality_root / "memory.db"

    config_path = Path(_HANDQ_CONFIG)
    ltm = await LongTermMemory.init(db_path=db_path, config_path=config_path)

    # ── PersonalityMonitor ────────────────────────────────────────────────
    # screenshot_root = personality_root; the monitor's ScreenshotStore
    # writes to <root>\ephemeral\ (via subdir("ephemeral")) per the
    # ARCHITECTURE.md §1.5 layout. Per-frame files are unlinked the
    # moment OCR returns; this directory is therefore empty almost all
    # the time.
    personality = PersonalityMonitor(
        ltm=ltm,
        screenshot_root=str(personality_root),
        config_path=config_path,
    )
    try:
        await personality.start()
    except Exception:
        _boot_logger.exception(
            "PersonalityMonitor.start failed; continuing without personality capture",
        )

    # ── Scheduler ─────────────────────────────────────────────────────────
    sched_path = user_root / "scheduled_tasks.json"
    # The dispatch closure is bound at scheduler-start time but the
    # bridge instance only exists once StdioBridge.run() builds one.
    # We therefore use a level of indirection: the bridge registers
    # itself into a module-level slot inside stdio_bridge after
    # construction. The scheduler's dispatch reads that slot.
    async def _dispatch_via_bridge(task) -> bool:  # type: ignore[no-untyped-def]
        return await stdio_bridge.dispatch_scheduled_task(task)

    scheduler = Scheduler(store_path=sched_path, dispatch=_dispatch_via_bridge)
    try:
        await scheduler.start()
    except Exception:
        _boot_logger.exception("Scheduler.start failed; continuing without scheduler")

    # Plug both services into the bridge module so its IPC handlers
    # can call them. We assign before stdio_bridge.run() so the
    # references are visible by the time the first IPC envelope
    # arrives.
    stdio_bridge.personality_monitor = personality   # type: ignore[attr-defined]
    stdio_bridge.scheduler = scheduler         # type: ignore[attr-defined]

    # ── Sync git post-commit hooks declared in personalization.git_hook_repos
    # The user manages this list through the Settings UI (which writes
    # the yaml) — on every bridge launch we walk the list and:
    #   - install in any listed repo that doesn't already have OUR hook
    #   - skip with warning for paths that don't exist or have a
    #     non-HandQ hook
    # Hooks the user wrote themselves (no marker) are never touched.
    try:
        from src.bridge.stdio_bridge import (
            _install_post_commit_hook,
        )
        import yaml as _yaml
        try:
            with open(_HANDQ_CONFIG, "r", encoding="utf-8") as _f:
                _user_cfg = _yaml.safe_load(_f) or {}
        except Exception:
            _user_cfg = {}
        _personalization = _user_cfg.get("personalization") or {}
        _declared_repos = _personalization.get("git_hook_repos") or []
        if isinstance(_declared_repos, list):
            _declared = set(
                str(r).strip() for r in _declared_repos if str(r).strip()
            )
        else:
            _declared = set()
            _boot_logger.warning(
                "personalization.git_hook_repos is not a list; ignoring",
            )
        for _repo in _declared:
            try:
                _result = _install_post_commit_hook(_repo)
                if _result.get("ok"):
                    _boot_logger.info(
                        "git hook ensured at %s", _result.get("path"),
                    )
                else:
                    _boot_logger.warning(
                        "git hook install skipped for %s: %s",
                        _repo, _result.get("error"),
                    )
            except Exception:
                _boot_logger.exception(
                    "git hook sync failed for %s", _repo,
                )
    except Exception:
        _boot_logger.exception("git hook sync raised; continuing without sync")

    try:
        await stdio_bridge.run()
    finally:
        try:
            await scheduler.shutdown()
        except Exception:
            _boot_logger.exception("Scheduler shutdown raised")
        try:
            await personality.shutdown()
        except Exception:
            _boot_logger.exception("PersonalityMonitor shutdown raised")
        try:
            await ltm.shutdown()
        except Exception:
            _boot_logger.exception("LongTermMemory shutdown raised")


if __name__ == "__main__":
    _boot_logger.info("bridge entrypoint: starting asyncio loop")
    try:
        asyncio.run(_run_with_long_term_memory())
    except (KeyboardInterrupt, SystemExit):
        _boot_logger.info("bridge interrupted by KeyboardInterrupt/SystemExit")
        raise
    except BaseException:
        _boot_logger.exception("bridge crashed before run() returned")
        raise
    finally:
        _boot_logger.info("bridge shutdown banner: asyncio.run() returned; exiting")
