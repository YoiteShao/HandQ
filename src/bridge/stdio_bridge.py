"""
stdio_bridge — line-delimited JSON IPC bridge between an Electron renderer
and the HandQ Python backend.

Wire format: one UTF-8 JSON object per line on stdin (renderer -> backend)
and stdout (backend -> renderer). After every emitted line the bridge calls
``sys.stdout.flush()``. ``stderr`` is reserved for backend logging.

Inbound message types : request, user_input, config_get, config_set, shutdown
Outbound message types: token_stream, status, final, error
"""
from __future__ import annotations

import asyncio
import uuid
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Inherits handlers configured by bridge_main.py. Must NOT call basicConfig.
logger = logging.getLogger("handq.bridge")
_ui_logger = logging.getLogger("handq.bridge.ui")


def _truncate(s: Any, n: int = 200) -> str:
    """Stringify *s* and clip to *n* chars with an ellipsis suffix."""
    try:
        text = s if isinstance(s, str) else repr(s)
    except Exception:
        text = "<unrepr>"
    if len(text) > n:
        return text[:n] + f"...<+{len(text) - n}>"
    return text


def _redact_payload(obj: Any, n: int = 200) -> str:
    """Render a JSON-ish object for logging with key fields redacted.

    Walks dicts and lists recursively so that nested secrets (e.g.
    config.llm.API_KEY) are scrubbed at any depth, not just the top level.
    """
    _SECRET_KEYS = ("API_KEY", "api_key")

    def _scrub(x: Any) -> Any:
        if isinstance(x, dict):
            return {
                k: ("***REDACTED***" if isinstance(k, str) and k in _SECRET_KEYS
                    else _scrub(v))
                for k, v in x.items()
            }
        if isinstance(x, list):
            return [_scrub(v) for v in x]
        if isinstance(x, tuple):
            return [_scrub(v) for v in x]
        return x

    try:
        cleaned = _scrub(obj)
        if isinstance(cleaned, (dict, list)):
            text = json.dumps(cleaned, ensure_ascii=False, default=str)
        else:
            text = repr(cleaned)
    except Exception:
        text = "<unserialisable>"
    return _truncate(text, n)

# Public symbols imported from the controller stack. FlowControllerV2
# is built lazily on the first 'request' so config-only round-trips don't
# need an API key. UserConfirmation is the value type returned by our async
# _StdioUI confirmation handlers; the InteractionManager is imported for
# typing only — the bridge never constructs one (FlowControllerV2 owns its
# IM and exposes it as ``flow.interaction_manager``).
from src.controller_v2.flow_controller import FlowControllerV2
from src.controller_v2.interaction_manager import InteractionManager  # noqa: F401  (type only)
from src.controller_v2.user_confirmation import UserConfirmation
from src.infrastructure.anthropic_streaming_service import (  # noqa: F401
    AnthropicStreamingService,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
    StreamDoneEvent,
)
from src.infrastructure.config_manager import ConfigManager
from src.infrastructure.llm_service import LLMService


# ---------------------------------------------------------------------------
# Session-scoped LLM service wrapper.
# Delegates streaming to a shared (bridge-global) service but keeps its own
# _exhausted flag and on_server_error callback, so per-session exhaustion
# tracking and error UI routing remain isolated.
# ---------------------------------------------------------------------------


class _SessionLLMService(LLMService):
    """Thin per-session wrapper around a shared LLMService instance."""

    def __init__(self, shared: LLMService) -> None:
        super().__init__(
            model=shared.model,
            max_tokens=shared.max_tokens,
            temperature=shared.temperature,
            max_retries=shared.max_retries,
            context_window=shared.context_window,
        )
        self._shared = shared

    @property
    def _base_url(self) -> str:  # type: ignore[override]
        return getattr(self._shared, "_base_url", "")

    async def chat_stream(  # type: ignore[override]
        self,
        messages,
        model=None,
        temperature=None,
        max_tokens=None,
        stop=None,
        json_mode=True,
        first_content=True,
        **kwargs,
    ):
        async for event in self._shared.chat_stream(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            json_mode=json_mode,
            first_content=first_content,
            **kwargs,
        ):
            yield event

    async def close(self) -> None:
        pass


DEFAULT_CONFIG_PATH = "./handq_config.yaml"


# ---------------------------------------------------------------------------
# Cross-module slots populated by bridge_main.py.
#
# ``personality_monitor`` and ``scheduler`` are constructed BEFORE the
# StdioBridge runs (bridge_main wires them up) and assigned here so the
# IPC handlers can reach them without StdioBridge holding direct refs.
# ``_active_bridge`` is set by StdioBridge.__init__ so the scheduler's
# dispatch closure can reach the live bridge instance.
#
# Keeping these at module level (instead of stuffing into StdioBridge)
# means bridge_main can wire them BEFORE the bridge instance is born and
# avoids a chicken-and-egg with the scheduler dispatch callback.
# ---------------------------------------------------------------------------

personality_monitor: Optional[Any] = None      # type: ignore[assignment]
scheduler: Optional[Any] = None             # type: ignore[assignment]
_active_bridge: Optional["StdioBridge"] = None


async def dispatch_scheduled_task(task) -> bool:  # type: ignore[no-untyped-def]
    """Bridge-side entry point for the scheduler.

    The scheduler holds a reference to this function; when a task fires,
    it calls in here. We:

    1. Refuse if the bridge isn't ready yet (boot still in progress).
    2. Refuse if shutdown is in progress (``_shutdown_requested``).
    3. Otherwise mint a fresh ``sched-{uuid}`` session, build a
       FlowControllerV2 for it, run the goal through ``on_user_message``,
       and block until the coordinator's reply returns. The session
       remains mounted in the UI after completion so the user can see the
       execution record.

    Returns True iff the bridge accepted the task. The scheduler
    interprets False as "bumped, try again later".
    """
    if _active_bridge is None:
        logger.info("scheduler dispatch refused: bridge not ready")
        return False
    return await _active_bridge.accept_scheduled_task(task)


def _entry_to_dict(entry) -> Dict[str, Any]:  # type: ignore[no-untyped-def]
    """Render an LTM ``Entry`` as a JSON-friendly dict for IPC.

    The renderer only needs enough to render a list view + open a
    detail pane — id (for archive), summary, content (full chunk text),
    facet (dim/category), source, dates, and version. We exclude raw
    Chunk objects since they're a per-storage detail and the joined
    content text is sufficient for display.
    """
    return {
        "id": entry.id,
        "kind": entry.kind.value if entry.kind else "",
        "summary": entry.summary,
        "content": entry.content,
        "dimension": entry.dimension.value if entry.dimension else None,
        "category": entry.category.value if entry.category else None,
        "source": entry.source,
        "source_ref": entry.source_ref,
        "version": entry.version,
        "archived": entry.archived,
        "archived_reason": entry.archived_reason,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "score": entry.score,
    }


# ── Git post-commit hook installer helpers ─────────────────────────────────
#
# Both helpers are sync (filesystem only, no asyncio) because the bridge
# dispatches them via asyncio.to_thread. They're module-level so unit
# tests / tooling can call them without spinning up a StdioBridge.
#
# Hook identity marker — used by uninstall to make sure we only delete
# files we wrote, not user-customised post-commit hooks. The marker is
# embedded in the hook script's docstring (HandQ git post-commit hook).

_HOOK_MARKER = "HandQ git post-commit hook"


def _hook_source_path() -> Path:
    """Locate ``scripts/handq_post_commit.py`` shipped with this build.

    Resolution order (mirrors bridge_main.py's INSTALL_DIR logic):
      1. Frozen builds: alongside ``sys.executable``
      2. Dev mode: ``<repo>/scripts/handq_post_commit.py``
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        install_dir = Path(os.path.dirname(os.path.abspath(sys.executable)))
        # Frozen builds may ship the script in either ``scripts/`` or
        # the install root, depending on packaging — try both.
        for candidate in (
            install_dir / "scripts" / "handq_post_commit.py",
            install_dir / "handq_post_commit.py",
        ):
            if candidate.exists():
                return candidate
    # Dev / fallback: from this file walk up to the repo root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scripts" / "handq_post_commit.py"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("handq_post_commit.py not found in install / repo")


def _install_post_commit_hook(repo_path: str) -> Dict[str, Any]:
    """Copy the bundled hook script into ``<repo>/.git/hooks/post-commit``.

    Returns ``{ok, path?, error?}``. We REFUSE to overwrite a hook the
    user wrote themselves — if a post-commit already exists and doesn't
    contain our marker, we bail with an error and let the user resolve
    the conflict manually.
    """
    repo = Path(repo_path).expanduser().resolve()
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return {"ok": False, "error": f"not a git repo: {repo}"}
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "post-commit"

    # Conflict detection: if the file exists and doesn't carry our
    # marker, the user has their own hook there. Don't clobber it.
    if target.exists():
        try:
            head = target.read_text(encoding="utf-8", errors="replace")[:2000]
        except Exception:
            head = ""
        if _HOOK_MARKER not in head:
            return {
                "ok": False,
                "error": (
                    f"existing post-commit hook at {target} is not ours; "
                    "remove or rename it before installing"
                ),
            }

    src = _hook_source_path()
    # The hook needs a shebang to be runnable on POSIX; on Windows git
    # invokes Python via the file extension association or via the
    # shebang+sh shim (Git for Windows). Either way the file content
    # is a Python script.
    script_body = src.read_text(encoding="utf-8")
    if not script_body.lstrip().startswith("#!"):
        import shutil
        python_exe = (
            "python3" if shutil.which("python3") else
            "python"  if shutil.which("python")  else
            None
        )
        if python_exe is None:
            return {
                "ok": False,
                "error": (
                    "no python or python3 found in PATH; "
                    "install Python and ensure it is on PATH before enabling this feature"
                ),
            }
        script_body = f"#!/usr/bin/env {python_exe}\n" + script_body
    target.write_text(script_body, encoding="utf-8")
    # chmod +x — git refuses to run hooks without the bit on POSIX.
    if sys.platform != "win32":
        try:
            mode = target.stat().st_mode
            target.chmod(mode | 0o755)
        except OSError:
            logger.warning("could not chmod %s", target, exc_info=True)
    logger.info("installed post-commit hook at %s", target)
    return {"ok": True, "path": str(target)}


def _uninstall_post_commit_hook(repo_path: str) -> Dict[str, Any]:
    """Delete the hook IF and only if it carries our marker.

    Refuses to delete an unrelated post-commit hook (the user's own).
    """
    repo = Path(repo_path).expanduser().resolve()
    target = repo / ".git" / "hooks" / "post-commit"
    if not target.exists():
        return {"ok": True, "removed": False, "note": "no hook to remove"}
    try:
        head = target.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        head = ""
    if _HOOK_MARKER not in head:
        return {
            "ok": False,
            "error": "post-commit hook is not ours; refusing to delete",
        }
    try:
        target.unlink()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "removed": True}


# ---------------------------------------------------------------------------
# Per-user roots
# ---------------------------------------------------------------------------
#
# The bridge owns two on-disk roots, both derived from the OS — never from
# yaml, never from a renderer-supplied value:
#
#   %USERPROFILE%\HandQ\               <- _user_handq_root()
#       handq_config.yaml              user config (read by bridge_main)
#       History\                       <- _session_history_root()
#           <YYYYMMDD-HHMMSS>-<slug>\  one directory per `request`
#               session_state.json
#               executions_logs\
#               ... (FlowController writes here)
#
# Rationale:
#   * Sessions can grow large (artifacts + execution logs) so we don't put
#     them in Documents (would pollute OneDrive sync, user backups).
#   * Config and History live together so the user has one place to find
#     "everything HandQ owns about me".
#   * Logs (frontend.log + bridge.log) live separately under
#     %LOCALAPPDATA%\HandQ\logs\ — they're machine-local debug artifacts,
#     not user-owned data.
# ---------------------------------------------------------------------------

def _user_handq_root() -> Path:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _session_history_root() -> Path:
    return _user_handq_root() / "History"


_SLUG_SAFE_RE = None  # lazy compile


def _slugify_goal(goal: str, max_len: int = 40) -> str:
    """Turn the user's goal into a filesystem-safe directory suffix."""
    import re
    global _SLUG_SAFE_RE
    if _SLUG_SAFE_RE is None:
        _SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9一-鿿]+")
    if not goal:
        return "untitled"
    cleaned = _SLUG_SAFE_RE.sub("-", goal.strip()).strip("-")
    if not cleaned:
        return "untitled"
    return cleaned[:max_len]


