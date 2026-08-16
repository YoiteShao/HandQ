# -*- coding: utf-8 -*-
"""
Remote HandQ Tool — drive a remote Linux HandQ daemon over SSH (Windows side).

This is the Win→Linux control channel for the "one Windows HandQ controls many
Linux HandQ" design. Each Linux box runs ``handq_linux`` as a resident
``FlowControllerV2`` daemon (setsid-detached, no tmux/systemd). This tool speaks
that daemon's file-IPC protocol so the agent only deals in high-level actions —
submit a goal, read status, fetch the reply, interrupt, start a fresh session.

File IPC layout (mirrors ``handq_linux.py``), under ``~/.handq/<user>@<host>/``:
  state.json            daemon writes coarse status + latest_tool
  messages/<id>.txt     inbound goal / follow-up (we write here)
  commands/<id>.json    inbound new_session / interrupt (we write here)
  reply/<id>.txt        outbound reply, keyed by the message id we chose
  handq.pid             daemon liveness (pid + kill -0)

Wake model: if the daemon is not alive we launch it detached over SSH with
``nohup setsid <launch> --_daemon`` — it then survives Windows power / network
loss and is resumable. ``<launch>`` is discovered on the remote, in priority:
  1. the ``handq_linux`` command installed by ``handq_setup.sh`` (on PATH or in
     ``~/.local/bin``) — the canonical entry; it injects the per-host config;
  2. a standalone Nuitka binary (``handq_linux.dist/handq_linux.bin``) sitting
     un-installed in a common dir — auto-loads its dist-root config;
  3. a source checkout (``python3 <root>/handq_linux.py``), preferring a
     ``.venv``/``venv`` interpreter co-located with the script.

Prerequisite: handq_linux must be present on the remote. Two ways to get
there:
  1. Auto-deploy (opt-in) — set ``update.linux_share_path`` in the local
     config to a folder holding built ``handq-linux-<X.Y.Z>.tar.gz``
     packages (see ``packaging/build_linux.sh``). ``submit_goal`` and
     ``new_session`` then check the remote's installed version against the
     newest package on the share and push/upgrade automatically before
     proceeding — see ``_ensure_installed``. The remote's config is seeded
     from this Windows install's own live config (API key, model pool),
     never from a blank template or a prompt.
  2. Manual — copy the built dist package (``handq_linux.dist/`` +
     ``handq_config.yaml`` + setup) to the remote and run
     ``handq_setup.sh`` yourself; a source checkout (``handq_linux.py``
     next to ``src/``) also works. Always available, regardless of whether
     auto-deploy is configured.

Reuses the SSH connection pool + credential infrastructure from ssh_tool.py.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import re
import sys
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .base_tool import BaseTool, ToolResult
from .cancellation import interruptible_sleep, run_with_abort
from .ssh_tool import (
    SshConnectionPool,
    _connect,
    _default_pool,
    _exec_command,
    _load_credentials,
)

logger = logging.getLogger("handq.tools.remote_handq")


# ── per-host discovery cache ─────────────────────────────────────────────────
# Keyed by "hostname:port" → resolved root + launch metadata from _discover().
_discovery_cache: Dict[str, Dict[str, Any]] = {}

POLL_INTERVAL = 1.0  # seconds between reply/state polls when waiting

# The module-level helper functions below (_exec, _remote_bash, _discover,
# _wake_daemon, _deploy_linux_package, ...) are called many layers deep from
# RemoteHandQTool._execute_sync, which runs the WHOLE action synchronously on
# one dedicated executor thread (via asyncio.to_thread). Threading a `pool`
# parameter through every one of those functions would be a much larger,
# invasive diff for no behavioral difference — a thread-local set once at the
# top of _execute_sync is visible to all of them on that same thread, exactly
# mirroring cancellation.py's existing current_interrupt()/current_abort()
# thread-local pattern used elsewhere in this tool family.
_pool_threadlocal = threading.local()


def _current_pool() -> "SshConnectionPool":
    """Return the pool installed for this executor thread, or the
    test-only default (see ``_default_pool``'s own docstring — the live
    flow always installs ``ctx.ssh_pool`` before this is ever read)."""
    return getattr(_pool_threadlocal, "pool", None) or _default_pool


def _host_key(creds: Dict[str, Any]) -> str:
    return f"{creds['hostname']}:{creds.get('port', 22)}"


def _exec(creds: Dict[str, Any], command: str, timeout: float = 15.0) -> Tuple[str, str, int]:
    """Run one command on the remote via a pooled connection. (stdout, stderr, rc).

    Prefer ``_remote_bash`` for anything using POSIX shell syntax — see its
    docstring for why the bash wrapper is not optional.
    """
    with _connect(creds, _current_pool()) as (client, _):
        return _exec_command(client, command, timeout=timeout)


def _remote_bash(
    creds: Dict[str, Any], inner: str, timeout: float = 15.0,
) -> Tuple[str, str, int]:
    """Run *inner* on the remote wrapped in ``bash -c '<inner>'``.

    The bash wrapper is mandatory: paramiko's exec_command uses the remote
    user's login shell, which on some hosts is tcsh/csh — those don't grok
    POSIX numeric-fd redirections (``2>/dev/null``, ``2>&1``) and will emit
    ``Ambiguous output redirect.`` on the stderr channel with an empty
    stdout, which looks indistinguishable from "file not found" to callers.
    Everything remote-side goes through this helper so the shell semantics
    stay constant regardless of the login shell.
    """
    return _exec(creds, f"bash -c {_shq(inner)}", timeout=timeout)


def _probe_home(creds: Dict[str, Any]) -> str:
    """Return the remote user's $HOME.

    Only used for diagnostics now — the install root comes from ``_discover``
    (``info["root"]``), which resolves it on the remote side.
    """
    stdout, _, _ = _remote_bash(creds, "echo $HOME", timeout=10.0)
    return stdout.strip() or "."


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery — where is handq_linux and how do we invoke it
# ─────────────────────────────────────────────────────────────────────────────
_PROBE = r"""
U=$(whoami); H=$(hostname -s 2>/dev/null || hostname | cut -d. -f1); HM="$HOME"
echo "USER=$U"; echo "HOST=$H"; echo "HOME=$HM"
LSH=$(getent passwd "$U" 2>/dev/null | cut -d: -f7)
[ -z "$LSH" ] && LSH="$SHELL"
echo "LOGIN_SHELL=$LSH"

# ── The install root ────────────────────────────────────────────────────────
# The recorded root is the AUTHORITY. handq_setup.sh resolves the candidate chain
# once and writes the answer here; re-deriving it on every probe is how the three
# implementations (setup script, daemon, this probe) would drift apart.
HOSTCONF="$HM/.config/handq/hosts/$H"
ROOT=""
if [ -f "$HOSTCONF" ]; then
  ROOT=$(sed -n 's/^export HANDQ_ROOT="\(.*\)"$/\1/p' "$HOSTCONF" | head -1)
fi
if [ -n "$ROOT" ]; then
  echo "ROOT_SOURCE=recorded"
else
  # Nothing recorded yet: a first-ever install. Run the chain ourselves so the
  # caller has a deploy target. Deliberately does NOT mkdir — `discover` is a
  # read-only action, so an absent candidate is judged by its parent's
  # writability. handq_setup.sh does the real create-and-write probe when it
  # actually installs, and its answer then becomes the recorded authority.
  for c in "/local/mnt/workspace/${U}@handq" "/var/tmp/${U}@handq" "$HM/handq/${U}@${H}"; do
    if [ -L "$c" ]; then
      echo "ROOT_REJECT=$c: is a symlink"; continue
    fi
    if [ -e "$c" ]; then
      [ -d "$c" ] || { echo "ROOT_REJECT=$c: not a directory"; continue; }
      [ -O "$c" ] || { echo "ROOT_REJECT=$c: owned by another user"; continue; }
      [ -w "$c" ] || { echo "ROOT_REJECT=$c: not writable"; continue; }
    else
      P=$(dirname "$c")
      [ -d "$P" ] || { echo "ROOT_REJECT=$c: parent $P missing"; continue; }
      [ -w "$P" ] || { echo "ROOT_REJECT=$c: parent $P not writable"; continue; }
    fi
    ROOT="$c"; break
  done
  echo "ROOT_SOURCE=chain"
fi
echo "ROOT=$ROOT"

# ── Is the per-host dispatcher conf stale? ──────────────────────────────────
# This conf is what `handq` / `hi` source at the shell, and what records the root
# for every reader above. A conf written before the root relocation is a single
# bare command line with no `export HANDQ_ROOT=`, so the sed above yields "" and
# ROOT_SOURCE silently degrades to `chain`, while the alias still tries to exec a
# binary that may no longer exist ("<conf>: line 1: <path>: No such file or
# directory"). Report both facts so the caller can repair by re-running
# handq_setup.sh, whose rewrite of this file is unconditional. Read-only.
#
# Note there is deliberately NO "recorded root != resolved root" check: when
# ROOT_SOURCE=recorded the root came OUT of this file, so the two cannot disagree.
HCSTALE=0; HCBIN=""
if [ ! -f "$HOSTCONF" ]; then
  HCSTALE=1
else
  grep -q '^export HANDQ_ROOT=' "$HOSTCONF" 2>/dev/null || HCSTALE=1
  # First double-quoted absolute path on a non-export line: whatever the alias
  # actually exec's. Matches the current `exec "<bin>" --config …` form and the
  # older bare `"<bin>" …` one, and for a source checkout it lands on the
  # interpreter — which is still the right thing to test for existence.
  #
  # `^[^"]*"` — anchored, and consuming only NON-quote characters before the
  # first quote — is load-bearing. A leading `.*` is greedy, so it walked to the
  # LAST quoted path on the line and captured --config's argument instead: the
  # config file is not executable, so every healthy conf reported itself stale.
  HCBIN=$(grep -v '^export ' "$HOSTCONF" 2>/dev/null | sed -n 's/^[^"]*"\(\/[^"]*\)".*/\1/p' | head -1)
  if [ -z "$HCBIN" ]; then
    HCSTALE=1
  elif [ ! -x "$HCBIN" ]; then
    HCSTALE=1
  fi
fi
echo "HOSTCONF=$HOSTCONF"
echo "HOSTCONF_STALE=$HCSTALE"
echo "HOSTCONF_BIN=$HCBIN"

# ── Daemon state lives directly in the root ─────────────────────────────────
echo "HANDQDIR=$ROOT"
if [ -n "$ROOT" ] && [ -d "$ROOT" ]; then echo "DIREXISTS=1"; else echo "DIREXISTS=0"; fi
PF="$ROOT/handq.pid"
if [ -n "$ROOT" ] && [ -f "$PF" ]; then
  P=$(cat "$PF" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "ALIVE=1"; else echo "ALIVE=0"; fi
else
  echo "ALIVE=0"
fi

# ── The launch, resolved from the ROOT and nowhere else ─────────────────────
# Anything outside the root is legacy and reported separately below; it is never
# adopted as the launch. That inversion is what stops a stale wrapper on PATH
# (whose baked-in path still happens to run) from silently keeping this host on
# the old shared install forever.
RBIN=""; RSCRIPT=""; RPY=""
if [ -n "$ROOT" ]; then
  [ -x "$ROOT/handq_linux.dist/handq_linux.bin" ] && RBIN="$ROOT/handq_linux.dist/handq_linux.bin"
  [ -z "$RBIN" ] && [ -x "$ROOT/handq_linux.bin" ] && RBIN="$ROOT/handq_linux.bin"
  [ -f "$ROOT/handq_linux.py" ] && RSCRIPT="$ROOT/handq_linux.py"
  if [ -n "$RSCRIPT" ]; then
    for c in "$ROOT/.venv/bin/python3" "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python3" "$ROOT/venv/bin/python"; do
      [ -x "$c" ] && { RPY="$c"; break; }
    done
    [ -z "$RPY" ] && RPY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)
  fi
