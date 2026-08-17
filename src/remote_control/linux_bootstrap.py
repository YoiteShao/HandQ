"""Linux auto-pairing — get a control address without walking to the other machine.

Manual pairing works on Linux exactly as it does on Windows: the daemon prints
``CONNECT ME: handq://…`` and the operator copies it. But for a Linux box the
address can be *fetched* instead, because HandQ already has an authenticated
channel to it — the SSH connection ``remote_handq_tool`` uses to install and
start ``handq_linux``. This module walks that path once:

    discover → install/upgrade if needed → start the daemon →
    read state.json → return handq://host:port/token

and from then on the direct connection is used for everything. SSH goes back to
being a deployment tool, which is the role the design doc argues it is actually
good at.

``remote_handq_tool`` is deliberately **imported, not modified**. Its
module-level ``_discover`` / ``_ensure_installed`` / ``_ensure_daemon`` /
``_read_state`` / ``_remote_bash`` helpers are reused verbatim, and the tool's
own eleven actions keep working unchanged — it stays in service this phase,
and a rewrite of it is out of scope. The only new requirement it places on the
Linux side is two extra fields in ``state.json``, which
``StateMirror.snapshot`` now writes.

Notably absent: any config patching. A Linux daemon **always** serves the
direct channel (``handq_linux._start_remote_control`` does not read
``remote_control.serve``), because a machine with no local UI has nobody to
opt in on its behalf. That deletes what used to be the most dangerous code
here — SFTP-patching ``serve: true`` into the remote config and then having to
judge whether it was acceptable to SIGTERM a daemon mid-task just to make the
setting take effect.

Two subtleties this module still owns:

* **A stale version needs a restart, and that IS still gated.**
  ``_ensure_installed`` skips its own upgrade check whenever the daemon is
  alive (by design — it never rm -rf's a live install's files), so a daemon
  that predates ``remote_control`` support can otherwise never upgrade itself.
  Forcing that requires stopping it, which is only safe when nothing is
  running: :func:`_require_idle_or_forced` checks ``state.json``'s
  ``task_status`` and refuses with :class:`LinuxDaemonBusyError` rather than
  guessing. ``force=True`` is the named, explicit override.

* **state.json is written milliseconds AFTER the pid file.**
  ``_ensure_daemon`` polls the pid file to detect liveness, but the daemon
  writes state.json (and only then the remote_control_port/token fields) after
  ``_start_remote_control`` returns — a small but real gap. Read-once would
  intermittently see empty fields on a fast machine, and reporting that as an
  error would be a phantom failure. We poll for the fields for a bounded time
  instead.

Everything here is blocking paramiko I/O, so :func:`resolve_linux_address` hands
the work to a thread and installs the SSH pool into that thread's
``_pool_threadlocal`` — the same pattern ``RemoteHandQTool.execute`` uses,
required because the helpers open connections several frames down and reach the
pool through a thread-local rather than an argument.
"""
from __future__ import annotations

import asyncio
import logging
import posixpath
import time
from typing import Any, Dict, NamedTuple, Optional

from .address import ControlAddress

logger = logging.getLogger("handq.remote_control.linux_bootstrap")

#: How long to wait for state.json to reflect the running daemon's
#: remote_control_port/token after the pid file appears. Small — the daemon
#: writes them straight after start_remote_control returns, so this only
#: covers scheduling jitter, not a network round-trip.
STATE_POLL_TIMEOUT_SEC = 8.0
STATE_POLL_INTERVAL_SEC = 0.5

#: How long to wait for a --exit to actually take the daemon down (unlinks the
#: pid file). Longer than STATE_POLL because Python + Nuitka shutdown can carry
#: cleanup work (LLM services closing, file handles).
EXIT_POLL_TIMEOUT_SEC = 12.0

