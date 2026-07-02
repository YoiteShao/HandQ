"""
InteractionManager — UI bus for the controller stack.

Design points:

* **No singleton.** The IM is constructed by the caller (FlowControllerV2 or
  test harness) and injected. Multiple sessions in the same process get
  independent IMs.
* **No threads.** No CLI ingress: user messages enter via
  ``Orchestrator.on_user_message`` directly.
* **No ``Decision`` parameter.** Confirmation methods take pre-rendered
  description / parameter / hint strings; the IM just forwards them.
* **Async confirmations.** ``request_*`` methods are awaitable so the agent's
  asyncio event loop is not blocked while waiting for the user. UI delegate
  implementations of these methods must therefore also be async.
* **Method-name forwarding.** Public ``notify_state_changed`` →
  delegate ``show_state_changed``; public ``notify_inline_event`` →
  delegate ``show_inline_event``.

The ``UIDelegate`` Protocol below documents the methods a UI may implement.
None of them are required — missing methods are silently skipped — but a
delegate that wants confirmations to actually function must implement at
least ``request_risk_confirmation`` / ``request_tool_confirmation`` /
``request_secret_input``.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, Optional, Protocol, TYPE_CHECKING

from .user_confirmation import UserConfirmation

if TYPE_CHECKING:
    pass


# ── UI delegate contract ─────────────────────────────────────────────────────

class UIDelegate(Protocol):
    """Methods a UI implementation may expose. Missing methods = silently skipped.
    """

    # ── Fire-and-forget messages ──────────────────────────────────────────
    def display_error(self, msg: str) -> None: ...

    # ── State / inline events ─────────────────────────────────────────────
    def show_state_changed(self, state: str) -> None: ...
    def show_inline_event(self, icon: str, desc: str) -> None: ...

    # ── Agent telemetry ───────────────────────────────────────────────────
    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int
    ) -> None: ...
    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        output: Any,
    ) -> None: ...

    # ── Async confirmations ───────────────────────────────────────────────
    async def request_risk_confirmation(
        self,
        description: str,
        *,
        title: Optional[str] = None,
        approve_label: Optional[str] = None,
    ) -> UserConfirmation: ...
    async def request_tool_confirmation(
        self, tool_name: str, params: Dict[str, Any], hint: str
    ) -> UserConfirmation: ...
    async def request_secret_input(self, prompt: str) -> str: ...
    async def request_user_text(self, prompt: str) -> str: ...

    # ── Receptionist reply streaming ──────────────────────────────────────
    def show_receptionist_thinking(self) -> None: ...
    def clear_receptionist_thinking(self) -> None: ...
    def stream_receptionist_reply_chunk(self, text: str) -> None: ...
    def seal_receptionist_reply(self) -> None: ...


# ── InteractionManager ───────────────────────────────────────────────────────

class InteractionManager:
    """V2 I/O bus — pure delegate forwarder, async-friendly, no shared state.

    Construct one per session (no global singleton). Wire a ``UIDelegate``
    via ``__init__(delegate=...)`` or ``set_delegate(ui)`` after construction.
    Hand the resulting IM to ``FlowControllerV2`` and ``PersistentAgent``.

    Public API:

      Wiring          : ``set_delegate``
      Fire-and-forget : ``display_error``,
                        ``notify_state_changed``, ``notify_inline_event``,
                        ``notify_decision_made``,
                        ``notify_tool_execution_started``
      Async input     : ``request_risk_confirmation``,
                        ``request_tool_confirmation``,
                        ``request_secret_input``, ``request_user_text``
      Reply streaming : ``notify_receptionist_thinking``,
                        ``clear_receptionist_thinking``,
                        ``stream_receptionist_reply_chunk``,
                        ``seal_receptionist_reply``
    """

    def __init__(self, delegate: Optional[UIDelegate] = None) -> None:
        self._ui: Optional[UIDelegate] = delegate

    # ── Wiring ────────────────────────────────────────────────────────────

    def set_delegate(self, ui: Optional[UIDelegate]) -> None:
        """Install (or clear with ``None``) the UI delegate."""
        self._ui = ui

    # ── Fire-and-forget UI calls ──────────────────────────────────────────

    def _ui_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Invoke ``method`` on the UI delegate if present, swallow errors.

        Non-existent methods are silent no-ops by design — UI implementations
        opt into individual events. Any exception raised by the delegate is
        also swallowed so a flaky UI cannot derail the controller.
        """
        if self._ui is None:
            return
        fn = getattr(self._ui, method, None)
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except Exception:
            pass

    def display_error(self, msg: str) -> None:
        self._ui_call("display_error", str(msg))

    def notify_state_changed(self, new_state: str) -> None:
        """Agent / controller state transition (e.g. ``"thinking"``)."""
        self._ui_call("show_state_changed", str(new_state))

    def notify_inline_event(self, icon: str, desc: str) -> None:
        """Single-line status banner (icon + text)."""
        self._ui_call("show_inline_event", str(icon or "·"), str(desc or ""))

    def notify_recall_started(self) -> None:
        """LTM recall is in flight. Drives a transient ``recalling…`` label on
        the activity strip; the next state / decision / tool event supersedes
        it. Fire-and-forget — no-op when the delegate doesn't expose it."""
        self._ui_call("show_recall_started")

    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int = 0
    ) -> None:
        """The agent emitted reasoning for its latest turn."""
        self._ui_call("notify_decision_made", int(iteration), str(reasoning), int(token_count))

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        output: Any,
    ) -> None:
        """Tool invocation telemetry. Called twice per tool: once with
        ``params`` set and ``output=None`` (start), once with ``params=None``
        and ``output`` set (finish)."""
        self._ui_call(
            "notify_tool_execution_started",
            int(iteration),
            tool_name,
            params,
            output,
        )

    def notify_desktop_takeover_started(self, reason: str) -> None:
        """Desktop tool entered an input-driving phase. The Electron overlay
        listens for the resulting ``desktop_takeover_started`` envelope to
        show a fullscreen border + Ctrl+Shift+C revoke hook. Kept out of the
        UIDelegate Protocol — delegates opt in by exposing a same-named method."""
        self._ui_call("notify_desktop_takeover_started", str(reason))

    def notify_desktop_takeover_ended(self, reason: str) -> None:
        """Counterpart of ``notify_desktop_takeover_started``. Fired when the
        desktop tool's input phase ends (task completion or user revoke)."""
        self._ui_call("notify_desktop_takeover_ended", str(reason))

    def notify_session_event(self, event_name: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Interactive shell session lifecycle (opened / data / input /
        exec_done / closed). The Electron renderer turns these into a live
        session-monitor panel. Fire-and-forget; dropped when no delegate is
        wired (e.g. unit tests). Kept out of the UIDelegate Protocol — delegates
        opt in by exposing a same-named method."""
        self._ui_call("notify_session_event", str(event_name), data if isinstance(data, dict) else {})

    def notify_checklist_changed(self, items: list) -> None:
        """Live checklist snapshot for the UI task panel. ``items`` is the list
        of ``{item_id, instruction, status}`` dicts from
        ``SharedCheckList.get_ui_snapshot``. Fired on every checklist mutation
        (item completed / pending tail replaced). Fire-and-forget; kept out of
        the UIDelegate Protocol — delegates opt in by exposing a same-named
        method, so unit tests with no delegate silently drop it."""
        self._ui_call("notify_checklist_changed", items if isinstance(items, list) else [])

    # ── Async confirmation / input flows ──────────────────────────────────

    async def _await_delegate(
        self, method: str, default: Any, *args: Any, **kwargs: Any,
    ) -> Any:
        """Invoke an async delegate method; return ``default`` if absent."""
        if self._ui is None:
            return default
        fn = getattr(self._ui, method, None)
        if fn is None:
            return default
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            # Tolerate a sync delegate impl — wraps the value as-is.
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return default

    async def request_risk_confirmation(
        self,
        description: str,
        *,
        title: Optional[str] = None,
        approve_label: Optional[str] = None,
    ) -> UserConfirmation:
        """Ask the user to approve a high-risk operation.

        ``title`` and ``approve_label`` are optional UI overrides. When
        provided they replace the modal's default title ("High-risk
        operation") and Approve button label. Callers use this to reframe
        the modal for non-generic flows — e.g. ``browser.request_user_login``
        surfaces the SAME modal machinery but wants an "I've completed the
        login" button label. ``None`` = keep the default.

        Default when no delegate is wired: ``UserConfirmation.no()`` (refuse
        rather than auto-approve).
        """
        return await self._await_delegate(
            "request_risk_confirmation",
            UserConfirmation.no(),
            str(description),
            title=title,
            approve_label=approve_label,
        )

    async def request_tool_confirmation(
        self,
        tool_name: str,
        params: Dict[str, Any],
        hint: str,
    ) -> UserConfirmation:
        """Ask the user to approve a tool execution.

        Default: ``UserConfirmation.no()``.
        """
        return await self._await_delegate(
            "request_tool_confirmation",
            UserConfirmation.no(),
            str(tool_name),
            params or {},
            str(hint),
        )

    async def request_secret_input(self, prompt: str) -> str:
        """Ask the user for a secret (e.g. SSH password). Default: ``""``."""
        return await self._await_delegate(
            "request_secret_input",
            "",
            str(prompt),
        )

    async def request_user_text(self, prompt: str) -> str:
        """Ask the user a free-form (non-secret) clarifying question.

        Unlike :meth:`request_secret_input`, the answer is NOT masked — the
        delegate renders a normal text field. Used by the ``ask_human`` tool.
        Default when no delegate is wired: ``""``.
        """
        return await self._await_delegate(
            "request_user_text",
            "",
            str(prompt),
        )

    # ── Receptionist reply streaming (fire-and-forget) ────────────────────

    def notify_receptionist_thinking(self) -> None:
        """Receptionist/planner started composing a reply but hasn't streamed
        any text yet. Renderer shows a transient "thinking…" bubble; cleared
        by the first ``reply_delta`` or by ``clear_receptionist_thinking``."""
        self._ui_call("show_receptionist_thinking")

    def clear_receptionist_thinking(self) -> None:
        """Counterpart of ``notify_receptionist_thinking``. Safety net for the
        silent path where no reply chunk ever streams (empty ``response_to_user``)
        so the thinking bubble doesn't linger. Idempotent on the renderer."""
        self._ui_call("clear_receptionist_thinking")

    def stream_receptionist_reply_chunk(self, text: str) -> None:
        """Push one streamed fragment of the receptionist/planner reply to
        the UI (renderer appends it to the live assistant bubble)."""
        self._ui_call("stream_receptionist_reply_chunk", str(text))

    def seal_receptionist_reply(self) -> None:
        """Finalize the current streamed reply bubble (renderer seals it)."""
        self._ui_call("seal_receptionist_reply")
