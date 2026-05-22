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
from src.infrastructure.anthropic_streaming_service import (  # noqa: F401
    AnthropicStreamingService,
    StreamTextDeltaEvent,
    StreamToolCallEvent,
    StreamDoneEvent,
)
from src.infrastructure.config_manager import ConfigManager


DEFAULT_CONFIG_PATH = "./handq_config.yaml"


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

    def __init__(self, generation: int = 0) -> None:
        self._generation = generation

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

    def receptionist_thinking_on(self) -> None:
        _ui_logger.debug("receptionist_thinking_on")
        _emit({"type": "status", "kind": "receptionist_thinking_on"},
              gen=self._generation)

    def receptionist_thinking_off(self) -> None:
        _ui_logger.debug("receptionist_thinking_off")
        _emit({"type": "status", "kind": "receptionist_thinking_off"},
              gen=self._generation)


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
        self._ui = _StdioUI(self._generation)

        im = InteractionManager.get_instance()
        im.set_ui(self._ui)
        self._im = im

        self._shutdown_requested: bool = False

        logger.info("StdioBridge initialised; config_path=%s gen=%d",
                    self.config_path, self._generation)

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

        if msg_type == "config_set":
            try:
                new_cfg = msg.get("config") or {}
                if not isinstance(new_cfg, dict):
                    raise ValueError("'config' must be a JSON object")
                logger.info("config_set: path=%s keys=%d", self.config_path.resolve(), len(new_cfg))
                self._save_config_dict(new_cfg)
                if self._flow is not None:
                    try:
                        self._flow.config_manager.reload_config()
                    except Exception:
                        logger.exception("config_set: reload_config failed (continuing)")
                _emit({
                    "type": "final",
                    "id": msg_id,
                    "result": {"saved": True, "path": str(self.config_path.resolve())},
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
    # Flow lifecycle
    # ------------------------------------------------------------------

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

        api_key = llm_cfg.get("API_KEY") or ""
        max_tokens = int(llm_cfg.get("max_tokens", 4096))
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

        logger.info(
            "FlowController lazy construction: top_level_keys=%s llm_keys=%s session_keys=%s "
            "roles={agent:%d, planner:%d, receptionist:%d, from_data:%d} "
            "max_tokens=%d api_key_present=%s session_dir=%s",
            sorted(cfg.keys()) if isinstance(cfg, dict) else None,
            sorted(llm_cfg.keys()),
            sorted(sess_cfg.keys()),
            len(roles["agent"]), len(roles["planner"]),
            len(roles["receptionist"]), len(roles["from_data"]),
            max_tokens,
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
        svc_map = {
            m: AnthropicStreamingService(
                api_key=api_key, model=m, max_tokens=max_tokens, max_retries=3,
            )
            for m in shared_models
        }
        agent_services        = [svc_map[m] for m in roles.get("agent", [])]
        receptionist_services = [svc_map[m] for m in roles.get("receptionist", [])]
        from_data_services    = [svc_map[m] for m in roles.get("from_data", [])]
        planner_services      = [
            AnthropicStreamingService(
                api_key=api_key, model=m, max_tokens=max_tokens, max_retries=50,
            )
            for m in roles.get("planner", [])
        ]

        # Track every distinct service for shutdown.
        self._services = list(svc_map.values()) + planner_services

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
        logger.info("flow session starting; id=%s gen=%d", msg_id, gen)
        try:
            result = await self._flow.start_idle_session()
            logger.info("flow session completed; id=%s gen=%d", msg_id, gen)
            _emit({"type": "final", "id": msg_id, "result": result}, gen=gen)
        except asyncio.CancelledError:
            logger.info("flow session cancelled; id=%s gen=%d", msg_id, gen)
            raise
        except Exception as exc:
            logger.exception("flow session crashed; id=%s gen=%d", msg_id, gen)
            _emit({"type": "error", "id": msg_id, "where": "engine",
                   "message": f"start_idle_session failed: {exc}",
                   "fatal": True}, gen=gen)

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

    async def _do_new_session(self, msg_id: Optional[str]) -> None:
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
        logger.info("new_session sequence begin; id=%s old_gen=%d new_gen=%d",
                    msg_id, old_gen, new_gen)
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
        except Exception:
            logger.exception("new_session chain raised unexpectedly")
        finally:
            elapsed = (time.monotonic() - t0) * 1000.0
            logger.info("new_session sequence complete (%.2f ms; new_gen=%d)",
                        elapsed, new_gen)
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

        while True:
            msg = await self._inbox.get()
            if msg is None:
                # stdin EOF — exit cleanly.
                logger.info("inbox sentinel received; exiting main loop")
                break
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
        await StdioBridge(config_path=config_path).run()
    except Exception:
        logger.exception("StdioBridge.run() raised")
        raise
    finally:
        logger.info("StdioBridge.run() exit")


if __name__ == "__main__":
    asyncio.run(run())
