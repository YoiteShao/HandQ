"""Controlled side — the listener that lets another HandQ drive this one.

Structure is a direct port of the ``Server`` class in
``verify_fleet_scheduling/local_protocol_check.py``, which was written as the
runnable prototype for exactly this module and passes 9/9 on the five mechanisms
the design doc calls mandatory. The control flow (auth-first handshake, watchdog
and pinger as separate tasks, read loop that treats a bare timeout as
``continue``, generation-bumping supersede) is carried over unchanged. Two things
differ from the prototype, both deliberate:

* **Disconnect does not resolve pending confirmations.** The prototype resolved
  them to a safe ``no`` default. Here they are *parked* — see
  :meth:`RemoteSession.on_owner_lost`. The agent stays blocked on the human
  rather than being told the human said no.

* **Sessions are multiplexed over one connection** and every session-scoped
  frame carries ``session_id``. The prototype had one session per connection,
  which let it omit the field.

The host environment (Electron bridge on Windows, ``handq_linux.py`` daemon on
Linux) supplies a :class:`SessionHost` that knows how to build a real
``FlowControllerV2``. This module never imports the controller stack, so it can
be exercised against a stub — which is what the tests do.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Protocol

from ..infrastructure.chatroom.transport import JsonlConnection
from . import protocol
from .event_log import EventLog

logger = logging.getLogger("handq.remote_control.server")

#: Outbound frames buffered for an attached controller before we give up on it.
#: Overflow closes the connection rather than growing without bound: the event
#: log already holds every event, so the controller reattaches with a
#: ``since_seq`` and loses nothing. A wedged reader therefore costs us a bounded
#: buffer and one reconnect, not memory.
SEND_QUEUE_LIMIT = 2_000

#: Concurrent sessions one server will host. A controller that asks for more is
#: refused rather than being allowed to fork unbounded agent stacks on someone
#: else's machine.
MAX_SESSIONS = 16

#: Unauthenticated connections tolerated at once, total. Bounds what a bare
#: port scanner can hold open even though every one of them is also
#: individually capped by ``AUTH_TIMEOUT_SEC``.
MAX_PENDING_CONNS = 32

#: How much of a session's newest human-readable line is carried in
#: :meth:`RemoteSession.describe`. Bounded because ``auth_ok`` and
#: ``sessions_list`` carry one descriptor per session, so an unbounded field
#: would let a single verbose reply dominate a frame that exists to summarise
#: the whole machine.
LAST_MESSAGE_MAX_CHARS = 160

#: ``publish_event`` method → the role tag reported alongside the text, and the
#: index of the argument the text lives in. Only genuinely human-readable lines
#: are here: the controlled operator's dashboard wants "what was last said", which is
#: not the same question as "what state is it in" (``last_state`` answers that
#: one already, from the same call site).
_LAST_MESSAGE_METHODS: Dict[str, tuple] = {
    "show_user_message_echo": ("user", 0),
    "show_coordinator_reply": ("agent", 0),
    "show_user_notice": ("notice", 0),
    "display_error": ("error", 0),
}


class RemoteFlowHandle(Protocol):
    """What the server needs from whatever drives the agent."""

    async def on_user_message(self, text: str) -> str: ...
    async def destroy(self) -> None: ...


class SessionHost(Protocol):
    """Host-environment hook. Implemented by the Electron bridge and the Linux daemon."""

    def describe(self) -> Dict[str, str]:
        """``{"name": ..., "platform": ...}`` for the ``auth_ok`` frame."""
        ...

    async def create_flow(self, session: "RemoteSession", goal: str) -> RemoteFlowHandle:
        """Build + start a flow whose ``InteractionManager`` delegate is a
        :class:`~src.remote_control.network_delegate.NetworkUIDelegate` bound to
        ``session``. Raise to refuse the session."""
        ...

    async def handle_user_input(
        self, session: "RemoteSession", kind: str, payload: Dict[str, Any]
    ) -> None:
        """Non-message input (currently only ``desktop_takeover_revoked``)."""
        ...

    async def handle_rpc(
        self, session: "RemoteSession", action: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Answer a session-scoped question (currently only ``file_undo``).
        Raise to have the failure reported back as ``ok=False``."""
        ...

    async def push_skills(
        self, skills: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Mirror each given skill folder into this machine's user Skill
        root, overwriting whatever is currently there under that name.

        Connection-scoped, not session-scoped — unlike ``handle_rpc``/
        ``handle_user_input`` this has nothing to do with any particular
        ``RemoteSession``; it is gated by the connection's auth alone (see
        ``SKILL_PUSH``'s protocol comment). Returns one
        ``{name, ok, error}`` per input skill; a failure on one entry must
        not stop the rest from being written.
        """
        ...

    def on_client_released(self) -> None:
        """The controller sent ``release_server`` (explicit Disconnect), and the
        server has already destroyed every session and dropped the connection.

        This is where the two host environments diverge: the Windows bridge
        returns to normal local operation (keeps listening or not, per its own
        UI state), while the Linux daemon — whose only reason to exist is being
        driven — exits the process. Default is a no-op so a host that doesn't
        care needn't implement it.
        """
        ...

    def on_session_destroyed(self, session: "RemoteSession") -> Any:
        """A session is gone for good — release everything keyed on its id.

        Exists because ``create_flow`` is not symmetric with ``flow.destroy()``.
        Both hosts build a session by going through their *ordinary* local
        session-setup path, which allocates far more than the flow object:
        the Windows bridge registers the flow in ``_flows``, a per-session LLM
        service list, a ``_StdioUI``, a reply sink, a dispatch lock, and a file
        handler attached to the ROOT logger. Awaiting ``flow.destroy()`` frees
        none of that, and no other code path ever runs for a ``rc-`` session
        (the controlled machine has no tab for it, so no ``close_session`` IPC ever
        arrives). Without this hook every session the machine has ever served
        stays pinned for the life of the process.

        Called after the flow has been destroyed, so implementations can assume
        the agent is already stopped. May be sync or ``async`` — the server
        awaits the result if it is awaitable, because the Windows bridge has to
        drain per-session httpx pools here. Must not raise; the server logs and
        continues if it does, because a failed cleanup must not leave the
        session half-destroyed in the registry.
        """
        ...


class _Conn:
    """One accepted TCP connection, with its own ordered outbound queue."""

    def __init__(self, jc: JsonlConnection, loop: asyncio.AbstractEventLoop) -> None:
        self.jc = jc
        self.loop = loop
        self.authed = False
        self.client_id = ""
        self.client_name = ""
        self.closed = False
        self.last_seen = time.monotonic()
        #: Sessions this connection currently owns. Used to detach on close.
        self.sessions: Dict[str, "RemoteSession"] = {}
        # deque + Event rather than asyncio.Queue: `publish_event` may be called
        # from a tool's executor thread, and only `call_soon_threadsafe` is safe
        # to invoke from there. deque append/popleft are atomic in CPython.
        self._outbox: Deque[Dict[str, Any]] = deque()
        self._wake = asyncio.Event()
        self._overflow = False

    @property
    def peer(self) -> str:
        try:
            return str(self.jc.peername)
        except Exception:
            return "?"

    def enqueue(self, frame: Dict[str, Any]) -> bool:
        """Queue one frame, preserving order. Safe from any thread.

        Returns False when the queue has overflowed, meaning the caller should
        consider this connection lost.
        """
        if self.closed or self._overflow:
            return False
        if len(self._outbox) >= SEND_QUEUE_LIMIT:
            self._overflow = True
            logger.warning(
                "remote_control: send queue overflow for %s; dropping connection",
                self.peer,
            )
            self.loop.call_soon_threadsafe(self._wake.set)
            return False
        self._outbox.append(frame)
        self.loop.call_soon_threadsafe(self._wake.set)
        return True

    async def writer_loop(self) -> None:
        """Drain the outbox in order. One task per connection, so two events can
        never reach the wire out of the order the delegate produced them."""
        while not self.closed:
            if not self._outbox:
                if self._overflow:
                    break
                self._wake.clear()
                if not self._outbox:  # re-check after clear closes the race
                    await self._wake.wait()
                continue
            frame = self._outbox.popleft()
            try:
                await self.jc.send(frame)
            except Exception:
                logger.debug("remote_control: send failed to %s", self.peer, exc_info=True)
                break
        if self._overflow:
            await self.close()

    async def send_now(self, frame: Dict[str, Any]) -> None:
        """Bypass the queue for handshake frames written from the read loop.

        Safe because it is only used before any event traffic exists for this
        connection (``auth_ok``) or for terminal frames (``session_closed``,
        ``error``), where ordering against the event stream is not meaningful.
        ``JsonlConnection.send`` holds its own write lock, so this cannot
        interleave a half-line with the writer loop.
        """
        try:
            await self.jc.send(frame)
        except Exception:
            logger.debug("remote_control: direct send failed to %s", self.peer, exc_info=True)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._wake.set()
        await self.jc.close()


class _PendingConfirm:
    """A confirmation the controlled agent is blocked on.

    Lives on the session, not on the connection, which is the mechanical reason a
    disconnect cannot disturb it.
    """

    __slots__ = ("request_id", "method", "args", "kwargs", "future", "created_at")

    def __init__(
        self,
        request_id: str,
        method: str,
        args: List[Any],
        kwargs: Dict[str, Any],
        future: "asyncio.Future[Any]",
    ) -> None:
        self.request_id = request_id
        self.method = method
        self.args = args
        self.kwargs = kwargs
        self.future = future
        self.created_at = time.time()

    def to_frame(self, session_id: str) -> Dict[str, Any]:
        return protocol.make_confirm_request(
            session_id, self.request_id, self.method, self.args, self.kwargs
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "args": self.args,
            "kwargs": self.kwargs,
            "created_at": self.created_at,
        }


class RemoteSession:
    """One agent session on this machine, driven from elsewhere."""

    def __init__(
        self,
        session_id: str,
        title: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.session_id = session_id
        self.title = title
        self.loop = loop
        #: High-entropy, per-session, and independent of the daemon-wide auth
        #: token. Holding the auth token gets you a connection; it does not get
        #: you someone else's session. (design doc §3 mechanism 2)
        self.capability = secrets.token_hex(32)
        self.generation = 0
        self.owner: Optional[_Conn] = None
        self.event_log = EventLog()
        self.pending_confirms: Dict[str, _PendingConfirm] = {}
        self.flow: Optional[RemoteFlowHandle] = None
        self.closed = False
        self.created_at = time.time()
        #: Wall-clock of the newest published event, so the controller can sort
        #: its chips by "most recently did something" rather than by when the
        #: local tab happened to last be open (which is what its own record's
        #: ``updated_at`` measures, and is a different question).
        self.last_activity_at = self.created_at
        self.last_state = ""
        #: Newest human-readable line this session produced, and who produced
        #: it (see ``_LAST_MESSAGE_METHODS``). Exists for the controlled operator's
        #: dashboard: that machine has no tab for a ``rc-`` session by design,
        #: so without this the only thing visible about a session running real
        #: tools on their box was an opaque id. Derived from the same
        #: ``publish_event`` call ``last_state`` is derived from, so it costs
        #: nothing extra and can never disagree with the event log.
        self.last_message = ""
        self.last_message_role = ""
        #: Display name of the controller that opened this session, for the controlled
        #: machine's own UI. Set by the server at open time.
        self.controller_name = ""
        #: Whether the Coordinator has EVER classified a turn in this session
        #: as "queue" (a real task, not idle chat). Set by the host's
        #: on_intent_classified hook — see SessionHost.create_flow's docstring
        #: and _BridgeSessionHost / _LinuxSessionHost's wiring.
        #:
        #: Purely descriptive. It used to gate whether the controller kept a
        #: re-adopt record on release, and that was wrong twice over: a
        #: chat-only session on either controlled platform is a real
        #: ``FlowControllerV2`` with a real workspace that survives a detach
        #: exactly like a task does, so dropping its record orphaned a live
        #: session nobody could reach; and the controller learned the flag from
        #: a one-shot replayable event, which a re-attach with a later
        #: ``since_seq`` never replays — so re-adopting a finished task and
        #: closing the tab again silently deleted its record. Both are gone:
        #: the controller keeps a record for every session, and this flag now
        #: only rides along in :meth:`describe` so the panel can badge a chip
        #: "task" or "chat".
        self.is_task = False

    # ── Called from the delegate (possibly off-loop) ─────────────────────────

    def publish_event(self, method: str, args: List[Any]) -> None:
        """Record an event and push it to the attached controller, if any.

        When nobody is attached this is a pure log append — the log *is* the
        buffer, so there is no separate "offline queue" to size or drain.
        """
        seq = self.event_log.append(method, args)
        self.last_activity_at = time.time()
        if method == "show_state_changed" and args:
            self.last_state = str(args[0])
        else:
            summary = _LAST_MESSAGE_METHODS.get(method)
            if summary is not None:
                role, index = summary
                if len(args) > index and args[index] is not None:
                    text = " ".join(str(args[index]).split())
                    if text:
                        self.last_message = text[:LAST_MESSAGE_MAX_CHARS]
                        self.last_message_role = role
        owner = self.owner
        if owner is None:
            return
        frame = protocol.make_agent_event(self.session_id, seq, method, args)
        if not owner.enqueue(frame):
            # Overflowed or closed; the writer loop tears the conn down and the
            # controller replays from the log on reattach.
            pass

    def mark_task_started(self) -> None:
        """Called the first time the Coordinator classifies a turn in this
        session as "queue" (a real task — see ``_on_coordinator_intent`` in the
        Windows bridge and the ``_on_intent_classified`` lambda in the Linux
        host). Idempotent: this only ever transitions False → True.

        Sets the flag and nothing else. It deliberately does NOT publish an
        event: doing so put a name that is not a UI delegate method
        ("mark_task_started") into the replayable event log, which every
        consumer then had to special-case, and made the controller's copy of
        the flag depend on that one event surviving inside the replay window —
        it does not, so a re-attach past its seq lost it. ``describe()`` carries
        the flag instead, which is replay-independent and always current.
        """
        self.is_task = True

    def register_confirm(
        self,
        request_id: str,
        method: str,
        args: List[Any],
        kwargs: Dict[str, Any],
        future: "asyncio.Future[Any]",
    ) -> None:
        pending = _PendingConfirm(request_id, method, args, kwargs, future)
        self.pending_confirms[request_id] = pending
        owner = self.owner
        if owner is not None:
            owner.enqueue(pending.to_frame(self.session_id))
        else:
            logger.info(
                "remote_control: session %s parked confirm %s (%s) with no controller attached",
                self.session_id, request_id, method,
            )

    def discard_confirm(self, request_id: str) -> None:
        self.pending_confirms.pop(request_id, None)

    def discard_confirm_and_notify(self, request_id: str) -> None:
        """Like :meth:`discard_confirm`, but also tells the connected owner
        (if any) to cancel whatever it already relayed for this
        ``request_id`` — used when the controlled side gives up on its own
        (ask_human's 30-minute timeout fired) rather than the session being
        torn down. ``discard_confirm`` stays silent bookkeeping-only for the
        teardown/cancellation case; this is the "I gave up, please close your
        modal" signal for the still-connected case."""
        self.pending_confirms.pop(request_id, None)
        owner = self.owner
        if owner is not None:
            owner.enqueue(protocol.make_confirm_cancel(self.session_id, request_id))

    def resolve_confirm(self, request_id: str, value: Any) -> bool:
        pending = self.pending_confirms.pop(request_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            # set_result, never cancel(): InteractionManager._await_delegate
            # re-raises CancelledError (interaction_manager.py:289), so a
            # cancelled future would surface to the agent as "the task was
            # cancelled" rather than as the user's actual answer.
            self.loop.call_soon_threadsafe(pending.future.set_result, value)
        return True

    # ── Attach / detach ──────────────────────────────────────────────────────

    def on_owner_lost(self, conn: _Conn) -> None:
        """The controlling connection went away.

        Pending confirmations are left registered on purpose — the agent stays
        blocked and the next controller to attach is handed them again. Nothing
        is resolved, nothing is cancelled, and the agent never learns that the
        human's screen went away.
        """
        if self.owner is conn:
            self.owner = None
            if self.pending_confirms:
                logger.info(
                    "remote_control: session %s lost its controller with %d "
                    "confirmation(s) parked; agent stays blocked until reattach",
                    self.session_id, len(self.pending_confirms),
                )

    def describe(self) -> Dict[str, Any]:
        """Summary for ``auth_ok`` / ``sessions_list``, so a controller can see
        what exists here without having to remember more than
        ``(id, capability)`` — and can tell what has stopped existing.

        This is the one authoritative account of a session's existence. The
        controller reconciles its persisted records against a list of these
        (``hub.refresh_sessions``), so anything the panel needs to render a chip
        honestly has to be in here: ``pending_confirms`` because a session
        blocked on a human with no tab open is otherwise invisible, ``is_task``
        for the chip badge, ``title`` because the controller never stored one,
        ``last_message``/``last_message_role`` because the controlled machine has no
        tab for this session at all and its dashboard row is the only place its
        operator can see what is being said on their hardware.

        Every field here is additive with respect to the wire protocol: a peer
        that doesn't know a key ignores it, which is why growing this does not
        earn a ``PROTOCOL_VERSION`` bump (see protocol.py's note).
        """
        return {
            "session_id": self.session_id,
            "title": self.title,
            "state": self.last_state,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "attached": self.owner is not None,
            "pending_confirms": len(self.pending_confirms),
            "cur_seq": self.event_log.cur_seq,
            "is_task": self.is_task,
            "last_message": self.last_message,
            "last_message_role": self.last_message_role,
        }



class RemoteControlServer:
    """``asyncio.start_server`` + auth + heartbeat + a session registry."""

    def __init__(
        self,
        token: str,
        host: SessionHost,
        *,
        server_name: str = "",
        max_sessions: int = MAX_SESSIONS,
    ) -> None:
        if not token:
            raise ValueError("remote control server requires a non-empty token")
        self._token = token
        self._host = host
        self._server_name = server_name
        self._max_sessions = max_sessions
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: Dict[str, RemoteSession] = {}
        self._conns: set[_Conn] = set()
        self._pending_conns = 0
        self._port = 0
        self._bind = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: set[asyncio.Task] = set()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        """The port actually bound. Meaningful only after :meth:`start`."""
        return self._port

    @property
    def token(self) -> str:
        return self._token

    def sessions(self) -> List[RemoteSession]:
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Optional[RemoteSession]:
        return self._sessions.get(session_id)

    async def start(self, bind: str = "0.0.0.0", port: int = 0) -> int:
        """Bind and begin accepting. Returns the port (resolved when ``port=0``).

        ``port=0`` is the default because it is the mode the user already
        validated in the real machine pool, and because it means two HandQ
        instances on one host never collide.
        """
        self._loop = asyncio.get_running_loop()
        self._bind = bind
        self._server = await asyncio.start_server(self._handle, bind, port)
        sockets = self._server.sockets or []
        self._port = int(sockets[0].getsockname()[1]) if sockets else int(port)
        logger.info("remote_control: listening on %s:%d", bind, self._port)
        return self._port

    async def disconnect_client(self) -> int:
        """Drop the current controller and destroy every session it was driving,
        but KEEP LISTENING for the next one.

        This is the v6 "Disconnect Client" action, and it is deliberately more
        destructive than a network drop: a network drop parks sessions so the
        same controller can pick them back up, whereas this is the controlled operator
        saying "I'm done serving this client". Nothing is left behind for the
        next controller to inherit — it gets a clean server.

        Returns the number of sessions destroyed, so the caller can report it.

        Distinct from :meth:`stop`, which additionally closes the listener and
        takes this machine out of server mode entirely.
        """
        destroyed = 0
        for session in list(self._sessions.values()):
            await self._destroy_session(session, protocol.REASON_CLOSED_BY_CONTROLLER)
            destroyed += 1

        for conn in list(self._conns):
            await conn.send_now(
                protocol.make_error(
                    protocol.REASON_CLOSED_BY_CONTROLLER,
                    "disconnected by the server operator",
                )
            )
            await conn.close()
        self._conns.clear()
        logger.info(
            "remote_control: disconnected client, destroyed %d session(s), "
            "still listening on :%d", destroyed, self._port,
        )
        return destroyed

    async def close_session_by_id(self, session_id: str) -> bool:
        """Destroy one session from the controlled side (dashboard "Close" button).

        The controller's tab learns about it through the ordinary
        ``session_closed`` frame and cannot recover it — that is the intended
        semantics: a session closed here is over, not parked.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        await self._destroy_session(session, protocol.REASON_CLOSED_BY_CONTROLLER)
        return True

    async def stop(self) -> None:
        """Close the listener, tell every controller why, tear down every session."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

        for session in list(self._sessions.values()):
            await self._destroy_session(session, protocol.REASON_SERVER_SHUTDOWN)

        for conn in list(self._conns):
            await conn.send_now(
                protocol.make_error(protocol.REASON_SERVER_SHUTDOWN, "server stopping")
            )
            await conn.close()
        self._conns.clear()

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def _spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        try:
            task.set_name(name)
        except Exception:
            pass
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── Connection handling ──────────────────────────────────────────────────

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        loop = asyncio.get_running_loop()
        conn = _Conn(JsonlConnection(reader, writer), loop)

        if self._pending_conns >= MAX_PENDING_CONNS:
            logger.warning(
                "remote_control: refusing %s — too many unauthenticated connections",
                conn.peer,
            )
            await conn.close()
            return

        self._pending_conns += 1
        self._conns.add(conn)
        writer_task: Optional[asyncio.Task] = None
        watchdog: Optional[asyncio.Task] = None
        pinger: Optional[asyncio.Task] = None
        try:
            if not await self._authenticate(conn):
                return
            self._pending_conns -= 1

            writer_task = self._spawn(conn.writer_loop(), f"rc-writer-{conn.client_id}")
            watchdog = self._spawn(self._watchdog(conn), f"rc-watchdog-{conn.client_id}")
            pinger = self._spawn(self._pinger(conn), f"rc-pinger-{conn.client_id}")

            await self._read_loop(conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("remote_control: connection handler failed for %s", conn.peer)
        finally:
            if not conn.authed:
                self._pending_conns = max(0, self._pending_conns - 1)
            for task in (watchdog, pinger, writer_task):
                if task is not None:
                    task.cancel()
            self._conns.discard(conn)
            self._detach_all(conn)
            await conn.close()
            logger.info("remote_control: connection from %s closed", conn.peer)

    async def _authenticate(self, conn: _Conn) -> bool:
        """First frame must be a valid ``auth``. Anything else drops the conn."""
        try:
            frame = await asyncio.wait_for(
                conn.jc.recv(), timeout=protocol.AUTH_TIMEOUT_SEC
            )
        except (asyncio.TimeoutError, Exception):
            await conn.send_now(
                protocol.make_error(protocol.REASON_AUTH_TIMEOUT, "no auth frame")
            )
            return False

        if not frame or frame.get("t") != protocol.AUTH:
            await conn.send_now(
                protocol.make_error(protocol.REASON_AUTH_REQUIRED, "expected auth frame")
            )
            return False

        peer_version = int(frame.get("protocol_version") or 0)
        if peer_version != protocol.PROTOCOL_VERSION:
            await conn.send_now(
                protocol.make_error(
                    protocol.REASON_VERSION_MISMATCH,
                    f"server speaks v{protocol.PROTOCOL_VERSION}, client sent v{peer_version}",
                )
            )
            return False

        supplied = str(frame.get("token") or "")
        # Constant-time: a length-independent early return would leak token
        # length, and a plain == would leak a prefix through timing.
        if not secrets.compare_digest(supplied, self._token):
            logger.warning("remote_control: rejected %s — bad token", conn.peer)
            await conn.send_now(
                protocol.make_error(protocol.REASON_INVALID_TOKEN, "token rejected")
            )
            return False

        conn.authed = True
        conn.client_id = str(frame.get("client_id") or "")[:64]
        conn.client_name = str(frame.get("client_name") or "")[:120]
        conn.last_seen = time.monotonic()

        described = self._host.describe()
        await conn.send_now(
            protocol.make_auth_ok(
                server_name=self._server_name or described.get("name", ""),
                platform=described.get("platform", ""),
                sessions=[s.describe() for s in self._sessions.values()],
            )
        )
        logger.info(
            "remote_control: %s authenticated as %s (%s)",
            conn.peer, conn.client_name or "?", conn.client_id or "?",
        )
        return True

    async def _read_loop(self, conn: _Conn) -> None:
        """Dispatch frames until the peer really is gone.

        The timeout here is generous on purpose and a bare timeout is NOT a
        disconnect: liveness is the watchdog's call, and only the watchdog's.
        Conflating "no frame arrived for a while" with "peer is gone" in the read
        loop is what made the prototype's scenario 5 (silent peer, socket still
        open, no EOF) necessary to write in the first place.
        """
        idle_budget = protocol.HEARTBEAT_TIMEOUT_SEC + protocol.HEARTBEAT_INTERVAL_SEC
        while not conn.closed:
            try:
                frame = await conn.jc.recv(timeout=idle_budget)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if frame is None:
                break
            conn.last_seen = time.monotonic()
            try:
                await self._dispatch(conn, frame)
            except Exception:
                logger.exception(
                    "remote_control: dispatch failed for %s frame from %s",
                    frame.get("t"), conn.peer,
                )

    async def _watchdog(self, conn: _Conn) -> None:
        while not conn.closed:
            await asyncio.sleep(protocol.HEARTBEAT_INTERVAL_SEC / 2)
            if time.monotonic() - conn.last_seen > protocol.HEARTBEAT_TIMEOUT_SEC:
                logger.info(
                    "remote_control: %s silent for >%.0fs; closing",
                    conn.peer, protocol.HEARTBEAT_TIMEOUT_SEC,
                )
                await conn.close()
                return

    async def _pinger(self, conn: _Conn) -> None:
        while not conn.closed:
            await asyncio.sleep(protocol.HEARTBEAT_INTERVAL_SEC)
            conn.enqueue(protocol.make_ping(time.time()))

    def _detach_all(self, conn: _Conn) -> None:
        for session in list(conn.sessions.values()):
            session.on_owner_lost(conn)
        conn.sessions.clear()

    # ── Frame dispatch ───────────────────────────────────────────────────────

    async def _dispatch(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        kind = frame.get("t")

        if kind == protocol.PONG:
            return
        if kind == protocol.PING:
            conn.enqueue(protocol.make_pong(frame.get("ts") or time.time()))
            return
        if kind == protocol.OPEN_SESSION:
            await self._open_session(conn, frame)
            return
        if kind == protocol.ATTACH_SESSION:
            await self._attach_session(conn, frame)
            return
        if kind == protocol.LIST_SESSIONS:
            # Connection-scoped, so no capability check: the auth token already
            # earned the right to know what this server hosts, and the answer
            # grants nothing — driving any of these still needs that session's
            # capability.
            conn.enqueue(
                protocol.make_sessions_list(
                    [s.describe() for s in self._sessions.values()]
                )
            )
            return
        if kind == protocol.DETACH_SESSION:
            session = self._sessions.get(str(frame.get("session_id") or ""))
            if session is not None:
                session.on_owner_lost(conn)
                conn.sessions.pop(session.session_id, None)
            return
        if kind == protocol.CLOSE_SESSION:
            await self._close_session(conn, frame)
            return
        if kind == protocol.USER_MESSAGE:
            await self._user_message(conn, frame)
            return
        if kind == protocol.USER_INPUT:
            await self._user_input(conn, frame)
            return
        if kind == protocol.SESSION_RPC:
            await self._session_rpc(conn, frame)
            return
        if kind == protocol.SKILL_PUSH:
            # Own task, same reasoning as SESSION_RPC: file I/O must not block
            # the read loop, which still has to answer heartbeats.
            self._spawn(self._skill_push(conn, frame), f"rc-skill-push-{conn.peer}")
            return
        if kind == protocol.SKILL_LIST:
            self._spawn(self._skill_list(conn), f"rc-skill-list-{conn.peer}")
            return
        if kind == protocol.CONFIRM_RESPONSE:
            self._confirm_response(conn, frame)
            return
        if kind == protocol.RELEASE_SERVER:
            # Explicit "I'm done with you" — destroy every session (not park),
            # drop the connection, then let the host react (Linux exits, Windows
            # returns to normal). See RELEASE_SERVER's protocol comment.
            await self.disconnect_client()

            try:
                self._host.on_client_released()
            except Exception:
                logger.exception("remote_control: on_client_released hook failed")
            return

        logger.warning("remote_control: unhandled frame %r from %s", kind, conn.peer)

    def _owned(self, conn: _Conn, frame: Dict[str, Any]) -> Optional[RemoteSession]:
        """Resolve the frame's session, but only if this conn currently owns it.

        Ownership rather than capability is checked for steady-state traffic:
        capability is what earns you the attach, and the attach is what makes you
        the owner. Re-checking capability per frame would let a superseded
        controller keep injecting messages into a session it has been kicked off.
        """
        session_id = str(frame.get("session_id") or "")
        session = self._sessions.get(session_id)
        if session is None:
            conn.enqueue(
                protocol.make_session_closed(session_id, protocol.REASON_UNKNOWN_SESSION)
            )
            return None
        if session.owner is not conn:
            conn.enqueue(
                protocol.make_error(
                    protocol.REASON_NOT_ATTACHED,
                    f"session {session_id} is not attached to this connection",
                )
            )
            return None
        return session

    async def _open_session(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        if len(self._sessions) >= self._max_sessions:
            await conn.send_now(
                protocol.make_error(
                    protocol.REASON_TOO_MANY_SESSIONS,
                    f"server already hosts {len(self._sessions)} sessions",
                )
            )
            return

        goal = str(frame.get("goal") or "")
        title = str(frame.get("title") or "").strip() or goal.strip()[:30]
        session_id = f"rc-{secrets.token_hex(8)}"
        assert self._loop is not None
        session = RemoteSession(session_id, title, self._loop)
        session.controller_name = conn.client_name or conn.peer
        self._sessions[session_id] = session

        # Attach BEFORE building the flow, so events emitted during start-up
        # (state changes, the first task plan) reach the controller live instead
        # of only via replay.
        session.owner = conn
        conn.sessions[session_id] = session
        await conn.send_now(
            protocol.make_session_opened(session_id, session.capability, title)
        )

        try:
            session.flow = await self._host.create_flow(session, goal)
        except Exception as exc:
            logger.exception("remote_control: create_flow failed for %s", session_id)
            self._sessions.pop(session_id, None)
            conn.sessions.pop(session_id, None)
            conn.enqueue(
                protocol.make_session_closed(session_id, protocol.REASON_SESSION_FAILED)
            )
            conn.enqueue(protocol.make_error(protocol.REASON_SESSION_FAILED, str(exc)))
            return

        if goal:
            session.publish_event("show_user_message_echo", [goal])
            self._spawn(
                self._deliver_message(session, goal), f"rc-goal-{session_id}"
            )

    async def _attach_session(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session_id = str(frame.get("session_id") or "")
        capability = str(frame.get("capability") or "")
        since_seq = int(frame.get("since_seq") or 0)

        session = self._sessions.get(session_id)
        if session is None:
            await conn.send_now(
                protocol.make_session_closed(session_id, protocol.REASON_UNKNOWN_SESSION)
            )
            return
        if not secrets.compare_digest(capability, session.capability):
            logger.warning(
                "remote_control: %s presented a bad capability for %s",
                conn.peer, session_id,
            )
            await conn.send_now(
                protocol.make_session_closed(
                    session_id, protocol.REASON_INVALID_CAPABILITY
                )
            )
            return

        previous = session.owner
        if previous is not None and previous is not conn:
            # Never displace an owner silently — it would look to that operator
            # like the session simply went quiet. (design doc §3 mechanism 3)
            session.generation += 1
            await previous.send_now(
                protocol.make_session_superseded(session_id, session.generation)
            )
            previous.sessions.pop(session_id, None)
            await previous.close()

        session.owner = conn
        conn.sessions[session_id] = session

        replay = session.event_log.replay_since(since_seq)
        for seq, method, args in replay.events:
            conn.enqueue(
                protocol.make_agent_event(session_id, seq, method, args)
            )
        conn.enqueue(
            protocol.make_session_attached(
                session_id=session_id,
                cur_seq=replay.cur_seq,
                gap=replay.gap,
                snapshot=session.event_log.snapshot(),
                pending_confirms=[
                    p.describe() for p in session.pending_confirms.values()
                ],
            )
        )
        # Re-offer every parked confirmation as a live request. These are NOT in
        # the replayed event stream on purpose: an already-answered confirmation
        # replayed as a request would open a modal nobody can ever close.
        for pending in list(session.pending_confirms.values()):
            conn.enqueue(pending.to_frame(session_id))

        logger.info(
            "remote_control: %s attached to %s (replayed %d event(s) from seq %d, "
            "gap=%s, %d parked confirm(s))",
            conn.peer, session_id, len(replay.events), since_seq,
            replay.gap, len(session.pending_confirms),
        )

    async def _user_message(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session = self._owned(conn, frame)
        if session is None:
            return
        text = str(frame.get("text") or "")
        # Publish the echo BEFORE spawning the turn, so its seq precedes every
        # event the resulting coordinator turn produces — a reattach that
        # lands mid-reply must see the user's own message first, in the
        # order it actually happened.
        session.publish_event("show_user_message_echo", [text])
        # Dispatched as its own task: on_user_message runs the coordinator turn
        # and can take a long time, and the read loop must stay free to accept
        # the confirmation responses that turn may end up asking for.
        self._spawn(
            self._deliver_message(session, text), f"rc-msg-{session.session_id}"
        )

    async def _deliver_message(self, session: RemoteSession, text: str) -> None:
        flow = session.flow
        if flow is None:
            return
        try:
            await flow.on_user_message(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "remote_control: on_user_message failed for %s", session.session_id
            )
            session.publish_event("display_error", [f"Remote failed to process message: {exc}"])

    async def _user_input(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session = self._owned(conn, frame)
        if session is None:
            return
        kind = str(frame.get("kind") or "")
        payload = frame.get("payload")
        try:
            await self._host.handle_user_input(
                session, kind, payload if isinstance(payload, dict) else {}
            )
        except Exception:
            logger.exception("remote_control: user_input %s failed", kind)

    async def _session_rpc(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session = self._owned(conn, frame)
        if session is None:
            return
        rpc_id = str(frame.get("rpc_id") or "")
        action = str(frame.get("action") or "")
        payload = frame.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        # Its own task: an RPC may take as long as the operation it wraps, and
        # the read loop has to stay available for heartbeats and confirmations.
        self._spawn(
            self._run_rpc(session, conn, rpc_id, action, payload),
            f"rc-rpc-{action}-{session.session_id}",
        )

    async def _run_rpc(
        self,
        session: RemoteSession,
        conn: _Conn,
        rpc_id: str,
        action: str,
        payload: Dict[str, Any],
    ) -> None:
        try:
            result = await self._host.handle_rpc(session, action, payload)
            conn.enqueue(
                protocol.make_session_rpc_result(
                    session.session_id, rpc_id, True, result
                )
            )
        except Exception as exc:
            logger.exception("remote_control: rpc %s failed", action)
            conn.enqueue(
                protocol.make_session_rpc_result(
                    session.session_id, rpc_id, False, None, str(exc)
                )
            )

    async def _skill_push(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        skills = frame.get("skills")
        skills = skills if isinstance(skills, list) else []
        try:
            results = await self._host.push_skills(skills)
        except Exception as exc:
            logger.exception("remote_control: skill push failed")
            results = [
                {"name": s.get("name"), "ok": False, "error": str(exc)}
                for s in skills
                if isinstance(s, dict)
            ]
        conn.enqueue(protocol.make_skill_push_result(results))

    async def _skill_list(self, conn: _Conn) -> None:
        """Answer "what skills do you have" straight off ``SkillRegistry``,
        with no ``SessionHost`` involvement — unlike ``create_flow``/
        ``handle_rpc``, listing skills has nothing platform-specific about it:
        the registry's root is resolved purely from the local install, the
        same on the Windows bridge and the Linux daemon. Connection-scoped
        like ``SKILL_PUSH``, so no capability check.
        """
        try:
            from ..infrastructure.skills import SkillRegistry

            def _reload_and_list():
                registry = SkillRegistry.get()
                registry.reload()
                return registry.list_all(include_bundled=True)

            entries = await asyncio.to_thread(_reload_and_list)
            skills = [
                {
                    "name": e.get("name"),
                    "description": e.get("description"),
                    "origin": e.get("origin"),
                    "enabled": e.get("enabled"),
                    "mtime": e.get("mtime"),
                }
                for e in entries
            ]
        except Exception:
            logger.exception("remote_control: skill list failed")
            skills = []
        conn.enqueue(protocol.make_skill_list_result(skills))

    def _confirm_response(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session = self._owned(conn, frame)
        if session is None:
            return
        request_id = str(frame.get("request_id") or "")
        if not session.resolve_confirm(request_id, frame.get("value")):
            # Benign and expected: a controller that reconnects may answer a
            # confirmation the previous owner already answered.
            logger.debug(
                "remote_control: confirm_response for unknown request %s", request_id
            )

    async def _close_session(self, conn: _Conn, frame: Dict[str, Any]) -> None:
        session_id = str(frame.get("session_id") or "")
        capability = str(frame.get("capability") or "")
        session = self._sessions.get(session_id)
        if session is None:
            # Acknowledge instead of returning silently. "Already gone" is a
            # success for the caller — it asked for this session not to exist —
            # and the controller now WAITS for confirmation before dropping its
            # own record (see protocol.CLOSE_CONFIRM_TIMEOUT_SEC). Staying quiet
            # here would make a duplicate or late close time out, and the
            # controller would then keep a record for a session this server has
            # never heard of: a chip that can never be cleared.
            await conn.send_now(
                protocol.make_session_closed(
                    session_id, protocol.REASON_UNKNOWN_SESSION
                )
            )
            return
        if secrets.compare_digest(capability, session.capability):
            await self._close_and_ack(conn, session, protocol.REASON_CLOSED_BY_CONTROLLER)
            return
        # No matching capability, but _dispatch never reaches this handler for
        # an unauthenticated connection (CLOSE_SESSION is not in
        # PRE_AUTH_FRAMES) — so conn already proved it holds the auth token.
        # That is enough to force-close (never to attach/drive) any session
        # this server hosts, the same trust LIST_SESSIONS already extends one
        # step further: auth earns "end it", not just "see it".
        await self._close_and_ack(conn, session, protocol.REASON_FORCE_CLOSED)

    async def _close_and_ack(
        self, conn: _Conn, session: RemoteSession, reason: str
    ) -> None:
        """Destroy a session AND make sure the requesting connection hears about
        it, even when that connection is not the owner.

        ``_destroy_session`` only notifies the session's *owner*. That is right
        for a dashboard close or a supersede, but a force-close comes from a
        connection that holds the auth token and not this session's capability —
        i.e. NOT the owner. The controller now waits for a ``session_closed``
        before dropping its record (protocol.CLOSE_CONFIRM_TIMEOUT_SEC), so a
        requester that never gets one would time out and keep a chip for a
        session it just successfully ended. Send it here if the owner path
        didn't already cover this exact connection.
        """
        was_owner = session.owner is conn
        await self._destroy_session(session, reason)
        if not was_owner and not conn.closed:
            conn.enqueue(protocol.make_session_closed(session.session_id, reason))

    async def _destroy_session(self, session: RemoteSession, reason: str) -> None:
        if session.closed:
            return
        session.closed = True
        self._sessions.pop(session.session_id, None)

        owner = session.owner
        if owner is not None:
            owner.enqueue(protocol.make_session_closed(session.session_id, reason))
            owner.sessions.pop(session.session_id, None)
            session.owner = None

        # Only now are parked confirmations released, and with a result rather
        # than a cancellation — the flow is going away, so the awaiting agent
        # needs to unblock, and a `no` is the safe reading of "never answered".
        for pending in list(session.pending_confirms.values()):
            if not pending.future.done():
                from . import codec  # local import: keeps module import cycle-free

                if pending.method in codec.TEXT_METHODS:
                    default: Any = codec.encode_text("")
                elif pending.method in codec.FORM_METHODS:
                    default = codec.encode_form({})
                else:
                    default = {"kind": "confirmation", "type": "no", "message": None}
                pending.future.set_result(default)
        session.pending_confirms.clear()

        if session.flow is not None:
            try:
                await asyncio.wait_for(session.flow.destroy(), timeout=5.0)
            except Exception:
                logger.warning(
                    "remote_control: destroy of %s did not finish cleanly",
                    session.session_id, exc_info=True,
                )
            session.flow = None

        # Let the host release everything else keyed on this session id. Runs
        # even if the flow teardown above failed or timed out — a wedged agent
        # is no reason to also leak the session's logger handler and service
        # pool for the rest of the process's life. See
        # SessionHost.on_session_destroyed for what "everything else" is.
        try:
            hook = getattr(self._host, "on_session_destroyed", None)
            if hook is not None:
                result = hook(session)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            logger.exception(
                "remote_control: on_session_destroyed hook failed for %s",
                session.session_id,
            )
