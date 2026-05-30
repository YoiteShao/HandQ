"""Top-level entry script for the Electron renderer to spawn.

Usage (from Electron main.js):
    child_process.spawn(pythonExe, ['bridge_main.py'], {stdio: ['pipe','pipe','pipe']})
"""
import io
import os
import sys
import time

# Wall-clock origin for boot-progress timing. Set as early as possible so
# every phase counter measures elapsed time from the moment the entry
# script starts running, not from logger init or import completion.
_BOOT_T0 = time.monotonic()

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

# ---------------------------------------------------------------------------
# Boot-progress emitter.
#
# A minimal, dependency-free JSON-line writer that drops envelopes onto the
# real (dup'd) stdout fd so the Electron renderer can show "Starting…"
# progress BEFORE the heavy src/* imports complete and stdio_bridge claims
# the channel.
#
# Why json by hand and not json.dumps? We want zero runtime cost and zero
# imports beyond what the interpreter has already loaded. json IS a stdlib
# module and is essentially free, so we do use it — but we deliberately
# avoid importing ANY src/* code. The whole point is that this works during
# the import phase that we are trying to measure.
#
# Wire format matches stdio_bridge's status envelopes so the renderer can
# treat boot_progress like any other status event:
#   {"type":"status","kind":"boot_progress","phase":"<name>","elapsed_ms":<int>, ...}
#
# Failure modes (broken pipe, fd already closed, encoding error, …) are
# all swallowed: boot progress is purely advisory, never a blocker.
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402  — std-lib, tiny, only used by emitter


def _boot_elapsed_ms() -> int:
    return int((time.monotonic() - _BOOT_T0) * 1000)


def _emit_boot_progress(phase: str, **fields: object) -> None:
    """Write a single JSON line to the real stdout fd.

    Safe to call before logging is configured. Never raises.
    """
    try:
        envelope = {
            "type": "status",
            "kind": "boot_progress",
            "phase": phase,
            "elapsed_ms": _boot_elapsed_ms(),
        }
        if fields:
            for k, v in fields.items():
                envelope[k] = v
        line = _json.dumps(envelope, ensure_ascii=False, default=str) + "\n"
        os.write(_real_stdout_fd, line.encode("utf-8", errors="replace"))
    except Exception:
        pass


_emit_boot_progress("fd_setup_done")

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

    Per ARCHITECTURE.md §1.5 / §2 the **Windows dev/prod data layout is
    unified** under ``%USERPROFILE%\\HandQ\\`` — that includes the config
    file. So on Windows we always run, regardless of frozen state: a dev
    install gets the same per-user config seed as a packaged build, the
    repo's ``handq_config.yaml`` is treated as a ship-default source only.

    On **Linux/macOS** there is no equivalent user-root convention: every
    runtime artifact (including config) stays under ``install_dir``, so
    we skip the copy and let ``_resolve_config_path`` use the install
    file directly.

    Best-effort: failures (perms / disk / AV lock) are swallowed —
    ``_resolve_config_path`` falls back to the install default and the
    bridge still boots; we just won't have a writable user copy yet.
    The boot logger is not yet configured at this point in the file,
    so there's nowhere useful to log a copy failure.
    """
    if sys.platform != "win32":
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
_emit_boot_progress(
    "config_resolved",
    config_path=_HANDQ_CONFIG,
    config_exists=os.path.exists(_HANDQ_CONFIG),
    install_dir=_INSTALL_DIR,
    frozen=bool(getattr(sys, "frozen", False) or "__compiled__" in globals()),
)

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

import re  # noqa: E402

_LAUNCH_TS_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


def _user_log_root() -> Path:
    """Per-user log root: %USERPROFILE%\\HandQ\\logs\\ (Windows) or ~/HandQ/logs.

    Co-located with config + History under a single user root so the user has
    one place to find every artifact HandQ writes about them. The diag tree
    lives as a hidden subdirectory below this (see _DIAG_DIR).
    """
    return Path(_user_handq_root()) / "logs"


def _prune_old_log_dirs(base: Path, keep: int = 30) -> None:
    """Keep only the *keep* most-recent launch directories under *base*.

    Each launch creates a fresh ``<YYYYMMDD-HHMMSS>/`` subdirectory under
    *base*; without pruning they accumulate forever. We delete the oldest
    once the count exceeds *keep*.

    Safety guards:
      * Only directories whose name matches ``YYYYMMDD-HHMMSS`` are eligible.
        Dot-prefixed entries (e.g. ``.dia/`` for the diag log) and any other
        files / sibling directories are left untouched.
      * Best-effort — silently swallows OSError. The boot logger is not yet
        configured here, so a failure can't be reported anywhere useful.
    """
    if not base.is_dir():
        return
    try:
        candidates = [
            p for p in base.iterdir()
            if p.is_dir() and _LAUNCH_TS_RE.match(p.name)
        ]
    except OSError:
        return
    if len(candidates) <= keep:
        return
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    import shutil
    for stale in candidates[keep:]:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except Exception:
            pass


def _set_hidden_on_windows(path: Path) -> None:
    """Apply FILE_ATTRIBUTE_HIDDEN to *path*. No-op on non-Windows.

    Dot-prefixing (``.dia``) makes a directory inconspicuous on Linux but
    Windows Explorer shows it by default. Setting the NTFS hidden attribute
    keeps it out of the user's normal browse view (still visible with "Show
    hidden files" enabled).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


