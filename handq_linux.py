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
  * No tmux, no systemd, no LTM / scheduler / personality.
  * The daemon is a plain Python process detached with ``setsid``
    (``start_new_session=True``); Windows (or this console) can wake it at any
    time. The *process* persists across Windows power / network loss — an
    in-flight task is NOT replayed if the daemon itself dies (task-level
    durability is out of scope; see the deferred notes in the design).
  * Windows-only tools are gated off by the ``flow_controller`` platform check,
    so a bare ``FlowControllerV2`` on Linux already exposes exactly
    shell / ssh + the file built-ins + the coding context provider.

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

File IPC layout (``~/.handq/<user>@<host>/``):
  state.json            daemon writes coarse status + latest_tool + checklist
  messages/<id>.txt     inbound goal / follow-up (console AND Windows write here)
  commands/<id>.json    inbound new_session / interrupt
  reply/<id>.txt        outbound reply (console fetches it; keyed by message id)
  confirmation_request.json / confirmation_response.json
                        bidirectional tool / risk / secret / ask_human confirms
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import subprocess
import sys
import time
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

# App version. Fallback only — the deployed version is read from the config's
# top-level ``version:`` when present (see ``_resolve_version``).
__version__ = "1.3.0"


# ── IPC layout (~/.handq/<user>@<host>/) ─────────────────────────────────────
def _short_host() -> str:
    return socket.gethostname().split(".")[0]


def _user_name() -> str:
    # getpass.getuser() consults env then pwd; mirrors the $(whoami) probe that
    # remote_handq_tool runs so both sides resolve the SAME directory.
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "default")


