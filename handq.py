#!/usr/bin/env python3
"""
handq.py — HandQ Entry Point

States:
  state0: handq not started
  state1: handq started, no task
  state2: handq started, task running
  state3: handq started, task completed

Commands:
  handq                  - start (state0→state1) or show dialog (state1/2/3)
  handq <goal text...>   - submit goal directly (starts runtime if needed)
  handq --new            - stop current session, create workspace, enter state1
  handq --exit           - exit to state0, kill tmux session
  handq --config PATH    - start with specified config
"""
from __future__ import annotations

__version__ = "1.1.1"

import argparse
import asyncio
import atexit
import itertools
import json
import os

import shlex
import signal
import subprocess
import sys
import threading
import time
import posixpath as _pp
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.markdown import Markdown
# Reliable Nuitka compilation detection.
# "__compiled__" is a compile-time constant in Nuitka — it may NOT appear in
# globals() at runtime (Nuitka optimises it away), so "in globals()" is
# unreliable.  Using try/except on the name itself always works.
try:
    _IS_COMPILED: bool = bool(__compiled__)   # type: ignore[name-defined]
except NameError:
    _IS_COMPILED: bool = False

# In Nuitka, sys.executable may be set to a Python symlink path inside
# handq.dist/ (e.g. handq.dist/python3) that does NOT exist on the target
# machine.  sys.argv[0] is always the actual binary path that was invoked.
# Use _SELF_BINARY when spawning child processes in compiled mode.
_SELF_BINARY: str = str(Path(sys.argv[0]).resolve()) if _IS_COMPILED else ""

# In Nuitka standalone mode the binary lives at <dist>/handq.dist/handq.bin.
# _HERE is the *dist root* (<dist>/), one level above handq.dist/, so that
# state files, config, and setup.sh all share the same directory as seen by
# both the binary and handq_setup.sh (which sets SCRIPT_DIR to <dist>/).
if _IS_COMPILED:
    # Use _SELF_BINARY (sys.argv[0] resolved = <dist>/handq.dist/handq.bin) rather
    # than sys.executable, which Nuitka sets to a bundled Python symlink whose
    # path depends on Nuitka internals and may not exist on target machines.
    _HERE = Path(_SELF_BINARY).parent.parent  # <dist>/handq.dist/../ = <dist>/
else:
    _HERE = Path(__file__).parent.resolve()
import socket as _socket
_HANDQ_HOST = _socket.gethostname().split(".")[0]  # short hostname (no domain)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDQ_DIR                  = _HERE / ".handq" / f"{os.environ.get('USER', 'default')}@{_HANDQ_HOST}"
STATE_FILE                 = HANDQ_DIR / "state.json"
PID_FILE                   = HANDQ_DIR / "handq.pid"
MESSAGES_DIR               = HANDQ_DIR / "messages"   # queue directory for IPC
CONFIRMATION_REQUEST_FILE  = HANDQ_DIR / "confirmation_request.json"
CONFIRMATION_RESPONSE_FILE = HANDQ_DIR / "confirmation_response.txt"
SHELL_CONTEXT_FILE         = HANDQ_DIR / "shell_context.txt"
TERM_LOG_FILE              = HANDQ_DIR / "term.log"
CAPTURE_START_FILE         = HANDQ_DIR / "capture_start"
CAPTURE_STOP_FILE          = HANDQ_DIR / "capture_stop"
SAVE_REQUEST_FILE          = HANDQ_DIR / "save_requested"  # sentinel: foreground → background
DEFAULT_CONFIG             = _HERE / ("handq_config.yaml" if _IS_COMPILED else "./handq_config.yaml")
_CALLER_CWD                = os.getcwd()
HANDQ_TMUX_SESSION         = os.environ.get("HANDQ_TMUX_SESSION", f"handq-{os.environ.get('USER', 'default')}@{_HANDQ_HOST}")
# Use a per-user socket so HandQ gets its own tmux server (avoids inheriting
# another user's .tmux.conf when the default server was started by someone else).
_TMUX_USER = os.environ.get("USER", "default")
HANDQ_TMUX_SOCKET          = os.environ.get("HANDQ_TMUX_SOCKET", f"handq-{_TMUX_USER}@{_HANDQ_HOST}")
# Dedicated tmux config file — written by handq_setup.sh, loaded via -f so
# HandQ's tmux settings never touch the user's ~/.tmux.conf.
HANDQ_TMUX_CONF            = HANDQ_DIR / "tmux.conf"
# Prefix args to pass to every tmux invocation to select the dedicated socket
# and config file.  -f must come before the subcommand.
_TMUX_SOCK_ARGS: list[str]  = ["-L", HANDQ_TMUX_SOCKET, "-f", str(HANDQ_TMUX_CONF)]
# Lock file written by the foreground before Popen and removed by the child
# after it writes PID_FILE.  Prevents a second foreground call from spawning
# a duplicate child during the startup window.
_SPAWN_LOCK_FILE            = HANDQ_DIR / "spawning.lock"

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _ensure_handq_dir() -> None:
    HANDQ_DIR.mkdir(parents=True, exist_ok=True)


