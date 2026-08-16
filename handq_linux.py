#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
handq_linux.py — Linux HandQ entry point (console client + resident daemon).

One Windows HandQ controls many Linux HandQ. Each Linux HandQ is a "Sub HandQ
System": a resident ``FlowControllerV2`` daemon that Windows drives one-way
(Win→Linux) over SSH via ``remote_handq_tool``. The local console here is an
emergency channel for when operating Windows is inconvenient — it shares the
same file pipe as Windows, so the two are symmetric and debuggable.

Design (see memory ``linux-handq-design``):
  * No tmux, no systemd.
  * The daemon is a plain Python process detached with ``setsid``
    (``start_new_session=True``); Windows (or this console) can wake it at any
    time. The *process* persists across Windows power / network loss — an
    in-flight task is NOT replayed if the daemon itself dies (task-level
    durability is out of scope; see the deferred notes in the design).

Platform capability matrix — what a Linux HandQ has and, deliberately, does not.
There is no single config switch for this; it is the sum of three mechanisms, so
the whole picture is written down here rather than inferred from scattered gates:

  * Tools: the ONLY gate is ``tool_registry._IS_WINDOWS`` (``tool_registry.py``,
    NOT ``flow_controller`` — this file's old note said flow_controller and was
    wrong). Linux registers file built-ins (read/write/edit/glob/grep),
    shell, ssh (claimable), read_skill, todo_write, spawn_agent/fan_out_agents,
    notify_user, wait_interval, claim_tool/release_tool, and schedule_wakeup.
    Everything Windows-only — browser_*, desktop_*, live_shell_*, web_search,
    email, teams, ask_human, and schedule_create/list/delete — is simply never
    registered, so it also never appears in the claimable menu.
  * Process singletons initialised HERE vs on the Windows bridge:
      - SkillRegistry:  initialised (``_init_skill_registry``). Skills are the
        one cross-cutting capability a Linux被控 session keeps, so read_skill and
        the [Available Skills] menu work. Requires the packaged ``Skill/`` dir to
        ship — see ``packaging/build_linux.sh``.
      - LongTermMemory: NOT initialised. ``.get()`` returns a null instance, so
        recall/submit are silent no-ops. A Linux box has no personal history to
        carry; the embedding model + sqlite it needs are not packaged either.
      - PersonalityMonitor / activity capture: NOT initialised (Windows-only
        capture stack; no local user to observe).
      - Scheduler (cron): NOT initialised, so ``ctx.scheduler`` is None and the
        Windows-only schedule_create/list/delete are correctly absent.
        schedule_wakeup does NOT use it (it re-queues on the TaskChannel) and so
        stays available.
  * Vision / OCR / screenshots: unreachable — their only callers are the
    Windows-only desktop_/browser_ tools and the personality capture stack.

Commands:
  handq_linux                  start (if needed) + interactive console
  handq_linux <goal text...>   submit a goal directly, print the reply
  handq_linux --goal "..."     same as inline goal
  handq_linux --new            (re)start a fresh session
  handq_linux --status         print the daemon's state.json
  handq_linux --exit           stop the daemon
  handq_linux --config PATH    use a specific config file
  handq_linux --version        print the version and exit
  handq_linux --_daemon        internal: run the resident daemon (setsid target)

Every non-daemon command prints a one-line session banner first — the current
session_id and its working directory — and flags whether it continues the
session this console last talked to or a fresh one has taken its place (daemon
restart, or someone ran ``--new``). Inside the console, ``new`` starts a fresh
session and ``status`` inspects the current one without leaving. This is how a
Linux user tells "same session as before, keep going" from "start over".

