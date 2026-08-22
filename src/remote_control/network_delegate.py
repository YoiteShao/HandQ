"""Controlled-side ``UIDelegate`` — turns one session's UI events into wire frames.

Installed on a remote session's ``InteractionManager`` in place of the local
``_StdioUI``. Two responsibilities:

**Fire-and-forget events fan out.** Each of the non-blocking delegate methods
is recorded in the session's :class:`~src.remote_control.event_log.EventLog`
(giving it a ``seq``) and pushed to the attached controller. Optionally it also
goes to a *local* delegate — on a Windows controlled machine that is the machine's own
``_StdioUI``, so the person sitting at it can watch what the remote operator is
having their HandQ do. That visibility is the main thing the "embed the server in
Electron" decision buys, and it costs one extra call here.

**Blocking requests park indefinitely.** The 4 ``request_*`` methods create a
future, register it as a pending confirmation on the session, emit a
``confirm_request`` to the controller, and then just await. Nothing times out and
nothing defaults. If the controller disconnects mid-await the future stays
registered; whoever attaches next is handed it again in ``session_attached``.
This is the whole point of the "hang indefinitely, wait for reconnect" decision: the controlled agent behaves
exactly as it would locally when a human walks away from the keyboard — blocked,
not failed.

That parking is also why confirmations are routed to the controller *only* and
never to the local delegate. Two delegates both prompting for the same
confirmation would race, and whichever lost would leave a dead modal on someone's
screen. The local side is told about the request through an ordinary
``show_inline_event`` instead, so the controlled machine's user can see that a
confirmation is outstanding without being able to answer it.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..controller_v2.user_confirmation import UserConfirmation
from . import codec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .server import RemoteSession

logger = logging.getLogger("handq.remote_control.delegate")


class NetworkUIDelegate:
    """Forwards one session's UI traffic to its controlling client.

    Every method here is spelled out rather than synthesised through
    ``__getattr__``. That is not verbosity for its own sake:
    ``InteractionManager`` probes for optional methods with
    ``getattr(ui, method, None)`` (``interaction_manager.py:130``, ``:280``), so a
    catch-all ``__getattr__`` would make *every* name appear implemented —
    including the four ``request_*`` ones, which would then be answered by a
    plain forwarder instead of an awaitable, and confirmations would silently
    resolve to whatever that returned.
    """

    def __init__(
        self,
        session: "RemoteSession",
        local_delegate: Optional[Any] = None,
        allow_secret_prompt: bool = False,
    ) -> None:
        self._session = session
        #: The controlled machine's own UI, when it has one (Electron host). Receives
        #: fire-and-forget events for visibility; never receives confirmations.
        self._local = local_delegate
        #: See :meth:`request_secret_input`.
        self._allow_secret_prompt = allow_secret_prompt

    # ── Fire-and-forget ──────────────────────────────────────────────────────

    def _forward(self, method: str, *args: Any) -> None:
        """Log, stamp with a seq, push to the controller, mirror locally.

        Never raises. ``InteractionManager._ui_call`` swallows delegate
        exceptions (``interaction_manager.py:135``), so an exception escaping
        here would be invisible — the event would simply never appear on either
        screen with no trace of why.
        """
        try:
            safe = codec.safe_args(list(args))
            self._session.publish_event(method, safe)
        except Exception:
            logger.exception("remote_control: failed to publish %s", method)
        self._mirror_local(method, args)

    def _mirror_local(self, method: str, args: Any) -> None:
        if self._local is None:
            return
        try:
            fn = getattr(self._local, method, None)
            if fn is not None:
                fn(*args)
        except Exception:
            # The local mirror is a convenience, never a correctness
            # requirement — the controller has already been served.
            logger.debug("remote_control: local mirror of %s failed", method, exc_info=True)

    def display_error(self, msg: str) -> None:
        self._forward("display_error", str(msg))

    def show_state_changed(self, state: str) -> None:
        self._forward("show_state_changed", str(state))

    def show_inline_event(self, icon: str, desc: str) -> None:
        self._forward("show_inline_event", str(icon), str(desc))

    def show_user_notice(self, message: str, urgent: bool = False) -> None:
        self._forward("show_user_notice", str(message), bool(urgent))

    def show_recall_started(self) -> None:
        self._forward("show_recall_started")

    def show_task_completed(self, summary: str) -> None:
        self._forward("show_task_completed", str(summary))

    def notify_decision_made(
        self, iteration: int, reasoning: str, token_count: int = 0
    ) -> None:
        self._forward("notify_decision_made", int(iteration), str(reasoning), int(token_count))

    def notify_tool_execution_started(
        self,
        iteration: int,
        tool_name: Optional[str],
        params: Optional[Dict[str, Any]],
        output: Any,
    ) -> None:
        # Called twice per tool — once with params and output=None (start), once
        # with params=None and output set (finish). Both are forwarded verbatim
        # so the remote trace panel behaves identically to a local one; only
        # per-value size is bounded (see codec.safe_value).
        self._forward("notify_tool_execution_started", int(iteration), tool_name, params, output)

    def notify_desktop_takeover_started(self, reason: str = "input_action") -> None:
        self._forward("notify_desktop_takeover_started", str(reason))

    def notify_desktop_takeover_ended(self, reason: str = "task_ended") -> None:
        self._forward("notify_desktop_takeover_ended", str(reason))

    def notify_session_event(self, event_name: str, data: Any = None) -> None:
        self._forward("notify_session_event", str(event_name), data if isinstance(data, dict) else {})

    def notify_task_plan_changed(self, items: Any = None) -> None:
        self._forward("notify_task_plan_changed", items if isinstance(items, list) else [])

    def notify_agent_todo_changed(self, todos: Any = None) -> None:
        self._forward("notify_agent_todo_changed", todos if isinstance(todos, list) else [])

    def notify_model_stats_changed(self, models: Any = None) -> None:
        self._forward("notify_model_stats_changed", models if isinstance(models, list) else [])

    def notify_file_touch(
        self,
        path: str,
        kind: str,
        tool: str,
        item_id: str = "",
        reversible: bool = False,
    ) -> None:
        self._forward(
            "notify_file_touch",
            str(path),
            str(kind),
            str(tool),
            str(item_id or ""),
            bool(reversible),
        )

    def show_coordinator_reply(self, text: str) -> None:
        """The coordinator's complete reply, as one bubble.

        Not part of the ``UIDelegate`` Protocol — it exists because
        ``FlowControllerV2`` delivers this particular message through the
        ``on_reply_to_user`` *callback* rather than through the
        InteractionManager (``flow_controller.py:533-536``), and that callback
        writes straight to the local renderer. Two things arrive only this way
        and would otherwise be invisible to a remote controller: a reply the
        coordinator did not stream (``orchestrator.py:436`` fires the callback
        only when ``_last_response_streamed`` is False) and every background
        task-completion summary (``orchestrator.py:1361``, unconditional). The
        second is precisely the "the task is done, what's the result" message, so losing it
        would gut the feature.

        The controlled host routes that callback here; ``_StdioUI`` grew a same-named
        method so the controlling side can replay it with no special case.
        """
        self._forward("show_coordinator_reply", str(text))

    def show_coordinator_thinking(self) -> None:
        self._forward("show_coordinator_thinking")
    def clear_coordinator_thinking(self) -> None:
        self._forward("clear_coordinator_thinking")

    def stream_coordinator_reply_chunk(self, text: str) -> None:
        self._forward("stream_coordinator_reply_chunk", str(text))

    def seal_coordinator_reply(self) -> None:
        self._forward("seal_coordinator_reply")

    # ── Blocking requests — parked until answered ────────────────────────────

    async def _ask(
        self,
        method: str,
        args: List[Any],
        kwargs: Optional[Dict[str, Any]] = None,
        *,
        local_notice: str = "",
    ) -> Any:
        """Emit a ``confirm_request`` and await the answer.

        The future is owned by the session, not by this delegate, so it survives
        the controller disconnecting and reconnecting. No timeout for
        risk/tool/secret confirmations: see the module docstring. ``ask_human``
        (``request_user_form``) is the one exception — it enforces the same
        ``ASK_HUMAN_TIMEOUT_S`` deadline the local Electron path does, because
        an unattended controller must not park the controlled agent forever on a
        question nobody will ever answer.
        """
        request_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Any]" = loop.create_future()

        self._session.register_confirm(
            request_id=request_id,
            method=method,
            args=args,
            kwargs=kwargs or {},
            future=future,
        )
        if local_notice:
            self._mirror_local("show_inline_event", ("⏸", local_notice))

        timeout: Optional[float] = None
        if method == "request_user_form":
            from ..tools.ask_human_tool import ASK_HUMAN_TIMEOUT_S
            timeout = ASK_HUMAN_TIMEOUT_S

        try:
            raw = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            # Controlled side gave up before the controller answered. Tell the
            # controller to cancel whatever it already relayed locally (its
            # own Electron modal) — reuses stdio_bridge.py's
            # ask_human_expired envelope via RemoteSessionBridge.
            # on_confirm_cancel — and let the outer ask_human_tool.py's own
            # wait_for produce the single agent-facing error, rather than
            # duplicating that text here.
            self._session.discard_confirm_and_notify(request_id)
            raise
        except asyncio.CancelledError:
            # Genuine task cancellation (the session is being torn down), not a
            # disconnect. Drop the registration so the session's pending list
            # doesn't keep offering a confirmation nobody is waiting on.
            self._session.discard_confirm(request_id)
            raise
        return codec.decode_for_method(method, raw)

    async def request_risk_confirmation(
        self,
        description: str,
        *,
        title: Optional[str] = None,
        approve_label: Optional[str] = None,
    ) -> UserConfirmation:
        kwargs: Dict[str, Any] = {}
        if title:
            kwargs["title"] = str(title)
        if approve_label:
            kwargs["approve_label"] = str(approve_label)
        return await self._ask(
            "request_risk_confirmation",
            [str(description)],
            kwargs,
            local_notice="Waiting for the remote operator to confirm a high-risk action",
        )

    async def request_tool_confirmation(
        self,
        tool_name: str,
        params: Dict[str, Any],
        hint: str,
    ) -> UserConfirmation:
        # params is flattened here, not on the controlling side: the modal that
        # renders it lives there, but producing a renderable payload is this
        # delegate's obligation (see codec.safe_params).
        return await self._ask(
            "request_tool_confirmation",
            [str(tool_name), codec.safe_params(params), str(hint)],
            local_notice=f"Waiting for the remote operator to confirm tool call: {tool_name}",
        )

    async def request_secret_input(self, prompt: str) -> str:
        """A password prompt on the controlled machine — refused across the wire by default.

        This has exactly one real caller: ``ssh_setup._prompt_password``
        (``ssh_setup.py:505``), and it is step 4 of a 4-step chain — key auth,
        then keyring, and only then a prompt. So it fires once per (this machine,
        target host) pair, the first time this machine SSHes somewhere with no key
        and nothing in its keyring; the answer is then stored in the keyring
        (``ssh_setup.py:434``) and never asked for again.

        Forwarding it would send that password across a channel with no TLS. Once
        is enough for anyone capturing the traffic, and unlike a tool
        confirmation the value is a durable credential for a *third* machine. So
        the default is to refuse and tell the operator how to do it out of band —
        ``ssh_setup.py:527`` already prints the exact command. Returning ``""``
        is the same thing ``InteractionManager`` returns when no delegate is
        wired (``interaction_manager.py:343``), so the SSH layer treats it as a
        failed attempt and reports it normally rather than crashing.

        Set ``remote_control.allow_remote_secret_prompt: true`` to forward it
        anyway; the controlling-side prompt then carries an explicit cleartext warning
        (``session_bridge._prompt_text``). That switch should be revisited the
        moment TLS lands, at which point forwarding becomes the sane default.
        """
        if not self._allow_secret_prompt:
            hint = (
                "⚠ Remote HandQ is requesting a password (typically a first-time SSH login to a third machine). "
                "This channel does not have TLS enabled, so the password will NOT be sent over the network — it has been refused automatically.\n"
                "Instead, establish key trust from the controlled machine (one-time, won't be asked again):"
                "\n    ssh-copy-id <username>@<hostname>\n"
                "Or set remote_control.allow_remote_secret_prompt: true in the controlled machine's handq_config.yaml to allow forwarding "
                "(only enable this after explicitly accepting the cleartext risk).\n"
                f"Original prompt: {prompt}"
            )
            logger.warning(
                "remote_control: refused a remote secret prompt for session %s "
                "(allow_remote_secret_prompt is off)",
                self._session.session_id,
            )
            # Goes to BOTH sides: the operator needs the instruction, and the
            # person at the controlled machine needs to know a credential was asked for.
            self._forward("show_user_notice", hint, True)
            return ""

        return await self._ask(
            "request_secret_input",
            [str(prompt)],
            {"insecure": True},
            local_notice="Waiting for the remote operator to enter credentials (cleartext channel)",
        )

    async def request_user_form(self, question: str, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._ask(
            "request_user_form",
            [str(question), fields or []],
            local_notice="Waiting for the remote operator to respond",
        )