#: The oldest installed version known to publish remote_control_port/token in
#: state.json. Anything older will accept a `remote_control:` config section
#: silently (unknown top-level keys are just ignored) and never bind a port —
#: which otherwise surfaces as a content-free "state.json never got the
#: fields" timeout with no hint that the real problem is simply an old binary.
#: Bump this if remote_control ships in a later minor than expected.
MIN_VERSION_WITH_REMOTE_CONTROL = (1, 5, 5)


def _exc_text(exc: BaseException) -> str:
    """Render *exc* for an operator-facing message, never as the empty string.

    Argument-less exceptions stringify to ``""`` (``str(EOFError())`` being the
    case that actually bit us here), which turns ``f"...: {exc}"`` into a
    message with nothing after the colon. Fall back to the class name.
    """
    return str(exc) or exc.__class__.__name__


class LinuxBootstrapError(RuntimeError):
    """Raised with an operator-actionable message."""


class LinuxDaemonBusyError(LinuxBootstrapError):
    """Raised when the remote daemon has a task in flight and the caller did
    not explicitly ask to interrupt it. Distinct from LinuxBootstrapError so a
    UI can offer a specific "force anyway" retry instead of just showing text.
    """


class LinuxAddressResult(NamedTuple):
    """What auto-pair resolved: the control address plus any deferred upgrade.

    ``upgrade_pending`` is ``{}`` in the normal case and
    ``{from, to, reason}`` when a newer package was found on the share but not
    installed because the remote was busy — the caller surfaces it as a panel
    banner and re-offers the upgrade once the machine drains (see
    ``hub.pair_linux_over_ssh``).
    """
    address: ControlAddress
    upgrade_pending: Dict[str, Any]


async def resolve_linux_address(
    *,
    ssh_target: str = "",
    credentials_file: str = "",
    interaction_manager: Optional[Any] = None,
    install: bool = True,
    name: str = "",
    force: bool = False,
    on_log: Optional[Any] = None,
) -> LinuxAddressResult:
    """Bring up ``handq_linux`` on a host over SSH and return its control address.

    ``ssh_target`` (``user@host``) establishes credentials on first use, prompting
    for a password through ``interaction_manager`` and storing it in the OS
    keyring — the same lazy flow ``remote_handq_tool`` uses, so a host already set
    up for that tool needs no new credentials. Pass ``credentials_file`` instead
    to reuse a known ``~/.ssh/handq_<host>.yaml``.

    ``install=False`` skips the deploy/upgrade step, for a host known to be
    current — that step can copy a tarball and is the slow part.

    ``force=False`` (the default): if the daemon needs restarting to upgrade and
    a task or remote session is running there, the upgrade is DEFERRED (returned
    in ``upgrade_pending``) rather than interrupting it. Pass ``force=True`` only
    after the operator has been shown that and explicitly chosen to restart.

    ``on_log(message)`` — optional callback for operator-facing upgrade-decision
    lines. It is invoked from a worker thread (the bootstrap is blocking), so it
    is marshalled back onto this loop with ``call_soon_threadsafe`` before being
    called; a caller can pass a plain synchronous sink and not think about
    threads.
    """
    if not ssh_target and not credentials_file:
        raise LinuxBootstrapError(
            "Requires ssh_target (e.g. user@host) or an existing credentials_file"
        )

    if not credentials_file:
        from ..infrastructure.ssh_setup import (
            ensure_ssh_credentials_lazy,
            SSHSetupError,
        )

        try:
            credentials_file = await ensure_ssh_credentials_lazy(
                ssh_target, interaction_manager
            )
        except SSHSetupError as exc:
            raise LinuxBootstrapError(
                f"Failed to establish SSH credentials to {ssh_target}: {_exc_text(exc)}"
            ) from exc

    loop = asyncio.get_running_loop()

    def _thread_log(msg: str) -> None:
        if on_log is None:
            return
        try:
            loop.call_soon_threadsafe(on_log, msg)
        except Exception:
            pass

    def _work() -> Dict[str, Any]:
        return _bootstrap_sync(
            credentials_file, install=install, force=force, on_log=_thread_log,
        )

    payload = await loop.run_in_executor(None, _work)

    address = ControlAddress(
        host=payload["host"],
        port=payload["port"],
        token=payload["token"],
        name=name or payload.get("hostname") or payload["host"],
    )
    return LinuxAddressResult(
        address=address,
        upgrade_pending=payload.get("upgrade_pending") or {},
    )