Install root and file IPC layout — everything lives under ONE machine-local root
(``$HANDQ_ROOT``, resolved by ``handq_setup.sh`` and exported from the per-host
dispatcher config; see ``_resolve_root``)::

  <root>/                        /local/mnt/workspace/<user>@handq, or /var/tmp/…,
                                 or ~/handq/<user>@<host> as a last resort
    handq_linux.dist/            binary + deps + bundled Skill/
    handq_config.yaml            carries the API key — root is chmod 700
    state.json                   daemon writes coarse status + latest_tool
                                 (includes session_id + working_dir)
    handq.pid
    messages/<id>.txt            inbound goal / follow-up (console AND Windows write here)
    commands/<id>.json           inbound new_session / interrupt
    reply/<id>.txt               outbound reply (console fetches it; keyed by message id)
    confirmation_request.json / confirmation_response.json
                                 bidirectional tool / risk / secret / form (ask_human) confirms
    .last_seen_session           client-side breadcrumb: session_id this console last saw
    daemon.log  daemon_error.txt
    skills/                      pushed skills (sibling of the dist, so upgrades
                                 don't wipe them)
    workspace/<session_id>/      agent working directory

The root is deliberately NOT under ``$HOME``: in this deployment ``$HOME`` is
cloud-synced across several physical Linux hosts for the same user, so a
``$HOME``-based root means two machines share one install and one state directory
— which is exactly what stopped the same user running a daemon on two machines at
once. Only the last-resort candidate lives under ``$HOME``, and it carries a host
segment for that reason.
"""
from __future__ import annotations

import argparse
import contextvars
import getpass
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── repo root on sys.path so ``import src.*`` resolves regardless of CWD ──────
# The daemon is setsid-detached and may inherit an arbitrary CWD; Python puts
# the script's own directory on sys.path[0], but we make it explicit so a
# frozen / symlinked launch still resolves the package tree.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── frozen detection ─────────────────────────────────────────────────────────
# Nuitka standalone does NOT set ``sys.frozen`` (that's the PyInstaller /
# cx_Freeze convention); it injects a module-level ``__compiled__`` instead.
# Check both so the frozen-path branches — config resolution and the daemon
# self-spawn — fire under Nuitka. Without this, ``_self_invocation`` re-execs a
# non-existent ``python3`` next to the binary → FileNotFoundError on daemon spawn.
_IS_FROZEN = bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())

# Pure fallback — NOT the source of truth and NOT a build input. The single
# version authority is electron/package.json; build_linux.sh stamps that value
# into the packaged handq_config.yaml's top-level ``version:``, and
# ``_resolve_version`` reads it from there at runtime. This constant is only
# consulted when the config has no ``version:`` at all (source-run / unpackaged
# dev checkout), so it may drift harmlessly — production never reads it.
__version__ = "0.0.0"


# ── Install root ─────────────────────────────────────────────────────────────
def _short_host() -> str:
    return socket.gethostname().split(".")[0]


def _user_name() -> str:
    # getpass.getuser() consults env then pwd; mirrors the $(whoami) probe that
    # remote_handq_tool runs so both sides resolve the SAME directory.
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "default")


def _root_candidates() -> List[Path]:
    """The install-root candidate chain, in priority order.

    Must stay in sync with ``handq_setup.sh``'s ``_resolve_handq_root`` and
    ``remote_handq_tool``'s ``_PROBE``. See :func:`_resolve_root` for why all
    three exist and why the duplication is bounded.
    """
    user = _user_name()
    return [
        Path("/local/mnt/workspace") / f"{user}@handq",
        Path("/var/tmp") / f"{user}@handq",
        # Last resort only. $HOME is cloud-synced across machines in this
        # deployment, so this candidate MUST carry the host segment or two
        # machines land on the same root again.
        Path.home() / "handq" / f"{user}@{_short_host()}",
    ]


def _root_usable(path: Path) -> bool:
    """Can we own and write *path*? Mirrors the shell probe's checks.

    A bare ``mkdir`` is not enough: these candidates are multi-user visible and
    ``/var/tmp`` is sticky, so an existing entry may belong to someone else or be
    a symlink they planted. And a directory existing is not proof a file can be
    created in it (per-user quota) — so actually write one.
    """
    try:
        if path.is_symlink():
            return False
        if path.exists():
            if not path.is_dir():
                return False
            if hasattr(os, "geteuid") and path.stat().st_uid != os.geteuid():
                return False
        else:
            path.mkdir(parents=True, exist_ok=True)
        probe = path / f".handq_probe.{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _resolve_root() -> Path:
    """Where this machine's HandQ install and state live.

    ``$HANDQ_ROOT`` is the authority and the normal path: ``handq_setup.sh``
    resolves the root once and exports it from the per-host dispatcher config
    (``~/.config/handq/hosts/<shorthost>``), which the dispatcher sources before
    exec'ing us. Reading it rather than re-deriving it is what keeps this module,
    the setup script and the Windows probe from drifting apart.

    The candidate chain is only reached when the binary is invoked directly,
    without going through the dispatcher — a developer running it by hand, or a
    host whose per-host config has not been written yet. Any divergence between
    the three implementations can therefore only affect that pre-setup window, and
    self-heals the moment setup records a root.

    Deliberately NOT under ``$HOME``: in this deployment ``$HOME`` is cloud-synced
    across several physical Linux hosts for the same user, so a ``$HOME``-based
    root means two machines share one install directory and one state directory —
    precisely what stopped the same user running a daemon on two machines at once.
    Only the last-resort candidate lives under ``$HOME``, and it carries a host
    segment for that reason.
    """
    env_root = os.environ.get("HANDQ_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser()
    for cand in _root_candidates():
        if _root_usable(cand):
            return cand
    # Nothing usable. Return the last candidate anyway so the failure surfaces as
    # a concrete permission/space error against a named path, rather than as a
    # None deref five frames later.
    return _root_candidates()[-1]


#: Install root: the binary, config, file-IPC pipe, pushed skills and the agent
#: workspace all live here. Machine-local, so nothing in it is shared between
#: hosts (see _resolve_root).
HANDQ_ROOT = _resolve_root()
STATE_FILE = HANDQ_ROOT / "state.json"
PID_FILE = HANDQ_ROOT / "handq.pid"
MESSAGES_DIR = HANDQ_ROOT / "messages"
COMMANDS_DIR = HANDQ_ROOT / "commands"
REPLY_DIR = HANDQ_ROOT / "reply"
PROCESSED_DIR = MESSAGES_DIR / ".processed"
CONFIRM_REQUEST_FILE = HANDQ_ROOT / "confirmation_request.json"
CONFIRM_RESPONSE_FILE = HANDQ_ROOT / "confirmation_response.json"
DAEMON_LOG = HANDQ_ROOT / "daemon.log"
# Where a pushed skill lands. Deliberately a SIBLING of handq_linux.dist/, not
# inside it: the deploy script swaps the dist directory wholesale, so a skills dir
# inside it would be wiped on every upgrade. Keeping it out here also makes the
# user root differ from the bundled root, which restores the user-shadows-bundled
# layering SkillRegistry is built around (see _init_skill_registry).
SKILLS_DIR = HANDQ_ROOT / "skills"
# Client-side breadcrumb: the session_id this console last talked to. Lets a
# later invocation distinguish "same session I was on" from "the daemon
# restarted / someone ran --new since". Written by the client, never the
# daemon — it records the client's own view, not authoritative state.
LAST_SEEN_SESSION_FILE = HANDQ_ROOT / ".last_seen_session"

# The msgid whose on_user_message() call is currently on this asyncio Task's
# call stack — set for the duration of _drain_messages' await, unset outside
# it. _on_agent_reply reads this to tell a DIRECT synchronous reply (chat, or
# a synchronous completion-reply fallback — same Task, sees the var) apart
# from an ASYNC task-completion summary arriving from the daemon's
# independent agent background task, which snapshots its context at creation
# time (in ``start()``, before any message exists) and so never observes a
# later ``.set()`` from _drain_messages, regardless of lock/await interleaving.
_current_msgid: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_current_msgid", default=None
)


def _default_config_path() -> Path:
    """Where to auto-load handq_config.yaml from when --config is omitted.

    Source run: next to this file (the repo root).
    Frozen run (Nuitka standalone): the binary lives at
    ``<dist_root>/handq_linux.dist/handq_linux.bin`` while the user-editable
    config that ``build_linux.sh`` ships sits at the *dist root*, one level
    above the ``.dist`` dir — so resolve there, not next to the binary.
    """
    if _IS_FROZEN:
        bin_dir = os.path.dirname(os.path.abspath(sys.argv[0]))  # handq_linux.dist/
        dist_root = os.path.dirname(bin_dir)                     # dist root
        return Path(dist_root) / "handq_config.yaml"
    return Path(_REPO_ROOT) / "handq_config.yaml"


DEFAULT_CONFIG = _default_config_path()

POLL_INTERVAL = 0.2  # seconds — file-pipe poll cadence (both daemon and client)
# Confirmation wait: a risk / tool / secret prompt blocks the agent until a
# response file appears. For a headless remote daemon the safe default on
# timeout is "no" (refuse) — matches InteractionManager's own no-delegate
# default. Override with HANDQ_CONFIRM_TIMEOUT.
try:
    CONFIRM_TIMEOUT = float(os.environ.get("HANDQ_CONFIRM_TIMEOUT", "300"))
except ValueError:
    CONFIRM_TIMEOUT = 300.0


# ── tiny filesystem helpers ──────────────────────────────────────────────────
def _ensure_dirs() -> None:
    for d in (HANDQ_ROOT, MESSAGES_DIR, COMMANDS_DIR, REPLY_DIR, PROCESSED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Restrict the root to its owner: anyone who can write here can drive the
    # agent (submit goals, answer confirmations), and handq_config.yaml in this
    # same directory carries the LLM API key. Under $HOME that was covered by the
    # home directory's own permissions; the machine-local roots this now resolves
    # to (/local/mnt/workspace, /var/tmp) are multi-user visible, so it has to be
    # explicit. Best-effort — a no-op on filesystems without POSIX permissions.
    try:
        HANDQ_ROOT.chmod(0o700)
    except Exception:
        pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Write then rename so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  StateMirror — the daemon's UIDelegate
#
#  Sole job (per design): mirror controller activity into state.json and route
#  confirmation requests through the file pipe. It never renders anything. The
#  authoritative full reply is NOT the return value of on_user_message (which is
#  only the initial plan ack for a task) — it arrives later via the
#  on_reply_to_user sink the daemon wires into FlowControllerV2.
# ─────────────────────────────────────────────────────────────────────────────
class StateMirror:
    """``UIDelegate`` that writes ``state.json`` and file-routes confirmations.

    Only the methods the daemon cares about are implemented; the
    InteractionManager silently skips any delegate method that is absent, so
    this class deliberately omits the coordinator-reply-streaming hooks.
    """

    def __init__(self, session_id: str, working_dir: str = "") -> None:
        self._session_id = session_id
        self._working_dir = working_dir
        self._task_channel: Any = None
        self._status = "idle"
        # "" = no task yet, "running" = task in flight, "idle" = settled.
        self._task_status = ""
        self._latest_tool: Optional[Dict[str, Any]] = None
        # Direct control channel — published in state.json so the Windows side
        # can read the address over the SSH channel it already uses for
        # deployment and then switch to a direct connection. Empty/0 when the
        # daemon is not serving.
        self._remote_port = 0
        self._remote_token = ""
        #: Callable returning a list of {session_id, title, state, ...} dicts for
        #: the active remote-driven sessions, so state.json can expose them to
        #: the CLI's --list-sessions / --close-session. Set by the daemon after
        #: the server starts. None until then (or when not serving).
        self._remote_sessions_provider = None

    # ── wiring ────────────────────────────────────────────────────────────
    def attach_task_channel(self, task_channel: Any) -> None:
        self._task_channel = task_channel

    def set_working_dir(self, working_dir: str) -> None:
        self._working_dir = working_dir

    def reset(self, session_id: str, working_dir: str = "") -> None:
        self._session_id = session_id
        self._working_dir = working_dir
        self._status = "idle"
        self._task_status = ""
        self._latest_tool = None

    def set_task_status(self, status: str) -> None:
        self._task_status = status

    def set_remote_control(self, port: int, token: str) -> None:
        """Publish the direct-control address into ``state.json``.

        Written here rather than by the server itself so the whole
        daemon→Windows contract stays in one file with one writer. The token is
        a secret in a 0700 directory, which is the same protection
        ``~/.handq/`` already gives the message and reply files.
        """
        self._remote_port = int(port)
        self._remote_token = str(token)

    def mark_task_starting(self) -> None:
        """Call the instant a message is drained, before on_user_message runs.

        Closes a race where a fast poller's get_status call could otherwise
        observe task_status='running' (set eagerly here) alongside a stale
        status_text='idle' left over from the PREVIOUS task — self-
        contradictory, since show_state_changed only ever moves status_text
        off 'idle' once the agent's own loop actually starts (which happens
        moments later, asynchronously). Setting both fields together here
        keeps the snapshot internally consistent for that gap.
        """
        self._status = "starting"
        self._task_status = "running"

    # ── state-mirroring delegate hooks ───────────────────────────────────
    def show_state_changed(self, state: str) -> None:
        self._status = state
        # "idle" is the only host-visible task-settled signal.
        self._task_status = "idle" if state == "idle" else "running"
        self.snapshot()

    def show_inline_event(self, icon: str, desc: str) -> None:
        # Coarse one-liner; folded into status_text so Windows sees the latest
        # activity banner without a separate field.
        self._status = f"{icon} {desc}".strip()
        self.snapshot()

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        output: Any,
    ) -> None:
        # Called twice per tool: (params set, output None) on start, then
        # (params None, output set) on finish. Capture the START snapshot —
        # the design surfaces only the latest single tool call, not history.
        if tool_name is None:
            return
        if params is not None and output is None:
            self._latest_tool = {
                "iteration": int(iteration),
                "name": str(tool_name),
                "params": _truncate(params),
            }
            self.snapshot()

    # ── state.json snapshot ──────────────────────────────────────────────
    def snapshot(self) -> None:
        state = {
            "pid": os.getpid(),
            "handq_active": True,
            "session_id": self._session_id,
            "working_dir": self._working_dir,
            "task_status": self._task_status,
            "status_text": self._status,
            "latest_tool": self._latest_tool,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            # Direct control channel (docs/fleet_scheduling_design.md §9.8).
            # 0 / "" when this daemon is not serving.
            "remote_control_port": self._remote_port,
            "remote_control_token": self._remote_token,
            # Active remote-driven sessions (rc-*), so the CLI's --list-sessions
            # / --close-session can see them. Empty when not serving or when
            # nobody is connected.
            "remote_sessions": self._collect_remote_sessions(),
        }
        try:
            _atomic_write_json(STATE_FILE, state)
        except Exception:
            pass

    def _collect_remote_sessions(self) -> list:
        provider = self._remote_sessions_provider
        if provider is None:
            return []
        try:
            return provider()
        except Exception:
            return []

    # ── confirmation routing (async, file-based) ─────────────────────────
    async def request_risk_confirmation(self, description: str):
        from src.controller_v2.user_confirmation import UserConfirmation
        resp = await self._await_response("risk", {"description": str(description)})
        if not resp:
            return UserConfirmation.no()
        decision = resp.get("decision", "no")
        if decision == "yes":
            return UserConfirmation.yes()
        if decision in ("message", "risk_guidance"):
            return UserConfirmation.risk_guidance(resp.get("message", ""))
        return UserConfirmation.no()

    async def request_tool_confirmation(
        self, tool_name: str, params: Dict[str, Any], hint: str
    ):
        from src.controller_v2.user_confirmation import UserConfirmation
        resp = await self._await_response(
            "tool",
            {"tool_name": str(tool_name), "params": _truncate(params), "hint": str(hint)},
        )
        if not resp:
            return UserConfirmation.no()
        decision = resp.get("decision", "no")
        if decision == "yes":
            return UserConfirmation.yes()
        if decision == "message":
            return UserConfirmation.with_message(resp.get("message", ""))
        return UserConfirmation.no()

    async def request_secret_input(self, prompt: str) -> str:
        resp = await self._await_response("secret", {"prompt": str(prompt)})
        return (resp or {}).get("value", "")

    async def request_user_form(self, question: str, fields: list) -> Dict[str, Any]:
        resp = await self._await_response("form", {"question": str(question), "fields": fields or []})
        value = (resp or {}).get("value")
        return value if isinstance(value, dict) else {}

    async def _await_response(
        self, kind: str, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Write a confirmation_request and poll for a matching response.

        Returns the response dict, or ``None`` on timeout (caller maps that to
        the safe default). Uses ``asyncio.sleep`` so the agent's event loop
        keeps running while a human (or Windows) answers.
        """
        import asyncio

        cid = uuid.uuid4().hex
        request = {"id": cid, "kind": kind, "ts": time.time(), **payload}
        try:
            _atomic_write_json(CONFIRM_REQUEST_FILE, request)
        except Exception:
            return None
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        try:
            while time.monotonic() < deadline:
                resp = _read_json(CONFIRM_RESPONSE_FILE)
                if resp and resp.get("id") == cid:
                    _unlink(CONFIRM_RESPONSE_FILE)
                    _unlink(CONFIRM_REQUEST_FILE)
                    return resp
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            # Clear a stale request so the next prompt starts clean.
            _unlink(CONFIRM_REQUEST_FILE)
        return None