HANDQ_DIR = Path.home() / ".handq" / f"{_user_name()}@{_short_host()}"
STATE_FILE = HANDQ_DIR / "state.json"
PID_FILE = HANDQ_DIR / "handq.pid"
MESSAGES_DIR = HANDQ_DIR / "messages"
COMMANDS_DIR = HANDQ_DIR / "commands"
REPLY_DIR = HANDQ_DIR / "reply"
PROCESSED_DIR = MESSAGES_DIR / ".processed"
CONFIRM_REQUEST_FILE = HANDQ_DIR / "confirmation_request.json"
CONFIRM_RESPONSE_FILE = HANDQ_DIR / "confirmation_response.json"
DAEMON_LOG = HANDQ_DIR / "daemon.log"


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
    for d in (HANDQ_DIR, MESSAGES_DIR, COMMANDS_DIR, REPLY_DIR, PROCESSED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Restrict the IPC root to its owner: anyone who can write here can drive
    # the agent (submit goals, answer confirmations). Best-effort — a no-op on
    # filesystems without POSIX permissions.
    try:
        HANDQ_DIR.chmod(0o700)
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
    this class deliberately omits the receptionist-streaming hooks.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._checklist: Any = None
        self._status = "idle"
        # "" = no task yet, "running" = task in flight, "idle" = settled.
        self._task_status = ""
        self._latest_tool: Optional[Dict[str, Any]] = None

    # ── wiring ────────────────────────────────────────────────────────────
    def attach_checklist(self, checklist: Any) -> None:
        self._checklist = checklist

    def reset(self, session_id: str) -> None:
        self._session_id = session_id
        self._status = "idle"
        self._task_status = ""
        self._latest_tool = None

    def set_task_status(self, status: str) -> None:
        self._task_status = status

    # ── state-mirroring delegate hooks ───────────────────────────────────
    def show_state_changed(self, state: str) -> None:
        self._status = state
        # "idle" is the only host-visible task-settled signal.
        self._task_status = "idle" if state == "idle" else "running"
        self.snapshot()

    def show_inline_event(self, icon: str, desc: str) -> None:
        # Coarse one-liner; folded into status_text so Windows sees the latest
        # planner banner without a separate field.
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
        checklist_text = ""
        completed = total = 0
        if self._checklist is not None:
            try:
                total = int(self._checklist.total_items)
                completed = int(self._checklist.completed_count)
                if total > 0:
                    checklist_text = (
                        self._checklist.get_checklist_context_for_planner()
                    )
            except Exception:
                pass
        state = {
            "pid": os.getpid(),
            "handq_active": True,
            "session_id": self._session_id,
            "task_status": self._task_status,
            "status_text": self._status,
            "latest_tool": self._latest_tool,
            "checklist": checklist_text,
            "completed": completed,
            "total": total,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _atomic_write_json(STATE_FILE, state)
        except Exception:
            pass

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

    async def request_user_text(self, prompt: str) -> str:
        resp = await self._await_response("text", {"prompt": str(prompt)})
        return (resp or {}).get("value", "")

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
        # The message id currently being processed by on_user_message. The
        # on_reply_to_user sink uses it to key the reply file, since the real
        # completion summary is delivered asynchronously (after on_user_message
        # has already returned the plan ack).
        self._active_msgid: Optional[str] = None
        # Message ids for which the sink has already written the authoritative
        # reply, so _drain_messages' post-return ack write won't clobber it.
        self._final_reply_msgids: set = set()

    # ── lifecycle ─────────────────────────────────────────────────────────
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
            name="HandQ", level=level, log_file=None, log_dir=str(HANDQ_DIR)
        )
        self._logger = get_logger()

        self._consolidated, self._helper = _build_llm_services(self._config_path)
        session_id = _new_session_id()
        self._mirror = StateMirror(session_id)
        self._flow = self._build_flow(session_id)
        await self._flow.start()
        self._bind_mirror()

        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self._mirror.set_task_status("")
        self._mirror.snapshot()
        self._logger.info(
            "[handq_linux] daemon up pid=%s session=%s dir=%s",
            os.getpid(), session_id, HANDQ_DIR,
        )

    def _build_flow(self, session_id: str) -> Any:
        from src.controller_v2.flow_controller import FlowControllerV2
        from src.infrastructure.config_manager import ConfigManager

        cm = ConfigManager(self._config_path)
        sess_cfg = cm.get_section("session") or {}
        workspace_base = sess_cfg.get("workspace_base", ".workspace") or ".workspace"
        session_dir = Path.home() / workspace_base / session_id
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
        )

    def _bind_mirror(self) -> None:
        assert self._mirror is not None
        if self._flow.interaction_manager is not None:
            self._flow.interaction_manager.set_delegate(self._mirror)
        self._mirror.attach_checklist(self._flow._checklist)

    def _on_agent_reply(self, reply: str) -> None:
        """Sink for ``FlowControllerV2.on_reply_to_user`` — the authoritative reply.

        V2 runs task execution in a background task: ``on_user_message`` returns
        only the initial plan ack, and the real completion summary is emitted
        later via ``Orchestrator._emit_completion_reply`` (unconditional, full
        text). This sink writes that FULL reply to the active message's reply
        file and records its id so the post-return ack write in
        ``_drain_messages`` can't clobber it.

        For the chat path the sink fires synchronously inside ``on_user_message``
        (same id) — harmless, the reply file simply gets the chat reply.

        Known limitation: overlapping goals share ``_active_msgid``, so a reply
        is attributed to whichever message is currently active.
        """
        msgid = self._active_msgid
        if not msgid:
            return
        try:
            _atomic_write_text(REPLY_DIR / f"{msgid}.txt", reply or "")
            self._final_reply_msgids.add(msgid)
        except Exception:
            pass
        if self._mirror is not None:
            self._mirror.snapshot()

    async def _new_session(self) -> None:
        """Tear down the current session and start a fresh one (handq --new)."""
        old = self._flow
        session_id = _new_session_id()
        self._active_msgid = None
        self._final_reply_msgids.clear()
        self._flow = self._build_flow(session_id)
        await self._flow.start()
        assert self._mirror is not None
        self._mirror.reset(session_id)
        self._bind_mirror()
        self._mirror.snapshot()
        if old is not None:
            try:
                await old.destroy()
            except Exception:
                pass
        if self._logger:
            self._logger.info("[handq_linux] new session=%s", session_id)

    async def stop(self) -> None:
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
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):
                pass

        try:
            while not stop.is_set():
                await self._drain_commands()
                await self._drain_messages()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop()

    async def _drain_commands(self) -> None:
        for cmd_file in sorted(COMMANDS_DIR.glob("*.json")):
            cmd = _read_json(cmd_file) or {}
            _archive(cmd_file)
            await self._handle_command(cmd)

    async def _handle_command(self, cmd: Dict[str, Any]) -> None:
        action = cmd.get("action", "")
        if action == "interrupt":
            cl = self._flow._checklist if self._flow is not None else None
            if cl is not None:
                try:
                    if cl.get_current_item() is not None:
                        # A task is in flight: clear the pending tail first,
                        # then abort the in-flight item (order per
                        # SharedCheckList.replace_post_current). The agent acks
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

    async def _drain_messages(self) -> None:
        for msg_file in sorted(MESSAGES_DIR.glob("*.txt")):
            stem = msg_file.stem
            text = _read_text(msg_file)
            _archive(msg_file)
            if not text.strip():
                continue
            # Key the reply sink to this message BEFORE awaiting, so a reply
            # emitted during on_user_message (chat) or later by the background
            # task (completion summary) lands in the right reply file.
            self._active_msgid = stem
            if self._mirror is not None:
                self._mirror.set_task_status("running")
                self._mirror.snapshot()
            try:
                reply = await self._flow.on_user_message(text)
            except Exception as exc:
                reply = f"[handq_linux] error: {exc}"
            # Write the return value only if the sink hasn't already delivered
            # the authoritative reply for this id. Chat → the return IS the
            # reply. Task → the return is the plan ack; it serves as an
            # immediate placeholder, and the sink overwrites it with the full
            # completion summary once the task settles (which, for a very fast
            # task, may have already happened — hence the guard).
            if stem not in self._final_reply_msgids:
                _atomic_write_text(REPLY_DIR / f"{stem}.txt", reply or "")
            if self._mirror is not None:
                # Settle to idle when nothing is in flight. A freshly-submitted
                # task leaves its first item current here (the planner applies
                # items before on_user_message returns), so this never settles a
                # running task — show_state_changed drives that to idle at
                # completion. But the pure-chat path runs no agent/planner and so
                # never emits an "idle" state change; without this the optimistic
                # "running" set above would strand and get_status would report a
                # finished chat reply as a perpetually running task.
                cl = self._flow._checklist if self._flow is not None else None
                if cl is not None and cl.get_current_item() is None:
                    self._mirror.set_task_status("idle")
                self._mirror.snapshot()