def _bootstrap_sync(credentials_file: str, *, install: bool,
                    force: bool = False,
                    on_log: Optional[Any] = None) -> Dict[str, Any]:
    """Blocking half: SSH in, make sure the daemon is up AND serving, read
    its address.

    ``on_log(message)`` receives operator-facing one-liners about the upgrade
    decision (share scan, versions, deploy / defer). They surface in the Connect
    panel's log so "why didn't it upgrade?" is answerable without SSHing in —
    every branch that used to decide silently now says what it decided.
    """
    from ..tools import remote_handq_tool as rht
    from ..tools.ssh_tool import SshConnectionPool

    def _log(msg: str) -> None:
        if on_log is not None and msg:
            try:
                on_log(msg)
            except Exception:
                pass

    creds = rht._load_credentials(credentials_file)
    pool = SshConnectionPool()
    # The helpers below reach the pool through this thread-local, not through an
    # argument — see RemoteHandQTool.execute's comment on why.
    rht._pool_threadlocal.pool = pool
    upgrade_pending: Dict[str, Any] = {}
    try:
        if install:
            info = rht._ensure_installed(creds, on_log=_log)
        else:
            info = rht._discover(creds)

        installed_version = rht._get_installed_version(creds, info)

        # A newer package is on the share but _ensure_installed did not deploy it
        # (the daemon was alive, and it never rm -rf's a live install). Whether
        # we bounce-and-upgrade or defer depends on TWO things: is the daemon
        # busy, and can the CURRENT version even serve.
        #
        #   installed >= MIN  → the upgrade is optional (we can connect right now
        #     on the old build). Busy ⇒ DEFER: keep the working sessions, hand a
        #     pending-upgrade banner to the UI. Idle ⇒ bounce and upgrade.
        #   installed <  MIN  → the upgrade is MANDATORY (the old build can't
        #     speak the direct protocol at all, so there is no working fallback
        #     to defer to). Busy ⇒ raise LinuxDaemonBusyError so the operator
        #     chooses between waiting and force-restarting; there is no third
        #     option here. Idle ⇒ bounce and upgrade.
        if install and info.get("upgrade_available"):
            share_version = info.get("share_version") or "?"
            can_serve_now = (
                rht._parse_version(installed_version) >= MIN_VERSION_WITH_REMOTE_CONTROL
            )
            if _daemon_alive(rht, creds, info):
                if can_serve_now and not force and _task_running(rht, creds, info):
                    # Optional upgrade, machine in use → defer, don't interrupt.
                    upgrade_pending = {
                        "from": installed_version,
                        "to": share_version,
                        "reason": "remote_sessions_active",
                    }
                    _log(
                        f"Found a newer version {share_version}, but the remote has "
                        f"sessions/tasks running — deferring the upgrade and "
                        f"continuing with the current version {installed_version or 'unknown'}; "
                        f"you can upgrade from the panel after the session ends"
                    )
                else:
                    # Idle, or forced, or a mandatory (below-MIN) upgrade. The
                    # last case still refuses a busy machine unless forced —
                    # _require_idle_or_forced raises LinuxDaemonBusyError, which
                    # is correct: there is no working version to fall back to.
                    _require_idle_or_forced(
                        rht, creds, info, force=force,
                        reason=(
                            f"the share directory has a newer version {share_version}"
                            f" (current {installed_version or 'unknown'}), and a daemon "
                            f"restart is required to upgrade"
                        ),
                    )
                    _log(f"Remote is idle, restarting daemon to upgrade to {share_version}…")
                    _bounce_daemon(rht, creds, info)
                    info = rht._ensure_installed(
                        creds, on_log=_log, allow_deploy_when_alive=True,
                    )
                    installed_version = rht._get_installed_version(creds, info)
            else:
                # Not alive: _ensure_installed above would already have deployed.
                # Reaching here means it couldn't; re-run so the deploy happens
                # now that we've confirmed nothing is running.
                info = rht._ensure_installed(
                    creds, on_log=_log, allow_deploy_when_alive=True,
                )
                installed_version = rht._get_installed_version(creds, info)

        # Hard floor: too old to speak the direct protocol at all, and no newer
        # package to fix it. This is a real inability to connect (not merely a
        # missing feature), so it still fails rather than degrading.
        if rht._parse_version(installed_version) < MIN_VERSION_WITH_REMOTE_CONTROL:
            min_str = ".".join(str(p) for p in MIN_VERSION_WITH_REMOTE_CONTROL)
            raise LinuxBootstrapError(
                f"The handq_linux version installed on {creds.get('hostname')} is "
                f"{installed_version or 'unknown (unreadable)'}, which is below the "
                f"{min_str} required for direct-channel support, and there is no newer "
                f"package in the share directory (update.linux_share_path) for auto-upgrade. "
                f"Please place a handq-linux-*.tar.gz at {min_str} or newer into the share "
                f"directory and re-trigger pairing."
            )

        # No config patching and no restart-to-open-the-port here: a Linux
        # daemon always serves the direct channel (see
        # handq_linux._start_remote_control — `serve` is not read on Linux).
        #
        # But first: refuse to wake a launch that can't run. _discover sets
        # launch_ok=False when the install root has no runnable entry point —
        # either nothing is deployed there yet, or what is there fails `--version`
        # (a corrupt build, or one incompatible with this host's glibc).
        # _ensure_installed force-redeploys to heal it when a share package is
        # configured; reaching here still broken means the redeploy couldn't
        # happen (no share, or it failed). Say why, with the two fixes, instead of
        # proceeding into the cryptic "No such file or directory" wake failure
        # downstream.
        #
        # Gated on the daemon being DOWN: a live daemon already serves the direct
        # channel and _ensure_daemon returns early without touching the launch —
        # a broken respawn path is only fatal when we actually need to respawn.
        # (This also covers the odd case of a daemon still alive from a binary
        # that was since deleted: it keeps working, we just can't restart it.)
        if not info.get("launch_ok", False) and not _daemon_alive(rht, creds, info):
            raise LinuxBootstrapError(
                f"handq_linux is not runnable in the install root "
                f"({info.get('root') or '?'}) on {creds.get('hostname')} "
                f"(launch: {info.get('launch') or 'none installed'}). Configure "
                f"update.linux_share_path so HandQ can auto-deploy a working build, "
                f"or run handq_setup.sh on that host, then re-trigger pairing."
            )

        rht._ensure_daemon(creds, info, creds.get("config_path", "") or "")

        # state.json is written moments after the pid file. Poll rather than
        # read-once, but keep the window tight — a healthy daemon fills it
        # within a second.
        port, token = _poll_state_for_address(rht, creds, info["handq_dir"])

        if not port or not token:
            raise LinuxBootstrapError(
                f"The HandQ daemon on {creds.get('hostname')} started, but did not "
                f"publish remote_control_port / remote_control_token in state.json "
                f"within {STATE_POLL_TIMEOUT_SEC:.0f}s. "
                f"The Linux daemon is supposed to listen unconditionally, so this "
                f"usually means the port bind failed (port in use / blocked by "
                f"firewall) — please check "
                f"{posixpath.join(info.get('handq_dir') or '~/.handq', 'daemon.log')}"
                f" and daemon_error.txt."
            )

        return {
            "host": str(creds.get("hostname") or ""),
            "port": port,
            "token": token,
            "hostname": str(info.get("remote_host") or creds.get("hostname") or ""),
            "handq_dir": info.get("handq_dir", ""),
            "upgrade_pending": upgrade_pending,
        }
    except LinuxBootstrapError:
        raise
    except Exception as exc:
        raise LinuxBootstrapError(
            f"Failed to bootstrap HandQ via SSH on {creds.get('hostname', '?')}: {_exc_text(exc)}"
        ) from exc
    finally:
        rht._pool_threadlocal.pool = None
        try:
            pool.close()
        except Exception:
            logger.debug("remote_control: ssh pool close failed", exc_info=True)


