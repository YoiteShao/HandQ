"""Controlling side — one connection to one remote HandQ, multiplexing N sessions.

The client's job beyond framing is to make a disconnect boring. It owns the
reconnect loop, remembers ``(session_id, capability, seq)`` per session, and on
every successful reconnect re-attaches each session with the ``since_seq`` it
last saw. Callers (a :class:`~src.remote_control.session_bridge.RemoteSessionBridge`
per session) are told ``on_disconnected`` / ``on_reconnected`` and are handed
replayed events through the same callback as live ones, because from the local
UI's point of view there is no difference — a replayed ``notify_tool_execution_started``
should draw the same card it would have drawn at the time.

Outbound user messages are *not* queued while disconnected. A message typed
during an outage and delivered silently four minutes later is worse than a
refusal the operator can act on, so :meth:`send_user_message` raises and the UI
says so.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from ..infrastructure.chatroom.transport import JsonlConnection
from . import protocol
from .address import ControlAddress

logger = logging.getLogger("handq.remote_control.client")

#: Reconnect backoff. Starts fast because most disconnects are a flaky link
#: rather than a dead peer, and caps low enough that an operator who fixes the
#: network doesn't sit waiting on an exponential tail.
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


class SessionSink(Protocol):
    """What the client pushes into, per session. Implemented by RemoteSessionBridge."""

    def on_agent_event(self, method: str, args: List[Any], seq: int) -> None: ...
    def on_confirm_request(
        self, request_id: str, method: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> None: ...
    def on_confirm_cancel(self, request_id: str) -> None: ...
    def on_session_attached(
        self, cur_seq: int, gap: bool, snapshot: Dict[str, Any]
    ) -> None: ...
    def on_session_closed(self, reason: str) -> None: ...
    def on_session_superseded(self, generation: int) -> None: ...
    def on_disconnected(self, detail: str) -> None: ...
    def on_reconnected(self) -> None: ...


class _TrackedSession:
    """Client-side bookkeeping for one remote session."""

    __slots__ = ("session_id", "capability", "since_seq", "sink", "attached")

    def __init__(self, session_id: str, capability: str, sink: SessionSink) -> None:
        self.session_id = session_id
        self.capability = capability
        self.sink = sink
        #: Highest seq handed to the sink. The resume point on reattach.
        self.since_seq = 0
        self.attached = False


class RemoteControlError(RuntimeError):
    """Raised for a failure the caller (and the operator) should see."""


class RemoteControlClient:
    """A resilient connection to one remote HandQ."""

    def __init__(
        self,
        address: ControlAddress,
        *,
        client_name: str = "",
        auto_reconnect: bool = True,
    ) -> None:
        self.address = address
        self.client_id = secrets.token_hex(6)
        self.client_name = client_name or f"handq-{platform.node()}"
        self.auto_reconnect = auto_reconnect

        self.server_name = ""
        self.server_platform = ""
        #: Sessions the server reported at the last successful auth. Lets the UI
        #: offer "re-attach" for a session whose tab was closed locally.
        #: Refreshed by :meth:`list_remote_sessions` too, so a caller that just
        #: asked can read the answer from here as well.
        self.remote_sessions: List[Dict[str, Any]] = []
        #: Called with ``(session_id, reason)`` for a ``session_closed`` naming a
        #: session this client is not tracking. That happens whenever the server
        #: destroys a session we have no live sink for — its tab was closed, so
        #: only a persisted re-adopt record remains. Without this the frame was
        #: dropped on the floor and the record survived as a chip pointing at
        #: nothing. The hub sets this.
        self.on_orphan_session_closed: Optional[Callable[[str, str], None]] = None

        self._jc: Optional[JsonlConnection] = None
        self._sessions: Dict[str, _TrackedSession] = {}
        self._connected = asyncio.Event()
        self._closing = False
        self._tasks: List[asyncio.Task] = []
        self._last_seen = 0.0
        self._open_waiters: List["asyncio.Future[Dict[str, Any]]"] = []
        self._list_waiters: List["asyncio.Future[Dict[str, Any]]"] = []
        self._push_waiters: List["asyncio.Future[Dict[str, Any]]"] = []
        self._skill_list_waiters: List["asyncio.Future[Dict[str, Any]]"] = []
        #: session_id → futures awaiting that session's ``session_closed``. A
        #: destruction the operator asked for is only reported as done once the
        #: controlled side says so; see :meth:`close_remote_session`.
        self._close_waiters: Dict[str, List["asyncio.Future[str]"]] = {}
        #: Awaits the server's acknowledgement of ``release_server``.
        self._release_waiter: Optional["asyncio.Future[str]"] = None
        self._rpc_waiters: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self._state_listeners: List[Callable[[str, str], None]] = []

    # ── Public state ─────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self._jc is not None and not self._jc.closed

    def on_state_change(self, listener: Callable[[str, str], None]) -> None:
        """Register a ``(state, detail)`` listener. State is ``connected`` |
        ``disconnected`` | ``failed``. Used by the bridge host to drive UI."""
        self._state_listeners.append(listener)

    def _emit_state(self, state: str, detail: str = "") -> None:
        for listener in list(self._state_listeners):
            try:
                listener(state, detail)
            except Exception:
                logger.debug("remote_control: state listener failed", exc_info=True)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish the first connection. Raises on a failure worth surfacing.

        Deliberately *not* retried here: the first connect is the operator
        pressing a button, so a bad token or an unreachable host has to come back
        as an error they can read. Retry only takes over once a connection has
        worked at least once.
        """
        await self._connect_once()
        if self.auto_reconnect:
            self._tasks.append(
                asyncio.ensure_future(self._supervise(), )
            )

    async def _connect_once(self) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.address.host, self.address.port),
                timeout=protocol.CONNECT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            raise RemoteControlError(
                f"Connection to {self.address.endpoint} timed out — the target isn't listening, or is blocked by a firewall"
            ) from None
        except OSError as exc:
            raise RemoteControlError(f"Failed to connect to {self.address.endpoint}: {exc}") from exc

        jc = JsonlConnection(reader, writer)
        await jc.send(
            protocol.make_auth(self.address.token, self.client_id, self.client_name)
        )
        try:
            frame = await asyncio.wait_for(jc.recv(), timeout=protocol.AUTH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await jc.close()
            raise RemoteControlError("Target accepted the connection but did not respond to authentication") from None

        if frame is None:
            await jc.close()
            raise RemoteControlError("Target disconnected during authentication")
        if frame.get("t") == protocol.ERROR:
            reason = str(frame.get("reason") or "")
            await jc.close()
            raise RemoteControlError(_explain_auth_failure(reason, frame))
        if frame.get("t") != protocol.AUTH_OK:
            await jc.close()
            raise RemoteControlError(f"Received unexpected frame during authentication: {frame.get('t')!r}")

        self.server_name = str(frame.get("server_name") or "")
        self.server_platform = str(frame.get("platform") or "")
        sessions = frame.get("sessions")
        self.remote_sessions = sessions if isinstance(sessions, list) else []

        self._jc = jc
        self._last_seen = time.monotonic()
        self._connected.set()
        self._tasks.append(asyncio.ensure_future(self._read_loop(jc)))
        self._tasks.append(asyncio.ensure_future(self._watchdog(jc)))
        logger.info(
            "remote_control: connected to %s (%s) at %s",
            self.server_name or "?", self.server_platform or "?",
            self.address.endpoint,
        )
        self._emit_state("connected", self.server_name)

    async def close(self) -> None:
        self._closing = True
        self._connected.clear()
        jc, self._jc = self._jc, None
        if jc is not None:
            await jc.close()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def release(self) -> bool:
        """Tell the server we're done (explicit Disconnect), then close.

        Sends ``release_server`` so the server destroys its sessions and — if
        it's a Linux daemon — exits, rather than parking sessions for a
        reconnect. This is the difference between the operator clicking
        "Disconnect" and the app merely being closed: only the former releases
        the server.

        Returns whether the server ACKNOWLEDGED the release. It answers with an
        ``error(closed_by_controller)`` frame from inside ``disconnect_client``,
        written directly rather than queued, so it is the one part of the release
        that is ordered ahead of the socket teardown and therefore worth waiting
        for.

        A False is informative, not a failure: on the platform where release
        matters most it is the *expected* outcome. A Linux daemon's
        ``on_client_released`` hook exits the process immediately after
        ``disconnect_client`` writes that frame, so the socket often dies while we
        are still waiting on it and ``_on_connection_lost`` fails the waiter — a
        daemon that did exactly as asked reports as unconfirmed. ``hub.release_target``
        therefore treats it as a warning and still completes the local bookkeeping;
        the socket is closed either way, because a release we could not confirm is
        not a reason to keep driving.
        """
        confirmed = False
        if self._jc is not None and self.connected:
            loop = asyncio.get_running_loop()
            waiter: "asyncio.Future[str]" = loop.create_future()
            self._release_waiter = waiter
            try:
                await self._jc.send(protocol.make_release_server())
            except Exception:
                logger.debug("remote_control: release_server send failed",
                             exc_info=True)
                self._release_waiter = None
            else:
                try:
                    await asyncio.wait_for(
                        waiter, timeout=protocol.RELEASE_CONFIRM_TIMEOUT_SEC
                    )
                    confirmed = True
                except (asyncio.TimeoutError, RemoteControlError) as exc:
                    logger.warning(
                        "remote_control: %s did not confirm release_server (%s)",
                        self.address.endpoint,
                        "timeout" if isinstance(exc, asyncio.TimeoutError) else exc,
                    )
                finally:
                    self._release_waiter = None
        # Notify local sinks — the server is destroying these anyway, and
        # after close() we won't read the server's session_closed frames.
        # We're the initiator; our own bookkeeping is the source of truth.
        for tracked in list(self._sessions.values()):
            try:
                tracked.sink.on_session_closed(protocol.REASON_RELEASED_BY_CLIENT)
            except Exception:
                logger.debug("remote_control: sink.on_session_closed raised",
                             exc_info=True)
        self._sessions.clear()
        await self.close()
        return confirmed

    # ── Session operations ───────────────────────────────────────────────────

    async def open_session(self, goal: str, sink: SessionSink, title: str = "") -> str:
        """Ask the remote to create a session; returns its remote session id."""
        jc = self._require_connection()
        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._open_waiters.append(waiter)
        await jc.send(protocol.make_open_session(goal, title))
        try:
            frame = await asyncio.wait_for(waiter, timeout=30.0)
        except asyncio.TimeoutError:
            if waiter in self._open_waiters:
                self._open_waiters.remove(waiter)
            raise RemoteControlError("Remote did not confirm the new session within 30 seconds") from None

        session_id = str(frame.get("session_id") or "")
        capability = str(frame.get("capability") or "")
        if not session_id or not capability:
            raise RemoteControlError("Remote's session_opened response is missing id or capability")
        tracked = _TrackedSession(session_id, capability, sink)
        tracked.attached = True
        self._sessions[session_id] = tracked
        return session_id

    def capability_for(self, session_id: str) -> str:
        """The capability the server minted for a session we opened."""
        tracked = self._sessions.get(session_id)
        return tracked.capability if tracked else ""

    async def list_remote_sessions(
        self, *, timeout: float = 8.0
    ) -> List[Dict[str, Any]]:
        """Ask what sessions the server actually has right now.

        The answer, not this client's ``_sessions`` dict, is what "exists"
        means: we track only the sessions we currently drive, while the server
        also holds the ones whose local tab was closed — and has already
        forgotten the ones its own operator killed. Callers use it to reconcile
        persisted records against reality.

        Raises :class:`RemoteControlError` when disconnected or on timeout,
        rather than returning ``[]``: an empty list is a legitimate answer
        ("this server hosts nothing"), and a caller that reconciles against it
        would delete every record it holds. Those two cases must never be
        conflated, so the failure is loud.
        """
        jc = self._require_connection()
        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._list_waiters.append(waiter)
        await jc.send(protocol.make_list_sessions())
        try:
            frame = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            if waiter in self._list_waiters:
                self._list_waiters.remove(waiter)
            raise RemoteControlError(
                f"Remote did not return the session list within {timeout:.0f} seconds"
            ) from None
        sessions = frame.get("sessions")
        sessions = sessions if isinstance(sessions, list) else []
        self.remote_sessions = sessions
        return sessions

    async def push_skills(
        self, skills: List[Dict[str, Any]], *, timeout: float = 10.0
    ) -> List[Dict[str, Any]]:
        """Push skill folders to the connected server; returns per-skill
        ``{name, ok, error}`` results. Connection-scoped like
        ``list_remote_sessions`` — nothing here belongs to any one session.

        10s, not 30s: the largest real skill folder is ~106KB, so a genuine
        transfer finishes in well under a second even on a slow link. The only
        way this ever times out is a server too old to know ``SKILL_PUSH`` —
        it hits ``_dispatch``'s unhandled-frame fallback and never replies at
        all, so no timeout length would help; a short one just fails fast.
        """
        jc = self._require_connection()
        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._push_waiters.append(waiter)
        await jc.send(protocol.make_skill_push(skills))
        try:
            frame = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            if waiter in self._push_waiters:
                self._push_waiters.remove(waiter)
            raise RemoteControlError(
                f"Remote did not respond to the skill push within {timeout:.0f} seconds"
                " — it may be running an older HandQ build that doesn't support"
                " skill upload yet"
            ) from None
        results = frame.get("results")
        return results if isinstance(results, list) else []

    async def list_remote_skills(
        self, *, timeout: float = 8.0
    ) -> List[Dict[str, Any]]:
        """Ask what skills the server already has — bundled and
        user/uploaded alike. Connection-scoped like ``push_skills``, and the
        same "old server never replies" failure mode: a timeout, not an
        error, is how an unsupporting peer shows up.
        """
        jc = self._require_connection()
        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._skill_list_waiters.append(waiter)
        await jc.send(protocol.make_skill_list())
        try:
            frame = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            if waiter in self._skill_list_waiters:
                self._skill_list_waiters.remove(waiter)
            raise RemoteControlError(
                f"Remote did not return the skill list within {timeout:.0f} seconds"
                " — it may be running an older HandQ build that doesn't support"
                " skill listing yet"
            ) from None
        skills = frame.get("skills")
        return skills if isinstance(skills, list) else []

    def since_seq_for(self, session_id: str) -> int:
        tracked = self._sessions.get(session_id)
        return tracked.since_seq if tracked else 0

    async def attach_session(
        self, session_id: str, capability: str, sink: SessionSink,
        since_seq: int = 0, *, replay_all: bool = False,
    ) -> None:
        """Re-attach to an existing remote session (reconnect, or adopt from a
        stored pairing record after the local tab was closed).

        ``replay_all`` means "this sink has drawn nothing — send everything you
        still have". Pass it whenever ``sink`` is backed by a *fresh* UI, which
        is every re-adopt: a re-adopt mints a new tab whose DOM has never held
        any of this session's events. That is a different question from "where
        did this connection get to", which is what ``since_seq`` answers and
        what the reconnect loop uses.

        Conflating the two is what made a re-adopted tab render blank: the fresh
        tab inherited the *connection*'s resume point, so the server correctly
        withheld the whole transcript — no chat, no activity, no touched files.
        """
        jc = self._require_connection()
        tracked = self._sessions.get(session_id)
        if tracked is None:
            tracked = _TrackedSession(session_id, capability, sink)
            tracked.since_seq = 0 if replay_all else max(0, int(since_seq))
            self._sessions[session_id] = tracked
        else:
            tracked.sink = sink
            if replay_all:
                # Deliberately LOWERS the mark, which the max() below never
                # would. Both halves are needed: the wire request has to ask
                # the server for the full tail, AND the local mark has to drop
                # or ``_dispatch`` discards the replay on arrival (it drops
                # ``seq <= tracked.since_seq``). Safe precisely because the new
                # sink is empty — the risk a high mark guards against is a
                # duplicate, and the risk it creates here is a hole.
                tracked.since_seq = 0
            else:
                # Take the furthest-along position of the two. A caller adopting
                # from a persisted record may know a higher seq than a stale
                # tracked entry (or vice versa after a live run); replaying from
                # the lower one is at worst duplicate content, from the higher one
                # it would be a silent hole.
                tracked.since_seq = max(tracked.since_seq, int(since_seq))
            if capability:
                tracked.capability = capability
        await jc.send(
            protocol.make_attach_session(session_id, capability, tracked.since_seq)
        )

    def forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def detach_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if self.connected and self._jc is not None:
            try:
                await self._jc.send(protocol.make_detach_session(session_id))
            except Exception:
                pass

    async def close_remote_session(self, session_id: str,
                                    capability: str = "", *, force: bool = False) -> None:
        """Tell the controlled side to terminate a session, and WAIT for it to confirm.

        ``capability`` may be passed explicitly for a session this client isn't
        currently tracking (a panel chip whose tab was already closed); when
        omitted it falls back to the tracked capability. The controlled side
        normally requires the capability to authorize the close; ``force=True``
        sends the frame with whatever is available (possibly empty), which the
        controlled side accepts as an auth-token-only close — the one legitimate
        way to end a session this controller has lost the capability for
        (re-pairing, wiped record, another controller's orphan).

        Raises :class:`RemoteControlError` unless the controlled side answered with
        ``session_closed`` for this id. Every early exit here used to be a silent
        ``return``, and the caller's ``finally`` then deleted the local record
        regardless — so a close attempted over a link that had just dropped,
        or without a capability, ended with the record gone and the session
        still running. Confirmation first, local delete second, or neither.
        ``unknown_session`` counts as confirmation: the caller asked for this
        session not to exist, and it does not.
        """
        tracked = self._sessions.pop(session_id, None)
        cap = capability or (tracked.capability if tracked else "")
        if not self.connected or self._jc is None:
            raise RemoteControlError(
                f"Connection to {self.address.display()} was lost — cannot confirm whether the remote destroyed this "
                f"session, please retry after reconnecting"
            )
        if not cap and not force:
            raise RemoteControlError(
                f"This machine has no credentials for session {session_id}, cannot close it normally "
                f"(use \"Force Terminate\" instead)"
            )

        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[str]" = loop.create_future()
        self._close_waiters.setdefault(session_id, []).append(waiter)
        try:
            await self._jc.send(protocol.make_close_session(session_id, cap))
        except Exception as exc:
            self._discard_close_waiter(session_id, waiter)
            raise RemoteControlError(
                f"Failed to send close request: {exc}"
            ) from exc
        try:
            reason = await asyncio.wait_for(
                waiter, timeout=protocol.CLOSE_CONFIRM_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            raise RemoteControlError(
                f"Remote did not confirm within {protocol.CLOSE_CONFIRM_TIMEOUT_SEC:.0f} seconds "
                f"that session {session_id} was destroyed — local record kept, please retry"
            ) from None
        finally:
            # Also covers the link-dropped case, where _on_connection_lost has
            # already failed the future with its own RemoteControlError.
            self._discard_close_waiter(session_id, waiter)
        logger.info(
            "remote_control: %s confirmed %s closed (%s)",
            self.address.endpoint, session_id, reason or "no reason given",
        )

    def _discard_close_waiter(
        self, session_id: str, waiter: "asyncio.Future[str]"
    ) -> None:
        waiters = self._close_waiters.get(session_id)
        if not waiters:
            return
        if waiter in waiters:
            waiters.remove(waiter)
        if not waiters:
            self._close_waiters.pop(session_id, None)

    def _resolve_close_waiters(self, session_id: str, reason: str) -> None:
        """Hand a ``session_closed`` to whoever asked for that destruction.

        Called for EVERY ``session_closed``, before the tracked/untracked split
        below, because the two are independent: ``close_remote_session`` pops its
        tracked entry when it sends the frame, so its own confirmation always
        arrives on the untracked path — and a session the controlled operator closed
        from their own dashboard arrives on the tracked one with nobody waiting.
        Resolving in one place ahead of both keeps that from mattering.
        """
        for waiter in list(self._close_waiters.get(session_id) or ()):
            if not waiter.done():
                waiter.set_result(reason)

    async def send_user_message(self, session_id: str, text: str) -> None:
        jc = self._require_connection()
        await jc.send(protocol.make_user_message(session_id, text))

    async def send_user_input(
        self, session_id: str, kind: str, payload: Optional[Dict[str, Any]] = None
    ) -> None:
        jc = self._require_connection()
        await jc.send(protocol.make_user_input(session_id, kind, payload))

    async def call_session_rpc(
        self,
        session_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Round-trip a session-scoped question. Raises on failure or timeout."""
        jc = self._require_connection()
        rpc_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        waiter: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._rpc_waiters[rpc_id] = waiter
        await jc.send(protocol.make_session_rpc(session_id, rpc_id, action, payload))
        try:
            frame = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            self._rpc_waiters.pop(rpc_id, None)
            raise RemoteControlError(f"Remote did not respond to {action} within {timeout:.0f} seconds") from None
        if not frame.get("ok"):
            raise RemoteControlError(
                str(frame.get("error") or f"Remote failed to execute {action}")
            )
        result = frame.get("result")
        return result if isinstance(result, dict) else {}

    async def send_confirm_response(        self, session_id: str, request_id: str, value: Any
    ) -> None:
        """Answer a parked confirmation.

        Failure here is logged, not raised: the caller is a UI callback with
        nowhere to surface an exception, and the controlled side keeps the confirmation
        parked and re-offers it on the next attach, so the answer is simply
        asked for again rather than lost.
        """
        if not self.connected or self._jc is None:
            logger.warning(
                "remote_control: dropping confirm response for %s — not connected",
                request_id,
            )
            return
        try:
            await self._jc.send(
                protocol.make_confirm_response(session_id, request_id, value)
            )
        except Exception:
            logger.warning(
                "remote_control: failed to send confirm response %s", request_id,
                exc_info=True,
            )

    def _require_connection(self) -> JsonlConnection:
        if not self.connected or self._jc is None:
            raise RemoteControlError(
                f"Connection to {self.address.display()} was lost — reconnecting, please retry shortly"
            )
        return self._jc

    # ── Read loop / heartbeat / reconnect ────────────────────────────────────

    async def _read_loop(self, jc: JsonlConnection) -> None:
        idle_budget = protocol.HEARTBEAT_TIMEOUT_SEC + protocol.HEARTBEAT_INTERVAL_SEC
        while not self._closing and not jc.closed:
            try:
                frame = await jc.recv(timeout=idle_budget)
            except asyncio.TimeoutError:
                # Watchdog owns liveness, not this loop.
                continue
            except Exception:
                break
            if frame is None:
                break
            self._last_seen = time.monotonic()
            try:
                await self._dispatch(frame)
            except Exception:
                logger.exception(
                    "remote_control: failed to handle %r frame", frame.get("t")
                )
        await self._on_connection_lost(jc)

    async def _watchdog(self, jc: JsonlConnection) -> None:
        while not self._closing and not jc.closed:
            await asyncio.sleep(protocol.HEARTBEAT_INTERVAL_SEC / 2)
            if time.monotonic() - self._last_seen > protocol.HEARTBEAT_TIMEOUT_SEC:
                logger.info(
                    "remote_control: no traffic from %s for >%.0fs; reconnecting",
                    self.address.endpoint, protocol.HEARTBEAT_TIMEOUT_SEC,
                )
                await jc.close()
                return

    async def _on_connection_lost(self, jc: JsonlConnection) -> None:
        if self._jc is not jc:
            return
        self._jc = None
        self._connected.clear()
        # Fail in-flight RPCs now rather than letting each caller sit out its
        # full timeout for an answer that can no longer arrive.
        for rpc_id, waiter in list(self._rpc_waiters.items()):
            self._rpc_waiters.pop(rpc_id, None)
            if not waiter.done():
                waiter.set_result(
                    protocol.make_session_rpc_result(
                        "", rpc_id, False, None, "Connection was interrupted while waiting for a response"
                    )
                )
        # Same for the two handshake-shaped waiters. list_remote_sessions in
        # particular is called from the panel's refresh path, where an 8s hang
        # on a link that is already known to be down is a visible stall.
        # set_exception rather than cancel: the awaiting caller is not itself
        # being cancelled, and surfacing CancelledError to it would read as
        # "the user aborted this" all the way up the stack.
        for waiters in (self._list_waiters, self._open_waiters, self._push_waiters,
                        self._skill_list_waiters):
            for waiter in list(waiters):
                waiters.remove(waiter)
                if not waiter.done():
                    waiter.set_exception(
                        RemoteControlError("Connection was interrupted while waiting for the remote's response")
                    )
        # A destruction awaiting confirmation can no longer get one. Fail it
        # explicitly rather than letting it sit out its timeout: the caller must
        # NOT delete its local record on the strength of an answer that will
        # never arrive.
        for sid, waiters in list(self._close_waiters.items()):
            self._close_waiters.pop(sid, None)
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(
                        RemoteControlError(
                            f"Connection was interrupted while waiting for confirmation that session {sid} was destroyed"
                        )
                    )
        release_waiter, self._release_waiter = self._release_waiter, None
        if release_waiter is not None and not release_waiter.done():
            release_waiter.set_exception(
                RemoteControlError("Connection was interrupted while waiting for release confirmation")
            )
        for tracked in self._sessions.values():
            tracked.attached = False
            try:
                tracked.sink.on_disconnected(f"Connection to {self.address.display()} was interrupted")
            except Exception:
                logger.debug("remote_control: sink.on_disconnected failed", exc_info=True)
        self._emit_state("disconnected", self.address.display())

    async def _supervise(self) -> None:
        """Reconnect forever (until closed), re-attaching every tracked session."""
        attempt = 0
        while not self._closing:
            if self.connected:
                attempt = 0
                await asyncio.sleep(protocol.HEARTBEAT_INTERVAL_SEC)
                continue

            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            await asyncio.sleep(delay)
            if self._closing:
                return
            attempt += 1
            try:
                await self._connect_once()
            except RemoteControlError as exc:
                logger.info(
                    "remote_control: reconnect to %s failed (attempt %d): %s",
                    self.address.endpoint, attempt, exc,
                )
                continue

            # Re-attach every session we were driving, from where we left off.
            for tracked in list(self._sessions.values()):
                try:
                    assert self._jc is not None
                    await self._jc.send(
                        protocol.make_attach_session(
                            tracked.session_id, tracked.capability, tracked.since_seq
                        )
                    )
                except Exception:
                    logger.warning(
                        "remote_control: re-attach of %s failed", tracked.session_id,
                        exc_info=True,
                    )

    # ── Frame dispatch ───────────────────────────────────────────────────────

    async def _dispatch(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("t")

        if kind == protocol.PING:
            if self._jc is not None:
                await self._jc.send(protocol.make_pong(frame.get("ts") or time.time()))
            return
        if kind == protocol.PONG:
            return

        if kind == protocol.SESSION_OPENED:
            for waiter in list(self._open_waiters):
                if not waiter.done():
                    self._open_waiters.remove(waiter)
                    waiter.set_result(frame)
                    return
            logger.warning("remote_control: session_opened with nobody waiting")
            return

        if kind == protocol.SESSIONS_LIST:
            for waiter in list(self._list_waiters):
                if not waiter.done():
                    self._list_waiters.remove(waiter)
                    waiter.set_result(frame)
                    return
            # Nobody waiting: keep the payload anyway. It is strictly fresher
            # than what auth_ok left here, and a caller reading
            # ``remote_sessions`` directly should see the newest answer.
            sessions = frame.get("sessions")
            if isinstance(sessions, list):
                self.remote_sessions = sessions
            return

        if kind == protocol.SESSION_RPC_RESULT:
            waiter = self._rpc_waiters.pop(str(frame.get("rpc_id") or ""), None)
            if waiter is not None and not waiter.done():
                waiter.set_result(frame)
            return

        if kind == protocol.SKILL_PUSH_RESULT:
            for waiter in list(self._push_waiters):
                if not waiter.done():
                    self._push_waiters.remove(waiter)
                    waiter.set_result(frame)
                    return
            logger.warning("remote_control: skill_push_result with nobody waiting")
            return

        if kind == protocol.SKILL_LIST_RESULT:
            for waiter in list(self._skill_list_waiters):
                if not waiter.done():
                    self._skill_list_waiters.remove(waiter)
                    waiter.set_result(frame)
                    return
            logger.warning("remote_control: skill_list_result with nobody waiting")
            return

        if kind == protocol.ERROR:
            logger.warning(
                "remote_control: server error %s: %s",
                frame.get("reason"), frame.get("detail"),
            )
            reason = str(frame.get("reason") or "")
            # ``disconnect_client`` answers a release with this frame, written
            # directly rather than queued, which makes it the release's one
            # reliably-ordered acknowledgement. ``server_shutdown`` counts too:
            # the sessions are just as gone.
            waiter = self._release_waiter
            if waiter is not None and not waiter.done() and reason in (
                protocol.REASON_CLOSED_BY_CONTROLLER,
                protocol.REASON_SERVER_SHUTDOWN,
            ):
                waiter.set_result(reason)
            self._emit_state("failed", str(frame.get("detail") or frame.get("reason") or ""))
            return

        session_id = str(frame.get("session_id") or "")
        if kind == protocol.SESSION_CLOSED:
            self._resolve_close_waiters(
                session_id, str(frame.get("reason") or "")
            )
        tracked = self._sessions.get(session_id)
        if tracked is None:
            if kind == protocol.SESSION_CLOSED:
                # A session we hold no live sink for just died on the server —
                # its tab was closed earlier, so all that is left locally is a
                # persisted re-adopt record. Tell the hub so that record goes
                # too; dropping this frame (which is what used to happen) left
                # a chip in the panel pointing at a session that no longer
                # exists, discoverable only by clicking it and failing.
                if self.on_orphan_session_closed is not None:
                    try:
                        self.on_orphan_session_closed(
                            session_id, str(frame.get("reason") or "")
                        )
                    except Exception:
                        logger.debug(
                            "remote_control: on_orphan_session_closed failed",
                            exc_info=True,
                        )
            else:
                logger.debug(
                    "remote_control: %r frame for untracked session %s", kind, session_id
                )
            return
        sink = tracked.sink

        if kind == protocol.AGENT_EVENT:
            seq = int(frame.get("seq") or 0)
            method = str(frame.get("method") or "")
            args = frame.get("args")
            args = args if isinstance(args, list) else []
            # Drop anything at or below what we've already rendered. Live frames
            # always carry a fresh seq, so this only fires when a replay overlaps
            # what we have — e.g. two attach_session frames racing after a
            # supersede-then-reattach.
            if seq and seq <= tracked.since_seq:
                return
            if seq:
                tracked.since_seq = seq
            sink.on_agent_event(method, args, seq)
            return

        if kind == protocol.CONFIRM_REQUEST:
            sink.on_confirm_request(
                str(frame.get("request_id") or ""),
                str(frame.get("method") or ""),
                frame.get("args") if isinstance(frame.get("args"), list) else [],
                frame.get("kwargs") if isinstance(frame.get("kwargs"), dict) else {},
            )
            return

        if kind == protocol.CONFIRM_CANCEL:
            sink.on_confirm_cancel(str(frame.get("request_id") or ""))
            return

        if kind == protocol.SESSION_ATTACHED:
            tracked.attached = True
            cur_seq = int(frame.get("cur_seq") or 0)
            tracked.since_seq = max(tracked.since_seq, cur_seq)
            snapshot = frame.get("snapshot")
            sink.on_session_attached(
                cur_seq,
                bool(frame.get("gap")),
                snapshot if isinstance(snapshot, dict) else {},
            )
            sink.on_reconnected()
            return

        if kind == protocol.SESSION_SUPERSEDED:
            tracked.attached = False
            sink.on_session_superseded(int(frame.get("generation") or 0))
            return

        if kind == protocol.SESSION_CLOSED:
            self._sessions.pop(session_id, None)
            sink.on_session_closed(str(frame.get("reason") or ""))
            return

        logger.warning("remote_control: unhandled frame %r from server", kind)


def _explain_auth_failure(reason: str, frame: Dict[str, Any]) -> str:
    """Turn a reason code into something an operator can act on."""
    detail = str(frame.get("detail") or "")
    if reason == protocol.REASON_INVALID_TOKEN:
        return "Pairing token was rejected — please re-copy the control address on the controlled machine"
    if reason == protocol.REASON_VERSION_MISMATCH:
        return f"Protocol version mismatch — both machines' HandQ versions need to match ({detail})"
    if reason == protocol.REASON_AUTH_TIMEOUT:
        return "Target did not receive the authentication frame before timing out"
    return f"Authentication failed: {reason or 'unknown'}{(' — ' + detail) if detail else ''}"
