"""ChatRoomService — the LAN chat-room orchestrator.

Topology: **hub-and-spoke**. The room *owner* runs a TCP relay server (the
hub) and a UDP discovery beacon; every other node connects to it as a *member*
(spoke). All chat flows through the owner, which is what gives the room a
single authoritative roster and a single total ordering (the ``seq`` counter).

One process runs exactly one role at a time:

    svc = ChatRoomService(identity=NodeIdentity.local(display_name="win-pc-1"),
                          on_message=..., on_roster=..., on_presence=...)
    info = await svc.host(room="dev")             # become the owner
      -- or --
    info = await svc.join(host="192.168.1.5", port=48611)   # become a member
    ...
    await svc.send("hello everyone")              # broadcast chat
    await svc.send("@win-pc-2 restart the build", intent=MessageIntent.TASK)
    ...
    await svc.shutdown()

**What this service enforces on the wire**: only R2 (pair cooldown) and R3
(room-wide handq quota). Both trip only on ``sender_kind == HANDQ``; human
messages flow freely. Every other decision — including "should my agent act
on this?" — belongs to the orchestrator downstream of ``on_message``. Chat
delivery is a dumb pipe by design; the orchestrator already knows how to
tell chat from task.

Callbacks fire on the asyncio loop. ``on_message`` receives an
:class:`IncomingMessage` whose classification tells the caller the address
relationship (mine? broadcast? from me?). Callbacks may be plain functions
or coroutine functions (the latter are scheduled as tasks so a slow handler
can't stall the relay).
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from . import discovery, protocol
from ._constants import (
    BROADCAST_SEND_TIMEOUT_SEC,
    BUDGET_MAX_HANDQ_MSGS,
    BUDGET_WINDOW_SEC,
    DEFAULT_TCP_PORT,
    ECHO_COOLDOWN_SEC,
    ECHO_MAX_PAIR_MSGS,
    ECHO_WINDOW_SEC,
    HEARTBEAT_INTERVAL_SEC,
    HELLO_TIMEOUT_SEC,
    MAX_BODY_BYTES,
    PROTOCOL_VERSION,
    RECONNECT_BASE_SEC,
    RECONNECT_MAX_ATTEMPTS,
    RECONNECT_MAX_SEC,
    TRANSCRIPT_MAX_LEN,
)
from .models import (
    ChatMessage,
    IncomingMessage,
    MessageIntent,
    NodeIdentity,
    NodeRole,
    Participant,
    RoomInfo,
    SenderKind,
)
from .router import classify, parse_mentions
from .transport import JsonlConnection, connect_to_hub

_logger = logging.getLogger("handq.chatroom")

MessageCallback = Callable[[IncomingMessage], Union[None, Awaitable[None]]]
RosterCallback = Callable[[List[Participant]], Union[None, Awaitable[None]]]
PresenceCallback = Callable[[str, NodeIdentity], Union[None, Awaitable[None]]]
StateCallback = Callable[[str, Dict[str, Any]], Union[None, Awaitable[None]]]
ErrorCallback = Callable[[Exception], Union[None, Awaitable[None]]]


class ChatRoomError(Exception):
    """Raised on join/host failures the caller should see."""


class _MemberConn:
    """Owner-side bookkeeping for one connected member."""

    __slots__ = ("conn", "node")

    def __init__(self, conn: JsonlConnection, node: NodeIdentity) -> None:
        self.conn = conn
        self.node = node


class ChatRoomService:
    def __init__(
        self,
        *,
        identity: NodeIdentity,
        on_message: Optional[MessageCallback] = None,
        on_roster: Optional[RosterCallback] = None,
        on_presence: Optional[PresenceCallback] = None,
        on_state: Optional[StateCallback] = None,
        on_error: Optional[ErrorCallback] = None,
    ) -> None:
        self.identity = identity
        self._on_message = on_message
        self._on_roster = on_roster
        self._on_presence = on_presence
        self._on_state = on_state
        self._on_error = on_error

        self.room: str = ""
        self._role: Optional[NodeRole] = None
        self._room_secret: str = ""

        # Shared transcript + dedup (every node keeps its own copy, all in the
        # same seq order because the owner assigns seq).
        self._transcript: List[ChatMessage] = []
        self._seen_ids: set = set()
        self._roster: Dict[str, Participant] = {}
        self._lock = asyncio.Lock()

        # Owner (hub) state.
        self._server: Optional[asyncio.AbstractServer] = None
        self._members: Dict[str, _MemberConn] = {}
        self._seq: int = 0
        self._announce_task: Optional[asyncio.Task] = None
        self._announce_stop: Optional[asyncio.Event] = None
        self._bound_host: str = ""
        self._bound_port: int = 0

        # Guardrails (owner-enforced, sender_kind=HANDQ only). Track rolling
        # per-node handq activity + which pairs are currently cooling. Human
        # messages are neither logged nor rate-limited.
        self._handq_msg_log: List[Tuple[str, float]] = []  # (node_id, ts)
        self._pair_cooldown_until: Dict[Tuple[str, str], float] = {}

        # Member (spoke) state.
        self._hub_conn: Optional[JsonlConnection] = None
        self._owner_identity: Optional[NodeIdentity] = None
        self._client_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._join_params: Optional[Dict[str, Any]] = None
        self._reconnect_attempt: int = 0
        self._reconnect_task: Optional[asyncio.Task] = None

        self._closed = False

    # ── Introspection ─────────────────────────────────────────────────────

    @property
    def is_owner(self) -> bool:
        return self._role is NodeRole.OWNER

    @property
    def role(self) -> Optional[NodeRole]:
        return self._role

    def roster(self) -> List[Participant]:
        return list(self._roster.values())

    def transcript(self) -> List[ChatMessage]:
        return list(self._transcript)

    # ── Owner: host a room ────────────────────────────────────────────────

    async def host(
        self,
        *,
        room: str,
        bind_host: str = "0.0.0.0",
        tcp_port: int = DEFAULT_TCP_PORT,
        announce: bool = True,
        room_secret: str = "",
    ) -> RoomInfo:
        """Become the room owner: start the relay server (+ discovery beacon).

        ``tcp_port=0`` lets the OS pick a free port (used by tests). The
        returned :class:`RoomInfo` carries the actual bound port + LAN IP so a
        caller can print "others join at <ip>:<port>".
        """
        if self._role is not None:
            raise ChatRoomError(f"already active as {self._role.value}")
        self.room = room
        self._room_secret = room_secret
        self.identity.is_owner = True
        self._role = NodeRole.OWNER

        try:
            self._server = await asyncio.start_server(
                self._serve_member, bind_host, tcp_port
            )
        except OSError as exc:
            self._role = None
            raise ChatRoomError(f"cannot bind {bind_host}:{tcp_port}: {exc}") from exc

        sockname = self._server.sockets[0].getsockname()
        self._bound_port = int(sockname[1])
        lan_ip = discovery.get_local_ip()
        self._bound_host = lan_ip

        # The owner is participant #1 in its own roster.
        self._roster[self.identity.node_id] = Participant(
            node=self.identity, address="local", joined_at=time.time()
        )

        if announce:
            self._announce_stop = asyncio.Event()
            self._announce_task = asyncio.create_task(
                discovery.announce(
                    room=room,
                    tcp_port=self._bound_port,
                    owner_display=self.identity.display_name,
                    node_id=self.identity.node_id,
                    stop_event=self._announce_stop,
                    host=lan_ip,
                ),
                name="chatroom-announce",
            )

        _logger.info(
            "hosting room=%r on %s:%d (announce=%s)",
            room, lan_ip, self._bound_port, announce,
        )
        await self._fire_state("hosting", {
            "room": room, "host": lan_ip, "port": self._bound_port,
        })
        return RoomInfo(
            room=room, host=lan_ip, port=self._bound_port,
            is_owner=True, node_count=len(self._roster),
        )

    async def _serve_member(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Per-connection handler on the owner: handshake, then relay loop."""
        conn = JsonlConnection(reader, writer)
        node: Optional[NodeIdentity] = None
        try:
            try:
                hello = await conn.recv(timeout=HELLO_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                await self._safe_send(conn, protocol.make_error("timeout", "no hello"))
                return
            if not hello or hello.get("t") != protocol.HELLO:
                await self._safe_send(conn, protocol.make_error("bad_hello"))
                return
            if hello.get("protocol_version") != PROTOCOL_VERSION:
                await self._safe_send(
                    conn,
                    protocol.make_error(
                        "protocol_mismatch",
                        f"owner speaks v{PROTOCOL_VERSION}",
                    ),
                )
                return

            node = NodeIdentity.from_dict(hello.get("node") or {})
            node.is_owner = False  # only the host is the owner, never a joiner

            # Fix #2: Authenticate the joining member if a room secret is set.
            if self._room_secret:
                expected = protocol.compute_auth_token(
                    self.room, node.node_id, self._room_secret
                )
                got = hello.get("auth") or ""
                if not hmac.compare_digest(expected, got):
                    await self._safe_send(
                        conn, protocol.make_error("auth_failed", "bad secret")
                    )
                    return

            # Reject duplicate display names — in the work-group model a single
            # @mention must resolve to exactly one node; collisions would make a
            # TASK actionable on multiple agents simultaneously.
            taken = self._display_name_taken(node.display_name, node.node_id)
            if taken:
                await self._safe_send(
                    conn,
                    protocol.make_error(
                        "name_taken",
                        f"display name {node.display_name!r} already in use",
                    ),
                )
                return

            address = self._peer_str(conn)
            self._members[node.node_id] = _MemberConn(conn, node)
            self._roster[node.node_id] = Participant(
                node=node, address=address, joined_at=time.time()
            )
            _logger.info("member joined: %s (%s) from %s",
                         node.display_name, node.node_id[:8], address)

            async with self._lock:
                cur_seq = self._seq
            await conn.send(protocol.make_welcome(
                room=self.room,
                you=node.to_dict(),
                owner=self.identity.to_dict(),
                roster=self._roster_dicts(),
                seq=cur_seq,
            ))

            # Replay missed messages if the member is reconnecting (last_seq > 0).
            member_last_seq = hello.get("last_seq")
            if member_last_seq is not None:
                await self._replay_missed(conn, int(member_last_seq))

            await self._broadcast_roster()
            await self._broadcast_presence("join", node)

            async for frame in conn.messages():
                await self._handle_member_frame(node, conn, frame)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            _logger.exception("member handler crashed")
        finally:
            if node is not None:
                self._members.pop(node.node_id, None)
                self._roster.pop(node.node_id, None)
                _logger.info("member left: %s (%s)",
                             node.display_name, node.node_id[:8])
                await self._safe(self._broadcast_roster())
                await self._safe(self._broadcast_presence("leave", node))
            await conn.close()

    def _display_name_taken(self, name: str, node_id: str) -> bool:
        """True if *name* is already used by a different online node."""
        norm = name.strip().lower()
        if self.identity.display_name.strip().lower() == norm:
            return self.identity.node_id != node_id
        for nid, participant in self._roster.items():
            if nid == node_id:
                continue
            if participant.node.display_name.strip().lower() == norm:
                return True
        return False

    async def _handle_member_frame(
        self, node: NodeIdentity, conn: JsonlConnection, frame: Dict[str, Any]
    ) -> None:
        t = frame.get("t")
        if t == protocol.CHAT:
            msg = ChatMessage.from_dict(frame.get("msg") or {})
            # Owner authoritatively stamps identity — members can't spoof
            # another node's name or claim owner status.
            msg.sender_node_id = node.node_id
            msg.sender_display = node.display_name
            msg.sender_is_owner = False
            msg.room = self.room
            # sender_kind: members may claim USER or HANDQ. SYSTEM is owner-
            # only (used for guardrail notices) — downgrade any member
            # attempt so the audit trail is honest.
            if msg.sender_kind is SenderKind.SYSTEM:
                _logger.warning(
                    "member %s tried to send sender_kind=SYSTEM; downgrading to HANDQ",
                    node.display_name,
                )
                msg.sender_kind = SenderKind.HANDQ
            if len(msg.body.encode("utf-8", "replace")) > MAX_BODY_BYTES:
                await self._safe_send(conn, protocol.make_error("too_large"))
                return
            await self._commit_and_relay(msg)
        elif t == protocol.PING:
            await self._safe_send(conn, protocol.make_pong(time.time()))
        elif t == protocol.BYE:
            raise ConnectionError("member said bye")

    # ── Member: join a room ───────────────────────────────────────────────

    async def join(
        self,
        *,
        room: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        discover_timeout: float = 3.0,
        room_secret: str = "",
    ) -> RoomInfo:
        """Connect to an existing room as a member.

        Provide an explicit ``host``/``port`` (reliable), or omit them to
        discover a room over UDP (best-effort; ``room`` optionally filters
        which discovered room to pick).
        """
        if self._role is not None:
            raise ChatRoomError(f"already active as {self._role.value}")

        if not host:
            host, port, room = await self._discover_target(room, discover_timeout)

        assert host is not None and port is not None
        try:
            conn = await connect_to_hub(host, port)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ChatRoomError(f"cannot connect to {host}:{port}: {exc}") from exc

        self._hub_conn = conn
        self._room_secret = room_secret

        # Compute auth token if a shared secret is provided.
        auth_token: Optional[str] = None
        if room_secret:
            auth_token = protocol.compute_auth_token(
                room or "", self.identity.node_id, room_secret
            )
        # Pass last_seq on reconnect so the owner can replay missed messages.
        last_seq = self._seq if self._seq > 0 else None
        await conn.send(protocol.make_hello(
            self.identity.to_dict(), auth=auth_token, last_seq=last_seq,
        ))
        try:
            welcome = await conn.recv(timeout=HELLO_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await conn.close()
            raise ChatRoomError("no welcome from owner (timeout)")
        if not welcome or welcome.get("t") != protocol.WELCOME:
            await conn.close()
            code = (welcome or {}).get("code") if isinstance(welcome, dict) else None
            raise ChatRoomError(f"join rejected by owner: {code or 'no welcome'}")

        self.room = str(welcome.get("room") or room or "")
        self._owner_identity = NodeIdentity.from_dict(welcome.get("owner") or {})
        self._seq = int(welcome.get("seq") or 0)
        self._load_roster(welcome.get("roster") or [])
        self._role = NodeRole.MEMBER
        self._reconnect_attempt = 0

        # Store join params for auto-reconnect.
        self._join_params = {
            "host": host, "port": port, "room": room, "room_secret": room_secret,
        }

        self._client_task = asyncio.create_task(
            self._client_loop(), name="chatroom-client")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="chatroom-heartbeat")

        _logger.info("joined room=%r via %s:%d as %s",
                     self.room, host, port, self.identity.display_name)
        await self._fire_state("joined", {"room": self.room, "host": host, "port": port})
        await self._fire_roster()
        return RoomInfo(
            room=self.room, host=host, port=port,
            is_owner=False, node_count=len(self._roster),
        )

    async def _discover_target(
        self, room: Optional[str], timeout: float
    ) -> tuple:
        beacons = await discovery.discover(timeout=timeout)
        if not beacons:
            raise ChatRoomError("no rooms discovered on the LAN")
        if room:
            for b in beacons:
                if b.room == room:
                    return b.host, b.tcp_port, b.room
            raise ChatRoomError(f"room {room!r} not found (found: "
                                f"{[b.room for b in beacons]})")
        b = beacons[0]
        return b.host, b.tcp_port, b.room

    async def _client_loop(self) -> None:
        assert self._hub_conn is not None
        try:
            async for frame in self._hub_conn.messages():
                t = frame.get("t")
                if t == protocol.CHAT:
                    self._commit(ChatMessage.from_dict(frame.get("msg") or {}))
                elif t == protocol.ROSTER:
                    self._load_roster(frame.get("roster") or [])
                    self._seq = int(frame.get("seq") or self._seq)
                    await self._fire_roster()
                elif t == protocol.PRESENCE:
                    node = NodeIdentity.from_dict(frame.get("node") or {})
                    await self._fire_presence(str(frame.get("event") or ""), node)
                elif t == protocol.PING:
                    await self._safe_send(self._hub_conn, protocol.make_pong(time.time()))
                elif t == protocol.ERROR:
                    _logger.warning("owner error: %s %s",
                                    frame.get("code"), frame.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("client loop crashed")
        finally:
            if not self._closed:
                await self._fire_state("disconnected", {"room": self.room})
                # Fix #4: Auto-reconnect with exponential backoff.
                if self._join_params is not None:
                    self._role = None
                    self._reconnect_task = asyncio.create_task(
                        self._reconnect_loop(), name="chatroom-reconnect"
                    )

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                if self._hub_conn is None or self._hub_conn.closed:
                    return
                try:
                    await self._hub_conn.send(protocol.make_ping(time.time()))
                except (ConnectionError, OSError):
                    return
        except asyncio.CancelledError:
            raise

    async def _reconnect_loop(self) -> None:
        """Exponential-backoff reconnect for members after disconnect."""
        while (
            not self._closed
            and self._reconnect_attempt < RECONNECT_MAX_ATTEMPTS
            and self._join_params is not None
        ):
            delay = min(
                RECONNECT_BASE_SEC * (2 ** self._reconnect_attempt),
                RECONNECT_MAX_SEC,
            )
            _logger.info(
                "reconnect attempt %d in %.1fs",
                self._reconnect_attempt + 1, delay,
            )
            await asyncio.sleep(delay)
            if self._closed:
                return
            self._reconnect_attempt += 1
            try:
                await self.join(**self._join_params)
                return
            except (ChatRoomError, OSError, asyncio.TimeoutError) as exc:
                _logger.warning(
                    "reconnect attempt %d failed: %s", self._reconnect_attempt, exc
                )
        if self._reconnect_attempt >= RECONNECT_MAX_ATTEMPTS:
            _logger.error(
                "reconnect exhausted after %d attempts", RECONNECT_MAX_ATTEMPTS
            )
            await self._fire_state("reconnect_failed", {"room": self.room})

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        body: str,
        *,
        sender_kind: SenderKind = SenderKind.USER,
        intent: MessageIntent = MessageIntent.CHAT,
        mentions: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        hop: int = 0,
    ) -> ChatMessage:
        """Publish a message to the room.

        If ``mentions`` is omitted it's parsed from ``@tokens`` in ``body``.
        ``intent`` defaults to CHAT; pass ``MessageIntent.TASK`` to make a
        directed message actionable on the target's agent. An agent replying to
        a task should use ``sender_kind=HANDQ`` and ``intent=RESULT`` so its
        reply can never re-trigger another agent.
        """
        if self._role is None:
            raise ChatRoomError("not in a room; call host() or join() first")
        if mentions is None:
            mentions = parse_mentions(body)
        msg = ChatMessage.new(
            room=self.room,
            sender=self.identity,
            sender_kind=sender_kind,
            intent=intent,
            body=body,
            mentions=mentions,
            hop=hop,
            reply_to=reply_to,
        )
        if self._role is NodeRole.OWNER:
            await self._commit_and_relay(msg)
        else:
            assert self._hub_conn is not None
            await self._hub_conn.send(protocol.make_chat(msg.to_dict()))
            # Member does NOT commit locally; it commits when the owner relays
            # the message back with an assigned seq, keeping every node's
            # transcript identically ordered.
        return msg

    # ── Commit + relay (owner assigns seq) ────────────────────────────────

    async def _commit_and_relay(self, msg: ChatMessage) -> None:
        """Owner-only: assign the authoritative seq, commit, fan out to all.

        Handq-authored messages are subject to R2 (pair cooldown) and R3
        (room-wide handq quota) guardrails. Blocked handq messages are
        replaced by a SYSTEM notice so the room — and any human watching —
        sees the block happen. Human messages bypass all guardrails.
        """
        block_reason = self._check_guardrails(msg)
        if block_reason is not None:
            _logger.info(
                "guardrail dropped handq msg from %s: %s",
                msg.sender_display, block_reason,
            )
            await self._emit_system(
                f"[chatroom] dropped handq msg from {msg.sender_display}: {block_reason}"
            )
            return

        now = time.time()
        async with self._lock:
            self._seq += 1
            msg.seq = self._seq
        fresh_pair = self._record_handq_msg(msg, now)
        self._commit(msg)
        await self._broadcast(protocol.make_chat(msg.to_dict()))

        if fresh_pair is not None:
            await self._emit_system(
                f"[chatroom] pair cooldown activated "
                f"({fresh_pair[0][:8]}<->{fresh_pair[1][:8]}) "
                f"for {ECHO_COOLDOWN_SEC:.0f}s"
            )

    async def _emit_system(self, body: str) -> None:
        """Owner-side: assign a seq to a SYSTEM notice and fan it out.

        Recursion-safe: SYSTEM messages bypass ``_check_guardrails`` so
        emitting a "you were blocked" notice can never itself be blocked.
        """
        notice = ChatMessage.new(
            room=self.room,
            sender=self.identity,
            sender_kind=SenderKind.SYSTEM,
            intent=MessageIntent.SYSTEM,
            body=body,
        )
        async with self._lock:
            self._seq += 1
            notice.seq = self._seq
        self._commit(notice)
        await self._broadcast(protocol.make_chat(notice.to_dict()))

    # ── Guardrails: R2 pair cooldown + R3 room budget ─────────────────────

    @staticmethod
    def _pair_key(a: str, b: str) -> Tuple[str, str]:
        return (a, b) if a < b else (b, a)

    def _prune_guardrail_state(self, now: float) -> None:
        """Drop expired entries from handq log and pair cooldowns."""
        cutoff = now - max(ECHO_WINDOW_SEC, BUDGET_WINDOW_SEC)
        self._handq_msg_log = [
            (nid, ts) for nid, ts in self._handq_msg_log if ts >= cutoff
        ]
        self._pair_cooldown_until = {
            p: t for p, t in self._pair_cooldown_until.items() if t > now
        }

    def _check_guardrails(self, msg: ChatMessage) -> Optional[str]:
        """Return None to allow *msg*, or a human-readable reason to block.

        Only handq messages are subject to guardrails; USER and SYSTEM are
        always allowed through. Called by ``_commit_and_relay`` on the owner
        before assigning a seq — a blocked message never becomes part of the
        room's transcript.
        """
        if msg.sender_kind is not SenderKind.HANDQ:
            return None
        now = time.time()
        self._prune_guardrail_state(now)
        # R2 · Is the sender currently in a pair cooldown?
        for pair, until in self._pair_cooldown_until.items():
            if msg.sender_node_id in pair:
                other = pair[1] if pair[0] == msg.sender_node_id else pair[0]
                return (
                    f"pair cooldown with {other[:8]} "
                    f"({until - now:.0f}s left)"
                )
        # R3 · Room-wide handq quota.
        window_start = now - BUDGET_WINDOW_SEC
        recent_handq = sum(1 for _, ts in self._handq_msg_log if ts >= window_start)
        if recent_handq >= BUDGET_MAX_HANDQ_MSGS:
            return (
                f"room budget exceeded "
                f"({recent_handq}/{BUDGET_MAX_HANDQ_MSGS} handq msgs "
                f"in {BUDGET_WINDOW_SEC:.0f}s)"
            )
        return None

    def _record_handq_msg(
        self, msg: ChatMessage, now: float
    ) -> Optional[Tuple[str, str]]:
        """Record *msg* in the handq log; return a pair key if this message
        just tripped a fresh R2 cooldown, else None.

        A cooldown trips when two agents' *combined* handq-msg count within
        ECHO_WINDOW_SEC reaches ECHO_MAX_PAIR_MSGS — this catches both pure
        A<->B ping-pong AND many-to-one chatter (3+1, 4+0-with-someone-else).
        """
        if msg.sender_kind is not SenderKind.HANDQ:
            return None
        self._handq_msg_log.append((msg.sender_node_id, now))
        window_start = now - ECHO_WINDOW_SEC
        per_node: Dict[str, int] = {}
        for nid, ts in self._handq_msg_log:
            if ts >= window_start:
                per_node[nid] = per_node.get(nid, 0) + 1
        my_count = per_node.get(msg.sender_node_id, 0)
        for other_nid, other_count in per_node.items():
            if other_nid == msg.sender_node_id:
                continue
            if my_count + other_count >= ECHO_MAX_PAIR_MSGS:
                pair = self._pair_key(msg.sender_node_id, other_nid)
                if self._pair_cooldown_until.get(pair, 0.0) <= now:
                    self._pair_cooldown_until[pair] = now + ECHO_COOLDOWN_SEC
                    return pair
        return None

    def reset_guardrails(self) -> None:
        """Human-triggered: clear all active pair cooldowns and budget state.

        Wired to a user command (e.g. ``/reset_budget``) at the orchestrator
        layer. Only the owner needs to call this; members' local state is
        purely a mirror of the owner's authoritative counters.
        """
        self._handq_msg_log.clear()
        self._pair_cooldown_until.clear()
        _logger.info("chatroom guardrails reset by user request")

    def _commit(self, msg: ChatMessage) -> None:
        """Add to local transcript (dedup) and fire on_message with a verdict."""
        if msg.msg_id in self._seen_ids:
            return
        self._seen_ids.add(msg.msg_id)
        self._transcript.append(msg)
        # Track highest seq seen (used by member to request replay on reconnect).
        if msg.seq is not None and msg.seq > self._seq:
            self._seq = msg.seq
        if len(self._transcript) > TRANSCRIPT_MAX_LEN:
            self._transcript = self._transcript[-TRANSCRIPT_MAX_LEN:]
            self._seen_ids = {m.msg_id for m in self._transcript}
        verdict = classify(msg, self.identity)
        if self._on_message is not None:
            self._invoke(self._on_message, IncomingMessage(message=msg, classification=verdict))

    # ── Fan-out helpers (owner) ───────────────────────────────────────────

    async def _broadcast(self, frame: Dict[str, Any]) -> None:
        """Send *frame* to every connected member concurrently; prune dead conns.

        Each send has an independent ``BROADCAST_SEND_TIMEOUT_SEC`` deadline
        so a single slow member can't stall the relay. Dead connections are
        removed from local state here; the per-member reader loop
        (``_serve_member``) will follow up with a roster/presence broadcast
        when it notices the socket has gone away.
        """
        members = list(self._members.items())
        if not members:
            return

        async def _send_one(nid: str, member: _MemberConn) -> Optional[str]:
            try:
                await asyncio.wait_for(
                    member.conn.send(frame), BROADCAST_SEND_TIMEOUT_SEC
                )
                return None
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return nid

        results = await asyncio.gather(
            *(_send_one(nid, m) for nid, m in members)
        )
        for nid in results:
            if not nid:
                continue
            member = self._members.pop(nid, None)
            self._roster.pop(nid, None)
            if member is not None:
                await member.conn.close()

    async def _broadcast_roster(self) -> None:
        frame = protocol.make_roster(self._roster_dicts(), seq=self._seq)
        await self._broadcast(frame)
        await self._fire_roster()

    async def _broadcast_presence(self, event: str, node: NodeIdentity) -> None:
        await self._broadcast(protocol.make_presence(event, node.to_dict()))
        await self._fire_presence(event, node)

    async def _replay_missed(self, conn: JsonlConnection, last_seq: int) -> None:
        """Send transcript entries with seq > last_seq to a reconnecting member."""
        for msg in self._transcript:
            if msg.seq is not None and msg.seq > last_seq:
                try:
                    await conn.send(protocol.make_chat(msg.to_dict()))
                except (ConnectionError, OSError):
                    return

    # ── Roster bookkeeping ────────────────────────────────────────────────

    def _roster_dicts(self) -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self._roster.values()]

    def _load_roster(self, entries: List[Dict[str, Any]]) -> None:
        self._roster = {}
        for e in entries:
            p = Participant.from_dict(e)
            self._roster[p.node.node_id] = p

    # ── Callback plumbing (sync or async) ─────────────────────────────────

    def _invoke(self, cb: Callable, *args: Any) -> None:
        try:
            result = cb(*args)
        except Exception as exc:
            _logger.exception("chatroom callback raised")
            self._notify_error(exc)
            return
        if inspect.isawaitable(result):
            asyncio.create_task(self._await_cb(result))

    async def _await_cb(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception as exc:
            _logger.exception("chatroom async callback raised")
            self._notify_error(exc)

    def _notify_error(self, exc: Exception) -> None:
        """Fire on_error if set; never raises."""
        if self._on_error is None:
            return
        try:
            result = self._on_error(exc)
            if inspect.isawaitable(result):
                asyncio.create_task(self._safe_await_error(result))
        except Exception:
            _logger.debug("on_error callback raised", exc_info=True)

    async def _safe_await_error(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception:
            _logger.debug("async on_error callback raised", exc_info=True)

    async def _fire_state(self, state: str, info: Dict[str, Any]) -> None:
        if self._on_state is not None:
            self._invoke(self._on_state, state, info)

    async def _fire_roster(self) -> None:
        if self._on_roster is not None:
            self._invoke(self._on_roster, list(self._roster.values()))

    async def _fire_presence(self, event: str, node: NodeIdentity) -> None:
        if self._on_presence is not None:
            self._invoke(self._on_presence, event, node)

    # ── Teardown ──────────────────────────────────────────────────────────

    async def leave(self) -> None:
        """Leave the room (member) or stop hosting (owner). Idempotent."""
        role = self._role
        if role is None:
            return

        if role is NodeRole.MEMBER:
            if self._hub_conn is not None:
                await self._safe_send(
                    self._hub_conn, protocol.make_bye(self.identity.node_id))
            await self._cancel(self._reconnect_task)
            await self._cancel(self._heartbeat_task)
            await self._cancel(self._client_task)
            if self._hub_conn is not None:
                await self._hub_conn.close()
            self._hub_conn = None
            self._join_params = None
        else:  # OWNER
            if self._announce_stop is not None:
                self._announce_stop.set()
            await self._cancel(self._announce_task)
            for member in list(self._members.values()):
                await member.conn.close()
            self._members.clear()
            if self._server is not None:
                self._server.close()
                try:
                    await self._server.wait_closed()
                except Exception:
                    pass
                self._server = None

        self._role = None
        self._roster.clear()
        _logger.info("left room=%r (was %s)", self.room, role.value)

    async def shutdown(self) -> None:
        """Full teardown for process exit. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        await self.leave()

    # ── Small utilities ───────────────────────────────────────────────────

    async def _cancel(self, task: Optional[asyncio.Task]) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _safe(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception:
            _logger.debug("suppressed teardown error", exc_info=True)

    async def _safe_send(self, conn: JsonlConnection, frame: Dict[str, Any]) -> None:
        try:
            await conn.send(frame)
        except (ConnectionError, OSError):
            pass

    def _peer_str(self, conn: JsonlConnection) -> str:
        peer = conn.peername
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return str(peer or "?")