def _allocate_session_dir(goal: str, workspace_subdir: str = ".workspace") -> Path:
    """Create %USERPROFILE%\\HandQ\\History\\<TS>-<slug>\\ and pre-create the
    inner agent workspace subdir. Returns the session root.

    The agent's prompt only ever sees ``<session>/<workspace_subdir>/``. The
    session root itself holds framework metadata (handq-engine.log,
    executions_logs/) and is invisible to the LLM — keeping the workspace as a
    dedicated child means even an absolute-path slip-up by the agent lands
    inside the session subtree, not the user's filesystem.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = _slugify_goal(goal)
    base = _session_history_root()
    candidate = base / f"{ts}-{slug}"
    # Collision guard for sub-second double-fires.
    n = 1
    while candidate.exists():
        n += 1
        candidate = base / f"{ts}-{slug}-{n}"
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / workspace_subdir).mkdir(exist_ok=True)
    return candidate


# ---------------------------------------------------------------------------
# stdout writer
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def _open_ipc_stdout():
    """Return a UTF-8 text stream pointing at the real IPC stdout.

    ``bridge_main.py`` dup's the original fd 1 to a private fd and exports it
    in ``HANDQ_BRIDGE_STDOUT_FD`` so the engine's logger (which writes to
    ``sys.stdout``) cannot pollute the JSON channel. If that env var is not
    set (e.g. the module is imported directly) we fall back to ``sys.stdout``.
    """
    fd_str = os.environ.get("HANDQ_BRIDGE_STDOUT_FD")
    if fd_str:
        try:
            fd = int(fd_str)
            stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1, closefd=False)
            logger.info("IPC stdout: using private fd %s", fd_str)
            return stream
        except Exception:
            logger.exception("IPC stdout: failed to open private fd %s; falling back", fd_str)
    logger.warning("IPC stdout: HANDQ_BRIDGE_STDOUT_FD missing, falling back to sys.stdout")
    return sys.stdout


_ipc_out = _open_ipc_stdout()


def _emit(
    obj: Dict[str, Any],
    session_id: Optional[str] = None,
) -> None:
    """Serialise *obj* as one JSON line on the IPC stdout and flush.

    If *session_id* is supplied (multi-session routing tag), it is stamped
    onto the envelope. Bridge-global events (config, ltm, cron, personality)
    omit it because they are not session-scoped. The renderer routes
    session-stamped events to the matching tab; envelopes without a
    session_id are treated as broadcast.
    """
    if session_id is not None:
        obj["session_id"] = session_id
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with _write_lock:
        _ipc_out.write(line + "\n")
        _ipc_out.flush()
    try:
        logger.debug(
            "outbound envelope type=%s id=%s session_id=%s",
            obj.get("type"), obj.get("id"), obj.get("session_id"),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# UI adapter — re-emits engine events as token_stream / status / final / error
# ---------------------------------------------------------------------------


class _StdioUI:
    """V2 ``UIDelegate`` implementation for the Electron renderer.

    Each method serialises a JSON envelope onto the IPC stdout.  The async
    ``request_*`` methods register an ``asyncio.Future`` keyed by prompt id;
    the stdin-reader thread resolves the matching future via
    :meth:`deliver_confirmation_response` when the user answers the modal.
    """

    def __init__(self, session_id: str = "default") -> None:
        self._session_id = session_id
        # Bridge-owned confirmation registry. The IM is a clean async
        # forwarder with no internal state to hook, so the bridge keeps
        # its own pending-future map. Per-session in the multi-session
        # world — each session's _StdioUI has its own map keyed by
        # prompt_id (prompt_id strings already carry timestamp+addr-hash
        # entropy so cross-session collisions are vanishingly rare).
        self._pending: Dict[str, "asyncio.Future[str]"] = {}
        self._pending_lock = threading.Lock()
        # Loop captured by ``StdioBridge`` after construction so confirmation
        # responses (delivered from the stdin-reader thread) can resolve
        # futures via ``call_soon_threadsafe``. ``None`` until bound.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def deliver_confirmation_response(self, prompt_id: str, answer: str) -> None:
        """Resolve a pending confirmation future. Called from the stdin
        reader thread when ``user_input.kind=confirmation`` arrives.
        Uses ``call_soon_threadsafe`` so the future's result is set on the
        asyncio loop owned by the bridge.
        """
        with self._pending_lock:
            fut = self._pending.pop(prompt_id, None)
        if fut is None:
            _ui_logger.warning(
                "deliver_confirmation_response: no pending future for id=%s",
                prompt_id,
            )
            return
        if self._loop is None or fut.done():
            return
        try:
            self._loop.call_soon_threadsafe(fut.set_result, answer)
        except RuntimeError:
            # Loop closed during shutdown — best-effort; future is dropped.
            pass

    # ── V2 UIDelegate Protocol — fire-and-forget ──────────────────────────

    def display_error(self, msg: str) -> None:
        _ui_logger.error("display_error: %s", _truncate(msg))
        _emit({"type": "error", "where": "engine", "message": str(msg),
               "fatal": False}, session_id=self._session_id)

    def show_state_changed(self, state: Any) -> None:
        _ui_logger.debug("show_state_changed: %s", _truncate(state))
        _emit({"type": "status", "kind": "state_changed", "state": str(state)},
              session_id=self._session_id)

    def show_inline_event(self, icon: str, desc: str) -> None:
        """Single-line status banner (icon + text). Renderer maps
        ``kind=inline_event`` to addStepBubble.
        """
        _emit({"type": "status", "kind": "inline_event",
               "icon": str(icon or "·"),
               "desc": str(desc or "")},
              session_id=self._session_id)

    def show_recall_started(self) -> None:
        """LTM recall in flight. Renderer maps ``kind=recall_started`` to a
        transient 'recalling…' label on the activity strip, superseded by the
        next state / decision / tool event.
        """
        _emit({"type": "status", "kind": "recall_started"},
              session_id=self._session_id)

    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int = 0,
    ) -> None:
        _ui_logger.debug("notify_decision_made: iter=%s tokens=%s",
                         iteration, token_count)
        # Renderer reads args[0]=iter, args[1]=reasoning (renderer.js:2004-2007).
        _emit({"type": "status", "kind": "decision_made",
               "args": [str(iteration), str(reasoning), str(token_count)],
               "kwargs": {}},
              session_id=self._session_id)

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        output: Any,
    ) -> None:
        _ui_logger.debug("notify_tool_execution_started: iter=%s tool=%s",
                         iteration, tool_name)
        # Renderer reads args[0..3] = iter, tool, params, output
        # (renderer.js:2009-2045). Pre-event has output=None; post-event
        # has output set. Both are emitted by V2 PersistentAgent.
        _emit({"type": "status", "kind": "tool_execution_started",
               "args": [str(iteration),
                        str(tool_name) if tool_name else "",
                        params,
                        output],
               "kwargs": {}},
              session_id=self._session_id)

    # ── Non-Protocol forwarders (called by IM via ``_ui_call``) ──────────
    # The V2 ``UIDelegate`` Protocol is intentionally minimal. These methods
    # receive events the Protocol doesn't list — the IM's ``_ui_call`` resolves
    # them by string name and silently skips when missing. Tools / coordinator
    # streaming hook here; renderer-side handlers stay unchanged.

    def notify_desktop_takeover_started(self, reason: str = "input_action") -> None:
        """Desktop tool entered an input-driving phase. Emit the
        ``desktop_takeover_started`` envelope so the Electron overlay shows
        the fullscreen border + Ctrl+Shift+C revoke hook.

        Pause coordination: the PersonalityMonitor pauses automatically by
        querying ``desktop_tool.is_any_session_holding_desktop`` — no
        explicit ``pause()`` call is needed here. That keeps the monitor's
        state strictly derived from the authoritative global owner, with no
        refcount and no drift even when ``_force_release_session_locks``
        force-resets the owner after a destroy timeout.
        """
        _ui_logger.debug("notify_desktop_takeover_started: reason=%s", reason)
        _emit({"type": "status", "kind": "desktop_takeover_started",
               "reason": str(reason)}, session_id=self._session_id)

    def notify_desktop_takeover_ended(self, reason: str = "task_ended") -> None:
        """Desktop tool finished its input-driving phase. Emits the
        ``desktop_takeover_ended`` envelope; personality un-pause is
        automatic via the desktop_query callable (see
        ``notify_desktop_takeover_started`` for rationale)."""
        _ui_logger.debug("notify_desktop_takeover_ended: reason=%s", reason)
        _emit({"type": "status", "kind": "desktop_takeover_ended",
               "reason": str(reason)}, session_id=self._session_id)

    def notify_session_event(self, event_name: str, data: Any = None) -> None:
        """Live shell session lifecycle (open / exec_done / close). Renderer
        renders a session monitor panel from these events. Currently no V2
        caller — ``session_tool`` is neutered until its V2 rewire — but kept
        here so the rewire is just adding the call back."""
        _ui_logger.debug("notify_session_event: %s", event_name)
        _emit({"type": "status", "kind": "session_event",
               "event": str(event_name),
               "data": data if isinstance(data, dict) else {}},
              session_id=self._session_id)

    def notify_task_plan_changed(self, items: Any = None) -> None:
        """Live task-plan snapshot → renderer ``kind=task_plan`` panel.
        ``items`` is the list of ``{item_id, instruction, status}`` dicts from
        ``TaskChannel.get_ui_snapshot``; an empty list tells the renderer to
        drop the panel."""
        _ui_logger.debug("notify_task_plan_changed: %d item(s)",
                         len(items) if isinstance(items, list) else 0)
        _emit({"type": "status", "kind": "task_plan",
               "items": items if isinstance(items, list) else []},
              session_id=self._session_id)

    def notify_agent_todo_changed(self, todos: Any = None) -> None:
        """Live snapshot of the agent's own todo → renderer ``kind=agent_todo``
        panel. ``todos`` is the list of ``{content, status}`` dicts the agent
        writes via the `todo_write` tool; an empty list tells the renderer to
        drop the panel. Distinct from ``notify_task_plan_changed`` (that one
        shows the Coordinator↔Agent IPC queue, which is at most one item —
        this is the agent's own multi-step breakdown of that item)."""
        _ui_logger.debug("notify_agent_todo_changed: %d item(s)",
                         len(todos) if isinstance(todos, list) else 0)
        _emit({"type": "status", "kind": "agent_todo",
               "todos": todos if isinstance(todos, list) else []},
              session_id=self._session_id)

    def show_coordinator_thinking(self) -> None:
        _emit({"type": "status", "kind": "coordinator_thinking_on"},
              session_id=self._session_id)

    def clear_coordinator_thinking(self) -> None:
        _emit({"type": "status", "kind": "coordinator_thinking_off"},
              session_id=self._session_id)

    def stream_coordinator_reply_chunk(self, text: str) -> None:
        _emit({"type": "status", "kind": "reply_delta", "text": str(text)},
              session_id=self._session_id)

    def seal_coordinator_reply(self) -> None:
        _emit({"type": "status", "kind": "reply_done"},
              session_id=self._session_id)

    # ── V2 UIDelegate Protocol — async confirmations ──────────────────────

    @staticmethod
    def _resolve_confirmation(answer: str) -> UserConfirmation:
        """Map a raw renderer answer string to a ``UserConfirmation``.

        Conventions:
          - ``"yes" / "y" / "ok" / "approve"`` → :py:meth:`UserConfirmation.yes`
          - ``"no" / "n" / "reject" / "deny"`` → :py:meth:`UserConfirmation.no`
          - empty / ``None``                   → ``no()``
          - ``"risk:<text>"``                  → :py:meth:`risk_guidance`
          - any other non-empty text           → :py:meth:`with_message`
        """
        a = (answer or "").strip()
        low = a.lower()
        if not a:
            return UserConfirmation.no()
        if low in ("yes", "y", "ok", "approve", "approved"):
            return UserConfirmation.yes()
        if low in ("no", "n", "reject", "rejected", "deny"):
            return UserConfirmation.no()
        if low.startswith("risk:"):
            return UserConfirmation.risk_guidance(a[len("risk:"):].strip())
        return UserConfirmation.with_message(a)

    async def _await_user_response(
        self, kind: str, payload: Dict[str, Any], prompt_id: str,
    ) -> str:
        """Emit a confirmation envelope; await the renderer's response.

        Registers a fresh ``asyncio.Future`` under ``prompt_id``; the stdin
        reader thread resolves it via :meth:`deliver_confirmation_response`
        when the user answers in the renderer modal.
        """
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        fut: "asyncio.Future[str]" = loop.create_future()
        with self._pending_lock:
            self._pending[prompt_id] = fut
        env: Dict[str, Any] = {"type": "status", "kind": kind, "id": prompt_id}
        env.update(payload)
        _emit(env, session_id=self._session_id)
        _ui_logger.debug("await_user_response: kind=%s id=%s", kind, prompt_id)
        try:
            return await fut
        except asyncio.CancelledError:
            with self._pending_lock:
                self._pending.pop(prompt_id, None)
            raise

    async def request_risk_confirmation(
        self,
        description: str,
        *,
        title: Optional[str] = None,
        approve_label: Optional[str] = None,
    ) -> UserConfirmation:
        """High-risk operation gate. Emits ``kind=risk_confirmation``;
        awaits the renderer's yes/no/text answer.

        ``title`` and ``approve_label`` are optional renderer-side overrides
        for the modal chrome. When provided they land in the envelope as
        ``title`` / ``approve_label`` fields — the renderer honors them if
        present and otherwise falls back to its baked-in defaults. Used by
        ``browser.request_user_login`` to reframe the modal as a
        "confirm-login-completed" gate rather than a generic "approve".
        """
        prompt_id = f"risk-{int(time.time() * 1000)}-{id(description) & 0xffff:04x}"
        payload: Dict[str, Any] = {"description": str(description)}
        if title:
            payload["title"] = str(title)
        if approve_label:
            payload["approve_label"] = str(approve_label)
        answer = await self._await_user_response("risk_confirmation", payload, prompt_id)
        return self._resolve_confirmation(answer)

    async def request_tool_confirmation(
        self,
        tool_name: str,
        params: Dict[str, Any],
        hint: str,
    ) -> UserConfirmation:
        """Tool-specific gate. ``desktop`` is task-scoped (single approval
        covers every desktop action until task end); other tools use
        per-call scope and surface the ``params`` dict so the modal can
        show what's about to run."""
        prompt_id = f"tool-{int(time.time() * 1000)}-{id(params) & 0xffff:04x}"
        payload: Dict[str, Any] = {
            "tool": str(tool_name),
            "hint": str(hint),
        }
        if str(tool_name).startswith("desktop_") or str(tool_name) == "desktop":
            payload["scope"] = "task"
            payload["description"] = (
                "The agent is requesting control of your desktop "
                "(mouse / keyboard / screen capture). Approving grants "
                "full desktop access for the remainder of this task — "
                "every subsequent desktop action will run without "
                "asking again. Press Ctrl+Shift+C anytime to revoke."
            )
        else:
            def _trunc(v: Any, n: int = 200) -> str:
                s = str(v)
                return s if len(s) <= n else s[:n] + "…"
            payload["params"] = {
                k: _trunc(v) for k, v in (params or {}).items()
            }
        answer = await self._await_user_response("tool_confirmation", payload, prompt_id)
        return self._resolve_confirmation(answer)

    async def request_secret_input(self, prompt: str) -> str:
        """Hidden text input (passwords, SSH credentials)."""
        prompt_id = f"secret-{int(time.time() * 1000)}"
        payload = {"prompt": str(prompt)}
        return await self._await_user_response("secret_input", payload, prompt_id)

    async def request_user_text(self, prompt: str) -> str:
        """Free-form (non-secret) clarifying question from the agent
        (``ask_human`` tool). Emits ``kind=ask_human`` — the renderer shows
        an UNMASKED text field (distinct from the masked ``secret_input``) —
        and awaits the user's typed answer."""
        prompt_id = f"ask-{int(time.time() * 1000)}"
        payload = {"prompt": str(prompt)}
        return await self._await_user_response("ask_human", payload, prompt_id)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class StdioBridge:
    """Single entry-point class for the stdio JSON bridge.

    Multi-session model: a single bridge process can host many concurrent
    ``FlowControllerV2`` instances keyed by ``session_id``. The renderer
    mints a UUID per tab and stamps it on every outbound envelope
    (request / user_input / close_session). Each session has
    its own ``_StdioUI`` delegate, its own engine.log handler, and its own
    LLM service pool.

    Bridge-meta envelopes (config_*, ltm_*, cron_*, personality_*, shutdown,
    scheduler housekeeping) are NOT session-scoped — they emit without a
    session_id and the renderer treats them as broadcast.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self.config_path: Path = Path(config_path)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._inbox: Optional[asyncio.Queue] = None

        # Per-session backend state. Each session_id has its own
        # FlowControllerV2 + LLM service pool + engine.log handler +
        # UI delegate. Built lazily on the first
        # ``request`` for that session_id.
        self._flows: Dict[str, FlowControllerV2] = {}
        self._services_by_session: Dict[str, List[LLMService]] = {}
        # Shared (bridge-global) LLM connection pools. Lazily constructed on
        # first session and reused across all sessions. Each session gets
        # lightweight _SessionLLMService wrappers with session-scoped
        # _exhausted tracking. Cleared on config change (API key / model edit).
        self._shared_services: Optional[List[LLMService]] = None
        self._shared_helper_services: Optional[List[LLMService]] = None
        # Session-scoped handq-engine.log handlers (one per session). Attached
        # by _ensure_flow on session creation and detached by _do_close_session
        # on teardown so each session gets an isolated engine log with no
        # cross-session bleed and no lingering Windows file lock.
        self._engine_log_handlers: Dict[str, Optional[logging.Handler]] = {}
        # Per-session UI delegate (binds to that session's IM).
        self._uis: Dict[str, _StdioUI] = {}

        # Bridge-meta envelopes (config_*, ltm_*, cron_*, personality_*,
        # boot, shutdown, scheduler housekeeping) emit through _emit()
        # WITHOUT a session stamp. The renderer treats unstamped envelopes
        # as "always accept" and routes to the active tab.
        self._session_id: Optional[str] = None

        # Sessions currently being torn down. Used to reject race-y `request`
        # / `user_input` envelopes arriving for a sid whose flow.destroy is
        # mid-flight; without this we could re-trigger _ensure_flow on a sid
        # whose ctx.close hasn't returned, leaving two sets of resources
        # racing for the same per-session file/socket state.
        self._closing: set = set()

        # Per-session dispatch. The main loop runs each inbound message as its
        # own asyncio task so a slow LLM round-trip for one session can't block
        # another session's input. Same-session messages stay in arrival order
        # via a per-sid lock; different sessions run concurrently.
        self._session_dispatch_locks: Dict[str, asyncio.Lock] = {}
        # Every live _dispatch task — drained on shutdown.
        self._inflight_tasks: set = set()
        # The request/user_input handler currently holding a session's lock, so
        # close_session can preempt a slow round-trip (N3).
        self._inflight_by_sid: Dict[str, asyncio.Task] = {}
        # Guards self._uis against the stdin-reader thread iterating it while
        # the loop thread mutates it (RuntimeError: dict changed size).
        self._uis_lock = threading.Lock()

        self._shutdown_requested: bool = False

        logger.info("StdioBridge initialised; config_path=%s",
                    self.config_path)
        # Publish self for module-level helpers (dispatch_scheduled_task).
        # See module docstring on the cross-module slots.
        global _active_bridge
        _active_bridge = self

        # Register the llm_pool fallback + network notifiers ONCE at boot.
        # These signals are session-agnostic — an API outage or model
        # fallback affects every session identically — so the envelopes
        # carry no session_id; the renderer treats them as bridge-meta
        # broadcasts and renders them as system bubbles in the active tab.
        self._register_llm_pool_notifiers()

    @staticmethod
    def _register_llm_pool_notifiers() -> None:
        """One-shot registration of llm_pool's broadcast notifiers.

        Idempotent: re-registering overwrites the same closure (no-op).
        Kept as a separate method so unit tests can re-register after
        mocking. Closures don't capture instance state — they're pure
        broadcast wrappers around the module-level _emit.
        """
        from ..infrastructure.llm_pool import (
            set_fallback_notifier,
            set_network_event_notifier,
        )

        def _on_llm_fallback(from_model: str, to_model: str, exc: Exception) -> None:
            _emit({
                "type": "status",
                "kind": "llm_fallback",
                "from_model": from_model,
                "to_model": to_model,
                "error": str(exc)[:200],
            })

        def _on_network_event(state: str, attempt: int, sleep_secs: int) -> None:
            if state == "down":
                _emit({
                    "type": "status",
                    "kind": "network_down",
                    "message": "网络已中断，LLM 服务暂不可达，等待恢复中…",
                })
            elif state == "waiting":
                _emit({
                    "type": "status",
                    "kind": "network_waiting",
                    "attempt": attempt,
                    "retry_in": sleep_secs,
                })
            elif state == "restored":
                _emit({
                    "type": "status",
                    "kind": "network_restored",
                    "message": "网络已恢复，继续执行",
                })

        set_fallback_notifier(_on_llm_fallback)
        set_network_event_notifier(_on_network_event)

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _resolve_session_id(self, msg: Dict[str, Any]) -> Optional[str]:
        """Pull session_id out of an inbound envelope. Returns None when
        the renderer didn't supply one — callers MUST handle that (emit
        error for session-scoped types, pass-through for bridge-meta).

        Note: ``_resolve_session_id`` no longer falls back to a default
        sid. The renderer mints a UUID on init and stamps every
        session-scoped envelope; a missing sid here is a protocol bug or
        a hand-crafted bridge-meta envelope (which shouldn't call this).
        """
        sid = msg.get("session_id")
        if isinstance(sid, str):
            sid = sid.strip()
            if sid:
                return sid
        return None

    def _get_or_create_ui(self, sid: str) -> "_StdioUI":
        """Idempotent — returns the existing per-session UI delegate or
        constructs a fresh one. The new UI captures the event loop ref so
        confirmation futures can be resolved from the stdin reader thread."""
        ui = self._uis.get(sid)
        if ui is None:
            ui = _StdioUI(session_id=sid)
            if self._loop is not None:
                ui._loop = self._loop
            with self._uis_lock:
                self._uis[sid] = ui
        return ui

    def _deliver_confirmation(
        self, sid_hint: Any, prompt_id: str, answer: str,
    ) -> None:
        """Resolve a confirmation future on the owning session's ``_StdioUI``.

        Prefer the renderer-supplied sid; if it is missing or unknown, scan
        every UI (prompt_id strings carry timestamp+addr-hash entropy, so a
        cross-session collision is vanishingly rare). Every ``_uis`` access is
        held under ``_uis_lock`` and the scan iterates a snapshot, so a
        concurrent loop-thread mutation cannot trip ``RuntimeError: dictionary
        changed size during iteration`` when this runs on the stdin-reader
        thread (F2). Shared by the reader fast-path and the inbox dispatcher.
        """
        ui = None
        if isinstance(sid_hint, str) and sid_hint.strip():
            with self._uis_lock:
                ui = self._uis.get(sid_hint.strip())
        if ui is not None:
            ui.deliver_confirmation_response(prompt_id, answer)
            return
        with self._uis_lock:
            uis = list(self._uis.values())
        for other_ui in uis:
            other_ui.deliver_confirmation_response(prompt_id, answer)

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _stdin_reader(self) -> None:
        """Read JSON lines from stdin and push them onto the asyncio queue."""
        # Use the private IPC stdin fd reserved by bridge_main.py if available;
        # fall back to sys.stdin otherwise (e.g. when run as a module directly).
        fd_str = os.environ.get("HANDQ_BRIDGE_STDIN_FD")
        using_private = False
        if fd_str:
            try:
                fd = int(fd_str)
                stream = os.fdopen(fd, "r", encoding="utf-8", buffering=1, closefd=False)
                using_private = True
            except Exception:
                logger.exception("stdin reader: failed to open private fd %s; using sys.stdin", fd_str)
                stream = sys.stdin
        else:
            stream = sys.stdin

        logger.info("stdin reader started; using_private_fd=%s", using_private)

        try:
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # Do NOT echo the raw line — a malformed message may still
                    # contain an "api_key":"..." substring that would leak.
                    logger.error("malformed JSON on stdin: %s; raw_len=%d", exc, len(raw))
                    _emit({"type": "error", "where": "bridge",
                           "message": f"Malformed JSON on stdin: {exc}",
                           "fatal": False}, session_id=self._session_id)
                    continue
                logger.debug(
                    "inbound message type=%s id=%s payload=%s",
                    obj.get("type") if isinstance(obj, dict) else None,
                    obj.get("id") if isinstance(obj, dict) else None,
                    _redact_payload(obj),
                )
                # ── Fast path: confirmation responses bypass asyncio ─────────
                # ``_StdioUI.request_*`` await an asyncio.Future. If we routed
                # the answer through the asyncio inbox, the dispatcher would
                # have to run on the same loop that's blocked awaiting the
                # future. Resolving the future directly from this thread (via
                # ``call_soon_threadsafe`` inside ``deliver_confirmation_response``)
                # avoids that ordering hazard.
                #
                # Multi-session routing: prefer the session_id supplied by the
                # renderer; if missing or unknown, fall through to a scan
                # across all UIs (prompt_id strings already carry timestamp +
                # addr-hash entropy so cross-session collisions are vanishingly
                # rare).
                if (isinstance(obj, dict)
                        and obj.get("type") == "user_input"
                        and obj.get("kind") == "confirmation"):
                    try:
                        prompt_id = str(obj.get("id") or "")
                        answer = str(obj.get("answer", ""))
                        self._deliver_confirmation(
                            obj.get("session_id"), prompt_id, answer,
                        )
                    except Exception:
                        logger.exception(
                            "stdin reader: deliver_confirmation_response failed"
                        )
                    continue
                if self._loop is not None and self._inbox is not None:
                    self._loop.call_soon_threadsafe(self._inbox.put_nowait, obj)
        except Exception as exc:
            logger.exception("stdin reader crashed")
            _emit({"type": "error", "where": "bridge",
                   "message": f"stdin reader crashed: {exc}", "fatal": True},
                  session_id=self._session_id)
        finally:
            logger.info("stdin EOF; signalling dispatcher")
            # Sentinel to wake the dispatcher on EOF.
            if self._loop is not None and self._inbox is not None:
                self._loop.call_soon_threadsafe(self._inbox.put_nowait, None)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config_dict(self) -> Dict[str, Any]:
        """Read the YAML file directly. Returns ``{}`` if it does not exist."""
        if not self.config_path.exists():
            logger.warning("config file does not exist: %s", self.config_path)
            return {}
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("config file root is not a dict; treating as empty")
            return {}
        logger.debug("config loaded; %d top-level keys", len(data))
        return data

    def _save_config_dict(self, cfg: Dict[str, Any]) -> None:
        """Write *cfg* to the YAML file with explicit UTF-8 encoding."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        logger.info("config written to %s", self.config_path)

    @staticmethod
    def _normalised_hook_repos(cfg: Dict[str, Any]) -> set:
        """Pull ``personalization.git_hook_repos`` out of *cfg* as a set
        of stripped, non-empty strings. Returns an empty set on any
        shape error so the diff caller can treat malformed config as
        "no repos requested" instead of crashing the save."""
        pers = cfg.get("personalization") if isinstance(cfg, dict) else None
        if not isinstance(pers, dict):
            return set()
        raw = pers.get("git_hook_repos")
        if not isinstance(raw, list):
            return set()
        return {str(r).strip() for r in raw if str(r).strip()}

    async def _sync_git_hooks(
        self,
        *,
        added: set,
        removed: set,
    ) -> Dict[str, Any]:
        """Apply the diff of ``personalization.git_hook_repos`` to disk.

        Runs the (blocking) install / uninstall helpers on a worker
        thread so the IPC dispatcher stays responsive. Each repo is
        independent — one failure doesn't block the others. Returns
        a structured summary the renderer can surface to the user.
        """
        installed: List[Dict[str, Any]] = []
        uninstalled: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for repo in sorted(added):
            try:
                r = await asyncio.to_thread(_install_post_commit_hook, repo)
            except Exception as exc:
                logger.exception("install_post_commit_hook crashed: %s", repo)
                errors.append({"repo": repo, "op": "install", "error": str(exc)})
                continue
            if r.get("ok"):
                installed.append({"repo": repo, "path": r.get("path")})
                logger.info("git hook installed at %s", r.get("path"))
            else:
                errors.append({"repo": repo, "op": "install",
                               "error": r.get("error") or "unknown"})
                logger.warning("git hook install skipped for %s: %s",
                               repo, r.get("error"))

        for repo in sorted(removed):
            try:
                r = await asyncio.to_thread(_uninstall_post_commit_hook, repo)
            except Exception as exc:
                logger.exception("uninstall_post_commit_hook crashed: %s", repo)
                errors.append({"repo": repo, "op": "uninstall", "error": str(exc)})
                continue
            if r.get("ok"):
                uninstalled.append({"repo": repo, "removed": r.get("removed", False)})
                logger.info("git hook uninstall ok for %s (removed=%s)",
                            repo, r.get("removed"))
            else:
                errors.append({"repo": repo, "op": "uninstall",
                               "error": r.get("error") or "unknown"})
                logger.warning("git hook uninstall failed for %s: %s",
                               repo, r.get("error"))

        return {"installed": installed, "uninstalled": uninstalled, "errors": errors}

    # ------------------------------------------------------------------
    # Inbound dispatch
    # ------------------------------------------------------------------

    async def _handle(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type")
        msg_id = msg.get("id")
        logger.debug("dispatch: type=%s id=%s", msg_type, msg_id)

        if msg_type == "config_get":
            try:
                logger.info("config_get: path=%s", self.config_path.resolve())
                cfg = self._load_config_dict()
                _emit({
                    "type": "final",
                    "id": msg_id,
                    "result": {
                        "config_path": str(self.config_path.resolve()),
                        "config": cfg,
                    },
                }, session_id=self._session_id)
            except Exception as exc:
                logger.exception("config_get failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_get failed: {exc}", "fatal": False},
                      session_id=self._session_id)
            return

        if msg_type == "ltm_stats":
            # Read-only diagnostic: per-source candidate acceptance / rejection
            # counts, totals, and DreamWorker queue depth. Lets the renderer
            # (or a CLI debug command) show whether the triage bar is
            # well-calibrated without exposing the raw memory tables.
            try:
                from src.infrastructure.long_term_memory import LongTermMemory
                stats = await LongTermMemory.get().triage_stats()
                _emit({
                    "type": "final", "id": msg_id, "result": stats,
                }, session_id=self._session_id)
            except Exception as exc:
                logger.exception("ltm_stats failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"ltm_stats failed: {exc}", "fatal": False},
                      session_id=self._session_id)
            return

        # ── LTM review / archive (user trust surface) ───────────────────
        # ``ltm_stats`` answers "how is triage performing"; the trio below
        # answers "what is HandQ remembering about me, and can I drop a
        # specific entry". We deliberately do NOT expose inline editing —
        # entries' content is the triage prompt's contract; allowing the
        # user to edit prose directly would let secret strings or persona
        # instructions slip past every guard. To "fix" an entry the user
        # archives it; the next session will re-emit a candidate and
        # triage decides afresh.
        if msg_type in ("ltm_list_memory", "ltm_list_knowledge", "ltm_archive"):
            try:
                from src.infrastructure.long_term_memory import (
                    LongTermMemory, MemoryDimension, KnowledgeCategory,
                    EntryKind,
                )
                ltm = LongTermMemory.get()
                if msg_type == "ltm_list_memory":
                    dim_raw = msg.get("dimension")
                    dim = MemoryDimension(dim_raw) if dim_raw else None
                    limit = int(msg.get("limit") or 50)
                    entries = await ltm.list_active_memory(
                        dimension=dim, limit=limit,
                    )
                    result = {"entries": [_entry_to_dict(e) for e in entries]}
                elif msg_type == "ltm_list_knowledge":
                    cat_raw = msg.get("category")
                    cat = KnowledgeCategory(cat_raw) if cat_raw else None
                    limit = int(msg.get("limit") or 50)
                    entries = await ltm.list_active_knowledge(
                        category=cat, limit=limit,
                    )
                    result = {"entries": [_entry_to_dict(e) for e in entries]}
                else:  # ltm_archive
                    # ``entry_id`` (not ``id``): the envelope's ``id`` is the
                    # RPC correlation key, overwritten by the renderer's rpc()
                    # layer, so the row id must travel under a distinct key.
                    eid = str(msg.get("entry_id") or "")
                    kind_raw = str(msg.get("kind") or "")
                    if not eid or not kind_raw:
                        raise ValueError("ltm_archive: entry_id and kind required")
                    kind = EntryKind(kind_raw)
                    reason = str(msg.get("reason") or "user_request")
                    await ltm.archive(entry_id=eid, kind=kind, reason=reason)
                    result = {"ok": True}
                _emit({"type": "final", "id": msg_id, "result": result},
                      session_id=self._session_id)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, session_id=self._session_id)
            return

        # ── Skill control-panel IPC ─────────────────────────────────────
        # The panel is the single hub for the skill lifecycle. ``skill_list``
        # returns the full SkillRegistry inventory (incl. disabled skills).
        # enable/disable + CRUD + import go through SkillRegistry; the write
        # methods do blocking file I/O so they run in a worker thread. There is
        # no approval queue — auto-generated skills are direct-written disabled
        # by triage and surface here for review.
        if msg_type in (
            "skill_list", "skill_set_enabled", "skill_set_standing",
            "skill_create", "skill_update", "skill_delete", "skill_import",
        ):
            try:
                from src.infrastructure.skills import SkillRegistry
                reg = SkillRegistry.get()
                if msg_type == "skill_list":
                    skills = await asyncio.to_thread(reg.list_all)
                    result = {"skills": skills}
                elif msg_type == "skill_set_enabled":
                    name = str(msg.get("name") or "")
                    if not name:
                        raise ValueError("skill_set_enabled: name required")
                    result = await asyncio.to_thread(
                        reg.set_enabled, name, bool(msg.get("enabled")),
                    )
                elif msg_type == "skill_set_standing":
                    name = str(msg.get("name") or "")
                    if not name:
                        raise ValueError("skill_set_standing: name required")
                    result = await asyncio.to_thread(
                        reg.set_standing, name, bool(msg.get("standing")),
                    )
                elif msg_type == "skill_create":
                    result = await asyncio.to_thread(
                        reg.create_skill,
                        str(msg.get("name") or ""),
                        str(msg.get("description") or ""),
                        str(msg.get("body") or ""),
                        standing=bool(msg.get("standing")),
                        allowed_tools=msg.get("allowed_tools"),
                    )
                elif msg_type == "skill_update":
                    name = str(msg.get("name") or "")
                    if not name:
                        raise ValueError("skill_update: name required")
                    # Absent keys stay None → update_skill keeps that field.
                    # A panel edit claims ownership: stamp origin=user so the
                    # auto-miner will never overwrite what the user has touched
                    # (even if this skill was originally triage-minted).
                    from src.infrastructure.skills import SKILL_ORIGIN_USER
                    result = await asyncio.to_thread(
                        reg.update_skill, name,
                        new_name=msg.get("new_name"),
                        description=msg.get("description"),
                        body=msg.get("body"),
                        standing=msg.get("standing"),
                        allowed_tools=msg.get("allowed_tools"),
                        origin=SKILL_ORIGIN_USER,
                    )
                elif msg_type == "skill_delete":
                    name = str(msg.get("name") or "")
                    if not name:
                        raise ValueError("skill_delete: name required")
                    result = await asyncio.to_thread(reg.delete_skill, name)
                else:  # skill_import
                    src_path = str(msg.get("path") or "")
                    if not src_path:
                        raise ValueError("skill_import: path required")
                    result = await asyncio.to_thread(reg.import_skill, src_path)
                _emit({"type": "final", "id": msg_id, "result": result},
                      session_id=self._session_id)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, session_id=self._session_id)
            return

        # ── Activity Monitor IPC ───────────────────────────────────────
        if msg_type in ("personality_status", "personality_pause", "personality_resume"):
            try:
                if personality_monitor is None:
                    raise RuntimeError("activity monitor not initialised")
                if msg_type == "personality_pause":
                    personality_monitor.pause_by_user()
                elif msg_type == "personality_resume":
                    personality_monitor.resume_by_user()
                snap = personality_monitor.snapshot_status()
                _emit({"type": "final", "id": msg_id, "result": snap},
                      session_id=self._session_id)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, session_id=self._session_id)
            return

        # ── Scheduler / Cron IPC ───────────────────────────────────────
        if msg_type in (
            "cron_list", "cron_create",
            "cron_delete", "cron_set_enabled", "cron_run_now",
        ):
            try:
                if scheduler is None:
                    raise RuntimeError("scheduler not initialised")
                result = await self._handle_cron(msg_type, msg)
                _emit({"type": "final", "id": msg_id, "result": result},
                      session_id=self._session_id)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, session_id=self._session_id)
            return

        # ── /remember (manual high-trust memory candidate) ──────────────
        if msg_type == "ltm_remember":
            try:
                text = str(msg.get("text") or "").strip()
                if not text:
                    result: Dict[str, Any] = {"ok": False, "error": "text is required"}
                else:
                    from src.infrastructure.long_term_memory import LongTermMemory
                    from src.infrastructure.long_term_memory.candidates import (
                        submit_manual,
                    )
                    ltm = LongTermMemory.get()
                    ref = str(msg.get("ref") or "")
                    cid = await submit_manual(ltm=ltm, text=text, ref=ref)
                    result = {"ok": bool(cid), "candidate_id": cid}
                _emit({"type": "final", "id": msg_id, "result": result},
                      session_id=self._session_id)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, session_id=self._session_id)
            return

        if msg_type == "config_set":
            try:
                new_cfg = msg.get("config") or {}
                if not isinstance(new_cfg, dict):
                    raise ValueError("'config' must be a JSON object")
                logger.info("config_set: path=%s keys=%d", self.config_path.resolve(), len(new_cfg))

                # Snapshot the OLD personalization.git_hook_repos BEFORE
                # writing the new config. Settings is the source of truth
                # for which repos should carry our post-commit hook; the
                # bridge keeps `.git/hooks/post-commit` in sync by diffing
                # the lists on every save:
                #   added (new \ old)   → install hook
                #   removed (old \ new) → uninstall (only our marker)
                # The boot path no longer touches `.git/hooks/`; this is
                # the single place where hook side effects fire.
                old_cfg = self._load_config_dict()
                old_repos = self._normalised_hook_repos(old_cfg)

                self._save_config_dict(new_cfg)
                self._invalidate_shared_pool()
                # Reload config on every live flow so saved changes take
                # effect across all running sessions (was a single self._flow
                # check in the single-session world).
                for sid, flow in list(self._flows.items()):
                    try:
                        flow.config_manager.reload_config()
                    except Exception:
                        logger.exception(
                            "config_set: reload_config failed for sid=%s (continuing)",
                            sid,
                        )

                new_repos = self._normalised_hook_repos(new_cfg)
                added = new_repos - old_repos
                removed = old_repos - new_repos
                hook_results = await self._sync_git_hooks(
                    added=added, removed=removed,
                )

                # Note: ``personalization.enabled`` and
                # ``personalization.excluded_apps`` still take effect on
                # the NEXT bridge launch — those are wired into the
                # PersonalityMonitor at startup and don't have a runtime
                # apply path. ``git_hook_repos`` IS applied immediately
                # via the diff above, so the Settings UI's "restart to
                # apply" hint only applies to the first two fields.
                _emit({
                    "type": "final",
                    "id": msg_id,
                    "result": {
                        "saved": True,
                        "path": str(self.config_path.resolve()),
                        "git_hook_sync": hook_results,
                    },
                }, session_id=self._session_id)
            except Exception as exc:
                logger.exception("config_set failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_set failed: {exc}", "fatal": False},
                      session_id=self._session_id)
            return

        if msg_type == "request":
            sid = self._resolve_session_id(msg)
            if sid is None:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "request: missing session_id",
                       "fatal": False})
                return
            if sid in self._closing:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"request: session {sid} is being torn down",
                       "fatal": False})
                return
            try:
                goal = msg.get("goal", "")
                # Early API-key guard — only on the first request for this
                # session (before the FlowController is built). An empty key
                # causes cryptic errors deep in the LLM stack; surface a
                # clear message here instead.
                if sid not in self._flows:
                    _cfg = self._load_config_dict()
                    if not (((_cfg.get("llm") or {}).get("API_KEY") or "")):
                        _emit(
                            {
                                "type": "error",
                                "id": msg_id,
                                "where": "config",
                                "message": (
                                    "API key is not configured. "
                                    "Please open Settings → LLM Configuration "
                                    "and enter your API key."
                                ),
                                "fatal": False,
                            },
                            session_id=sid,
                        )
                        return
                self._ensure_flow(sid, goal=str(goal))
                flow = self._flows[sid]
                if not flow.started:
                    await flow.start()
                # ``on_user_message`` returns the coordinator's
                # reply string (sync conversational answer); background
                # agent work proceeds inside the flow's own
                # asyncio tasks and emits status events through the IM
                # delegate as it happens. ``final`` correlates with this
                # request id; subsequent activity arrives as status events.
                try:
                    reply = await flow.on_user_message(str(goal))
                    _emit({"type": "final", "id": msg_id,
                           "result": {"reply": reply, "ok": True}},
                          session_id=sid)
                except Exception as exc:
                    logger.exception("on_user_message failed; sid=%s id=%s",
                                     sid, msg_id)
                    _emit({"type": "error", "id": msg_id, "where": "engine",
                           "message": f"on_user_message failed: {exc}",
                           "fatal": False}, session_id=sid)
            except Exception as exc:
                logger.exception("request failed; sid=%s", sid)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"request failed: {exc}", "fatal": True},
                      session_id=sid)
            return

        if msg_type == "user_input":
            sid = self._resolve_session_id(msg)
            if sid is None:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "user_input: missing session_id",
                       "fatal": False})
                return
            if sid in self._closing:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"user_input: session {sid} is being torn down",
                       "fatal": False})
                return
            try:
                kind = msg.get("kind", "message")
                if kind == "message":
                    text = str(msg.get("text", ""))
                    flow = self._flows.get(sid)
                    if flow is not None and flow.started:
                        await flow.on_user_message(text)
                    else:
                        logger.warning(
                            "user_input(message) sid=%s before flow started; ignoring",
                            sid,
                        )
                elif kind == "confirmation":
                    # Normally consumed by the stdin reader fast-path; this
                    # branch covers the (rare) case where the envelope reaches
                    # the dispatcher via the asyncio inbox instead. Shares the
                    # sid-first/scan-fallback delivery with the reader path.
                    prompt_id = str(msg.get("id") or "")
                    answer = str(msg.get("answer", ""))
                    self._deliver_confirmation(sid, prompt_id, answer)
                elif kind == "desktop_takeover_revoked":
                    # Frontend overlay's revoke hotkey (Ctrl+C or
                    # equivalent) sends this. We flip the takeover flag
                    # so subsequent input actions refuse for the rest
                    # of this task; read-only desktop actions stay
                    # available. The notify_desktop_takeover_ended
                    # event with reason='user_revoked' is emitted by
                    # the DesktopState's revoke method itself.
                    try:
                        flow = self._flows.get(sid)
                        ds = (
                            flow._ctx.desktop_state
                            if flow is not None and flow._ctx is not None
                            else None
                        )
                        if ds is not None:
                            changed = ds.revoke_takeover()
                        else:
                            # Fallback for the (rare) race where revoke arrives
                            # before the flow's ctx is built — the module-level
                            # helper still toggles the takeover state machine.
                            from ..tools.desktop_tool import revoke_takeover
                            changed = revoke_takeover()
                        logger.info(
                            "user_input desktop_takeover_revoked: sid=%s changed=%s",
                            sid, changed,
                        )
                    except Exception as exc:
                        logger.warning(
                            "user_input desktop_takeover_revoked failed: %s", exc,
                        )
                else:
                    logger.warning("user_input: unknown kind=%r", kind)
                    _emit({"type": "error", "id": msg_id, "where": "bridge",
                           "message": f"Unknown user_input.kind: {kind!r}",
                           "fatal": False}, session_id=sid)
            except Exception as exc:
                logger.exception("user_input failed; sid=%s", sid)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"user_input failed: {exc}", "fatal": False},
                      session_id=sid)
            return

        if msg_type == "shutdown":
            await self._do_shutdown(msg_id)
            return

        if msg_type == "close_session":
            # Renderer's per-tab close button. Tears down the flow at the
            # requested session_id, drains its LLM service pool, drops all
            # per-session state from the bridge so the next message for a
            # different sid is unaffected.
            sid = self._resolve_session_id(msg)
            if sid is None:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "close_session: missing session_id",
                       "fatal": False})
                return
            await self._do_close_session(sid, msg_id)
            return

        logger.warning("unknown inbound type=%r id=%s", msg_type, msg_id)
        _emit({"type": "error", "id": msg_id, "where": "bridge",
               "message": f"Unknown message type: {msg_type!r}",
               "fatal": False})

    # ------------------------------------------------------------------
    # Cron / scheduler IPC
    # ------------------------------------------------------------------

    async def _handle_cron(self, msg_type: str, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Route cron_* envelopes onto the scheduler.

        We keep this here (not in scheduler/) because the IPC envelope
        shape is bridge-specific. The scheduler exposes a clean Python
        API; this method is the JSON / dict bridge layer.
        """
        from src.infrastructure.scheduler.schedule import ScheduleSyntaxError

        if msg_type == "cron_list":
            tasks = await scheduler.list_tasks()  # type: ignore[union-attr]
            return {"tasks": tasks}

        if msg_type == "cron_create":
            try:
                # The renderer only collects Name + Prompt; the schedule
                # string is inferred by an LLM from the prompt content.
                # If the caller (e.g. an old client / power user) supplies
                # an explicit schedule, we honour it without inference.
                schedule_str = (msg.get("schedule") or "").strip()
                prompt_str = str(msg.get("prompt", ""))
                dispatch_prompt = ""
                inference_failed = False
                if not schedule_str:
                    from src.infrastructure.scheduler.inferer import infer_schedule
                    # Single-use LLM service built from current config —
                    # see inferer.py module docstring for rationale.
                    config = self._load_config_dict()
                    result = await infer_schedule(prompt_str, config)
                    schedule_str = result.schedule
                    # result.ok is False when inference failed and fell back to
                    # daily 09:00 — surface it so the UI warns instead of the
                    # user silently getting "tomorrow 9am" for "in 1 minute".
                    inference_failed = not result.ok
                    # Empty dispatch_prompt = "use prompt verbatim at fire
                    # time"; honour that by leaving the field blank on the
                    # task. Avoids storing a redundant duplicate when the
                    # LLM had nothing to strip.
                    if result.prompt and result.prompt.strip() != prompt_str.strip():
                        dispatch_prompt = result.prompt
                # Normalise relative one-shot ("once in 1 minute") into
                # absolute ("once at <abs>"). Idempotent on other forms.
                from src.infrastructure.scheduler.schedule import normalize_schedule
                schedule_str = normalize_schedule(schedule_str)
                t = await scheduler.create_task(  # type: ignore[union-attr]
                    name=str(msg.get("name", "")),
                    prompt=prompt_str,
                    dispatch_prompt=dispatch_prompt,
                    schedule=schedule_str,
                )
            except ScheduleSyntaxError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "task": t, "inference_failed": inference_failed}

        if msg_type == "cron_delete":
            tid = str(msg.get("task_id") or msg.get("id") or "")
            ok = await scheduler.delete_task(tid)  # type: ignore[union-attr]
            return {"ok": bool(ok)}

        if msg_type == "cron_set_enabled":
            tid = str(msg.get("task_id") or msg.get("id") or "")
            t = await scheduler.set_enabled(  # type: ignore[union-attr]
                tid, bool(msg.get("enabled", False)),
            )
            return {"ok": bool(t), "task": t}

        if msg_type == "cron_run_now":
            tid = str(msg.get("task_id") or msg.get("id") or "")
            t = await scheduler.run_now(tid)  # type: ignore[union-attr]
            return {"ok": bool(t), "task": t}

        return {"ok": False, "error": f"unknown cron op: {msg_type}"}

    # ------------------------------------------------------------------
    # Scheduler dispatch — bridge-side hook called from
    # stdio_bridge.dispatch_scheduled_task().
    # ------------------------------------------------------------------

    async def accept_scheduled_task(self, task) -> bool:  # type: ignore[no-untyped-def]
        """Fire a scheduled task on the persistent V2 flow.

        Returns True iff dispatch was accepted (i.e. the bridge is alive
        and not shutting down). The scheduler reads the return value to
        decide whether to bump the next-fire timestamp.

        Scheduled fires are just-another-user-message. ``ok`` passed to
        :meth:`Scheduler.notify_task_finished` therefore means "the
        coordinator returned a reply without raising", NOT "the agent
        finished its background work". The persistent flow has no per-task
        completion signal — the agent runs continuously until the next user
        message. If finer-grained tracking is needed later, hook into
        ``TaskChannel.on_item_done`` for the items enqueued in response to
        this dispatch.
        """
        if self._shutdown_requested:
            logger.info(
                "scheduler dispatch refused: shutdown in progress task=%s",
                task.id[:8],
            )
            return False
        # Scheduled fires get a freshly-minted session — renderer's onStatus
        # lazily creates a tab when it sees an unknown session_id, so the
        # user sees the cron task pop up as its own session (clean
        # separation from interactive work). Each fire is an independent
        # task; nothing about the cron schedule itself implies persistence
        # across fires.
        sid = f"sched-{uuid.uuid4().hex[:12]}"
        from ..infrastructure.logger import set_session_context
        set_session_context(sid)
        # Renderer will see this sid for the first time and lazy-mount a tab.
        # Publish a notification first so the renderer can show a
        # "scheduled task firing" toast.
        try:
            _emit({
                "type": "status", "kind": "scheduled_task_started",
                "id": task.id, "name": task.name,
                "schedule": task.schedule,
                "prompt_preview": _truncate(task.prompt, 200),
                "session_name": f"⏱ {task.name}" if task.name else "⏱ Scheduled",
            }, session_id=sid)
        except Exception:
            logger.exception("scheduler emit failed")

        msg_id = f"sched-{task.id}-{int(time.time())}"
        # ``dispatch_prompt``, when present, is the agent-facing variant
        # with relative-time language ("一分钟后…") stripped — see
        # ScheduledTask docstring. Empty means "use prompt verbatim".
        goal_text = task.dispatch_prompt or task.prompt

        try:
            self._ensure_flow(sid, goal=str(goal_text))
            flow = self._flows[sid]
            if not flow.started:
                await flow.start()
        except Exception as exc:
            logger.exception("scheduler dispatch: flow setup failed")
            if scheduler is not None:
                try:
                    await scheduler.notify_task_finished(
                        task.id, ok=False, error=f"flow setup failed: {exc}",
                    )
                except Exception:
                    logger.exception("scheduler notify_task_finished crashed")
            return True

        ok = True
        err = ""
        # Acquire the per-sid dispatch lock so close_session can preempt via
        # _cancel_inflight, and any user_input the user later sends into this
        # sched tab properly serializes against the initial fire.
        lock = self._session_dispatch_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            # Wrap on_user_message in its own task so we can register it in
            # _inflight_by_sid for preemption WITHOUT cancelling the scheduler's
            # own long-running loop task.
            run_task = asyncio.create_task(
                flow.on_user_message(str(goal_text))
            )
            self._inflight_by_sid[sid] = run_task
            self._inflight_tasks.add(run_task)
            run_task.add_done_callback(self._inflight_tasks.discard)
            try:
                reply = await run_task
                _emit({"type": "final", "id": msg_id,
                       "result": {"reply": reply, "ok": True, "scheduled": True}},
                      session_id=sid)
            except asyncio.CancelledError:
                ok = False
                err = "cancelled by session close"
                logger.info(
                    "scheduled task %s cancelled (session closed)",
                    task.id[:8],
                )
            except Exception as exc:
                ok = False
                err = str(exc)[:500]
                logger.exception("scheduled task dispatch failed")
                _emit({"type": "error", "id": msg_id, "where": "engine",
                       "message": err, "fatal": False},
                      session_id=sid)
            finally:
                if self._inflight_by_sid.get(sid) is run_task:
                    self._inflight_by_sid.pop(sid, None)

        if scheduler is not None:
            try:
                await scheduler.notify_task_finished(task.id, ok=ok, error=err)
            except Exception:
                logger.exception("scheduler notify_task_finished failed")
            # Wake the scheduler so PENDING tasks get re-scanned now that
            # this dispatch has returned.
            try:
                scheduler._wakeup.set()
            except Exception:
                pass
        return True


    def _get_or_create_shared_pool(
        self,
    ) -> tuple[List[LLMService], List[LLMService]]:
        """Lazily build and cache the bridge-global LLM connection pools.

        Returns (main_services, helper_services). Each session wraps these
        in _SessionLLMService for session-scoped exhaustion tracking.
        """
        if self._shared_services is not None:
            return self._shared_services, self._shared_helper_services or []

        from ..infrastructure.role_resolver import resolve_models_and_helper

        cm = ConfigManager(str(self.config_path))
        cfg = cm.get_config()
        llm_cfg = cfg.get("llm", {}) or {}

        api_key = llm_cfg.get("API_KEY") or ""
        _mt_raw = llm_cfg.get("max_tokens")
        try:
            _mt_int = int(_mt_raw) if _mt_raw is not None else 0
        except (TypeError, ValueError):
            _mt_int = 0
        max_tokens: Optional[int] = _mt_int if _mt_int > 0 else None

        models, _helper_models = resolve_models_and_helper(llm_cfg)
        if not models:
            models = ["anthropic::claude-4-5-haiku"]

        _mt_kwargs: dict = {"max_tokens": max_tokens} if max_tokens is not None else {}
        self._shared_services = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=10, **_mt_kwargs,
            )
            for m in models
        ]

        _helper_model_names = _helper_models or models
        self._shared_helper_services = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=10, **_mt_kwargs,
            )
            for m in _helper_model_names
        ]

        logger.info(
            "Shared LLM pool created: %d main services, %d helper services",
            len(self._shared_services), len(self._shared_helper_services),
        )
        return self._shared_services, self._shared_helper_services

    def _invalidate_shared_pool(self) -> None:
        """Clear shared pool so next session rebuilds from fresh config."""
        self._shared_services = None
        self._shared_helper_services = None

    def _ensure_flow(self, session_id: str, goal: str) -> None:
        if session_id in self._flows:
            return

        # Make sure the UI delegate exists for this sid before we wire
        # anything up. All status events for this session route through it.
        ui = self._get_or_create_ui(session_id)

        cm = ConfigManager(str(self.config_path))
        cfg = cm.get_config()
        llm_cfg = cfg.get("llm", {}) or {}
        sess_cfg = cfg.get("session", {}) or {}

        # Allocate this session's directory under %USERPROFILE%\HandQ\History\.
        # The agent operates inside <session>/<workspace_subdir>/ — that's the
        # ONLY path the agent's prompt knows about. The session root itself
        # holds framework metadata (handq-engine.log, executions_logs/) and is
        # never named in the system prompt. A leading-dot folder name from
        # yaml (default ".workspace") just becomes a normal subdir on NTFS.
        workspace_subdir = sess_cfg.get("workspace_base", ".workspace") or ".workspace"
        session_dir = _allocate_session_dir(goal, workspace_subdir=workspace_subdir)
        agent_workspace = session_dir / workspace_subdir

        # Initialise the HandQ engine logger now that we know the session dir
        # and can read log_level from config.  Must happen before FlowController
        # construction so every get_logger() call in src/ picks up the right
        # level and file handler.
        #
        # Per-task engine log lives INSIDE the session dir (not the bridge's
        # launch-scoped log dir) so the user can find a session's full trace
        # alongside its session_state.json + executions_logs/ without having to
        # cross-reference two different roots. The bridge-scoped log
        # (handq-bridge.log under HANDQ_LOG_DIR) is unaffected — it stays where
        # it is for cross-session correlation.
        from ..infrastructure.logger import (
            initialize_logger,
            add_root_file_handler,
            remove_root_file_handler,
            LogLevel as _LogLevel,
        )
        _log_level_str = sess_cfg.get("log_level", "INFO") or "INFO"
        _engine_log_dir = str(session_dir)
        try:
            _level = _LogLevel[_log_level_str.upper()]
            # initialize_logger sets the "HandQ" logger's level + console
            # handler, but with log_file=None the per-session engine.log is NOT
            # bound to the "HandQ" name. We attach it to the ROOT logger below
            # so it captures the WHOLE session (HandQ tree + every stdlib
            # logging.getLogger(__name__) module: shell_tool / session_tool /
            # session_context / ...), minus the background-daemon trees that set
            # propagate=False (handq.ltm / personality / activity / scheduler,
            # diverted to .dia/internal-trace.log by bridge_main.py). Binding the
            # file to "HandQ" too would double-write every get_logger() record.
            initialize_logger(
                name="HandQ",
                level=_level,
                log_file=None,
                log_dir=_engine_log_dir,
            )
            # Detach any stale handler for THIS session before attaching the
            # new one (defensive — close_session normally removes it first).
            stale_handler = self._engine_log_handlers.get(session_id)
            remove_root_file_handler(stale_handler)
            self._engine_log_handlers[session_id] = add_root_file_handler(
                str(session_dir / "handq-engine.log"),
                level=_level,
                session_id=session_id,
            )
            logger.info(
                "_ensure_flow[sid=%s]: HandQ engine logger initialised; level=%s log_dir=%s",
                session_id, _log_level_str.upper(), _engine_log_dir,
            )
        except Exception:
            logger.exception(
                "_ensure_flow[sid=%s]: initialize_logger failed (continuing with default)",
                session_id,
            )

        api_key = llm_cfg.get("API_KEY") or ""

        # ── Wrap the shared pool for this session ──────────────────────────
        shared_main, shared_helper = self._get_or_create_shared_pool()
        consolidated_services: List[LLMService] = [
            _SessionLLMService(s) for s in shared_main
        ]
        helper_services: List[LLMService] = [
            _SessionLLMService(s) for s in shared_helper
        ]

        models = [s.model for s in shared_main]
        _helper_models = [s.model for s in shared_helper]

        logger.debug(
            "FlowController lazy construction[sid=%s]: n_models=%d n_helper=%d "
            "api_key_present=%s session_dir=%s",
            session_id,
            len(models), len(_helper_models),
            bool(api_key),
            session_dir,
        )
        if not api_key:
            logger.warning("llm.API_KEY is empty in config; LLM calls will fail")

        # Track every distinct service for shutdown — per-session list so we
        # can drain only this session's pool on close_session.
        self._services_by_session[session_id] = list(consolidated_services) + list(helper_services)

        # Wire server-error notifications to the UI. Server-side issues are
        # session-agnostic (an API outage hits every session identically),
        # so the envelopes carry no session_id — renderer treats them as
        # broadcast bridge-meta and shows them in the active tab as a
        # system bubble.
        def _on_llm_server_error(msg: str, retry_in: int, attempts_left: int) -> None:
            _emit({
                "type": "status",
                "kind": "llm_server_error",
                "message": msg,
                "retry_in": retry_in,
                "attempts_left": attempts_left,
            })
        for svc in self._services_by_session[session_id]:
            svc.on_server_error = _on_llm_server_error

        # ``on_reply_to_user`` callback bound to this session so the coordinator
        # reply lands on the correct chat tab.
        def _on_reply(text: str) -> None:
            self._on_coordinator_reply(session_id, text)

        flow = FlowControllerV2(
            llm_services=consolidated_services,
            working_directory=str(agent_workspace),
            storage_directory=str(session_dir),
            config_path=str(self.config_path),
            on_reply_to_user=_on_reply,
            expose_session_storage_in_prompt=False,
            helper_llm_services=helper_services,
            session_id=session_id,
        )
        self._flows[session_id] = flow
        logger.info(
            "FlowControllerV2 constructed[sid=%s]; %d llm_service(s) in fallback chain",
            session_id, len(consolidated_services),
        )

        # Tell the UI where this session's agent workspace lives. The renderer
        # uses this path to surface produced files (drag-out / save-to /
        # preview). Emitted exactly once per session at construction time;
        # the path is stable for the session's lifetime.
        try:
            _emit({
                "type": "status",
                "kind": "session_started",
                "session_dir": str(session_dir),
                "workspace_dir": str(agent_workspace),
                "session_name": goal.strip()[:30] if goal else None,
            }, session_id=session_id)
        except Exception:
            logger.exception("Failed to emit session_started status event")

        # The agent's working directory is carried per-session on
        # ``ctx.working_directory`` (== agent_workspace, passed to
        # FlowControllerV2 above). File tools resolve relative paths against it
        # via ``BaseTool.resolve_in_workspace``, and subprocess tools default
        # their cwd to it — so a bare-filename write / shell command lands in
        # the session workspace without touching the process cwd. We deliberately
        # do NOT os.chdir here: process cwd is a shared global, and leaving it
        # unmutated is what lets multiple sessions run concurrently without
        # fighting over it. (electron spawns the bridge with no cwd set; that
        # launch dir is now irrelevant because nothing resolves against it.)

        # Bind this session's UI delegate to the IM that FlowControllerV2 owns.
        # All ``notify_*`` / ``request_*`` calls inside the V2 stack route
        # through here. The loop ref lets the stdin reader thread resolve
        # confirmation futures via call_soon_threadsafe.
        if flow.interaction_manager is not None:
            flow.interaction_manager.set_delegate(ui)
        try:
            ui._loop = asyncio.get_running_loop()
        except RuntimeError:
            # _ensure_flow may run before the loop is captured into _stdin
            # context; the loop will be set on first await of a confirmation.
            pass

        # Tools (desktop_tool, browser_tool) pick up their IM ref from
        # ``ctx.interaction_manager`` at construction (SessionContext DI).
        # The bridge no longer wires ``set_interaction_manager`` on each
        # tool — flow.start() built ctx with self.interaction_manager and
        # passed ctx to PersistentAgent → ToolRegistry → tool __init__.

        # NOTE on llm_pool notifiers: ``set_fallback_notifier`` and
        # ``set_network_event_notifier`` are registered ONCE at bridge boot
        # (see StdioBridge.__init__) — these signals (model fallback,
        # network down/restored) are session-agnostic (any LLM service in
        # any session can trigger them), so the envelopes are broadcast
        # without session_id. The renderer shows them as system bubbles
        # in the active tab.

    def _on_coordinator_reply(self, session_id: str, text: str) -> None:
        """``FlowControllerV2.on_reply_to_user`` callback. Emits the
        coordinator's chat reply as a ``kind=reply`` status envelope so
        the renderer can render an assistant text bubble. Streaming
        (``reply_delta`` / ``reply_done``) is currently unwired — V2
        baseline emits the full reply once. session_id is closed-over when
        the callback is bound in _ensure_flow so multi-session replies
        land in the correct chat tab."""
        if not text:
            return
        _emit({"type": "status", "kind": "reply", "text": str(text)},
              session_id=session_id)

    # ------------------------------------------------------------------
    # New-session chain — equivalent to `handq new`. Designed for three
    # invariants:
    #
    #  (1) BOUNDED — never block the bridge longer than ~6s total. The
    #      renderer's New button is fire-and-forget; if cleanup stalls,
    #      the user can still type the next goal and it sits in the
    #      stdin queue until cleanup finishes.
    #  (2) NO LEAKS — release the FlowControllerV2 graph and the per-service
    #      httpx connection pools so the next `request` builds against
    #      fresh state. V2 has no IM singleton — the IM dies with the flow.
    #  (3) NO ORPHAN SUBPROCESSES on Windows. The shell tool spawns child
    #      processes with CREATE_NEW_PROCESS_GROUP. ``flow.cancel_all_tasks``
    #      cancels the asyncio tasks that own them; cooperative shutdown
    #      via the agent's run_loop catches CancelledError and drains.
    #
    # Stragglers: if a child task ignores cancellation entirely, it survives
    # as a background coroutine. It can emit a short tail of status envelopes
    # before its underlying I/O finally times out. The renderer's
    # closedSessions set drops them, so they don't pollute the new
    # conversation — only correctness issue avoided is process leak, and the
    # asyncio cancel handles that on the next event-loop pass.
    # ------------------------------------------------------------------

    # Cleanup budget. Bridge total stall ≤ GRACE + CLOSE * len(services)
    # — for the default 4 services, ≤ ~10s worst case, ≤ ~2s typical.
    _NEW_SESSION_GRACE_TIMEOUT = 2.5    # cooperative interrupt
    _NEW_SESSION_HARD_TIMEOUT  = 1.5    # after explicit cancel
    _NEW_SESSION_CLOSE_TIMEOUT = 2.0    # per-service httpx pool drain
    _SHUTDOWN_DRAIN_TIMEOUT    = 3.0    # bounded wait for in-flight dispatch
    _INFLIGHT_CANCEL_TIMEOUT   = 2.0    # bounded wait for a preempted request

    # Inbound types whose handler is per-session ORDER-sensitive: a second
    # request/user_input for the same sid must run after the first. These run
    # under the per-sid dispatch lock (FIFO), so same-sid stays ordered while
    # different sids run concurrently. The close_session lifecycle op
    # deliberately skips the lock — it PREEMPTS the in-flight
    # request by cancelling it (see _cancel_inflight), so a slow LLM round-trip
    # can't wedge a tab-close.
    _ORDERED_DISPATCH_TYPES = frozenset({"request", "user_input"})

    @staticmethod
    def _force_release_session_locks(ctx, session_id: str) -> None:  # type: ignore[no-untyped-def]
        """Defensive release of the cross-session desktop ownership lock
        held by this session, regardless of whether flow.destroy() completed
        cleanly or timed out.

        Without this, a session that wedges on Windows blocking I/O during
        destroy would keep its desktop lock held forever, blocking other
        sessions that try to drive input. The release helper on
        DesktopState is idempotent (a no-op when this session didn't own
        the lock to begin with), so calling it twice or out of order is
        safe.

        Browser is **not** force-released here: per the multi-session v2
        model each session owns an independent Chromium user-data-dir, so
        there is no cross-session browser lock to release. The browser's
        own ``close()`` is called via ``flow.destroy() → ctx.close() →
        browser_session.close()`` and any partial wedge just leaks one
        Chromium process — harmless for other sessions.

        Personality monitor is **not** explicitly resumed here either:
        the monitor's ``_paused`` property is computed by querying
        ``desktop_tool.is_any_session_holding_desktop`` directly, so once
        ``_release_global_takeover_if_owned`` clears the global owner
        above, the very next monitor tick will see ``_paused=False`` and
        un-pause automatically. No refcount, no notifications, no
        balancing call to track.
        """
        _ = session_id  # reserved for future per-session logging
        if ctx is not None:
            try:
                ds = getattr(ctx, "desktop_state", None)
                if ds is not None:
                    ds._release_global_takeover_if_owned()
            except Exception:
                logger.warning("force release: desktop lock release raised", exc_info=True)

    async def _do_close_session(
        self, session_id: str, msg_id: Optional[str],
    ) -> None:
        """Renderer-initiated tear-down. Tears down the flow AND drops the
        per-session UI delegate so the slot is truly empty — no auto-recreate
        on the next stray emit. Renderer's "X" button on
        each session tab fires this.

        The flow.destroy() chain guarantees flow no residue (browser closed,
        shells killed, SSH drained, screenshot store swept, asyncio tasks
        cancelled, ctx resources dropped). After return, the session_id
        slot is removed from every per-session dict on the bridge.
        """
        logger.info("close_session sequence begin; sid=%s id=%s", session_id, msg_id)
        t0 = time.monotonic()

        # Mark sid as closing so concurrent requests are rejected during
        # the tear-down window (see _handle for the guard).
        self._closing.add(session_id)

        # Preempt this session's in-flight request (N3): cancel + bounded-await
        # it so a slow LLM round-trip can't wedge the tab-close, and so it
        # stops emitting/mutating per-session state before teardown.
        await self._cancel_inflight(session_id)

        flow = self._flows.pop(session_id, None)
        services = self._services_by_session.pop(session_id, [])
        with self._uis_lock:
            self._uis.pop(session_id, None)
        # Unknown-sid path (silent success by design — see §close_session in
        # MULTI_SESSION_DESIGN.md). Four legitimate triggers:
        #   1. User double-clicked the tab's X button (second IPC arrives
        #      after the first already cleaned up the sid).
        #   2. IPC reordering — close arrived before request actually built
        #      the flow.
        #   3. Renderer restart sent a stale close for a sid the new renderer
        #      doesn't know about, but the bridge already cleared.
        #   4. Test fixture / scripted client sent a typo'd sid.
        # We still emit ``close_session: ok`` because the renderer's UI is
        # authoritative: if the user closed the tab, bridge agreeing is
        # correct regardless of internal residue. The warning makes
        # production occurrences visible without disrupting flow.
        if flow is None and not services:
            logger.warning(
                "close_session[sid=%s]: no active flow/services found "
                "(double-close, IPC race, renderer restart, or unknown sid); "
                "proceeding with idempotent cleanup",
                session_id,
            )
        # Keep a ref to ctx for defensive lock release in finally — see
        # _force_release_session_locks. Without this, a wedged flow.destroy
        # leaves the global desktop/browser locks held forever and other
        # sessions deadlock on them.
        ctx_ref = getattr(flow, "_ctx", None) if flow is not None else None

        # Detach + close this session's engine.log handler.
        handler = self._engine_log_handlers.pop(session_id, None)
        try:
            from ..infrastructure.logger import remove_root_file_handler
            remove_root_file_handler(handler)
        except Exception:
            logger.exception(
                "close_session[sid=%s]: failed to detach engine.log handler",
                session_id,
            )

        try:
            if flow is not None:
                try:
                    await asyncio.wait_for(flow.destroy(), timeout=2.5)
                except asyncio.TimeoutError:
                    logger.warning(
                        "close_session[sid=%s]: flow.destroy timed out (2.5s)",
                        session_id,
                    )
                except Exception:
                    logger.warning(
                        "close_session[sid=%s]: flow.destroy failed",
                        session_id, exc_info=True,
                    )

            for i, svc in enumerate(services):
                try:
                    await asyncio.wait_for(
                        svc.close(),
                        timeout=self._NEW_SESSION_CLOSE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "close_session[sid=%s]: svc[%d].close timed out after %.1fs",
                        session_id, i, self._NEW_SESSION_CLOSE_TIMEOUT,
                    )
                except Exception:
                    logger.warning(
                        "close_session[sid=%s]: svc[%d].close failed",
                        session_id, i, exc_info=True,
                    )
        except Exception:
            logger.exception(
                "close_session[sid=%s] chain raised unexpectedly", session_id,
            )
        finally:
            # Defensive: release the cross-session desktop/browser locks
            # even if flow.destroy timed out.
            self._force_release_session_locks(ctx_ref, session_id)
            self._closing.discard(session_id)
            # Drop the per-sid dispatch lock + in-flight ref — the session is
            # gone, so the next message for this sid (if any) starts clean.
            self._session_dispatch_locks.pop(session_id, None)
            self._inflight_by_sid.pop(session_id, None)
            elapsed = (time.monotonic() - t0) * 1000.0
            _emit({"type": "final", "id": msg_id,
                   "result": {"close_session": "ok",
                              "session_id": session_id,
                              "elapsed_ms": round(elapsed, 1)}},
                  session_id=session_id)

    # ------------------------------------------------------------------
    # Shutdown chain (per backend_surface.md §1)
    # ------------------------------------------------------------------

    async def _do_shutdown(self, msg_id: Optional[str]) -> None:
        # _shutdown_requested is already True (set by run() before drain).
        # Double-shutdown cannot occur: the run-loop breaks immediately after
        # this handler returns, and no other caller invokes _do_shutdown.
        logger.info("shutdown sequence begin; id=%s", msg_id)
        overall_t0 = time.monotonic()

        def _step_ms(t0: float) -> float:
            return (time.monotonic() - t0) * 1000.0

        try:
            # Tear down every live session's flow + service pool. Each
            # destroy/close is independent — one stuck flow doesn't block
            # the others.
            session_ids = list(self._flows.keys())
            if not session_ids:
                logger.info("shutdown: no FlowControllerV2 to destroy")
            for sid in session_ids:
                flow = self._flows.pop(sid, None)
                services = self._services_by_session.pop(sid, [])
                t0 = time.monotonic()
                if flow is not None:
                    try:
                        # ``flow.destroy()`` is async: it trips the TaskChannel
                        # interrupt event, cancels both run-loops, and awaits
                        # ``SessionContext.close()`` to tear down all per-session
                        # resources (browser / shells / SSH pool / desktop state /
                        # file state). The bridge no longer detaches IM refs or
                        # calls flush_*_pool — destroy does all of it in one place.
                        await asyncio.wait_for(flow.destroy(), timeout=2.5)
                        logger.info(
                            "shutdown[sid=%s]: flow.destroy OK (%.2f ms)",
                            sid, _step_ms(t0),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "shutdown[sid=%s]: flow.destroy timed out (2.5s); "
                            "leaving any stragglers as background — "
                            "closedSessions will filter their late emits", sid,
                        )
                    except Exception:
                        logger.warning(
                            "shutdown[sid=%s]: flow.destroy failed (%.2f ms)",
                            sid, _step_ms(t0), exc_info=True,
                        )
                for i, svc in enumerate(services):
                    t0 = time.monotonic()
                    try:
                        await asyncio.wait_for(
                            svc.close(),
                            timeout=self._NEW_SESSION_CLOSE_TIMEOUT,
                        )
                        logger.info(
                            "shutdown[sid=%s]: svc[%d].close OK (%.2f ms)",
                            sid, i, _step_ms(t0),
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "shutdown[sid=%s]: svc[%d].close timed out after "
                            "%.1fs (%.2f ms)",
                            sid, i, self._NEW_SESSION_CLOSE_TIMEOUT,
                            _step_ms(t0),
                        )
                    except Exception:
                        logger.warning(
                            "shutdown[sid=%s]: svc[%d].close failed (%.2f ms)",
                            sid, i, _step_ms(t0), exc_info=True,
                        )

            # Close the shared LLM connection pools (the actual httpx clients).
            for svc in (self._shared_services or []) + (self._shared_helper_services or []):
                try:
                    await asyncio.wait_for(svc.close(), timeout=2.0)
                except Exception:
                    pass
            self._shared_services = None
            self._shared_helper_services = None
        except Exception:
            logger.exception("shutdown chain raised unexpectedly")
        finally:
            logger.info("shutdown sequence complete (%.2f ms total)", _step_ms(overall_t0))
            _emit({"type": "final", "id": msg_id, "result": {"shutdown": "ok"}})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Concurrent per-session dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Run ONE inbound envelope as its own task (F1).

        ``request`` / ``user_input`` for a sid run under that sid's dispatch
        lock so same-session messages stay in arrival order, while different
        sessions run concurrently. The running request task is published in
        ``_inflight_by_sid`` so a lifecycle op can preempt it (N3).

        Everything else (lifecycle ops, bridge-meta) runs directly — lifecycle
        handlers cancel the in-flight request themselves.
        """
        try:
            mtype = msg.get("type") if isinstance(msg, dict) else None
            if mtype in self._ORDERED_DISPATCH_TYPES:
                sid = self._resolve_session_id(msg)
                if sid is not None:
                    from ..infrastructure.logger import set_session_context
                    set_session_context(sid)
                    lock = self._session_dispatch_locks.setdefault(
                        sid, asyncio.Lock()
                    )
                    async with lock:
                        task = asyncio.current_task()
                        if task is not None:
                            self._inflight_by_sid[sid] = task
                        try:
                            await self._handle(msg)
                        finally:
                            if self._inflight_by_sid.get(sid) is task:
                                self._inflight_by_sid.pop(sid, None)
                    return
            await self._handle(msg)
        except asyncio.CancelledError:
            # Preempted by a lifecycle op (N3) or shutdown — let it unwind.
            raise
        except Exception as exc:
            logger.exception(
                "dispatch task crashed (type=%s)",
                msg.get("type") if isinstance(msg, dict) else None,
            )
            try:
                _emit({"type": "error", "where": "bridge",
                       "message": f"dispatch crashed: {exc}", "fatal": False})
            except Exception:
                pass

    async def _cancel_inflight(self, session_id: str) -> None:
        """Cancel + bounded-await the in-flight request/user_input task for a
        session so a lifecycle op (close/new) can preempt a slow LLM round-trip
        (N3). No-op when the session has no task running."""
        task = self._inflight_by_sid.pop(session_id, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        logger.info("preempting in-flight request; sid=%s", session_id)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=self._INFLIGHT_CANCEL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "in-flight request for sid=%s did not unwind in %.1fs; "
                "proceeding with teardown", session_id,
                self._INFLIGHT_CANCEL_TIMEOUT,
            )
        except asyncio.CancelledError:
            pass  # expected: the task we just cancelled
        except Exception:
            logger.debug("in-flight task for sid=%s raised on unwind",
                         session_id, exc_info=True)

    async def _drain_inflight(self, timeout: float) -> None:
        """Bounded wait for every live dispatch task before shutdown teardown.
        Stragglers past the deadline are left for ``_do_shutdown`` to cancel
        via each flow's ``destroy()``."""
        tasks = [t for t in self._inflight_tasks if not t.done()]
        if not tasks:
            return
        logger.info("shutdown: draining %d in-flight dispatch task(s)",
                    len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "shutdown: %d dispatch task(s) still pending after %.1fs "
                "drain; proceeding to teardown", len(pending), timeout,
            )

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._inbox = asyncio.Queue()

        reader = threading.Thread(
            target=self._stdin_reader, daemon=True, name="bridge-stdin"
        )
        reader.start()
        logger.info("event loop online; awaiting messages")

        first_msg_seen = False
        while True:
            msg = await self._inbox.get()
            if msg is None:
                # stdin EOF — exit cleanly.
                logger.info("inbox sentinel received; exiting main loop")
                break
            if not first_msg_seen:
                first_msg_seen = True
                # First inbound envelope confirms the renderer→bridge IPC
                # path is fully alive in BOTH directions (renderer wrote,
                # we read). Logging this explicitly turns "is the bridge
                # listening?" from a guess into a grep-able fact.
                logger.info(
                    "first IPC envelope received: type=%s id=%s",
                    msg.get("type") if isinstance(msg, dict) else type(msg).__name__,
                    msg.get("id") if isinstance(msg, dict) else None,
                )

            mtype = msg.get("type") if isinstance(msg, dict) else None
            if mtype == "shutdown":
                # Quiesce the scheduler FIRST: setting the flag prevents new
                # scheduled fires from being accepted (accept_scheduled_task
                # checks it). Only then drain existing in-flight tasks (which
                # now includes any live sched child tasks) before teardown.
                self._shutdown_requested = True
                # Shutdown is handled inline (not as a background task): drain
                # the live per-session dispatch tasks (bounded), then run the
                # teardown chain and exit the loop. Running it inline guarantees
                # we don't start tearing down flows while their request tasks
                # are still mutating per-session state.
                await self._drain_inflight(self._SHUTDOWN_DRAIN_TIMEOUT)
                try:
                    await self._handle(msg)
                except Exception as exc:
                    logger.exception("shutdown handler raised")
                    _emit({"type": "error", "where": "bridge",
                           "message": f"shutdown crashed: {exc}",
                           "fatal": False})
                break

            # Every other envelope runs as its own task so a slow LLM
            # round-trip for one session never blocks another session's input.
            task = asyncio.create_task(self._dispatch(msg))
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)

            if self._shutdown_requested:
                logger.info("shutdown_requested flag observed; exiting main loop")
                break


async def run() -> None:
    """Module-level entry point used by ``bridge_main.py``."""
    config_path = os.environ.get("HANDQ_CONFIG", DEFAULT_CONFIG_PATH)
    logger.info("StdioBridge.run() entry; config=%s", config_path)
    try:
        bridge = StdioBridge(config_path=config_path)
        logger.info("StdioBridge constructed; starting main loop")
        await bridge.run()
    except Exception:
        logger.exception("StdioBridge.run() raised")
        raise
    finally:
        logger.info("StdioBridge.run() exit")


if __name__ == "__main__":
    asyncio.run(run())
