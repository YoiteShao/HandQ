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

# Existing public symbols — imported per porting_design.md §3.1.
# These imports are required by the design contract; they are not all used
# at startup (FlowController is built lazily on the first 'request' message
# so that config-only round-trips do not need an API key).
from src.controller.flow_controller import FlowController  # noqa: F401
from src.controller.interaction_manager import InteractionManager
from src.models.decision import Decision
from src.models.state import UserConfirmation
from src.infrastructure.anthropic_streaming_service import (  # noqa: F401
    AnthropicStreamingService,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
    StreamDoneEvent,
)
from src.infrastructure.config_manager import ConfigManager


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
        script_body = "#!/usr/bin/env python\n" + script_body
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


def _allocate_session_dir(goal: str) -> Path:
    """Create %USERPROFILE%\\HandQ\\History\\<TS>-<slug>\\ and return it."""
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
    """
    UI delegate registered via ``InteractionManager.set_ui``.

    Method names match the ``_BackgroundUI`` protocol used by the engine;
    every call is serialised to a JSON envelope on stdout. Methods the
    engine does not call are simply absent (InteractionManager skips them
    silently — see interaction_manager.py:141).

    Generation tag: every _StdioUI instance is born with a generation
    number captured at construction time. The bridge bumps the
    generation in `_do_new_session` and constructs a *fresh* _StdioUI
    for the new singleton — the OLD _StdioUI remains attached to the
    old InteractionManager (still referenced by the old FlowController's
    captured singleton ref), so any straggling notify_* call from a
    wedged old subtask emits with the OLD generation. The renderer
    drops those, so the new conversation never sees old-flow content.
    """

    def __init__(self, generation: int = 0, im: Optional[InteractionManager] = None) -> None:
        self._generation = generation
        # InteractionManager reference is required for the confirmation
        # methods (request_risk_confirmation / request_tool_confirmation /
        # request_secret_input) to install a pending-callback into the IM
        # so inbound `user_input.kind=confirmation` envelopes can unblock
        # the waiter. None is allowed only for legacy code paths that
        # never call those methods.
        self._im: Optional[InteractionManager] = im

    # --- generic display ----------------------------------------------------
    def display_message(self, msg: str) -> None:
        _ui_logger.debug("display_message: %s", _truncate(msg))
        _emit({"type": "status", "kind": "message", "text": str(msg)},
              gen=self._generation)

    def display_receptionist_reply(self, msg: str) -> None:
        _ui_logger.debug("display_receptionist_reply: %s", _truncate(msg))
        _emit({"type": "status", "kind": "reply", "text": str(msg)},
              gen=self._generation)

    def display_error(self, msg: str) -> None:
        _ui_logger.error("display_error: %s", _truncate(msg))
        _emit({"type": "error", "where": "engine", "message": str(msg),
               "fatal": False}, gen=self._generation)

    def display_progress_status(self, current: int, total: int) -> None:
        _ui_logger.debug("display_progress_status: %s/%s", current, total)
        _emit({"type": "status", "kind": "progress",
               "current": current, "total": total}, gen=self._generation)

    # --- state / step events -----------------------------------------------
    def show_state_changed(self, state: Any) -> None:
        _ui_logger.debug("show_state_changed: %s", _truncate(state))
        _emit({"type": "status", "kind": "state_changed", "state": str(state)},
              gen=self._generation)

    def show_step_started(self, step_id: Any, desc: str = "") -> None:
        _ui_logger.debug("show_step_started: id=%s desc=%s", step_id, _truncate(desc))
        _emit({"type": "status", "kind": "step_started",
               "step_id": str(step_id), "desc": str(desc)},
              gen=self._generation)

    def show_step_completed(self, step_id: Any, desc: str = "") -> None:
        _ui_logger.debug("show_step_completed: id=%s desc=%s", step_id, _truncate(desc))
        _emit({"type": "status", "kind": "step_completed",
               "step_id": str(step_id), "desc": str(desc)},
              gen=self._generation)

    # --- engine notifications ----------------------------------------------
    def notify_decision_made(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_decision_made: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "decision_made",
               "args": [str(a) for a in args],
               "kwargs": {k: str(v) for k, v in kwargs.items()}},
              gen=self._generation)

    def notify_tool_execution_started(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_tool_execution_started: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "tool_execution_started",
               "args": [str(a) for a in args],
               "kwargs": {k: str(v) for k, v in kwargs.items()}},
              gen=self._generation)

    def notify_step_confidence(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_step_confidence: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "step_confidence",
               "args": [str(a) for a in args],
               "kwargs": {k: str(v) for k, v in kwargs.items()}},
              gen=self._generation)

    def notify_task_completed(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_task_completed: args=%s kwargs=%s",
                        _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "task_completed",
               "args": [str(a) for a in args],
               "kwargs": {k: str(v) for k, v in kwargs.items()}},
              gen=self._generation)

    # InteractionManager.notify_task_completed routes through
    # _ui_call("show_task_completed", summary) (interaction_manager.py:554),
    # so we MUST expose show_task_completed by that exact name. The previous
    # build only had notify_task_completed, which was never invoked from the
    # engine — the renderer therefore never saw the completion event. Promote
    # the summary to a top-level field so the renderer can read it directly.
    def show_task_completed(self, summary: Any = "", *args: Any, **kwargs: Any) -> None:
        text = "" if summary is None else str(summary)
        _ui_logger.debug("show_task_completed: %s", _truncate(text))
        _emit({"type": "status", "kind": "task_completed",
               "summary": text}, gen=self._generation)

    def show_metrics_report(self, markdown: str) -> None:
        _ui_logger.debug("show_metrics_report: %s", _truncate(markdown))
        _emit({"type": "status", "kind": "metrics_report", "text": str(markdown)},
              gen=self._generation)

    def show_receptionist_thinking(self) -> None:
        _ui_logger.debug("show_receptionist_thinking")
        _emit({"type": "status", "kind": "receptionist_thinking_on"},
              gen=self._generation)

    def clear_receptionist_thinking(self) -> None:
        _ui_logger.debug("clear_receptionist_thinking")
        _emit({"type": "status", "kind": "receptionist_thinking_off"},
              gen=self._generation)

    def stream_receptionist_reply_chunk(self, text: str) -> None:
        _emit({"type": "status", "kind": "reply_delta", "text": str(text)},
              gen=self._generation)

    def seal_receptionist_reply(self) -> None:
        _emit({"type": "status", "kind": "reply_done"},
              gen=self._generation)

    # ── GEP countdown ──────────────────────────────────────────────────────
    # FlowController._planner_loop fires notify_gep_countdown(remaining, name)
    # once per second while waiting for the user to confirm a matched template.
    # remaining_secs == -1 clears the timer (templates declined / activated).
    def show_gep_countdown(self, remaining_secs: int, template_name: str) -> None:
        _emit({"type": "status", "kind": "gep_countdown",
               "remaining": int(remaining_secs),
               "template_name": str(template_name or "")},
              gen=self._generation)

    def show_gep_intro(self, template_info: Any) -> None:
        """One-shot structured template info; renderer opens the parameter panel."""
        info = template_info if isinstance(template_info, dict) else {}
        _emit({"type": "status", "kind": "gep_intro",
               "template": info},
              gen=self._generation)

    def show_inline_event(self, icon: str, desc: str) -> None:
        """Emit a one-line status event styled like a planner step.

        Used for short GEP-flow banners (saving / activated / instantiation
        failed) that should appear as a tight icon + text line, identical
        in style to step_started events. The renderer maps kind=inline_event
        to addStepBubble.
        """
        _emit({"type": "status", "kind": "inline_event",
               "icon": str(icon or "·"),
               "desc": str(desc or "")},
              gen=self._generation)

    # ── Desktop takeover indicator ──────────────────────────────────────────
    #
    # InteractionManager._ui_call invokes these via getattr() when the
    # desktop tool's input-action guard fires the start/end events. The
    # Electron main process (electron/main.js) listens for the resulting
    # status envelopes and shows / hides the fullscreen takeover overlay.
    # Without these methods the events would be silently dropped — which
    # was the bug pre-2026-05.
    def notify_desktop_takeover_started(self, reason: str = "input_action") -> None:
        _ui_logger.debug("notify_desktop_takeover_started: reason=%s", reason)
        # Pause the activity monitor while the desktop tool is driving
        # the screen — capture during takeover would (a) interleave with
        # the agent's own mouse / keyboard events, polluting OCR samples,
        # and (b) potentially capture content the user already approved
        # to share with a specific tool but not with LTM.
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

    # ── Interactive session events ─────────────────────────────────────────
    #
    # The session_tool emits lifecycle events (open/exec_done/close) through
    # InteractionManager._ui_call → getattr(ui, "notify_session_event").
    # The renderer uses these to render a live session monitor panel showing
    # which sessions are active, their last command, and output previews.
    def notify_session_event(self, event_name: str, data: Any = None) -> None:
        _ui_logger.debug("notify_session_event: %s data=%s", event_name, _truncate(data))
        _emit({"type": "status", "kind": "session_event",
               "event": str(event_name),
               "data": data if isinstance(data, dict) else {}},
              gen=self._generation)

    # ── Confirmation dialogs (UI delegate path) ──────────────────────────────
    #
    # InteractionManager.request_*_confirmation() probes the UI delegate
    # via getattr(...). When these methods are present, the IM defers to
    # them instead of falling back to its CLI blocking path (which reads
    # stdin — useless under Electron because stdin is the bridge's IPC
    # pipe, not a keyboard).
    #
    # Synchronisation: the IM has a generic `_pending_confirmation_callback`
    # slot it invokes when an inbound `user_input.kind=confirmation`
    # envelope arrives (stdio_bridge dispatcher → submit_confirmation_response
    # → _deliver_confirmation_response → callback). We install a closure
    # that captures the user's answer and unblocks a threading.Event;
    # the calling thread (RuntimeAgent._check_before_act, executed inside
    # an async task on the event loop thread) blocks on .wait() until
    # the renderer responds.
    #
    # Caller threading: these methods are called from the asyncio event
    # loop thread via a synchronous call chain (act → _execute_one →
    # _check_before_act → confirmation_callback → IM → _StdioUI). Blocking
    # the event loop is the existing CLI-path behaviour and is acceptable
    # here because no other agent work can proceed until the user answers.
    # The stdin reader thread (and inbound IPC dispatcher) keep running
    # because they live on separate threads / asyncio tasks.

    def _summarise_decision(self, decision: Decision) -> Dict[str, Any]:
        """Produce a compact, JSON-safe summary of a Decision for the modal.

        Keys: tool_calls (list of {tool_name, params_preview}), reasoning.
        Parameter values are truncated to 200 chars each — the dialog only
        needs enough context for the user to recognise what's about to run.
        """
        def _trunc(v: Any, n: int = 200) -> str:
            s = str(v)
            return s if len(s) <= n else s[:n] + "…"

        tool_calls: List[Dict[str, Any]] = []
        for tc in (decision.tool_calls or []):
            params_preview = {k: _trunc(v) for k, v in (tc.parameters or {}).items()}
            tool_calls.append({
                "tool_name": tc.tool_name,
                "params": params_preview,
            })
        return {
            "tool_calls": tool_calls,
            "reasoning": _trunc(decision.reasoning or "", 500),
        }

    def _await_user_response(
        self,
        kind: str,
        payload: Dict[str, Any],
        prompt_id: str,
    ) -> str:
        """Emit a confirmation envelope and block until the renderer responds.

        - Installs a callback into IM._pending_confirmation_callback.
        - Emits {"type":"status","kind":<kind>, "id":prompt_id, ...payload}.
        - Blocks on a threading.Event released by the callback.
        - Returns the raw answer string from the user.

        ``prompt_id`` is included so the renderer can correlate a response
        with its prompt; this matters when bursts of confirmations arrive.
        """
        event = threading.Event()
        holder: List[str] = []

        def _on_response(answer: str) -> None:
            holder.append(answer)
            event.set()

        # IM has a generic single-slot pending callback (it serialises
        # confirmations — there is never more than one in flight). Install
        # ours; IM will clear it after delivering the response.
        with self._im._pending_confirmation_lock:
            self._im._pending_confirmation_question = payload.get("description") or payload.get("prompt") or ""
            self._im._pending_confirmation_callback = _on_response

        env: Dict[str, Any] = {"type": "status", "kind": kind, "id": prompt_id}
        env.update(payload)
        _emit(env, gen=self._generation)
        _ui_logger.debug("await_user_response: kind=%s id=%s", kind, prompt_id)

        event.wait()
        return holder[0] if holder else ""

    def request_risk_confirmation(
        self, decision: Decision, risk_description: str
    ) -> UserConfirmation:
        """High-risk operation gate. Blocks until renderer returns yes/no/text."""
        prompt_id = f"risk-{int(time.time() * 1000)}-{id(decision) & 0xffff:04x}"
        payload = {
            "description": str(risk_description),
            "decision": self._summarise_decision(decision),
        }
        answer = self._await_user_response("risk_confirmation", payload, prompt_id)
        return self._im._resolve_confirmation(answer)

    def request_tool_confirmation(
        self, tool_name: str, decision: Decision
    ) -> UserConfirmation:
        """Tool-specific gate (write/edit/bash/...). Same shape as risk."""
        prompt_id = f"tool-{int(time.time() * 1000)}-{id(decision) & 0xffff:04x}"
        payload: Dict[str, Any] = {
            "tool": str(tool_name),
        }
        # Desktop is task-scoped — be loud about it in the modal so the
        # user knows a single yes covers every desktop action until task
        # end (or until they hit the Ctrl+Shift+C revoke). The renderer
        # uses ``description`` (and styles the card differently when
        # ``scope=='task'``).
        if str(tool_name) == "desktop":
            payload["scope"] = "task"
            payload["description"] = (
                "The agent is requesting control of your desktop "
                "(mouse / keyboard / screen capture). Approving grants "
                "full desktop access for the remainder of this task — "
                "every subsequent desktop action will run without "
                "asking again. Press Ctrl+Shift+C anytime to revoke."
            )
            # Task-scope approval covers ALL desktop actions, not just
            # the FIRST one that happened to fire. Showing
            # "action: list_windows" in the modal is misleading — it
            # implies the user is approving that specific call.
            # Drop tool_calls / params and keep only the LLM's
            # reasoning so the user has high-level context without
            # the per-action red herring.
            reasoning = (decision.reasoning or "").strip()
            if reasoning:
                payload["reasoning"] = reasoning if len(reasoning) <= 500 else reasoning[:500] + "..."
        else:
            # Per-action confirmations (browser / write / edit / shell)
            # genuinely scope to the specific call, so the modal needs
            # to show what is about to run.
            payload["decision"] = self._summarise_decision(decision)
        answer = self._await_user_response("tool_confirmation", payload, prompt_id)
        return self._im._resolve_confirmation(answer)

    def request_secret_input(self, prompt: str) -> str:
        """Hidden text input (passwords, SSH credentials).

        Returns the raw string entered by the user. The renderer is
        responsible for rendering an <input type=password> so the value
        is not displayed on screen.
        """
        prompt_id = f"secret-{int(time.time() * 1000)}"
        payload = {"prompt": str(prompt)}
        return self._await_user_response("secret_input", payload, prompt_id)

    def request_user_text(self, prompt: str) -> str:
        """Free-text input (ask_human tool — agent's clarifying question).

        Same plumbing as request_secret_input, but the renderer shows a
        non-masked text input and an "agent question" framing. Returns the
        raw string entered by the user (may be empty if they dismiss the
        modal).
        """
        prompt_id = f"ask-{int(time.time() * 1000)}"
        payload = {"prompt": str(prompt)}
        return self._await_user_response("ask_human", payload, prompt_id)


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
        self._flow: Optional[FlowController] = None
        self._flow_task: Optional[asyncio.Task] = None
        self._services: List[AnthropicStreamingService] = []

        # When a scheduled task triggered this flow, stash the id so
        # _run_flow_session can call scheduler.notify_task_finished
        # in its finally-block. Cleared as soon as the notify lands.
        self._pending_scheduled_task_id: Optional[str] = None

        # ── Scheduled-task lifecycle markers ────────────────────────────
        # _scheduled_running_id: set when a scheduled flow_task is in
        # flight. Read by _do_new_session (to detect user-new abort) and
        # by _after_flow_done (to know whether to notify the scheduler).
        # Cleared inside _after_flow_done.
        # _cancelled_scheduled_ids: ids that the user-new-abort path
        # already wrote CANCELLED for. _after_flow_done checks this
        # set so it doesn't overwrite CANCELLED with FAILED when the
        # done-callback fires after the cancellation.
        self._scheduled_running_id: Optional[str] = None
        self._cancelled_scheduled_ids: set[str] = set()

        # Session generation. Bumped by _do_new_session before the new
        # singleton is constructed, so a fresh _StdioUI with the new
        # generation drives the new flow while the OLD _StdioUI (still
        # referenced by the old IM via the old FlowController) keeps
        # emitting with its captured OLD generation. The renderer drops
        # any envelope whose gen is older than its current generation,
        # which is what isolates the new conversation from a wedged
        # old subtask that may keep emitting until its blocking syscall
        # finally returns (Windows: no portable thread kill).
        self._generation: int = 0

        # Construct IM first so _StdioUI can hold a reference to it; the
        # confirmation-dialog methods (request_risk_confirmation etc.) need
        # to install a pending callback into IM so inbound user_input
        # envelopes can unblock the waiter.
        im = InteractionManager.get_instance()
        self._ui = _StdioUI(self._generation, im=im)
        im.set_ui(self._ui)
        self._im = im

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
                # _StdioUI.{request_risk,request_tool,request_secret}_confirmation
                # blocks the event loop on threading.Event.wait() while waiting
                # for the user. If we routed the answer through the asyncio
                # inbox, the dispatcher coroutine couldn't run (loop blocked) —
                # the answer would queue up forever. Resolve it directly from
                # this thread instead, which mirrors the IM CLI fallback's
                # daemon-stdin-thread → _confirmation_queue model.
                if (isinstance(obj, dict)
                        and obj.get("type") == "user_input"
                        and obj.get("kind") == "confirmation"):
                    try:
                        self._im.submit_confirmation_response(
                            str(obj.get("answer", ""))
                        )
                    except Exception:
                        logger.exception(
                            "stdin reader: submit_confirmation_response failed"
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
                    eid = str(msg.get("id") or "")
                    kind_raw = str(msg.get("kind") or "")
                    if not eid or not kind_raw:
                        raise ValueError("ltm_archive: id and kind required")
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
            "cron_list", "cron_create", "cron_update",
            "cron_delete", "cron_set_enabled", "cron_run_now",
            "cron_validate",
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

        # ── /remember + git post-commit hook install ────────────────────
        if msg_type in (
            "ltm_remember", "ltm_install_git_hook", "ltm_uninstall_git_hook",
        ):
            try:
                result = await self._handle_ltm_aux(msg_type, msg)
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
                self._ensure_flow(goal=str(goal))
                self._im.inject_user_message(str(goal))
                if self._flow_task is None or self._flow_task.done():
                    assert self._flow is not None
                    self._flow_task = asyncio.create_task(
                        self._run_flow_session(msg_id)
                    )
                    # External observer for schedule cleanup. Fires
                    # AFTER the task has fully completed, so the cleanup
                    # body (notify_task_finished + auto-new) runs in a
                    # fresh asyncio task — never inside the flow task
                    # itself, eliminating the finally-from-self deadlock.
                    self._flow_task.add_done_callback(self._on_flow_task_done)
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
                    self._im.inject_user_message(str(msg.get("text", "")))
                elif kind == "confirmation":
                    self._im.submit_confirmation_response(str(msg.get("answer", "")))
                elif kind == "desktop_takeover_revoked":
                    # Frontend overlay's revoke hotkey (Ctrl+C or
                    # equivalent) sends this. We flip the takeover flag
                    # so subsequent input actions refuse for the rest
                    # of this task; read-only desktop actions stay
                    # available. The notify_desktop_takeover_ended
                    # event with reason='user_revoked' is emitted by
                    # revoke_takeover() itself.
                    try:
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

        if msg_type == "gep_save":
            await self._do_gep_save(msg_id, msg)
            return

        if msg_type == "gep_list_templates":
            try:
                from src.infrastructure.gep_template import list_templates
                templates = list_templates(include_invalid=True)
                payload = []
                for t in templates:
                    entry = {
                        "id":          t.id,
                        "name":        t.name,
                        "description": t.description,
                        "version":     t.version,
                        "created_at":  t.created_at,
                        "params":      [
                            {
                                "name":        pname,
                                "type":        getattr(pspec, "type", "") or "",
                                "description": getattr(pspec, "description", "") or "",
                                "default":     getattr(pspec, "default", None),
                                "emphasis":    bool(getattr(pspec, "emphasis", False)),
                            }
                            for pname, pspec in (t.params_schema or {}).items()
                        ],
                        "steps": [
                            {
                                "step_id":     s.step_id,
                                "description": s.description,
                                "goal":        s.goal,
                                "tools_required": list(s.tools_required or []),
                            }
                            for s in (t.guide_steps or [])
                        ],
                    }
                    problems = getattr(t, "_problems", None)
                    if problems:
                        entry["problems"] = list(problems)
                        entry["source_path"] = getattr(t, "_source_path", "")
                    payload.append(entry)
                _emit({"type": "final", "id": msg_id,
                       "result": {"templates": payload}},
                      gen=self._generation)
            except Exception as exc:
                logger.exception("gep_list_templates failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"gep_list_templates failed: {exc}",
                       "fatal": False}, gen=self._generation)
            return

        if msg_type == "gep_delete_template":
            try:
                from src.infrastructure.gep_template import (
                    _templates_dir, _sanitize_template_id,
                )
                tid = str(msg.get("id") or "").strip()
                if not tid:
                    raise ValueError("missing template id")
                tdir = _templates_dir()
                target = tdir / f"{_sanitize_template_id(tid)}.json"
                if target.exists():
                    target.unlink()
                    ok = True
                else:
                    matches = list(tdir.glob(f"{_sanitize_template_id(tid)}*.json"))
                    if len(matches) == 1:
                        matches[0].unlink()
                        ok = True
                    else:
                        ok = False
                _emit({"type": "final", "id": msg_id,
                       "result": {"ok": ok}}, gen=self._generation)
            except Exception as exc:
                logger.exception("gep_delete_template failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"gep_delete_template failed: {exc}",
                       "fatal": False}, gen=self._generation)
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
                if not schedule_str:
                    from src.infrastructure.scheduler.inferer import infer_schedule
                    # Single-use LLM service built from current config —
                    # see inferer.py module docstring for rationale.
                    config = self._load_config_dict()
                    schedule_str = await infer_schedule(prompt_str, config)
                t = await scheduler.create_task(  # type: ignore[union-attr]
                    name=str(msg.get("name", "")),
                    prompt=prompt_str,
                    schedule=schedule_str,
                )
            except ScheduleSyntaxError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "task": t}

        if msg_type == "cron_update":
            tid = str(msg.get("id") or "")
            if not tid:
                return {"ok": False, "error": "missing id"}
            try:
                t = await scheduler.update_task(  # type: ignore[union-attr]
                    tid,
                    name=msg.get("name"),
                    prompt=msg.get("prompt"),
                    schedule=msg.get("schedule"),
                )
            except ScheduleSyntaxError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": bool(t), "task": t}

        if msg_type == "cron_delete":
            tid = str(msg.get("id") or "")
            ok = await scheduler.delete_task(tid)  # type: ignore[union-attr]
            return {"ok": bool(ok)}

        if msg_type == "cron_set_enabled":
            tid = str(msg.get("id") or "")
            t = await scheduler.set_enabled(  # type: ignore[union-attr]
                tid, bool(msg.get("enabled", False)),
            )
            return {"ok": bool(t), "task": t}

        if msg_type == "cron_run_now":
            tid = str(msg.get("id") or "")
            t = await scheduler.run_now(tid)  # type: ignore[union-attr]
            return {"ok": bool(t), "task": t}

        if msg_type == "cron_validate":
            from src.infrastructure.scheduler import Scheduler as _S
            try:
                _S.validate_schedule(str(msg.get("schedule", "")))
                return {"ok": True}
            except ScheduleSyntaxError as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"unknown cron op: {msg_type}"}

    # ------------------------------------------------------------------
    # /remember + git post-commit hook IPC
    # ------------------------------------------------------------------

    async def _handle_ltm_aux(self, msg_type: str, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the auxiliary LTM IPC envelopes that don't fit the
        list/archive/stats group:

          - ``ltm_remember`` — explicit /remember command. Submits a
            high-trust MANUAL_REMEMBER candidate.
          - ``ltm_install_git_hook`` — copy
            ``scripts/handq_post_commit.py`` into a target repo's
            ``.git/hooks/post-commit`` (chmod +x on POSIX). Returns the
            installed path on success.
          - ``ltm_uninstall_git_hook`` — delete the hook file IF and
            only if it's the one we installed (heuristic: head line
            matches our shebang + module docstring marker).

        These are bundled in one method because they all sit at the
        same trust boundary (admin-grade operations on the user's
        memory.db / git workspaces) and share error-handling.
        """
        if msg_type == "ltm_remember":
            text = str(msg.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "text is required"}
            try:
                from src.infrastructure.long_term_memory import LongTermMemory
                from src.infrastructure.long_term_memory.candidates import (
                    submit_manual,
                )
                ltm = LongTermMemory.get()
                ref = str(msg.get("ref") or "")
                cid = await submit_manual(ltm=ltm, text=text, ref=ref)
                return {"ok": bool(cid), "candidate_id": cid}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if msg_type == "ltm_install_git_hook":
            repo_path = str(msg.get("repo") or "").strip()
            if not repo_path:
                return {"ok": False, "error": "repo path required"}
            return await asyncio.to_thread(
                _install_post_commit_hook, repo_path,
            )

        if msg_type == "ltm_uninstall_git_hook":
            repo_path = str(msg.get("repo") or "").strip()
            if not repo_path:
                return {"ok": False, "error": "repo path required"}
            return await asyncio.to_thread(
                _uninstall_post_commit_hook, repo_path,
            )

        return {"ok": False, "error": f"unknown op: {msg_type}"}

    # ------------------------------------------------------------------
    # Scheduler dispatch — bridge-side hook called from
    # stdio_bridge.dispatch_scheduled_task().
    # ------------------------------------------------------------------

    async def accept_scheduled_task(self, task) -> bool:  # type: ignore[no-untyped-def]
        """Decide whether to fire *task* now.

        Returns True if the task is being dispatched, False if the
        bridge declined (busy / shutdown). The scheduler reads the
        return value to decide whether to bump the next-fire timestamp.
        """
        if self._shutdown_requested:
            logger.info(
                "scheduler dispatch refused: shutdown in progress task=%s",
                task.id[:8],
            )
            return False
        if self._flow_task is not None and not self._flow_task.done():
            logger.info(
                "scheduler dispatch refused: bridge busy task=%s "
                "(scheduler will mark task PENDING)",
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

        # Treat the firing exactly like an inbound `request` envelope.
        # Stamp the message id with a marker the renderer can match
        # against the scheduled_task_started toast.
        msg_id = f"sched-{task.id}-{int(time.time())}"
        synthetic = {
            "type": "request",
            "id": msg_id,
            "goal": task.prompt,
            "scheduled": True,
            "scheduled_task_id": task.id,
        }
        try:
            await self._handle(synthetic)
        except Exception as exc:
            logger.exception("scheduler synthetic _handle crashed")
            try:
                if scheduler is not None:
                    await scheduler.notify_task_finished(
                        task.id, ok=False, error=f"dispatch crashed: {exc}",
                    )
            except Exception:
                logger.exception("scheduler notify_task_finished crashed")
            return True  # already accepted; failure is recorded
        # The actual flow runs asynchronously in self._flow_task. Its
        # finally-block calls notify_task_finished; we attach that hook
        # by stashing the task id on the bridge so _run_flow_session
        # can pick it up.
        self._pending_scheduled_task_id = task.id
        return True



    def _ensure_flow(self, goal: str) -> None:
        if self._flow is not None:
            return
        from ..infrastructure.role_resolver import resolve_role_models

        cm = ConfigManager(str(self.config_path))
        cfg = cm.get_config()
        llm_cfg = cfg.get("llm", {}) or {}
        sess_cfg = cfg.get("session", {}) or {}

        # Allocate this session's directory under %USERPROFILE%\HandQ\History\.
        # Both `working_directory` and `storage_directory` point at it — the
        # legacy distinction (CLI-era: cwd-of-invocation vs. workspace_base/<id>)
        # is meaningless in the GUI, so we collapse them. All artifacts the
        # agent writes via relative paths land here and are auto-scoped to the
        # session.
        session_dir = _allocate_session_dir(goal)

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
        from ..infrastructure.logger import initialize_logger, LogLevel as _LogLevel
        _log_level_str = sess_cfg.get("log_level", "INFO") or "INFO"
        _engine_log_dir = str(session_dir)
        try:
            initialize_logger(
                name="HandQ",
                level=_LogLevel[_log_level_str.upper()],
                log_file="handq-engine.log",
                log_dir=_engine_log_dir,
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
        # capping every model at a legacy default (e.g. 4096 truncates Sonnet/Haiku
        # mid-tool-call, breaking write/edit with "missing required parameter").
        _mt_raw = llm_cfg.get("max_tokens")
        try:
            _mt_int = int(_mt_raw) if _mt_raw is not None else 0
        except (TypeError, ValueError):
            _mt_int = 0
        max_tokens: Optional[int] = _mt_int if _mt_int > 0 else None
        roles = resolve_role_models(llm_cfg)

        # Fallback when both `roles` and `models` are absent — keep the bridge
        # bootable so the user can open Settings and configure models from the UI.
        if not any(roles.values()):
            roles = {
                "agent":        ["anthropic::claude-4-5-haiku"],
                "planner":      ["anthropic::claude-4-5-haiku"],
                "receptionist": ["anthropic::claude-4-5-haiku"],
                "from_data":    ["anthropic::claude-4-5-haiku"],
            }

        logger.debug(
            "FlowController lazy construction: top_level_keys=%s llm_keys=%s session_keys=%s "
            "roles={agent:%d, planner:%d, receptionist:%d, from_data:%d} "
            "max_tokens=%s api_key_present=%s session_dir=%s",
            sorted(cfg.keys()) if isinstance(cfg, dict) else None,
            sorted(llm_cfg.keys()),
            sorted(sess_cfg.keys()),
            len(roles["agent"]), len(roles["planner"]),
            len(roles["receptionist"]), len(roles["from_data"]),
            max_tokens if max_tokens is not None else "auto(per-model ceiling)",
            bool(api_key),
            session_dir,
        )
        if not api_key:
            logger.warning("llm.API_KEY is empty in config; LLM calls will fail")

        # Shared services for non-planner roles, dedup'd. Planner gets dedicated
        # max_retries=50 instances to match the CLI behavior.
        shared_models: list = []
        for role_key in ("agent", "receptionist", "from_data"):
            for m in roles.get(role_key, []):
                if m not in shared_models:
                    shared_models.append(m)
        # Only forward max_tokens when explicitly configured; otherwise let
        # AnthropicStreamingService pick its constructor default and per-model
        # ceiling kick in via _resolve_max_tokens.
        _mt_kwargs: dict = {"max_tokens": max_tokens} if max_tokens is not None else {}
        svc_map = {
            m: AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=3, **_mt_kwargs,
            )
            for m in shared_models
        }
        agent_services        = [svc_map[m] for m in roles.get("agent", [])]
        receptionist_services = [svc_map[m] for m in roles.get("receptionist", [])]
        from_data_services    = [svc_map[m] for m in roles.get("from_data", [])]
        planner_services      = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_retries=50, **_mt_kwargs,
            )
            for m in roles.get("planner", [])
        ]

        # Track every distinct service for shutdown.
        self._services = list(svc_map.values()) + planner_services

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

        self._flow = FlowController(
            agent_llm_services=agent_services,
            planner_llm_services=planner_services,
            receptionist_llm_services=receptionist_services,
            from_data_llm_services=from_data_services,
            working_directory=None,
            storage_directory=str(session_dir),
            step_verification_threshold=float(
                sess_cfg.get("step_verification_threshold", 0.7)
            ),
            venv_path=sess_cfg.get("venv_path"),
            config_path=str(self.config_path),
        )
        logger.info(
            "FlowController constructed; %d total service(s) (agent=%d planner=%d receptionist=%d from_data=%d)",
            len(self._services),
            len(agent_services), len(planner_services),
            len(receptionist_services), len(from_data_services),
        )

    async def _run_flow_session(self, msg_id: Optional[str]) -> None:
        assert self._flow is not None
        # Snapshot the generation at task creation. If a new_session
        # happens mid-flight, this task continues to emit with its OLD
        # generation — the renderer drops those, isolating the new
        # session's conversation from this orphan's tail.
        gen = self._generation
        # Snapshot the scheduled-task id (if any). The eager-clear of
        # _pending_scheduled_task_id keeps it as a one-tick handoff
        # token; the long-lived marker is _scheduled_running_id, which
        # is read by _after_flow_done after this coroutine exits.
        sched_id = self._pending_scheduled_task_id
        self._pending_scheduled_task_id = None
        if sched_id is not None:
            self._scheduled_running_id = sched_id
        logger.info("flow session starting; id=%s gen=%d sched=%s",
                    msg_id, gen, sched_id)
        try:
            result = await self._flow.start_idle_session()
            logger.info("flow session completed; id=%s gen=%d", msg_id, gen)
            _emit({"type": "final", "id": msg_id, "result": result}, gen=gen)
        except asyncio.CancelledError:
            logger.info("flow session cancelled; id=%s gen=%d", msg_id, gen)
            # Re-raise so task.cancelled() reports True; _after_flow_done
            # uses that to set ok=False/error="cancelled".
            raise
        except Exception as exc:
            logger.exception("flow session crashed; id=%s gen=%d", msg_id, gen)
            _emit({"type": "error", "id": msg_id, "where": "engine",
                   "message": f"start_idle_session failed: {exc}",
                   "fatal": True}, gen=gen)
        # NOTE: schedule-related cleanup (notify_task_finished, auto-new,
        # scheduler wakeup) is NOT done here. It runs in _after_flow_done
        # via add_done_callback so it executes strictly after this task
        # has fully completed — keeping the flow internals pure and
        # making finally-from-self deadlocks unreachable.

    # ------------------------------------------------------------------
    # External post-flow observer — handles all schedule-related cleanup.
    #
    # The done-callback chain:
    #   _flow_task finishes → asyncio fires _on_flow_task_done (sync) →
    #   schedules _after_flow_done as a fresh asyncio.Task → that task
    #   runs *outside* _flow_task (which is already done), so anything
    #   it awaits — including _do_new_session's wait_for(_flow_task) —
    #   short-circuits immediately. No deadlock path.
    # ------------------------------------------------------------------

    def _on_flow_task_done(self, task: "asyncio.Task[Any]") -> None:
        """Sync done-callback. asyncio invokes this AFTER the task's
        coroutine has fully returned, so we are firmly outside the
        flow_task. Done-callbacks must not block the loop, so we just
        schedule the actual work as a fresh task."""
        try:
            asyncio.create_task(
                self._after_flow_done(task),
                name="after-flow-done",
            )
        except Exception:
            logger.exception("_on_flow_task_done failed to schedule cleanup")

    async def _after_flow_done(self, task: "asyncio.Task[Any]") -> None:
        """External cleanup that runs strictly after _flow_task is done.

        Three responsibilities, all schedule-related:
          1. Wake the scheduler so PENDING tasks get re-scanned now
             that bridge is idle (applies to every flow, not only
             scheduled ones — a user task finishing also unblocks
             pending schedules).
          2. Notify scheduler of the scheduled task's outcome.
          3. Trigger auto-new so the next scheduled fire starts on a
             clean _flow / IM / tool state.

        Two early-exit paths short-circuit (2) + (3):
          * sid is None — plain user task, scheduler is uninvolved.
          * sid in _cancelled_scheduled_ids — the user-new path already
            wrote CANCELLED and is itself running _do_new_session;
            doing it again here would just bump the generation an
            extra time and double-reset the IM.

        The flow itself owns NONE of this — _run_flow_session stays
        pure business logic.
        """
        sid = self._scheduled_running_id
        self._scheduled_running_id = None

        # (1) Wake scheduler — runs for every flow.
        if scheduler is not None:
            try:
                scheduler._wakeup.set()
            except Exception:
                pass

        if sid is None:
            return  # plain user task

        if sid in self._cancelled_scheduled_ids:
            # User-new abort path: _do_new_session is already in flight
            # on the caller side. CANCELLED is already written; auto-new
            # is already happening. Just clear the marker and exit.
            self._cancelled_scheduled_ids.discard(sid)
            return

        # (2) Real scheduled completion — notify outcome.
        ok = (not task.cancelled()) and task.exception() is None
        err = ""
        if task.cancelled():
            err = "cancelled"
        elif task.exception() is not None:
            err = str(task.exception())[:500]
        if scheduler is not None:
            try:
                await scheduler.notify_task_finished(
                    sid, ok=ok, error=err,
                )
            except Exception:
                logger.exception("scheduler notify_task_finished failed")

        # (3) Auto-new so the next scheduled fire starts clean. Runs in
        # this fresh task; _flow_task.done() is True, so _do_new_session's
        # wait_for(flow_task) returns immediately.
        try:
            await self._do_new_session(msg_id=None, _suppress_final=True)
        except Exception:
            logger.exception("auto-new after scheduled flow failed")

    # ------------------------------------------------------------------
    # New-session chain — equivalent to `handq new`. Designed for three
    # invariants:
    #
    #  (1) BOUNDED — never block the bridge longer than ~6s total. The
    #      renderer's New button is fire-and-forget; if cleanup stalls,
    #      the user can still type the next goal and it sits in the
    #      stdin queue until cleanup finishes.
    #  (2) NO LEAKS — release the FlowController graph, the per-service
    #      httpx connection pools, and the InteractionManager singleton
    #      so the next `request` builds against fresh state.
    #  (3) NO ORPHAN SUBPROCESSES on Windows. The bash tool spawns child
    #      processes with CREATE_NEW_PROCESS_GROUP and watches
    #      _interrupt_event to kill the whole tree. We MUST drive the
    #      shutdown through that interrupt path FIRST — hard-cancelling
    #      the flow task while bash_tool is parked in `asyncio.wait(
    #      [communicate_task, interrupt_task])` raises CancelledError on
    #      the wait but does NOT cancel its child tasks, leaving
    #      `process.communicate()` orphaned with a live subprocess.
    #
    # Stragglers: if a child task ignores cancellation entirely (rare —
    # would have to be wedged in a C-level syscall that doesn't honor
    # asyncio's cancel), it survives as a background coroutine. It can
    # emit a short tail of status envelopes before its underlying I/O
    # finally times out. The renderer just renders them as live events;
    # not a correctness issue, just cosmetic.
    # ------------------------------------------------------------------

    # Cleanup budget. Bridge total stall ≤ GRACE + HARD + CLOSE * len(services)
    # — for the default 4 services, ≤ ~12s worst case, ≤ ~2.5s typical.
    _NEW_SESSION_GRACE_TIMEOUT = 2.5    # cooperative interrupt
    _NEW_SESSION_HARD_TIMEOUT  = 1.5    # after explicit cancel
    _NEW_SESSION_CLOSE_TIMEOUT = 2.0    # per-service httpx pool drain

    async def _do_gep_save(
        self, msg_id: Optional[str], msg: Dict[str, Any],
    ) -> None:
        """Trigger the GEP save-session flow.

        Sole entry point for generating a template — invoked only by the
        Templates panel's "Load history" file picker, which always passes
        ``log_file``. The frontend already gates on idle, but this method
        is the single source of truth for the server-side rules:

          (a) NO task may be currently executing or replanning. We refuse
              outright; we no longer cancel a live task to make room for
              save (that was the old behaviour back when Save lived in the
              completion bubble — nonsensical now that the user has to
              navigate to Templates → Load history, which itself implies
              "I'm done with whatever I was doing").
          (b) A bootstrap FlowController is built on demand if none exists
              yet, so the save flow works even before the user has sent
              a single message in the session.

        The save flow (``flow._trigger_save_session``) then constructs its
        own fresh FlowController and takes over the InteractionManager —
        the conversation pane becomes the template-generation chat.
        """
        from src.models.state import SystemState

        log_file = msg.get("log_file") or None

        # ── (a) hard refuse if a task is in flight ───────────────────────
        if self._flow is not None and self._flow.state in (
            SystemState.EXECUTING, SystemState.REPLANNING,
        ):
            _emit({"type": "error", "id": msg_id, "where": "bridge",
                   "message": "GEP save: a task is currently running — wait for "
                              "completion or click New first.",
                   "fatal": False}, gen=self._generation)
            return

        # If the bridge already has a live flow_task whose state isn't
        # IDLE/COMPLETED, treat that as "task running" too. Defensive: covers
        # transient windows where state hasn't transitioned yet but the task
        # is mid-execution.
        if (
            self._flow_task is not None
            and not self._flow_task.done()
            and self._flow is not None
            and self._flow.state not in (SystemState.IDLE, SystemState.COMPLETED)
        ):
            _emit({"type": "error", "id": msg_id, "where": "bridge",
                   "message": "GEP save: a task is currently running — wait for "
                              "completion or click New first.",
                   "fatal": False}, gen=self._generation)
            return

        # ── No log_file + no execution recorder = nothing to save ────────
        # The Templates panel always supplies log_file, so this guard mostly
        # catches programmatic callers that omit it.
        if (
            log_file is None
            and (self._flow is None or self._flow._execution_recorder is None)
        ):
            _emit({"type": "error", "id": msg_id, "where": "bridge",
                   "message": "GEP save: log_file is required when no completed "
                              "task exists in the current session.",
                   "fatal": False}, gen=self._generation)
            return

        # ── (b) bootstrap a parent flow if none exists ───────────────────
        # _trigger_save_session builds its own save_flow but borrows the
        # parent for config_manager / services / storage_directory. The
        # bootstrap goal text is only used by _allocate_session_dir to slug
        # the placeholder dir.
        if self._flow is None:
            try:
                self._ensure_flow(goal="gep-save-bootstrap")
            except Exception as exc:
                logger.exception("gep_save: _ensure_flow failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"GEP save: could not initialise bridge "
                                  f"flow: {exc}", "fatal": False},
                      gen=self._generation)
                return

        flow = self._flow

        # If there's a stale idle flow_task (planner_loop waiting for next
        # message), gracefully cancel it so the save flow takes the IM
        # singleton without contention. Safe because we already verified
        # state is IDLE/COMPLETED above.
        if self._flow_task is not None and not self._flow_task.done():
            self._flow_task.cancel()
            try:
                flow.cancel_all_tasks()
            except Exception:
                logger.exception("gep_save: cancel_all_tasks raised")
            try:
                await asyncio.wait({self._flow_task}, timeout=3.0)
            except Exception:
                pass

        # Run the save flow as a new task; it manages its own lifecycle.
        save_task = asyncio.create_task(
            flow._trigger_save_session(log_file=log_file),
            name=f"handq-gep-save-{int(time.time())}",
        )
        # Bind the bridge's flow_task slot so subsequent gep_save / new_session
        # calls see "task running" until the save flow concludes.
        self._flow_task = save_task

        _emit({"type": "final", "id": msg_id,
               "result": {"started": True}},
              gen=self._generation)
        logger.info("gep_save: started save-session flow id=%s log_file=%s",
                    msg_id, log_file)

    async def _do_new_session(
        self, msg_id: Optional[str], *, _suppress_final: bool = False,
    ) -> None:
        # User-initiated new during a scheduled fire: tell the scheduler
        # the task was cancelled BEFORE we tear anything down. Marker
        # added to _cancelled_scheduled_ids so _after_flow_done (which
        # will fire when we cancel _flow_task below) skips its own
        # notify_task_finished and doesn't overwrite CANCELLED → FAILED.
        # We use count_as_failure=False because user-cancellations are
        # not real task failures and shouldn't trip the auto-disable
        # counter.
        if (
            self._scheduled_running_id is not None
            and scheduler is not None
        ):
            sid = self._scheduled_running_id
            try:
                await scheduler.notify_task_finished(
                    sid, ok=False,
                    error="cancelled by user new_session",
                    count_as_failure=False,
                )
                self._cancelled_scheduled_ids.add(sid)
            except Exception:
                logger.exception("scheduler cancel-notify failed")

        # Bump the generation BEFORE doing any cleanup, then construct a
        # fresh _StdioUI bound to the new gen. The OLD _StdioUI is NOT
        # mutated — it stays referenced by the OLD InteractionManager
        # (which is still referenced by the old FlowController), so any
        # straggler `notify_*` call from a wedged old subtask continues
        # to emit through the OLD _StdioUI with the OLD generation. The
        # renderer's gen-watermark drops those.
        old_gen = self._generation
        self._generation = old_gen + 1
        new_gen = self._generation
        new_ui = _StdioUI(new_gen)
        logger.info("new_session sequence begin; id=%s old_gen=%d new_gen=%d suppress_final=%s",
                    msg_id, old_gen, new_gen, _suppress_final)
        t0 = time.monotonic()

        # Snapshot + clear up-front. Any later in-flight `_emit` from
        # an orphaned subtask can still write to stdout (the IPC stream
        # is process-wide and unconditionally available), but it can no
        # longer alias state on `self`. Crucially, a request that arrives
        # next tick sees `_flow is None` and rebuilds from scratch.
        flow = self._flow
        flow_task = self._flow_task
        services = self._services
        self._flow = None
        self._flow_task = None
        self._services = []

        try:
            # ── 1. Cooperative shutdown ──────────────────────────────────
            # Set _interrupt_event before cancelling anything. The
            # engine's broadcast loop forwards it to per-agent events,
            # which the bash tool listens for and uses to call
            # _kill_process_tree (TerminateProcess on Windows). On Unix
            # it sends SIGTERM to the process group via start_new_session.
            if flow is not None:
                try:
                    flow._interrupt_event.set()
                except Exception:
                    logger.warning(
                        "new_session: interrupt_event.set failed",
                        exc_info=True,
                    )
                try:
                    flow.cancel_all_tasks()
                except Exception:
                    logger.warning(
                        "new_session: cancel_all_tasks failed",
                        exc_info=True,
                    )

            # ── 2. Bounded drain ─────────────────────────────────────────
            # asyncio.shield prevents wait_for from cancelling the task
            # on timeout, so we control the cancel cascade ourselves.
            # First wait for cooperative shutdown (engine sees the
            # interrupt and exits cleanly). Only escalate to a hard
            # cancel if it doesn't finish in time, since hard cancels
            # can orphan Windows subprocesses (see header).
            #
            # Honest limit: if the old flow is parked in
            # `loop.run_in_executor(blocking_io)` (e.g. ssh_tool retry
            # backoff, ssh_setup getpass), task.cancel() cancels the
            # asyncio future but the OS thread keeps running until the
            # syscall returns. Python on Windows has no portable way
            # to kill that thread. We accept the orphan and rely on
            # the generation tag to keep its eventual emits out of the
            # new conversation.
            if flow_task is not None and not flow_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(flow_task),
                        timeout=self._NEW_SESSION_GRACE_TIMEOUT,
                    )
                    logger.info(
                        "new_session: cooperative drain ok (%.2f ms)",
                        (time.monotonic() - t0) * 1000.0,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "new_session: cooperative drain expired after %.1fs; "
                        "escalating to cancel",
                        self._NEW_SESSION_GRACE_TIMEOUT,
                    )
                    flow_task.cancel()
                    try:
                        await asyncio.wait_for(
                            flow_task,
                            timeout=self._NEW_SESSION_HARD_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "new_session: flow_task did not drain after "
                            "cancel within %.1fs; leaving as background "
                            "(gen=%d). Stragglers will be filtered by the "
                            "renderer's generation watermark.",
                            self._NEW_SESSION_HARD_TIMEOUT, old_gen,
                        )
                    except (asyncio.CancelledError, Exception):
                        pass
                except (asyncio.CancelledError, Exception):
                    pass

            # ── 3. Drain HTTP connection pools ───────────────────────────
            # AnthropicStreamingService.close() awaits the bundled httpx
            # client's aclose(). On Windows a half-open TCP socket can
            # stall this for the full keepalive window, so cap it.
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

            # ── 4. Reset the InteractionManager singleton ────────────────
            # reset_instance() drops the cls._instance ref. The old
            # instance's daemon stdin thread already EOF'd at boot
            # (bridge_main.py redirects stdin → /dev/null), so no thread
            # leak. Attach the FRESH _StdioUI (bound to new_gen) so the
            # next FlowController's notify_* / show_* events route here
            # with the new generation.
            try:
                InteractionManager.reset_instance()
            except Exception:
                logger.warning(
                    "new_session: reset_instance failed", exc_info=True,
                )
            self._im = InteractionManager.get_instance()
            # Bind the fresh IM into the new UI so its confirmation
            # methods (request_risk_confirmation etc.) install pending
            # callbacks into the right IM instance.
            new_ui._im = self._im
            self._ui = new_ui
            self._im.set_ui(self._ui)

            # ── 5. Drop process-wide singleton state held by tools ───────
            # FileState (read-before-write tracker) and the SSH connection
            # pool both live at module level, so they survive the bridge's
            # FlowController reset. If we don't flush them, the new flow
            # could:
            #   * Find an OLD pooled paramiko client (possibly closed by
            #     our force_terminate fire) and try to use it.
            #   * Treat a file as "already read this session" because the
            #     OLD flow read it — bypassing the staleness gate.
            # Both are silent correctness bugs across new_session, so we
            # always flush.
            try:
                from ..tools.file_state import FileState as _FileState
                _FileState.reset_for_session()
                logger.info("new_session: FileState cleared")
            except Exception:
                logger.warning("new_session: FileState reset failed", exc_info=True)
            try:
                from ..tools.ssh_tool import flush_connection_pool as _flush_ssh
                closed = _flush_ssh()
                logger.info("new_session: SSH pool flushed (%d clients closed)", closed)
            except Exception:
                logger.warning("new_session: SSH pool flush failed", exc_info=True)
            try:
                from ..tools.browser_tool import flush_browser_pool as _flush_browser
                browser_closed = await _flush_browser()
                logger.info(
                    "new_session: browser pool flushed (%d sessions closed)",
                    browser_closed,
                )
            except Exception:
                logger.warning("new_session: browser pool flush failed", exc_info=True)
            try:
                from ..tools.session_tool import flush_session_pool as _flush_sessions
                session_result = await _flush_sessions()
                if session_result:
                    logger.info("new_session: %s", session_result)
            except Exception:
                logger.warning("new_session: session pool flush failed", exc_info=True)
            try:
                # Drop the desktop takeover memo so the next task starts
                # unapproved. If the OLD task still had the overlay up,
                # this emits notify_desktop_takeover_ended via the NEW
                # IM/UI (we already rebuilt them above), which the
                # Electron main process listens to and uses to close the
                # overlay window. Keep this AFTER the IM reset so the
                # event routes through the new UI.
                from ..tools.desktop_tool import reset_takeover_state as _reset_takeover
                _reset_takeover()
                logger.info("new_session: desktop takeover state reset")
            except Exception:
                logger.warning("new_session: desktop takeover reset failed", exc_info=True)
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
                    self._flow._interrupt_event.set()
                    logger.info("shutdown: interrupt_event.set OK (%.2f ms)", _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: interrupt_event.set failed (%.2f ms)",
                                   _step_ms(t0), exc_info=True)
                t0 = time.monotonic()
                try:
                    self._flow.cancel_all_tasks()
                    logger.info("shutdown: cancel_all_tasks OK (%.2f ms)", _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: cancel_all_tasks failed (%.2f ms)",
                                   _step_ms(t0), exc_info=True)
            else:
                logger.info("shutdown: no FlowController to interrupt/cancel")

            if self._flow_task is not None and not self._flow_task.done():
                t0 = time.monotonic()
                self._flow_task.cancel()
                try:
                    await self._flow_task
                    logger.info("shutdown: await flow_task OK (%.2f ms)", _step_ms(t0))
                except (asyncio.CancelledError, Exception):
                    logger.info("shutdown: await flow_task drained with exception (%.2f ms)",
                                _step_ms(t0), exc_info=True)
            else:
                logger.info("shutdown: no live flow_task to await")

            for i, svc in enumerate(self._services):
                t0 = time.monotonic()
                try:
                    await svc.close()
                    logger.info("shutdown: svc[%d].close OK (%.2f ms)", i, _step_ms(t0))
                except Exception:
                    logger.warning("shutdown: svc[%d].close failed (%.2f ms)",
                                   i, _step_ms(t0), exc_info=True)

            t0 = time.monotonic()
            try:
                InteractionManager.reset_instance()
                logger.info("shutdown: InteractionManager.reset_instance OK (%.2f ms)", _step_ms(t0))
            except Exception:
                logger.warning("shutdown: InteractionManager.reset_instance failed (%.2f ms)",
                               _step_ms(t0), exc_info=True)
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