# ── helpers ─────────────────────────────────────────────────────────────────

def _daemon_alive(rht, creds: Dict[str, Any], info: Dict[str, Any]) -> bool:
    """Cheap probe — did the daemon leave a pid file behind."""
    handq_dir = info.get("handq_dir") or ""
    if not handq_dir:
        return False
    return rht._daemon_alive(creds, handq_dir)


def _task_running(rht, creds: Dict[str, Any], info: Dict[str, Any]) -> bool:
    """Is the daemon actually in use right now — file-IPC task OR remote session.

    Two independent signals, because ``StateMirror`` on the Linux side
    deliberately keeps remote-session activity OUT of ``task_status``
    (handq_linux.py: folding it in would corrupt the field the legacy
    remote_handq_tool polls). So ``task_status == "running"`` alone is BLIND to
    a machine that is busy only with direct-connection sessions — and bouncing
    the daemon to upgrade would kill every one of them. ``remote_sessions`` (also
    mirrored into state.json) is the other half; either being non-empty means
    "someone is using this, do not restart it".
    """
    handq_dir = info.get("handq_dir") or ""
    if not handq_dir:
        return False
    state = rht._read_state(creds, handq_dir)
    if str(state.get("task_status") or "") == "running":
        return True
    remote_sessions = state.get("remote_sessions")
    return bool(isinstance(remote_sessions, list) and remote_sessions)


