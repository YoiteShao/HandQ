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
import os
import posixpath
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from .base_tool import BaseTool, ToolResult
from .cancellation import interruptible_sleep, run_with_abort
from .ssh_tool import (
    SshConnectionPool,
    _connect,
    _default_pool,
    _exec_command,
    _load_credentials,
)


# ── per-host discovery cache ─────────────────────────────────────────────────
# Keyed by "hostname:port" → resolved launch metadata from _discover().
_discovery_cache: Dict[str, Dict[str, str]] = {}

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
    """Return the remote user's $HOME. Used when _discover() has nothing to
    offer yet (handq_linux not found at all) but a deploy target dir is
    still needed."""
    stdout, _, _ = _remote_bash(creds, "echo $HOME", timeout=10.0)
    return stdout.strip() or "."


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery — where is handq_linux and how do we invoke it
# ─────────────────────────────────────────────────────────────────────────────
_PROBE = r"""
U=$(whoami); H=$(hostname -s 2>/dev/null || hostname); HM="$HOME"
echo "USER=$U"; echo "HOST=$H"; echo "HOME=$HM"
LSH=$(getent passwd "$U" 2>/dev/null | cut -d: -f7)
[ -z "$LSH" ] && LSH="$SHELL"
echo "LOGIN_SHELL=$LSH"
D="$HM/.handq/${U}@${H}"
echo "HANDQDIR=$D"
[ -d "$D" ] && echo "DIREXISTS=1" || echo "DIREXISTS=0"
PF="$D/handq.pid"
if [ -f "$PF" ]; then
  P=$(cat "$PF" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then echo "ALIVE=1"; else echo "ALIVE=0"; fi
else
  echo "ALIVE=0"
fi
# 1. Installed dispatcher (handq_setup.sh): canonical entry, on PATH or in
#    ~/.local/bin (the latter is usually NOT on a non-interactive SSH PATH).
#    It forwards "$@" and injects the per-host --config itself.
BIN=$(command -v handq_linux 2>/dev/null || true)
[ -z "$BIN" ] && [ -x "$HM/.local/bin/handq_linux" ] && BIN="$HM/.local/bin/handq_linux"
# 2/3. Un-installed copies in a common dir: a standalone Nuitka binary
#      (handq_linux.dist/handq_linux.bin auto-loads its dist-root config) or a
#      source checkout (handq_linux.py next to src/).
SBIN=""; SCRIPT=""
for r in "$HM/handq" "$HM/HandQ" "$HM" "$HM/.local/share/handq"; do
  [ -z "$SBIN" ] && [ -x "$r/handq_linux.dist/handq_linux.bin" ] && SBIN="$r/handq_linux.dist/handq_linux.bin"
  [ -z "$SBIN" ] && [ -x "$r/handq_linux.bin" ] && SBIN="$r/handq_linux.bin"
  [ -z "$SCRIPT" ] && [ -f "$r/handq_linux.py" ] && SCRIPT="$r/handq_linux.py"
done
PY=""
if [ -n "$SCRIPT" ]; then
  ROOT=$(dirname "$SCRIPT")
  for c in "$ROOT/.venv/bin/python3" "$ROOT/.venv/bin/python" "$ROOT/venv/bin/python3" "$ROOT/venv/bin/python"; do
    [ -x "$c" ] && { PY="$c"; break; }
  done
  [ -z "$PY" ] && PY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)
fi
echo "BIN=$BIN"; echo "SBIN=$SBIN"; echo "SCRIPT=$SCRIPT"; echo "PY=$PY"
"""


