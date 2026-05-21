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


def _emit(obj: Dict[str, Any]) -> None:
    """Serialise *obj* as one JSON line on the IPC stdout and flush."""
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with _write_lock:
        _ipc_out.write(line + "\n")
        _ipc_out.flush()
    try:
        logger.debug(
            "outbound envelope type=%s id=%s",
            obj.get("type"), obj.get("id"),
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
    """

    # --- generic display ----------------------------------------------------
    def display_message(self, msg: str) -> None:
        _ui_logger.debug("display_message: %s", _truncate(msg))
        _emit({"type": "status", "kind": "message", "text": str(msg)})

    def display_receptionist_reply(self, msg: str) -> None:
        _ui_logger.debug("display_receptionist_reply: %s", _truncate(msg))
        _emit({"type": "status", "kind": "reply", "text": str(msg)})

    def display_error(self, msg: str) -> None:
        _ui_logger.error("display_error: %s", _truncate(msg))
        _emit({"type": "error", "where": "engine", "message": str(msg), "fatal": False})

    def display_progress_status(self, current: int, total: int) -> None:
        _ui_logger.debug("display_progress_status: %s/%s", current, total)
        _emit({"type": "status", "kind": "progress", "current": current, "total": total})

    # --- state / step events -----------------------------------------------
    def show_state_changed(self, state: Any) -> None:
        _ui_logger.debug("show_state_changed: %s", _truncate(state))
        _emit({"type": "status", "kind": "state_changed", "state": str(state)})

    def show_step_started(self, step_id: Any, desc: str = "") -> None:
        _ui_logger.debug("show_step_started: id=%s desc=%s", step_id, _truncate(desc))
        _emit({"type": "status", "kind": "step_started",
               "step_id": str(step_id), "desc": str(desc)})

    def show_step_completed(self, step_id: Any, desc: str = "") -> None:
        _ui_logger.debug("show_step_completed: id=%s desc=%s", step_id, _truncate(desc))
        _emit({"type": "status", "kind": "step_completed",
               "step_id": str(step_id), "desc": str(desc)})

    # --- engine notifications ----------------------------------------------
    def notify_decision_made(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_decision_made: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "decision_made",
               "args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})

    def notify_tool_execution_started(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_tool_execution_started: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "tool_execution_started",
               "args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})

    def notify_step_confidence(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_step_confidence: args=%s kwargs=%s",
                         _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "step_confidence",
               "args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})

    def notify_task_completed(self, *args: Any, **kwargs: Any) -> None:
        _ui_logger.debug("notify_task_completed: args=%s kwargs=%s",
                        _redact_payload(list(args)), _redact_payload(kwargs))
        _emit({"type": "status", "kind": "task_completed",
               "args": [str(a) for a in args], "kwargs": {k: str(v) for k, v in kwargs.items()}})

    def receptionist_thinking_on(self) -> None:
        _ui_logger.debug("receptionist_thinking_on")
        _emit({"type": "status", "kind": "receptionist_thinking_on"})

    def receptionist_thinking_off(self) -> None:
        _ui_logger.debug("receptionist_thinking_off")
        _emit({"type": "status", "kind": "receptionist_thinking_off"})


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

        # Singleton UI registration happens at startup so that even early
        # engine output (e.g. logger init) routes through us if the engine
        # is later constructed.
        self._ui = _StdioUI()
        im = InteractionManager.get_instance()
        im.set_ui(self._ui)
        self._im = im

        self._shutdown_requested: bool = False

        logger.info("StdioBridge initialised; config_path=%s", self.config_path)

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
                           "fatal": False})
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
                   "message": f"stdin reader crashed: {exc}", "fatal": True})
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
                })
            except Exception as exc:
                logger.exception("config_get failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_get failed: {exc}", "fatal": False})
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
                })
            except Exception as exc:
                logger.exception("config_set failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"config_set failed: {exc}", "fatal": False})
            return

        if msg_type == "request":
            try:
                goal = msg.get("goal", "")
                self._ensure_flow(
                    working_directory=msg.get("working_directory", "."),
                    storage_directory=msg.get("storage_directory"),
                )
                self._im.inject_user_message(str(goal))
                if self._flow_task is None or self._flow_task.done():
                    assert self._flow is not None
                    self._flow_task = asyncio.create_task(
                        self._run_flow_session(msg_id)
                    )
            except Exception as exc:
                logger.exception("request failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"request failed: {exc}", "fatal": True})
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
                           "fatal": False})
            except Exception as exc:
                logger.exception("user_input failed")
                _emit({"type": "error", "id": msg_id, "where": "bridge",
                       "message": f"user_input failed: {exc}", "fatal": False})
            return

        if msg_type == "shutdown":
            await self._do_shutdown(msg_id)
            return

        logger.warning("unknown inbound type=%r id=%s", msg_type, msg_id)
        _emit({"type": "error", "id": msg_id, "where": "bridge",
               "message": f"Unknown message type: {msg_type!r}",
               "fatal": False})

    # ------------------------------------------------------------------
    # Flow lifecycle
    # ------------------------------------------------------------------

    def _ensure_flow(self, working_directory: str, storage_directory: Optional[str]) -> None:
        if self._flow is not None:
            return
        cm = ConfigManager(str(self.config_path))
        cfg = cm.get_config()
        llm_cfg = cfg.get("llm", {}) or {}
        sess_cfg = cfg.get("session", {}) or {}

        api_key = llm_cfg.get("API_KEY") or ""
        max_tokens = int(llm_cfg.get("max_tokens", 4096))
        models = llm_cfg.get("models") or ["anthropic::claude-4-5-haiku"]

        logger.info(
            "FlowController lazy construction: top_level_keys=%s llm_keys=%s session_keys=%s "
            "models=%s max_tokens=%d api_key_present=%s working_directory=%s",
            sorted(cfg.keys()) if isinstance(cfg, dict) else None,
            sorted(llm_cfg.keys()),
            sorted(sess_cfg.keys()),
            models,
            max_tokens,
            bool(api_key),
            working_directory,
        )
        if not api_key:
            logger.warning("llm.API_KEY is empty in config; LLM calls will fail")

        self._services = [
            AnthropicStreamingService(api_key=api_key, model=m, max_tokens=max_tokens)
            for m in models
        ]

        self._flow = FlowController(
            agent_llm_services=self._services,
            planner_llm_services=self._services,
            receptionist_llm_services=self._services,
            from_data_llm_services=self._services,
            working_directory=working_directory,
            storage_directory=storage_directory,
            step_verification_threshold=float(
                sess_cfg.get("step_verification_threshold", 0.7)
            ),
            venv_path=sess_cfg.get("venv_path"),
            config_path=str(self.config_path),
        )
        logger.info("FlowController constructed; %d service(s)", len(self._services))

    async def _run_flow_session(self, msg_id: Optional[str]) -> None:
        assert self._flow is not None
        logger.info("flow session starting; id=%s", msg_id)
        try:
            result = await self._flow.start_idle_session()
            logger.info("flow session completed; id=%s", msg_id)
            _emit({"type": "final", "id": msg_id, "result": result})
        except asyncio.CancelledError:
            logger.info("flow session cancelled; id=%s", msg_id)
            raise
        except Exception as exc:
            logger.exception("flow session crashed; id=%s", msg_id)
            _emit({"type": "error", "id": msg_id, "where": "engine",
                   "message": f"start_idle_session failed: {exc}", "fatal": True})

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
            _emit({"type": "final", "id": msg_id, "result": {"shutdown": "ok"}})

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
                       "message": f"dispatch crashed: {exc}", "fatal": False})
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