_env_log_dir = os.environ.get("HANDQ_LOG_DIR")
if _env_log_dir:
    # Electron (or another launcher) already created the per-launch dir and
    # told us where it is via env. Use it verbatim.
    _LOG_DIR = Path(_env_log_dir)
    _LOG_BASE = _LOG_DIR.parent
else:
    # Per-platform default — independent of dev vs frozen so behaviour is
    # consistent across both modes:
    #   * Windows           — %USERPROFILE%\HandQ\logs\<TS>\
    #     (per ARCHITECTURE.md §1.5: every user-owned HandQ artifact lives
    #     under %USERPROFILE%\HandQ\, including dev-mode logs).
    #   * Linux / macOS     — <install_dir>/logs/<TS>\
    #     (install_dir = parent of sys.executable for frozen builds, repo
    #     root in dev). No equivalent "user root" convention on these
    #     platforms; co-locating with the bridge keeps everything self-
    #     contained. The dev-mode "logs next to source" behaviour falls
    #     out of this for free since install_dir == repo root.
    if sys.platform == "win32":
        _LOG_BASE = _user_log_root()
    else:
        _LOG_BASE = Path(_INSTALL_DIR) / "logs"
    _LOG_DIR = _LOG_BASE / datetime.now().strftime("%Y%m%d-%H%M%S")
_LOG_BASE.mkdir(parents=True, exist_ok=True)
_LOG_DIR.mkdir(parents=True, exist_ok=True)
# Write back so stdio_bridge (and any other src/ module) can find the launch
# log dir without re-deriving it. Safe to overwrite: if Electron passed
# HANDQ_LOG_DIR it equals _LOG_DIR; if not, we just set it for the first time.
os.environ["HANDQ_LOG_DIR"] = str(_LOG_DIR)
_LOG_FILE = _LOG_DIR / "handq-bridge.log"

# Trim old launch dirs. Runs once per boot, BEFORE the boot logger is
# configured (so we can't log here). 30 ≈ a few weeks of normal usage,
# enough to cross-reference past sessions; old launches roll off silently.
_prune_old_log_dirs(_LOG_BASE, keep=30)

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

_emit_boot_progress("logging_ready", log_dir=str(_LOG_DIR))

# ---------------------------------------------------------------------------
# Internal-trace log (LTM / activity / scheduler).
#
# Production debugging needs deep visibility into the long-term memory and
# activity-monitor subsystems WITHOUT cluttering the main bridge log.
# This handler attaches to specific logger trees and writes to a separate
# file at <logs_base>/.dia/ — a dot-prefixed sibling of the per-launch
# directories. We additionally set the Windows HIDDEN attribute on the
# directory so it stays out of the user's normal browse view; a curious
# user with "Show hidden files" can still find it. We're not hiding from
# a determined investigator — we're just keeping the visible-debug-surface
# small.
#
# The diag file is bounded by RotatingFileHandler (1 MB × 5) so it can't
# grow without bound. It is intentionally NOT wiped by _prune_old_log_dirs
# (which only deletes timestamped launch dirs) — diag is meant to span
# multiple launches for cross-launch correlation.
try:
    _diag_dir_env = os.environ.get("HANDQ_DIAG_DIR")
    if _diag_dir_env:
        _DIAG_DIR = Path(_diag_dir_env)
    else:
        _DIAG_DIR = _LOG_BASE / ".dia"
    _DIAG_DIR.mkdir(parents=True, exist_ok=True)
    _set_hidden_on_windows(_DIAG_DIR)
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


def _timed_import(label: str, do_import):
    """Run a single import call wrapped in wall-clock timing.

    Emits both a structured log line AND a boot_progress envelope so the
    renderer can show "Importing scheduler…" while it happens. Re-raises
    on failure so the original ImportError surfaces to the user.
    """
    _t = time.monotonic()
    _emit_boot_progress("importing", module=label)
    _boot_logger.info("importing %s …", label)
    try:
        result = do_import()
    except BaseException as exc:
        _boot_logger.exception("import failed: %s", label)
        _emit_boot_progress("import_failed", module=label, error=str(exc))
        raise
    elapsed_ms = int((time.monotonic() - _t) * 1000)
    _boot_logger.info("imported %s (took %dms)", label, elapsed_ms)
    _emit_boot_progress("imported", module=label, took_ms=elapsed_ms)
    return result


