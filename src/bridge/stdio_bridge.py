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
    2. Refuse if the bridge is currently running another flow session
       — SCHEDULER_BUSY_POLICY = "skip" applies (see scheduler docs).
    3. Otherwise emit a ``scheduled_task_started`` status envelope so
       the UI can show "scheduled task X firing", then route a synthetic
       ``request`` envelope through the same dispatcher path that
       a manually-typed request takes.

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


def _emit(obj: Dict[str, Any], gen: Optional[int] = None) -> None:
    """Serialise *obj* as one JSON line on the IPC stdout and flush.

    If *gen* is supplied (the caller's session generation — see
    StdioBridge._generation and _StdioUI._generation), it is stamped onto
    the envelope as ``gen``. The renderer uses this field to drop events
    from flows that have been superseded by a `new_session`. Bridge-meta
    events (config_get / config_set / shutdown final, etc.) call without
    a *gen* — the renderer treats unstamped envelopes as "always accept",
    so this remains backwards-compatible if either side gets out of sync.
    """
    if gen is not None:
        obj["gen"] = gen
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with _write_lock:
        _ipc_out.write(line + "\n")
        _ipc_out.flush()
    try:
        logger.debug(
            "outbound envelope type=%s id=%s gen=%s",
            obj.get("type"), obj.get("id"), obj.get("gen"),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# UI adapter — re-emits engine events as token_stream / status / final / error
# ---------------------------------------------------------------------------


class _StdioUI:
    """V2 ``UIDelegate`` implementation for the Electron renderer.

    Each method serialises a JSON envelope onto the IPC stdout. The async
    ``request_*`` methods register an ``asyncio.Future`` keyed by prompt id;
    the stdin-reader thread resolves the matching future via
    :meth:`deliver_confirmation_response` when the user answers the modal.

    Generation tag: each instance is born with a generation captured at
    construction; every envelope carries it. ``_do_new_session`` bumps gen
    and builds a fresh ``_StdioUI``, leaving the OLD instance bound to the
    OLD flow's ``InteractionManager``. Stragglers from the old flow keep
    emitting with the OLD generation; the renderer's gen-watermark drops
    them, so the new conversation never sees old-flow content.
    """

    def __init__(self, generation: int = 0) -> None:
        self._generation = generation
        # Bridge-owned confirmation registry. The IM is a clean async
        # forwarder with no internal state to hook, so the bridge keeps
        # its own pending-future map.
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
               "fatal": False}, gen=self._generation)

    def show_state_changed(self, state: Any) -> None:
        _ui_logger.debug("show_state_changed: %s", _truncate(state))
        _emit({"type": "status", "kind": "state_changed", "state": str(state)},
              gen=self._generation)

    def show_inline_event(self, icon: str, desc: str) -> None:
        """Single-line status banner (icon + text). Renderer maps
        ``kind=inline_event`` to addStepBubble.
        """
        _emit({"type": "status", "kind": "inline_event",
               "icon": str(icon or "·"),
               "desc": str(desc or "")},
              gen=self._generation)

    def show_recall_started(self) -> None:
        """LTM recall in flight. Renderer maps ``kind=recall_started`` to a
        transient 'recalling…' label on the activity strip, superseded by the
        next state / decision / tool event.
        """
        _emit({"type": "status", "kind": "recall_started"},
              gen=self._generation)

    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int = 0,
    ) -> None:
        _ui_logger.debug("notify_decision_made: iter=%s tokens=%s",
                         iteration, token_count)
        # Renderer reads args[0]=iter, args[1]=reasoning (renderer.js:2004-2007).
        _emit({"type": "status", "kind": "decision_made",
               "args": [str(iteration), str(reasoning), str(token_count)],
               "kwargs": {}},
              gen=self._generation)

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
              gen=self._generation)

    # ── Non-Protocol forwarders (called by IM via ``_ui_call``) ──────────
    # The V2 ``UIDelegate`` Protocol is intentionally minimal. These methods
    # receive events the Protocol doesn't list — the IM's ``_ui_call`` resolves
    # them by string name and silently skips when missing. Tools / receptionist
    # streaming hook here; renderer-side handlers stay unchanged.

    def notify_desktop_takeover_started(self, reason: str = "input_action") -> None:
        """Desktop tool entered an input-driving phase. Pause the activity
        monitor (so its OCR samples don't capture agent-driven mouse / keyboard
        events) and emit the ``desktop_takeover_started`` envelope so the
        Electron overlay shows the fullscreen border + Ctrl+Shift+C revoke
        hook.
        """
        _ui_logger.debug("notify_desktop_takeover_started: reason=%s", reason)
        try:
            if personality_monitor is not None:
                personality_monitor.pause()
        except Exception:
            _ui_logger.exception("personality_monitor.pause failed")
        _emit({"type": "status", "kind": "desktop_takeover_started",
               "reason": str(reason)}, gen=self._generation)

    def notify_desktop_takeover_ended(self, reason: str = "task_ended") -> None:
        _ui_logger.debug("notify_desktop_takeover_ended: reason=%s", reason)
        try:
            if personality_monitor is not None:
                personality_monitor.resume()
        except Exception:
            _ui_logger.exception("personality_monitor.resume failed")
        _emit({"type": "status", "kind": "desktop_takeover_ended",
               "reason": str(reason)}, gen=self._generation)

    def notify_session_event(self, event_name: str, data: Any = None) -> None:
        """Live shell session lifecycle (open / exec_done / close). Renderer
        renders a session monitor panel from these events. Currently no V2
        caller — ``session_tool`` is neutered until its V2 rewire — but kept
        here so the rewire is just adding the call back."""
        _ui_logger.debug("notify_session_event: %s", event_name)
        _emit({"type": "status", "kind": "session_event",
               "event": str(event_name),
               "data": data if isinstance(data, dict) else {}},
              gen=self._generation)

    def show_receptionist_thinking(self) -> None:
        _emit({"type": "status", "kind": "receptionist_thinking_on"},
              gen=self._generation)

    def clear_receptionist_thinking(self) -> None:
        _emit({"type": "status", "kind": "receptionist_thinking_off"},
              gen=self._generation)

    def stream_receptionist_reply_chunk(self, text: str) -> None:
        _emit({"type": "status", "kind": "reply_delta", "text": str(text)},
              gen=self._generation)

    def seal_receptionist_reply(self) -> None:
        _emit({"type": "status", "kind": "reply_done"},
              gen=self._generation)

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
        _emit(env, gen=self._generation)
        _ui_logger.debug("await_user_response: kind=%s id=%s", kind, prompt_id)
        try:
            return await fut
        except asyncio.CancelledError:
            with self._pending_lock:
                self._pending.pop(prompt_id, None)
            raise

    async def request_risk_confirmation(
        self, description: str,
    ) -> UserConfirmation:
        """High-risk operation gate. Emits ``kind=risk_confirmation``;
        awaits the renderer's yes/no/text answer."""
        prompt_id = f"risk-{int(time.time() * 1000)}-{id(description) & 0xffff:04x}"
        payload = {"description": str(description)}
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
        if str(tool_name) == "desktop":
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
    """Single entry-point class for the stdio JSON bridge."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self.config_path: Path = Path(config_path)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._inbox: Optional[asyncio.Queue] = None

        # Lazy backend state — built on first 'request' message.
        self._flow: Optional[FlowControllerV2] = None
        self._services: List[LLMService] = []

        # Session-scoped handq-engine.log handler, attached to the ROOT logger
        # by _ensure_flow and detached by _do_new_session so each session gets
        # a fresh, isolated engine log (see logger.add_root_file_handler).
        self._engine_log_handler: Optional[logging.Handler] = None

        # Session generation. Bumped by _do_new_session before the new
        # singleton is constructed, so a fresh _StdioUI with the new
        # generation drives the new flow while the OLD _StdioUI (still
        # referenced by the old IM via the old FlowControllerV2) keeps
        # emitting with its captured OLD generation. The renderer drops
        # any envelope whose gen is older than its current generation,
        # which is what isolates the new conversation from a wedged
        # old subtask that may keep emitting until its blocking syscall
        # finally returns (Windows: no portable thread kill).
        self._generation: int = 0

        # The bridge no longer owns an InteractionManager — FlowControllerV2
        # constructs and exposes one at ``flow.interaction_manager``. We
        # build the _StdioUI here without an IM ref; ``_ensure_flow`` calls
        # ``flow.interaction_manager.set_delegate(self._ui)`` to wire
        # delegate-mode events.
        self._ui = _StdioUI(self._generation)

        self._shutdown_requested: bool = False

        logger.info("StdioBridge initialised; config_path=%s gen=%d",
                    self.config_path, self._generation)
        # Publish self for module-level helpers (dispatch_scheduled_task).
        # See module docstring on the cross-module slots.
        global _active_bridge
        _active_bridge = self

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
                           "fatal": False}, gen=self._generation)
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
                if (isinstance(obj, dict)
                        and obj.get("type") == "user_input"
                        and obj.get("kind") == "confirmation"):
                    try:
                        prompt_id = str(obj.get("id") or "")
                        self._ui.deliver_confirmation_response(
                            prompt_id, str(obj.get("answer", "")),
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
                  gen=self._generation)
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
                }, gen=self._generation)
            except Exception as exc:
                logger.exception("config_get failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_get failed: {exc}", "fatal": False},
                      gen=self._generation)
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
                }, gen=self._generation)
            except Exception as exc:
                logger.exception("ltm_stats failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"ltm_stats failed: {exc}", "fatal": False},
                      gen=self._generation)
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
                      gen=self._generation)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, gen=self._generation)
            return

        # ── LTM 2.0 Skill proposal + Summary IPC ────────────────────────
        # ``ltm_list_skill_proposals``: surface staged skill_proposal rows
        #   so the UI can render an approval queue.
        # ``ltm_approve_skill_proposal``: move the staging SKILL.md to live,
        #   reload SkillRegistry, flip status='approved'.
        # ``ltm_reject_skill_proposal``: archive the row and remove staging.
        # ``ltm_query_summary``: read one obs_summaries row (date+type) for UI.
        if msg_type in (
            "ltm_list_skill_proposals", "ltm_approve_skill_proposal",
            "ltm_reject_skill_proposal", "ltm_query_summary",
        ):
            try:
                from src.infrastructure.long_term_memory import LongTermMemory
                ltm = LongTermMemory.get()
                if msg_type == "ltm_list_skill_proposals":
                    status = str(msg.get("status") or "proposed")
                    limit = int(msg.get("limit") or 50)
                    proposals = await ltm.list_skill_proposals(
                        status=status, limit=limit,
                    )
                    result = {"proposals": proposals}
                elif msg_type == "ltm_approve_skill_proposal":
                    # The entity id rides under ``skill_id``: the envelope's
                    # ``id`` is the RPC correlation key and is overwritten by
                    # the renderer's rpc() layer, so it cannot carry the row id.
                    skill_id = str(msg.get("skill_id") or "")
                    if not skill_id:
                        raise ValueError("ltm_approve_skill_proposal: skill_id required")
                    result = await ltm.approve_skill_proposal(skill_id)
                elif msg_type == "ltm_reject_skill_proposal":
                    skill_id = str(msg.get("skill_id") or "")
                    if not skill_id:
                        raise ValueError("ltm_reject_skill_proposal: skill_id required")
                    reason = str(msg.get("reason") or "")
                    result = await ltm.reject_skill_proposal(skill_id, reason=reason)
                else:  # ltm_query_summary
                    date_iso = str(msg.get("date") or "")
                    # NB: the envelope's ``type`` field is the routing key
                    # (== "ltm_query_summary" here), so the period must travel
                    # under a distinct key or it would be clobbered.
                    type_ = str(msg.get("summary_type") or "daily")
                    lang = str(msg.get("language") or "en")
                    if not date_iso:
                        raise ValueError("ltm_query_summary: date required")
                    row = await ltm._store.get_obs_summary(
                        date=date_iso, type_=type_, language=lang,
                    )
                    if row is None:
                        result = {"found": False}
                    else:
                        import json as _json
                        try:
                            moments = _json.loads(row[3]) if row[3] else []
                        except (TypeError, _json.JSONDecodeError):
                            moments = []
                        result = {
                            "found": True,
                            "date": row[0], "type": row[1], "language": row[2],
                            "moments": moments,
                            "summary_text": row[4] or "",
                            "generated_model": row[5] or "",
                            "generated_at": int(row[6] or 0),
                        }
                _emit({"type": "final", "id": msg_id, "result": result},
                      gen=self._generation)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, gen=self._generation)
            return

        # ── Activity Monitor IPC ───────────────────────────────────────
        if msg_type in ("personality_status", "personality_pause", "personality_resume"):
            try:
                if personality_monitor is None:
                    raise RuntimeError("activity monitor not initialised")
                if msg_type == "personality_pause":
                    personality_monitor.pause()
                elif msg_type == "personality_resume":
                    personality_monitor.resume()
                snap = personality_monitor.snapshot_status()
                _emit({"type": "final", "id": msg_id, "result": snap},
                      gen=self._generation)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, gen=self._generation)
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
                      gen=self._generation)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, gen=self._generation)
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
                      gen=self._generation)
            except Exception as exc:
                logger.exception("%s failed", msg_type)
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"{msg_type} failed: {exc}",
                       "fatal": False}, gen=self._generation)
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
                if self._flow is not None:
                    try:
                        self._flow.config_manager.reload_config()
                    except Exception:
                        logger.exception("config_set: reload_config failed (continuing)")

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
                }, gen=self._generation)
            except Exception as exc:
                logger.exception("config_set failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_set failed: {exc}", "fatal": False},
                      gen=self._generation)
            return

        if msg_type == "request":
            try:
                goal = msg.get("goal", "")
                # Early API-key guard — only on the first request (before the
                # FlowController is built). An empty key causes cryptic errors
                # deep in the LLM stack; surface a clear message here instead.
                if self._flow is None:
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
                            gen=self._generation,
                        )
                        return
                self._ensure_flow(goal=str(goal))
                assert self._flow is not None
                if not self._flow.started:
                    await self._flow.start()
                # ``on_user_message`` returns the receptionist's
                # reply string (sync conversational answer); background
                # agent + planner work proceeds inside the flow's own
                # asyncio tasks and emits status events through the IM
                # delegate as it happens. ``final`` correlates with this
                # request id; subsequent activity arrives as status events.
                try:
                    reply = await self._flow.on_user_message(str(goal))
                    _emit({"type": "final", "id": msg_id,
                           "result": {"reply": reply, "ok": True}},
                          gen=self._generation)
                except Exception as exc:
                    logger.exception("on_user_message failed; id=%s", msg_id)
                    _emit({"type": "error", "id": msg_id, "where": "engine",
                           "message": f"on_user_message failed: {exc}",
                           "fatal": False}, gen=self._generation)
            except Exception as exc:
                logger.exception("request failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"request failed: {exc}", "fatal": True},
                      gen=self._generation)
            return

        if msg_type == "user_input":
            try:
                kind = msg.get("kind", "message")
                if kind == "message":
                    text = str(msg.get("text", ""))
                    if self._flow is not None and self._flow.started:
                        await self._flow.on_user_message(text)
                    else:
                        logger.warning(
                            "user_input(message) before flow started; ignoring"
                        )
                elif kind == "confirmation":
                    # Normally consumed by the stdin reader fast-path; this
                    # branch covers the (rare) case where the envelope reaches
                    # the dispatcher via the asyncio inbox instead.
                    prompt_id = str(msg.get("id") or "")
                    self._ui.deliver_confirmation_response(
                        prompt_id, str(msg.get("answer", "")),
                    )
                elif kind == "desktop_takeover_revoked":
                    # Frontend overlay's revoke hotkey (Ctrl+C or
                    # equivalent) sends this. We flip the takeover flag
                    # so subsequent input actions refuse for the rest
                    # of this task; read-only desktop actions stay
                    # available. The notify_desktop_takeover_ended
                    # event with reason='user_revoked' is emitted by
                    # the DesktopState's revoke method itself.
                    try:
                        ds = (
                            self._flow._ctx.desktop_state
                            if self._flow is not None and self._flow._ctx is not None
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
                            "user_input desktop_takeover_revoked: changed=%s",
                            changed,
                        )
                    except Exception as exc:
                        logger.warning(
                            "user_input desktop_takeover_revoked failed: %s", exc,
                        )
                else:
                    logger.warning("user_input: unknown kind=%r", kind)
                    _emit({"type": "error", "id": msg_id, "where": "bridge",
                           "message": f"Unknown user_input.kind: {kind!r}",
                           "fatal": False}, gen=self._generation)
            except Exception as exc:
                logger.exception("user_input failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"user_input failed: {exc}", "fatal": False},
                      gen=self._generation)
            return

        if msg_type == "shutdown":
            await self._do_shutdown(msg_id)
            return

        if msg_type == "new_session":
            await self._do_new_session(msg_id)
            return

        logger.warning("unknown inbound type=%r id=%s", msg_type, msg_id)
        _emit({"type": "error", "id": msg_id, "where": "bridge",
               "message": f"Unknown message type: {msg_type!r}",
               "fatal": False}, gen=self._generation)

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
                if not schedule_str:
                    from src.infrastructure.scheduler.inferer import infer_schedule
                    # Single-use LLM service built from current config —
                    # see inferer.py module docstring for rationale.
                    config = self._load_config_dict()
                    result = await infer_schedule(prompt_str, config)
                    schedule_str = result.schedule
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
            return {"ok": True, "task": t}

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
        receptionist returned a reply without raising", NOT "the agent
        finished its background work". The persistent flow has no per-task
        completion signal — the planner / agent run continuously until the
        next user message. If finer-grained tracking is needed later, hook
        into ``SharedCheckList.on_item_done`` for the items the planner
        spawned in response to this dispatch.
        """
        if self._shutdown_requested:
            logger.info(
                "scheduler dispatch refused: shutdown in progress task=%s",
                task.id[:8],
            )
            return False
        # Publish a notification first so the renderer can show a
        # "scheduled task firing" toast.
        try:
            _emit({
                "type": "status", "kind": "scheduled_task_started",
                "id": task.id, "name": task.name,
                "schedule": task.schedule,
                "prompt_preview": _truncate(task.prompt, 200),
            }, gen=self._generation)
        except Exception:
            logger.exception("scheduler emit failed")

        msg_id = f"sched-{task.id}-{int(time.time())}"
        # ``dispatch_prompt``, when present, is the agent-facing variant
        # with relative-time language ("一分钟后…") stripped — see
        # ScheduledTask docstring. Empty means "use prompt verbatim".
        goal_text = task.dispatch_prompt or task.prompt

        try:
            self._ensure_flow(goal=str(goal_text))
            assert self._flow is not None
            if not self._flow.started:
                await self._flow.start()
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
        try:
            reply = await self._flow.on_user_message(str(goal_text))
            _emit({"type": "final", "id": msg_id,
                   "result": {"reply": reply, "ok": True, "scheduled": True}},
                  gen=self._generation)
        except Exception as exc:
            ok = False
            err = str(exc)[:500]
            logger.exception("scheduled task dispatch failed")
            _emit({"type": "error", "id": msg_id, "where": "engine",
                   "message": err, "fatal": False},
                  gen=self._generation)

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



    def _ensure_flow(self, goal: str) -> None:
        if self._flow is not None:
            return
        from ..infrastructure.role_resolver import resolve_models_and_helper

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
            # Detach any stale handler from a prior session before attaching the
            # new one (defensive — _do_new_session normally removes it first).
            remove_root_file_handler(self._engine_log_handler)
            self._engine_log_handler = add_root_file_handler(
                str(session_dir / "handq-engine.log"),
                level=_level,
            )
            logger.info(
                "_ensure_flow: HandQ engine logger initialised; level=%s log_dir=%s",
                _log_level_str.upper(), _engine_log_dir,
            )
        except Exception:
            logger.exception(
                "_ensure_flow: initialize_logger failed (continuing with default)"
            )

        api_key = llm_cfg.get("API_KEY") or ""
        # When `max_tokens` is missing or non-positive, leave it unset (None) so
        # AnthropicStreamingService falls back to the per-model ceiling instead of
        # capping every model at a default (e.g. 4096 truncates Sonnet/Haiku
        # mid-tool-call, breaking write/edit with "missing required parameter").
        _mt_raw = llm_cfg.get("max_tokens")
        try:
            _mt_int = int(_mt_raw) if _mt_raw is not None else 0
        except (TypeError, ValueError):
            _mt_int = 0
        max_tokens: Optional[int] = _mt_int if _mt_int > 0 else None

        # ── Resolve the main pool ───────────────────────────────────────
        # Two pools come out of the resolver: ``models`` (main controller
        # stack) and ``helper_models`` (auxiliary cheap pool consumed by
        # scheduler.inferer + LTM triage / reranker / retriage). The bridge
        # only cares about ``models`` here; helper_models is read directly by
        # those auxiliary callers when they construct their own services.
        models, _helper_models = resolve_models_and_helper(llm_cfg)

        # Fallback when no models are configured at all — keep the bridge
        # bootable so the user can open Settings and configure models from the UI.
        if not models:
            models = ["anthropic::claude-4-5-haiku"]

        logger.debug(
            "FlowController lazy construction: top_level_keys=%s llm_keys=%s "
            "session_keys=%s n_models=%d n_helper=%d max_tokens=%s "
            "api_key_present=%s session_dir=%s",
            sorted(cfg.keys()) if isinstance(cfg, dict) else None,
            sorted(llm_cfg.keys()),
            sorted(sess_cfg.keys()),
            len(models), len(_helper_models),
            max_tokens if max_tokens is not None else "auto(per-model ceiling)",
            bool(api_key),
            session_dir,
        )
        if not api_key:
            logger.warning("llm.API_KEY is empty in config; LLM calls will fail")

        # ── Build the service list ──────────────────────────────────────
        # One service per model. ``max_retries=10`` gives ~1-2 minutes of
        # patience per model on transient rate limits before falling through
        # to the next service in the chain. The fallback chain (built from
        # the priority-ordered ``models`` list) is the primary resilience
        # mechanism; max_retries handles short blips.
        _mt_kwargs: dict = {"max_tokens": max_tokens} if max_tokens is not None else {}
        consolidated_services: List[LLMService] = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=10, **_mt_kwargs,
            )
            for m in models
        ]

        # Track every distinct service for shutdown.
        self._services = list(consolidated_services)

        # Wire server-error notifications to the UI.  The closure captures
        # `self` so it always reads the current _generation at call time —
        # important because the same services are reused across sessions.
        _bridge = self
        def _on_llm_server_error(msg: str, retry_in: int, attempts_left: int) -> None:
            _emit({
                "type": "status",
                "kind": "llm_server_error",
                "message": msg,
                "retry_in": retry_in,
                "attempts_left": attempts_left,
            }, gen=_bridge._generation)
        for svc in self._services:
            svc.on_server_error = _on_llm_server_error

        self._flow = FlowControllerV2(
            llm_services=consolidated_services,
            working_directory=str(agent_workspace),
            storage_directory=str(session_dir),
            config_path=str(self.config_path),
            on_reply_to_user=self._on_receptionist_reply,
            expose_session_storage_in_prompt=False,
        )
        logger.info(
            "FlowControllerV2 constructed; %d llm_service(s) in fallback chain",
            len(consolidated_services),
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
            }, gen=self._generation)
        except Exception:
            logger.exception("Failed to emit session_started status event")

        # Make the agent's working directory real. The prompt advertises
        # ``Working directory: <agent_workspace>`` and tells the agent to keep
        # deliverables there, but the file/shell tools resolve relative paths
        # against the PROCESS cwd (Path(p).absolute() / subprocess inherit) —
        # and electron spawns the bridge with NO cwd set (electron/main.js:
        # "No cwd is set on purpose"), so cwd was the electron launch dir. A
        # bare-filename write therefore landed in C:\...\electron\ instead of
        # the session workspace. chdir here closes that prompt↔runtime gap:
        # relative writes/shell commands now land in the workspace, and the
        # absolute path reported back (Path.absolute()) is the one the user
        # can actually find. Bridge config/logs use absolute paths, so they
        # are unaffected; on new_session this re-points cwd at the new
        # session's workspace (single active session — no concurrency race).
        try:
            os.chdir(str(agent_workspace))
            logger.info("session cwd set to agent workspace: %s", agent_workspace)
        except Exception:
            logger.exception(
                "Failed to chdir into agent workspace %s; relative writes may "
                "land in the process launch dir", agent_workspace,
            )

        # Bind the bridge's UI delegate to the IM that FlowControllerV2 owns.
        # All ``notify_*`` / ``request_*`` calls inside the V2 stack route
        # through here. The loop ref lets the stdin reader thread resolve
        # confirmation futures via call_soon_threadsafe.
        if self._flow.interaction_manager is not None:
            self._flow.interaction_manager.set_delegate(self._ui)
        try:
            self._ui._loop = asyncio.get_running_loop()
        except RuntimeError:
            # _ensure_flow may run before the loop is captured into _stdin
            # context; the loop will be set on first await of a confirmation.
            pass

        # Tools (desktop_tool, browser_tool) pick up their IM ref from
        # ``ctx.interaction_manager`` at construction (SessionContext DI).
        # The bridge no longer wires ``set_interaction_manager`` on each
        # tool — flow.start() built ctx with self.interaction_manager and
        # passed ctx to PersistentAgent → ToolRegistry → tool __init__.

        # Wire module-level bridge hooks so llm_pool can emit status events
        # without touching deep planner/agent/receptionist call paths.
        from ..infrastructure.llm_pool import (
            set_fallback_notifier as _set_fn,
            set_network_event_notifier as _set_net_fn,
        )
        _bridge_ref = self

        def _on_llm_fallback(from_model: str, to_model: str, exc: Exception) -> None:
            _emit(
                {
                    "type": "status",
                    "kind": "llm_fallback",
                    "from_model": from_model,
                    "to_model": to_model,
                    "error": str(exc)[:200],
                },
                gen=_bridge_ref._generation,
            )

        _set_fn(_on_llm_fallback)

        def _on_network_event(state: str, attempt: int, sleep_secs: int) -> None:
            if state == "down":
                _emit(
                    {
                        "type": "status",
                        "kind": "network_down",
                        "message": "网络已中断，LLM 服务暂不可达，等待恢复中…",
                    },
                    gen=_bridge_ref._generation,
                )
            elif state == "waiting":
                _emit(
                    {
                        "type": "status",
                        "kind": "network_waiting",
                        "attempt": attempt,
                        "retry_in": sleep_secs,
                    },
                    gen=_bridge_ref._generation,
                )
            elif state == "restored":
                _emit(
                    {
                        "type": "status",
                        "kind": "network_restored",
                        "message": "网络已恢复，继续执行",
                    },
                    gen=_bridge_ref._generation,
                )

        _set_net_fn(_on_network_event)

    def _on_receptionist_reply(self, text: str) -> None:
        """``FlowControllerV2.on_reply_to_user`` callback. Emits the
        receptionist's chat reply as a ``kind=reply`` status envelope so
        the renderer can render an assistant text bubble. Streaming
        (``reply_delta`` / ``reply_done``) is currently unwired — V2
        baseline emits the full reply once."""
        if not text:
            return
        _emit({"type": "status", "kind": "reply", "text": str(text)},
              gen=self._generation)

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
    #      via the orchestrator's planner_loop / agent's run_loop catches
    #      CancelledError and drains.
    #
    # Stragglers: if a child task ignores cancellation entirely, it survives
    # as a background coroutine. It can emit a short tail of status envelopes
    # before its underlying I/O finally times out. The renderer's
    # generation-tag watermark drops them, so they don't pollute the new
    # conversation — only correctness issue avoided is process leak, and the
    # asyncio cancel handles that on the next event-loop pass.
    # ------------------------------------------------------------------

    # Cleanup budget. Bridge total stall ≤ GRACE + CLOSE * len(services)
    # — for the default 4 services, ≤ ~10s worst case, ≤ ~2s typical.
    _NEW_SESSION_GRACE_TIMEOUT = 2.5    # cooperative interrupt
    _NEW_SESSION_HARD_TIMEOUT  = 1.5    # after explicit cancel
    _NEW_SESSION_CLOSE_TIMEOUT = 2.0    # per-service httpx pool drain

    async def _do_new_session(
        self, msg_id: Optional[str], *, _suppress_final: bool = False,
    ) -> None:
        # Bump the generation BEFORE doing any cleanup, then construct a
        # fresh _StdioUI bound to the new gen. The OLD _StdioUI is NOT
        # mutated — it stays referenced by the OLD InteractionManager
        # (which is still referenced by the old FlowControllerV2), so any
        # straggler ``notify_*`` call from a wedged old subtask continues
        # to emit through the OLD _StdioUI with the OLD generation. The
        # renderer's gen-watermark drops those.
        old_gen = self._generation
        self._generation = old_gen + 1
        new_gen = self._generation
        new_ui = _StdioUI(new_gen)
        try:
            new_ui._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        logger.info("new_session sequence begin; id=%s old_gen=%d new_gen=%d suppress_final=%s",
                    msg_id, old_gen, new_gen, _suppress_final)
        t0 = time.monotonic()

        # Snapshot + clear up-front. Any later in-flight `_emit` from
        # an orphaned subtask can still write to stdout (the IPC stream
        # is process-wide and unconditionally available), but it can no
        # longer alias state on `self`. Crucially, a request that arrives
        # next tick sees `_flow is None` and rebuilds from scratch.
        flow = self._flow
        services = self._services
        self._flow = None
        self._services = []

        # Detach + close this session's engine.log handler from the root logger
        # so the file is released and the next session's _ensure_flow opens a
        # fresh handq-engine.log (no cross-session bleed, no lingering Windows
        # file lock). The next _ensure_flow re-attaches a new one.
        try:
            from ..infrastructure.logger import remove_root_file_handler
            remove_root_file_handler(self._engine_log_handler)
        except Exception:
            logger.exception("new_session: failed to detach engine.log handler")
        finally:
            self._engine_log_handler = None

        try:
            # ``flow.destroy()`` trips the SharedCheckList interrupt event
            # (waking shell/session tools parked on subprocess waits), cancels
            # the agent + planner asyncio tasks, then awaits
            # ``SessionContext.close()`` which closes the Playwright browser,
            # kills interactive shell sessions, drains the SSH connection
            # pool, sweeps the desktop / browser screenshot stores, and drops
            # the per-session ``FileState`` + ``DesktopState`` instances.
            #
            # That single async call collapses what would otherwise be 6
            # manual flush sites + 2 detach calls. New tools that need
            # session-scoped cleanup register on the SessionContext; the
            # bridge does not need to know about them.
            if flow is not None:
                try:
                    await asyncio.wait_for(flow.destroy(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("new_session: flow.destroy timed out (2.0s)")
                except Exception:
                    logger.warning(
                        "new_session: flow.destroy failed", exc_info=True,
                    )

            # AnthropicStreamingService instances live on the bridge (not on
            # the flow) so flow.destroy doesn't touch them; the bridge owns
            # their httpx pool drain. Cap each close to keep new_session
            # bounded if a half-open TCP socket stalls.
            for i, svc in enumerate(services):
                try:
                    await asyncio.wait_for(
                        svc.close(),
                        timeout=self._NEW_SESSION_CLOSE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "new_session: svc[%d].close timed out after %.1fs",
                        i, self._NEW_SESSION_CLOSE_TIMEOUT,
                    )
                except Exception:
                    logger.warning(
                        "new_session: svc[%d].close failed",
                        i, exc_info=True,
                    )

            self._ui = new_ui
        except Exception:
            logger.exception("new_session chain raised unexpectedly")
        finally:
            elapsed = (time.monotonic() - t0) * 1000.0
            logger.info("new_session sequence complete (%.2f ms; new_gen=%d)",
                        elapsed, new_gen)
            if not _suppress_final:
                _emit({"type": "final", "id": msg_id,
                       "result": {"new_session": "ok",
                                  "generation": new_gen,
                                  "elapsed_ms": round(elapsed, 1)}},
                      gen=new_gen)

    # ------------------------------------------------------------------
    # Shutdown chain (per backend_surface.md §1)
    # ------------------------------------------------------------------

    async def _do_shutdown(self, msg_id: Optional[str]) -> None:
        if self._shutdown_requested:
            logger.debug("shutdown: already in progress; ignoring id=%s", msg_id)
            return
        self._shutdown_requested = True
        logger.info("shutdown sequence begin; id=%s", msg_id)
        overall_t0 = time.monotonic()

        def _step_ms(t0: float) -> float:
            return (time.monotonic() - t0) * 1000.0

        try:
            if self._flow is not None:
                t0 = time.monotonic()
                try:
                    # ``flow.destroy()`` is async: it trips the checklist
                    # interrupt event, cancels both run-loops, and awaits
                    # ``SessionContext.close()`` to tear down all per-session
                    # resources (browser / shells / SSH pool / desktop state /
                    # file state). The bridge no longer detaches IM refs or
                    # calls flush_*_pool — destroy does all of it in one place.
                    await asyncio.wait_for(
                        self._flow.destroy(),
                        timeout=2.5,
                    )
                    logger.info("shutdown: flow.destroy OK (%.2f ms)", _step_ms(t0))
                except asyncio.TimeoutError:
                    logger.warning(
                        "shutdown: flow.destroy timed out (2.5s); leaving "
                        "any stragglers as background — generation tag will "
                        "filter their late emits",
                    )
                except Exception:
                    logger.warning("shutdown: flow.destroy failed (%.2f ms)",
                                   _step_ms(t0), exc_info=True)
            else:
                logger.info("shutdown: no FlowControllerV2 to destroy")

            for i, svc in enumerate(self._services):
                t0 = time.monotonic()
                try:
                    await svc.close()
                    logger.info("shutdown: svc[%d].close OK (%.2f ms)", i, _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: svc[%d].close failed (%.2f ms)",
                                   i, _step_ms(t0), exc_info=True)
        except Exception:
            logger.exception("shutdown chain raised unexpectedly")
        finally:
            logger.info("shutdown sequence complete (%.2f ms total)", _step_ms(overall_t0))
            _emit({"type": "final", "id": msg_id, "result": {"shutdown": "ok"}},
                  gen=self._generation)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

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
            try:
                await self._handle(msg)
            except Exception as exc:
                logger.exception("dispatcher caught exception")
                _emit({"type": "error", "where": "bridge",
                       "message": f"dispatch crashed: {exc}", "fatal": False},
                      gen=self._generation)
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