def _require_idle_or_forced(rht, creds: Dict[str, Any], info: Dict[str, Any],
                            *, force: bool, reason: str) -> None:
    """Refuse to let a caller proceed toward restarting the daemon while a
    task is running there, unless they explicitly forced it.

    ``handq_linux --exit`` is a bare SIGTERM (see ``cmd_exit`` in
    handq_linux.py) — it does not know or care whether an agent is mid-tool-
    call. Auto-pair triggering that unconditionally on a machine someone is
    actually using would be exactly the kind of silent destructive action this
    codebase's own operating rules say to avoid. So the default is to stop and
    tell the operator, not to guess that "probably fine" — restarting is only
    ever attempted after this passes (task not running) or the caller passed
    ``force=True`` having already been told what's at stake.
    """
    if force:
        logger.warning(
            "remote_control: force=True on %s — restarting despite %s "
            "without checking whether a task is running",
            creds.get("hostname"), reason,
        )
        return
    if _task_running(rht, creds, info):
        raise LinuxDaemonBusyError(
            f"A task is running on {creds.get('hostname')}, {reason}, "
            f"but the running task will not be auto-interrupted. Please wait for it "
            f"to finish and re-trigger pairing, or explicitly choose "
            f"\"Force Restart\" (this will interrupt the current task)."
        )