def _discover(creds: Dict[str, Any], *, force: bool = False) -> Dict[str, str]:
    """Probe the remote once for handq_dir + launch invocation. Cached per host.

    Returns {handq_dir, launch, remote_user, remote_host}. ``launch`` is the
    command prefix to invoke handq_linux, resolved in priority order:
      1. installed ``handq_linux`` dispatcher (injects the per-host --config),
      2. a standalone Nuitka binary (auto-loads its dist-root config),
      3. ``<python> <handq_linux.py>`` from a source checkout.
    Raises RuntimeError if handq_linux can't be found on the remote.
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
    stdout, _, _ = _remote_bash(creds, probe_inner, timeout=15.0)
    fields: Dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v

    handq_dir = fields.get("HANDQDIR", "")
    binary = fields.get("BIN", "")
    standalone = fields.get("SBIN", "")
    script = fields.get("SCRIPT", "")
    py = fields.get("PY", "python3") or "python3"

    if binary:
        launch = _shq(binary)
    elif standalone:
        launch = _shq(standalone)
    elif script:
        launch = f"{_shq(py)} {_shq(script)}"
    else:
        raise RuntimeError(
            f"handq_linux not found on {creds['hostname']}. Copy the built dist "
            f"package (handq_linux.dist/ + handq_config.yaml + handq_setup.sh) to "
            f"the remote and run 'bash handq_setup.sh' to install the handq_linux "
            f"command, or place a source checkout (handq_linux.py next to src/) in "
            f"~/handq/, then retry."
        )

    info = {
        "handq_dir": handq_dir,
        "launch": launch,
        "remote_user": fields.get("USER", ""),
        "remote_host": fields.get("HOST", ""),
        "remote_home": fields.get("HOME", ""),
        "login_shell": fields.get("LOGIN_SHELL", ""),
        "alive": fields.get("ALIVE", "0"),
        "dir_exists": fields.get("DIREXISTS", "0"),
    }
    _discovery_cache[hk] = info
    return info


def _shq(s: str) -> str:
    """Single-quote a string for safe embedding in a remote bash command."""
    return "'" + s.replace("'", "'\\''") + "'"


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


def _wake_daemon(creds: Dict[str, Any], info: Dict[str, str], config_path: str = "") -> bool:
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
    # nohup + setsid: detached from the SSH session's process group so it
    # survives the connection closing (and Windows power/network loss).
    wake = f"nohup setsid {launch} --_daemon{cfg} >/dev/null 2>&1 </dev/null & echo WOKE"
    _remote_bash(creds, wake, timeout=20.0)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if _daemon_alive(creds, handq_dir):
            return True
        if interruptible_sleep(POLL_INTERVAL):
            raise InterruptedError("remote_handq: wake-daemon wait aborted")
    return False


def _ensure_daemon(creds: Dict[str, Any], info: Dict[str, str], config_path: str = "") -> None:
    if _daemon_alive(creds, info["handq_dir"]):
        return
    if not _wake_daemon(creds, info, config_path):
        handq_dir = info["handq_dir"]
        diag = ""
        try:
            err_tail = _tail_remote_file(creds, posixpath.join(handq_dir, "daemon_error.txt"))
            log_tail = _tail_remote_file(creds, posixpath.join(handq_dir, "daemon.log"))
            if err_tail:
                diag += f"\n\n--- daemon_error.txt (tail) ---\n{err_tail.strip()}"
            if log_tail:
                diag += f"\n\n--- daemon.log (tail) ---\n{log_tail.strip()}"
        except Exception:
            pass  # diagnostics are best-effort; never mask the original failure
        raise RuntimeError(
            f"Failed to wake remote HandQ daemon on {creds['hostname']}. "
            f"Launch tried: {info['launch']} --_daemon.{diag}"
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
    """
    if not share_path:
        return None
    try:
        entries = os.listdir(share_path)
    except OSError:
        return None
    best: Optional[Tuple[Tuple[int, ...], str, str]] = None
    for name in entries:
        m = _VERSION_TARBALL_RE.match(name)
        if not m:
            continue
        version = m.group(1)
        parsed = _parse_version(version)
        if best is None or parsed > best[0]:
            best = (parsed, version, name)
    if best is None:
        return None
    _, version, name = best
    return version, os.path.join(share_path, name)