fi
echo "ROOTBIN=$RBIN"; echo "ROOTSCRIPT=$RSCRIPT"; echo "PY=$RPY"
[ -f "$ROOT/handq_config.yaml" ] && echo "ROOTCONFIG=$ROOT/handq_config.yaml" || echo "ROOTCONFIG="

# ── Legacy findings (reported, never adopted) ───────────────────────────────
# PATHBIN and LOCALBIN are reported SEPARATELY. They used to be collapsed into one
# field with `[ -z "$BIN" ] &&` fallback semantics, so a stale /usr/local/bin
# wrapper meant ~/.local/bin was never even considered.
echo "PATHBIN=$(command -v handq_linux 2>/dev/null || true)"
LOCALBIN=""
[ -x "$HM/.local/bin/handq_linux" ] && LOCALBIN="$HM/.local/bin/handq_linux"
echo "LOCALBIN=$LOCALBIN"
LEGACY_DIST=""
for r in "$HM/handq" "$HM/HandQ" "$HM" "$HM/.local/share/handq"; do
  [ -z "$LEGACY_DIST" ] && [ -x "$r/handq_linux.dist/handq_linux.bin" ] && LEGACY_DIST="$r/handq_linux.dist/handq_linux.bin"
done
echo "LEGACY_DIST=$LEGACY_DIST"
# A daemon still running out of the pre-migration IPC dir. This is the ONE legacy
# artefact that must be actively stopped: the new root has its own pid file, so
# without this the caller would wake a SECOND daemon on the same host, each
# serving its own port.
LEGACY_IPC="$HM/.handq/${U}@${H}"
[ -d "$LEGACY_IPC" ] && echo "LEGACY_IPC=$LEGACY_IPC" || echo "LEGACY_IPC="
LP="$LEGACY_IPC/handq.pid"
if [ -f "$LP" ]; then
  P=$(cat "$LP" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "LEGACY_ALIVE=1"; else echo "LEGACY_ALIVE=0"; fi
else
  echo "LEGACY_ALIVE=0"
fi
"""


def _discover(creds: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
    """Probe the remote once for the install root + launch invocation. Cached per host.

    Returns ``{root, root_source, handq_dir, launch, launch_ok, remote_user,
    remote_host, remote_home, login_shell, alive, dir_exists, legacy}``.

    **The root is authoritative.** ``launch`` is built from the root and nothing
    else — ``HANDQ_ROOT=<root> <root>/handq_linux.dist/handq_linux.bin --config
    <root>/handq_config.yaml`` (or the ``<python> handq_linux.py`` form for a
    source checkout in the root). The env assignment is explicit rather than
    relying on the dispatcher to export it, so waking the daemon does not depend
    on ``~/.local/bin`` being intact or on PATH resolution at all.

    This inverts what this function used to do. It previously built candidates
    from ``command -v handq_linux`` / an un-installed dist / a source checkout and
    took the first whose ``--version`` succeeded. With a cloud-synced ``$HOME``
    that let legacy win permanently: a pre-4.1 wrapper in ``/usr/local/bin`` whose
    baked path pointed at the old shared ``~/handq`` install still ran, so it was
    adopted, and the host stayed on the old install no matter what was deployed.
    Anything found outside the root is now reported under ``legacy`` for repair or
    warning, and never adopted.

    ``launch`` is ``""`` with ``launch_ok=False`` when the root holds no runnable
    entry point — the normal state before the first deploy. This function does
    **not** raise for that any more: "nothing installed yet" is an expected
    condition during migration, and raising discarded the ``legacy`` findings the
    caller needs (in particular a still-running pre-migration daemon).
    """
    hk = _host_key(creds)
    if not force and hk in _discovery_cache:
        return _discovery_cache[hk]

    # _PROBE is a multi-line POSIX script. Embedding its literal newlines
    # inside a single-quoted `bash -c` argument breaks under a tcsh/csh
    # login shell: sshd runs the command via `$SHELL -c "..."`, and tcsh's
    # line-oriented parser doesn't handle a quoted token that spans several
    # physical lines the way bash does — it loses track of the closing
    # quote and reinterprets the remaining lines as tcsh commands (surfaces
    # as "Unmatched '''." / "Illegal variable name." on the remote side).
    # Base64-transport the script instead, same idiom as
    # _write_remote_file: the payload becomes a single alnum/+//= token
    # with nothing left for any shell to misparse.
    b64 = base64.b64encode(_PROBE.encode("utf-8")).decode("ascii")
    probe_inner = f"printf %s {_shq(b64)} | base64 -d | bash"
    stdout, _, _ = _remote_bash(creds, probe_inner, timeout=20.0)
    fields: Dict[str, str] = {}
    rejects: List[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        # ROOT_REJECT can repeat, one line per refused candidate.
        if k == "ROOT_REJECT":
            rejects.append(v)
        else:
            fields[k] = v

    root = fields.get("ROOT", "")
    root_bin = fields.get("ROOTBIN", "")
    root_script = fields.get("ROOTSCRIPT", "")
    py = fields.get("PY", "python3") or "python3"
    root_config = fields.get("ROOTCONFIG", "")

    # Build the launch from the root. The config argument is only added when the
    # file is actually there: passing --config at a non-existent path would make
    # handq_linux fall back to its own resolution and mask the real problem.
    launch = ""
    if root and (root_bin or root_script):
        env_prefix = f"HANDQ_ROOT={_shq(root)} "
        if root_bin:
            launch = env_prefix + _shq(root_bin)
        else:
            launch = env_prefix + f"{_shq(py)} {_shq(root_script)}"
        if root_config:
            launch += f" --config {_shq(root_config)}"

    launch_ok = bool(launch) and _launch_runs(creds, launch)

    legacy = {
        "path_bin": fields.get("PATHBIN", ""),
        "local_bin": fields.get("LOCALBIN", ""),
        "dist": fields.get("LEGACY_DIST", ""),
        "ipc_dir": fields.get("LEGACY_IPC", ""),
        "daemon_alive": fields.get("LEGACY_ALIVE", "0") == "1",
    }

    info: Dict[str, Any] = {
        "root": root,
        "root_source": fields.get("ROOT_SOURCE", ""),
        "root_rejects": rejects,
        # Daemon state lives directly in the root now; kept under the old key so
        # every state.json / pid / messages path builder keeps working unchanged.
        "handq_dir": root,
        "launch": launch,
        # Whether `launch` actually ran (`--version` rc 0). False also covers
        # "the root has no entry point yet", which is normal pre-deploy.
        "launch_ok": launch_ok,
        "remote_user": fields.get("USER", ""),
        "remote_host": fields.get("HOST", ""),
        "remote_home": fields.get("HOME", ""),
        "login_shell": fields.get("LOGIN_SHELL", ""),
        "alive": fields.get("ALIVE", "0"),
        "dir_exists": fields.get("DIREXISTS", "0"),
        # The per-host dispatcher conf: where it is, whether it still points at
        # something runnable, and what it points at. Stale means `handq`/`hi` are
        # broken for a human at the shell AND that root_source fell back to the
        # candidate chain — see _repair_host_setup, which heals both.
        "hostconf": fields.get("HOSTCONF", ""),
        "hostconf_stale": fields.get("HOSTCONF_STALE", "0") == "1",
        "hostconf_bin": fields.get("HOSTCONF_BIN", ""),
        "legacy": legacy,
    }
    _discovery_cache[hk] = info
    return info


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in a remote bash command."""
    return "'" + s.replace("'", "'\\''") + "'"


def _launch_runs(creds: Dict[str, Any], launch: str) -> bool:
    """Does ``<launch> --version`` actually run (rc 0)?

    A dispatcher can resolve on PATH (``command -v`` / ``-x`` succeed) yet
    ``exec`` a binary that no longer exists on THIS host — the exact failure
    mode when ``~`` is cloud-synced across machines: ``~/.local/bin/handq_linux``
    and its per-host config ``~/.config/handq/hosts/<host>`` travel over intact,
    but the ``handq_linux.dist/handq_linux.bin`` they point at was built on a
    different box and isn't here. That resolves fine at discovery and only blows
    up at ``exec`` time (bash: "No such file or directory", rc 127) deep inside
    ``_wake_daemon``, as a cryptic wake failure.

    Probing ``--version`` — a clean, config-free ``exit(0)`` in handq_linux.py,
    already the fallback path in ``_get_installed_version`` — is the
    shell-agnostic way to tell a runnable launch from a ghost one BEFORE we
    depend on it. Any non-zero rc (127 for a broken exec, anything else for a
    crash) means "do not trust this launch".
    """
    try:
        _, _, rc = _remote_bash(creds, launch + " --version", timeout=15.0)
    except Exception:
        return False
    return rc == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Remote file primitives (base64 transport — safe for any goal text)
# ─────────────────────────────────────────────────────────────────────────────
def _write_remote_file(creds: Dict[str, Any], remote_path: str, content: str) -> None:
    """Atomically write *content* to *remote_path* (write .tmp then mv).

    Content is base64-encoded on this side and decoded remotely, so arbitrary
    text (quotes, newlines, unicode) transfers without shell-escaping hazards.
    """
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    d = posixpath.dirname(remote_path)
    tmp = remote_path + ".tmp"
    _inner = (
        f'mkdir -p {_shq(d)} && printf %s {_shq(b64)}'
        f' | base64 -d > {_shq(tmp)} && mv {_shq(tmp)} {_shq(remote_path)}'
    )
    _, stderr, rc = _remote_bash(creds, _inner, timeout=15.0)
    if rc != 0:
        raise RuntimeError(f"Failed to write {remote_path}: {stderr.strip()}")


def _read_remote_file(creds: Dict[str, Any], remote_path: str) -> Optional[str]:
    """Return file contents, or None if missing/empty.

    Routed through ``_remote_bash`` so the ``2>/dev/null`` fd-redirection
    parses correctly regardless of the remote user's login shell (tcsh/csh
    would otherwise emit "Ambiguous output redirect." and clobber stdout).
    """
    stdout, _, _ = _remote_bash(creds, f"cat {_shq(remote_path)} 2>/dev/null", timeout=15.0)
    return stdout if stdout.strip() else None


def _tail_remote_file(creds: Dict[str, Any], remote_path: str, n: int = 30) -> Optional[str]:
    """Return the last *n* lines of a remote file, or None if missing/empty.

    Used for failure diagnostics (daemon.log / daemon_error.txt) where the
    file can be large — ``tail`` avoids shipping the whole thing over SSH.
    Same login-shell caveat as ``_read_remote_file`` — go through
    ``_remote_bash`` so ``2>/dev/null`` is guaranteed to parse.
    """
    stdout, _, _ = _remote_bash(
        creds, f"tail -n {int(n)} {_shq(remote_path)} 2>/dev/null", timeout=15.0,
    )
    return stdout if stdout.strip() else None


def _sftp_put_file(creds: Dict[str, Any], local_path: str, remote_path: str) -> None:
    """Upload a local file to *remote_path* via real SFTP (not base64+bash).

    Reserved for large binary payloads (a Nuitka standalone dist tarball can
    be tens of MB) — base64 transport through ``_write_remote_file`` would
    inflate that by ~33% and route it through the exec channel instead of
    SFTP's own framing.
    """
    d = posixpath.dirname(remote_path)
    with _connect(creds, _current_pool()) as (client, _):
        if d:
            _exec_command(client, f"mkdir -p {_shq(d)}", timeout=15.0)
        sftp = client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()


def _read_state(creds: Dict[str, Any], handq_dir: str) -> Dict[str, Any]:
    raw = _read_remote_file(creds, posixpath.join(handq_dir, "state.json"))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _daemon_alive(creds: Dict[str, Any], handq_dir: str) -> bool:
    pf = posixpath.join(handq_dir, "handq.pid")
    _inner = (
        f'P=$(cat {_shq(pf)} 2>/dev/null); '
        '[ -n "$P" ] && kill -0 "$P" 2>/dev/null && echo ALIVE || echo DEAD'
    )
    stdout, _, _ = _remote_bash(creds, _inner, timeout=10.0)
    return stdout.strip() == "ALIVE"


#: Appended to daemon.log immediately before each wake so the failure path can
#: show only THIS attempt's output. daemon.log is append-only and shared by every
#: wake ever made against the host, so an un-scoped ``tail`` presents the oldest
#: surviving error as though it were the current one — which is exactly how a
#: fixed-and-redeployed failure keeps "reproducing" for the operator reading it.
_WAKE_MARKER = "--- handq wake ---"


def _legacy_ipc_candidate(info: Dict[str, Any]) -> str:
    """The pre-relocation IPC dir for this host: ``~/.handq/<user>@<shorthost>``.

    Derived from the probe's user/host/home rather than read from its
    ``LEGACY_IPC`` field, which only reports a dir that already existed when the
    probe ran. On a first wake against a pre-relocation build the daemon creates
    that dir moments AFTER the probe, so the recorded value is empty precisely
    when we need it. Falls back to the recorded value when the probe did not
    report enough to derive one.
    """
    home = info.get("remote_home") or ""
    user = info.get("remote_user") or ""
    host = info.get("remote_host") or ""
    if not (home and user and host):
        return (info.get("legacy") or {}).get("ipc_dir") or ""
    return posixpath.join(home, ".handq", f"{user}@{host}")


def _misplaced_daemon(creds: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    """Is a daemon alive in the LEGACY ipc dir instead of the install root?

    That is the signature of a build predating the root relocation: it ignores
    ``$HANDQ_ROOT`` and writes ``state.json``/``handq.pid`` under ``$HOME``, so
    every root-based liveness check reports DEAD even though the daemon started
    and is serving. Detecting it is what separates "the daemon is broken" from
    "the daemon is fine, the installed build puts its state somewhere else".

    Returns ``{ipc_dir, port}`` when found, ``{}`` otherwise. Never raises —
    this only ever runs on a path that is already failing.
    """
    ipc_dir = _legacy_ipc_candidate(info)
    if not ipc_dir or ipc_dir == (info.get("handq_dir") or ""):
        return {}
    try:
        if not _daemon_alive(creds, ipc_dir):
            return {}
        state = _read_state(creds, ipc_dir)
        return {
            "ipc_dir": ipc_dir,
            "port": int(state.get("remote_control_port") or 0),
        }
    except Exception:
        logger.debug("remote_handq: misplaced-daemon probe failed", exc_info=True)
        return {}


def _misplaced_diagnosis(stray: Dict[str, Any], info: Dict[str, Any]) -> str:
    port = stray.get("port") or 0
    return (
        f"\n\nThe daemon is actually up, but it wrote its state to {stray['ipc_dir']} "
        f"instead of the install root {info.get('handq_dir') or '?'}"
        + (f" (listening on port {port})" if port else "")
        + ". The handq_linux version installed on this machine"
        f" ({info.get('installed_version') or 'unknown'}) predates the root-relocation "
        "change and doesn't recognize $HANDQ_ROOT, so the Windows side can never find "
        "its pid/state by looking under the install root."
        "\nFix: rebuild the Linux package from current code (packaging/build_linux.sh) "
        "and drop it in update.linux_share_path; since the version number would be "
        "unchanged this won't trigger a redeploy, so bump the version first, or manually "
        "clear out the remote install root."
        "\nWorkaround: paste the CONNECT ME line from daemon.log below into the connect "
        "panel to pair manually."
    )


def _wake_daemon(creds: Dict[str, Any], info: Dict[str, Any], config_path: str = "") -> bool:
    """Launch the daemon detached and wait for its pid file. Returns aliveness.

    Raises InterruptedError if the wait is aborted by the engine's stop
    signal — distinct from a genuine deadline timeout (returns False),
    matching ssh_tool.py's convention (rate_limit / _new_client's retry
    backoff) of raising rather than folding "interrupted" and "timed out"
    into the same return value. _ensure_daemon lets this propagate instead
    of wrapping it in a "failed to wake daemon" RuntimeError, since an
    interrupt means the user asked to stop, not that the daemon is broken.
    """
    handq_dir = info["handq_dir"]
    launch = info["launch"]
    cfg = f" --config {_shq(config_path)}" if config_path else ""
    # Never spawn on top of a daemon that is already up in the legacy ipc dir.
    # A pre-relocation build ignores $HANDQ_ROOT, so root-based liveness reads
    # DEAD forever and every retry used to fork ANOTHER daemon — the observed
    # case left two live daemons (12:22 and 12:25) on one host, on different
    # ports, both writing the same $HOME state dir and clobbering each other's
    # state.json. Bail out with the diagnosis instead of adding to the pile.
    stray = _misplaced_daemon(creds, info)
    if stray:
        info["wake_diagnosis"] = _misplaced_diagnosis(stray, info)
        return False
    # nohup + setsid: detached from the SSH session's process group so it
    # survives the connection closing (and Windows power/network loss).
    #
    # Redirected to daemon.log (handq_linux.py's own DAEMON_LOG path — same
    # HANDQ_DIR this wake call already resolved via _discover), not
    # /dev/null. This daemon.log is the ONE the operator is told to check on
    # every failure path below (state.json timeout, LinuxBootstrapError) and
    # in the connect_v6_reference troubleshooting section — pointing them at
    # a file this exact wake discarded into /dev/null made every one of those
    # pointers a dead end for a Windows-initiated wake specifically (a
    # locally-started daemon's log was never affected, since cmd_daemon opens
    # DAEMON_LOG itself before this redirect would apply).
    log_path = posixpath.join(handq_dir, "daemon.log") if handq_dir else "/dev/null"
    # mkdir -p first: on a fresh install this directory doesn't exist yet (only
    # the daemon's own _ensure_dirs() creates it, and that runs AFTER this
    # redirect would already need it to exist) — without this, >> to a missing
    # directory fails and the backgrounded command dies before ever exec'ing
    # the binary, silently, with no diagnostic anywhere.
    mkdir_cmd = f"mkdir -p {_shq(handq_dir)}; " if handq_dir else ""
    # `env` is load-bearing, not decoration. `launch` opens with a shell
    # assignment prefix (HANDQ_ROOT=<root>, see _discover), and a prefix is only
    # an assignment when it precedes the command word of a simple command. Here
    # the command word is `nohup`, so without `env` the assignment degrades into
    # a plain argument: nohup hands it to setsid, which execvp()s it literally
    # and dies with "setsid: failed to execute HANDQ_ROOT=...: No such file or
    # directory" — before the binary is ever reached. `env` parses VAR=val args
    # itself, restoring the assignment semantics inside the detached process.
    #
    # This path is the only non-command-initial consumer of `launch`;
    # _launch_runs and _probe_version both put it at the start of the command,
    # where the bare prefix works. That asymmetry is why launch_ok can be True
    # while the wake fails, so do not "simplify" this back to `setsid {launch}`.
    wake = (
        f"{mkdir_cmd}printf '%s\\n' {_shq(_WAKE_MARKER)} >>{_shq(log_path)} 2>/dev/null; "
        f"nohup setsid env {launch} --_daemon{cfg} "
        f">>{_shq(log_path)} 2>&1 </dev/null & echo WOKE"
    )
    # Record what was ACTUALLY run so the failure path can report it verbatim.
    # It used to report `info['launch'] --_daemon`, which is a different string
    # from the one executed — and one that works when pasted into a shell by
    # hand, since there it IS command-initial. An operator debugging the setsid
    # failure above was therefore handed a command that could not reproduce it.
    info["last_wake_cmd"] = wake
    _remote_bash(creds, wake, timeout=20.0)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _daemon_alive(creds, handq_dir):
            return True
        if interruptible_sleep(POLL_INTERVAL):
            raise InterruptedError("remote_handq: wake-daemon wait aborted")
    # The root never got a pid file. Before reporting a dead daemon, check whether
    # the one we just spawned came up in the legacy ipc dir — on a first wake the
    # pre-relocation dir does not exist yet at probe time, so this is the only
    # point where that can be observed.
    stray = _misplaced_daemon(creds, info)
    if stray:
        info["wake_diagnosis"] = _misplaced_diagnosis(stray, info)
    return False


def _scope_to_last_wake(log_tail: str, keep: int = 40) -> Tuple[str, bool]:
    """Trim a daemon.log tail to the output of the most recent wake.

    Returns ``(text, is_current)``. ``is_current`` is False when no wake marker
    is present, which means every line shown predates this attempt — the caller
    labels it as history rather than presenting it as the current failure.

    This exists because daemon.log is append-only and shared by every wake ever
    made against the host. An un-scoped tail showed a months-old error above a
    successful start-up and read as the live diagnosis; a real fix looked like it
    had changed nothing.
    """
    if _WAKE_MARKER in log_tail:
        after = log_tail.rsplit(_WAKE_MARKER, 1)[1]
        lines = [ln for ln in after.splitlines() if ln.strip()]
        return "\n".join(lines[-keep:]).strip(), True
    lines = [ln for ln in log_tail.splitlines() if ln.strip()]
    return "\n".join(lines[-keep:]).strip(), False


def _ensure_daemon(creds: Dict[str, Any], info: Dict[str, Any], config_path: str = "") -> None:
    if _daemon_alive(creds, info["handq_dir"]):
        return
    if not info.get("launch"):
        legacy = info.get("legacy") or {}
        raise RuntimeError(
            f"handq_linux is not installed in the install root "
            f"({info.get('root') or 'unresolved'}) on {creds['hostname']}, so there "
            f"is nothing to wake. Configure update.linux_share_path so HandQ can "
            f"deploy it, or run 'bash handq_setup.sh' on that host."
            + (
                f"\n\nA pre-migration install exists at {legacy['dist']}. It is "
                f"outside the install root and is deliberately NOT used: the root is "
                f"machine-local, whereas that path is under a $HOME that is synced "
                f"between hosts. Deploying will migrate this host to the root and "
                f"leave the old copy alone."
                if legacy.get("dist") else ""
            )
        )
    if not _wake_daemon(creds, info, config_path):
        handq_dir = info["handq_dir"]
        diag = ""
        # Most specific first: "the daemon is up, just not where we look" is a
        # completely different problem from "the daemon will not start", and
        # leading with the raw log tail buried that distinction.
        diag += info.get("wake_diagnosis") or ""
        # The entry point in the root resolved but would not run. Say so up front,
        # with the fixes, before dumping the raw daemon.log tail — otherwise the
        # operator just sees "No such file or directory" with no hint why.
        if info.get("launch_ok") is False:
            diag += (
                "\n\nThe handq_linux entry point under the install root won't run "
                "(corrupt binary, missing dependency, or incompatible with this "
                "machine's glibc). Fix: configure update.linux_share_path so HandQ "
                "can auto-redeploy, or re-run handq_setup.sh on that host."
            )
        try:
            err_tail = _tail_remote_file(creds, posixpath.join(handq_dir, "daemon_error.txt"))
            log_tail = _tail_remote_file(creds, posixpath.join(handq_dir, "daemon.log"), n=200)
            if err_tail:
                diag += f"\n\n--- daemon_error.txt (tail) ---\n{err_tail.strip()}"
            if log_tail:
                scoped, is_current = _scope_to_last_wake(log_tail)
                label = "this wake" if is_current else "history, this wake produced no output"
                diag += f"\n\n--- daemon.log ({label}) ---\n{scoped}"
        except Exception:
            pass  # diagnostics are best-effort; never mask the original failure
        raise RuntimeError(
            f"Failed to wake remote HandQ daemon on {creds['hostname']}. "
            f"Command run: {info.get('last_wake_cmd') or (info['launch'] + ' --_daemon')}{diag}"
        )


def _new_msg_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


# ─────────────────────────────────────────────────────────────────────────────
#  Auto-deploy — push a packaged Linux build from a share when the remote is
#  missing handq_linux or is running an older version than what's available.
#  Opt-in: inert unless update.linux_share_path is set in the local config.
# ─────────────────────────────────────────────────────────────────────────────
_VERSION_TARBALL_RE = re.compile(r"^handq-linux-(\d+\.\d+\.\d+)\.tar\.gz$")
_INSTALLED_VERSION_RE = re.compile(r"handq_linux\s+(\S+)")


def _parse_version(s: str) -> Tuple[int, ...]:
    """Parse '1.2.0' → (1, 2, 0). Returns () on any failure — an empty tuple
    compares smaller than any real version, so an unparsable/missing version
    is treated as "very old" and forces a redeploy."""
    if not isinstance(s, str) or not s.strip():
        return ()
    try:
        return tuple(int(p) for p in s.strip().split("."))
    except ValueError:
        return ()


def _resolve_linux_share_version(share_path: str) -> Optional[Tuple[str, str]]:
    """Scan *share_path* for the highest-semver ``handq-linux-<X.Y.Z>.tar.gz``.

    Returns ``(version, local_tarball_path)``, or ``None`` if the share is
    blank, unreachable, or has no matching file. Mirrors electron/updater.js's
    ``scanLatestVersion`` — same file-naming convention, same "just read the
    filesystem" approach (a UNC path is a plain local path from Windows).

    Thin wrapper over :func:`_scan_linux_share` that discards the diagnostic —
    kept because several callers only want the answer. Prefer the scan function
    directly when you need to tell "nothing configured" from "path unreachable"
    from "package is misnamed", which is exactly what the operator needs to see
    when an expected upgrade doesn't happen.
    """
    result = _scan_linux_share(share_path)
    if result.version and result.tarball_path:
        return result.version, result.tarball_path
    return None


class _ShareScan(NamedTuple):
    """Outcome of scanning the Linux update share, with a reason when empty."""
    version: str            # highest semver found, "" if none usable
    tarball_path: str       # absolute path to that tarball, "" if none
    reason: str             # machine slug: ok | no_share | unreachable | no_match
    detail: str             # human line for the connect log


def _scan_linux_share(share_path: str) -> _ShareScan:
    """Scan the share and say WHY when it yields nothing.

    Every empty outcome here used to be an indistinguishable ``None`` that the
    caller turned into a silent "already current" — so an operator who dropped a
    new package and saw no upgrade had no way to tell a mistyped share path from
    a mis-named tarball from "actually up to date". The reason codes make the one
    connect-log line that explains it possible.
    """
    if not share_path:
        return _ShareScan("", "", "no_share",
                          "update.linux_share_path is not configured — skipping auto-upgrade check")
    try:
        entries = os.listdir(share_path)
    except OSError as exc:
        return _ShareScan(
            "", "", "unreachable",
            f"Share directory unreachable ({share_path}): {exc} — cannot check for a newer version",
        )
    best: Optional[Tuple[Tuple[int, ...], str, str]] = None
    saw_any_tarball = False
    for name in entries:
        if name.endswith(".tar.gz") and "handq-linux" in name:
            saw_any_tarball = True
        m = _VERSION_TARBALL_RE.match(name)
        if not m:
            continue
        version = m.group(1)
        parsed = _parse_version(version)
        if best is None or parsed > best[0]:
            best = (parsed, version, name)
    if best is None:
        if saw_any_tarball:
            detail = (
                f"Share directory has handq-linux packages, but none of the filenames "
                f"match the convention handq-linux-<X.Y.Z>.tar.gz "
                f"(e.g. v1.6.0 / 1.6 / -rc1 / .tgz are all ignored)"
            )
        else:
            detail = f"Share directory {share_path} has no handq-linux-<X.Y.Z>.tar.gz package"
        return _ShareScan("", "", "no_match", detail)
    _, version, name = best
    return _ShareScan(
        version, os.path.join(share_path, name), "ok",
        f"Latest version available on share directory: {version}",
    )


def _get_installed_version(creds: Dict[str, Any], info: Dict[str, Any]) -> str:
    """Return the version installed **in the root**, or "" when nothing is there.

    Reads ``<root>/handq_config.yaml``'s top-level ``version:``. The deployed
    config is the truth about what is installed under the root, so read it at the
    source rather than running ``<launch> --version`` (whose ``--config`` argument
    is pinned to whatever existed when setup last ran, and so reported the OLD
    config's version even right after a deploy wrote a new one — making every
    comparison see the install as perpetually stale).

    Two rules keep this from lying during the migration off the old shared
    ``~/handq`` layout, and both matter:

    * Only the ROOT's config counts. Reading ``~/handq/handq_config.yaml`` (the old
      path) would report the legacy install's version, the comparison would decide
      "already current", and the new root would **never be deployed to** — a
      deadlock that is completely silent, because nothing errors: it just quietly
      does nothing forever.
    * The ``<launch> --version`` fallback — kept for source-checkout layouts — is
      used only when the launch actually lives inside the root. Otherwise it would
      report a legacy binary's version and reintroduce the same deadlock by
      another route.

    Returns "" (meaning "not installed") when the root has no config, which is what
    makes the first deploy fire.
    """
    root = info.get("root") or info.get("handq_dir") or ""
    if not root:
        return ""
    config_path = posixpath.join(root, "handq_config.yaml")
    # grep the top-level `version:` line; tolerate quotes and surrounding space.
    stdout, _, rc = _remote_bash(
        creds,
        f"grep -E '^version:' {_shq(config_path)} 2>/dev/null | head -1",
        timeout=10.0,
    )
    if rc == 0 and stdout.strip():
        _, _, value = stdout.strip().partition(":")
        value = value.strip().strip("'\"")
        if _parse_version(value):
            return value
    # Fallback for source checkouts / unusual layouts — but only when the launch is
    # inside the root. See the docstring: a legacy launch here means a silent
    # never-upgrade deadlock.
    launch = info.get("launch") or ""
    if not launch or root not in launch:
        return ""
    stdout, _, rc = _remote_bash(creds, launch + ' --version', timeout=15.0)
    if rc != 0:
        return ""
    m = _INSTALLED_VERSION_RE.search(stdout)
    return m.group(1) if m else ""


# Deploy script: extract to a staging dir, verify the extracted binary
# actually launches, THEN atomically swap it into place. The live
# handq_linux.dist/ is never touched until the new one has proven it runs —
# a corrupt transfer or bad build aborts with the old install fully intact.
# Base64-transported like _PROBE (see its comment): a multi-line here-doc
# breaks under a tcsh/csh login shell, so the whole script travels as one
# opaque token and only ever runs through `| bash` on the far side.
_DEPLOY_SCRIPT = r"""
set -e
ROOT={root}
STAGING={staging}
BACKUP={backup}
TARBALL={tarball}

mkdir -p "$ROOT"
rm -rf "$STAGING"
mkdir -p "$STAGING"
if ! tar xzf "$TARBALL" -C "$STAGING" 2>&1; then
  echo "STAGE=extract_failed"
  rm -rf "$STAGING" "$TARBALL"
  exit 1
fi
rm -f "$TARBALL"

BIN="$STAGING/handq_linux.dist/handq_linux.bin"
if [ ! -x "$BIN" ]; then
  echo "STAGE=binary_missing"
  rm -rf "$STAGING"
  exit 1
fi

if ! VEROUT=$("$BIN" --version 2>&1); then
  echo "STAGE=verify_failed"
  echo "$VEROUT"
  rm -rf "$STAGING"
  exit 1
fi
echo "STAGE=verify_ok $VEROUT"

rm -rf "$BACKUP"
if [ -d "$ROOT/handq_linux.dist" ]; then
  mv "$ROOT/handq_linux.dist" "$BACKUP"
fi
mv "$STAGING/handq_linux.dist" "$ROOT/handq_linux.dist"
# handq_setup.sh travels alongside the binary dir in the tarball but isn't
# part of the runtime install — copy it over too so _install_human_aliases
# (and any human who wants to re-run it by hand) finds it at $ROOT.
# `if` rather than `[ -f … ] && cp …`: under `set -e` a bare AND-list whose test
# is FALSE makes the script exit 1 — after the swap has already succeeded — and
# the caller then reports a bogus "deploy script exited 1" for a deploy that
# actually worked.
if [ -f "$STAGING/handq_setup.sh" ]; then
  cp "$STAGING/handq_setup.sh" "$ROOT/handq_setup.sh"
fi
rm -rf "$STAGING" "$BACKUP"
echo "STAGE=swap_ok"
"""

_DEPLOY_STAGE_MESSAGES = {
    "extract_failed": "failed to extract the package (corrupt transfer or disk full)",
    "binary_missing": "extracted package has no handq_linux.dist/handq_linux.bin — bad tarball",
    "verify_failed": "extracted binary failed to run (--version did not succeed) — old install left untouched",
}


def _validated_local_config() -> Dict[str, Any]:
    """This controller's own config — the authoritative source for every remote's
    credentials and model pool — refusing to hand out one with a blank API key.

    A blank ``llm.API_KEY`` is never a deliberate thing to push. It is also
    invisible on the controller: with an empty key the Anthropic SDK falls back to
    ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` from the environment, so a
    Windows box whose config key went missing keeps working and nothing local
    complains. A Linux daemon has no such environment, so the same blank lands
    there as ``TypeError: Could not resolve authentication method`` on the first
    LLM call — retried for ~17 minutes before anyone hears about it.

    That is not hypothetical: an upgrade blanked this machine's key, the next
    deploy copied the blank over a remote key that had been working, and the
    remote went silent. Failing here keeps a controller that cannot authenticate
    from taking a remote down with it.
    """
    from ..infrastructure.config_manager import ConfigManager

    cfg = dict(ConfigManager().get_config())
    if not str((cfg.get("llm") or {}).get("API_KEY") or "").strip():
        raise RuntimeError(
            "This machine's handq_config.yaml has a blank llm.API_KEY — refusing to sync "
            "it to the remote (that would leave the remote HandQ completely unable to call "
            "the LLM). Fill in the API key under Settings -> LLM Configuration first, or "
            "edit ~/HandQ/handq_config.yaml directly. "
            "Note: this machine may keep working even with a blank key — the Anthropic SDK "
            "falls back to the ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN environment "
            "variables, but the remote machine has no such environment."
        )
    return cfg


def _deploy_linux_package(
    creds: Dict[str, Any], info: Dict[str, str], tarball_local_path: str, version: str,
) -> str:
    """Push a packaged Linux build to the remote and swap it into place.

    Target root is the machine-local install root resolved by ``_discover``
    (``info["root"]``) — NOT ``~/handq`` any more. Under a cloud-synced ``$HOME``
    that old target meant every host deployed into the same directory, so one
    host's upgrade swapped the dist out from under another host's running daemon
    (and the "don't deploy while alive" guard could not see it, because it checks
    only the local pid file). The root satisfies the layout
    ``handq_linux.py``'s frozen-config resolution expects (dist root one level
    above ``handq_linux.dist/``), so the daemon is launchable immediately after
    the swap, before handq_setup.sh ever runs.

    Extraction is staged and verified before touching the live install (see
    _DEPLOY_SCRIPT) — a bad transfer or broken build never deletes a working
    old version. Config comes from Windows' own live config
    (:func:`_validated_local_config`, which refuses a blank API key) with
    ``version`` explicitly forced to *version* (the tarball just
    deployed) rather than passed through — otherwise the remote's
    ``--version`` would echo back Windows' version instead of its own,
    silently breaking the next round of version comparison.

    After the swap, ``handq_setup.sh`` is invoked once more for its side effects:
    recording ``HANDQ_ROOT`` in the per-host dispatcher config, and installing the
    ``handq``/``hi`` aliases + PATH entry so a human who later logs in by hand gets
    the same command Windows uses internally.
    """
    remote_root = info.get("root") or ""
    if not remote_root:
        raise RuntimeError(
            f"Cannot deploy to {creds.get('hostname')}: no install root could be "
            f"resolved. None of /local/mnt/workspace/<user>@handq, "
            f"/var/tmp/<user>@handq or ~/handq/<user>@<host> was usable"
            + (
                " — " + "; ".join(info.get("root_rejects") or [])
                if info.get("root_rejects") else ""
            )
        )
    # Staging, backup and the uploaded tarball all live INSIDE the root, so they
    # are machine-local like everything else: two hosts can deploy concurrently
    # without touching each other's extraction, and a ~50MB tarball is no longer
    # written into a cloud-synced $HOME on every upgrade.
    remote_tmp = posixpath.join(remote_root, f".handq_deploy_{version}.tar.gz")
    staging_dir = posixpath.join(remote_root, f".handq_staging_{version}")
    backup_dir = posixpath.join(remote_root, ".handq_backup_dist")

    # Resolved BEFORE anything is uploaded or swapped: this raises when our own
    # API key is blank, and a remote left with a new binary plus an unusable
    # config is worse than one we never touched.
    local_config = dict(_validated_local_config())

    _sftp_put_file(creds, tarball_local_path, remote_tmp)

    script = _DEPLOY_SCRIPT.format(
        root=_shq(remote_root), staging=_shq(staging_dir),
        backup=_shq(backup_dir), tarball=_shq(remote_tmp),
    )
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    inner = f"printf %s {_shq(b64)} | base64 -d | bash"
    stdout, stderr, rc = _remote_bash(creds, inner, timeout=90.0)
    if rc != 0:
        stage = next((s for s in _DEPLOY_STAGE_MESSAGES if f"STAGE={s}" in stdout), "")
        reason = _DEPLOY_STAGE_MESSAGES.get(stage, f"deploy script exited {rc}")
        raise RuntimeError(
            f"Failed to deploy Linux HandQ {version} on {creds['hostname']}: "
            f"{reason}\n{stdout.strip()}\n{stderr.strip()}"
        )

    import yaml as _yaml
    local_config["version"] = version
    remote_config_path = posixpath.join(remote_root, "handq_config.yaml")
    _write_remote_file(creds, remote_config_path, _yaml.safe_dump(local_config, sort_keys=False))

    return _install_human_aliases(creds, remote_root, remote_config_path)


def _install_human_aliases(
    creds: Dict[str, Any], remote_root: str, remote_config_path: str
) -> str:
    """Best-effort: run handq_setup.sh so a human who logs in by hand later
    gets the handq/hi aliases + PATH entry. Never raises — this is pure
    convenience, not load-bearing for Windows' own control path (_discover
    finds the binary by absolute path regardless of whether this succeeds).

    Returns a short diagnostic string ("" on clean success) rather than
    swallowing everything into ``>/dev/null``. handq_setup.sh ``die``s before it
    writes the dispatcher whenever ``validate_config`` rejects the config, and
    that failure used to be completely invisible — the caller redirected both
    streams to /dev/null and ``except: pass``'d. When it fails the dispatcher
    keeps pointing at the previous binary, so a deploy that "succeeded" still
    launches the old version; the operator needs to see that, even though it
    must not abort the deploy.
    """
    setup_script = posixpath.join(remote_root, "handq_setup.sh")
    # --root is passed explicitly so setup does not re-run its own candidate chain
    # and cannot pick a different directory than the one we just deployed into.
    # It is also what records HANDQ_ROOT in the per-host dispatcher config, which
    # every later _discover reads as the authority.
    inner = (
        f"chmod +x {_shq(setup_script)} 2>/dev/null; "
        f"bash {_shq(setup_script)} --root {_shq(remote_root)} "
        f"--config {_shq(remote_config_path)} 2>&1"
    )
    try:
        stdout, stderr, rc = _remote_bash(creds, inner, timeout=60.0)
    except Exception as exc:
        return f"handq_setup.sh failed to run (aliases/PATH not updated, doesn't affect Windows control): {exc}"
    if rc != 0:
        tail = (stdout or stderr or "").strip().splitlines()[-3:]
        return (
            "handq_setup.sh failed (aliases/PATH not updated; if the dispatcher still "
            "points at the old binary, the handq command used for manual logins may be "
            "the old version): " + " / ".join(tail)
        )
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Repair — heal a per-host setup left behind by an older handq_setup.sh
# ─────────────────────────────────────────────────────────────────────────────
def _install_dir() -> Path:
    """Directory next to the entry point; the repo root in dev mode.

    Same algorithm as ``bridge_main._INSTALL_DIR`` and
    ``src/infrastructure/skills.py``'s ``_install_dir``, reimplemented for this
    module's own depth (``src/tools/`` is two levels under the repo root — the
    same depth as ``src/infrastructure/``).
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(__file__).parent.parent.parent.resolve()


def _local_setup_script() -> Tuple[str, str]:
    """The best ``handq_setup.sh`` we can lay hands on, plus where it came from.

    Returns ``(text, source)``, or ``("", why_not)`` when there is no source at
    all — never a silent empty string the caller could mistake for success.

    Two sources, in order:

    1. ``<install dir>/handq_setup.sh`` — present in a source checkout, and by
       definition the freshest copy.
    2. the ``handq_setup.sh`` member of the newest package on
       ``update.linux_share_path``. This fallback is what makes repair work in a
       packaged Windows build, which does **not** bundle the script:
       ``packaging/build.ps1`` ships only ``handq_config.yaml`` and
       ``uia_query.ps1`` as data files.

    Never raises.
    """
    local = _install_dir() / "handq_setup.sh"
    try:
        if local.is_file():
            return local.read_text(encoding="utf-8"), str(local)
    except Exception:
        logger.debug("remote_handq: reading %s failed", local, exc_info=True)

    try:
        from ..infrastructure.config_manager import ConfigManager
        share_path = ConfigManager().get_section("update").get("linux_share_path", "") or ""
        scan = _scan_linux_share(share_path)
        if scan.tarball_path:
            with tarfile.open(scan.tarball_path, "r:gz") as tf:
                # build_linux.sh tars from inside the dist dir, so the member is
                # bare — but tolerate the "./" prefix other tar builders emit.
                for name in ("handq_setup.sh", "./handq_setup.sh"):
                    try:
                        fh = tf.extractfile(name)
                    except KeyError:
                        continue
                    if fh is not None:
                        return (
                            fh.read().decode("utf-8"),
                            f"handq_setup.sh inside {scan.tarball_path}",
                        )
    except Exception:
        logger.debug("remote_handq: share extract of handq_setup.sh failed", exc_info=True)

    return "", (
        f"No usable handq_setup.sh found: local {local} doesn't exist, "
        f"and the share package didn't have it either — skipping repair"
    )


def _repair_host_setup(
    creds: Dict[str, Any], info: Dict[str, Any], log: Any
) -> str:
    """Rewrite the per-host dispatcher conf, the dispatcher and the handq/hi
    symlinks by re-running handq_setup.sh on the remote.

    Why push and run the real script instead of writing the two-line conf from
    here: that format is handq_setup.sh's to own. Its own comment at the write
    site calls the recorded root "the single authority ... what keeps three
    implementations from drifting apart" — a fourth implementation living in
    Python is precisely the drift being warned about. Running the script is also
    copy-free once it sits in the root: ``stage_into_root`` returns immediately
    when ``PKG_DIR == HANDQ_ROOT``, so this rewrites configuration without
    touching the installed binary.

    The pushed script matters: the copy already on the remote can predate
    ``--root`` and reject it with a usage dump (rc 2), which is how this host got
    a stale conf in the first place.

    Never raises. Returns "" on clean success, else a short diagnostic.
    """
    root = info.get("root") or ""
    if not root:
        return "Cannot repair per-host config: no install root has been resolved yet"

    text, source = _local_setup_script()
    if not text:
        return source

    setup_script = posixpath.join(root, "handq_setup.sh")
    try:
        _write_remote_file(creds, setup_script, text)
    except Exception as exc:
        return f"Failed to push handq_setup.sh: {exc}"

    note = _install_human_aliases(creds, root, posixpath.join(root, "handq_config.yaml"))
    if note:
        return note
    log(
        f"Re-ran handq_setup.sh using {source} — rewrote "
        f"{info.get('hostconf') or 'the per-host config'} + dispatcher + handq/hi aliases"
    )
    return ""


def _drop_legacy_ipc_dir(creds: Dict[str, Any], info: Dict[str, Any], log: Any) -> None:
    """Remove ``~/.handq/<user>@<host>`` once nothing is running out of it.

    This is safe to delete where the pre-migration INSTALL is not: the path
    carries ``@<host>``, so it belongs to this machine alone, whereas the install
    lives in a ``$HOME`` that is cloud-synced between hosts and may still be
    another, not-yet-migrated host's live copy. handq_setup.sh refuses to delete
    that one for the same reason and only prints an info line; this function
    keeps that refusal and does not touch it either.

    Liveness is re-checked immediately before the ``rm``, not taken from the
    probe: a human can start a daemon between the two, and deleting the dir from
    under a live daemon strands it — its pid/state files vanish while it keeps
    serving a port nobody can then discover.
    """
    ipc_dir = _legacy_ipc_candidate(info)
    if not ipc_dir or ipc_dir == (info.get("handq_dir") or ""):
        return
    try:
        present, _, _ = _remote_bash(
            creds, f"[ -d {_shq(ipc_dir)} ] && echo YES || echo NO", timeout=10.0,
        )
        if present.strip() != "YES":
            return
        if _daemon_alive(creds, ipc_dir):
            log(
                f"The legacy layout's IPC dir {ipc_dir} still has a daemon running — "
                f"skipping deletion this time; it will be cleaned up on the next "
                f"connect after it stops"
            )
            return
        _remote_bash(creds, f"rm -rf {_shq(ipc_dir)}", timeout=20.0)
        log(
            f"Cleaned up the legacy layout's leftover IPC dir {ipc_dir}"
            f" (the old install directory is left alone: $HOME syncs across machines, "
            f"another host may still be using it)"
        )
    except Exception:
        logger.debug("remote_handq: legacy ipc cleanup failed", exc_info=True)


def _ensure_installed(
    creds: Dict[str, Any],
    *,
    on_log: Optional[Any] = None,
    allow_deploy_when_alive: bool = False,
) -> Dict[str, Any]:
    """Discover the remote and decide what the installed-vs-available versions
    mean, ALWAYS — then deploy only when it is safe to.

    The returned ``info`` always carries the decision so a caller can act on it
    without re-deriving it:
      * ``installed_version`` / ``share_version`` — what is on disk, what is on
        the share (either may be "" when unknown).
      * ``upgrade_available`` — a newer package exists on the share.
      * ``deployed`` — this call actually swapped a new build in.

    The previous version returned early the instant the daemon was alive,
    without ever comparing versions — so a running daemon could never learn a
    newer package existed, and "I dropped a new build but nothing upgraded" had
    no diagnosable cause. The comparison now always happens; what stays gated on
    daemon liveness is the *deploy*, because ``_deploy_linux_package`` rm -rf's
    ``handq_linux.dist/`` and must not pull files from under a live process.
    ``allow_deploy_when_alive`` is for the caller that has already stopped the
    daemon (see linux_bootstrap): the "don't deploy while alive" guard would
    otherwise still refuse, having re-probed a daemon the caller just bounced.

    Legacy handling (migration off the old shared ``~/handq`` layout): a daemon
    still running out of the pre-migration IPC dir is stopped first — see
    :func:`_stop_legacy_daemon`. The old install itself is deliberately NOT
    deleted; it may be the live install of another host that has not migrated yet,
    and the delete would be unrecoverable.

    ``on_log(message)`` — optional sink for one-line operator-facing notes
    (share scan result, installed version, decision). Every branch that used to
    ``return`` silently now says why through here.
    """
    def _log(msg: str) -> None:
        if on_log is not None:
            try:
                on_log(msg)
            except Exception:
                pass

    # _discover no longer raises for "nothing installed yet" — that is the normal
    # state before the first deploy, and raising discarded the legacy findings
    # needed below.
    info: Dict[str, Any] = dict(_discover(creds))
    legacy = info.get("legacy") or {}

    if not info.get("root"):
        raise RuntimeError(
            f"No usable HandQ install root on {creds.get('hostname')}. Tried "
            f"/local/mnt/workspace/<user>@handq, /var/tmp/<user>@handq and "
            f"~/handq/<user>@<host>"
            + (
                ": " + "; ".join(info.get("root_rejects") or [])
                if info.get("root_rejects") else ""
            )
        )

    # A daemon from the OLD layout is still running. It must be stopped before we
    # start one at the new root, or the host ends up with two daemons serving two
    # different ports off two different installs.
    if legacy.get("daemon_alive"):
        _stop_legacy_daemon(creds, info, _log)
        legacy["daemon_alive"] = False

    # Heal a per-host setup left behind by an older handq_setup.sh. Gated on the
    # probe's staleness flag, so once healed this costs one dict lookup. It sits
    # BEFORE the share scan deliberately: a stale conf breaks `handq`/`hi` for a
    # human at the shell and demotes root_source to the candidate chain, neither
    # of which has anything to do with whether a newer package exists — tying the
    # repair to an available upgrade is what would keep a same-version host broken
    # forever.
    if info.get("hostconf_stale"):
        _log(
            f"Per-host config is stale ({info.get('hostconf') or '?'} points at "
            f"{info.get('hostconf_bin') or 'nothing'}, so handq/hi will fail with "
            f"No such file or directory) — repairing with the current handq_setup.sh"
        )
        note = _repair_host_setup(creds, info, _log)
        if note:
            _log(note)
        else:
            # Re-probe so hostconf_stale / root_source reflect the repair rather
            # than the state that triggered it.
            info = dict(_discover(creds, force=True))
            legacy = info.get("legacy") or {}

    # Historical leftover from the old shared layout. Gated on the probe having
    # actually seen the directory, so healthy hosts pay nothing.
    if legacy.get("ipc_dir"):
        _drop_legacy_ipc_dir(creds, info, _log)

    from ..infrastructure.config_manager import ConfigManager
    share_path = ConfigManager().get_section("update").get("linux_share_path", "") or ""
    scan = _scan_linux_share(share_path)
    _log(scan.detail)

    installed_version = _get_installed_version(creds, info)
    info["installed_version"] = installed_version
    info["share_version"] = scan.version
    info["upgrade_available"] = bool(
        scan.version
        and _parse_version(installed_version) < _parse_version(scan.version)
    )
    info["deployed"] = False

    # Nothing installed at the root yet. Say so explicitly: with the old layout
    # still present on disk this is the migration case, and it is worth naming
    # rather than letting it look like a fresh machine.
    if not installed_version and (legacy.get("dist") or legacy.get("ipc_dir")):
        _log(
            f"This machine is still on the old layout ({legacy.get('dist') or legacy.get('ipc_dir')}) — "
            f"installing into the new machine-local root {info['root']}. The old directory "
            f"will not be deleted: another not-yet-migrated machine may still be using it"
        )

    if not scan.version:
        # Nothing to deploy FROM. With no install at the root either, this is a
        # hard failure — and now it can say precisely what is missing.
        if not info.get("launch"):
            raise RuntimeError(
                f"handq_linux is not installed at {info['root']} on "
                f"{creds.get('hostname')}, and update.linux_share_path has no "
                f"package to deploy from. Copy the built dist package "
                f"(handq_linux.dist/ + handq_config.yaml + handq_setup.sh) to the "
                f"remote and run 'bash handq_setup.sh', or configure "
                f"update.linux_share_path so it can be deployed automatically."
                + (
                    f" (A pre-migration install exists at {legacy['dist']}, but it "
                    f"is outside the install root and is deliberately not adopted.)"
                    if legacy.get("dist") else ""
                )
            )
        return info

    alive = _daemon_alive(creds, info["handq_dir"])
    # launch_ok False now means "the root has no runnable entry point" — either
    # nothing is installed there yet (migration / fresh host) or what is there is
    # broken. Both want a deploy. It is checked as part of needs_deploy so it runs
    # BEFORE the "remote is already up to date" version-match short-circuit below.
    launch_broken = not info.get("launch_ok", False)
    needs_deploy = bool(info.get("upgrade_available")) or launch_broken

    if launch_broken and scan.version:
        if info.get("launch"):
            _log(
                f"The handq_linux entry point under the install root won't run — "
                f"redeploying {scan.version} to fix it"
            )
        else:
            _log(f"Nothing installed yet under root {info['root']} — deploying {scan.version}")

    if not needs_deploy:
        _log(f"Remote is already up to date ({installed_version or 'unknown'})")
        return info

    if alive and not allow_deploy_when_alive:
        # A redeploy is warranted (newer package, or a broken launch) but the
        # daemon is running. Do NOT deploy here — _deploy_linux_package rm -rf's
        # handq_linux.dist/ and must not pull files from under a live process.
        # The caller decides whether it is safe to bounce it (see
        # linux_bootstrap._require_idle_or_forced). Report the decision instead
        # of silently returning the stale info.
        #
        # Note this check is now sound for the multi-machine case in a way it
        # never used to be: the root is machine-local, so the pid file being read
        # belongs to the only daemon that can possibly be running out of this
        # install. Under the old shared ~/handq the guard was structurally blind
        # to a sibling host's live daemon and would happily delete the dist out
        # from under it.
        if launch_broken:
            _log(
                f"The launch path won't run and needs to redeploy {scan.version}, but the "
                f"daemon is running — leaving the decision to restart-to-fix to the caller"
            )
        else:
            _log(
                f"Found newer version {scan.version} (current {installed_version or 'unknown'}), "
                f"but the daemon is running — leaving the decision to restart-to-upgrade to the caller"
            )
        return info

    _log(
        f"Deploying handq_linux {scan.version} to {info['root']}"
        + (f" (overwriting {installed_version})" if installed_version else " (fresh install)")
    )
    setup_note = _deploy_linux_package(creds, info, scan.tarball_path, scan.version)
    if setup_note:
        _log(setup_note)
    info = dict(_discover(creds, force=True))
    info["installed_version"] = _get_installed_version(creds, info)
    info["share_version"] = scan.version
    info["upgrade_available"] = False
    info["deployed"] = True
    return info


def _stop_legacy_daemon(
    creds: Dict[str, Any], info: Dict[str, Any], log: Any
) -> None:
    """Stop a daemon still running out of the pre-migration ``~/.handq`` IPC dir.

    This is the one legacy artefact that has to be actively dealt with rather than
    just ignored. The new root has its own pid file, so a probe of the root reports
    "no daemon" while the old one is still alive and serving — wake the new one and
    the host is running two daemons off two installs, each on its own port, with
    the Windows side talking to one and the operator's console possibly to the
    other.

    Uses the legacy dispatcher if one is still on PATH (it injects the old
    ``--config`` and knows the old root); falls back to SIGTERM on the pid from the
    legacy pid file. Never raises: failing to stop the old daemon must not block
    the migration, but it is logged loudly because the two-daemon state it leaves
    behind is confusing to debug.
    """
    legacy = info.get("legacy") or {}
    ipc_dir = legacy.get("ipc_dir") or ""
    launch = legacy.get("path_bin") or legacy.get("local_bin") or ""
    log(
        "Detected a daemon still running under the legacy layout — stopping it first, "
        "otherwise this host would run two daemons at once (each on its own port)"
    )
    if launch:
        try:
            _remote_bash(creds, _shq(launch) + " --exit 2>&1 || true", timeout=25.0)
        except Exception:
            logger.debug("remote_handq: legacy --exit failed", exc_info=True)
    if ipc_dir:
        pf = posixpath.join(ipc_dir, "handq.pid")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not _daemon_alive(creds, ipc_dir):
                log("Old daemon has stopped")
                return
            time.sleep(0.5)
        # --exit did not take. SIGTERM the pid directly rather than leaving two
        # daemons up.
        try:
            _remote_bash(
                creds,
                f'P=$(cat {_shq(pf)} 2>/dev/null); '
                '[ -n "$P" ] && kill -TERM "$P" 2>/dev/null || true',
                timeout=15.0,
            )
        except Exception:
            logger.debug("remote_handq: legacy SIGTERM failed", exc_info=True)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not _daemon_alive(creds, ipc_dir):
                log("Old daemon has stopped (SIGTERM)")
                return
            time.sleep(0.5)
        log(
            f"Warning: the old daemon ({pf}) failed to stop. The new daemon will still "
            f"start, but this host will have two daemons running at once — please handle "
            f"this manually"
        )


class RemoteHandQTool(BaseTool):
    """Drive a remote Linux HandQ daemon over SSH.

    First call to a host: pass ``ssh_target`` (no ``credentials_file``). The
    tool establishes SSH credentials on the fly (key auth → OS keyring →
    one-time password prompt, cached for future sessions) and returns the
    resolved ``credentials_file`` path in the result — reuse that path on
    subsequent calls instead of ``ssh_target``.

    Actions:
      discover      Locate handq_linux + report daemon state
      ensure_installed  Deploy/upgrade handq_linux from update.linux_share_path if configured
      submit_goal   Wake daemon if needed + queue a goal (optionally wait for reply)
      send_message  Queue a follow-up message into the running task
      get_status    Read state.json (task_status, status_text, latest_tool)
      get_result    Fetch reply/<message_id>.txt for a previously submitted goal
      get_confirmation    Read a pending risk/tool/secret/ask_human request, if any
      answer_confirmation Answer a pending confirmation so the remote task resumes
      new_session   Tell the daemon to start a fresh session
      interrupt     Abort the in-flight task (clears any queued follow-ups)
      exit_handq    Stop the remote daemon
    """

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover", "ensure_installed", "submit_goal", "send_message",
                    "get_status", "get_result", "get_confirmation", "answer_confirmation",
                    "new_session", "interrupt", "exit_handq",
                ],
                "description": "Action to perform on the remote HandQ daemon.",
            },
            "credentials_file": {
                "type": "string",
                "description": (
                    "Path to a local SSH credentials YAML file. Omit this on your "
                    "FIRST call to a host and pass ssh_target instead — the tool "
                    "establishes credentials on the fly and returns the resolved "
                    "path for you to reuse."
                ),
            },
            "ssh_target": {
                "type": "string",
                "description": (
                    "'user@host' or bare host/IP. Use this INSTEAD of credentials_file "
                    "on your first call to a host you haven't connected to yet — the "
                    "tool establishes SSH credentials (key / OS keyring / one-time "
                    "password prompt) and returns 'credentials_file' in the result. "
                    "Pass that resolved path on subsequent calls instead of ssh_target."
                ),
            },
            "goal": {
                "type": "string",
                "description": "[submit_goal] Task description to submit to the remote agent.",
            },
            "message_id": {
                "type": "string",
                "description": "[get_result] The id returned by submit_goal/send_message, used to fetch its reply.",
            },
            "confirmation_id": {
                "type": "string",
                "description": "[answer_confirmation] The id from get_confirmation/get_status's pending_confirmation, identifying which request to answer.",
            },
            "decision": {
                "type": "string",
                "enum": ["yes", "no", "message"],
                "description": "[answer_confirmation] For a tool/risk confirmation: 'yes' to approve, 'no' to refuse, 'message' to refuse with guidance (requires 'message').",
            },
            "value": {
                "description": (
                    "[answer_confirmation] For a 'secret' confirmation: the "
                    "secret string to supply. For a 'form' (ask_human) "
                    "confirmation: a JSON object mapping each field's id to "
                    "its answer (array of strings for checkbox fields, "
                    "plain string otherwise)."
                ),
            },
            "reason": {
                "type": "string",
                "description": "[interrupt] Optional human-readable reason recorded for the interrupt.",
            },
            "message": {
                "type": "string",
                "description": "[send_message] Follow-up message to inject into the running task. [answer_confirmation] Guidance text when decision='message'.",
            },
            "config_path": {
                "type": "string",
                "description": "Remote config path passed as --config when waking the daemon (optional; the daemon auto-loads a co-located handq_config.yaml).",
            },
            "handq_dir": {
                "type": "string",
                "description": "Override the remote HANDQ_DIR if already known (skips part of discovery).",
            },
            "wait_timeout": {
                "type": "number",
                "description": "[submit_goal/send_message/get_status/get_result] Max seconds to wait (0=return immediately). Default: 0.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None):
        super().__init__("remote_handq", ctx=ctx)
        # Per-session connection pool from the SessionContext, same as
        # ssh_tool.py's StatelessSSHTool — ctx=None test fixtures fall back to
        # the module-level default so connections/close() lifecycle stay
        # isolated per session instead of leaking across them.
        self.pool: SshConnectionPool = (
            ctx.ssh_pool if ctx is not None else _default_pool
        )
        self.interrupt_event = ctx.interrupt_event if ctx is not None else None

    async def execute(self, **params) -> ToolResult:
        creds_file = params.get("credentials_file", "")
        ssh_target = params.get("ssh_target", "")
        newly_established = False

        if not creds_file and ssh_target:
            from ..infrastructure.ssh_setup import (
                ensure_ssh_credentials_lazy, SSHSetupError,
            )
            im = self.ctx.interaction_manager if self.ctx is not None else None
            try:
                creds_file = await ensure_ssh_credentials_lazy(ssh_target, im)
                newly_established = True
                params = {**params, "credentials_file": creds_file}
            except SSHSetupError as exc:
                return self._fail(
                    params,
                    f"Failed to establish SSH credentials for '{ssh_target}': {exc}",
                )
        elif not creds_file:
            return self._fail(
                params,
                "Either 'credentials_file' (existing) or 'ssh_target' (to establish new) is required.",
            )

        def _run() -> ToolResult:
            # Installs this session's pool on the executor thread so every
            # module-level helper (_exec, _remote_bash, _discover, ...) that
            # opens a connection several call-frames down reaches THIS
            # session's pool via _current_pool(), not the shared test-only
            # default. Mirrors cancellation.py's own thread-local pattern.
            _pool_threadlocal.pool = self.pool
            try:
                return self._execute_sync(**params)
            finally:
                _pool_threadlocal.pool = None

        # Inner handlers are blocking paramiko I/O — run off the event loop.
        # run_with_abort mirrors self.interrupt_event into a thread-safe
        # token so the polling loops in _wake_daemon/_poll_reply (via
        # interruptible_sleep) can self-abort at their next check point
        # instead of riding out the full poll timeout after a stop signal.
        result = await run_with_abort(
            _run,
            interrupt_event=self.interrupt_event,
            shutdown_deadline=self.shutdown_deadline,
        )
        if newly_established and isinstance(result.output, dict):
            result.output["credentials_file"] = creds_file
            result.output["credentials_file_note"] = (
                "Newly established — pass this exact path as credentials_file "
                "on subsequent calls to the same host."
            )
        return result

    def _execute_sync(self, **params) -> ToolResult:
        action = params.get("action", "")
        creds_file = params.get("credentials_file", "")

        # Anchor a relative credentials_file path to the per-session workspace
        # rather than the process cwd. _load_credentials only does
        # os.path.expanduser; without this resolve a bare "creds.yaml" would
        # be looked up under the install dir (process cwd is no longer the
        # session workspace — see concurrency work). A freshly-established
        # path is already absolute so this resolve is a no-op for it.
        resolved_creds_file = self.resolve_in_workspace(creds_file)
        try:
            creds = _load_credentials(resolved_creds_file)
        except Exception as e:
            return self._fail(params, f"Failed to load credentials: {e}")

        handler = {
            "discover": self._action_discover,
            "ensure_installed": self._action_ensure_installed,
            "submit_goal": self._action_submit_goal,
            "send_message": self._action_send_message,
            "get_status": self._action_get_status,
            "get_result": self._action_get_result,
            "get_confirmation": self._action_get_confirmation,
            "answer_confirmation": self._action_answer_confirmation,
            "new_session": self._action_new_session,
            "interrupt": self._action_interrupt,
            "exit_handq": self._action_exit_handq,
        }.get(action)
        if not handler:
            return self._fail(
                params,
                f"Unknown action: {action}. Valid: discover, ensure_installed, submit_goal, "
                f"send_message, get_status, get_result, get_confirmation, "
                f"answer_confirmation, new_session, interrupt, exit_handq",
            )
        try:
            return ToolResult(
                success=True, output=handler(creds, params),
                tool_name=self.name, tool_parameters=params,
            )
        except Exception as e:
            return self._fail(params, f"{type(e).__name__}: {e}")

    def _fail(self, params: Dict[str, Any], error: str) -> ToolResult:
        return ToolResult(
            success=False, output=None,
            tool_name=self.name, tool_parameters=params, error=error,
        )

    # ── discovery / dir resolution ───────────────────────────────────────────
    def _info(self, creds: Dict[str, Any], params: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
        info = _discover(creds, force=force)
        override = params.get("handq_dir", "")
        if override:
            info = dict(info)
            info["handq_dir"] = override
        return info

    def _info_ensured(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Like _info, but auto-deploys/upgrades first when a newer package
        is available on update.linux_share_path. Used only by the two
        actions that actually need a runnable daemon (submit_goal,
        new_session) — discovery/status/send_message stay read-only."""
        info = _ensure_installed(creds)
        override = params.get("handq_dir", "")
        if override:
            info = dict(info)
            info["handq_dir"] = override
        return info

    # ── actions ────────────────────────────────────────────────────────────
    def _action_discover(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params, force=True)
        handq_dir = info["handq_dir"]
        alive = _daemon_alive(creds, handq_dir)
        state = _read_state(creds, handq_dir) if alive else {}
        legacy = info.get("legacy") or {}
        return {
            # The machine-local install root. handq_dir is the same path (daemon
            # state lives directly in the root) — both kept for callers that key
            # on either name.
            "root": info.get("root", ""),
            "root_source": info.get("root_source", ""),
            "handq_dir": handq_dir,
            "launch": info["launch"],
            "launch_ok": info.get("launch_ok", False),
            "remote_user": info.get("remote_user", ""),
            "remote_hostname": info.get("remote_host", ""),
            # The remote user's login shell (from getent passwd / $SHELL). Only
            # a diagnostic hint — this tool routes everything remote-side
            # through `bash -c` internally, so the login shell doesn't affect
            # any action on this tool. Surfaced so callers writing their own
            # SSH commands to the same host know to wrap in bash themselves
            # when the shell isn't bash (tcsh/csh don't grok `2>/dev/null`).
            "login_shell": info.get("login_shell", ""),
            "daemon_alive": alive,
            "session_id": state.get("session_id", ""),
            "task_status": state.get("task_status", ""),
            # Migration visibility: a pre-migration install/daemon still on disk.
            # Reported so an operator can see WHY a host is being migrated and
            # whether an old daemon is (still) running that will be stopped.
            "legacy_install": legacy.get("dist", ""),
            "legacy_ipc_dir": legacy.get("ipc_dir", ""),
            "legacy_daemon_alive": bool(legacy.get("daemon_alive")),
            # The per-host dispatcher conf backing `handq` / `hi`. Stale means a
            # human at the shell gets "<conf>: line 1: <path>: No such file or
            # directory", and that root_source fell back to the candidate chain.
            "hostconf": info.get("hostconf", ""),
            "hostconf_stale": bool(info.get("hostconf_stale")),
            "hostconf_bin": info.get("hostconf_bin", ""),
        }

    def _action_ensure_installed(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        pre_info = _discover(creds)
        pre_version = _get_installed_version(creds, pre_info)
        info = self._info_ensured(creds, params)
        post_version = _get_installed_version(creds, info)
        return {
            "root": info.get("root", ""),
            "handq_dir": info["handq_dir"],
            "launch": info["launch"],
            "deployed": bool(info.get("deployed")) or (pre_version != post_version),
            "old_version": pre_version,
            "new_version": post_version,
        }

    def _action_submit_goal(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        goal = params.get("goal", "")
        if not goal:
            raise ValueError("'goal' is required for submit_goal.")
        info = self._info_ensured(creds, params)
        _ensure_daemon(creds, info, params.get("config_path", ""))
        return self._post_and_maybe_wait(creds, info, goal, params, label="Goal")

    def _action_send_message(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        message = params.get("message", "")
        if not message:
            raise ValueError("'message' is required for send_message.")
        info = self._info(creds, params)
        if not _daemon_alive(creds, info["handq_dir"]):
            raise RuntimeError("Remote HandQ daemon is not running; use submit_goal to start a task first.")
        return self._post_and_maybe_wait(creds, info, message, params, label="Message")

    def _post_and_maybe_wait(
        self, creds: Dict[str, Any], info: Dict[str, Any], text: str,
        params: Dict[str, Any], *, label: str,
    ) -> Dict[str, Any]:
        handq_dir = info["handq_dir"]
        msgid = _new_msg_id()
        _write_remote_file(creds, posixpath.join(handq_dir, "messages", f"{msgid}.txt"), text)
        out: Dict[str, Any] = {
            "message_id": msgid,
            "handq_dir": handq_dir,
            "note": f"{label} queued. Use get_result with this message_id to fetch the reply, "
                    f"or get_status to monitor progress.",
        }
        wait = float(params.get("wait_timeout", 0) or 0)
        if wait > 0:
            reply = self._poll_reply(creds, handq_dir, msgid, wait)
            if reply is not None:
                out["reply"] = reply
                out["note"] = f"{label} processed; reply attached."
            else:
                out["note"] = f"{label} queued; timed out after {int(wait)}s waiting for the reply (task may still be running)."
        return out

    def _action_get_status(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        wait = float(params.get("wait_timeout", 0) or 0)
        deadline = time.time() + wait
        while True:
            # A crashed daemon leaves a stale state.json (only graceful stop()
            # unlinks it), so cross-check liveness before trusting the status.
            alive = _daemon_alive(creds, handq_dir)
            state = _read_state(creds, handq_dir)
            status = self._format_status(state)
            status["daemon_alive"] = alive
            if not alive:
                status["handq_active"] = False
                status["note"] = "Daemon is not alive; state.json may be stale."
                return status
            # alive=True but state.json is unreadable → read path is broken,
            # not "daemon has nothing to say". state.json is rewritten on
            # every snapshot; a healthy daemon must produce non-empty state.
            # Common causes: remote read plumbing regressed (e.g., a shell
            # incompatibility in _read_remote_file), or file permissions
            # changed. Surface loudly instead of returning empty fields that
            # look indistinguishable from an idle daemon.
            if not state:
                status["handq_active"] = False
                status["_read_path_failed"] = True
                status["note"] = (
                    "Daemon is alive but state.json is unreadable or empty. "
                    "This usually means the remote read path failed (e.g., login "
                    "shell doesn't grok POSIX redirections, or file permissions "
                    f"changed). Cross-check on-remote: cat {handq_dir}/state.json"
                )
                return status
            # Surface a pending confirmation so the agent knows it must answer
            # before the task can make progress. A confirmation can only be
            # pending while a task runs (the agent blocks mid-item on the
            # response file), so skip the extra round-trip when idle.
            pending = (
                self._pending_confirmation(creds, handq_dir)
                if status.get("task_status") == "running" else None
            )
            if pending:
                status["pending_confirmation"] = pending
                status["note"] = (
                    "A confirmation is pending — call answer_confirmation with "
                    "this confirmation_id so the remote task can continue."
                )
                return status
            if wait <= 0 or status["task_status"] == "idle" or time.time() >= deadline:
                if wait > 0 and status["task_status"] == "running":
                    status["note"] = f"Timed out after {int(wait)}s — task still running."
                return status
            if interruptible_sleep(max(POLL_INTERVAL, 2.0)):
                return status

    def _action_get_result(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        msgid = params.get("message_id", "")
        if not msgid:
            raise ValueError("'message_id' is required for get_result (the id returned by submit_goal/send_message).")
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        wait = float(params.get("wait_timeout", 0) or 0)
        reply = self._poll_reply(creds, handq_dir, msgid, wait) if wait > 0 else \
            _read_remote_file(creds, posixpath.join(handq_dir, "reply", f"{msgid}.txt"))
        state = _read_state(creds, handq_dir)
        # A dead daemon with no reply means the result will never arrive; tell
        # the agent so it doesn't keep polling a stale 'running' status.
        alive = _daemon_alive(creds, handq_dir)
        out: Dict[str, Any] = {
            "message_id": msgid,
            "found": reply is not None,
            "reply": reply or "",
            "task_status": state.get("task_status", ""),
            "session_id": state.get("session_id", ""),
            "daemon_alive": alive,
        }
        if not alive and reply is None:
            out["note"] = "Daemon is not alive and no reply was found; the task may have died with the daemon."
        # alive + missing reply + empty state → the reply file may already
        # exist on disk but the read path can't see it. Same failure mode as
        # _action_get_status: don't disguise a broken pipe as "still running".
        elif alive and reply is None and not state:
            out["_read_path_failed"] = True
            out["note"] = (
                "Daemon is alive but state.json is unreadable AND no reply was "
                "found — the remote read path may be broken. The reply file may "
                "already exist on-remote; cross-check: "
                f"cat {handq_dir}/reply/{msgid}.txt"
            )
        return out

    def _action_get_confirmation(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        pending = self._pending_confirmation(creds, handq_dir)
        if not pending:
            return {"pending": False, "note": "No confirmation is pending on the remote daemon."}
        return {"pending": True, **pending}

    def _action_answer_confirmation(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        handq_dir = info["handq_dir"]
        cid = params.get("confirmation_id", "")
        if not cid:
            raise ValueError("'confirmation_id' is required for answer_confirmation (from get_confirmation/get_status).")
        req = self._read_confirmation_request(creds, handq_dir)
        if not req:
            return {"success": False, "note": "No confirmation is pending (it may have already been answered or timed out)."}
        pending_id = req.get("id", "")
        if pending_id and pending_id != cid:
            return {
                "success": False,
                "note": f"confirmation_id mismatch: the pending request is {pending_id!r}, not {cid!r}. "
                        f"Call get_confirmation to read the current request.",
            }
        kind = req.get("kind", "")
        resp: Dict[str, Any] = {"id": cid}
        if kind == "secret":
            value = params.get("value")
            if value is None:
                raise ValueError(f"'value' is required to answer a '{kind}' confirmation.")
            resp["value"] = str(value)
        elif kind == "form":
            value = params.get("value")
            if not isinstance(value, dict):
                raise ValueError(
                    "'value' must be a JSON object mapping field id to answer "
                    "for a 'form' confirmation."
                )
            resp["value"] = value
        else:  # tool / risk
            decision = str(params.get("decision", "")).strip().lower()
            if decision not in ("yes", "no", "message"):
                raise ValueError("'decision' must be 'yes', 'no', or 'message' for a tool/risk confirmation.")
            resp["decision"] = decision
            if decision == "message":
                msg = params.get("message", "")
                if not msg:
                    raise ValueError("'message' is required when decision='message'.")
                resp["message"] = str(msg)
        self._write_confirmation_response(creds, handq_dir, resp)
        return {
            "success": True, "confirmation_id": cid, "kind": kind,
            "note": "Answer delivered; the remote task will resume.",
        }

    def _action_new_session(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info_ensured(creds, params)
        was_running = (
            _daemon_alive(creds, info["handq_dir"])
            and _read_state(creds, info["handq_dir"]).get("task_status") == "running"
        )
        _ensure_daemon(creds, info, params.get("config_path", ""))
        self._post_command(creds, info["handq_dir"], {"action": "new_session"})
        note = "Fresh session requested on the remote daemon."
        if was_running:
            note += (
                " Warning: a task was in flight and has been aborted — "
                "new_session tears down the old session entirely."
            )
        return {"success": True, "note": note}

    def _action_interrupt(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        if not _daemon_alive(creds, info["handq_dir"]):
            return {"success": False, "note": "Daemon not running; nothing to interrupt."}
        was_running = _read_state(creds, info["handq_dir"]).get("task_status") == "running"
        self._post_command(creds, info["handq_dir"], {
            "action": "interrupt",
            "reason": params.get("reason", "remote interrupt"),
        })
        if was_running:
            note = "Interrupt queued; the in-flight task will be aborted and its pending tail cleared."
        else:
            note = "No task is in flight; cleared any pending tail (no-op if none)."
        return {"success": True, "note": note}

    def _action_exit_handq(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        info = self._info(creds, params)
        hk = _host_key(creds)
        _remote_bash(creds, info['launch'] + ' --exit 2>&1 || true', timeout=20.0)
        _discovery_cache.pop(hk, None)
        return {"success": True, "note": "Remote HandQ daemon shutdown requested."}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _post_command(self, creds: Dict[str, Any], handq_dir: str, cmd: Dict[str, Any]) -> None:
        cid = _new_msg_id()
        _write_remote_file(
            creds,
            posixpath.join(handq_dir, "commands", f"{cid}.json"),
            json.dumps(cmd, ensure_ascii=False),
        )

    def _read_confirmation_request(self, creds: Dict[str, Any], handq_dir: str) -> Optional[Dict[str, Any]]:
        raw = _read_remote_file(creds, posixpath.join(handq_dir, "confirmation_request.json"))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _write_confirmation_response(self, creds: Dict[str, Any], handq_dir: str, resp: Dict[str, Any]) -> None:
        _write_remote_file(
            creds,
            posixpath.join(handq_dir, "confirmation_response.json"),
            json.dumps(resp, ensure_ascii=False),
        )

    def _pending_confirmation(self, creds: Dict[str, Any], handq_dir: str) -> Optional[Dict[str, Any]]:
        """Shape a pending confirmation_request.json for the host, or None."""
        req = self._read_confirmation_request(creds, handq_dir)
        if not req:
            return None
        kind = req.get("kind", "")
        shaped: Dict[str, Any] = {
            "confirmation_id": req.get("id", ""),
            "kind": kind,
        }
        if kind == "risk":
            shaped["description"] = req.get("description", "")
            shaped["how_to_answer"] = "answer_confirmation with decision=yes|no|message (message needs 'message')."
        elif kind == "tool":
            shaped["tool_name"] = req.get("tool_name", "")
            shaped["hint"] = req.get("hint", "")
            shaped["params"] = req.get("params")
            shaped["how_to_answer"] = "answer_confirmation with decision=yes|no|message (message needs 'message')."
        elif kind == "form":
            shaped["question"] = req.get("question", "")
            shaped["fields"] = req.get("fields", [])
            shaped["how_to_answer"] = (
                "answer_confirmation with 'value' set to a JSON object mapping "
                "each field's id to its answer (an array of strings for "
                "checkbox fields, a plain string otherwise)."
            )
        else:  # secret
            shaped["prompt"] = req.get("prompt", "")
            shaped["how_to_answer"] = "answer_confirmation with 'value' set to the requested input."
        return shaped

    def _poll_reply(self, creds: Dict[str, Any], handq_dir: str, msgid: str, timeout: float) -> Optional[str]:
        """Wait for the *settled* reply for ``msgid`` (None on timeout / death).

        The reply file passes through two states for a task (Fix 1): while the
        task runs it holds only the plan-ack placeholder, and the authoritative
        completion summary overwrites it once the task settles. We must not
        return the placeholder, so each poll reads ``state.json`` BEFORE the
        reply file and returns only when both hold:
          * ``task_status == "idle"`` — the task has settled, and
          * the reply file exists.

        Read-state-before-reply matters: the orchestrator emits the ``idle``
        state change immediately before writing the final reply
        (``_emit_completion_reply``: notify idle → on_reply_to_user). Reading
        state first, then crossing the SSH round-trip to read the reply, absorbs
        that sub-millisecond gap — by the time our ``cat reply`` lands, the
        final summary has been written. The empty-body case (no completed item)
        never fires the reply sink, so the placeholder under an ``idle`` status
        is the correct fallback there.
        """
        path = posixpath.join(handq_dir, "reply", f"{msgid}.txt")
        deadline = time.time() + timeout
        while True:
            state = _read_state(creds, handq_dir)
            task_status = state.get("task_status", "")
            reply = _read_remote_file(creds, path)
            if reply is not None and task_status == "idle":
                return reply
            # A crashed daemon will never settle; hand back whatever exists
            # (the placeholder, or None) so the caller can report accurately.
            if not _daemon_alive(creds, handq_dir):
                return reply
            if time.time() >= deadline:
                return None
            if interruptible_sleep(max(POLL_INTERVAL, 2.0)):
                return reply

    @staticmethod
    def _format_status(state: Dict[str, Any]) -> Dict[str, Any]:
        """Shape state.json into the host-facing status (latest_tool + status_text)."""
        return {
            "handq_active": state.get("handq_active", False),
            "task_status": state.get("task_status", ""),
            "status_text": state.get("status_text", ""),
            "session_id": state.get("session_id", ""),
            "working_dir": state.get("working_dir", ""),
            "latest_tool": state.get("latest_tool"),
            "last_updated": state.get("last_updated", ""),
        }