def _bounce_daemon(rht, creds: Dict[str, Any], info: Dict[str, Any]) -> None:
    """<launch> --exit, then wait for the pid file to disappear.

    Callers MUST have already checked (or been forced past)
    :func:`_require_idle_or_forced` — this function itself does not re-check,
    since by the time it's called the decision has already been made and
    logged once.

    Uses the same command shape ``_action_exit_handq`` uses; also evicts the
    discovery cache so the caller's subsequent ``_ensure_daemon`` re-probes
    (a fresh binary path in memory would be stale if we somehow ended up
    installing during this bootstrap).
    """
    launch = info.get("launch") or ""
    if launch:
        try:
            rht._remote_bash(creds, launch + " --exit 2>&1 || true", timeout=20.0)
        except Exception:
            # A dying daemon can drop its stdout mid-run; the pid-file check
            # below is what really tells us it's gone.
            logger.debug("remote_control: --exit stderr swallowed", exc_info=True)

    # Wait for pid file to disappear — that is the truthful "gone" signal.
    handq_dir = info.get("handq_dir") or ""
    if handq_dir:
        deadline = time.monotonic() + EXIT_POLL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if not rht._daemon_alive(creds, handq_dir):
                break
            time.sleep(0.4)

    # Force the next _discover to re-run so cached info doesn't lie about
    # daemon liveness. Use the exact key remote_handq_tool builds so we hit
    # the same entry it wrote.
    try:
        rht._discovery_cache.pop(rht._host_key(creds), None)
    except Exception:
        pass


def _poll_state_for_address(rht, creds: Dict[str, Any],
                            handq_dir: str) -> tuple:
    """Wait for state.json to publish the remote_control_port/token pair."""
    deadline = time.monotonic() + STATE_POLL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        state = rht._read_state(creds, handq_dir)
        port = int(state.get("remote_control_port") or 0)
        token = str(state.get("remote_control_token") or "")
        if port and token:
            return port, token
        time.sleep(STATE_POLL_INTERVAL_SEC)
    return 0, ""


# ── llm.* config sync ─────────────────────────────────────────────────────────

#: The single config subtree the controller is authoritative for. Credentials +
#: model pool: the things a controller has and a headless daemon does not. Nothing
#: else is touched — see :func:`sync_linux_llm_config` for the three keys that must
#: NOT be synced and why.
_SYNCED_LLM_KEYS = ("API_KEY", "available_models", "agent_models", "helper_models")


class LlmSyncResult(NamedTuple):
    """Outcome of a connect-time llm.* sync.

    ``changed`` — the remote yaml was rewritten. ``restarted`` — the daemon was
    also bounced so it would re-read it. ``pending`` — ``{}`` normally, else
    ``{from, to, reason}`` when the write happened but a restart was deferred
    because the machine was busy (the panel surfaces it like ``upgrade_pending``).
    ``address`` — the daemon's CURRENT control address whenever it could be
    read (a restart minted a new port/token, or the daemon was already alive
    and simply asked what it's listening on), else ``None`` only when nothing
    is running or state.json never published the fields. Not gated on
    ``restarted``: a live daemon's port can have moved for reasons unrelated
    to this sync (crash, out-of-band restart, host reboot), and the caller
    must not keep trusting a cached address the daemon has since abandoned.
    """
    changed: bool
    restarted: bool
    pending: Dict[str, Any]
    address: Optional[ControlAddress]


