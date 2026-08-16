"""Controlling side — the object that sits in ``stdio_bridge._flows[sid]`` for a remote session.

``stdio_bridge`` reaches into its flows in a handful of places (``flow.started``,
``flow.on_user_message``, ``flow.destroy``, ``flow._ctx.resume_search_disabled``,
``flow.config_manager.reload_config``, ``flow.undo_files``). Rather than teach the
bridge about two kinds of session, this class satisfies that surface and is
handed to the same code paths. A remote session therefore travels the ordinary
``request`` / ``user_input`` / ``close_session`` routes with no branching.

The replay target is the local ``_StdioUI`` — the very delegate a local session
would use. Because ``agent_event`` frames carry *delegate* method names, replay is
``getattr(ui, frame["method"])(*frame["args"])`` with no translation table. Every
envelope the renderer already knows how to draw is produced by the code that
already produces it, stamped with the local session id, which is why the remote
session renders identically to a local one and why no renderer change was needed
for the chat surface at all.

Confirmations round-trip through that same delegate: ``_StdioUI.request_tool_confirmation``
emits the envelope, the renderer's inline confirm card answers it, the resulting
``UserConfirmation`` is encoded and sent back. The local UI cannot tell it is
approving something on another machine — except where it must, which is
``request_secret_input`` (see :meth:`_prompt_text`).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from . import codec, protocol
from .client import RemoteControlClient, RemoteControlError
from .history import RemoteHistory

logger = logging.getLogger("handq.remote_control.bridge")

#: Delegate methods that replay a cumulative snapshot after a gap.
_SNAPSHOT_REPLAY = (
    ("task_plan", "notify_task_plan_changed"),
    ("agent_todos", "notify_agent_todo_changed"),
)


class _RemoteCtx:
    """Stands in for ``FlowControllerV2._ctx``.

    ``stdio_bridge`` reads two things off it. ``resume_search_disabled`` is
    hard-True: session-resume searches *local* history digests, and offering a
    local session as the continuation of a remote one would be nonsense.
    ``desktop_state.revoke_takeover()`` has to reach the controlled machine, so it is
    forwarded rather than stubbed — the Ctrl+Shift+C panic hotkey working against
    a remote desktop takeover is exactly the case where a no-op would be
    dangerous.
    """

    def __init__(self, on_revoke: Callable[[], None]) -> None:
        self.resume_search_disabled = True
        self.desktop_state = _RemoteDesktopState(on_revoke)


class _RemoteDesktopState:
    def __init__(self, on_revoke: Callable[[], None]) -> None:
        self._on_revoke = on_revoke

    def revoke_takeover(self) -> bool:
        self._on_revoke()
        return True


class _NullConfigManager:
    """``config_set`` calls ``reload_config()`` on every live flow.

    A remote session's config lives on the controlled machine and is reloaded there;
    reloading anything here would be meaningless. A no-op is the correct
    behaviour, but it needs to *exist* or the config-save path raises.
    """

    def reload_config(self) -> None:
        return None


class RemoteSessionBridge:
    """One local session tab backed by a session on another machine."""

    def __init__(
        self,
        *,
        local_session_id: str,
        target_id: str,
        client: RemoteControlClient,
        local_ui: Any,
        loop: asyncio.AbstractEventLoop,
        emit: Callable[[Dict[str, Any], Optional[str]], None],
        on_seq_advanced: Optional[Callable[[str, str, int], None]] = None,
        on_session_gone: Optional[Callable[[str, str], None]] = None,
        remote_session_id: str = "",
        remote_capability: str = "",
        since_seq: int = 0,
        title: str = "",
    ) -> None:
        self._sid = local_session_id
        self._target_id = target_id
        self._client = client
        self._ui = local_ui
        self._loop = loop
        self._emit = emit
        self._on_seq_advanced = on_seq_advanced
        #: Called when the controlled session no longer exists (controlled side restarted), so the
        #: hub can drop the now-useless re-adopt record from the registry.
        self._on_session_gone = on_session_gone

        #: Set once the remote session exists. Empty means "not opened yet".
        self.remote_session_id = remote_session_id
        self.remote_capability = remote_capability
        #: How far THIS tab has consumed the remote event stream. Used only for
        #: the registry checkpoint (``_notify_seq``) and for de-duplicating
        #: events inside a tab's own lifetime — it deliberately does NOT gate the
        #: attach any more. See :meth:`start`: a fresh bridge always replays in
        #: full, because a resume point describes a *connection*, not a tab's UI.
        self._since_seq = int(since_seq)
        #: Human-readable name for this session, used as the panel chip's label.
        #: Seeded from the registry record on re-adopt and from the goal on a
        #: fresh open. Worth carrying explicitly: the hub writes it into the
        #: record, and until it did, every chip in the Connect panel was labelled
        #: with a raw ``rc-3f2a…`` id — the title was computed at open time, put
        #: on the wire, and then dropped on the floor by both sides.
        self.title = str(title or "")


        # ── stdio_bridge-facing surface ──────────────────────────────────────
        self.started = False
        self.working_directory: Optional[str] = None
        self.config_manager = _NullConfigManager()
        self.interaction_manager = None
        self._ctx = _RemoteCtx(self._revoke_desktop_takeover)

        self._closed = False
        #: request_ids currently being prompted locally. A reattach re-offers
        #: every parked confirmation, so without this a reconnect would open a
        #: second card for a question already on screen.
        self._active_confirms: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        #: Texts sent by THIS tab, in send order, not yet seen back as an
        #: echo. The controlled side echoes every user message (including this tab's
        #: own) through the same replay channel a reattach uses, so this queue
        #: is how ``on_agent_event`` tells "my own live echo, already drawn at
        #: submit time" apart from "a genuine replay of a message this tab
        #: never sent" — matched by position, not content, since the wire is
        #: strictly ordered and the server always emits the echo before
        #: anything else for that message.
        self._pending_local_echoes: Deque[str] = deque()
        #: Per-remote-session digest writer — makes this session appear in the
        #: Windows resume list after the tab closes. Populated lazily from the
        #: first meaningful event so a tab that never really opens leaves no
        #: empty History dir behind.
        self._history: Optional[RemoteHistory] = RemoteHistory(
            local_session_id=local_session_id,
            target_id=target_id,
            server_name=client.server_name,
            remote_session_id=remote_session_id,
            title=self.title,
        )


    # ── FlowControllerV2-shaped API ──────────────────────────────────────────

    async def start(self) -> None:
        """Attach to (or create) the remote session.

        ``stdio_bridge`` calls this before the first ``on_user_message``. When
        ``remote_session_id`` was supplied we are re-adopting a session that
        outlived its old tab, so we attach; otherwise creation is deferred to the
        first message, because ``open_session`` carries the goal and the goal is
        that message.

        The attach always asks for a FULL replay (``replay_all``), never
        ``self._since_seq``. A bridge is constructed at the same moment as its
        tab, so its local UI is guaranteed empty — whatever seq an earlier tab
        (or an earlier run) reached says nothing about what *this* DOM has
        drawn. Passing that inherited seq here is what made a re-adopted tab
        come up blank: the controlled side had every event and correctly
        withheld all of them. See ``RemoteControlClient.attach_session``.
        """
        if self.started:
            return
        self.started = True
        if self.remote_session_id:
            await self._client.attach_session(
                self.remote_session_id,
                self.remote_capability,
                self,
                since_seq=0,
                replay_all=True,
            )

    async def on_user_message(self, text: str) -> str:
        """Send a message to the remote session; create it on first use.

        Returns ``""`` rather than the coordinator's reply. That is safe and not a
        shortcut: the renderer never reads ``final.result.reply`` — the assistant
        bubble is produced entirely by the ``reply``/``reply_delta``/``reply_done``
        status events, which arrive here as ordinary agent events. Blocking this
        call until the remote turn finished would freeze the session's dispatch
        lock in ``stdio_bridge`` for the whole turn and stop the operator from
        answering the very confirmations that turn raises.
        """
        if self._closed:
            # Not silent. This slot can still be reachable after the bridge is
            # gone (an explicit Disconnect tears bridges down out from under
            # tabs that are still on screen), and ``_ensure_any_flow`` returns
            # early for any occupied ``_flows`` slot — so returning "" here made
            # the tab swallow everything the user typed with no reply, no error,
            # and no clue that it was over.
            self._ui.display_error(
                "This remote session has ended (the connection was released or the session was destroyed)."
                " Please reconnect or start a new remote session from the Connect panel."
            )
            return ""
        # Record what the user typed BEFORE we send it: even if the SSH send
        # fails, the user's intent is worth capturing in the digest, and
        # doing it here (not in the resulting agent_event stream) is the only
        # way — user messages travel UP the wire and never come back down.
        if self._history is not None:
            self._history.note_user_message(text)
        # The controlled side echoes every user message back through the same
        # replay channel a reattach uses (see on_agent_event's
        # show_user_message_echo handling) — including this tab's own,
        # since publish_event always live-pushes to the current owner. Track
        # it here so that live echo is recognized as "already drawn" instead
        # of producing a second bubble.
        self._pending_local_echoes.append(text)
        try:
            if not self.remote_session_id:
                await self._open_remote_session(text)
            else:
                await self._client.send_user_message(self.remote_session_id, text)
        except RemoteControlError as exc:
            self._ui.display_error(str(exc))
        except Exception as exc:
            logger.exception("remote_control: sending message to %s failed", self._sid)
            self._ui.display_error(f"Failed to send to remote: {exc}")
        return ""

    async def _open_remote_session(self, goal: str) -> None:
        title = goal.strip()[:30]
        session_id = await self._client.open_session(goal, self, title=title)
        self.remote_session_id = session_id
        self.remote_capability = self._client.capability_for(session_id)
        # Keep the title. The hub writes it into the registry record, which is
        # what the Connect panel's chip is labelled with.
        self.title = title
        # Now that a real remote session exists, the history writer can carry
        # its id for the workspace_dir pointer.
        if self._history is not None:
            self._history.bind_remote_session(session_id, title)
        self._notify_seq()
        self._status("remote_session_opened", remote_session_id=session_id)

    async def destroy(self) -> None:
        """Local tab closed.

        Detaches; does NOT close the remote session. That asymmetry is the
        feature — the controlled machine is meant to behave like a server, so closing a
        window here must not kill work there. Explicit termination goes through
        :meth:`close_remote`.
        """
        self._closed = True
        # Finalize the local digest. Status reflects what is true of the REMOTE
        # session, not of this tab: a detach leaves the agent running, so the
        # digest stays "running" and the resume list stops claiming a live
        # session was destroyed. Only close_remote (and a server-side close)
        # writes "destroyed".
        if self._history is not None:
            try:
                self._history.finalize(
                    reason="tab closed / detached", status="running"
                )
            except Exception:
                logger.debug("remote_control: history finalize failed",
                              exc_info=True)
            self._history = None

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.remote_session_id:
            try:
                await self._client.detach_session(self.remote_session_id)
            except Exception:
                logger.debug("remote_control: detach failed", exc_info=True)

    async def close_remote(self) -> None:
        """Terminate the session on the controlled machine as well.

        The remote destruction goes FIRST and is allowed to raise: it now waits
        for the controlled side to confirm (see
        ``RemoteControlClient.close_remote_session``), and everything after this
        point records "this session is over". Finalising the History digest as
        ``destroyed`` before knowing that would leave the local record claiming
        the session ended while its agent kept running on the other machine —
        the same inversion that used to drop the registry record on an
        unconfirmed close. On a raise the tab stays exactly as it was and the
        operator can retry.
        """
        if self.remote_session_id:
            await self._client.close_remote_session(self.remote_session_id)
        if self._history is not None:
            try:
                self._history.finalize(
                    reason="session closed by operator", status="destroyed"
                )
            except Exception:
                logger.debug("remote_control: history finalize failed", exc_info=True)
            self._history = None
        await self.destroy()

    async def undo_files(self, item_id: Optional[str]) -> Dict[str, Any]:
        """Serve the sidebar's ↺ button by round-tripping to the controlled machine."""
        if not self.remote_session_id:
            return {
                "ok": False,
                "mode": "notice",
                "restored": [],
                "conflicts": [
                    {"path": "-", "conflict": "not_connected",
                     "detail": "Remote session has not been established yet"}
                ],
            }
        try:
            return await self._client.call_session_rpc(
                self.remote_session_id, "file_undo", {"item_id": item_id}
            )
        except RemoteControlError as exc:
            return {
                "ok": False,
                "mode": "notice",
                "restored": [],
                "conflicts": [
                    {"path": "-", "conflict": "remote_error", "detail": str(exc)}
                ],
            }

    # ── SessionSink — inbound frames ─────────────────────────────────────────

    def on_agent_event(self, method: str, args: List[Any], seq: int) -> None:
        """Replay one delegate call onto the local UI.

        ``getattr`` with a None default mirrors ``InteractionManager._ui_call``
        (``interaction_manager.py:130``): a method the local delegate does not
        implement is a silent no-op, which keeps a newer controlled machine emitting an
        event an older controlling machine has never heard of from being an error.
        """
        if self._closed:
            return
        if method == "show_user_message_echo":
            self._on_user_message_echo(args, seq)
            return
        fn = getattr(self._ui, method, None)

        if fn is None:
            logger.debug("remote_control: local UI has no %s; dropping", method)
            return
        try:
            fn(*args)
        except Exception:
            logger.debug("remote_control: replay of %s failed", method, exc_info=True)
        # Mirror into the History digest so this session is findable later —
        # only three delegate methods actually contribute (see RemoteHistory.record);
        # everything else is a fast no-op there.
        if self._history is not None:
            try:
                self._history.record(method, args)
            except Exception:
                logger.debug("remote_control: history record failed",
                              exc_info=True)
        if seq > self._since_seq:
            self._since_seq = seq
            self._notify_seq()

    def _on_user_message_echo(self, args: List[Any], seq: int) -> None:
        """Handle the controlled side's echo of a user message (see
        ``server.py``'s ``_user_message``/``_open_session``).

        Every echo — including this tab's own — arrives through this same
        channel, since ``publish_event`` always live-pushes to the current
        owner. If this tab just sent the exact text at the front of
        ``_pending_local_echoes``, it is the live echo of our own send: the
        bubble is already on screen from the synchronous ``addUserBubble``
        call at submit time, and ``note_user_message`` already recorded it,
        so there is nothing left to do but advance ``seq``. Anything else is
        a genuine replay (a reattach, or another tab's message) and gets
        drawn + recorded like any other event.
        """
        text = str(args[0]) if args else ""
        if self._pending_local_echoes and self._pending_local_echoes[0] == text:
            self._pending_local_echoes.popleft()
        else:
            fn = getattr(self._ui, "show_user_message_echo", None)
            if fn is not None:
                try:
                    fn(text)
                except Exception:
                    logger.debug("remote_control: replay of show_user_message_echo failed",
                                  exc_info=True)
            if self._history is not None:
                try:
                    self._history.record("show_user_message_echo", args)
                except Exception:
                    logger.debug("remote_control: history record failed",
                                  exc_info=True)
        if seq > self._since_seq:
            self._since_seq = seq
            self._notify_seq()


    def on_confirm_request(
        self, request_id: str, method: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> None:
        if self._closed or request_id in self._active_confirms:
            return
        self._active_confirms.add(request_id)
        self._spawn(
            self._answer_confirm(request_id, method, args, kwargs),
            f"rc-confirm-{request_id}",
        )

    def on_confirm_cancel(self, request_id: str) -> None:
        """Controlled side gave up on this prompt (ask_human's own 30-minute
        timeout fired) before we answered it. Cancel our own in-flight
        _answer_confirm task for it — which is still awaiting the LOCAL
        Electron modal — so the modal closes. Cancelling that task delivers
        CancelledError at its `await fn(...)` call, which for ask_human is
        `_StdioUI.request_user_form` → `_await_user_response`'s own
        `except asyncio.CancelledError` branch — the exact same branch that
        already emits the `ask_human_expired` envelope for a purely local
        timeout, so both paths converge on one renderer handler with no
        extra plumbing needed to smuggle the local prompt_id back out here.
        """
        if request_id not in self._active_confirms:
            return  # already answered locally, or never started — no-op
        task_name = f"rc-confirm-{request_id}"
        for task in list(self._tasks):
            if task.get_name() == task_name:
                task.cancel()
        self._active_confirms.discard(request_id)

    async def _answer_confirm(
        self, request_id: str, method: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> None:
        """Prompt locally, then ship the answer back.

        No timeout on the local prompt: the controlled side has parked the agent
        indefinitely, so the only deadline is the operator's attention.
        """
        try:
            fn = getattr(self._ui, method, None)
            if fn is None:
                logger.warning("remote_control: local UI cannot serve %s", method)
                return

            if method in codec.TEXT_METHODS:
                prompt = self._prompt_text(method, args, kwargs)
                answer = await fn(prompt)
                value: Any = codec.encode_text(answer)
            elif method in codec.FORM_METHODS:
                question = str(args[0]) if args else ""
                fields = args[1] if len(args) > 1 else []
                answer = await fn(question, fields)
                value = codec.encode_form(answer)
            else:
                safe_kwargs = {
                    k: v for k, v in kwargs.items()
                    if k in ("title", "approve_label")
                }
                result = await fn(*args, **safe_kwargs)
                value = codec.encode_confirmation(result)

            await self._client.send_confirm_response(
                self.remote_session_id, request_id, value
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("remote_control: answering %s failed", method)
        finally:
            self._active_confirms.discard(request_id)

    def _prompt_text(
        self, method: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> str:
        """Build the prompt for a text/secret request.

        The ``insecure`` flag is folded into the prompt string rather than passed
        through as a kwarg, because ``_StdioUI.request_secret_input`` takes only
        ``prompt`` — and because the warning belongs where the operator is
        actually looking. With TLS out of scope for this phase, a password typed
        here crosses the network in cleartext, and that has to be said out loud
        at the moment of typing rather than buried in a settings page.
        """
        prompt = str(args[0]) if args else ""
        if method == "request_secret_input" and kwargs.get("insecure"):
            return (
                "⚠ This credential will be sent over the network in cleartext to the remote machine"
                f" ({self._client.address.endpoint}, TLS is not enabled this session).\n\n{prompt}"
            )
        return prompt

    def on_session_attached(
        self, cur_seq: int, gap: bool, snapshot: Dict[str, Any]
    ) -> None:
        self._since_seq = max(self._since_seq, int(cur_seq))
        self._notify_seq()

        if gap:
            # Say so. A transcript with a hole in it that presents as complete is
            # the failure mode this whole seq mechanism exists to avoid.
            self._ui.show_user_notice(
                "⚠ Some events during the disconnection from the remote have exceeded the remote's retention window,"
                " the content below may be incomplete. Current task state has been refreshed from the remote's snapshot.",
                True,
            )
            for key, method in _SNAPSHOT_REPLAY:
                fn = getattr(self._ui, method, None)
                if fn is not None:
                    try:
                        fn(snapshot.get(key) or [])
                    except Exception:
                        logger.debug("remote_control: snapshot replay failed", exc_info=True)
            takeover = snapshot.get("desktop_takeover")
            if takeover:
                fn = getattr(self._ui, "notify_desktop_takeover_started", None)
                if fn is not None:
                    fn(str(takeover))

        self._status("remote_attached", cur_seq=int(cur_seq), gap=bool(gap))

    def on_disconnected(self, detail: str) -> None:
        # Not an error and not a task failure — the controlled agent is still working.
        # The renderer shows a reconnecting state on the card.
        self._status("remote_disconnected", detail=detail)
        self._ui.show_inline_event("⇄", f"{detail}, automatically reconnecting…")

    def on_reconnected(self) -> None:
        self._status("remote_connected")

    def on_session_superseded(self, generation: int) -> None:
        self._status("remote_superseded", generation=generation)
        self._ui.show_user_notice(
            "This remote session has been taken over by another controller. This tab has stopped receiving updates,"
            " reconnect to take it over again.",
            True,
        )

    def on_session_closed(self, reason: str) -> None:
        """The controlled session is over. Distinguish "gone" from "unreachable".

        Every reason here means the session no longer exists on the other
        machine, so its re-adopt record must go — a record whose session is gone
        is a chip that can only fail when clicked. The one reason that is NOT in
        this method is a plain disconnect (:meth:`on_disconnected`): there the
        session is parked and very much alive, and forgetting it would be the
        expensive mistake.
        """
        self._closed = True
        self._status("remote_session_closed", reason=reason)
        if reason == protocol.REASON_SERVER_SHUTDOWN:
            self._ui.show_user_notice("Remote HandQ has closed, this session has ended.", True)
        elif reason == protocol.REASON_UNKNOWN_SESSION:
            self._ui.show_user_notice(
                "The remote no longer has this session (the remote may have restarted). A new remote session is needed.", True
            )
        elif reason == protocol.REASON_RELEASED_BY_CLIENT:
            self._ui.show_user_notice(
                "Disconnected from this remote machine; this session was destroyed on the other end.", True
            )
        elif reason == protocol.REASON_FORCE_CLOSED:
            self._ui.show_user_notice(
                "This session was force-ended by another controller holding the same key"
                " (it lacked credentials for this session, so it could only force-terminate rather than close normally).", True
            )
        elif reason != protocol.REASON_CLOSED_BY_CONTROLLER:
            self._ui.show_user_notice(f"Remote session has ended ({reason}).", True)

        # Drop the re-adopt record. Previously only the controlled-restart case
        # (unknown_session) did this, so a session the other operator closed
        # from their dashboard — or one killed by server_shutdown — left a chip
        # behind that stayed until someone clicked it and got an error.
        if self._on_session_gone and self.remote_session_id:
            try:
                self._on_session_gone(self._target_id, self.remote_session_id)
            except Exception:
                logger.debug("remote_control: on_session_gone failed", exc_info=True)
        if self._history is not None:
            try:
                self._history.finalize(
                    reason=f"remote session closed ({reason})", status="destroyed"
                )
            except Exception:
                logger.debug("remote_control: history finalize failed", exc_info=True)
            self._history = None


    # ── Helpers ──────────────────────────────────────────────────────────────

    def _revoke_desktop_takeover(self) -> None:
        """Forward the local revoke hotkey to the controlled machine."""
        if not self.remote_session_id:
            return
        self._spawn(
            self._client.send_user_input(
                self.remote_session_id, "desktop_takeover_revoked", {}
            ),
            "rc-revoke",
        )

    def _status(self, kind: str, **fields: Any) -> None:
        """Emit a remote-control status envelope to the renderer for this tab."""
        payload: Dict[str, Any] = {
            "type": "status",
            "kind": kind,
            "target_id": self._target_id,
            "endpoint": self._client.address.endpoint,
            "server_name": self._client.server_name,
        }
        payload.update(fields)
        try:
            self._emit(payload, self._sid)
        except Exception:
            logger.debug("remote_control: status emit failed", exc_info=True)

    def _notify_seq(self) -> None:
        if self._on_seq_advanced and self.remote_session_id:
            try:
                self._on_seq_advanced(
                    self._target_id, self.remote_session_id, self._since_seq
                )
            except Exception:
                logger.debug("remote_control: seq checkpoint failed", exc_info=True)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.ensure_future(coro)
        try:
            task.set_name(name)
        except Exception:
            pass
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