def _import_stdio_bridge():
    from src.bridge import stdio_bridge as _m
    return _m


def _import_skill_registry():
    from src.infrastructure.skills import SkillRegistry as _m
    return _m


def _import_long_term_memory():
    from src.infrastructure.long_term_memory import LongTermMemory as _m
    return _m


def _import_personality_monitor():
    from src.infrastructure.personality import PersonalityMonitor as _m
    return _m


def _import_scheduler():
    from src.infrastructure.scheduler import Scheduler as _m
    return _m


_imports_t0 = time.monotonic()
stdio_bridge = _timed_import("src.bridge.stdio_bridge", _import_stdio_bridge)
SkillRegistry = _timed_import(
    "src.infrastructure.skills", _import_skill_registry)
LongTermMemory = _timed_import(
    "src.infrastructure.long_term_memory", _import_long_term_memory)
PersonalityMonitor = _timed_import(
    "src.infrastructure.personality", _import_personality_monitor)
Scheduler = _timed_import(
    "src.infrastructure.scheduler", _import_scheduler)
_imports_total_ms = int((time.monotonic() - _imports_t0) * 1000)
_boot_logger.info("import phase complete (took %dms total)", _imports_total_ms)
_emit_boot_progress("imports_done", took_ms=_imports_total_ms)


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

    # ── Skill registry ────────────────────────────────────────────────
    # Built once at boot from %USERPROFILE%\HandQ\Skill\<name>\SKILL.md.
    # The receptionist sees the L0 menu (name + description) on every user
    # message; planner sees full bodies of activated skills. Bad files are
    # skipped with a warning — a single broken skill must not block boot.
    _emit_boot_progress("skills_init_start")
    _t_skills = time.monotonic()
    try:
        SkillRegistry.init()
    except Exception as exc:
        _boot_logger.exception(
            "SkillRegistry.init failed; continuing with empty registry"
        )
        _emit_boot_progress("skills_init_failed", error=str(exc))
    else:
        _skills_ms = int((time.monotonic() - _t_skills) * 1000)
        _boot_logger.info("SkillRegistry.init took %dms", _skills_ms)
        _emit_boot_progress("skills_init_done", took_ms=_skills_ms)

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
    _emit_boot_progress("ltm_init_start", db_path=str(db_path))
    _t_ltm = time.monotonic()
    try:
        ltm = await LongTermMemory.init(db_path=db_path, config_path=config_path)
    except BaseException as exc:
        _emit_boot_progress("ltm_init_failed", error=str(exc))
        raise
    _ltm_ms = int((time.monotonic() - _t_ltm) * 1000)
    _boot_logger.info("LongTermMemory.init took %dms (db=%s)",
                      _ltm_ms, db_path)
    _emit_boot_progress("ltm_init_done", took_ms=_ltm_ms)

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
    _emit_boot_progress("personality_start_start")
    _t_pers = time.monotonic()
    try:
        await personality.start()
    except Exception:
        _boot_logger.exception(
            "PersonalityMonitor.start failed; continuing without personality capture",
        )
        _emit_boot_progress("personality_start_failed")
    else:
        _pers_ms = int((time.monotonic() - _t_pers) * 1000)
        _boot_logger.info("PersonalityMonitor.start took %dms", _pers_ms)
        _emit_boot_progress("personality_start_done", took_ms=_pers_ms)

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
    _emit_boot_progress("scheduler_start_start", store_path=str(sched_path))
    _t_sched = time.monotonic()
    try:
        await scheduler.start()
    except Exception:
        _boot_logger.exception("Scheduler.start failed; continuing without scheduler")
        _emit_boot_progress("scheduler_start_failed")
    else:
        _sched_ms = int((time.monotonic() - _t_sched) * 1000)
        _boot_logger.info("Scheduler.start took %dms", _sched_ms)
        _emit_boot_progress("scheduler_start_done", took_ms=_sched_ms)

    # Plug both services into the bridge module so its IPC handlers
    # can call them. We assign before stdio_bridge.run() so the
    # references are visible by the time the first IPC envelope
    # arrives.
    stdio_bridge.personality_monitor = personality   # type: ignore[attr-defined]
    stdio_bridge.scheduler = scheduler         # type: ignore[attr-defined]

    # Note: git post-commit hooks declared in personalization.git_hook_repos
    # are NOT installed at boot. The Settings UI is the single source of
    # truth — the bridge installs/uninstalls hooks on `config_set` (see
    # StdioBridge._handle for the diff-driven sync). Booting without
    # touching .git/hooks/ keeps the launch path side-effect-free.

    try:
        _boot_logger.info(
            "all subsystems initialised; entering stdio loop "
            "(total boot %dms)", _boot_elapsed_ms(),
        )
        _emit_boot_progress("stdio_loop_ready")
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