async def sync_linux_llm_config(
    *,
    ssh_target: str,
    credentials_file: str = "",
    name: str = "",
    force: bool = False,
    interaction_manager: Optional[Any] = None,
    on_log: Optional[Any] = None,
) -> LlmSyncResult:
    """Push this controller's ``llm.*`` config to a Linux target before connecting.

    The controller's yaml is the authoritative source for a headless daemon's
    credentials and model pool — a daemon has no operator to type an API key. So
    every connect re-asserts it: this is what makes fixing the key on the
    controller (step 0/1) heal the remote automatically, instead of waiting for a
    deploy to happen to run.

    **Only ``llm.*`` is synced** (:data:`_SYNCED_LLM_KEYS`). Three things on the
    remote are deliberately left alone:

    * ``version:`` — ``remote_handq_tool._get_installed_version`` reads this to
      decide what is installed there. Overwriting it with the controller's version
      makes that lie, and the upgrade check breaks forever (the mirror image of the
      §15.5 bug). The daemon owns its own version line.
    * ``remote_control.*`` and ``allow_remote_secret_prompt`` — per §10.3 these are
      the controlled machine's own advanced knobs (port, max_sessions, and a *security*
      switch that only that machine's owner should set). A controller flattening
      them would be overreach.
    * everything else the daemon or its operator set locally.

    A daemon reads config only at startup, so a write alone changes nothing live.
    When the write differs and the machine is idle we bounce it (``--exit``; it is
    restarted by whatever supervises it, or the caller reconnects to the fresh
    port/token). When it is busy we DEFER: write the file, report ``pending``, and
    let the running work finish — the same idle/busy line the upgrade path draws
    (:func:`_require_idle_or_forced`). ``force=True`` bounces regardless.

    Skips silently (``changed=False``) when the controller's own ``llm.API_KEY``
    is blank: pushing a blank is never intended, and refusing here matches
    ``remote_handq_tool._validated_local_config``.
    """
    from ..infrastructure.config_manager import ConfigManager

    local_llm = dict(ConfigManager().get_section("llm") or {})
    if not str(local_llm.get("API_KEY") or "").strip():
        # Nothing worth syncing, and a blank key would only break the remote.
        # Step 1's deploy guard says the same thing; say it here too so a
        # connect doesn't quietly wipe a remote key that was fine.
        if on_log is not None:
            on_log("Skipping llm config sync: local llm.API_KEY is empty")
        return LlmSyncResult(False, False, {}, None)

    desired = {k: local_llm[k] for k in _SYNCED_LLM_KEYS if k in local_llm}

    if not credentials_file:
        from ..infrastructure.ssh_setup import (
            ensure_ssh_credentials_lazy,
            SSHSetupError,
        )
        try:
            credentials_file = await ensure_ssh_credentials_lazy(
                ssh_target, interaction_manager
            )
        except SSHSetupError as exc:
            raise LinuxBootstrapError(
                f"Failed to establish SSH credentials to {ssh_target}: {_exc_text(exc)}"
            ) from exc

    loop = asyncio.get_running_loop()

    def _thread_log(msg: str) -> None:
        if on_log is None:
            return
        try:
            loop.call_soon_threadsafe(on_log, msg)
        except Exception:
            pass

    def _work() -> LlmSyncResult:
        return _sync_llm_sync(
            credentials_file, desired, name=name, force=force, on_log=_thread_log,
        )

    return await loop.run_in_executor(None, _work)