def _get_installed_version(creds: Dict[str, Any], info: Dict[str, str]) -> str:
    """Return the remote's installed handq_linux version, or "" on any failure."""
    stdout, _, rc = _remote_bash(creds, info['launch'] + ' --version', timeout=15.0)
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
[ -f "$STAGING/handq_setup.sh" ] && cp "$STAGING/handq_setup.sh" "$ROOT/handq_setup.sh"
rm -rf "$STAGING" "$BACKUP"
echo "STAGE=swap_ok"
"""

_DEPLOY_STAGE_MESSAGES = {
    "extract_failed": "failed to extract the package (corrupt transfer or disk full)",
    "binary_missing": "extracted package has no handq_linux.dist/handq_linux.bin — bad tarball",
    "verify_failed": "extracted binary failed to run (--version did not succeed) — old install left untouched",
}


def _deploy_linux_package(
    creds: Dict[str, Any], info: Dict[str, str], tarball_local_path: str, version: str,
) -> None:
    """Push a packaged Linux build to the remote and swap it into place.

    Target root is ``~/handq`` — the same un-installed-copy location the
    discovery probe already searches (see _PROBE), and the layout
    ``handq_linux.py``'s own frozen-config resolution expects (dist root one
    level above ``handq_linux.dist/``). The swap itself does NOT depend on
    ``handq_setup.sh`` — ``_discover()`` finds an un-installed dist directly
    via its SBIN probe, so the daemon is launchable immediately after the
    swap, before handq_setup.sh ever runs (see _install_human_aliases below).

    Extraction is staged and verified before touching the live install (see
    _DEPLOY_SCRIPT) — a bad transfer or broken build never deletes a working
    old version. Config comes from Windows' own live config (ConfigManager)
    with ``version`` explicitly forced to *version* (the tarball just
    deployed) rather than passed through — otherwise the remote's
    ``--version`` would echo back Windows' version instead of its own,
    silently breaking the next round of version comparison.

    After the swap, ``handq_setup.sh`` is invoked once more, best-effort, for
    its side effect only: installing the ``handq``/``hi`` aliases and PATH
    entry so a human who later logs in by hand gets the same command Windows
    already uses internally (_discover resolves an absolute path and never
    depends on PATH/aliases, so this step is purely a courtesy — its exit
    code and self-test results are deliberately ignored).
    """
    remote_home = info.get("remote_home") or "~"
    remote_root = posixpath.join(remote_home, "handq")
    remote_tmp = posixpath.join(remote_home, f".handq_deploy_{version}.tar.gz")
    staging_dir = posixpath.join(remote_root, f".handq_staging_{version}")
    backup_dir = posixpath.join(remote_root, ".handq_backup_dist")

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

    from ..infrastructure.config_manager import ConfigManager
    import yaml as _yaml
    local_config = dict(ConfigManager().get_config())
    local_config["version"] = version
    remote_config_path = posixpath.join(remote_root, "handq_config.yaml")
    _write_remote_file(creds, remote_config_path, _yaml.safe_dump(local_config, sort_keys=False))

    _install_human_aliases(creds, remote_root, remote_config_path)


def _install_human_aliases(creds: Dict[str, Any], remote_root: str, remote_config_path: str) -> None:
    """Best-effort: run handq_setup.sh so a human who logs in by hand later
    gets the handq/hi aliases + PATH entry. Never raises — this is pure
    convenience, not load-bearing for Windows' own control path (_discover
    finds the binary by absolute path regardless of whether this succeeds)."""
    setup_script = posixpath.join(remote_root, "handq_setup.sh")
    inner = (
        f"chmod +x {_shq(setup_script)} 2>/dev/null; "
        f"bash {_shq(setup_script)} --config {_shq(remote_config_path)} >/dev/null 2>&1 || true"
    )
    try:
        _remote_bash(creds, inner, timeout=60.0)
    except Exception:
        pass



def _ensure_installed(creds: Dict[str, Any]) -> Dict[str, str]:
    """Discover the remote; auto-deploy/upgrade if a newer package is on the
    configured share. No-op (existing behavior) when linux_share_path is
    blank or the discovered version is already current.

    Never redeploys while the daemon is alive: _deploy_linux_package rm -rf's
    handq_linux.dist/, which would yank files out from under a resident
    process. The stale build keeps serving until the daemon is next
    restarted (new_session / exit_handq), at which point the version check
    catches up.
    """
    try:
        info = _discover(creds)
        discover_exc: Optional[RuntimeError] = None
    except RuntimeError as exc:
        info = None
        discover_exc = exc

    if info is not None and _daemon_alive(creds, info["handq_dir"]):
        return info

    from ..infrastructure.config_manager import ConfigManager
    share_path = ConfigManager().get_section("update").get("linux_share_path", "") or ""
    latest = _resolve_linux_share_version(share_path)
    if latest is None:
        if info is None:
            # No share configured (or nothing on it) and nothing installed —
            # surface _discover's own error verbatim so behavior is unchanged
            # from before auto-deploy existed when the feature isn't in use.
            raise discover_exc
        return info

    latest_version, tarball_path = latest
    installed_version = _get_installed_version(creds, info) if info is not None else ""
    if info is None or _parse_version(installed_version) < _parse_version(latest_version):
        deploy_info = info if info is not None else {"remote_home": _probe_home(creds)}
        _deploy_linux_package(creds, deploy_info, tarball_path, latest_version)
        info = _discover(creds, force=True)
    return info


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
                "type": "string",
                "description": "[answer_confirmation] For a secret/text (ask_human) confirmation: the secret or text value to supply.",
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
    def _info(self, creds: Dict[str, Any], params: Dict[str, Any], *, force: bool = False) -> Dict[str, str]:
        info = _discover(creds, force=force)
        override = params.get("handq_dir", "")
        if override:
            info = dict(info)
            info["handq_dir"] = override
        return info

    def _info_ensured(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, str]:
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
        return {
            "handq_dir": handq_dir,
            "launch": info["launch"],
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
        }

    def _action_ensure_installed(self, creds: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            pre_info = _discover(creds)
        except RuntimeError:
            pre_info = None
        pre_version = _get_installed_version(creds, pre_info) if pre_info else ""
        info = self._info_ensured(creds, params)
        post_version = _get_installed_version(creds, info)
        return {
            "handq_dir": info["handq_dir"],
            "launch": info["launch"],
            "deployed": (pre_info is None) or (pre_version != post_version),
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
        self, creds: Dict[str, Any], info: Dict[str, str], text: str,
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
        if kind in ("secret", "text"):
            value = params.get("value")
            if value is None:
                raise ValueError(f"'value' is required to answer a '{kind}' confirmation.")
            resp["value"] = str(value)
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
        else:  # secret / text (ask_human)
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