def _truncate(obj: Any, limit: int = 600) -> Any:
    """Shrink tool params for state.json — Windows wants a glimpse, not the payload."""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) <= limit:
        try:
            return json.loads(s)
        except Exception:
            return s
    return s[:limit] + "…"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Daemon
# ─────────────────────────────────────────────────────────────────────────────
class _LinuxSessionHost:
    """被控-side ``SessionHost`` for the Linux daemon.

    Each remotely-driven session gets its own ``FlowControllerV2`` in its own
    session directory, independent of the daemon's single file-IPC session. Two
    consequences worth naming:

    * ``remote_handq_tool`` (SSH + ``state.json`` polling) and a direct
      connection can be used at the same time without interfering. That matters
      during the transition, since the tool stays in place for now.
    * ``StateMirror`` is deliberately NOT wired as a local mirror. It represents
      "the one session Windows polls over SSH", and folding remote-session events
      into it would corrupt the very ``task_status`` the old tool reads.
    """

    def __init__(self, daemon: "_LinuxDaemon", allow_secret_prompt: bool = False) -> None:
        self._daemon = daemon
        self._allow_secret_prompt = allow_secret_prompt

    def describe(self) -> Dict[str, str]:
        return {"name": socket.gethostname(), "platform": sys.platform}

    async def create_flow(self, session: Any, goal: str) -> Any:
        from src.remote_control.network_delegate import NetworkUIDelegate

        daemon = self._daemon
        flow = daemon._build_flow(session.session_id)
        delegate = NetworkUIDelegate(
            session,
            local_delegate=None,
            allow_secret_prompt=self._allow_secret_prompt,
        )
        if flow.interaction_manager is not None:
            flow.interaction_manager.set_delegate(delegate)
        # Must be set BEFORE start(): FlowControllerV2.start() reads
        # self._on_intent_classified once, at Orchestrator construction time
        # (flow_controller.py's start()), and never again — setting this
        # after start() would silently bind nothing. _build_flow (shared with
        # the daemon's own file-IPC session) never wires this hook, because
        # that path has no resume/task-tracking use for it; a remote session
        # DOES: marking is_task lets the controller decide, on release,
        # whether this session is worth a re-adopt record (chat-only
        # sessions aren't — see RemoteSession.mark_task_started).
        flow._on_intent_classified = lambda intent: (
            session.mark_task_started() if intent == "queue" else None
        )
        await flow.start()
        # on_reply_to_user was bound to the daemon's file-IPC reply sink by
        # _build_flow. For a remote session that sink would write into
        # reply/<msgid>.txt for messages that have nothing to do with it, so it
        # is replaced by the delegate's own reply path. This carries the
        # task-completion summary — the single most important message in the
        # whole stream — so getting it wrong would be silent and severe.
        flow._on_reply_to_user = delegate.show_coordinator_reply
        daemon._remote_flows[session.session_id] = flow
        # Say it now rather than let the first LLM call say it in 17 minutes.
        # The controller has no view of this machine's config, so an unusable
        # daemon has to announce itself into the session the operator is looking
        # at (the startup ERROR in daemon.log only helps someone already on this
        # box). Not fatal: the session still exists and becomes useful the moment
        # the key is fixed and this daemon restarts.
        if daemon._llm_credential_problem:
            session.publish_event(
                "display_error",
                [f"远端 HandQ 无法调用 LLM：{daemon._llm_credential_problem}"],
            )
        return flow

    async def handle_user_input(
        self, session: Any, kind: str, payload: Dict[str, Any]
    ) -> None:
        # desktop_takeover_revoked is the only kind, and desktop_tool is not
        # registered on Linux (tool_registry.py gates it Windows-only), so there
        # is nothing to revoke here. Logged rather than silently dropped.
        if self._daemon._logger:
            self._daemon._logger.info(
                f"[handq_linux] ignoring remote user_input kind={kind!r} "
                "(not applicable on Linux)"
            )

    async def handle_rpc(
        self, session: Any, action: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        if action != "file_undo":
            raise ValueError(f"unsupported remote rpc {action!r}")
        flow = self._daemon._remote_flows.get(session.session_id)
        if flow is None:
            raise RuntimeError("session has no flow")
        return await flow.undo_files(payload.get("item_id"))

    async def push_skills(self, skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from src.infrastructure.skills import SkillRegistry

        registry = SkillRegistry.get()
        results: List[Dict[str, Any]] = []
        for entry in skills:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            files = entry.get("files")
            files = files if isinstance(files, list) else []
            results.append(registry.receive_skill_push(name, files))
        return results

    def on_session_destroyed(self, session: Any) -> None:
        """Drop this session's flow reference.

        The daemon keeps ``_remote_flows`` so ``handle_rpc`` can find a session's
        flow; a destroyed session must leave it, or the dict grows for the life of
        the process and ``stop()`` later tries to destroy flows that are already
        gone. Cheap here because Linux allocates far less per session than the
        Windows bridge does — no root log handler, no per-session service list
        (the daemon's LLM services are shared and closed once in ``stop()``).

        The ``--close-session`` command handler used to pop this itself, which
        covered exactly one of the four ways a session dies. This covers all of
        them.
        """
        self._daemon._remote_flows.pop(session.session_id, None)

    def on_client_released(self) -> None:
        """Client sent an explicit Disconnect. A Linux daemon exists ONLY to be
        driven, so releasing it means its job is done — exit the whole process.

        The server has already destroyed the sessions and dropped the
        connection by the time this fires; we just need to tear the daemon down.
        Scheduled on the loop rather than done inline so the dispatch that
        triggered it can return cleanly first.
        """
        if self._daemon._logger:
            self._daemon._logger.info(
                "[handq_linux] client released the server — exiting daemon"
            )
        self._daemon.request_shutdown()


class _LinuxDaemon:
    """Resident FlowControllerV2 + file-IPC pump.

    Heavy controller imports are deferred to ``start`` so the console client
    path (which only touches files + subprocess) never pulls the module tree.
    """

    def __init__(self, config_path: Optional[str]) -> None:
        self._config_path = config_path
        self._flow: Any = None
        self._mirror: Optional[StateMirror] = None
        self._consolidated: List[Any] = []
        self._helper: List[Any] = []
        self._logger: Any = None
        # Message ids awaiting an asynchronous task-completion broadcast (see
        # _on_agent_reply). Includes the message currently being drained plus
        # any follow-up submitted while a task was still running.
        self._pending_msgids: set = set()
        # Message ids for which the sink has already written the authoritative
        # reply, so _drain_messages' post-return ack write won't clobber it.
        self._final_reply_msgids: set = set()
        # Direct control channel (docs/fleet_scheduling_design.md). Sessions
        # driven from a Windows HandQ live here, entirely separate from the
        # single file-IPC session above — so the legacy remote_handq_tool path
        # and a direct connection can be in use at the same time without either
        # noticing the other.
        self._remote_server: Any = None
        self._remote_flows: Dict[str, Any] = {}
        #: "" when this machine can authenticate to the LLM, else the reason it
        #: cannot. Computed once in start(); see _llm_credential_problem.
        self._llm_credential_problem: str = ""
        #: Set by run() so request_shutdown() can trip the loop from a client
        #: release. None until the daemon's run loop starts.
        self._stop_event: Any = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _init_skill_registry(self) -> None:
        """Load the Skill roster so read_skill and the [Available Skills] menu
        work on Linux exactly as they do on Windows.

        This is a process-level singleton the Windows bridge initialises in
        ``bridge_main`` (:806-832) and the Linux daemon simply never did — so
        ``SkillRegistry.get()`` fell back to an empty, unscanned instance, the
        agent's skill prelude vanished, and ``read_skill`` failed for every
        name while telling the model to "pick one from the menu" that also
        wasn't there. Skills are the ONE cross-cutting capability a Linux被控
        session is meant to keep (tools are gated Windows-only, LTM/personality
        are deliberately off), so this belongs on the boot path, not out of it.

        Requires the packaged ``Skill/`` directory to actually ship (see
        ``packaging/build_linux.sh``) — without both halves the roster is empty.
        A broken skill file must never stop the daemon coming up, so this is
        best-effort and logged, mirroring the Windows try/except.

        The user root is passed EXPLICITLY as ``<root>/skills``. Left to its
        default, ``_default_skills_root()``'s POSIX branch returns
        ``_install_dir()/Skill`` — which under a frozen build is
        ``<root>/handq_linux.dist/Skill``, i.e. the *same physical directory* as
        the bundled root. Three things went wrong as a result, and this one
        argument fixes all of them:

          * ``_scan_two_roots`` hit its same-root short-circuit, so the
            user-shadows-bundled layering silently did not exist and a pushed
            skill OVERWROTE the shipped copy of the same name (via
            ``receive_skill_push`` → ``_mirror_files_into``, which rmtree's the
            target first) — the exact corruption that function's docstring says
            the design prevents.
          * The deploy script swaps ``handq_linux.dist`` wholesale, so every
            pushed skill was deleted on each upgrade.
          * Under the old ``$HOME``-based layout the directory was cloud-synced,
            so a push on one host tore up a scan in progress on another.
        """
        try:
            from src.infrastructure.skills import SkillRegistry
        except Exception:
            if self._logger:
                self._logger.warning(
                    f"[handq_linux] SkillRegistry import failed; skills disabled: "
                    f"{traceback.format_exc()}"
                )
            return
        try:
            SkillRegistry.init(SKILLS_DIR)
        except Exception:
            if self._logger:
                self._logger.error(
                    f"[handq_linux] SkillRegistry.init failed; continuing with "
                    f"empty registry: {traceback.format_exc()}"
                )
            return
        if self._logger:
            try:
                roster = SkillRegistry.get().debug_roster()
            except Exception:
                roster = []
            self._logger.info(
                f"[handq_linux] SkillRegistry roster: {len(roster)} skill(s): "
                f"{'; '.join(roster) if roster else '(none)'}"
            )

    async def start(self) -> None:
        from src.infrastructure.logger import (
            initialize_logger, LogLevel, get_logger,
        )
        from src.infrastructure.config_manager import ConfigManager

        _ensure_dirs()
        cm = ConfigManager(self._config_path)
        sess_cfg = cm.get_section("session") or {}
        level_name = str(sess_cfg.get("log_level", "INFO") or "INFO").upper()
        try:
            level = LogLevel[level_name]
        except Exception:
            level = LogLevel.INFO
        initialize_logger(
            name="HandQ", level=level, log_file=None, log_dir=str(HANDQ_ROOT)
        )
        self._logger = get_logger()

        self._consolidated, self._helper = _build_llm_services(self._config_path)
        self._llm_credential_problem = _llm_credential_problem(self._config_path)
        if self._llm_credential_problem:
            self._logger.error(f"[handq_linux] {self._llm_credential_problem}")
        # Shared pool, so this is wired once here rather than per session — see
        # _on_llm_server_error for why a per-session closure would be wrong.
        for svc in list(self._consolidated) + list(self._helper):
            svc.on_server_error = self._on_llm_server_error
        self._init_skill_registry()
        session_id = _new_session_id()
        session_dir = self._session_dir(session_id)
        self._mirror = StateMirror(session_id, working_dir=str(session_dir))
        self._flow = self._build_flow(session_id)
        await self._flow.start()
        self._bind_mirror()

        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self._mirror.set_task_status("")
        await self._start_remote_control(cm)
        self._mirror.snapshot()
        self._logger.info(
            f"[handq_linux] daemon up pid={os.getpid()} session={session_id} "
            f"workdir={session_dir} root={HANDQ_ROOT}"
        )

    # ── direct control channel ────────────────────────────────────────────
    async def _start_remote_control(self, cm: Any) -> None:
        """Always serve the direct control channel.

        There is deliberately **no ``serve`` switch on Linux**. A Linux HandQ
        exists to be driven from somewhere else — it has no local UI, so
        "someone might be using it locally and should opt in first" (the reason
        the Windows side has a switch) simply does not apply. Making it
        unconditional also removes an entire class of failure that bit us in
        testing: the Windows auto-pair path used to have to SFTP-patch
        ``remote_control.serve: true`` into the remote config and then restart
        the daemon for it to take effect — which meant deciding whether it was
        safe to interrupt a running task just to open a port. None of that code
        needs to exist now.

        Never raises: a Linux box that cannot bind the port must still work as
        the file-IPC daemon it already was, so a failure is logged and the
        address fields in ``state.json`` simply stay empty. The Windows side
        reads those fields to decide whether a direct connection is available,
        so "empty" is a meaningful, handled answer rather than an error state.
        """
        try:
            from src.remote_control.serving import RemoteControlConfig
            from src.remote_control.server import RemoteControlServer
        except Exception:
            if self._logger:
                self._logger.warning("[handq_linux] remote_control unavailable")
            return

        # There is no `serve` flag any more — a Linux daemon exists to be driven,
        # so it always listens (see remote_control/serving.py's docstring). The
        # section's remaining keys — bind, port, max_sessions,
        # allow_remote_secret_prompt — are honoured.
        rc_cfg = RemoteControlConfig.from_config(cm.get_config())

        try:
            token = rc_cfg.resolve_token()
            server = RemoteControlServer(
                token=token,
                host=_LinuxSessionHost(
                    self, allow_secret_prompt=rc_cfg.allow_remote_secret_prompt
                ),
                server_name=socket.gethostname(),
                max_sessions=rc_cfg.max_sessions,
            )
            port = await server.start(rc_cfg.bind, rc_cfg.port)
            self._remote_server = server
            assert self._mirror is not None
            self._mirror.set_remote_control(port, token)
            # Let state.json expose the live remote sessions to the CLI
            # (--list-sessions / --close-session). The server's own registry is
            # the source of truth; we just surface a summary.
            self._mirror._remote_sessions_provider = self._remote_session_summaries
            if self._logger:
                self._logger.info(
                    f"[handq_linux] remote control listening on "
                    f"{rc_cfg.bind}:{port}"
                )
            # Printed, not just logged, so an operator who started the daemon in
            # a terminal can copy the pairing string straight out of it — the
            # manual-pairing path for Linux (design doc §2).
            from src.remote_control.address import format_address, guess_lan_ip

            print(
                "CONNECT ME: "
                + format_address(
                    guess_lan_ip(), port, token, socket.gethostname()
                ),
                flush=True,
            )
        except Exception as exc:
            if self._logger:
                self._logger.warning(
                    f"[handq_linux] remote control failed to start: {exc}"
                )

    def _session_dir(self, session_id: str) -> Path:
        """The per-session working directory: ``<root>/workspace/<session_id>/``.

        Deterministic from session_id, so both _build_flow (which the agent works
        inside) and the StateMirror (which reports it to the user via state.json)
        resolve the exact same path.

        Lives under the machine-local install root rather than ``$HOME`` — the old
        ``~/<workspace_base>/<session_id>`` was shared across every host whose home
        is cloud-synced, so two daemons that started in the same second wrote their
        digests and execution traces into one directory. ``session.workspace_base``
        still names the subdirectory, so the config knob keeps working; it is just
        no longer relative to ``$HOME``.
        """
        from src.infrastructure.config_manager import ConfigManager
        cm = ConfigManager(self._config_path)
        sess_cfg = cm.get_section("session") or {}
        workspace_base = sess_cfg.get("workspace_base", "workspace") or "workspace"
        return HANDQ_ROOT / workspace_base / session_id

    def _build_flow(self, session_id: str) -> Any:
        from src.controller_v2.flow_controller import FlowControllerV2

        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        # Linux CLI keeps expose_session_storage_in_prompt at its default (True):
        # the agent's mental model is the session dir, where it works directly.
        return FlowControllerV2(
            llm_services=self._consolidated,
            working_directory=str(session_dir),
            storage_directory=str(session_dir),
            config_path=self._config_path,
            on_reply_to_user=self._on_agent_reply,
            helper_llm_services=self._helper,
            # Passed so the session digest carries a real id rather than "".
            # The Windows bridge always passes it (stdio_bridge._ensure_flow);
            # omitting it here made every Linux digest.json — including those a
            # remote-driven rc- session writes — record an empty session_id.
            session_id=session_id,
        )

    def _bind_mirror(self) -> None:
        assert self._mirror is not None
        if self._flow.interaction_manager is not None:
            self._flow.interaction_manager.set_delegate(self._mirror)
        self._mirror.attach_task_channel(self._flow._task_channel)

    def _on_llm_server_error(self, msg: str, retry_in: int, attempts_left: int) -> None:
        """Fan an LLM-service error out to every live remote session.

        Cannot be a per-session closure the way ``stdio_bridge`` does it
        (``stdio_bridge.py``'s ``_on_llm_server_error``): the Windows bridge builds
        a pool per session, while this daemon shares one pool across all of them,
        so the last session created would own the callback and every other
        operator would be told nothing. Broadcasting is also the honest answer —
        a server-side problem is session-agnostic.

        Without this, a controller driving this machine saw nothing at all while a
        retry ladder ran: ``on_server_error`` was only ever wired on the Windows
        side, so the被控 Linux path had no way to say "still trying".
        """
        server = self._remote_server
        if server is None:
            return
        text = f"⚠ 远端 LLM 服务错误：{msg}（{retry_in}s 后重试，还剩 {attempts_left} 次）"
        for session in server.sessions():
            try:
                session.publish_event("show_user_notice", [text, False])
            except Exception:
                pass

    def _on_agent_reply(self, reply: str) -> None:
        """Sink for ``FlowControllerV2.on_reply_to_user`` — the authoritative reply.

        V2 runs task execution in a background task: ``on_user_message`` returns
        only the initial ack, and the real completion summary is emitted
        later via ``Orchestrator._emit_completion_reply`` (unconditional, full
        text). This callback carries no per-task correlation id, and more than
        one msgid can be pending at once (a follow-up submitted via
        send_message while a task is still running). We tell the two firing
        modes apart via ``_current_msgid`` (module-level ContextVar):

          * DIRECT — fires synchronously inside _drain_messages' own
            ``await on_user_message(...)`` (the chat path, or a synchronous
            error fallback). ``_current_msgid.get()`` is non-None
            here, naming exactly the message this reply belongs to.
          * ASYNC — fires later from the daemon's independent agent
            background task (the real task-completion summary).
            ``_current_msgid.get()`` is None here — that task's context was
            snapshotted at ``start()``, before any message existed, so it
            never observes _drain_messages' ``.set()``. With no correlation id
            to target, broadcast to every msgid still awaiting its reply.
        """
        direct = _current_msgid.get()
        targets = [direct] if direct is not None else list(self._pending_msgids)
        for msgid in targets:
            if msgid in self._final_reply_msgids:
                continue
            try:
                _atomic_write_text(REPLY_DIR / f"{msgid}.txt", reply or "")
                self._final_reply_msgids.add(msgid)
            except Exception:
                pass
        # Once the task channel has genuinely settled, every id this broadcast
        # covered is done — drop them so a later, unrelated task's completion
        # doesn't get broadcast to them too.
        cl = self._flow._task_channel if self._flow is not None else None
        if direct is None and cl is not None and cl.get_current_item() is None and not cl.has_pending:
            self._pending_msgids.clear()
        if self._mirror is not None:
            self._mirror.snapshot()

    async def _new_session(self) -> None:
        """Tear down the current session and start a fresh one (handq --new)."""
        old = self._flow
        session_id = _new_session_id()
        session_dir = self._session_dir(session_id)
        self._pending_msgids.clear()
        self._final_reply_msgids.clear()
        self._flow = self._build_flow(session_id)
        await self._flow.start()
        assert self._mirror is not None
        self._mirror.reset(session_id, working_dir=str(session_dir))
        self._bind_mirror()
        self._mirror.snapshot()
        if old is not None:
            try:
                await old.destroy()
            except Exception:
                pass
        if self._logger:
            self._logger.info(f"[handq_linux] new session={session_id} workdir={session_dir}")

    async def stop(self) -> None:
        # Remote control first, so attached controllers are told the sessions are
        # ending on purpose (server_shutdown) rather than seeing a bare socket
        # close and starting to reconnect to a process that is exiting.
        if self._remote_server is not None:
            try:
                await self._remote_server.stop()
            except Exception:
                pass
            self._remote_server = None
        for flow in list(self._remote_flows.values()):
            try:
                await flow.destroy()
            except Exception:
                pass
        self._remote_flows.clear()

        if self._flow is not None:
            try:
                await self._flow.destroy()
            except Exception:
                pass
        for svc in list(self._consolidated) + list(self._helper):
            try:
                await svc.close()
            except Exception:
                pass
        _unlink(PID_FILE)
        _unlink(STATE_FILE)

    # ── main pump ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        import asyncio
        import signal

        stop = asyncio.Event()
        # Expose the stop event so request_shutdown() (called from the server's
        # on_client_released hook when a client releases us) can trip it from
        # outside the run loop.
        self._stop_event = stop
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):
                pass

        try:
            _last_remote_snapshot = 0.0
            while not stop.is_set():
                await self._drain_commands()
                await self._drain_messages()
                # Refresh state.json's remote_sessions list periodically so the
                # CLI's --list-sessions reflects sessions that came/went via the
                # direct channel (which doesn't touch the file-IPC snapshot path).
                if self._remote_server is not None and self._mirror is not None:
                    now = time.monotonic()
                    if now - _last_remote_snapshot >= 2.0:
                        _last_remote_snapshot = now
                        self._mirror.snapshot()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    def request_shutdown(self) -> None:
        """Trip the run loop's stop event from outside (e.g. a client release).

        Safe to call from a coroutine running on the daemon's own loop; the
        next loop iteration sees the event set and falls into ``self.stop()``.
        """
        ev = getattr(self, "_stop_event", None)
        if ev is not None:
            ev.set()

    def _remote_session_summaries(self) -> list:
        """Summaries of the live remote-driven sessions, for state.json.

        Reads the server's own session registry (the source of truth) rather
        than _remote_flows, so the fields match what the client sees
        (session.describe()). Returns [] when not serving.
        """
        server = self._remote_server
        if server is None:
            return []
        try:
            return [s.describe() for s in server.sessions()]
        except Exception:
            return []

    async def _drain_commands(self) -> None:
        for cmd_file in sorted(COMMANDS_DIR.glob("*.json")):
            cmd = _read_json(cmd_file) or {}
            _archive(cmd_file)
            await self._handle_command(cmd)

    async def _handle_command(self, cmd: Dict[str, Any]) -> None:
        action = cmd.get("action", "")
        if action == "interrupt":
            cl = self._flow._task_channel if self._flow is not None else None
            if cl is not None:
                try:
                    if cl.get_current_item() is not None:
                        # A task is in flight: clear the pending tail first,
                        # then abort the in-flight item (order per
                        # TaskChannel.replace_post_current). The agent acks
                        # the interrupt inside its item loop, releasing
                        # interrupt_agent's wait on _interrupt_acked.
                        await cl.replace_post_current([])
                        # Re-check with NO await before signalling: the item may
                        # have completed during the replace_post_current await,
                        # parking the agent at wait_for_current_item() — and a
                        # parked agent never acks, which would wedge the next
                        # interrupt. interrupt_agent won't yield here because
                        # _interrupt_acked is set, so this check stays valid.
                        if cl.get_current_item() is not None:
                            await cl.interrupt_agent(
                                cmd.get("reason", "remote interrupt")
                            )
                    else:
                        # Idle: the agent is parked at wait_for_current_item()
                        # and will never ack an interrupt. Calling
                        # interrupt_agent here would leave _interrupt_event set
                        # (spuriously aborting the next task's first item) and,
                        # on a second idle interrupt, block _drain_commands
                        # forever on _interrupt_acked. So only drop any pending
                        # tail; never signal an interrupt when nothing runs.
                        await cl.replace_post_current([])
                except Exception:
                    pass
            if self._mirror is not None:
                self._mirror.set_task_status("idle")
                self._mirror.snapshot()
        elif action == "new_session":
            await self._new_session()
        elif action == "close_session":
            # CLI --close-session <id>: destroy one remote-driven session.
            # Goes through the server's registry so the client's tab is told
            # (session_closed) and the session is really gone, not just detached.
            # The _remote_flows entry is dropped by
            # _LinuxSessionHost.on_session_destroyed, which the server calls for
            # every destruction path rather than only this one.
            sid = str(cmd.get("session_id") or "")
            server = self._remote_server
            if sid and server is not None:
                try:
                    await server.close_session_by_id(sid)
                    if self._mirror is not None:
                        self._mirror.snapshot()
                except Exception:
                    if self._logger:
                        self._logger.warning(
                            f"[handq_linux] close_session {sid} failed: "
                            f"{traceback.format_exc()}")
        elif action == "exit_server":
            # CLI-driven full stop of the server role. On Linux that means
            # exiting the daemon entirely (its only purpose is being served).
            self.request_shutdown()

    async def _drain_messages(self) -> None:
        for msg_file in sorted(MESSAGES_DIR.glob("*.txt")):
            stem = msg_file.stem
            text = _read_text(msg_file)
            _archive(msg_file)
            if not text.strip():
                continue
            # Mark this message the DIRECT target for any reply produced
            # synchronously within the upcoming await (chat, or a synchronous
            # error fallback) — see _on_agent_reply / _current_msgid.
            # Deliberately NOT added to _pending_msgids yet: that set is what
            # an unrelated task's ASYNC completion broadcasts to, and until
            # on_user_message returns we don't yet know whether this message
            # became a real in-flight task — adding it early would expose it
            # to a same-event-loop race where another task's background
            # completion fires (and broadcasts) while this await is still
            # in flight, clobbering this message's reply before it starts.
            if self._mirror is not None:
                self._mirror.mark_task_starting()
                self._mirror.snapshot()
            token = _current_msgid.set(stem)
            try:
                reply = await self._flow.on_user_message(text)
            except Exception as exc:
                reply = f"[handq_linux] error: {exc}"
            finally:
                _current_msgid.reset(token)
            # on_user_message has returned; the coordinator has already
            # applied its item synchronously. Only now can we tell whether
            # this message left real work in flight — if so, register it so
            # the eventual ASYNC completion (background agent task) knows
            # to broadcast to it too.
            cl = self._flow._task_channel if self._flow is not None else None
            still_in_flight = cl is not None and (
                cl.get_current_item() is not None or cl.has_pending
            )
            if still_in_flight and stem not in self._final_reply_msgids:
                self._pending_msgids.add(stem)
            # Write the return value only if the sink hasn't already delivered
            # the authoritative reply for this id. Chat → the return IS the
            # reply (the sink already wrote it as the DIRECT target above, so
            # this is normally a no-op guarded by _final_reply_msgids). Task →
            # the return is the initial ack; it serves as an immediate
            # placeholder, and the async sink overwrites it with the full
            # completion summary once the task settles (which, for a very fast
            # task, may have already happened — hence the guard).
            if stem not in self._final_reply_msgids:
                _atomic_write_text(REPLY_DIR / f"{stem}.txt", reply or "")
            if self._mirror is not None:
                # Settle to idle when nothing is in flight. A freshly-submitted
                # task leaves its first item current here (the coordinator
                # applies it before on_user_message returns), so this never
                # settles a running task — show_state_changed drives that to
                # idle at completion. But the pure-chat path runs no agent and
                # so never emits an "idle" state change; without this the
                # optimistic "running" set above would strand and get_status
                # would report a finished chat reply as a perpetually running task.
                if not still_in_flight:
                    self._mirror.set_task_status("idle")
                self._mirror.snapshot()


def _new_session_id() -> str:
    """A fresh session id: timestamp plus a short random suffix.

    The suffix is not decoration. Second resolution alone collides in practice:
    ``_new_session`` runs on every ``new_session`` command from the Windows side,
    and a daemon can be restarted inside the same second. Two sessions sharing an
    id share a working directory — ``_build_flow``'s ``mkdir(exist_ok=True)``
    silently reuses it, ``SessionDigest.load`` then adopts the PREVIOUS session's
    digest as ``prior``, and ``ExecutionRecorder`` appends into the old session's
    records. Same idiom as ``_submit_message``'s msgid.
    """
    return datetime.now().strftime("session_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def _archive(path: Path) -> None:
    """Move a consumed message/command file into the .processed archive."""
    try:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(path, PROCESSED_DIR / path.name)
    except Exception:
        _unlink(path)


def _llm_credential_problem(config_path: Optional[str]) -> str:
    """Return "" if this machine can authenticate to the LLM, else why it cannot.

    A blank ``llm.API_KEY`` is *survivable* on a Windows controller: with an empty
    key the Anthropic SDK falls back to ``ANTHROPIC_API_KEY`` /
    ``ANTHROPIC_AUTH_TOKEN`` from the environment, so that machine keeps working
    and nothing there complains. A daemon has no such environment, so the same
    blank surfaces as ``TypeError: Could not resolve authentication method`` on the
    first LLM call of every session — and that is precisely how a blank got here
    once already: an upgrade wiped the controller's key and its next deploy copied
    the blank over a key that had been working.

    Checked up front so the failure is one readable line in daemon.log and one
    immediate message in the operator's tab, rather than a stall.
    """
    from src.infrastructure.config_manager import ConfigManager

    llm_cfg = ConfigManager(config_path).get_section("llm") or {}
    if str(llm_cfg.get("API_KEY") or "").strip():
        return ""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(name, "").strip():
            return ""
    return (
        f"llm.API_KEY 为空（{config_path or 'handq_config.yaml'}），且环境变量 "
        "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 都没有设置 —— 这台机器上的每一次 "
        "LLM 调用都会失败。请在这台机器的 handq_config.yaml 里填入 llm.API_KEY"
        "（控制端每次连接时也会自动同步这一段）。"
    )


def _build_llm_services(config_path: Optional[str]) -> Tuple[List[Any], List[Any]]:
    """Build (consolidated, helper) LLM service pools from config.

    Mirrors stdio_bridge's construction: one AnthropicStreamingService per
    model in priority order (the fallback chain), plus a distinct helper pool
    (llm.helper_models, falling back to the main models). The helper pool is
    threaded through to FlowControllerV2/Orchestrator for constructor
    compatibility but currently has no live consumer on this (LTM-less) Linux
    path — kept for pool-lifecycle symmetry with stdio_bridge's shutdown.
    """
    from src.infrastructure.anthropic_streaming_service import AnthropicStreamingService
    from src.infrastructure.config_manager import ConfigManager
    from src.infrastructure.role_resolver import resolve_models_and_helper

    cm = ConfigManager(config_path)
    llm_cfg = cm.get_section("llm") or {}
    api_key = llm_cfg.get("API_KEY") or ""

    models, helper_models = resolve_models_and_helper(llm_cfg)
    if not models:
        models = ["anthropic::claude-4-5-haiku"]

    consolidated = [
        AnthropicStreamingService(api_key=api_key, model=m, max_retries=10)
        for m in models
    ]
    helper = [
        AnthropicStreamingService(api_key=api_key, model=m, max_retries=10)
        for m in (helper_models or models)
    ]
    return consolidated, helper


def _run_daemon(config_path: Optional[str]) -> int:
    import asyncio

    daemon = _LinuxDaemon(config_path)

    async def _main() -> None:
        await daemon.start()
        await daemon.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        try:
            (HANDQ_ROOT / "daemon_error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  Console client (lightweight — no controller imports)
# ─────────────────────────────────────────────────────────────────────────────
def _daemon_alive() -> bool:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _self_invocation() -> List[str]:
    """Argv prefix that re-invokes this program (handles frozen binaries)."""
    if _IS_FROZEN:
        return [os.path.abspath(sys.argv[0])]
    return [sys.executable, os.path.abspath(__file__)]


def _spawn_daemon(config_path: Optional[str]) -> None:
    """Launch the resident daemon detached from this terminal (setsid)."""
    _ensure_dirs()
    args = _self_invocation() + ["--_daemon"]
    if config_path:
        args += ["--config", config_path]
    log = open(DAEMON_LOG, "a", encoding="utf-8")
    subprocess.Popen(
        args,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # setsid(2): detach from the controlling tty
        close_fds=True,
    )


def _ensure_daemon(config_path: Optional[str], timeout: float = 15.0) -> bool:
    if _daemon_alive():
        return True
    _spawn_daemon(config_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _daemon_alive():
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _session_banner() -> str:
    """One line telling the user which session they're on and where it works.

    Read straight from the daemon's state.json so it reflects the live
    session — the same session_id / working_dir the agent is actually using.
    Returns "" if the daemon isn't up yet or hasn't written state.
    """
    state = _read_json(STATE_FILE) or {}
    sid = state.get("session_id", "")
    wd = state.get("working_dir", "")
    if not sid:
        return ""
    parts = [f"session {sid}"]
    if wd:
        parts.append(f"workdir {wd}")
    return "  ".join(parts)


def _remember_session_id() -> Optional[str]:
    """The session_id this client last saw, persisted in the IPC dir so a
    later `handq_linux` invocation can tell 'same session as before' from
    'the daemon restarted / someone ran --new since I was last here'."""
    return _read_text(LAST_SEEN_SESSION_FILE).strip() or None


def _write_seen_session_id(sid: str) -> None:
    if sid:
        _atomic_write_text(LAST_SEEN_SESSION_FILE, sid)


def _wait_new_session(timeout: float = 15.0) -> None:
    """After posting a new_session command, wait for the daemon to actually
    swap in a session_id different from the one we last saw. Best-effort: on
    timeout we just fall through and let the banner report whatever's current."""
    prev = _remember_session_id()
    deadline = time.time() + timeout
    while time.time() < deadline:
        sid = (_read_json(STATE_FILE) or {}).get("session_id", "")
        if sid and sid != prev:
            return
        time.sleep(POLL_INTERVAL)


def _announce_session(config_path: Optional[str]) -> None:
    """Print the current session banner and whether it continues the one this
    client last interacted with — the signal a user needs to decide whether to
    keep going or start fresh with `--new` / the console `new` command."""
    # The daemon writes state.json immediately after the pid file, but
    # _ensure_daemon returns on pid-file liveness alone; give the session_id a
    # brief moment to appear so a freshly-spawned daemon still gets a banner.
    deadline = time.time() + 3.0
    state: Dict[str, Any] = {}
    while time.time() < deadline:
        state = _read_json(STATE_FILE) or {}
        if state.get("session_id"):
            break
        time.sleep(POLL_INTERVAL)
    sid = state.get("session_id", "")
    if not sid:
        return
    prev = _remember_session_id()
    banner = _session_banner()
    if prev is None:
        print(f"· {banner}", file=sys.stderr)
    elif prev == sid:
        print(f"· continuing {banner}", file=sys.stderr)
    else:
        print(f"· NEW session (previous {prev} is gone) — {banner}", file=sys.stderr)
    _write_seen_session_id(sid)


def _submit_message(text: str) -> str:
    _ensure_dirs()
    msgid = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    _atomic_write_text(MESSAGES_DIR / f"{msgid}.txt", text)
    return msgid


def _wait_reply(msgid: str, timeout: float = 600.0) -> Optional[str]:
    path = REPLY_DIR / f"{msgid}.txt"
    handled: set = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        # While a task runs it may raise a risk / tool / secret / ask_human
        # gate. Windows normally answers these over the same file pipe; when
        # only the local console is up, pump them here so the emergency channel
        # can actually unblock the agent instead of letting it time out.
        _pump_confirmation(handled)
        if path.exists():
            txt = _read_text(path)
            _unlink(path)
            return txt
        time.sleep(POLL_INTERVAL)
    return None


def _pump_confirmation(handled: set) -> None:
    """Answer one pending confirmation request interactively, if present.

    ``handled`` tracks request ids already answered this wait so a slow daemon
    (which unlinks the request only after reading our response) isn't prompted
    twice for the same id. Mirrors ``StateMirror._await_response``'s contract:
    a response is ``{"id", "decision"?, "message"?, "value"?}``.
    """
    req = _read_json(CONFIRM_REQUEST_FILE)
    if not req:
        return
    cid = req.get("id")
    if not cid or cid in handled:
        return
    kind = req.get("kind", "")
    resp: Dict[str, Any] = {"id": cid}
    print()  # break off the silent wait line
    if kind == "secret":
        resp["value"] = getpass.getpass(f"[confirm] {req.get('prompt', 'secret')}: ")
    elif kind == "form":
        question = str(req.get("question") or "")
        if question:
            print(f"[confirm] {question}")
        answers: Dict[str, Any] = {}
        for field in req.get("fields") or []:
            fid = str(field.get("id") or "")
            if not fid:
                continue
            label = str(field.get("label") or fid)
            ftype = str(field.get("type") or "text")
            options = field.get("options") or []
            if ftype == "radio" and options:
                print(f"  {label}:")
                for i, opt in enumerate(options, 1):
                    print(f"    {i}. {opt}")
                choice = _prompt("  choose number> ").strip()
                try:
                    answers[fid] = options[int(choice) - 1]
                except (ValueError, IndexError):
                    answers[fid] = choice
            elif ftype == "checkbox" and options:
                print(f"  {label}:")
                for i, opt in enumerate(options, 1):
                    print(f"    {i}. {opt}")
                choice = _prompt("  choose numbers (comma-separated)> ").strip()
                picked = []
                for tok in choice.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        picked.append(options[int(tok) - 1])
                    except (ValueError, IndexError):
                        pass
                answers[fid] = picked
            else:
                answers[fid] = _prompt(f"  {label}: ")
        resp["value"] = answers
    elif kind in ("tool", "risk"):
        if kind == "tool":
            label = f"run tool '{req.get('tool_name')}'"
            extra = str(req.get("hint") or "")
        else:
            label = "approve high-risk operation"
            extra = str(req.get("description") or "")
        if extra:
            print(f"  {extra}")
        ans = _prompt(f"[confirm] {label}? [y]es / [n]o / [m]essage: ").strip().lower()
        if ans.startswith("y"):
            resp["decision"] = "yes"
        elif ans.startswith("m"):
            resp["decision"] = "message"
            resp["message"] = _prompt("  message> ")
        else:
            resp["decision"] = "no"
    else:
        resp["decision"] = "no"
    _atomic_write_json(CONFIRM_RESPONSE_FILE, resp)
    handled.add(cid)


def _prompt(text: str) -> str:
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        return ""


def _send_command(action: str, **fields: Any) -> None:
    _ensure_dirs()
    cid = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    _atomic_write_json(COMMANDS_DIR / f"{cid}.json", {"action": action, **fields})


def cmd_goal(text: str, config_path: Optional[str]) -> int:
    if not _ensure_daemon(config_path):
        print("handq_linux: failed to start daemon (see daemon.log).", file=sys.stderr)
        return 1
    _announce_session(config_path)
    msgid = _submit_message(text)
    print("· submitted; waiting for reply…", file=sys.stderr)
    reply = _wait_reply(msgid)
    if reply is None:
        print("handq_linux: timed out waiting for reply (task may still be running; "
              "use --status).", file=sys.stderr)
        return 1
    print(reply)
    return 0


def cmd_console(config_path: Optional[str]) -> int:
    if not _ensure_daemon(config_path):
        print("handq_linux: failed to start daemon (see daemon.log).", file=sys.stderr)
        return 1
    _announce_session(config_path)
    print("HandQ (Linux) — type a goal, 'new' for a fresh session, 'status' to "
          "inspect, or 'exit' to leave the console (daemon keeps running).")
    while True:
        try:
            line = input("handq> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "status":
            cmd_status()
            continue
        if line == "new":
            _send_command("new_session")
            # The daemon swaps the session asynchronously; wait for the new
            # session_id to land so the banner reflects reality, not the old one.
            _wait_new_session()
            _announce_session(config_path)
            continue
        msgid = _submit_message(line)
        reply = _wait_reply(msgid)
        print(reply if reply is not None else
              "(no reply yet — task may still be running; type 'status')")


def cmd_new(config_path: Optional[str]) -> int:
    if _daemon_alive():
        _send_command("new_session")
        _wait_new_session()
        print("· new session requested.")
    else:
        if not _ensure_daemon(config_path):
            print("handq_linux: failed to start daemon (see daemon.log).", file=sys.stderr)
            return 1
        print("· daemon started with a fresh session.")
    _announce_session(config_path)
    return 0


def cmd_status() -> int:
    if not _daemon_alive():
        print("handq_linux: daemon not running.")
        return 1
    state = _read_json(STATE_FILE) or {}
    # Human-readable session/workdir line to stderr; full JSON stays on stdout
    # so anything parsing --status output is unaffected.
    banner = _session_banner()
    if banner:
        print(f"· {banner}", file=sys.stderr)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_exit() -> int:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        print("handq_linux: daemon not running.")
        return 0
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        _unlink(PID_FILE)
        print("handq_linux: daemon not running.")
        return 0
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not _daemon_alive():
            print("· daemon stopped.")
            return 0
        time.sleep(POLL_INTERVAL)
    print("handq_linux: daemon did not stop in time.", file=sys.stderr)
    return 1


def cmd_list_sessions() -> int:
    """List the active remote-driven sessions (rc-*), for operators managing a
    headless Linux server. Reads state.json (the daemon keeps it fresh)."""
    if not _daemon_alive():
        print("handq_linux: daemon not running.")
        return 1
    state = _read_json(STATE_FILE) or {}
    sessions = state.get("remote_sessions") or []
    port = state.get("remote_control_port") or 0
    if not port:
        print("· server not listening (no remote_control_port in state.json).",
              file=sys.stderr)
    if not sessions:
        print("· no active remote sessions.", file=sys.stderr)
    else:
        print(f"· {len(sessions)} active remote session(s):", file=sys.stderr)
    # Machine-readable list on stdout.
    print(json.dumps(sessions, ensure_ascii=False, indent=2))
    return 0


def cmd_close_session(session_id: str) -> int:
    """Destroy one remote-driven session by id. The client's tab is told and
    cannot recover it."""
    if not session_id:
        print("handq_linux: --close-session requires a session id.", file=sys.stderr)
        return 2
    if not _daemon_alive():
        print("handq_linux: daemon not running.")
        return 1
    _send_command("close_session", session_id=session_id)
    print(f"· requested close of session {session_id}.", file=sys.stderr)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  Arg parsing + dispatch
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_version(config_path: Optional[str]) -> str:
    """App version: the config's top-level ``version:``, else ``__version__``.

    Done with a tiny line scan rather than ConfigManager/yaml so ``--version``
    stays on the lightweight console path (no controller imports) and still
    works when no valid config is present.
    """
    p = Path(config_path) if config_path else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None
    )
    if p is not None and p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("version:"):
                    v = line.split(":", 1)[1].strip().strip("'\"")
                    if v:
                        return v
        except Exception:
            pass
    return __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handq_linux",
        description="HandQ (Linux) — resident AI task daemon controlled by "
                    "Windows HandQ, with a local emergency console.",
    )
    parser.add_argument("--config", "-c", default=None, metavar="PATH",
                        help="Path to config YAML (default: handq_config.yaml next to this file).")
    parser.add_argument("--goal", "-g", default=None,
                        help="Submit a goal directly and print the reply.")
    parser.add_argument("--new", action="store_true", dest="cmd_new",
                        help="Start a fresh session (restart if running).")
    parser.add_argument("--status", action="store_true", dest="cmd_status",
                        help="Print the daemon's state.json.")
    parser.add_argument("--exit", action="store_true", dest="cmd_exit",
                        help="Stop the daemon.")
    parser.add_argument("--list-sessions", action="store_true", dest="cmd_list_sessions",
                        help="List active remote-driven sessions.")
    parser.add_argument("--close-session", metavar="ID", default=None,
                        dest="cmd_close_session",
                        help="Destroy the named remote session.")
    parser.add_argument("--version", "-V", action="store_true", dest="show_version",
                        help="Print the version and exit.")
    parser.add_argument("--_daemon", action="store_true", dest="run_daemon",
                        help=argparse.SUPPRESS)
    parser.add_argument("inline_goal", nargs="*", default=[],
                        help="Goal text to submit directly.")
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    config_path: Optional[str] = None
    if args.config:
        config_path = str(Path(args.config).resolve())
    elif DEFAULT_CONFIG.exists():
        config_path = str(DEFAULT_CONFIG)

    if args.show_version:
        print(f"handq_linux {_resolve_version(config_path)}")
        sys.exit(0)
    if args.run_daemon:
        sys.exit(_run_daemon(config_path))
    if args.cmd_exit:
        sys.exit(cmd_exit())
    if args.cmd_list_sessions:
        sys.exit(cmd_list_sessions())
    if args.cmd_close_session is not None:
        sys.exit(cmd_close_session(str(args.cmd_close_session)))
    if args.cmd_status:
        sys.exit(cmd_status())
    if args.cmd_new:
        sys.exit(cmd_new(config_path))

    goal = args.goal or (" ".join(args.inline_goal).strip() if args.inline_goal else "")
    if goal:
        sys.exit(cmd_goal(goal, config_path))
    sys.exit(cmd_console(config_path))


if __name__ == "__main__":
    main()