def _sync_llm_sync(
    credentials_file: str,
    desired: Dict[str, Any],
    *,
    name: str = "",
    force: bool = False,
    on_log: Optional[Any] = None,
) -> LlmSyncResult:
    """Blocking half of :func:`sync_linux_llm_config`. Runs in a worker thread."""
    import yaml as _yaml

    from ..tools import remote_handq_tool as rht
    from ..tools.ssh_tool import SshConnectionPool

    def _log(msg: str) -> None:
        if on_log is not None and msg:
            try:
                on_log(msg)
            except Exception:
                pass

    creds = rht._load_credentials(credentials_file)
    pool = SshConnectionPool()
    rht._pool_threadlocal.pool = pool
    try:
        info = rht._discover(creds)
        # The config lives in the install root (machine-local), not in the old
        # ~/handq. _discover resolves the root; fall back to remote_home/handq
        # only when nothing is installed yet, so a first-time sync onto an
        # un-deployed host still points somewhere sane rather than raising.
        root = info.get("root") or posixpath.join(
            info.get("remote_home") or "~", "handq"
        )
        config_path = posixpath.join(root, "handq_config.yaml")

        raw = rht._read_remote_file(creds, config_path)
        if not raw:
            # No config to merge into. Pairing/deploy owns first-time seeding;
            # a bare sync should not invent a whole file.
            _log("Skipping llm config sync: remote has no handq_config.yaml (not deployed yet?)")
            return LlmSyncResult(False, False, {}, None)
        try:
            remote_cfg = _yaml.safe_load(raw) or {}
        except Exception:
            _log("Skipping llm config sync: remote handq_config.yaml could not be parsed")
            return LlmSyncResult(False, False, {}, None)
        if not isinstance(remote_cfg, dict):
            return LlmSyncResult(False, False, {}, None)

        remote_llm = dict(remote_cfg.get("llm") or {})
        # Compare only the keys we own. An identical subtree means no write and no
        # "needs restart" noise — the common case on every reconnect.
        needs_write = not all(remote_llm.get(k) == v for k, v in desired.items())

        if not needs_write:
            if _daemon_alive(rht, creds, info):
                # Config matches and the daemon is up — but "up" is not the
                # same claim as "up on the port the registry has cached".
                # The daemon can have restarted for reasons that have nothing
                # to do with this sync (a crash, a manual --exit/relaunch, an
                # out-of-band tool run, a host reboot) and come back on a new
                # dynamic port; nothing about that touches llm.* config, so
                # "no diff" would otherwise report a silent no-op forever
                # while the registry's address quietly rots. Always hand back
                # the CURRENT truth from state.json so the caller can re-pair
                # even when nothing here needed fixing.
                state = rht._read_state(creds, info["handq_dir"])
                port = int(state.get("remote_control_port") or 0)
                token = str(state.get("remote_control_token") or "")
                address = None
                if port and token:
                    host = creds.get("hostname") or ""
                    address = ControlAddress(
                        host=host, port=port, token=token, name=name or host,
                    )
                return LlmSyncResult(False, False, {}, address)
            # Config already matches — often because a previous sync (or a
            # manual fix) already wrote it — but nothing is listening, e.g.
            # the daemon was stopped out-of-band. "No diff" must not mean "do
            # nothing": the caller still needs a live port/token to connect
            # to, and a dead daemon with matching config would otherwise be
            # silently skipped forever (no diff ever appears to trigger a
            # start). No config write needed here, just bring the process up.
            _log("Remote config is already up to date, but the daemon isn't running — starting it")
            rht._ensure_daemon(creds, info, creds.get("config_path", "") or "")
            port, token = _poll_state_for_address(rht, creds, info["handq_dir"])
            address = None
            if port and token:
                host = creds.get("hostname") or ""
                address = ControlAddress(
                    host=host, port=port, token=token, name=name or host,
                )
                _log("Remote daemon started")
            else:
                _log("Tried to start the remote daemon, but couldn't read the port/token — please re-pair")
            return LlmSyncResult(False, True, {}, address)

        merged_llm = dict(remote_llm)
        merged_llm.update(desired)
        remote_cfg["llm"] = merged_llm
        # version:, remote_control.*, allow_remote_secret_prompt and everything
        # else are carried through untouched — we serialize the remote's own dict
        # with only llm replaced.
        rht._write_remote_file(
            creds, config_path, _yaml.safe_dump(remote_cfg, sort_keys=False),
        )
        _log("Synced local llm config (API key + model pool) to remote")

        # The write is inert until the daemon restarts. Idle → bounce now so it
        # takes effect; busy → defer, exactly like the upgrade path.
        if _task_running(rht, creds, info) and not force:
            _log(
                "Remote has a task/session running — not interrupting; the new llm "
                "config will take effect after the next restart"
            )
            return LlmSyncResult(
                True, False,
                {"reason": "llm_config", "detail": "config synced, restart deferred"},
                None,
            )

        _bounce_daemon(rht, creds, info)
        info = rht._discover(creds, force=True)
        rht._ensure_daemon(creds, info, creds.get("config_path", "") or "")
        port, token = _poll_state_for_address(rht, creds, info["handq_dir"])
        address = None
        if port and token:
            host = creds.get("hostname") or ""
            address = ControlAddress(
                host=host, port=port, token=token, name=name or host,
            )
            _log("Remote restarted with the new config")
        else:
            _log("Remote restarted, but couldn't read the new port/token — please re-pair")
        return LlmSyncResult(True, True, {}, address)
    finally:
        rht._pool_threadlocal.pool = None
        try:
            pool.close()
        except Exception:
            pass

