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
import dataclasses
import uuid
import json
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from src.controller_v2.resume_index import ResumeCandidate

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
#               digest.json            SessionDigest — session_digest.py
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

    def show_user_notice(self, message: str, urgent: bool = False) -> None:
        """Prominent agent→user message. Renderer maps ``kind=agent_notice``
        to a standalone system bubble (same weight as a network banner), NOT
        to the scrolling step trace.
        """
        _emit({"type": "status", "kind": "agent_notice",
               "message": str(message or ""),
               "urgent": bool(urgent)},
              session_id=self._session_id)

    def show_user_message_echo(self, text: str) -> None:
        """Replay of the operator's OWN message, published by the被控 server so
        a reattaching tab can redraw a bubble the local DOM never persisted.
        Renderer maps ``kind=user_message_echo`` to the same user-role bubble
        the local submit handler draws synchronously via ``addUserBubble``."""
        _emit({"type": "status", "kind": "user_message_echo",
               "text": str(text or "")},
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

    def notify_file_touch(
        self, path: str, kind: str, tool: str, item_id: str = "",
        reversible: bool = False,
    ) -> None:
        """Live file-touch event → renderer ``kind=file_touch``. Drives the
        per-session right-side sidebar (nebula + file-change list). Fired from
        write_tool / edit_tool / read_tool right after a successful op; the
        renderer accumulates them per session and updates the nebula orb +
        change list in place. Kind mapping: ``read`` blue, ``edit`` amber,
        ``hit`` green (grep-style scan). ``item_id`` is the task item the
        touch happened inside — empty string between items. ``reversible``
        is True only when a pre-op rewind snapshot exists; the sidebar gates
        the per-file ↺ button on this so files it can't restore (shell mtime
        hits, read/grep/glob) never expose the affordance."""
        _ui_logger.debug("notify_file_touch: %s %s %s item=%s reversible=%s",
                         kind, tool, _truncate(path, 80), item_id, reversible)
        _emit({"type": "status", "kind": "file_touch",
               "path": str(path or ""),
               "touch": str(kind or ""),
               "tool": str(tool or ""),
               "item_id": str(item_id or ""),
               "reversible": bool(reversible)},
              session_id=self._session_id)

    def show_coordinator_reply(self, text: str) -> None:
        """The coordinator's complete reply, as one assistant bubble.

        Same envelope :meth:`StdioBridge._on_coordinator_reply` used to emit
        inline. It became a delegate method so the remote-control path can carry
        it: ``FlowControllerV2`` delivers this message through the
        ``on_reply_to_user`` callback rather than the InteractionManager
        (``flow_controller.py:533``), which means it bypasses whatever delegate is
        installed. Routing it through the delegate instead lets a被控 session's
        ``NetworkUIDelegate`` put it on the wire, and lets the控制 side replay it
        here with no special case. Non-streamed replies and background
        task-completion summaries arrive only this way.
        """
        if not text:
            return
        _emit({"type": "status", "kind": "reply", "text": str(text)},
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
        timeout: Optional[float] = None,
    ) -> str:
        """Emit a confirmation envelope; await the renderer's response.

        Registers a fresh ``asyncio.Future`` under ``prompt_id``; the stdin
        reader thread resolves it via :meth:`deliver_confirmation_response`
        when the user answers in the renderer modal.

        ``timeout`` is ``None`` (unbounded) for risk/tool/secret confirmations
        — those are deliberately allowed to sit until the user acts. Only the
        ``ask_human`` caller passes a real deadline (``ASK_HUMAN_TIMEOUT_S``):
        on expiry the pending future is dropped AND an ``ask_human_expired``
        envelope is emitted so the renderer closes the stale modal and leaves
        a transcript record — without this, the modal would linger forever
        with no signal that the Python side gave up and moved on.
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
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            with self._pending_lock:
                self._pending.pop(prompt_id, None)
            if kind == "ask_human":
                _emit(
                    {"type": "status", "kind": "ask_human_expired", "id": prompt_id},
                    session_id=self._session_id,
                )
            raise
        except asyncio.CancelledError:
            with self._pending_lock:
                self._pending.pop(prompt_id, None)
            if kind == "ask_human":
                # A remote-controlled session's relayed ask_human prompt is
                # cancelled this way (RemoteSessionBridge.on_confirm_cancel
                # cancels the local _answer_confirm task when the 被控 side's
                # own timeout fires) — same envelope as the direct-timeout
                # branch above so both paths converge on one renderer handler.
                _emit(
                    {"type": "status", "kind": "ask_human_expired", "id": prompt_id},
                    session_id=self._session_id,
                )
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

    async def request_user_form(self, question: str, fields: list) -> Dict[str, Any]:
        """Free-form (non-secret) clarifying question from the agent
        (``ask_human`` tool). Emits ``kind=ask_human`` with the markdown
        ``question`` plus a ``fields`` list the renderer turns into labeled
        text/textarea/radio/checkbox controls, and awaits the user's answer.

        The renderer answers with a JSON-encoded ``{field_id: value}`` object
        string (see ``renderer.js``'s ask_human submit handler). Decoded here
        rather than at the caller so every ``request_user_form`` caller —
        local and remote — gets the same dict shape back. A malformed or
        legacy plain-string reply decodes to ``{"answer": <raw string>}`` so
        an old renderer build (or a stray non-JSON reply) never crashes the
        wait; it just loses structure.
        """
        prompt_id = f"ask-{int(time.time() * 1000)}"
        payload = {"question": str(question), "fields": fields or []}
        from ..tools.ask_human_tool import ASK_HUMAN_TIMEOUT_S
        try:
            raw = await self._await_user_response(
                "ask_human", payload, prompt_id, timeout=ASK_HUMAN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return {}
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
        return {"answer": raw or ""}


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class _ServedDesktopNotifier:
    """Local-side signal that a remote controller is driving THIS desktop.

    Passed to ``NetworkUIDelegate`` as its ``local_delegate``, and deliberately
    implements only the two takeover methods: ``_mirror_local`` resolves each
    call with ``getattr(self._local, method, None)`` and skips misses
    (``network_delegate.py:93``), so a narrow object silently ignores the other
    fifteen fire-and-forget events. That is the whole point — the被控 machine
    still gets **no mirror tab**, it just stops being blind to the one event
    where blindness is a safety problem.

    The gap this closes: the delegate used to be built with
    ``local_delegate=None``, so ``notify_desktop_takeover_started`` went only
    over the wire. The Electron overlay (fullscreen border, corner watermark)
    and the Ctrl+Shift+C revoke hotkey both react to a *locally* emitted
    takeover envelope, so the person sitting at the machine got no indication
    at all that another operator's agent was moving their cursor, and no way to
    stop it — the only revoke channel was the remote operator's own.

    Two things make this safe to emit where the mirror-tab gate would otherwise
    catch it:

    * The envelope is **unstamped** — it goes through the module-level ``_emit``
      rather than ``StdioBridge._emit_session``, because this is machine-level
      news ("someone is driving this box"), exactly like
      ``remote_serve_state``.
    * The session id travels as ``served_session_id``, never ``session_id``.
      ``renderer.js``'s status handler mounts a tab for any envelope carrying a
      ``session_id`` it has not seen, without looking at ``kind``, so putting
      the rc- id under that key is precisely the bug that has been fixed twice
      already. Under a different key the renderer stays blind while
      ``main.js`` still gets the id it needs to bind the revoke hotkey.

    The hotkey then writes ``user_input``/``desktop_takeover_revoked`` stamped
    with the rc- sid, which the bridge's ordinary local handler resolves against
    ``_flows["rc-…"]`` — a real ``FlowControllerV2`` with a real
    ``DesktopState`` — so a local revoke genuinely stops the remote agent, and
    the resulting ``notify_desktop_takeover_ended("user_revoked")`` reaches the
    remote operator through the same ``NetworkUIDelegate``.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def _emit_takeover(self, kind: str, reason: str) -> None:
        sid = str(getattr(self._session, "session_id", "") or "")
        _emit({
            "type": "status",
            "kind": kind,
            # NOT "session_id" — see the class docstring.
            "served_session_id": sid,
            "controller_name": str(
                getattr(self._session, "controller_name", "") or ""
            ),
            "title": str(getattr(self._session, "title", "") or ""),
            "reason": str(reason or ""),
        })

    def notify_desktop_takeover_started(self, reason: str = "input_action") -> None:
        self._emit_takeover("served_desktop_takeover_started", reason)

    def notify_desktop_takeover_ended(self, reason: str = "task_ended") -> None:
        self._emit_takeover("served_desktop_takeover_ended", reason)


class _BridgeSessionHost:
    """被控-side ``SessionHost`` for the Electron host.

    Builds a driven session by calling the bridge's ordinary
    :meth:`StdioBridge._ensure_flow` and then swapping the delegate. Going
    through the normal path rather than constructing a ``FlowControllerV2``
    directly is what makes a remote-driven session a first-class local session:
    it gets a real timestamped session directory under ``~/HandQ/History``, a
    per-session ``handq-engine.log``, the shared LLM pool, and a digest
    checkpointed on every item boundary.

    What it does NOT get is a tab in this machine's own renderer — v6 removed the
    mirror tab. The enforcement lives in ``StdioBridge._emit_session``, which
    drops every session-stamped envelope for a sid in ``_served_sessions``; the
    delegate swap below only covers events that travel through the
    ``InteractionManager``, and the paths that don't are exactly the ones that
    grew the dead tab twice. The person sitting at the被控 machine sees what is
    being done through the Connect panel's As Server dashboard, which reads the
    server's session registry.

    Reusing the local setup path has a cost that has to be paid back explicitly:
    it allocates six per-session things beyond the flow, and ``flow.destroy()``
    frees none of them. :meth:`on_session_destroyed` is that repayment.

    ``FlowControllerV2`` itself satisfies ``RemoteFlowHandle`` (it already has
    ``on_user_message`` and ``destroy``), so it is returned as-is.
    """

    def __init__(self, bridge: "StdioBridge") -> None:
        self._bridge = bridge

    def describe(self) -> Dict[str, str]:
        return {"name": platform.node(), "platform": sys.platform}

    async def create_flow(self, session: Any, goal: str) -> Any:
        from ..remote_control.network_delegate import NetworkUIDelegate

        sid = session.session_id
        bridge = self._bridge

        # Register as served BEFORE building anything. From this point on
        # ``bridge._emit_session`` drops every session-stamped envelope for this
        # sid, which is what keeps the被控 machine from growing a dead mirror
        # tab — see ``StdioBridge._emit_session`` for why this is a gate rather
        # than a flag threaded through _ensure_flow, and for the two paths that
        # each defeated the flag version in turn.
        bridge._served_sessions[sid] = session

        # Reuse the whole local setup path to get a real FlowControllerV2 with
        # session dir, per-session engine log, LLM pool, digest checkpointing.
        # The flow exists on THIS machine and drives the agent — the only thing
        # different from a local session is that the IM delegate pushes events
        # over the wire instead of onto a local _StdioUI.
        try:
            bridge._ensure_flow(sid, goal=goal)
        except Exception:
            # Never leave the sid registered as served without a flow: the entry
            # would silently swallow envelopes for a sid the bridge may later
            # reuse as an ordinary local session.
            bridge._served_sessions.pop(sid, None)
            raise
        flow = bridge._flows[sid]

        # A NARROW local delegate, not a full one: the server still does not
        # create a mirror tab (the dashboard observes sessions through the
        # RemoteControlServer's registry — session.describe() + event_log — not
        # through a second delegate chain into the renderer, per the v6
        # "server端不用镜像标签页" decision). What it does now carry locally is
        # the desktop-takeover pair, so the person at this machine can SEE and
        # REVOKE a remote operator driving their screen. See
        # _ServedDesktopNotifier for why that cannot be a stamped envelope.
        delegate = NetworkUIDelegate(
            session, local_delegate=_ServedDesktopNotifier(session)
        )
        if flow.interaction_manager is not None:
            flow.interaction_manager.set_delegate(delegate)
        # The coordinator's one-shot reply and every task-completion summary
        # arrive through on_reply_to_user, which bypasses the delegate entirely
        # (flow_controller.py:533). Redirect that sink or the controlling
        # machine never sees either.
        bridge._reply_sinks[sid] = delegate.show_coordinator_reply

        # Name this session for the benefit of OTHER sessions. When a local
        # session's desktop action queues behind this one on the process-wide
        # ownership lock, the wait message is the only explanation the local user
        # gets — and "another session" badly understates "an operator on a
        # different computer is using your mouse". Set here rather than in
        # FlowControllerV2 because the bridge is the only layer that knows a sid
        # is served. See desktop_tool._describe_desktop_holder.
        ctx = getattr(flow, "_ctx", None)
        desktop_state = getattr(ctx, "desktop_state", None) if ctx is not None else None
        if desktop_state is not None:
            controller = str(getattr(session, "controller_name", "") or "").strip()
            try:
                desktop_state.label = (
                    f"a session being driven remotely by {controller}"
                    if controller else
                    "a session being driven remotely from another machine"
                )
            except Exception:
                logger.debug("could not label served desktop state for %s", sid,
                             exc_info=True)

        if not flow.started:
            await flow.start()
        bridge._broadcast_serve_state()
        return flow

    async def handle_user_input(
        self, session: Any, kind: str, payload: Dict[str, Any]
    ) -> None:
        if kind != "desktop_takeover_revoked":
            logger.warning("remote_control: unknown user_input kind %r", kind)
            return
        # Same two-step fallback the local handler uses: prefer this session's
        # DesktopState, fall back to the module-level toggle if ctx isn't built
        # yet. A remote operator's panic revoke must not depend on that race.
        flow = self._bridge._flows.get(session.session_id)
        ctx = getattr(flow, "_ctx", None) if flow is not None else None
        desktop_state = getattr(ctx, "desktop_state", None) if ctx is not None else None
        if desktop_state is not None:
            desktop_state.revoke_takeover()
            return
        from ..tools.desktop_tool import revoke_takeover

        revoke_takeover()

    async def handle_rpc(
        self, session: Any, action: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        if action != "file_undo":
            raise ValueError(f"unsupported remote rpc {action!r}")
        flow = self._bridge._flows.get(session.session_id)
        if flow is None:
            raise RuntimeError("session has no flow")
        return await flow.undo_files(payload.get("item_id"))

    async def push_skills(self, skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        from ..infrastructure.skills import SkillRegistry

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

    def on_client_released(self) -> None:
        """Client sent an explicit Disconnect. On Windows the server just goes
        back to waiting for the next client — the sessions have already been
        destroyed by the server. Refresh the dashboard so it shows "no client".
        """
        try:
            self._bridge._broadcast_serve_state()
        except Exception:
            logger.debug("remote_control: serve-state broadcast after release failed",
                         exc_info=True)

    async def on_session_destroyed(self, session: Any) -> None:
        """Release the per-session state ``create_flow`` allocated via
        ``_ensure_flow``. See ``StdioBridge._purge_remote_session_state`` for why
        awaiting ``flow.destroy()`` is not enough on its own."""
        await self._bridge._purge_remote_session_state(session.session_id)
        try:
            self._bridge._broadcast_serve_state()
        except Exception:
            logger.debug("remote_control: serve-state broadcast after destroy failed",
                         exc_info=True)


# ---------------------------------------------------------------------------
# Session resume — pending-offer bookkeeping (docs/session_resume_design.md §6.4)
# ---------------------------------------------------------------------------
#
# A resume offer is a SOFT prompt surfaced to the renderer, fully decoupled
# from whether the triggering message actually executes:
#
#   * Resume search and ``on_user_message`` are independent — the search
#     runs first (so a hit's candidate card reaches the wire before the
#     coordinator's own reply/thinking envelopes, letting the user see
#     "candidates" and "coordinator answer" as two distinct signals), but
#     the message is NEVER held — on_user_message always runs right after,
#     every turn, hit or miss. This deliberately replaced an earlier
#     "hold the message up to 30s awaiting a decision" model: the user
#     found it confusing that resume search and the coordinator's own
#     answer were coupled — asking a normal question and having it hit an
#     old session's title made the reply itself wait on an unrelated UI
#     choice. Now they're independent: a hit is a side-channel hint the
#     user can act on or ignore; it is never a gate on this turn.
#
#   * The permanent stop switch (``SessionContext.resume_search_disabled``)
#     is driven by INTENT's own classification, not by the search hit/miss
#     itself — see ``StdioBridge._on_coordinator_intent``: the moment a
#     turn's FINAL intent lane resolves to "queue" (a real task, as opposed
#     to plain chat or a stop/cancel "interrupt"), the session's identity is
#     considered settled and searching stops for good, withdrawing whatever
#     card is showing. Chat keeps searching every turn (§ continuous
#     search); interrupt is inert (a control command isn't "starting a
#     task"). The user's explicit "Not resuming" button sets the same flag
#     directly, independent of INTENT.
#
# The _PendingResumeOffer TTL (120s) is a separate, cosmetic concern: how long
# the renderer keeps showing a card before hiding it. Expiry is checked lazily
# on lookup (no sweeper) — an expired offer is just gone.

_RESUME_OFFER_TTL_SECONDS = 120.0

# Bound on how long a resume search will wait for the boot-time warm-build
# to finish (see StdioBridge._resume_index_ready) before giving up and
# failing open. Cold build (model load + first-time embed of every existing
# digest) measured ~10.8s at 92 sessions (2026-08-01) — 15s covers that with
# headroom for a somewhat larger corpus without making a user who types
# during the boot window wait indefinitely if the warm-build is genuinely
# stuck.
_RESUME_INDEX_WAIT_TIMEOUT = 15.0


@dataclasses.dataclass
class _PendingResumeOffer:
    """One offer surfaced to the renderer after a resume search hit the
    gate (now re-evaluated on every message in the session, not just the
    first — see ``StdioBridge._refresh_resume_offer``). Tracked per
    session_id so a later ``resume_confirm``/``resume_dismiss`` can find it."""

    candidates: List["ResumeCandidate"]
    expires_at: float  # time.monotonic() deadline
    # The search query text that produced these candidates — for the
    # session's first message this is just that message; for any later
    # message it's the whole conversation-so-far + that message (see
    # ``_resume_query_text_for_followup``). Kept only for logging/debugging
    # (e.g. the ``_ensure_flow`` call's ``goal`` fallback when a resumed
    # candidate has no title of its own) — NOT replayed as a task. §
    # review-first resume: accepting an offer queues a fixed review
    # instruction, never this text (see _RESUME_REVIEW_INSTRUCTION) — this
    # is a "resume this" signal, not task content.
    goal: str


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

    # ── Remote-control slots, declared at class level ─────────────────────
    # These are optional subsystems (docs/fleet_scheduling_design.md): a bridge
    # that never serves and never controls leaves all four untouched. They are
    # declared here, not only assigned in __init__, because the test suite builds
    # bridges via ``object.__new__(StdioBridge)`` plus manual attribute wiring
    # (see tests_v3/test_resume_bridge.py::_make_bare_bridge) — a pattern that
    # skips __init__ entirely. Class-level defaults keep those instances working
    # without every read site needing a getattr, and without the test helper
    # having to track each new field.
    #
    # The two dicts being mutable class attributes is safe, not an oversight:
    # __init__ replaces both with fresh instances, and the only code that
    # *inserts* into them (_BridgeSessionHost.create_flow) requires a fully
    # constructed bridge with a running server. Every other access is a
    # ``.pop(sid, None)``, which is a no-op on an empty dict — so an
    # __init__-less instance can read and clean up but can never write through
    # to the shared default.
    _reply_sinks: Dict[str, Callable[[str], None]] = {}
    _remote_hub: Optional[Any] = None
    _remote_server: Optional[Any] = None
    _remote_server_error: str = ""
    _served_sessions: Dict[str, Any] = {}

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

        # Session-resume (docs/session_resume_design.md §6.4). Built once,
        # in the background, right after boot (see run()) — but a COLD
        # build (jieba dict + onnx model load + first-time embed of every
        # existing digest) measured ~10.8s at 92 sessions (2026-08-01), NOT
        # the "tens of milliseconds" a warm rebuild costs. A first message
        # can easily arrive before that finishes (measured: 2.3s after
        # boot in a real repro) — this is the bug _resume_index_ready
        # exists to close. Typed loosely (not importing
        # resume_index.ResumeIndex at module load) so fastembed/jieba only
        # load in the background build task, not on bridge startup.
        self._resume_index: Optional[Any] = None
        # Set (success OR failure — see _build_resume_index_background)
        # once the warm-build finishes. _search_resume_candidates awaits
        # this (bounded — see _RESUME_INDEX_WAIT_TIMEOUT) instead of
        # silently skipping when self._resume_index is still None, so a
        # message that lands during the cold-build window still gets
        # searched once the index is ready, rather than being invisibly
        # dropped from resume consideration for its entire session.
        self._resume_index_ready: asyncio.Event = asyncio.Event()
        # Pending offers, keyed by the TEMP session_id the offer was
        # attached to (the just-built parallel session — see module-level
        # _PendingResumeOffer docstring for why no timer task is needed).
        self._pending_resume_offers: Dict[str, _PendingResumeOffer] = {}

        # ── Direct control channel (docs/fleet_scheduling_design.md) ──────
        # Per-session override for where the coordinator's one-shot reply goes.
        # A local session has no entry and _on_coordinator_reply emits straight
        # to the renderer; a remote-controlled session installs a sink that also
        # puts the reply on the wire. See _on_coordinator_reply for why this one
        # message needs an indirection the other 20 don't.
        self._reply_sinks: Dict[str, Callable[[str], None]] = {}
        # 控制 role — paired targets, connections, per-tab bridges. Built lazily
        # on the first remote-control IPC so a user who never touches the feature
        # pays nothing (no registry file read, no keyring probe).
        self._remote_hub: Optional[Any] = None
        # 被控 role — the listener, when remote_control.serve is on.
        self._remote_server: Optional[Any] = None
        self._remote_server_error: str = ""
        # Sessions on THIS machine that are being driven from elsewhere: local
        # sid → the ``RemoteSession`` driving it. Populated by
        # ``_BridgeSessionHost.create_flow`` before the flow is built and
        # cleared by ``_purge_remote_session_state``. Read by
        # ``_emit_session``, which is what keeps a served session from ever
        # stamping an envelope the renderer would turn into a dead mirror tab.
        self._served_sessions: Dict[str, Any] = {}
        # v6 Connect panel role tracker — remembers whether the user last
        # picked "As Server" or "As Client", so a restart opens on the
        # right dashboard instead of the role-selection page. Loaded lazily
        # by _get_connect_state so the file read isn't on the boot path for
        # users who never open the panel.
        self._connect_state: Optional[Any] = None

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

    def _emit_session(self, envelope: Dict[str, Any], session_id: str) -> None:
        """Emit a session-stamped envelope, unless that session is one this
        machine is merely *serving* for a remote controller.

        The single gate for the被控 side's "no mirror tab" promise, and it has
        to be a gate rather than a per-call-site flag. ``renderer.js``'s status
        handler mounts a tab for ANY envelope carrying a session_id it has not
        seen, without looking at ``kind`` — so on a machine acting as a server,
        one stray stamped envelope for a ``rc-`` session is enough to spawn a
        tab that can never receive content, because everything real for that
        session goes over the wire through ``NetworkUIDelegate``.

        This has now been fixed twice at individual call sites and regressed
        both times, most recently via ``_clear_resume_offer``: the delegate swap
        in ``_BridgeSessionHost.create_flow`` only redirects events that travel
        through the ``InteractionManager``, and both ``_ensure_flow``'s own
        ``session_started`` and the resume-card envelopes are direct ``_emit``
        calls that bypass it entirely. Routing every session-stamped emit
        through here makes the invariant hold for paths not yet written, which
        is the only version of it that stays true.

        Unstamped (machine-level) envelopes do not belong here — they are
        broadcast by definition and go straight to ``_emit``.
        """
        if session_id in self._served_sessions:
            logger.debug(
                "suppressed %s envelope for served session %s (被控 role has "
                "no tab for it by design)",
                envelope.get("kind") or envelope.get("type"), session_id,
            )
            return
        _emit(envelope, session_id=session_id)

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

        if msg_type == "file_undo":
            # User-driven undo of a task's file changes (RewindStore, Tier-1.3).
            # Optional "item_id"; absent → undo the most recent item that
            # changed files. Routes into FlowControllerV2.undo_files, which
            # picks Case A (interrupt the in-flight item) vs Case B (revert +
            # faithful notice) and returns restored paths + any external-
            # modification conflicts for the renderer to surface.
            sid = self._resolve_session_id(msg)
            if sid is None or sid not in self._flows:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "file_undo: no such session", "fatal": False},
                      session_id=sid or self._session_id)
                return
            try:
                result = await self._flows[sid].undo_files(msg.get("item_id"))
                _emit({"type": "final", "id": msg_id, "result": result},
                      session_id=sid)
            except Exception as exc:
                logger.exception("file_undo failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"file_undo failed: {exc}", "fatal": False},
                      session_id=sid)
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
                    # The panel's only skill-list surface, and its "refresh"
                    # button is just this same call again — so reload() (a
                    # full re-scan of the Skill root) has to happen HERE, not
                    # behind a separate message the frontend doesn't send.
                    # Without it, a file added/removed on disk out-of-band
                    # (copying a new skill in, deleting one) never appears —
                    # the registry only reflects what init() saw at boot plus
                    # whatever the panel's own CRUD paths wrote in-memory.
                    def _reload_and_list():
                        reg.reload()
                        return reg.list_all()
                    skills = await asyncio.to_thread(_reload_and_list)
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
                is_first_message = sid not in self._flows
                # A remote-controlled tab runs entirely on the被控 machine,
                # which has its own key and its own history. Neither the local
                # API-key guard nor the local resume search applies to it.
                is_remote_session = (
                    self._remote_hub is not None
                    and self._remote_hub.is_remote(sid)
                )
                if is_first_message and not is_remote_session:
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

                # Session-resume soft offer (§6.4) — runs AFTER
                # _ensure_flow/start so the renderer's session is already
                # mounted when the resume_candidates envelope arrives (the
                # frontend's _showResumeCandidates looks up sessions.get(sid)
                # — if the session isn't mounted yet it silently drops the
                # event, causing the "card never appears" bug). The search
                # itself doesn't need the flow; only the envelope ordering
                # matters.
                await self._ensure_any_flow(sid, str(goal))
                flow = self._flows[sid]
                if not flow.started:
                    await flow.start()

                # § coordinator-and-resume-are-independent (2026-08-01):
                # resume search runs BEFORE on_user_message, in the SAME
                # coroutine — that ordering alone is what guarantees the
                # candidate-card envelope (on a hit) reaches the wire
                # before on_user_message's own thinking-bubble/reply
                # envelopes, so the user sees "candidates" and "coordinator
                # answer" as two distinct, correctly-ordered signals. There
                # is NO manual thinking_on/off here (that used to exist to
                # paper over a "message held, nothing is really happening"
                # state that no longer exists) — FlowControllerV2.on_user_message
                # natively brackets the whole INTENT+reply lifecycle with
                # notify_coordinator_thinking/clear_coordinator_thinking
                # (flow_controller.py), so the bubble now purely reflects
                # "is an LLM call actually in flight", independent of
                # whether resume happened to hit. The message ALWAYS runs —
                # a resume hit is a side-channel hint, never a gate on
                # whether this turn gets processed. Session-resume's own
                # gate lives in _on_coordinator_intent (fires once INTENT's
                # final lane is known) — it decides, DELAYED relative to
                # this call, whether to permanently stop searching.
                if is_first_message and not is_remote_session:
                    await self._refresh_resume_offer(
                        sid, str(goal), trigger_text=str(goal),
                    )

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
                    if flow is None or not flow.started:
                        logger.warning(
                            "user_input(message) sid=%s before flow started; ignoring",
                            sid,
                        )
                    else:
                        # § coordinator-and-resume-are-independent: every
                        # message keeps searching — not just the first —
                        # until the session's identity is settled via
                        # _on_coordinator_intent (a "queue" lane
                        # permanently disables it) or the "Not resuming"
                        # button (resume_disable_for_session IPC, same
                        # flag). The query is the WHOLE conversation so far
                        # + this message — see
                        # _resume_query_text_for_followup's docstring for
                        # why (a user often clarifies across several short
                        # messages). The message ALWAYS runs, unconditionally,
                        # right after — a resume hit is a side-channel hint
                        # shown to the user, never a gate on this turn.
                        if flow._ctx is not None and not flow._ctx.resume_search_disabled:
                            query_text = self._resume_query_text_for_followup(flow, text)
                            await self._refresh_resume_offer(
                                sid, query_text, trigger_text=text,
                            )
                        await flow.on_user_message(text)
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

        if msg_type in (
            "remote_control_status",
            "remote_pair",
            "remote_probe",
            "remote_forget",
            "remote_connect",
            "remote_disconnect",
            "remote_push_skills",
            "remote_bind",
            "remote_close_session",
            "remote_pair_linux",
            # v6 Connect Panel verbs — see docs/connect_v6_reference.md.
            # The new panel uses these; the older `remote_*` verbs above stay
            # for now so a mid-transition renderer isn't broken.
            "connect_start_server",
            "connect_stop_server",
            "connect_disconnect_client",
            "connect_close_session_server_side",
            "connect_release_target",
            "connect_exit_client",
        ):
            await self._handle_remote_control(msg_type, msg, msg_id)
            return

        if msg_type == "resume_confirm":
            # User clicked [Continue] on a resume_candidates offer. sid is
            # the TEMP session the offer was attached to; session_dir picks
            # which offered candidate to resume (an offer may carry any
            # number of candidates above the dense-cos gate — no fixed cap
            # — so the renderer must echo back which one; no implicit
            # "always pick #1").
            sid = self._resolve_session_id(msg)
            if sid is None:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "resume_confirm: missing session_id",
                       "fatal": False})
                return
            session_dir = str(msg.get("session_dir") or "")
            await self._do_resume_confirm(sid, session_dir, msg_id)
            return

        if msg_type == "resume_dismiss":
            # Bridge-side survivor of an earlier three-button design
            # (Continue / New Task / No Resume) — the frontend currently has
            # no button wired to this IPC type (the only button left is
            # "Not resuming" → resume_disable_for_session below). Kept
            # as pure bookkeeping in case a future UI wants a one-shot
            # "not this particular offer, but keep asking" affordance
            # distinct from the permanent opt-out: drop the pending offer
            # only. Does NOT disable future searches — the next message
            # searches again (§ continuous search) regardless.
            sid = self._resolve_session_id(msg)
            if sid is not None:
                self._pending_resume_offers.pop(sid, None)
            _emit({"type": "final", "id": msg_id, "result": {"ok": True}},
                  session_id=sid)
            return

        if msg_type == "resume_disable_for_session":
            # User clicked "Not resuming" — an explicit, permanent opt-out
            # for THIS session: no more resume searches will run for any
            # future message here, matching the permanent stop that a
            # successful resume_confirm (SessionContext.resume_search_disabled)
            # or a "queue"-lane INTENT classification (_on_coordinator_intent)
            # already cause. The message that triggered whatever offer is
            # showing was never held — it already ran, or is running — so
            # unlike the old hold-model there is nothing to "release"; this
            # is purely "stop asking", nothing else.
            sid = self._resolve_session_id(msg)
            if sid is None:
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": "resume_disable_for_session: missing session_id",
                       "fatal": False})
                return
            flow = self._flows.get(sid)
            if flow is not None and flow._ctx is not None:
                flow._ctx.resume_search_disabled = True
            self._pending_resume_offers.pop(sid, None)
            _emit({"type": "final", "id": msg_id, "result": {"ok": True}},
                  session_id=sid)
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

        models, _helper_models = resolve_models_and_helper(llm_cfg)
        if not models:
            models = ["anthropic::claude-4-5-haiku"]

        self._shared_services = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=10,
            )
            for m in models
        ]

        _helper_model_names = _helper_models or models
        self._shared_helper_services = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=10,
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

    # ------------------------------------------------------------------
    # Direct control channel — 被控 role (docs/fleet_scheduling_design.md)
    # ------------------------------------------------------------------

    async def _start_remote_control_server(self) -> bool:
        """Start listening for remote controllers. Idempotent.

        Returns True when a server is listening on return (including the case
        where one already was).

        There is no ``serve`` config flag and no automatic start. Listening for
        remote control is a deliberate, per-run act: the Connect panel's "As
        Server" button is the only caller, every press mints a fresh token, and
        nothing about being restarted or upgraded puts a machine into server
        mode. A persisted "serve: true" used to exist alongside this and start a
        listener at boot, which meant the panel and a settings checkbox were two
        answers to the same question — and the boot path minted a token the user
        was never shown. Config still says *how* to bind (``bind`` / ``port`` /
        ``max_sessions``), never *whether*.

        Failure is recorded and reported to the renderer rather than raised: a
        port that will not bind must not stop the machine from being used
        locally. ``_remote_server_error`` is surfaced by
        ``remote_control_status`` so the operator sees why the address panel is
        empty instead of wondering.
        """
        if self._remote_server is not None:
            return True
        try:
            from ..remote_control.serving import RemoteControlConfig
        except Exception:
            logger.exception("remote_control: package unavailable")
            return False

        rc_cfg = RemoteControlConfig.from_config(self._load_config_dict())

        try:
            from ..remote_control.server import RemoteControlServer

            host = _BridgeSessionHost(self)
            server = RemoteControlServer(
                token=rc_cfg.resolve_token(),
                host=host,
                server_name=platform.node(),
                max_sessions=rc_cfg.max_sessions,
            )
            port = await server.start(rc_cfg.bind, rc_cfg.port)
            self._remote_server = server
            self._remote_server_error = ""
            logger.info(
                "remote_control: serving on %s:%d (%d session slot(s))",
                rc_cfg.bind, port, rc_cfg.max_sessions,
            )
            return True
        except Exception as exc:
            self._remote_server_error = str(exc)
            logger.exception("remote_control: failed to start the server")
            return False

    async def _stop_remote_control_server(self) -> None:
        """Stop listening and end every session being driven from elsewhere.

        Deliberately destructive in one specific way, and it has to be: the
        remote sessions this machine is hosting cannot outlive their transport,
        so ``server.stop()`` tells each attached controller ``server_shutdown``
        and tears the sessions down. That is why the titlebar toggle asks for
        confirmation when sessions are live rather than just flipping.
        """
        server, self._remote_server = self._remote_server, None
        if server is None:
            return
        try:
            await asyncio.wait_for(server.stop(), timeout=8.0)
            logger.info("remote_control: stopped serving")
        except Exception:
            logger.warning("remote_control: server stop failed", exc_info=True)

    async def _connect_after_pair(self, hub: Any, target_id: str, label: str) -> None:
        """Bring a just-paired target's direct channel up immediately.

        ``hub.pair``/``hub.pair_linux_over_ssh`` only record the address in the
        registry — opening the socket is a separate act. Without this call a
        target paired mid-session would sit in ``state: "offline"`` until the
        operator clicked Connect, right after they watched the pairing succeed.
        Never fatal: a failed connect here still leaves the pairing recorded, and
        the panel shows it as offline with a log line instead of losing the
        pairing entirely.

        Connecting otherwise happens strictly on demand (the ``remote_connect``
        IPC type, sent when the client dashboard opens or the operator clicks
        Connect). There is deliberately no boot-time sweep: "connected" is not a
        durable property worth restoring — a被控 machine parks its sessions
        regardless, so nothing is lost by not holding a socket, and it serves one
        controller at a time, so holding one has a cost. The sweep also had to
        invent a whole ``restoring`` UI state to explain the 15s-per-machine window
        in which it misreported every not-yet-tried target as offline.
        """
        try:
            await hub.ensure_client(target_id)
        except Exception as exc:
            logger.info("connect: could not connect newly paired %s: %s",
                        target_id, exc)
            self._emit_connect_log(
                "pair", f"{label} 已配对，但连接失败: {exc}",
                "warn", "client",
            )

    def _remote_control_address_payload(self) -> Dict[str, Any]:
        """The被控-side "my control address" panel data.

        Includes the token — it is the whole point of the pairing string — so
        every path that logs this payload has to redact it. The renderer and
        Electron main both extend their redaction lists for ``pairing`` /
        ``token`` for exactly this reason.
        """
        from ..remote_control.address import format_address, local_endpoints

        server = self._remote_server
        if server is None:
            return {
                "serving": False,
                "error": self._remote_server_error,
                "endpoints": [],
                "pairing": "",
                "sessions": [],
            }

        endpoints = local_endpoints(server.port)
        primary = endpoints[0] if endpoints else f"127.0.0.1:{server.port}"
        host, _, port_text = primary.rpartition(":")
        sessions = [s.describe() for s in server.sessions()]
        # The one currently-attached controller's name (v6 spec: 1 client ↔ 1
        # server). "" when nobody is attached. Peek at the first live conn
        # — the server's own supersede logic guarantees at most one is
        # actually driving sessions.
        client_name = ""
        for conn in server._conns:
            if getattr(conn, "client_name", "") and not conn.closed:
                client_name = conn.client_name
                break
        return {
            "serving": True,
            "error": "",
            "port": server.port,
            "endpoints": endpoints,
            # Convenience fields for the v6 Connect panel — first LAN endpoint
            # and the raw token, each with its own "Copy" button. Both are
            # redacted in logs (`token` is in preload.js's redact set;
            # `pairing` too, and `endpoint` is not a secret).
            "endpoint": primary,
            "token": server.token,
            "pairing": format_address(
                host, int(port_text), server.token, platform.node()
            ),
            "server_name": platform.node(),
            "client_name": client_name,
            "sessions": sessions,
            # Convenience for the titlebar indicator, which only needs a count
            # and shouldn't have to know the shape of a session descriptor.
            "session_count": len(sessions),
            "attached_count": sum(1 for s in sessions if s.get("attached")),
        }

    def _broadcast_serve_state(self) -> None:
        """Push the被控 state so the titlebar indicator reflects reality.

        Unstamped (no session_id) because this is machine-level news, not
        session-level — see ``_emit``'s routing contract. Called whenever the
        listener starts/stops or a driven session appears/disappears, so the
        indicator never shows a stale count.
        """
        payload = self._remote_control_address_payload()
        _emit({
            "type": "status",
            "kind": "remote_serve_state",
            "serving": bool(payload.get("serving")),
            "session_count": int(payload.get("session_count") or 0),
            "attached_count": int(payload.get("attached_count") or 0),
            "port": payload.get("port") or 0,
            "error": payload.get("error") or "",
        })

    def _emit_connect_log(
        self, source: str, message: str, level: str = "info",
        role: str = "client",
    ) -> None:
        """Push one line into the Connect panel's Log area.

        The v6 design (§10) is explicit: connection-layer status (SSH
        bootstrap, deploy, version check, auto-reconnect attempts, pair
        errors) never appears in a chat bubble — it lands ONLY in the panel's
        Log area. This helper is the single sink; every site that would have
        gone through a system bubble now goes through here.

        ``source`` is a short slug for grouping ("ssh", "deploy", "pair",
        "reconnect", "release"), ``level`` is "info"/"warn"/"error", and
        ``role`` is "client" or "server" so the panel routes to the right
        log pane. Machine-level (unstamped) envelope — no session_id.
        """
        _emit({
            "type": "status",
            "kind": "connect_log",
            "source": str(source),
            "level": str(level),
            "role": str(role),
            "message": str(message),
        })

    # ------------------------------------------------------------------
    # Direct control channel — 控制 role
    # ------------------------------------------------------------------

    def _get_remote_hub(self) -> Any:
        """Lazily build the控制-side hub.

        Lazy so a user who never pairs anything never reads the registry file or
        probes the OS keyring.
        """
        if self._remote_hub is None:
            from ..remote_control.hub import RemoteControlHub

            self._remote_hub = RemoteControlHub(
                emit=lambda envelope, sid: _emit(envelope, session_id=sid),
                ui_factory=self._get_or_create_ui,
                client_name=f"handq-{platform.node()}",
                on_bridge_released=self._on_remote_bridge_released,
                on_log=lambda source, message, level: self._emit_connect_log(
                    source, message, level, "client"
                ),
            )
        return self._remote_hub

    def _on_remote_bridge_released(self, session_id: str) -> None:
        """The hub tore a bridge down on its own initiative (explicit Disconnect).

        The tab-close path already drops these slots itself, but a release comes
        from the panel, not from the tab — so without this the ``_flows`` slot
        keeps a destroyed bridge. ``_ensure_any_flow`` returns early for any
        occupied slot, so the tab stayed on screen accepting messages that the
        closed bridge silently discarded.
        """
        self._flows.pop(session_id, None)
        with self._uis_lock:
            self._uis.pop(session_id, None)
        self._reply_sinks.pop(session_id, None)
        self._session_dispatch_locks.pop(session_id, None)
        self._inflight_by_sid.pop(session_id, None)

    async def _refresh_remote_sessions(self, hub: Any) -> None:
        """Re-ask every connected target for its live session list, in parallel.

        Sequential would make the panel's open latency the *sum* of every paired
        machine's round trip; concurrently it is the slowest one, bounded by
        ``hub.SESSION_REFRESH_TIMEOUT``. Exceptions are absorbed per target —
        one unreachable machine must not blank the whole panel.
        """
        target_ids = [
            str(t.get("target_id") or "")
            for t in (hub.list_targets() or [])
            if isinstance(t, dict) and t.get("state") == "connected"
        ]
        if not target_ids:
            return
        await asyncio.gather(
            *(hub.refresh_sessions(tid) for tid in target_ids if tid),
            return_exceptions=True,
        )

    def _get_connect_state(self) -> Any:
        """Lazily load the last-picked Connect role from disk. Same laziness
        rationale as :meth:`_get_remote_hub` — a user who never opens the
        Connect panel never pays for a file read on boot.
        """
        if self._connect_state is None:
            from ..remote_control.connect_state import ConnectState

            self._connect_state = ConnectState.load()
        return self._connect_state

    async def _handle_remote_control(
        self, msg_type: str, msg: Dict[str, Any], msg_id: Optional[str],
    ) -> None:
        """Every remote-control IPC type.

        Grouped into one handler because they share the error contract: every
        failure comes back as a ``final`` with ``{ok: False, error}`` rather than
        an ``error`` envelope, so the renderer's RPC helper can settle its
        promise and show the message inline in the pairing dialog. An ``error``
        envelope would reject the promise and lose the structured detail.
        """
        from ..remote_control.address import AddressError
        from ..remote_control.client import RemoteControlError
        from ..remote_control.linux_bootstrap import LinuxDaemonBusyError

        def _reply(result: Dict[str, Any]) -> None:
            _emit({"type": "final", "id": msg_id, "result": result},
                  session_id=self._session_id)

        try:
            if msg_type == "remote_control_status":
                hub = self._get_remote_hub()
                # Re-ask each connected server what sessions it actually has
                # before answering. The panel calls this on open and after every
                # action, so it is the natural reconcile point: a chip for a
                # session the other operator closed disappears here, and a
                # session sitting on a parked confirmation gets its badge. Never
                # fatal — refresh_sessions swallows a timeout and leaves the last
                # known chips in place (see its docstring on why an unreachable
                # machine must not look like an empty one).
                await self._refresh_remote_sessions(hub)
                _reply({
                    "ok": True,
                    "serving": self._remote_control_address_payload(),
                    "targets": hub.list_targets(),
                    # v6: which role the user last picked, so the Connect
                    # panel can open on that dashboard instead of the
                    # role-selection page.
                    "role": self._get_connect_state().role,
                })
                return

            if msg_type == "remote_probe":
                hub = self._get_remote_hub()
                _reply({"ok": True, **await hub.probe(str(msg.get("pairing") or ""))})
                return

            if msg_type == "remote_pair":
                hub = self._get_remote_hub()
                pairing_name = str(msg.get("name") or "")
                self._emit_connect_log(
                    "pair",
                    f"手动配对 {pairing_name or '(new)'} …",
                    "info", "client",
                )
                target = hub.pair(
                    str(msg.get("pairing") or ""), name=pairing_name
                )
                self._get_connect_state().set_role("client")
                self._emit_connect_log(
                    "pair",
                    f"已配对 {target.name or target.host}",
                    "info", "client",
                )
                await self._connect_after_pair(
                    hub, target.target_id, target.name or target.host
                )
                _reply({
                    "ok": True,
                    "target": target.to_public_dict(),
                    "targets": hub.list_targets(),
                })
                return

            if msg_type == "remote_forget":
                hub = self._get_remote_hub()
                target = hub.registry.get(str(msg.get("target_id") or ""))
                removed = hub.forget(str(msg.get("target_id") or ""))
                if removed and target is not None:
                    self._emit_connect_log(
                        "pair",
                        f"已忘记 {target.name or target.host}",
                        "info", "client",
                    )
                _reply({"ok": removed, "targets": hub.list_targets()})
                return

            if msg_type == "remote_pair_linux":
                # Linux only: fetch the address over the SSH channel the deploy
                # path already uses instead of asking the operator to walk to the
                # other machine. Reuses remote_handq_tool's install/start
                # helpers; that tool itself is untouched.
                hub = self._get_remote_hub()
                sid = self._resolve_session_id(msg)
                # Credential prompts (first-time SSH password) surface through
                # whichever session asked, so they land in a real chat tab.
                im = None
                flow = self._flows.get(sid) if sid else None
                if flow is not None:
                    im = getattr(flow, "interaction_manager", None)
                ssh_target = str(msg.get("ssh_target") or "")
                self._emit_connect_log(
                    "ssh", f"引导 Linux 目标: {ssh_target} …",
                    "info", "client",
                )
                try:
                    target = await hub.pair_linux_over_ssh(
                        ssh_target=ssh_target,
                        credentials_file=str(msg.get("credentials_file") or ""),
                        name=str(msg.get("name") or ""),
                        install=msg.get("install") is not False,
                        interaction_manager=im,
                        force=bool(msg.get("force")),
                        # Upgrade-decision lines (share scan, versions, deploy /
                        # defer) land in the panel's client log so "why didn't it
                        # upgrade?" is answerable without SSHing in.
                        on_log=lambda line: self._emit_connect_log(
                            "upgrade", line, "info", "client",
                        ),
                    )
                except Exception as exc:
                    self._emit_connect_log(
                        "ssh", f"{ssh_target} 引导失败: {exc}",
                        "error", "client",
                    )
                    raise
                pub = target.to_public_dict()
                pending = pub.get("upgrade_pending") or {}
                if pending:
                    self._emit_connect_log(
                        "upgrade",
                        f"{target.name or target.host}: 已连接当前版本 "
                        f"{pending.get('from') or '未知'}，新版本 "
                        f"{pending.get('to') or '?'} 待安装（有会话在运行，"
                        f"结束后可从面板升级）",
                        "warn", "client",
                    )
                self._emit_connect_log(
                    "ssh",
                    f"{ssh_target} 引导完成 (监听 {target.host}:{target.port})",
                    "info", "client",
                )
                self._get_connect_state().set_role("client")
                await self._connect_after_pair(
                    hub, target.target_id, target.name or target.host
                )
                _reply({
                    "ok": True,
                    "target": pub,
                    "targets": hub.list_targets(),
                })
                return

            if msg_type == "remote_connect":
                hub = self._get_remote_hub()
                client = await hub.ensure_client(str(msg.get("target_id") or ""))
                # Explicit connect from the panel = user is being a client now.
                self._get_connect_state().set_role("client")
                _reply({
                    "ok": True,
                    "server_name": client.server_name,
                    "platform": client.server_platform,
                    "sessions": client.remote_sessions,
                    "targets": hub.list_targets(),
                })
                return

            if msg_type == "remote_disconnect":
                # "断开连接" — a passive close, and the ordinary way to stop using
                # a machine. Drops the socket and nothing else: sessions park on
                # the被控 side, the pairing and its session records stay,
                # reconnecting resumes where we left off. Also how you hand the
                # machine back, since it serves one controller at a time.
                #
                # The destructive counterpart is `connect_release_target`, which
                # the panel confirms first.
                hub = self._get_remote_hub()
                target_id = str(msg.get("target_id") or "")
                await hub.close_client(target_id)
                self._emit_connect_log(
                    "disconnect",
                    f"已断开与 {target_id} 的连接（远端会话保持运行）",
                    "info", "client",
                )
                _reply({"ok": True, "targets": hub.list_targets()})
                return

            if msg_type == "remote_push_skills":
                hub = self._get_remote_hub()
                target_id = str(msg.get("target_id") or "")
                names = [str(n) for n in (msg.get("names") or []) if str(n)]
                results = await hub.push_skills_to(target_id, names)
                _reply({"ok": True, "results": results})
                return

            if msg_type == "remote_bind":
                # Renderer declares "this tab is backed by that machine", before
                # its first `request`. _ensure_any_flow reads this.
                sid = self._resolve_session_id(msg)
                if sid is None:
                    _reply({"ok": False, "error": "remote_bind: missing session_id"})
                    return
                hub = self._get_remote_hub()
                remote_sid = str(msg.get("remote_session_id") or "")
                hub.bind(
                    sid,
                    str(msg.get("target_id") or ""),
                    remote_session_id=remote_sid,
                    capability=str(msg.get("capability") or ""),
                    since_seq=int(msg.get("since_seq") or 0),
                )
                # v6 fix: when re-adopting an existing session (remote_session_id
                # is non-empty), create the bridge NOW and start it so the event
                # replay arrives immediately — the user should not have to send a
                # message first to see the remote session's content.
                if remote_sid:
                    try:
                        self._get_or_create_ui(sid)
                        bridge = await hub.create_bridge(sid)
                        self._flows[sid] = bridge
                        await bridge.start()
                    except Exception as exc:
                        # If bridge creation fails (server unreachable, stale
                        # capability, etc.), roll back: remove the binding so
                        # the tab doesn't pretend to be remote when it isn't.
                        hub._bindings.pop(sid, None)
                        hub._bridges.pop(sid, None)
                        self._flows.pop(sid, None)
                        logger.warning(
                            "remote_control: remote_bind adopt failed for %s: %s",
                            sid, exc)
                        _reply({"ok": False,
                                "error": f"无法恢复远端会话: {exc}"})
                        return
                _reply({"ok": True, "session_id": sid})
                return

            if msg_type == "remote_close_session":
                # Distinct from close_session: this kills the被控-side session
                # too. Closing the tab alone only detaches (see
                # RemoteSessionBridge.destroy) — that asymmetry is the "remote
                # machine behaves like a server" property.
                #
                # Two entry shapes: a local tab (session_id present, has an open
                # bridge) OR a panel chip (target_id + remote_session_id, no
                # open tab). The chip path is how you terminate a session you
                # left running yesterday without having to re-open it first.
                remote_sid = str(msg.get("remote_session_id") or "")
                target_id = str(msg.get("target_id") or "")
                if remote_sid and target_id:
                    hub = self._get_remote_hub()
                    await hub.close_remote_session_by_id(
                        target_id, remote_sid, force=bool(msg.get("force"))
                    )
                    _reply({"ok": True, "targets": hub.list_targets()})
                    return
                sid = self._resolve_session_id(msg)
                if sid is None:
                    _reply({"ok": False, "error": "missing session_id"})
                    return
                hub = self._get_remote_hub()
                await hub.close_remote_session(sid)
                self._flows.pop(sid, None)
                _reply({"ok": True})
                return

            # ── v6 Connect Panel verbs ──────────────────────────────────────

            if msg_type == "connect_start_server":
                # As Server button clicked in the Connect panel. Idempotent:
                # returns the current address if already listening.
                started = await self._start_remote_control_server()
                if not started:
                    err = (self._remote_server_error
                           or "无法开始监听（详见 handq-bridge.log）")
                    self._emit_connect_log("start", err, "error", "server")
                    _reply({"ok": False, "error": err})
                    return
                self._broadcast_serve_state()
                self._get_connect_state().set_role("server")
                payload = self._remote_control_address_payload()
                self._emit_connect_log(
                    "start",
                    f"正在监听 {payload.get('endpoint') or ''}",
                    "info", "server",
                )
                _reply({"ok": True, "serving": payload})
                return

            if msg_type == "connect_stop_server":
                # Exit Server button in the Connect panel — stops listening and
                # takes this machine out of server mode entirely. Any active
                # sessions are destroyed in the process (see server.stop).
                await self._stop_remote_control_server()
                self._broadcast_serve_state()
                # Only clear role if we WERE the server. A machine can be both
                # a server (this) AND a client (some target); we don't want
                # stopping the server to also forget "user was doing client
                # things" if that's actually where they are.
                cs = self._get_connect_state()
                if cs.role == "server":
                    cs.set_role(None)
                self._emit_connect_log("stop", "server 已停止", "info", "server")
                _reply({"ok": True})
                return

            if msg_type == "connect_disconnect_client":
                # "Disconnect Client" on the As Server dashboard: drops the
                # controller + destroys its sessions, keeps listening.
                server = getattr(self, "_remote_server", None)
                if server is None:
                    _reply({"ok": False, "error": "not currently serving"})
                    return
                destroyed = await server.disconnect_client()
                self._broadcast_serve_state()
                self._emit_connect_log(
                    "disconnect",
                    f"已断开 client，销毁了 {destroyed} 个 session",
                    "info", "server",
                )
                _reply({"ok": True, "destroyed": destroyed})
                return

            if msg_type == "connect_close_session_server_side":
                # "Close" button on a session row in the As Server dashboard.
                sid = str(msg.get("session_id") or "")
                if not sid:
                    _reply({"ok": False, "error": "missing session_id"})
                    return
                server = getattr(self, "_remote_server", None)
                if server is None:
                    _reply({"ok": False, "error": "not currently serving"})
                    return
                ok = await server.close_session_by_id(sid)
                self._broadcast_serve_state()
                self._emit_connect_log(
                    "session",
                    f"session {sid} {'已销毁' if ok else '关闭失败'}",
                    "info" if ok else "warn", "server",
                )
                _reply({"ok": ok})
                return

            if msg_type == "connect_release_target":
                # The ONE destructive action a client can take against a server:
                # destroy every session it was running for us and end the serving
                # relationship. On Linux the daemon exits and the pairing is
                # forgotten; elsewhere the machine keeps listening and the pairing
                # is kept (see hub.release_target). Non-destructive "stop using it
                # for now" is `remote_disconnect`.
                hub = self._get_remote_hub()
                target_id = str(msg.get("target_id") or "")
                outcome = await hub.release_target(target_id)
                self._emit_connect_log(
                    "release",
                    f"已结束与 {target_id} 的服务关系"
                    + ("（配对已移除）" if outcome.get("forgot") else "（配对保留）"),
                    "info", "client",
                )
                if outcome.get("warning"):
                    self._emit_connect_log(
                        "release", str(outcome["warning"]), "warn", "client",
                    )
                _reply({
                    "ok": True,
                    "confirmed": bool(outcome.get("confirmed")),
                    "forgot": bool(outcome.get("forgot")),
                    "warning": str(outcome.get("warning") or ""),
                    "targets": hub.list_targets(),
                })
                return

            if msg_type == "connect_exit_client":
                # "Exit Client Mode" — LOCAL only. Disconnects every socket;
                # nothing on any remote machine is destroyed and no pairing is
                # dropped. It used to release every connected target, which made
                # leaving client mode reach across the network and tear down every
                # machine in the list.
                hub = self._get_remote_hub()
                await hub.exit_client()
                cs = self._get_connect_state()
                if cs.role == "client":
                    cs.set_role(None)
                self._emit_connect_log(
                    "disconnect",
                    "已退出 client 模式：所有连接已断开，远端会话保持运行",
                    "info", "client",
                )
                _reply({"ok": True, "targets": hub.list_targets()})
                return

        except AddressError as exc:
            _reply({"ok": False, "error": f"配对地址无法解析: {exc}"})
            return
        except LinuxDaemonBusyError as exc:
            # Distinct kind, not just distinct text: this is the ONE failure
            # where retrying with a different argument (force=True) is a
            # legitimate next step rather than "something is broken" — the
            # renderer's pairing dialog checks this flag to offer a
            # "强制重启" retry button instead of just showing the error.
            _reply({"ok": False, "error": str(exc), "busy": True})
            return
        except RemoteControlError as exc:
            _reply({"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            logger.exception("remote_control: %s failed", msg_type)
            _reply({"ok": False, "error": f"{msg_type} 失败: {exc}"})
            return

    # ------------------------------------------------------------------
    # Session resume (docs/session_resume_design.md §6.3/§6.4)
    # ------------------------------------------------------------------

    async def _build_resume_index_background(self) -> None:
        """Warm-build the ResumeIndex once at boot, off the message-dispatch
        path. fastembed/jieba import + model load happens here (not at
        module import time — see the loose ``Optional[Any]`` typing on
        ``self._resume_index``), so a slow first-ever model load never
        delays the bridge announcing itself alive. But it CAN delay a
        user's actual first message now — see ``_resume_index_ready``:
        the cold build (model load + first-time embed of every existing
        digest) measured ~10.8s at 92 sessions, and a first message can
        land well before that (measured 2.3s after boot in a real repro).
        ``_search_resume_candidates`` bounded-waits on ``_resume_index_ready``
        rather than silently skipping, so that message still gets searched.

        This is warm-up, not the only build: ``_search_resume_candidates``
        rebuilds the index (cheap once the model is warm AND the embedding
        cache is populated — see resume_index.py's ``_embed_cache``) right
        before every search, so a session destroyed earlier in the SAME
        bridge process is visible to resume without needing a restart.

        ``_resume_index_ready`` is set on BOTH the success and failure path
        below — a permanently-failed warm-build must still release anyone
        waiting on it (they then see ``self._resume_index is None`` and
        fail open), never leave them blocked until the bounded-wait timeout.
        """
        try:
            from src.controller_v2.resume_index import ResumeIndex

            index = ResumeIndex()
            t0 = time.monotonic()
            await index.build()
            self._resume_index = index
            logger.info(
                "resume index warm-built: %d session(s) in %.2fs",
                index.size, time.monotonic() - t0,
            )
        except Exception:
            logger.exception(
                "resume index warm-build failed; resume offers disabled "
                "this session (fails open — no offers, no crash)",
            )
        finally:
            self._resume_index_ready.set()

    async def _search_resume_candidates(self, query_text: str) -> List["ResumeCandidate"]:
        """Best-effort resume search for a session's message.

        ``query_text`` is the FULL text to search with — for the session's
        first message this is just that message; for every later message
        (see ``_resume_query_text_for_followup``) it's the whole
        conversation so far joined together, so a user who clarifies across
        several short messages ("你还记得翻译任务吗" → "QPM" → "翻译") gets
        a query that accumulates all three, not just whichever one happened
        to land first. Search itself (dense-cos + BM25) is unchanged —
        only what text callers feed it differs.

        Rebuilds the index right before searching — with the embedding
        cache (see resume_index.py's ``_embed_cache``), a rebuild only
        re-embeds new/changed digests, so this is cheap (~100-200ms at
        ~80 sessions, measured 2026-08-01) even though it runs on every
        message. Without the rebuild, a session destroyed earlier in the
        SAME bridge process is invisible to resume until the bridge
        restarts — confirmed live: index built once at boot
        (§_build_resume_index_background), a session destroyed 8 hours
        later within that same process never entered it. Logs build/search
        timing at INFO so a live "why did this feel slow" report can be
        read straight from handq-engine.log without flipping log_level to
        DEBUG first.

        Returns [] (never raises) when the query is empty, the warm-build
        never finishes within ``_RESUME_INDEX_WAIT_TIMEOUT`` (build failed
        or is pathologically slow), or the search itself fails — a missing
        resume offer is indistinguishable from "no candidates found" by
        design; this is a soft enhancement, never a hard dependency for a
        new session to start. Does NOT return [] just because the warm-build
        hasn't finished YET — see the ``_resume_index_ready`` wait below;
        that used to be a silent, unlogged miss for any message landing in
        the ~10s cold-build window (confirmed live 2026-08-01: a user's
        first message 2.3s after bridge boot got no resume search at all).
        """
        if not query_text:
            return []
        if self._resume_index is None:
            try:
                await asyncio.wait_for(
                    self._resume_index_ready.wait(),
                    timeout=_RESUME_INDEX_WAIT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "resume search: warm-build still not ready after %.0fs; "
                    "giving up on this message's resume search (fails open)",
                    _RESUME_INDEX_WAIT_TIMEOUT,
                )
                return []
            if self._resume_index is None:
                # Event was set on the FAILURE path (see
                # _build_resume_index_background's finally) — warm-build
                # ran and errored, not "still running". Nothing to wait
                # for; fail open.
                return []
        t0 = time.monotonic()
        try:
            await self._resume_index.build()
            t_build = time.monotonic()
            results = await self._resume_index.search(query_text)
            t_search = time.monotonic()
            logger.info(
                "resume search: build=%.0fms search=%.0fms total=%.0fms "
                "hits=%d index_size=%d",
                (t_build - t0) * 1000, (t_search - t_build) * 1000,
                (t_search - t0) * 1000, len(results), self._resume_index.size,
            )
            return results
        except Exception:
            logger.exception("resume search failed; continuing without an offer")
            return []

    @staticmethod
    def _resume_query_text_for_followup(
        flow: "FlowControllerV2", latest_text: str,
        prior_messages: Optional[List[str]] = None,
    ) -> str:
        """Build the resume search query for a NON-first message: everything
        the user has said so far, verbatim, followed by the message that just
        arrived.

        Why the full history and not just latest_text: a user who
        clarifies across several short messages ("你还记得翻译任务吗" →
        "QPM" → "翻译") wants each new message to sharpen the search, not
        replace it — searching only the latest turn throws away everything
        earlier turns already established. Plain string join, not a
        digest/summary — this is a search query, not something a user or
        model ever reads back.

        Two sources of "so far", depending on whether the session is HELD:
          * Held (fresh session, nothing executed yet) → the caller passes
            ``prior_messages`` = the accumulated held messages, because those
            were NEVER fed to on_user_message and so are absent from
            conversation_history. Without this the follow-up search would see
            only the latest line and lose the accumulation the hold exists to
            build.
          * Not held (a running session's FYI search) → prior_messages is
            None; fall back to the flow's conversation_history (the executed
            turns).
        """
        if prior_messages is not None:
            parts = [f"user: {m}" for m in prior_messages]
        else:
            history = (
                flow._orchestrator.conversation_history
                if flow._orchestrator is not None else []
            )
            parts = [f"{turn.get('role', '')}: {turn.get('content', '')}" for turn in history]
        parts.append(f"user: {latest_text}")
        return "\n".join(parts)

    def _emit_resume_offer(
        self, temp_sid: str, goal: str, candidates: List["ResumeCandidate"],
        trigger_text: str = "",
    ) -> None:
        """Register the offer + emit the soft-prompt envelope.

        ``kind=resume_candidates`` is intentionally NOT routed through
        ``_StdioUI._pending`` (the hard-block confirmation Future
        mechanism) — per design, ignoring this envelope (typing a new
        message, or just doing nothing) must have zero effect, which a
        blocking Future can't express without extra machinery to cancel
        it out from under an in-flight await.

        The envelope still carries a ``hold_seconds`` field for wire
        compatibility with the renderer's existing (dormant) countdown
        code, but it is always ``None`` now — messages are never held
        awaiting a resume decision (§ coordinator-and-resume-are-independent,
        2026-08-01); this is a pure FYI card every time.

        ``trigger_text``: the user's own words that produced this offer
        (clean, NOT the role-prefixed search query) — the renderer quotes
        it in the panel title so the user knows which message the
        candidates are a response to.
        """
        self._pending_resume_offers[temp_sid] = _PendingResumeOffer(
            candidates=candidates,
            expires_at=time.monotonic() + _RESUME_OFFER_TTL_SECONDS,
            goal=goal,
        )
        self._emit_session({
            "type": "status",
            "kind": "resume_candidates",
            "candidates": [
                {
                    "session_dir": str(c.session_dir),
                    "title": c.digest.title,
                    "updated_at": c.digest.updated_at,
                    "status": c.digest.status,
                    # Completion signal for the card (§6.4.1's "已完成 vs
                    # 中断于第N步"): whether ANY work was left in the queue
                    # at close time — NOT the digest's top-level
                    # destroyed/crashed status, which only reflects HOW the
                    # session ended, not whether its task was finished.
                    "is_fully_done": (
                        c.digest.current is None and not c.digest.pending
                    ),
                    "final_answer": (
                        c.digest.completed[-1].get("final_answer", "")
                        if c.digest.completed else ""
                    ),
                    "workspace_files": c.digest.workspace_files,
                }
                for c in candidates
            ],
            "ttl_seconds": _RESUME_OFFER_TTL_SECONDS,
            "hold_seconds": None,
            "trigger_text": trigger_text,
        }, temp_sid)

    def _clear_resume_offer(self, temp_sid: str) -> None:
        """Drop any pending offer for temp_sid and tell the renderer to hide
        the card — the counterpart to _emit_resume_offer for the
        now-continuous search (§ persistent resume search): a later message
        in the same session can search from a stronger position (more
        conversation accumulated) and no longer clear the gate that an
        earlier message cleared, so a stale card must not linger. No-op
        (still emits) even if there was nothing pending — idempotent from
        the renderer's point of view (hiding an already-hidden card is
        harmless), so callers don't need to track whether a card is
        currently showing. Also the withdrawal mechanism
        ``_on_coordinator_intent`` uses once INTENT settles a turn on the
        "queue" lane — a card left showing after that would be stale.

        That last caller is how this method became the被控 side's dead-mirror-tab
        bug: ``_ensure_flow`` wires ``on_intent_classified`` for every session
        including a remotely-driven one, so the first real task on a served
        session reached here and emitted a ``rc-`` stamped envelope, which the
        renderer turned into a tab that could never receive anything. The emit
        now goes through ``_emit_session``, so for a served session this whole
        method is inert.
        """
        self._pending_resume_offers.pop(temp_sid, None)
        self._emit_session({
            "type": "status",
            "kind": "resume_candidates",
            "candidates": [],
            "ttl_seconds": _RESUME_OFFER_TTL_SECONDS,
            "hold_seconds": None,
        }, temp_sid)

    async def _refresh_resume_offer(
        self, sid: str, query_text: str, trigger_text: str = "",
    ) -> bool:
        """Search + emit-or-clear in one call — the shared step run before
        BOTH a session's first message (``request``) and every later
        message (``user_input``, kind=message). Centralized here so both
        call sites emit/clear the same way instead of duplicating the
        "candidates truthy?" branch.

        Returns True iff the search HIT (an offer was emitted), False
        otherwise (the card was cleared) — callers currently don't act on
        this return value (the message runs unconditionally either way;
        see the request/user_input branches), but it's kept for callers
        that want to know without re-deriving it from side effects.

        ``trigger_text`` is the user's clean words (not the role-prefixed
        ``query_text``) shown in the panel title; defaults to query_text
        when the caller doesn't distinguish them.
        """
        candidates = await self._search_resume_candidates(query_text)
        if candidates:
            self._emit_resume_offer(
                sid, query_text, candidates,
                trigger_text=trigger_text or query_text,
            )
            return True
        self._clear_resume_offer(sid)
        return False

    def _pop_valid_resume_offer(self, temp_sid: str) -> Optional[_PendingResumeOffer]:
        """Pop and return the offer for temp_sid iff it hasn't expired.
        Lazy-expiry check (see module-level docstring) — a stale entry is
        just discarded here rather than proactively swept by a timer."""
        offer = self._pending_resume_offers.pop(temp_sid, None)
        if offer is None:
            return None
        if time.monotonic() > offer.expires_at:
            return None
        return offer

    async def _ensure_any_flow(self, session_id: str, goal: str) -> None:
        """:meth:`_ensure_flow` plus the remote-session branch.

        A tab bound to a remote target gets a ``RemoteSessionBridge`` in the
        ``_flows`` slot instead of a ``FlowControllerV2``. Everything downstream —
        ``flow.started``, ``flow.start()``, ``flow.on_user_message()``,
        ``flow.destroy()``, ``flow._ctx.resume_search_disabled`` — is duck-typed
        by that class, so the ``request`` / ``user_input`` / ``close_session``
        handlers need no branch of their own. None of ``_ensure_flow``'s setup
        applies either (session dir, per-session engine log, LLM service pool):
        the被控 machine owns all of it.

        Separate from ``_ensure_flow`` because building the bridge has to await a
        TCP connect, and ``_ensure_flow`` is sync and called from two other
        (always-local) places.
        """
        if session_id in self._flows:
            return
        hub = self._remote_hub
        if hub is not None and hub.is_remote(session_id):
            # The local _StdioUI still exists and is still the render target —
            # the bridge replays remote events onto it, stamped with this sid.
            self._get_or_create_ui(session_id)
            self._flows[session_id] = await hub.create_bridge(session_id)
            return
        self._ensure_flow(session_id, goal=goal)

    def _ensure_flow(
        self, session_id: str, goal: str,
        resume_session_dir: Optional[Path] = None,
    ) -> None:
        if session_id in self._flows:
            return

        # Make sure the UI delegate exists for this sid before we wire
        # anything up. All status events for this session route through it.
        ui = self._get_or_create_ui(session_id)

        cm = ConfigManager(str(self.config_path))
        cfg = cm.get_config()
        llm_cfg = cfg.get("llm", {}) or {}
        sess_cfg = cfg.get("session", {}) or {}

        workspace_subdir = sess_cfg.get("workspace_base", ".workspace") or ".workspace"
        if resume_session_dir is not None:
            # Resume path (§6.5 "复用既有目录"): the workspace already
            # exists from the original session — re-point here instead of
            # allocating a fresh timestamped dir, so ① world state (the
            # agent's files) and the digest's digest.json stay
            # exactly where the user's earlier session left them.
            session_dir = resume_session_dir
            agent_workspace = session_dir / workspace_subdir
            agent_workspace.mkdir(exist_ok=True)
        else:
            # Allocate this session's directory under %USERPROFILE%\HandQ\History\.
            # The agent operates inside <session>/<workspace_subdir>/ — that's the
            # ONLY path the agent's prompt knows about. The session root itself
            # holds framework metadata (handq-engine.log, executions_logs/) and is
            # never named in the system prompt. A leading-dot folder name from
            # yaml (default ".workspace") just becomes a normal subdir on NTFS.
            session_dir = _allocate_session_dir(goal, workspace_subdir=workspace_subdir)
            agent_workspace = session_dir / workspace_subdir

        # Initialise the HandQ engine logger now that we know the session dir
        # and can read log_level from config.  Must happen before FlowController
        # construction so every get_logger() call in src/ picks up the right
        # level and file handler.
        #
        # Per-task engine log lives INSIDE the session dir (not the bridge's
        # launch-scoped log dir) so the user can find a session's full trace
        # alongside its digest.json + executions_logs/ without having to
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

        # ``on_intent_classified`` callback — session-resume's gate (see
        # _on_coordinator_intent's docstring). Same closure shape as
        # _on_reply above: both close over this ``session_id`` local so the
        # bound method knows which session's state to touch.
        def _on_intent(intent: str) -> None:
            self._on_coordinator_intent(session_id, intent)

        flow = FlowControllerV2(
            llm_services=consolidated_services,
            working_directory=str(agent_workspace),
            storage_directory=str(session_dir),
            config_path=str(self.config_path),
            on_reply_to_user=_on_reply,
            on_intent_classified=_on_intent,
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
        #
        # Routed through _emit_session, not _emit: for a session this machine is
        # serving for a remote controller there is no local tab, and this
        # envelope's sid would be enough to make the renderer invent one (its
        # status handler mounts on any unseen sid, ignoring ``kind``). That gate
        # replaced a ``suppress_started_event`` parameter here — the flag was
        # correct for this one call and did nothing for the next path that
        # emitted a stamped envelope. See _emit_session.
        try:
            self._emit_session({
                "type": "status",
                "kind": "session_started",
                "session_dir": str(session_dir),
                "workspace_dir": str(agent_workspace),
                "session_name": goal.strip()[:30] if goal else None,
            }, session_id)
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
        land in the correct chat tab.

        Routed through a per-session sink rather than emitting inline: a
        remote-controlled session replaces its sink so this reply reaches the
        controlling machine too. It has to be intercepted *here* because the
        callback deliberately bypasses the InteractionManager
        (``flow_controller.py:533-536`` prefers the callback over the IM), and
        this is the only message that does — so it is also the only one a
        delegate swap alone would fail to redirect.
        """
        if not text:
            return
        sink = self._reply_sinks.get(session_id)
        if sink is not None:
            try:
                sink(str(text))
                return
            except Exception:
                logger.warning(
                    "remote_control: reply sink for %s failed; falling back to "
                    "the local emit", session_id, exc_info=True,
                )
        self._emit_session(
            {"type": "status", "kind": "reply", "text": str(text)}, session_id
        )

    def _on_coordinator_intent(self, session_id: str, intent: str) -> None:
        """``FlowControllerV2.on_intent_classified`` callback — fires the
        instant Orchestrator settles this turn's FINAL intent lane
        (chat/queue/interrupt), independent of whether/when
        ``on_user_message`` returns. This is session-resume's gate:

          * "queue" (a real task starting) → the user's intent is now
            unambiguous: settle this session's identity as "not resuming",
            permanently (mirrors the other two permanent-stop paths — a
            successful resume_confirm and the "Not resuming" button; see
            SessionContext.resume_search_disabled's docstring) and
            withdraw whatever candidate card is currently showing — it's
            now stale, the user has moved on to a real task.
          * "chat" → deliberately inert. Ordinary conversation doesn't
            settle the session's identity; resume search keeps running on
            every subsequent message (§ continuous search) and the card
            stays live/refreshable.
          * "interrupt" → also deliberately inert (user-confirmed): a
            control command ("stop", "cancel") is not "the user has
            started a real task" — it doesn't carry the same
            identity-settling weight "queue" does, so it must NOT close
            the resume gate.

        idempotent: if resume_search_disabled is already True (settled by
        an earlier turn via any path), this is a no-op — no redundant
        clear-offer emit on every subsequent queue-lane turn.
        """
        if intent != "queue":
            return

        # v6: this session_id might ALSO be a remote-driven session (the被控
        # side uses the same self._flows dict, keyed by the same rc-xxx id —
        # see _BridgeSessionHost.create_flow). If so, record that it stopped
        # being idle chat, so the controller's panel can badge its chip as a
        # task. No-op for an ordinary local session: self._remote_server is
        # None until the user has ever clicked "As Server".
        #
        # FIRST, before the resume-gate early-returns below. It used to sit
        # after them and worked only because the very first "queue" turn is
        # necessarily also the turn that flips resume_search_disabled — an
        # ordering coincidence, not a reason. Any future path that pre-sets
        # that flag (a resumed session, say) would have silently stopped
        # marking remote sessions as tasks.
        server = self._remote_server
        if server is not None:
            session = server.get_session(session_id)
            if session is not None:
                session.mark_task_started()

        flow = self._flows.get(session_id)
        if flow is None or flow._ctx is None:
            return
        if flow._ctx.resume_search_disabled:
            return
        flow._ctx.resume_search_disabled = True
        self._clear_resume_offer(session_id)

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

        # A remote-controlled tab: release the bridge, which DETACHES from the
        # 被控 session without terminating it. Closing a window here must not
        # kill work on the other machine — that is the whole "remote machine is a
        # server" property. Deliberate termination goes through
        # `remote_close_session`. The seq the tab reached is persisted on the way
        # out so a later re-adopt resumes from the right place.
        if self._remote_hub is not None and self._remote_hub.is_remote(session_id):
            self._flows.pop(session_id, None)
            with self._uis_lock:
                self._uis.pop(session_id, None)
            try:
                await self._remote_hub.release_bridge(session_id)
            except Exception:
                logger.warning(
                    "remote_control: releasing bridge for %s failed",
                    session_id, exc_info=True,
                )
            self._reply_sinks.pop(session_id, None)
            self._closing.discard(session_id)
            # No _force_release_session_locks here: a remote session holds no
            # local desktop lock — the被控 machine owns that state and releases
            # it there.
            self._session_dispatch_locks.pop(session_id, None)
            self._inflight_by_sid.pop(session_id, None)
            _emit({"type": "final", "id": msg_id,
                   "result": {"close_session": "ok", "session_id": session_id,
                              "elapsed_ms": int((time.monotonic() - t0) * 1000)}},
                  session_id=session_id)
            logger.info("close_session (remote) complete; sid=%s", session_id)
            return

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
            # Remote-control per-session slots. Harmless no-ops for an ordinary
            # local session; required for one that was being driven from another
            # machine, so a later session reusing this sid cannot inherit a stale
            # reply sink pointing at a dead NetworkUIDelegate.
            self._reply_sinks.pop(session_id, None)
            # If this was a remotely-driven session being destroyed, refresh
            # the serve-state broadcast so the dashboard stays honest.
            if self._remote_server is not None:
                self._broadcast_serve_state()
            elapsed = (time.monotonic() - t0) * 1000.0
            _emit({"type": "final", "id": msg_id,
                   "result": {"close_session": "ok",
                              "session_id": session_id,
                              "elapsed_ms": round(elapsed, 1)}},
                  session_id=session_id)

    async def _purge_remote_session_state(self, session_id: str) -> None:
        """Free every per-session slot held for a被控-side remote session.

        The teardown counterpart to ``_BridgeSessionHost.create_flow``. That
        method deliberately goes through ``_ensure_flow``, which is what makes a
        remotely-driven session a first-class local one — and therefore also
        what makes it allocate the same seven things any local session does. Only
        one of them (the flow) is freed by the server awaiting ``flow.destroy()``;
        the rest have no other release path, because the被控 machine has no tab
        for a ``rc-`` session and so no ``close_session`` IPC ever arrives for it.
        Before this existed, every remote session a machine had ever served
        stayed pinned for the life of the process: a dead flow in ``_flows``, a
        per-session LLM service list with live httpx pools, a ``_StdioUI``, a
        reply sink, a dispatch lock, and a file handler still attached to the
        ROOT logger.

        Called from the ``on_session_destroyed`` hook, i.e. after the flow is
        already gone, so it must not destroy it again. It DOES still release the
        cross-session desktop/browser locks: a remote session runs real tools on
        this machine and can be holding them, and a wedged teardown that left
        them held would deadlock every other session on this box — the same
        reason ``_do_close_session`` does it defensively in a ``finally``.
        """
        flow = self._flows.pop(session_id, None)
        ctx_ref = getattr(flow, "_ctx", None) if flow is not None else None
        services = self._services_by_session.pop(session_id, [])
        with self._uis_lock:
            self._uis.pop(session_id, None)

        handler = self._engine_log_handlers.pop(session_id, None)
        try:
            from ..infrastructure.logger import remove_root_file_handler
            remove_root_file_handler(handler)
        except Exception:
            logger.exception(
                "remote_control: failed to detach engine.log handler for %s",
                session_id,
            )

        for i, svc in enumerate(services):
            try:
                await asyncio.wait_for(
                    svc.close(), timeout=self._NEW_SESSION_CLOSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "remote_control: svc[%d].close for %s timed out after %.1fs",
                    i, session_id, self._NEW_SESSION_CLOSE_TIMEOUT,
                )
            except Exception:
                logger.warning(
                    "remote_control: svc[%d].close for %s failed",
                    i, session_id, exc_info=True,
                )

        self._force_release_session_locks(ctx_ref, session_id)
        self._reply_sinks.pop(session_id, None)
        self._session_dispatch_locks.pop(session_id, None)
        self._inflight_by_sid.pop(session_id, None)
        self._closing.discard(session_id)
        # LAST, deliberately. While this entry is present ``_emit_session``
        # suppresses stamped envelopes for the sid, and teardown above can still
        # produce them (a service close or lock release that logs through a
        # session-scoped path). Dropping it first would reopen the dead-tab hole
        # for the duration of the purge.
        self._served_sessions.pop(session_id, None)
        logger.info(
            "remote_control: purged per-session state for served session %s",
            session_id,
        )

    # ------------------------------------------------------------------
    # Session-resume confirm (docs/session_resume_design.md §6.4/§6.5)
    # ------------------------------------------------------------------

    # Fixed review-task instruction (§ review-first resume): deliberately
    # does NOT include the verbatim text that triggered the offer — the
    # trigger is a pure signal ("resume this one"), not new task content
    # (see _PendingResumeOffer.goal's docstring). INTENT still classifies
    # this normally (it reads as world-work — inspecting a real workspace —
    # so it lands on "queue" on its own merits); the "don't act" instruction
    # is carried in the instruction text itself, the same way any other
    # task constrains the agent via its own wording, not a side-channel flag.
    _RESUME_REVIEW_INSTRUCTION = (
        "You just resumed this session from a saved trajectory (see the "
        "resume banner above). Review what's been restored — the "
        "conversation so far, the completed/pending task queue, and the "
        "current state of the workspace on disk — and write a short summary "
        "for the user covering: what this session was about, what's already "
        "been done and verified, and what (if anything) is still "
        "outstanding. Reading files to refresh your view of the workspace "
        "is fine. Do NOT start, continue, or verify-by-redoing any task, "
        "and do NOT call any tool that changes anything (write/edit/shell/"
        "browser actions, etc.) — this turn is purely informational. Wait "
        "for the user's next instruction before acting on anything."
    )

    # First bubble the user sees after confirming — sent verbatim, not
    # something INTENT is trusted to phrase (see _do_resume_confirm): INTENT
    # classifies _RESUME_REVIEW_INSTRUCTION into "queue" but its own
    # response_to_user for a queue lane is never forwarded to the UI (see
    # Orchestrator._handle_user_message — only the chat lane emits via
    # _on_reply_to_user), so without this explicit bubble the user would see
    # nothing until the review task's completion reply lands.
    _RESUME_REVIEWING_BUBBLE = "↻ Resuming — reviewing the previous session before doing anything else…"

    async def _do_resume_confirm(
        self, temp_sid: str, session_dir: str, msg_id: Optional[str],
    ) -> None:
        """User accepted a resume offer. Tears down whatever's currently
        running on the TEMP session (§ coordinator-and-resume-are-independent:
        the message that triggered this offer was NEVER held — it already
        ran, or is actively running right now — so there is typically real
        in-flight work to discard, not an idle flow; ``_cancel_inflight``
        below is exactly the "interrupt a real in-flight request/user_input"
        mechanism this needs), then rebuilds a flow at the SAME bridge sid
        pointed at the OLD session_dir, restores its digest, and queues a
        fixed REVIEW task instead of replaying the message that triggered
        the offer (§ review-first resume) — the agent looks at what it just
        got back and reports before doing anything else. The trigger message
        itself is simply never resurrected: picking a candidate means "I want
        the old session, not whatever this new ask was" — its work (if any
        got far enough to produce one) is discarded along with the temp
        session, same as the old work-discarding always was.

        Same sid, not a new one: the renderer's tab is already showing
        temp_sid — resuming must not require it to somehow discover a
        different id (§6.5 "让那条工作线醒过来继续，不是拷贝一份副本").
        """
        offer = self._pop_valid_resume_offer(temp_sid)
        if offer is None:
            _emit({"type": "error", "id": msg_id, "where": "bridge",
                   "message": "resume_confirm: no pending offer for this "
                              "session (expired or already resolved)",
                   "fatal": False}, session_id=temp_sid)
            return
        candidate = next(
            (c for c in offer.candidates if str(c.session_dir) == session_dir),
            None,
        )
        if candidate is None:
            _emit({"type": "error", "id": msg_id, "where": "bridge",
                   "message": "resume_confirm: session_dir does not match "
                              "any offered candidate",
                   "fatal": False}, session_id=temp_sid)
            return

        logger.info(
            "resume_confirm sequence begin; temp_sid=%s -> session_dir=%s",
            temp_sid, session_dir,
        )
        t0 = time.monotonic()

        # ── Tear down the temp session (same teardown as close_session,
        #    minus its close_session-shaped final emit) ────────────────
        self._closing.add(temp_sid)
        await self._cancel_inflight(temp_sid)
        temp_flow = self._flows.pop(temp_sid, None)
        temp_services = self._services_by_session.pop(temp_sid, [])
        ctx_ref = getattr(temp_flow, "_ctx", None) if temp_flow is not None else None
        handler = self._engine_log_handlers.pop(temp_sid, None)
        try:
            from ..infrastructure.logger import remove_root_file_handler
            remove_root_file_handler(handler)
        except Exception:
            logger.exception(
                "resume_confirm[sid=%s]: failed to detach engine.log handler",
                temp_sid,
            )
        try:
            if temp_flow is not None:
                try:
                    await asyncio.wait_for(temp_flow.destroy(), timeout=2.5)
                except asyncio.TimeoutError:
                    logger.warning(
                        "resume_confirm[sid=%s]: temp flow.destroy timed out (2.5s)",
                        temp_sid,
                    )
                except Exception:
                    logger.warning(
                        "resume_confirm[sid=%s]: temp flow.destroy failed",
                        temp_sid, exc_info=True,
                    )
            for i, svc in enumerate(temp_services):
                try:
                    await asyncio.wait_for(
                        svc.close(), timeout=self._NEW_SESSION_CLOSE_TIMEOUT,
                    )
                except Exception:
                    logger.warning(
                        "resume_confirm[sid=%s]: temp svc[%d].close failed",
                        temp_sid, i, exc_info=True,
                    )
        finally:
            self._force_release_session_locks(ctx_ref, temp_sid)
            self._closing.discard(temp_sid)

        # ── Rebuild at the SAME sid, pointed at the OLD session_dir ─────
        try:
            self._ensure_flow(
                temp_sid, goal=candidate.digest.title or "resume",
                resume_session_dir=candidate.session_dir,
            )
            flow = self._flows[temp_sid]
            # _ensure_flow's own session_started envelope named the tab
            # after the NEW trigger message (offer.goal) — override with the
            # OLD session's actual title so the tab reflects what's really
            # continuing, not what the user just typed to trigger it.
            if candidate.digest.title:
                self._emit_session({
                    "type": "status", "kind": "session_started",
                    "session_dir": str(candidate.session_dir),
                    "workspace_dir": flow.working_directory,
                    "session_name": candidate.digest.title.strip()[:30],
                }, temp_sid)
            await flow.start(resume_digest=candidate.digest)

            # This session's identity is now settled — never search again
            # (the other permanent-stop path is the "No Resume" button; see
            # SessionContext.resume_search_disabled's docstring).
            if flow._ctx is not None:
                flow._ctx.resume_search_disabled = True

            # ── Review-first (§ review-first resume): announce, THEN queue
            #    the review — not the trigger message — as a real task. ──
            self._forward_reply_to_ui_for(flow, self._RESUME_REVIEWING_BUBBLE)
            # on_user_message runs the review instruction through the normal
            # INTENT lane (expected to land on "queue" — it reads as
            # inspecting a real workspace); its own response_to_user is
            # intentionally unused here (queue-lane replies aren't forwarded
            # to the UI anyway — see _RESUME_REVIEWING_BUBBLE's docstring).
            # The task's actual completion reply (the summary) reaches the
            # UI later, asynchronously, via the flow's own on_reply_to_user
            # wiring once PersistentAgent finishes the item — same path
            # every other task's completion reply already takes.
            await flow.on_user_message(self._RESUME_REVIEW_INSTRUCTION)

            elapsed = (time.monotonic() - t0) * 1000.0
            _emit({
                "type": "final", "id": msg_id,
                "result": {
                    "resume_confirm": "ok",
                    "session_dir": str(candidate.session_dir),
                    "elapsed_ms": round(elapsed, 1),
                },
            }, session_id=temp_sid)
        except Exception as exc:
            logger.exception(
                "resume_confirm[sid=%s]: rebuild against %s failed",
                temp_sid, session_dir,
            )
            _emit({"type": "error", "id": msg_id, "where": "engine",
                   "message": f"resume_confirm failed: {exc}", "fatal": True},
                  session_id=temp_sid)

    @staticmethod
    def _forward_reply_to_ui_for(flow: "FlowControllerV2", text: str) -> None:
        """Send one assistant-bubble-shaped reply through the SAME path a
        normal coordinator/task-completion reply takes (FlowControllerV2's
        own ``_forward_reply_to_ui``), instead of calling ``_emit`` directly
        here — keeps this bubble indistinguishable from any other reply on
        the renderer side (same envelope shape, same fallback-to-inline-event
        behaviour if no ``on_reply_to_user`` callback is wired)."""
        flow._forward_reply_to_ui(text)



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
            # Remote control first, before any flow teardown. On the控制 side
            # this detaches cleanly and persists each session's replay position;
            # on the被控 side it tells every attached controller *why* their
            # sessions are ending (server_shutdown) instead of letting them see a
            # bare socket close and start reconnecting to a process that is gone.
            if self._remote_hub is not None:
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(self._remote_hub.close(), timeout=5.0)
                    logger.info("shutdown: remote hub closed (%.2f ms)", _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: remote hub close failed", exc_info=True)
                self._remote_hub = None
            if self._remote_server is not None:
                t0 = time.monotonic()
                try:
                    await asyncio.wait_for(self._remote_server.stop(), timeout=8.0)
                    logger.info("shutdown: remote server stopped (%.2f ms)", _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: remote server stop failed", exc_info=True)
                self._remote_server = None

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

        # Kick off the resume index build in the background (§6.4) —
        # fire-and-forget, never awaited on the boot path. A first message
        # arriving before this finishes just gets no resume offer (fails
        # open), which is the correct behaviour for a soft, non-blocking
        # search: there is nothing to gate here, only something to skip
        # gracefully if it isn't ready yet.
        asyncio.create_task(
            self._build_resume_index_background(), name="resume-index-build",
        )

        # Deliberately NOT starting the被控-side listener here. A machine goes
        # into server mode only when someone presses "As Server" in the Connect
        # panel (connect_start_server), never because it was restarted or
        # upgraded — see _start_remote_control_server's docstring. The client
        # side does auto-resume, below: reconnecting to machines we already
        # paired with is recovering a relationship the user set up, whereas
        # auto-listening would be opening this machine up on its behalf.

        # Tell the renderer the initial被控 state so the titlebar indicator is
        # correct from the first paint rather than only after the first toggle.
        self._broadcast_serve_state()

        # No boot-time client reconnect sweep. Connecting to a paired target is
        # on demand now (the Connect panel's ``remote_connect``, fired when its
        # client dashboard opens or the operator clicks Connect): a被控 machine
        # parks its sessions whether or not we hold a socket, so nothing is lost
        # by not restoring one at boot, and it serves one controller at a time,
        # so eagerly grabbing sockets for every paired machine at startup would
        # lock others out of machines this user isn't even looking at.

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