def _read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _ensure_handq_dir()
    except Exception as _e:
        # Write permission error — log to stderr so it's visible in tests
        import traceback as _tb
        print(f"HandQ: cannot create state dir {HANDQ_DIR}: {_e}", file=sys.stderr)
        return
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(STATE_FILE)
    except Exception as _e:
        print(f"HandQ: cannot write state {STATE_FILE}: {_e}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass



def _get_handq_state() -> int:
    """
    Derive the current handq state (0-4) from state.json.

    state0: no state file, or handq_active is False/absent
    state1: handq_active=True, no task_status (or task_status absent)
    state2: handq_active=True, task_status="running"
    state3: handq_active=True, task_status="completed"
    state4: state2 + confirmation_request.json present (waiting for confirmation)
    """
    state = _read_state()
    if not state or not state.get("handq_active", False):
        return 0
    task_status = state.get("task_status")
    if task_status == "running":
        if CONFIRMATION_REQUEST_FILE.exists():
            return 4
        return 2
    if task_status == "completed":
        return 3
    return 1


def _set_handq_active(active: bool, **extra) -> None:
    """Set handq_active flag and optional extra fields in state.json."""
    state = _read_state()
    state["handq_active"] = active
    state.update(extra)
    _write_state(state)


# ---------------------------------------------------------------------------
# Terminal title helpers
# ---------------------------------------------------------------------------

def _get_tty_path() -> Optional[str]:
    """
    Return the path to the current controlling terminal device (Unix only).
    Returns None on Windows or when no tty is available.
    """
    if not hasattr(os, "ttyname"):
        return None
    for fd in (sys.stdout.fileno(), sys.stdin.fileno(), sys.stderr.fileno()):
        try:
            return os.ttyname(fd)  # type: ignore[attr-defined]
        except Exception:
            continue
    return None




# ---------------------------------------------------------------------------
# tmux status push — event-driven state display
# ---------------------------------------------------------------------------

_PROMPT_TITLES = {
    0: "",
    1: "[HandQ]",
    2: "[HandQ Running]",
    3: "[HandQ Complete]",
    4: "[HandQ Confirm?]",  # state2 with pending confirmation
}

# Path to the helper script written to <handq_dir>/tmux_status.py
# tmux status-left calls: python3 <handq_dir>/tmux_status.py
_TMUX_STATUS_HELPER_PATH = HANDQ_DIR / "tmux_status.py"

# Template — {handq_dir} is substituted at write time with the actual path.
_TMUX_STATUS_HELPER_TMPL = '''\
import json, os, time
p = {handq_dir!r} + "/state.json"
try:
    d = json.load(open(p))
except Exception:
    raise SystemExit(0)
if not d.get("handq_active"):
    raise SystemExit(0)
ts   = d.get("task_status", "")
conf = os.path.exists({handq_dir!r} + "/confirmation_request.json")
sp   = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
c    = sp[int(time.time() * 4) % 10]
if conf:
    print("#[fg=colour1,bold]" + c + " [HandQ Confirm?]#[default]", end="")
elif ts == "running":
    detail = ""
    si = d.get("status_icon", "")
    st = d.get("status_text", "")
    if si and st:
        detail = " #[fg=colour8,nobold]" + si + " " + st[:80] + "#[default]"
    hist = d.get("confidence_history", [])
    spark = ""
    if hist:
        chars = "▁▂▃▄▅▆▇█"
        spark = "".join(chars[min(7, int(v * 8))] for v in hist[-8:])
        latest = hist[-1]
        spark = " #[fg=colour6]" + spark + " " + ("%.2f" % latest) + "#[default]"
    print("#[fg=colour3,bold]" + c + " [HandQ Running]#[default]" + detail + spark, end="")
elif ts == "completed":
    print("#[fg=colour6,bold]\\u2713 [HandQ Complete]#[default]", end="")
else:
    detail = ""
    si = d.get("status_icon", "")
    st = d.get("status_text", "")
    if si and st:
        detail = " #[fg=colour8,nobold]" + si + " " + st[:80] + "#[default]"
    print("#[fg=colour2,bold]\\u00b7 [HandQ]#[default]" + detail, end="")
'''


def _ensure_tmux_status_helper() -> None:
    """Write <handq_dir>/tmux_status.py if missing or outdated."""
    _ensure_handq_dir()
    src = _TMUX_STATUS_HELPER_TMPL.format(handq_dir=str(HANDQ_DIR))
    try:
        if (
            not _TMUX_STATUS_HELPER_PATH.exists()
            or _TMUX_STATUS_HELPER_PATH.read_text(encoding="utf-8") != src
        ):
            _TMUX_STATUS_HELPER_PATH.write_text(src, encoding="utf-8")
    except Exception:
        pass


def _write_default_tmux_conf() -> None:
    """Create HANDQ_TMUX_CONF for this host if handq_setup.sh hasn't been run here.

    Equivalent to configure_tmux() in handq_setup.sh.  Called automatically
    when a new host accesses a shared HandQ installation (e.g. via NFS home),
    so the user doesn't have to re-run handq_setup.sh on every machine.
    """
    _ensure_handq_dir()
    _ensure_tmux_status_helper()

    # tmux_status.py is a Python script — always needs a real interpreter,
    # even in compiled-binary mode where sys.executable is the binary.
    python_exe = _resolve_python(None) if _IS_COMPILED else sys.executable

    status_left = f"#({python_exe} {_TMUX_STATUS_HELPER_PATH}) #[default] ◈ #S "
    conf = (
        "# HandQ isolated tmux config — auto-generated by HandQ\n"
        "# This file is loaded via -f and does NOT affect ~/.tmux.conf\n"
        "\n"
        f'set -g status-left "{status_left}"\n'
        "set -g status-left-length 60\n"
        'set -g status-right "#[dim]Alt+\\u2191\\u2193 scroll  #[default]%H:%M %d-%b"\n'
        "set -g status-interval 1\n"
        "set -g status-style bg=default\n"
        "\n"
        "# Alt+Up/Down: scroll without stealing focus\n"
        "bind-key -n M-Up copy-mode \\; send-keys -X scroll-up\n"
        "bind-key -n M-Down send-keys -X scroll-down\n"
        "\n"
        "# Remove PS1/PS2 from tmux environment (prevents zsh startup errors)\n"
        "set-environment -gu PS1\n"
        "set-environment -gu PS2\n"
        "set-environment -gu PS3\n"
        "set-environment -gu PS4\n"
    )
    try:
        HANDQ_TMUX_CONF.write_text(conf, encoding="utf-8")
    except Exception as e:
        print(f"HandQ: warning — could not write tmux config {HANDQ_TMUX_CONF}: {e}",
              file=sys.stderr)


def _push_tmux_status(state: int) -> None:
    """
    Configure tmux status-left to call ~/.handq/tmux_status.py every second.

    The helper script reads state.json and renders a braille spinner for
    active states.  status-interval 1 drives the animation.

    Uses -t <session> (per-session) instead of -g (global) so that multiple
    users sharing the same tmux server do not overwrite each other's status bar.

    Works from both foreground and background processes — does not require
    TMUX env var; targets the HandQ session by name directly.
    No-op when tmux is not available or the HandQ session does not exist.
    """
    _ensure_tmux_status_helper()
    # tmux_status.py is a plain script invoked by tmux (which may run after the
    # binary exits).  Use the real system python3 instead of sys.executable,
    # which in Nuitka onefile mode points to the temp extraction directory that
    # is cleaned up when the binary exits.
    import shutil as _shutil
    python_exe = (_shutil.which("python3") or sys.executable) if _IS_COMPILED else sys.executable
    status_left = f"#({python_exe} {_TMUX_STATUS_HELPER_PATH}) #[default] ◈ #S "
    target = ["-t", HANDQ_TMUX_SESSION]
    try:
        subprocess.Popen(
            ["tmux", *_TMUX_SOCK_ARGS, "set-option", *target, "status-left", status_left],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.Popen(
            ["tmux", *_TMUX_SOCK_ARGS, "set-option", *target, "status-left-length", "160"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.Popen(
            ["tmux", *_TMUX_SOCK_ARGS, "set-option", *target, "status-interval", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.Popen(
            ["tmux", *_TMUX_SOCK_ARGS, "refresh-client", "-t", HANDQ_TMUX_SESSION, "-S"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _ensure_handq_tmux_session() -> bool:
    """
    Ensure the user is inside the HandQ tmux session.

    Called from the foreground interactive/inline-goal paths only (not from
    subcommands like --exit, --new so those still work outside tmux).

    Behaviour:
      - Windows or tmux not installed: warn once and return False (graceful
        degradation — HandQ continues in the current terminal).
      - Already inside tmux ($TMUX set): no-op, return True.
      - Session 'handq' exists: os.execvp("tmux attach-session") — replaces
        the current process so no zombie parent is left.
      - Session does not exist: os.execvp("tmux new-session") — creates the
        session and attaches; the user's $SHELL becomes window 0.

    Returns True if already in tmux (no exec needed), False on degradation.
    After os.execvp the function never returns.
    """
    try:
        subprocess.run(
            ["tmux", "-V"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=2.0,
        )
    except Exception:
        print("HandQ: tmux not found — running without tmux session.", file=sys.stderr)
        return False

    # Auto-create tmux config for this host if missing (multi-host NFS scenario:
    # handq_setup.sh was run on another machine; new host gets config on first run).
    if not HANDQ_TMUX_CONF.exists():
        _write_default_tmux_conf()

    # Already inside a tmux session — nothing to do
    if os.environ.get("TMUX"):
        return True

    # Check whether the HandQ session already exists
    result = subprocess.run(
        ["tmux", *_TMUX_SOCK_ARGS, "has-session", "-t", HANDQ_TMUX_SESSION],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        # Session exists — attach (replaces this process)
        os.execvp("tmux", ["tmux", *_TMUX_SOCK_ARGS, "attach-session", "-t", HANDQ_TMUX_SESSION])
    else:
        # Create a new session detached, install cleanup hook, then attach.
        # The hook fires when the last window in the session closes (user types
        # 'exit' or closes the terminal), ensuring HandQ state and the session
        # itself are always cleaned up even without an explicit 'handq --exit'.
        # Compiled binary: spawn itself directly; source mode: python + handq.py
        if _IS_COMPILED:
            handq_cmd = shlex.join([_SELF_BINARY, "--exit"])
        else:
            handq_cmd = shlex.join([sys.executable, str(Path(__file__).resolve()), "--exit"])
        subprocess.run(
            ["tmux", *_TMUX_SOCK_ARGS, "new-session", "-d", "-s", HANDQ_TMUX_SESSION],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # session-closed must be a global hook (-g), not per-session (-t).
        # Per-session hooks are destroyed with the session before they can fire.
        subprocess.run(
            ["tmux", *_TMUX_SOCK_ARGS, "set-hook", "-g",
             "session-closed", handq_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.execvp("tmux", ["tmux", *_TMUX_SOCK_ARGS, "attach-session", "-t", HANDQ_TMUX_SESSION])
    return True  # unreachable after execvp




# ---------------------------------------------------------------------------
# Dialog with concurrent spinner animation
# ---------------------------------------------------------------------------

_SPINNER_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_ANSI_ESC = __import__("re").compile(r'\x1b\[[0-9;]*[A-Za-z]|\x1b[()][AB012]')


def _decode_and_clean(raw_bytes: bytes) -> "Optional[str]":
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            raw = raw_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raw = raw_bytes.decode("utf-8", errors="replace")
    # Strip ANSI escape sequences (arrow keys, colour codes, etc.)
    raw = _ANSI_ESC.sub("", raw)
    # Collapse embedded newlines (multiline paste) into spaces
    raw = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return raw.strip() or None


def _readline_input(prompt: str, tty_path: Optional[str] = None) -> Optional[str]:
    """
    Read input with GNU readline (arrow keys, line editing) and bracketed paste
    mode (multi-line paste collected as single message, newlines preserved).
    Returns stripped text or None on cancel (Ctrl-C / Ctrl-D / empty).
    """
    try:
        import readline as _rl  # noqa: F401 — activates readline for input()
    except ImportError:
        pass

    BPASTE_ON  = "\x1b[?2004h"
    BPASTE_OFF = "\x1b[?2004l"
    BP_BEGIN   = "\x1b[200~"
    BP_END     = "\x1b[201~"

    # When a tty_path is given (background process whose fd0/1 are /dev/null),
    # use dup2 to redirect fd 0 and fd 1 to the tty.  This keeps readline
    # operating on fd 0, so arrow-key editing works correctly.  Simply
    # replacing sys.stdin/stdout objects does NOT work because readline hooks
    # the underlying C-level fd 0 at import time.
    _saved_fd0 = _saved_fd1 = _tty_fd = None
    if tty_path:
        try:
            import os as _os
            _tty_fd   = _os.open(tty_path, _os.O_RDWR)
            _saved_fd0 = _os.dup(0)
            _saved_fd1 = _os.dup(1)
            _os.dup2(_tty_fd, 0)
            _os.dup2(_tty_fd, 1)
            # Keep sys.stdin/stdout in sync so Python's text layer agrees.
            sys.stdin  = open(0, "r", encoding="utf-8", errors="replace", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace", closefd=False)
        except Exception:
            # Fall back: close any partial fds and leave fd 0/1 untouched.
            import os as _os
            for _fd in (_tty_fd, _saved_fd0, _saved_fd1):
                if _fd is not None:
                    try: _os.close(_fd)
                    except Exception: pass
            _saved_fd0 = _saved_fd1 = _tty_fd = None

    try:
        sys.stdout.write(BPASTE_ON)
        sys.stdout.flush()
        try:
            raw = input(prompt)
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            try:
                sys.stdout.write(BPASTE_OFF)
                sys.stdout.flush()
            except Exception:
                pass

        # Bracketed paste: terminal may include markers in the returned string
        # (happens when readline doesn't strip them). Extract pasted content.
        if BP_BEGIN in raw:
            start = raw.index(BP_BEGIN) + len(BP_BEGIN)
            end   = raw.index(BP_END) if BP_END in raw else len(raw)
            raw   = raw[start:end]

        # Normalize line endings, preserve newlines (multi-line paste = one message)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        return raw.strip() or None

    finally:
        if _saved_fd0 is not None:
            import os as _os
            try: sys.stdin.close()
            except Exception: pass
            try: sys.stdout.close()
            except Exception: pass
            try: _os.dup2(_saved_fd0, 0)
            except Exception: pass
            try: _os.dup2(_saved_fd1, 1)
            except Exception: pass
            try: _os.close(_saved_fd0)
            except Exception: pass
            try: _os.close(_saved_fd1)
            except Exception: pass
            try: _os.close(_tty_fd)
            except Exception: pass
            sys.stdin  = sys.__stdin__
            sys.stdout = sys.__stdout__


def _show_dialog(
    state_label: str,
    prompt_text: str,
    header_lines: Optional[List[str]] = None,
    tty_path: Optional[str] = None,
) -> Optional[str]:
    """
    Show a terminal dialog with a concurrent spinner animation.

    Layout (printed to stdout):
      [header_lines]
      ⠋ <state_label>          ← spinner thread updates this line in-place
        <prompt_text>_          ← user types here

    The spinner runs in a background thread and uses ANSI cursor-save /
    cursor-restore escape sequences (\\0337 / \\0338) to update the spinner
    line without disturbing the input cursor.

    The main thread reads input via sys.stdin.readline() so that the
    spinner thread can write to stdout concurrently without conflict.

    If tty_path is provided, I/O is redirected to that tty device
    (for background processes whose stdin/stdout are /dev/null).

    Returns the stripped input string, or None if the user cancelled
    (Ctrl-C / Ctrl-D / empty Enter).
    """
    stop_event = threading.Event()
    write_lock = threading.Lock()

    try:
        out = open(tty_path, "w") if tty_path else sys.stdout
        inp = open(tty_path, "r") if tty_path else None  # opened below if needed
    except Exception:
        out = sys.stdout
        inp = None

    def _write(s: str, flush: bool = False) -> None:
        with write_lock:
            try:
                out.write(s)
                if flush:
                    out.flush()
            except Exception:
                pass

    if header_lines:
        for line in header_lines:
            _write(line + "\n", flush=True)

    # Print spinner line only (prompt will be printed by _readline_input via input()).
    # Spinner line is above; input prompt is below.
    _write(f"  \u283b {state_label}\n", flush=True)

    def _spinner() -> None:
        for char in itertools.cycle(_SPINNER_CHARS):
            if stop_event.is_set():
                break
            # Move up one line, rewrite spinner char, then move back down.
            # Do NOT rewrite the prompt — that would overwrite user's typed input.
            # Save cursor, go up, rewrite spinner, restore cursor (back to input position).
            _write(f"\0337\033[1A\r  {char} {state_label}  \0338", flush=True)
            time.sleep(0.1)
        # Cleanup is handled by the finally block in the caller

    spinner_thread = threading.Thread(
        target=_spinner, daemon=True, name="handq-spinner"
    )
    spinner_thread.start()

    try:
        # Stop spinner before readline takes over cursor management.
        # The spinner uses cursor-save/restore which conflicts with readline.
        stop_event.set()
        spinner_thread.join(timeout=0.3)
        # Clear spinner line (move up, erase line, move back down)
        with write_lock:
            try:
                out.write("\033[1A\r\033[2K\033[1B\r")
                out.flush()
            except Exception:
                pass

        return _readline_input(f"  {prompt_text}", tty_path=tty_path)
    except (KeyboardInterrupt, EOFError):
        return None
    finally:
        # Restore terminal to sane state (fixes any stray raw-mode or attribute leaks)
        try:
            subprocess.run(["stty", "sane"], timeout=1.0,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if tty_path:
            try:
                out.close()
            except Exception:
                pass
            if inp is not None:
                try:
                    inp.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
HandQ — AI Task Execution Agent

USAGE
  handq [OPTIONS] [goal text...]

OPTIONS
  --help        Print this help text and exit
  --exit        Exit HandQ and kill the tmux session (-> state0)
  --new         Stop current session, create a new workspace, enter state1
  --show-config Print the current config file and exit
  -c, --config PATH  Use specified config YAML file
  --save [PATH] Save last completed task as a GEP template.
                Without PATH: requires a completed task in the current session.
                With PATH: generate a template from any session log file,
                e.g.  handq --save ~/.handq/sessions/20250506_task.log
  --list        List all available GEP templates

INLINE GOAL (no dialog)
  handq 测试当前路径
  handq list all files in /tmp

INTERACTIVE MODE (no subcommand)
  state0 (not started)   -- Starts HandQ runtime, enters tmux session.
  state1 (no task)       -- Shows a dialog to enter a new goal.
  state2 (task running)  -- Shows a dialog to send a message to the running task.
  state3 (task done)     -- Shows a dialog to start a new task or follow-up.

TMUX STATUS BAR
  HandQ state is shown in the tmux status bar automatically.
  The status bar updates instantly when the state changes (event-driven).
  Run 'handq_setup.sh' once to configure the tmux status bar.
"""


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_help() -> int:
    print(_HELP_TEXT, end="")
    return 0


def cmd_save(save_path: Optional[str] = None) -> int:
    """
    Request the background child to generate a GEP template from the last
    completed task's execution log.

    When ``save_path`` is provided, the template is generated from that
    session log instead of the current session (the background process must
    still be running, but no completed task is required).

    Without a path, a completed task in the current session (state 3) is
    required.  States 0/1/2 (not started, idle, running) are rejected with
    an informative message.
    """
    hs = _get_handq_state()
    if hs == 0:
        print("HandQ: not running — start a session first.", file=sys.stderr)
        return 1
    if save_path is None and hs == 1:
        print("HandQ: no completed task yet — run a task first.", file=sys.stderr)
        return 1
    if hs in (2, 4):
        print(
            "HandQ: task still in progress — wait for completion before saving.",
            file=sys.stderr,
        )
        return 1
    # state 3 (completed) — but first verify the background process is alive.
    # _get_handq_state() reads state.json which is NOT cleared on process exit
    # (only the PID file is removed).  If the process has since exited, the
    # sentinel file would be written but never picked up.
    if _get_running_child_pid() is None:
        print(
            "HandQ: background process has exited — run 'handq' to start a new session.",
            file=sys.stderr,
        )
        return 1
    if save_path is not None:
        p = Path(save_path)
        if not p.exists():
            print(f"HandQ: session log not found: {save_path}", file=sys.stderr)
            return 1
        sentinel_content = str(p.resolve())
    else:
        sentinel_content = "save"
    _ensure_handq_dir()
    try:
        SAVE_REQUEST_FILE.write_text(sentinel_content, encoding="utf-8")
        print("HandQ: save requested — generating GEP template…")
        return 0
    except Exception as exc:
        print(f"HandQ: failed to write save request: {exc}", file=sys.stderr)
        return 1


def cmd_list_templates(config_path: Optional[str] = None) -> int:
    """List available GEP templates directly from disk (no LLM, no background process)."""
    try:
        from src.infrastructure.gep_template import list_templates as _list_templates
        templates = _list_templates()
    except Exception as exc:
        print(f"HandQ: error reading templates: {exc}", file=sys.stderr)
        return 1

    if not templates:
        print(
            "No GEP templates found yet.\n"
            "Run 'handq --save' after completing a task to create one."
        )
        return 0

    print(f"\nAvailable GEP templates ({len(templates)}):\n")
    for t in templates:
        print(f"  [{t.name}]  v{t.version}")
        print(f"    {t.description}")
        print()
    return 0


def cmd_config(config_path: Optional[str] = None) -> int:
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    print("HandQ Configuration")
    print(f"  Config file: {cfg_path}\n")
    try:
        print(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"  [Config file not found: {cfg_path}]")
        return 1
    except Exception as exc:
        print(f"  [Error reading config: {exc}]")
        return 1
    return 0


def cmd_models(config_path: Optional[str] = None) -> int:
    """Print the model-to-role assignment derived from the current config."""
    import yaml
    from src.infrastructure.role_resolver import resolve_role_models, used_legacy_models
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        llm_cfg = cfg.get("llm", {}) or {}
    except FileNotFoundError:
        print(f"HandQ: config file not found: {cfg_path}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"HandQ: error reading config: {exc}", file=sys.stderr)
        return 1

    legacy = used_legacy_models(llm_cfg)
    roles = resolve_role_models(llm_cfg)

    if not any(roles.values()):
        print("HandQ: no models configured (set llm.roles in handq_config.yaml).",
              file=sys.stderr)
        return 1

    def _fmt(models: list) -> str:
        if not models:
            return "    (empty)"
        return "\n".join(f"    [{i}] {m}" for i, m in enumerate(models))

    print("\nHandQ Model Assignment\n")
    if legacy:
        print("  (derived from legacy llm.models — will be migrated to "
              "llm.roles on next Save in the Settings UI)\n")
    print(f"  Agent  ({len(roles['agent'])} models, max_retries=3)")
    print(_fmt(roles["agent"]))
    print(f"\n  Planner  ({len(roles['planner'])} models, max_retries=50, dedicated instances)")
    print(_fmt(roles["planner"]))
    print(f"\n  Receptionist  ({len(roles['receptionist'])} models, max_retries=3)")
    print(_fmt(roles["receptionist"]))
    print(f"\n  Helper  ({len(roles['from_data'])} models, max_retries=3)  [internal: from_data / error_explain]")
    print(_fmt(roles["from_data"]))
    print()
    return 0


def _wait_for_pid(pid: int, timeout: float = 2.0) -> None:
    """
    Wait up to *timeout* seconds for *pid* to exit.

    Sends SIGTERM first, waits, then escalates to SIGKILL and waits again
    until the process is confirmed gone.  Returns only when the process no
    longer exists (or was never alive).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return  # process is gone
        time.sleep(0.05)
    # Still alive after SIGTERM timeout — force kill
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return  # already gone
    except Exception:
        pass
    # Wait for SIGKILL to take effect (kernel needs a scheduler tick)
    kill_deadline = time.time() + 2.0
    while time.time() < kill_deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)


def _read_pid_file() -> Optional[int]:
    """
    Read PID_FILE and return the integer PID, or None on any error.

    Handles empty files, non-integer content, and missing files safely.
    """
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return int(text)
    except Exception:
        return None


def _idle_mode_pgrep_pattern() -> str:
    """Return a pgrep -f pattern scoped to this installation's idle child.

    Three-layer filter — all must appear in the child's command line:
      1. Installation path  (sys.executable or _HERE/handq.py) — unique per installation
      2. --_idle_mode       — only background child processes
      3. user@host tag      — extra guard; pgrep -u already filters by uid,
                              this adds hostname so ps output is self-documenting

    Compiled binary child cmdline:
      <_HERE>/handq.dist/handq.bin --_idle_mode --_handq_instance user@host ...
    Python child cmdline:
      <python> <_HERE>/handq.py --_idle_mode --_handq_instance user@host ...
    """
    import re
    marker = re.escape(_SELF_BINARY if _IS_COMPILED else str(_HERE / "handq.py"))
    instance = re.escape(f"{os.environ.get('USER', 'default')}@{_HANDQ_HOST}")
    return f"{marker}.*_idle_mode.*{instance}"


def _kill_all_handq_processes() -> None:
    """
    Kill the background child process and wipe all HandQ state/IPC files.

    Safe to call when no processes are running (all operations are
    idempotent).  Guarantees that when this function returns:
      • The background child is dead (SIGTERM → wait → SIGKILL → wait).
      • PID_FILE, state.json, and all IPC files are removed.

    Kills both the PID recorded in PID_FILE and any stray --_idle_mode
    processes belonging to the current user (handles the case where a
    duplicate was spawned before the spawn-lock fix was in place).
    """
    # ── Collect all PIDs to kill ──────────────────────────────────────────
    pids_to_kill: set = set()

    # 1. PID from PID_FILE (primary)
    child_pid: Optional[int] = _read_pid_file()
    if child_pid is not None:
        pids_to_kill.add(child_pid)

    # 2. Any stray --_idle_mode processes owned by this user (safety net)
    #    Pattern is scoped to this installation's path so we don't touch
    #    idle children spawned by a different HandQ installation on this host.
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", _idle_mode_pgrep_pattern()],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        for line in result.stdout.decode().splitlines():
            try:
                pids_to_kill.add(int(line.strip()))
            except ValueError:
                pass
    except Exception:
            pass

    # ── Read and immediately remove PID_FILE (atomic claim) ──────────────
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    # ── Send SIGTERM to all collected PIDs ────────────────────────────────
    alive_pids: set = set()
    for pid in pids_to_kill:
        try:
            os.kill(pid, signal.SIGTERM)
            alive_pids.add(pid)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
        except Exception:
            pass

    for pid in alive_pids:
        _wait_for_pid(pid, timeout=5.0)

    # ── Wipe state so next startup is completely fresh ────────────────────
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    # ── Clear all IPC files ───────────────────────────────────────────────
    for f in (CONFIRMATION_REQUEST_FILE, CONFIRMATION_RESPONSE_FILE, _SPAWN_LOCK_FILE):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        if MESSAGES_DIR.exists():
            for msg_file in list(MESSAGES_DIR.iterdir()):
                try:
                    msg_file.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass


def cmd_exit() -> int:
    """
    Exit HandQ: kill all related processes, wipe state, kill tmux session (state0).

    Human-readable messages go to stderr so they are always visible.
    Always performs a full cleanup regardless of reported state, so stale
    PID files and IPC files are never left behind.
    """
    hs = _get_handq_state()

    # Full cleanup unconditionally — handles stale files even in state0.
    _kill_all_handq_processes()
    _push_tmux_status(0)

    # Kill the HandQ tmux session (if it exists)
    try:
        subprocess.run(
            ["tmux", *_TMUX_SOCK_ARGS, "kill-session", "-t", HANDQ_TMUX_SESSION],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except Exception:
        pass

    if hs == 0:
        print("HandQ: not running (cleaned up stale files).", file=sys.stderr)
    else:
        print("HandQ: exited.", file=sys.stderr)
    return 0


def _wait_for_no_idle_processes(timeout: float = 3.0) -> None:
    """
    Block until no --_idle_mode processes owned by this user are running.

    Called by cmd_new after _kill_all_handq_processes() to close the race
    window where a previously-spawning child has been Popen'd but hasn't
    yet written PID_FILE (so _kill_all_handq_processes couldn't find it).
    Without this wait, a second cmd_new call could spawn a duplicate child
    that races with the first one through _register_singleton().

    On timeout, any surviving stragglers are force-killed with SIGKILL.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-f", _idle_mode_pgrep_pattern()],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:  # pgrep exits 1 when no match found
                return
        except Exception:
            return
        time.sleep(0.05)
    # Timeout — force-kill any surviving stragglers
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", _idle_mode_pgrep_pattern()],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        for line in result.stdout.decode().splitlines():
            try:
                os.kill(int(line.strip()), signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass


def cmd_new(config_path: Optional[str] = None) -> int:
    """
    Stop all current HandQ processes and start a fresh idle session (state1).

    Sequence (each step only runs after the previous one completes):
      1. Kill old child + wipe all state/IPC files (blocking).
      1b. Wait until no --_idle_mode process is alive (closes spawn-window race).
      2. Spawn new idle background child.
      3. Wait until the child has written its PID and initial state.json.
      4. Print session info.

    This ordering guarantees no overlap between old and new sessions.
    """
    # 1. Hard stop — blocks until old child is confirmed dead and all files wiped.
    _kill_all_handq_processes()

    # 1b. Wait until no --_idle_mode process is alive.  This closes the race
    #     window where a child was Popen'd but hasn't written PID_FILE yet:
    #     _kill_all_handq_processes() can't find it by PID, so it survives.
    #     Without this wait, a second cmd_new call would spawn a duplicate.
    _wait_for_no_idle_processes(timeout=3.0)

    # 2. Spawn new background child.  PID_FILE is absent at this point so
    #    _register_singleton() in the child will succeed on the first try.
    _push_tmux_status(1)
    _start_idle_background(config_path)

    # 3. Wait for the child to write its initial state.json (up to 15 s).
    #    We check STATE_FILE with handq_active=True rather than requiring the
    #    PID to be alive simultaneously: the compiled binary may take longer to
    #    start (loading shared libs from handq.dist/), and a child that writes
    #    state.json then immediately crashes still leaves a valid state1 file.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if STATE_FILE.exists() and _read_state().get("handq_active"):
            break
        time.sleep(0.05)

    # 4. Print session info from state written by the child.
    state = _read_state()
    session_id    = state.get("session_id", "")
    workspace_str = state.get("workspace_path", "")
    print("HandQ: new session started.")
    if session_id:
        print(f"  Session ID : {session_id}")
    if workspace_str:
        print(f"  Workspace  : {workspace_str}")
    return 0


def cmd_prompt_state() -> int:
    """Print the prompt prefix for the current state (used by tmux status bar)."""
    print(_PROMPT_TITLES.get(_get_handq_state(), ""), end="")
    return 0


def _display_status(state: dict) -> None:
    """Display rich-formatted status snapshot of the current HandQ session."""
    from src.ui.status_tui import run_status_tui
    run_status_tui(
        workspace_path=state.get("workspace_path", ""),
        handq_dir=str(HANDQ_DIR),
        state=state,
    )


# ---------------------------------------------------------------------------
# Interactive-mode state handlers
# ---------------------------------------------------------------------------

def _enter_state1(config_path: Optional[str] = None) -> None:
    """
    Transition from state0 to state1.

    Sets handq_active=True in state.json, pushes state1 to tmux status bar,
    spawns the receptionist background process, and prints the welcome message.
    """
    _ensure_handq_dir()
    _set_handq_active(True)
    _push_tmux_status(1)
    _start_idle_background(config_path)
    print("HandQ runtime enabled")


def _handle_state1(config_path: Optional[str]) -> bool:
    """State1 dialog: send a message to the receptionist background process.

    Returns True if the user submitted a message, False if they cancelled.
    """
    _ws = _read_state().get("workspace_path", "")
    msg = _show_dialog(
        state_label="[HandQ] Ready",
        prompt_text="Message: ",
        header_lines=[
            "",
            "  +----------------------------------+",
            "  |  HandQ  *  No active task        |",
            "  +----------------------------------+",
            *([f"  Workspace: {_ws}"] if _ws else []),
        ],
    )
    if msg:
        # Ensure receptionist background is running (may have been killed externally)
        _start_idle_background(config_path)
        _send_message_to_task(msg)
        return True
    return False





def _handle_state2(config_path: Optional[str] = None) -> None:
    """State2 dialog: show status, then send a message to the running task, or respond to a confirmation."""
    state   = _read_state()
    session = state.get("session_id", "unknown")

    # Check for a pending confirmation request from the background child.
    conf_req = _read_confirmation_request()
    if conf_req is not None:
        _handle_state2_confirmation(conf_req, session)
        return

    # Display rich status snapshot (like state3 shows completion result)
    _display_status(state)

    _ws = state.get("workspace_path", "")
    msg = _show_dialog(
        state_label="[HandQ Running]",
        prompt_text="Message (Enter to cancel): ",
        header_lines=[
            "",
            "  +----------------------------------+",
            "  |  HandQ  *  Task running          |",
            "  +----------------------------------+",
            *([f"  Workspace: {_ws}"] if _ws else []),
            "",
        ],
    )
    if msg:
        _send_message_to_task(msg)


def _handle_state2_confirmation(conf_req: dict, session: str) -> None:
    """
    Show a confirmation dialog for a pending background-child request.

    Writes the user's response to CONFIRMATION_RESPONSE_FILE so the
    background child can unblock and continue.
    """
    req_type = conf_req.get("type", "tool")

    if req_type == "secret_input":
        _handle_state2_secret_input(conf_req, session)
        return

    if req_type == "tool":
        tool_name  = conf_req.get("tool_name", "unknown")
        parameters = conf_req.get("parameters", "")
        reasoning  = conf_req.get("reasoning", "")
        header_lines = [
            "",
            "  +----------------------------------+",
            "  |  HandQ  *  Confirmation needed   |",
            f"  |  Session: {session[:22]:<22}|",
            "  +----------------------------------+",
            f"  Tool:       {tool_name}",
            f"  Parameters: {str(parameters)}",
            f"  Reasoning:  {str(reasoning)}",
            "",
        ]
        prompt_text = "yes / no / guidance: "
        state_label = f"[HandQ] Confirm: {tool_name}"
    else:
        risk_desc = conf_req.get("risk_description", "")
        header_lines = [
            "",
            "  +----------------------------------+",
            "  |  HandQ  *  HIGH-RISK OPERATION   |",
            f"  |  Session: {session[:22]:<22}|",
            "  +----------------------------------+",
            f"  {str(risk_desc)}",
            "",
        ]
        prompt_text = "yes / no / guidance: "
        state_label = "[HandQ] High-risk confirmation"

    response = _show_dialog(
        state_label=state_label,
        prompt_text=prompt_text,
        header_lines=header_lines,
    )

    if response is None:
        # User cancelled (Ctrl-C / empty Enter) → default to "no"
        response = "no"

    _write_confirmation_response(response)
    print(f"HandQ: confirmation response sent ({response!r}).")


def _handle_state2_secret_input(conf_req: dict, session: str) -> None:
    """
    Handle a secret-input (password) request from the background child.

    The background child generated an ephemeral RSA key pair and embedded
    the public key in the request file.  The private key only lives in the
    child's memory — never written to disk.  We encrypt the password here
    with the public key before writing to CONFIRMATION_RESPONSE_FILE, so
    even if the response file lingers on disk it cannot be decrypted without
    the in-memory private key.
    """
    import getpass as _gp
    import base64
    from cryptography.hazmat.primitives.asymmetric import padding as _asym_padding
    from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser

    prompt     = conf_req.get("prompt", "Password required")
    public_pem = conf_req.get("public_key", "")

    print("")
    print("  +----------------------------------+")
    print("  |  HandQ  *  Password required     |")
    print(f"  |  Session: {session[:22]:<22}|")
    print("  +----------------------------------+")
    print(f"  {prompt}")
    print("")

    try:
        password = _gp.getpass("  Password (hidden): ")
    except (EOFError, KeyboardInterrupt):
        password = ""

    if password and public_pem:
        try:
            public_key = _ser.load_pem_public_key(public_pem.encode("ascii"))
            encrypted  = public_key.encrypt(
                password.encode("utf-8"),
                _asym_padding.OAEP(
                    mgf=_asym_padding.MGF1(algorithm=_hashes.SHA256()),
                    algorithm=_hashes.SHA256(),
                    label=None,
                ),
            )
            response = base64.b64encode(encrypted).decode("ascii")
        except Exception as exc:
            print(f"HandQ: encryption error — {exc}", file=sys.stderr)
            response = ""
    else:
        response = ""

    _write_confirmation_response(response)
    print("HandQ: password response sent.")


# ---------------------------------------------------------------------------
# Completion result printer — uses rich for markdown rendering
# ---------------------------------------------------------------------------

def _read_metrics_summary(workspace_path: str) -> Optional[str]:
    """
    Read metrics_summary.json from the workspace and return a Markdown string,
    or None if the file is absent or unreadable.
    """
    try:
        metrics_file = Path(workspace_path) / "metrics_summary.json"
        if not metrics_file.exists():
            return None
        data = json.loads(metrics_file.read_text(encoding="utf-8"))
        m = data.get("metrics", {})

        rows = []
        if m.get("total_duration_seconds", 0) > 0:
            rows.append(("Total duration", f"{m['total_duration_seconds']:.1f}s"))
            rows.append(("Avg task duration", f"{m['avg_duration_seconds']:.1f}s"))
            rows.append(("Min task duration", f"{m['min_duration_seconds']:.1f}s"))
            rows.append(("Max task duration", f"{m['max_duration_seconds']:.1f}s"))
        if m.get("step_confidence_avg", 0) > 0:
            rows.append(("Avg confidence", f"{m['step_confidence_avg']:.2f}"))
        if m.get("avg_iterations_per_step", 0) > 0:
            rows.append(("Avg iters/step", f"{m['avg_iterations_per_step']:.1f}"))
        if m.get("replan_count", 0) > 0:
            rows.append(("Replans", str(m["replan_count"])))
        if m.get("interrupt_count", 0) > 0:
            rows.append(("Interrupts", str(m["interrupt_count"])))
        if m.get("total_steps", 0) > 0:
            rows.append(("Total steps", str(m["total_steps"])))
        if m.get("total_tokens", 0) > 0:
            rows.append(("Total tokens", str(m["total_tokens"])))
            rows.append(("  Input tokens", str(m["total_input_tokens"])))
            rows.append(("  Output tokens", str(m["total_output_tokens"])))
        if m.get("total_cache_creation_tokens", 0) > 0 or m.get("total_cache_read_tokens", 0) > 0:
            rows.append(("  Cache create tokens", str(m.get("total_cache_creation_tokens", 0))))
            rows.append(("  Cache read tokens", str(m.get("total_cache_read_tokens", 0))))

        if not rows:
            return None

        lines = [
            "",
            "---",
            "📊 **Session Metrics**",
            "",
            "| Metric | Value |",
            "|---|---|",
        ]
        for label, value in rows:
            lines.append(f"| {label} | {value} |")
        return "\n".join(lines)
    except Exception:
        return None


def _display_message(message: str, tag: Optional[str] = None, tty_path: Optional[str] = None) -> None:
    """
    Print a message using rich markdown rendering.

    Renders the message as a styled terminal markdown preview
    (headings, tables, bold, code, etc.) without any truncation.
    If tag is provided, a header rule with the tag is shown above the content.
    If tty_path is provided, output goes to that tty device (for background processes).
    """
    try:
        f = open(tty_path, "w") if tty_path else None
    except Exception:
        f = None
    console = Console(highlight=False, file=f, force_terminal=bool(tty_path))
    try:
        console.print()
        if tag:
            console.rule(f"[bold green]{tag}[/bold green]", style="dim")
            console.print()
        console.print(Markdown(message))
        console.print()
        console.rule(style="dim")
        console.print()
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


def _handle_state3(config_path: Optional[str]) -> None:
    """State3 dialog: show completion result and send follow-up to receptionist."""
    state  = _read_state()
    reason = state.get("completion_reason", "Task completed.")

    # Append metrics summary if available
    workspace_path = state.get("workspace_path")
    if workspace_path:
        metrics_md = _read_metrics_summary(workspace_path)
        if metrics_md:
            reason = reason + "\n" + metrics_md

    _display_message(reason, tag="✅  HandQ — Task Complete")

    _ws = state.get("workspace_path", "")
    msg = _show_dialog(
        state_label="[HandQ Complete]",
        prompt_text="Message (Enter to cancel): ",
        header_lines=[
            "",
            "  +----------------------------------+",
            "  |  HandQ  *  Task complete         |",
            "  +----------------------------------+",
            *([f"  Workspace: {_ws}"] if _ws else []),
            "",
        ],
    )
    if msg:
        _send_message_to_task(msg)


# ---------------------------------------------------------------------------
# IPC helpers — message queue directory
# ---------------------------------------------------------------------------

def _send_message_to_task(message: str) -> None:
    """
    Enqueue a message for the background child via the messages directory.

    Each call writes one file to MESSAGES_DIR named
    ``<timestamp>_<pid>.txt``.  Because every message is a distinct file,
    rapid successive calls never overwrite each other — the background
    child drains the queue one file at a time in chronological order.

    The write itself uses an atomic write-then-rename pattern so the
    background child never reads a partially-written file.
    """
    _ensure_handq_dir()
    MESSAGES_DIR.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    msg_file = MESSAGES_DIR / f"{ts}_{os.getpid()}.txt"
    tmp      = msg_file.with_suffix(".tmp")

    try:
        tmp.write_text(message, encoding="utf-8")
        tmp.replace(msg_file)
        print("HandQ: Got it.")
    except Exception as exc:
        print(f"HandQ: failed to send message: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read_message_file() -> Optional[str]:
    """
    Dequeue and return the oldest pending message from MESSAGES_DIR.

    Reads the lexicographically first ``*.txt`` file (chronological order
    because filenames are timestamp-prefixed), deletes it, and returns its
    content.  Returns None when the queue is empty or on any error.

    Called by the background child's monitor loop every ~200 ms.
    """
    try:
        if not MESSAGES_DIR.exists():
            return None

        # Collect only fully-written files (exclude *.tmp)
        msg_files = sorted(
            f for f in MESSAGES_DIR.iterdir()
            if f.suffix == ".txt" and not f.name.endswith(".tmp")
        )

        if not msg_files:
            return None

        oldest = msg_files[0]
        try:
            msg = oldest.read_text(encoding="utf-8").strip()
            oldest.unlink(missing_ok=True)
            return msg if msg else None
        except Exception:
            return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Confirmation IPC helpers
# ---------------------------------------------------------------------------

def _write_confirmation_request(request: dict) -> None:
    """
    Write a confirmation request from the background child to a file.

    The foreground process reads this file when the user runs 'handq' in
    state2 and shows a confirmation dialog.  Uses atomic write-then-rename.
    """
    _ensure_handq_dir()
    tmp = CONFIRMATION_REQUEST_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(request, indent=2), encoding="utf-8")
        tmp.replace(CONFIRMATION_REQUEST_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read_confirmation_request() -> Optional[dict]:
    """
    Read and return the pending confirmation request, or None if absent.

    Called by the foreground process to check whether the background child
    is waiting for a confirmation response.
    """
    try:
        if not CONFIRMATION_REQUEST_FILE.exists():
            return None
        return json.loads(CONFIRMATION_REQUEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_confirmation_request() -> None:
    """Remove the confirmation request file after the response is sent."""
    try:
        CONFIRMATION_REQUEST_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _write_confirmation_response(response: str) -> None:
    """
    Write the user's confirmation response for the background child to read.

    Uses atomic write-then-rename.
    """
    _ensure_handq_dir()
    tmp = CONFIRMATION_RESPONSE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(response, encoding="utf-8")
        tmp.replace(CONFIRMATION_RESPONSE_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read_confirmation_response() -> Optional[str]:
    """
    Read and consume the confirmation response file.

    Called by the background child to get the user's answer.  Deletes the
    file after reading so it is not consumed twice.
    """
    try:
        if not CONFIRMATION_RESPONSE_FILE.exists():
            return None
        resp = CONFIRMATION_RESPONSE_FILE.read_text(encoding="utf-8").strip()
        CONFIRMATION_RESPONSE_FILE.unlink(missing_ok=True)
        return resp if resp else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Background child — singleton / PID management
# ---------------------------------------------------------------------------

_pid_registered: bool = False
_clean_exit: bool = False


def _get_running_child_pid() -> Optional[int]:
    """
    Return the PID of the running background child, or None.

    Primary: reads handq.pid and verifies the process is alive.
    Fallback: pgrep on --_idle_mode + --_handq_instance tag, for the rare
    case where the PID file was lost (crash before atexit ran) but the
    process is still running.  Uses the same pattern as _kill_all_handq_processes.
    """
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # raises OSError if not alive
        return pid
    except Exception:
        pass

    # PID file missing or stale — try pgrep as fallback
    try:
        result = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", _idle_mode_pgrep_pattern()],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        for line in result.stdout.decode().splitlines():
            try:
                return int(line.strip())
            except ValueError:
                pass
    except Exception:
        pass
    return None


def _register_singleton() -> None:
    """
    Register this process as the singleton background child.

    Writes the current PID to PID_FILE and installs signal handlers for
    SIGTERM (clean exit) and SIGINT (double-Ctrl-C to force exit).
    Raises RuntimeError if another child is already running and cannot be
    displaced.

    Uses O_CREAT|O_EXCL for atomic PID-file creation so two processes
    spawned in rapid succession cannot both pass the singleton check.

    Newer-wins rule: if two children race, the one with the higher PID
    (spawned more recently by cmd_new) wins.  The older process calls
    os._exit(0) silently; the newer one kills the older and claims the slot.

    If the file already exists but the recorded PID is dead (stale lock),
    the file is removed and creation is retried once.
    """
    global _pid_registered
    _ensure_handq_dir()

    def _try_create() -> bool:
        """Atomically create PID_FILE. Returns True on success, False if it exists."""
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False

    if not _try_create():
        # File exists — check whether the recorded PID is still alive.
        existing = _get_running_child_pid()
        if existing is not None:
            # Both processes are alive.  The one with the higher PID was
            # spawned more recently (cmd_new scenario).  The newer process
            # wins: if we are older, exit silently so the newer one takes
            # over.  If we are newer, kill the older one and claim the slot.
            if existing > os.getpid():
                # We are the older process — yield to the newer one.
                os._exit(0)
            else:
                # We are the newer process — kill the older one and retry.
                try:
                    os.kill(existing, signal.SIGTERM)
                    _wait_for_pid(existing, timeout=3.0)
                except Exception:
                    pass
                try:
                    PID_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                if not _try_create():
                    # Lost the race again — another process snuck in.
                    existing = _get_running_child_pid()
                    if existing is not None and existing > os.getpid():
                        os._exit(0)
                    raise RuntimeError(
                        f"Another HandQ instance is already running (PID {existing})."
                    )
        else:
            # Stale lock: dead process left the file behind.  Remove and retry once.
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            if not _try_create():
                # Another process won the race on the retry — apply newer-wins rule.
                existing = _get_running_child_pid()
                if existing is not None and existing > os.getpid():
                    os._exit(0)
                raise RuntimeError(
                    f"Another HandQ instance is already running (PID {existing})."
                )
    _pid_registered = True
    atexit.register(_cleanup_child)
    # Remove the spawn lock now that PID_FILE is written — the foreground
    # process uses this lock to avoid spawning a duplicate during startup.
    try:
        _SPAWN_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    _last_sigint_time: list = [0.0]

    def _sig_handler(signum, frame):
        if signum == signal.SIGINT:
            now = time.time()
            if now - _last_sigint_time[0] <= 2.0:
                _cleanup_child()
                os._exit(0)
            else:
                _last_sigint_time[0] = now
        else:
            # SIGTERM = explicit exit → wipe all state so the next startup
            # begins completely fresh with no data residue.
            global _clean_exit
            _clean_exit = True
            _cleanup_child()
            os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _sig_handler)
        except (OSError, ValueError):
            pass


def _cleanup_child() -> None:
    """
    Cleanup handler for the background child process.

    Removes the PID file.  If the process is exiting cleanly (SIGTERM from
    cmd_exit/cmd_new), we only remove the PID file — the foreground process
    is responsible for wiping state.json *after* we exit, so we must NOT
    touch it here (race condition: our write would overwrite the foreground's
    fresh state written by cmd_new).  If the process crashed/was killed
    unexpectedly, set task_status="completed" so the next foreground
    invocation shows state3.
    """
    global _pid_registered
    if not _pid_registered:
        return
    _pid_registered = False
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    if not _clean_exit:
        state = _read_state()
        # Guard: only write if state.json still records our PID.
        # If cmd_new has already spawned a new child that wrote its own
        # initial state, our PID will no longer match and we must not
        # overwrite the new child's state.
        if state.get("task_status") == "running" and state.get("pid") == os.getpid():
            state["task_status"] = "completed"
            _write_state(state)


# ---------------------------------------------------------------------------
# Background child — state updater
# ---------------------------------------------------------------------------

class _StateUpdater:
    """Thread-safe helper that writes task progress to state.json."""

    def __init__(self, session_id: str, workspace_path: str) -> None:
        self._session_id     = session_id
        self._workspace_path = workspace_path
        self._lock           = threading.Lock()

    def update(self, task_status: str, message: str = "") -> None:
        with self._lock:
            _write_state({
                "pid":            os.getpid(),
                "workspace_path": self._workspace_path,
                "task_status":    task_status,
                "session_id":     self._session_id,
                "started_at":     datetime.now(timezone.utc).isoformat(),
                "last_message":   message,
                "handq_active":   True,
            })
            _state_map = {"running": 2, "completed": 3, "": 1}
            _push_tmux_status(_state_map.get(task_status, 1))

    def update_completed(self, summary: str) -> None:
        """Transition to 'completed' and persist the completion reason."""
        with self._lock:
            state = _read_state()
            state["task_status"]       = "completed"
            state["completion_reason"] = summary
            state["last_message"]      = summary
            _write_state(state)
            _push_tmux_status(3)


# ---------------------------------------------------------------------------
# Background child — minimal UI adapter
# ---------------------------------------------------------------------------

class _BackgroundUI:
    """
    Minimal UI adapter for the background child process.

    Routes task-lifecycle events to state.json via _StateUpdater so the
    foreground process can reflect the correct state (state2/state3) on the
    next 'handq' invocation.  display_message() shows a non-blocking tmux
    popup in the top-right corner that auto-dismisses after a few seconds.
    """

    def __init__(self, updater: _StateUpdater, tty_path: Optional[str] = None) -> None:
        self._updater = updater
        self._tty_path = tty_path

    # ── Task-completion hooks ─────────────────────────────────────────────

    def show_task_completed(self, summary: str) -> None:
        """Persist task_status='completed' + completion_reason to state.json."""
        self._updater.update_completed(summary)

    # ── State-change hook ─────────────────────────────────────────────────

    def show_state_changed(self, state: str) -> None:
        """
        Sync task_status to 'running' whenever the planner enters an active
        state (first goal or follow-up after completion).
        """
        if state in ("executing", "planning", "replanning"):
            current = _read_state()
            if current.get("task_status") in ("", "completed"):
                self._updater.update("running", f"Resuming: {state}")

    # ── Status bar detail writer ──────────────────────────────────────────

    def _write_status_detail(self, icon: str, text: str) -> None:
        """Write status_icon and status_text to state.json for tmux status bar."""
        try:
            state = _read_state()
            if state.get("task_status") == "running":
                state["status_icon"] = icon
                state["status_text"] = text
                _write_state(state)
        except Exception:
            pass

    def show_gep_countdown(self, remaining_secs: int, template_name: str) -> None:
        """Update tmux status bar with GEP countdown; -1 clears it."""
        if remaining_secs < 0:
            try:
                state = _read_state()
                state.pop("status_icon", None)
                state.pop("status_text", None)
                _write_state(state)
            except Exception:
                pass
        else:
            icon = "⏳"
            text = f"{template_name or 'GEP'} in {remaining_secs}s"
            try:
                state = _read_state()
                state['status_icon'] = icon
                state['status_text'] = text
                _write_state(state)
            except Exception:
                pass

    # ── No-op stubs for all other UI methods ─────────────────────────────

    def display_receptionist_reply(self, message: str) -> None:
        """Show a Receptionist reply in the terminal (no interactive follow-up)."""
        _display_message(message, tag="💬  HandQ — Receptionist Answer", tty_path=self._tty_path)

    def display_message(self, message: str) -> None:
        """Render a general user-facing message to the tty via rich Markdown.

        The background child's stdout is detached from the user's tty, so
        print() calls never reach the user.  This method writes directly to
        self._tty_path (the tty of the terminal that launched HandQ), mirroring
        the display_receptionist_reply path which is the confirmed-visible
        output channel in the background child.
        """
        _display_message(message, tty_path=self._tty_path)

    def _display_status_message(self, message: str) -> None:
        """Show a one-line status notification via tmux display-message.

        Strips markdown/ANSI and truncates to 200 chars so it fits the status
        bar.  Completely non-interactive — never steals focus.
        """
        import re as _re
        # Strip markdown formatting and ANSI codes for plain status bar display
        plain = _re.sub(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]', '', message)
        plain = _re.sub(r'[*_`#]', '', plain)
        plain = plain.replace('\n', ' ').strip()
        if len(plain) > 200:
            plain = plain[:197] + '...'
        if not plain:
            return
        # Resolve socket from $TMUX to handle non-default UID directories.
        tmux_env = os.environ.get("TMUX", "")
        tmux_pane = os.environ.get("TMUX_PANE")
        target = tmux_pane if tmux_pane else HANDQ_TMUX_SESSION
        if tmux_env:
            sock_args = ["-S", tmux_env.split(",")[0]]
        else:
            sock_args = _TMUX_SOCK_ARGS
        try:
            subprocess.run(
                ["tmux", *sock_args,
                 "display-message", "-t", target, "-d", "8000",
                 f"[HandQ] {plain}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
        except Exception:
            pass

    def display_error(self, message: str) -> None:
        if "I/O operation on closed file" in message:
            return
        if self._tty_path:
            try:
                with open(self._tty_path, "w") as f:
                    f.write(f"[ERROR] {message}\n")
                    f.flush()
            except Exception:
                pass

    def show_notification(self, *a, **kw) -> None:
        pass

    def show_step_started(self, step_id: str, desc: str) -> None:
        self._write_status_detail("▶", desc[:40] if desc else "")

    def show_step_completed(self, step_id: str, desc: str) -> None:
        self._write_status_detail("✓", desc if desc else desc[:40])

    def show_reasoning(self, text: str) -> None:
        self._write_status_detail("💬", text[:50])

    def show_receptionist_thinking(self) -> None:
        """Show 'receptionist thinking' indicator in tmux status bar."""
        try:
            state = _read_state()
            state["status_icon"] = "💬"
            state["status_text"] = "Receptionist thinking..."
            _write_state(state)
            _push_tmux_status(_get_handq_state())
        except Exception:
            pass

    def clear_receptionist_thinking(self) -> None:
        """Clear the receptionist thinking indicator from tmux status bar."""
        try:
            state = _read_state()
            state.pop("status_icon", None)
            state.pop("status_text", None)
            _write_state(state)
            _push_tmux_status(_get_handq_state())
        except Exception:
            pass

    def update_notification(self, *a, **kw) -> None:
        pass

    def dismiss_notification(self, *a, **kw) -> None:
        pass

    def show_confirmation(self, *a, **kw) -> None:
        pass

    def add_log(self, *a, **kw) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def notify_decision_made(self, iteration: int, reasoning: str, token_count: int = 0) -> None:
        """Show 'thinking' indicator in tmux status bar."""
        self._write_status_detail(f"💬[{iteration}]", reasoning[:100])

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[dict],
        output: Optional[dict],
    ) -> None:
        """Show current tool name in tmux status bar."""
        if tool_name:
            # Before execution: show tool being called
            if params:
                # Extract primary param for display
                if tool_name == "bash":
                    cmd = str(params.get("command", ""))[:30]
                    self._write_status_detail("$", f"{tool_name}: {cmd}")
                elif tool_name in ("read", "write", "edit"):
                    path = str(params.get("path", ""))
                    # Show just the filename
                    fname = _pp.basename(path)[:30]
                    self._write_status_detail(f"⊙[{iteration}]", f"{tool_name}: {fname}")
                else:
                    self._write_status_detail(f"⊙[{iteration}]", tool_name)
            else:
                self._write_status_detail(f"⊙[{iteration}]", tool_name)

    def display_progress_status(self, current: int, total: int) -> None:
        """Show step progress in tmux status bar."""
        self._write_status_detail("≡", f"{current}/{total} steps")

    def notify_step_confidence(self, confidence: float) -> None:
        """Append confidence score to confidence_history in state.json (keep last 20)."""
        try:
            state = _read_state()
            hist = state.get("confidence_history", [])
            hist.append(round(confidence, 3))
            if len(hist) > 20:
                hist = hist[-20:]
            state["confidence_history"] = hist
            _write_state(state)
        except Exception:
            pass

    # ── File-based confirmation (background child path) ───────────────────

    async def _request_confirmation_via_file(
        self, request: dict, timeout: float = 300.0
    ) -> str:
        """
        Write a confirmation request to a file and poll for the response.

        The foreground process reads the request file when the user runs
        'handq' and writes the response to the response file.  This method
        suspends the event loop between polls so other coroutines can run.

        Returns the raw response string (e.g. "yes", "no", or free text).
        Defaults to "no" on timeout.
        """
        _write_confirmation_request(request)
        _push_tmux_status(4)  # state4: waiting for confirmation
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = _read_confirmation_response()
            if resp is not None:
                _clear_confirmation_request()
                _push_tmux_status(2)  # back to running
                return resp
            await asyncio.sleep(0.5)
        # Timeout — clean up and default to rejection
        _clear_confirmation_request()
        return "no"

    async def request_tool_confirmation(self, tool_name: str, decision) -> "UserConfirmation":
        """
        Request tool-execution confirmation via file-based IPC.

        Writes the request to ~/.handq/confirmation_request.json and blocks
        until the foreground process delivers a response via
        ~/.handq/confirmation_response.txt.
        """
        from src.models.state import UserConfirmation
        request = {
            "type": "tool",
            "tool_name": tool_name,
            "parameters": str(getattr(decision, "parameters", "")),
            "reasoning": str(getattr(decision, "reasoning", "")),
        }
        raw = await self._request_confirmation_via_file(request)
        if raw.lower() in ("y", "yes"):
            return UserConfirmation.yes()
        elif raw.lower() in ("n", "no"):
            return UserConfirmation.no()
        else:
            return UserConfirmation.with_message(raw)

    async def request_risk_confirmation(self, decision, risk_description: str) -> "UserConfirmation":
        """
        Request high-risk-operation confirmation via file-based IPC.
        """
        from src.models.state import UserConfirmation
        request = {
            "type": "risk",
            "risk_description": risk_description,
            "reasoning": str(getattr(decision, "reasoning", "")),
        }
        raw = await self._request_confirmation_via_file(request)
        if raw.lower() in ("y", "yes"):
            return UserConfirmation.yes()
        elif raw.lower() in ("n", "no"):
            return UserConfirmation.no()
        else:
            return UserConfirmation.with_message(raw)

    async def request_secret_input(self, prompt: str) -> str:
        """
        Request a secret (password) from the user via file-based IPC.

        Generates an ephemeral RSA-2048 key pair.  The public key travels in
        the request file; the private key never leaves this process's memory.
        The foreground encrypts the user's password with the public key and
        writes the ciphertext (base64) to the response file.  Even if the
        response file lingers on disk, it cannot be decrypted without the
        in-memory private key.
        """
        import base64
        from cryptography.hazmat.primitives.asymmetric import (
            rsa, padding as _asym_padding,
        )
        from cryptography.hazmat.primitives import hashes as _hashes, serialization as _ser

        # Generate ephemeral key pair — private key stays in memory only
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        public_pem = private_key.public_key().public_bytes(
            _ser.Encoding.PEM,
            _ser.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

        request = {
            "type":       "secret_input",
            "prompt":     prompt,
            "public_key": public_pem,
        }
        raw = await self._request_confirmation_via_file(request)

        if not raw:
            return ""

        try:
            encrypted = base64.b64decode(raw)
            decrypted = private_key.decrypt(
                encrypted,
                _asym_padding.OAEP(
                    mgf=_asym_padding.MGF1(algorithm=_hashes.SHA256()),
                    algorithm=_hashes.SHA256(),
                    label=None,
                ),
            )
            return decrypted.decode("utf-8")
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Background child — async flow runner
# ---------------------------------------------------------------------------

async def _run_flow_bg(flow, tty_path: Optional[str] = None) -> None:
    """
    Run flow.start_idle_session() and monitor for IPC messages / exit signals.

    The monitor coroutine runs concurrently with the flow task:
      • Polls MESSAGES_DIR every 200 ms and forwards any queued message to
        InteractionManager.inject_user_message() so the running planner can
        incorporate user feedback without restarting.
      • Watches for the sentinel file ~/.handq/exit_requested (written by
        'handq exit') and cancels the flow task gracefully when found.
    """
    from src.controller.interaction_manager import InteractionManager

    im = InteractionManager.get_instance()

    flow_task = asyncio.create_task(
        flow.start_idle_session(),
        name="handq-flow"
    )

    async def _monitor() -> None:
        global _clean_exit
        _tty_miss_count = 0  # consecutive polls where tty is gone
        while not flow_task.done():
            await asyncio.sleep(0.2)

            # Drain all queued messages and forward them to the planner
            while True:
                msg = _read_message_file()
                if msg is None:
                    break
                try:
                    im.inject_user_message(msg)
                except Exception:
                    pass

            # Graceful exit via sentinel file (written by 'handq exit')
            sentinel = HANDQ_DIR / "exit_requested"
            if sentinel.exists():
                try:
                    sentinel.unlink(missing_ok=True)
                except Exception:
                    pass
                _clean_exit = True
                try:
                    flow._interrupt_event.set()
                except Exception:
                    pass
                flow_task.cancel()
                return

            # Save-session request via sentinel file (written by 'handq --save')
            if SAVE_REQUEST_FILE.exists():
                _save_log_file: Optional[str] = None
                try:
                    _save_content = SAVE_REQUEST_FILE.read_text(encoding="utf-8").strip()
                    SAVE_REQUEST_FILE.unlink(missing_ok=True)
                    # Content is either "save" (current session) or an absolute log file path.
                    if _save_content and _save_content != "save":
                        _save_log_file = _save_content
                except Exception:
                    pass
                # _trigger_save_session starts an independent FlowController that
                # takes over the InteractionManager.  We cancel the original flow
                # task first (it is in IDLE, no work is lost) so it does not
                # compete for messages while the save flow runs.
                flow_task.cancel()
                flow.cancel_all_tasks()
                try:
                    await asyncio.wait({flow_task}, timeout=3.0)
                except Exception:
                    pass
                # Launch the save/GEP session as a sibling task so this monitor
                # loop keeps running and continues to drain MESSAGES_DIR every
                # 200 ms, forwarding messages to the save flow's InteractionManager.
                save_task = asyncio.create_task(
                    flow._trigger_save_session(log_file=_save_log_file),
                    name="handq-save-session",
                )
                while not save_task.done():
                    await asyncio.sleep(0.2)
                    while True:
                        msg = _read_message_file()
                        if msg is None:
                            break
                        try:
                            im.inject_user_message(msg)
                        except Exception:
                            pass
                # Save flow has finished (user :exit'd or template was confirmed).
                # Exit the monitor — the background process will terminate naturally.
                return

            # Exit if the HandQ tmux session has disappeared.
            # setpgrp() means we won't receive SIGHUP, so we poll instead.
            # Check the tmux session (not the pane TTY) so that closing one
            # pane while staying in the handq session does not kill us.
            # Fall back to tty_path check when tmux is not available.
            # Require 3 consecutive misses (~0.6 s) to avoid false positives.
            _session_gone = False
            try:
                _tmux_proc = await asyncio.create_subprocess_exec(
                    "tmux", *_TMUX_SOCK_ARGS, "has-session", "-t", HANDQ_TMUX_SESSION,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(_tmux_proc.wait(), timeout=1.0)
                    _session_gone = _tmux_proc.returncode != 0
                except asyncio.TimeoutError:
                    _tmux_proc.kill()
                    _session_gone = False
            except Exception:
                # tmux not available — fall back to TTY path check
                if tty_path:
                    _session_gone = not os.path.exists(tty_path)
            if _session_gone:
                _tty_miss_count += 1
                if _tty_miss_count >= 3:
                    _clean_exit = True
                    try:
                        flow._interrupt_event.set()
                    except Exception:
                        pass
                    flow_task.cancel()
                    return
            else:
                _tty_miss_count = 0

    monitor_task = asyncio.create_task(_monitor(), name="handq-monitor")
    try:
        await flow_task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        # Do NOT cancel monitor_task here: the monitor may be inside
        # _trigger_save_session() (kicked off by 'handq --save').  Cancelling
        # it would silently abort the save flow mid-execution because the
        # monitor cancelled flow_task first, which unblocks this 'await
        # flow_task' and races us into the finally block while the monitor
        # is still running.  The monitor always exits via 'return', so just
        # waiting for it is safe and correct.
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# LLM role assignment — moved to src.infrastructure.role_resolver
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Background child — session builder
# ---------------------------------------------------------------------------

def _build_session(
    config_path: Optional[str] = None,
    session_id: Optional[str] = None,
    shell_context_path: Optional[str] = None,
    _dbg_fn=None,
) -> tuple:
    """
    Build and return (FlowController, List[LLMService], session_dir_str).

    All HandQ module imports are deferred to this function so the foreground
    process (which only shows dialogs) never imports the heavy module tree.
    """
    def _d(msg: str) -> None:
        if _dbg_fn:
            _dbg_fn(f"  [build] {msg}")

    _d("importing yaml, logger, FlowController, AnthropicStreamingService...")
    import yaml  # type: ignore
    from src.infrastructure.logger import initialize_logger, LogLevel
    _d("OK: yaml + logger")
    from src.controller.flow_controller import FlowController
    _d("OK: FlowController")
    # from qgenie_service import QGenieLLMService  # type: ignore  # TODO: re-enable Qgenie support
    from src.infrastructure.anthropic_streaming_service import AnthropicStreamingService
    from src.infrastructure.role_resolver import resolve_role_models
    _d("OK: AnthropicStreamingService")
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    _d(f"config path: {cfg_path}  exists={cfg_path.exists()}")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        _d(f"config loaded: top-level keys={list(config.keys())}")
    except FileNotFoundError:
        _d(f"config file not found: {cfg_path}")
        raise FileNotFoundError(
            f"Config file not found: {cfg_path}. "
            "Pass --config <path>, or place handq_config.yaml next to the binary."
        )
    except yaml.YAMLError as _cfg_err:
        # Common cause: missing space after a colon (e.g. "API_KEY:value" instead
        # of "API_KEY: value") — silently swallowing this used to surface as
        # "No agent models configured", which sent users on a wild goose chase.
        _d(f"config YAML parse FAILED: {_cfg_err}")
        raise ValueError(
            f"Invalid YAML in config file {cfg_path}: {_cfg_err}"
        ) from _cfg_err

    llm_cfg       = config.get("llm") or {}
    _d(f"llm_cfg keys: {list(llm_cfg.keys())}")
    api_key_val   = llm_cfg.get("API_KEY") or ""
    max_tokens    = llm_cfg.get("max_tokens", None)
    # Per-role model lists. Internal keys: agent / planner / receptionist / from_data
    # (helper at the YAML/UI boundary maps to from_data internally).
    roles         = resolve_role_models(llm_cfg)
    log_level_str = config.get("session", {}).get("log_level", "INFO")
    threshold     = float(config.get("session", {}).get(
        "step_verification_threshold",
        FlowController.DEFAULT_STEP_VERIFICATION_THRESHOLD,
    ))
    workspace_base = config.get("session", {}).get("workspace_base", ".workspace")
    venv_path      = config.get("session", {}).get("venv_path")

    if session_id is None:
        session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")

    session_dir = Path(_CALLER_CWD) / workspace_base / session_id
    (session_dir / "logs").mkdir(parents=True, exist_ok=True)
    (session_dir / "executions_logs").mkdir(parents=True, exist_ok=True)

    initialize_logger(
        name="HandQ",
        level=LogLevel[log_level_str.upper()],
        log_file=f"handq_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        log_dir=str(session_dir / "logs"),
    )
    _d("OK: initialize_logger")

    # Build one LLMService per unique model (across roles), then index into svc_map
    # for agent/receptionist/from_data.  Planner gets fresh dedicated instances
    # with max_retries=50, kept separate so its retry budget is never shared.
    _d(f"API_KEY_present={bool(api_key_val)}  roles={ {k: len(v) for k, v in roles.items()} }")

    def _make_service(m: str, max_retries: int) -> "LLMService":
        if m.startswith("anthropic::"):
            return AnthropicStreamingService(
                model=m,
                api_key=api_key_val,
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
        import warnings
        warnings.warn(
            f"Model '{m}' is not an Anthropic model (expected 'anthropic::' prefix). "
            "Only Anthropic models are currently supported. QGenie support is temporarily disabled.",
            UserWarning,
            stacklevel=2,
        )
        raise ValueError(
            f"Unsupported model '{m}': only Anthropic models (anthropic::...) are supported."
        )

    # Shared services for non-planner roles. Dedup so the same model isn't
    # instantiated twice when it appears in agent + receptionist + from_data.
    shared_models: list = []
    for role_key in ("agent", "receptionist", "from_data"):
        for m in roles.get(role_key, []):
            if m not in shared_models:
                shared_models.append(m)
    svc_map = {m: _make_service(m, max_retries=3) for m in shared_models}

    agent_services        = [svc_map[m] for m in roles.get("agent", [])]
    receptionist_services = [svc_map[m] for m in roles.get("receptionist", [])]
    from_data_services    = [svc_map[m] for m in roles.get("from_data", [])]
    planner_services      = [_make_service(m, max_retries=50) for m in roles.get("planner", [])]

    if not agent_services:
        raise ValueError(
            "No agent models configured. Set llm.roles.agent in handq_config.yaml "
            "(or the legacy llm.models list)."
        )

    _d(f"OK: role services  planner={len(planner_services)}"
       f"  receptionist={len(receptionist_services)}"
       f"  from_data={len(from_data_services)}"
       f"  agent={len(agent_services)}")

    _d("creating FlowController...")
    flow = FlowController(
        agent_llm_services=agent_services,
        planner_llm_services=planner_services,
        receptionist_llm_services=receptionist_services,
        from_data_llm_services=from_data_services,
        working_directory=str(Path(_CALLER_CWD)),
        storage_directory=str(session_dir),
        step_verification_threshold=threshold,
        venv_path=venv_path,
        config_path=config_path or str(cfg_path.resolve()),
        shell_context_path=shell_context_path,
    )
    _d("OK: FlowController")

    # Return every service the caller must close on shutdown — dedup by identity
    # so the shared svc_map instances aren't closed twice. Planner services are
    # always distinct (their own max_retries=50 instances), so include them all.
    all_services: list = list(svc_map.values()) + planner_services
    return flow, all_services, str(session_dir)


# ---------------------------------------------------------------------------
# Background child — task entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Task management — foreground side
# ---------------------------------------------------------------------------

def _capture_shell_context() -> None:
    """
    Capture current terminal context and write to SHELL_CONTEXT_FILE.

    Uses tmux capture-pane to capture the visible screen content (input +
    output) of the current pane.  Since HandQ always runs inside a tmux
    session, this is always available.  Writes an empty file if not in tmux
    or if capture fails.
    """
    import re as _re
    _ANSI_ESCAPE = _re.compile(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]|\x1b\].*?\x07|\r')

    lines: List[str] = []

    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane and os.environ.get("TMUX"):
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-S", "-300", "-t", tmux_pane],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            if result.returncode == 0:
                raw = result.stdout.decode("utf-8", errors="replace")
                for line in raw.splitlines():
                    clean = _ANSI_ESCAPE.sub("", line).strip()
                    if clean:
                        lines.append(clean)
        except Exception:
            pass

    last200 = lines[-300:] if len(lines) > 300 else lines
    content = "\n".join(last200)

    _ensure_handq_dir()
    tmp = SHELL_CONTEXT_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(SHELL_CONTEXT_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _resolve_python(config_path: Optional[str]) -> str:
    """
    Resolve the Python executable for spawning background child processes.

    Priority (highest to lowest):
      1. config venv_path  — explicit venv set in handq_config.yaml
      2. VIRTUAL_ENV env   — currently activated venv in the user's shell
      3. shutil.which("python3") — system Python on PATH
         (venv_path: null in config means "use system Python")
      4. sys.executable    — last resort; unreliable in Nuitka compiled mode
         where sys.executable may be a fake Python path inside handq.dist/

    Uses a simple regex scan to read venv_path from the YAML config without
    importing the yaml module (which may not be available at this point).
    """
    import re

    # 1. Config venv_path
    cfg = Path(config_path) if config_path else DEFAULT_CONFIG
    try:
        text = cfg.read_text(encoding="utf-8")
        m = re.search(r"^\s*venv_path\s*:\s*(\S+)", text, re.MULTILINE)
        if m and m.group(1) not in ("null", "~", ""):
            venv_python = Path(m.group(1)) / "bin" / "python3"
            if venv_python.exists():
                return str(venv_python)
    except Exception:
        pass

    # 2. Active VIRTUAL_ENV
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        venv_python = Path(venv_env) / "bin" / "python3"
        if venv_python.exists():
            return str(venv_python)

    # 3. System Python — implements "venv_path: null" = use system Python.
    #    Must come before sys.executable: in Nuitka compiled mode sys.executable
    #    is set to a fake path (e.g. handq.dist/python3) that does not exist on
    #    the target machine.
    import shutil as _shutil
    system_python = _shutil.which("python3") or _shutil.which("python")
    if system_python:
        return system_python

    # 4. Last resort
    return sys.executable



def _start_idle_background(config_path: Optional[str]) -> None:
    """
    Spawn handq.py --_idle_mode as the background child.

    The idle background process loads the full session (FlowController with
    Receptionist) and calls start_idle_session(): the message processor and
    planner loop start immediately, but no initial planning is done.  The
    Receptionist handles all incoming messages from the start, preserving
    full conversation_history across state1 → state2 → state3.

    No-op if a background child is already running or is in the process of
    starting up (spawn lock file present).
    """
    if _get_running_child_pid() is not None:
        return  # already alive

    # Guard against the startup window: the child takes time to import modules
    # and write PID_FILE.  If _start_idle_background is called twice in rapid
    # succession (e.g. _enter_state1 then _handle_state1), the second call
    # would see no PID_FILE and spawn a duplicate.  The spawn lock file covers
    # this window: written here before Popen, removed by the child after it
    # writes PID_FILE.
    _ensure_handq_dir()
    if _SPAWN_LOCK_FILE.exists():
        return  # another spawn is already in progress
    try:
        _SPAWN_LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    tty_path = _get_tty_path()
    _started_tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    _instance_tag = f"{os.environ.get('USER', 'default')}@{_HANDQ_HOST}"
    if _IS_COMPILED:
        # Use _SELF_BINARY (sys.argv[0] resolved) rather than sys.executable:
        # Nuitka may set sys.executable to a Python symlink path inside
        # handq.dist/ that does not exist on the target machine.
        cmd = [
            _SELF_BINARY,
            "--_idle_mode",
            "--_handq_instance", _instance_tag,
            "--_started", _started_tag,
            "--_pos", _CALLER_CWD,
        ]
    else:
        _python = _resolve_python(config_path)
        cmd = [
            _python,
            str(_HERE / "handq.py"),
            "--_idle_mode",
            "--_handq_instance", _instance_tag,
            "--_started", _started_tag,
            "--_pos", _CALLER_CWD,
        ]
    if config_path:
        cmd += ["--config", config_path]
    if tty_path:
        cmd += ["--_tty", tty_path]
    if SHELL_CONTEXT_FILE.exists():
        cmd += ["--_shell_context", str(SHELL_CONTEXT_FILE)]

    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=_CALLER_CWD,
        env={**os.environ,
             "HANDQ_TMUX_SESSION": HANDQ_TMUX_SESSION,
             "HANDQ_TMUX_SOCKET": HANDQ_TMUX_SOCKET},
    )
    kwargs["preexec_fn"] = os.setpgrp  # type: ignore[attr-defined]

    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def _read_workspace_base(config_path: Optional[str]) -> str:
    """Read workspace_base from config YAML without importing yaml (regex scan)."""
    import re
    cfg = Path(config_path) if config_path else DEFAULT_CONFIG
    try:
        text = cfg.read_text(encoding="utf-8")
        m = re.search(r"^\s*workspace_base\s*:\s*(\S+)", text, re.MULTILINE)
        if m and m.group(1) not in ("null", "~", ""):
            return m.group(1)
    except Exception:
        pass
    return ".workspace"


def _run_idle_background(tty_path: Optional[str], config_path: Optional[str], shell_context_path: Optional[str] = None) -> int:
    """
    Background child entry point for idle/receptionist mode.

    Loads the full session and calls flow.start_idle_session() which starts
    the message processor (Receptionist) and planner loop immediately without
    an initial goal.  The first REPLAN message from the user becomes the goal.
    """
    # ── Debug log helper ──────────────────────────────────────────────────────
    _dbg_path = HANDQ_DIR / "idle_debug.log"
    _crash_log_path = HANDQ_DIR / "crash.log"
    def _dbg(msg: str) -> None:
        try:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(_dbg_path, "a", encoding="utf-8") as _f:
                _f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
    _dbg(f"--- _run_idle_background START  pid={os.getpid()}  config={config_path}")
    _dbg(f"    _IS_COMPILED={_IS_COMPILED}  _HERE={_HERE}  HANDQ_DIR={HANDQ_DIR}")

    # Enable faulthandler to capture segfaults / C-level crashes that bypass
    # Python exception handling.  Output goes to crash.log in HANDQ_DIR.
    try:
        import faulthandler as _fh
        _crash_log_f = open(str(_crash_log_path), "w")
        _fh.enable(file=_crash_log_f)
        _dbg(f"faulthandler enabled → {_crash_log_path}")
    except Exception as _fh_err:
        _dbg(f"faulthandler setup failed: {_fh_err}")

    for _sig_name in ("SIGTTIN",):
        _sig_val = getattr(signal, _sig_name, None)
        if _sig_val is not None:
            try:
                signal.signal(_sig_val, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
    # SIGHUP is sent by tmux when the session closes.  Install a handler that
    # triggers a clean exit so the background child does not become an orphan
    # when the user types 'exit' in the tmux session.
    _sighup_val = getattr(signal, "SIGHUP", None)
    if _sighup_val is not None:
        try:
            def _sighup_handler(signum, frame):
                global _clean_exit
                _clean_exit = True
                _cleanup_child()
                os._exit(0)
            signal.signal(_sighup_val, _sighup_handler)
        except (OSError, ValueError):
            pass

    try:
        _register_singleton()
    except RuntimeError:
        _dbg("FAIL: _register_singleton() raised RuntimeError — another instance running?")
        return 1
    _dbg("OK: _register_singleton()")

    session_id = datetime.now().strftime("session_%Y%m%d_%H%M%S")
    workspace_base = _read_workspace_base(config_path)
    workspace = str(Path(_CALLER_CWD) / workspace_base / session_id)
    updater = _StateUpdater(session_id=session_id, workspace_path=workspace)

    # Write initial state: active, no task yet (task_status="" → state1)
    _write_state({
        "pid":            os.getpid(),
        "workspace_path": workspace,
        "task_status":    "",
        "session_id":     session_id,
        "started_at":     datetime.now(timezone.utc).isoformat(),
        "last_message":   "",
        "handq_active":   True,
    })
    _dbg("OK: _write_state (state1)")

    try:
        from src.controller.interaction_manager import InteractionManager
        im = InteractionManager.get_instance()
        im.set_ui(_BackgroundUI(updater, tty_path=tty_path))
        _dbg("OK: InteractionManager")
    except Exception as exc:
        import traceback as _tb
        _tb_str = _tb.format_exc()
        _dbg(f"FAIL: InteractionManager — {exc}\n{_tb_str}")
        try:
            (HANDQ_DIR / "run_flow_error.txt").write_text(
                f"InteractionManager init failed:\n{_tb_str}", encoding="utf-8"
            )
        except Exception:
            pass
        updater.update_completed(f"Session init failed: {exc}")
        return 1

    try:
        _dbg("building session (_build_session)...")
        flow, all_llm_services, _ = _build_session(
            config_path=config_path, session_id=session_id,
            shell_context_path=shell_context_path,
            _dbg_fn=_dbg,
        )
        _dbg("OK: _build_session")
    except BaseException as exc:
        import traceback as _tb
        _tb_str = _tb.format_exc()
        _dbg(f"FAIL: _build_session — {type(exc).__name__}: {exc}\n{_tb_str}")
        try:
            (HANDQ_DIR / "last_build_error.txt").write_text(_tb_str, encoding="utf-8")
        except Exception:
            pass
        if not isinstance(exc, Exception):
            raise   # re-raise SystemExit / KeyboardInterrupt after logging
        updater.update_completed(f"Session build failed: {exc}")
        return 1

    try:
        _dbg("starting asyncio.run(_run_flow_bg)...")
        asyncio.run(_run_flow_bg(flow, tty_path=tty_path))
        _dbg("asyncio.run returned normally")
    except KeyboardInterrupt:
        _dbg("asyncio.run: KeyboardInterrupt")
    except Exception as _exc:
        import traceback as _tb
        _tb_str = _tb.format_exc()
        _dbg(f"FAIL: asyncio.run(_run_flow_bg) — {_exc}\n{_tb_str}")
        try:
            (HANDQ_DIR / "flow_bg_error.txt").write_text(_tb_str, encoding="utf-8")
        except Exception:
            pass

    try:
        for svc in all_llm_services:
            asyncio.run(svc.close())
    except Exception:
        pass

    _dbg("--- _run_idle_background EXIT")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handq",
        description="HandQ — AI task execution agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=(
            "Options: --help  --config  --exit  --new  --models\n"
            "No args: interactive mode\n"
            "Inline goal: handq <goal text...>"
        ),
    )
    parser.add_argument(
        "--config", "-c",
        default=None, dest="config", metavar="PATH",
        help="Path to config YAML file",
    )
    # User-facing subcommands (now --flags)
    parser.add_argument("--version", "-v", action="version",
                        version="handq %s" % __version__)
    parser.add_argument("--help",        action="store_true", default=False, dest="cmd_help")
    parser.add_argument("--exit",        action="store_true", default=False, dest="cmd_exit")
    parser.add_argument("--new",         action="store_true", default=False, dest="cmd_new")
    parser.add_argument("--show-config", action="store_true", default=False, dest="cmd_show_config")
    parser.add_argument("--models",      action="store_true", default=False, dest="cmd_models",
                        help="Show model-to-role assignment from current config")
    parser.add_argument("--prompt-state", action="store_true", default=False, dest="cmd_prompt_state")
    parser.add_argument("--save",        nargs="?", const=True, default=False, dest="cmd_save",
                        metavar="PATH",
                        help="Save last completed task as a GEP template; "
                             "optionally provide a session log PATH to save from")
    parser.add_argument("--list",        action="store_true", default=False, dest="cmd_list",
                        help="List available GEP templates")
    # Inline goal: remaining positional args
    parser.add_argument(
        "inline_goal",
        nargs="*", default=[],
        help="Goal text to submit directly without the interactive dialog",
    )
    # File goal: read goal text from a file
    parser.add_argument(
        "--file", "-f",
        default=None, dest="file_goal", metavar="PATH",
        help="Path to a file whose text content is used as the goal (same as inline_goal)",
    )
    # Hidden flag: tty device path forwarded to background child
    parser.add_argument(
        "--_tty", default=None, dest="tty_path", help=argparse.SUPPRESS,
    )
    # Hidden flag: run as the background idle/receptionist process
    parser.add_argument(
        "--_idle_mode", action="store_true", default=False,
        dest="idle_mode", help=argparse.SUPPRESS,
    )
    # Hidden flag: shell context file path (recent command history)
    parser.add_argument(
        "--_shell_context", default=None, dest="shell_context_path",
        help=argparse.SUPPRESS,
    )
    # Hidden flag: start timestamp tag (visible in ps aux for identification)
    parser.add_argument(
        "--_started", default=None, dest="started_tag",
        help=argparse.SUPPRESS,
    )
    # Hidden flag: user@host instance tag — purely a label for ps/pgrep identification.
    # Allows scoping pgrep to this specific user+host instance without relying
    # solely on the -u uid filter.
    parser.add_argument(
        "--_handq_instance", default=None, dest="handq_instance",
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--_pos", default=None, dest="pos_tag",
        help=argparse.SUPPRESS,
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_SUBCOMMAND_ALIASES = {
    "help":         "--help",
    "exit":         "--exit",
    "new":          "--new",
    "show-config":  "--show-config",
    "prompt-state": "--prompt-state",
    "config":       "--config",
    "save":         "--save",
    "list":         "--list",
}


def _normalize_argv() -> None:
    """
    Allow bare subcommand names (e.g. ``handq exit``) as aliases for their
    ``--flag`` equivalents, while leaving inline goals untouched.

    Rules:
    - ``handq config <path>`` → ``handq --config <path>``  (exactly 2 extra args)
    - All other subcommands (help/exit/new/status/…) only trigger when they
      are the sole non-flag argument, i.e. ``handq exit`` or
      ``handq --config foo.yaml exit`` but NOT ``handq exit the building``.
    """
    if len(sys.argv) < 2:
        return

    # Skip past --config PATH (and -c PATH) to find the first positional arg
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--config", "-c") and i + 1 < len(sys.argv):
            i += 2  # skip flag and its value
            continue
        break  # first non-config arg

    if i >= len(sys.argv):
        return

    first = sys.argv[i]
    if first not in _SUBCOMMAND_ALIASES:
        return

    if first == "config":
        # Allow "handq config <path>" → "handq --config <path>"
        sys.argv[i] = "--config"
    elif i == len(sys.argv) - 1:
        # Only replace when the subcommand is the last (sole positional) argument
        sys.argv[i] = _SUBCOMMAND_ALIASES[first]


def main() -> None:
    _normalize_argv()
    args = _build_parser().parse_args()

    config_path: Optional[str] = None
    if args.config:
        config_path = str(Path(args.config).resolve())
    elif DEFAULT_CONFIG.exists():
        config_path = str(DEFAULT_CONFIG)

    # ── Idle mode (internal, spawned by _start_idle_background) ──────────
    if args.idle_mode:
        sys.exit(_run_idle_background(
            tty_path=args.tty_path,
            config_path=config_path,
            shell_context_path=args.shell_context_path,
        ))

    # ── Subcommands (--flags) — no shell context needed ──────────────────
    if args.cmd_help:
        sys.exit(cmd_help())
    if args.cmd_exit:
        sys.exit(cmd_exit())
    if args.cmd_new:
        sys.exit(cmd_new(config_path))
    if args.cmd_prompt_state:
        sys.exit(cmd_prompt_state())
    if args.cmd_show_config:
        sys.exit(cmd_config(config_path))
    if args.cmd_models or args.inline_goal == ["models"]:
        sys.exit(cmd_models(config_path))
    if args.cmd_save:
        save_path = args.cmd_save if isinstance(args.cmd_save, str) else None
        sys.exit(cmd_save(save_path))
    if args.cmd_list:
        sys.exit(cmd_list_templates(config_path))

    # ── Foreground interactive/inline: capture shell context first ────────
    _capture_shell_context()

    # ── File goal: handq --file <path> ───────────────────────────────────
    if args.file_goal:
        try:
            goal = Path(args.file_goal).read_text(encoding="utf-8")
        except Exception as exc:
            print(f"HandQ: cannot read file {args.file_goal!r}: {exc}", file=sys.stderr)
            sys.exit(1)
        hs = _get_handq_state()
        if hs == 0:
            _ensure_handq_dir()
            _set_handq_active(True)
            _push_tmux_status(1)
        _ensure_handq_tmux_session()  # enter tmux session (execvp if not inside)
        _start_idle_background(config_path)
        _send_message_to_task(goal)
        sys.exit(0)

    # ── Inline goal: handq <goal text...> ────────────────────────────────
    if args.inline_goal:
        goal = " ".join(args.inline_goal)
        hs = _get_handq_state()
        if hs == 0:
            _ensure_handq_dir()
            _set_handq_active(True)
            _push_tmux_status(1)
        _ensure_handq_tmux_session()  # enter tmux session (execvp if not inside)
        _start_idle_background(config_path)
        _send_message_to_task(goal)
        sys.exit(0)

    # ── Interactive mode — branch on current state ────────────────────────
    hs = _get_handq_state()
    _ensure_handq_tmux_session()  # enter tmux session (execvp if not inside)

    if hs == 0:
        # state0 → state1: start HandQ runtime, then immediately show goal dialog
        _enter_state1(config_path)
        _handle_state1(config_path)
    elif hs == 1:
        # state1: no active task — show goal-entry dialog
        _handle_state1(config_path)
    elif hs == 2:
        # state2: task running — show message dialog
        _handle_state2(config_path)
    elif hs == 3:
        # state3: task completed — show completion dialog
        _handle_state3(config_path)
    elif hs == 4:
        # state4: task running, confirmation pending — show confirmation dialog
        _handle_state2(config_path)


if __name__ == "__main__":
    main()