def _new_session_id() -> str:
    return datetime.now().strftime("session_%Y%m%d_%H%M%S")


def _archive(path: Path) -> None:
    """Move a consumed message/command file into the .processed archive."""
    try:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        os.replace(path, PROCESSED_DIR / path.name)
    except Exception:
        _unlink(path)


def _build_llm_services(config_path: Optional[str]) -> Tuple[List[Any], List[Any]]:
    """Build (consolidated, helper) LLM service pools from config.

    Mirrors stdio_bridge's construction: one AnthropicStreamingService per
    model in priority order (the fallback chain), plus a distinct helper pool
    (llm.helper_models, falling back to the main models) for the Tier-1
    progress watcher.
    """
    from src.infrastructure.anthropic_streaming_service import AnthropicStreamingService
    from src.infrastructure.config_manager import ConfigManager
    from src.infrastructure.role_resolver import resolve_models_and_helper

    cm = ConfigManager(config_path)
    llm_cfg = cm.get_section("llm") or {}
    api_key = llm_cfg.get("API_KEY") or ""

    raw_mt = llm_cfg.get("max_tokens")
    try:
        mt_int = int(raw_mt) if raw_mt is not None else 0
    except (TypeError, ValueError):
        mt_int = 0
    max_tokens = mt_int if mt_int > 0 else None
    mt_kwargs: Dict[str, Any] = {"max_tokens": max_tokens} if max_tokens is not None else {}

    models, helper_models = resolve_models_and_helper(llm_cfg)
    if not models:
        models = ["anthropic::claude-4-5-haiku"]

    consolidated = [
        AnthropicStreamingService(api_key=api_key, model=m, max_retries=10, **mt_kwargs)
        for m in models
    ]
    helper = [
        AnthropicStreamingService(api_key=api_key, model=m, max_retries=10, **mt_kwargs)
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
            (HANDQ_DIR / "daemon_error.txt").write_text(
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
    elif kind == "text":
        resp["value"] = _prompt(f"[confirm] {req.get('prompt', 'input')}: ")
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
    print("HandQ (Linux) — type a goal, or 'exit' to leave the console "
          "(daemon keeps running).")
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
        msgid = _submit_message(line)
        reply = _wait_reply(msgid)
        print(reply if reply is not None else
              "(no reply yet — task may still be running; type 'status')")


def cmd_new(config_path: Optional[str]) -> int:
    if _daemon_alive():
        _send_command("new_session")
        print("· new session requested.")
    else:
        if not _ensure_daemon(config_path):
            print("handq_linux: failed to start daemon (see daemon.log).", file=sys.stderr)
            return 1
        print("· daemon started with a fresh session.")
    return 0


def cmd_status() -> int:
    if not _daemon_alive():
        print("handq_linux: daemon not running.")
        return 1
    state = _read_json(STATE_FILE) or {}
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
