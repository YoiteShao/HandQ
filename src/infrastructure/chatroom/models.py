"""Data models for the LAN chat room.

Three concepts, kept deliberately small and JSON-round-trippable so they can
travel over the wire and (later) be handed to the bridge's IPC layer verbatim:

* :class:`NodeIdentity` — *who* a HandQ instance is on the LAN. One node ==
  one HandQ process == one machine. A node carries BOTH a human user and a
  HandQ agent; the per-message :class:`SenderKind` says which of the two
  authored a given line.
* :class:`ChatMessage` — a single line in the shared transcript.
* :class:`Classification` / :class:`IncomingMessage` — the *receiver-relative*
  verdict computed by :mod:`.router` (is this mine? a broadcast? actionable?).

Everything is a plain dataclass with ``to_dict`` / ``from_dict`` (matching the
``scheduler`` / ``long_term_memory`` convention) rather than pydantic, to keep
the import graph and boot cost minimal.
"""
from __future__ import annotations

import enum
import os
import platform
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SenderKind(str, enum.Enum):
    """Who authored a message *within* a node.

    A node hosts both a human and an agent; every receiver must be able to tell
    them apart (requirement: "distinguish user vs HandQ"). ``SYSTEM`` is for
    presence/roster notices the service itself emits.
    """

    USER = "user"
    HANDQ = "handq"
    SYSTEM = "system"


class MessageIntent(str, enum.Enum):
    """What a message is *for*.

    Only ``TASK`` is ever actionable by a remote agent — this is the pivot the
    anti-recursion classifier keys on:

    * ``CHAT``   — ambient conversation / discussion. Never triggers an agent.
    * ``TASK``   — a directive. Actionable *only* when directed at you.
    * ``RESULT`` — an agent reporting back. Explicitly non-actionable so a
                   result can't re-trigger another agent (breaks A->B->A loops).
    * ``SYSTEM`` — join/leave/roster notices.
    """

    CHAT = "chat"
    TASK = "task"
    RESULT = "result"
    SYSTEM = "system"


class NodeRole(str, enum.Enum):
    """A node's role in a room. Exactly one OWNER per room (the host)."""

    OWNER = "owner"
    MEMBER = "member"


@dataclass
class NodeIdentity:
    """Stable identity of one HandQ instance on the LAN."""

    node_id: str
    display_name: str
    user_name: str
    hostname: str
    platform: str
    is_owner: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "user_name": self.user_name,
            "hostname": self.hostname,
            "platform": self.platform,
            "is_owner": self.is_owner,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodeIdentity":
        return cls(
            node_id=str(d.get("node_id") or uuid.uuid4().hex),
            display_name=str(d.get("display_name") or "unknown"),
            user_name=str(d.get("user_name") or ""),
            hostname=str(d.get("hostname") or ""),
            platform=str(d.get("platform") or ""),
            is_owner=bool(d.get("is_owner", False)),
        )

    @classmethod
    def local(
        cls,
        *,
        display_name: Optional[str] = None,
        user_name: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> "NodeIdentity":
        """Build an identity for *this* machine, autodetecting host/user/OS.

        ``display_name`` defaults to the hostname (that's what other people on
        the LAN will type after ``@`` to address this node).
        """
        host = socket.gethostname() or "handq"
        user = (
            user_name
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or ""
        )
        return cls(
            node_id=node_id or uuid.uuid4().hex,
            display_name=display_name or host,
            user_name=user,
            hostname=host,
            platform=platform.system() or "",
            is_owner=False,
        )


@dataclass
class ChatMessage:
    """One line in the shared transcript.

    ``mentions`` are addressing tokens (node display names / ids, or a
    broadcast token like ``all``). Empty ``mentions`` == broadcast. ``seq`` is
    assigned by the owner (the relay hub) to give every node the same total
    ordering; it is ``None`` on a message that hasn't been relayed yet.
    """

    msg_id: str
    room: str
    sender_node_id: str
    sender_display: str
    sender_kind: SenderKind
    sender_is_owner: bool
    intent: MessageIntent
    mentions: List[str]
    body: str
    hop: int = 0
    reply_to: Optional[str] = None
    ts: float = field(default_factory=time.time)
    seq: Optional[int] = None

    @classmethod
    def new(
        cls,
        *,
        room: str,
        sender: NodeIdentity,
        sender_kind: SenderKind,
        intent: MessageIntent,
        body: str,
        mentions: Optional[List[str]] = None,
        hop: int = 0,
        reply_to: Optional[str] = None,
    ) -> "ChatMessage":
        return cls(
            msg_id=uuid.uuid4().hex,
            room=room,
            sender_node_id=sender.node_id,
            sender_display=sender.display_name,
            sender_kind=sender_kind,
            sender_is_owner=sender.is_owner,
            intent=intent,
            mentions=list(mentions or []),
            body=body,
            hop=hop,
            reply_to=reply_to,
            ts=time.time(),
            seq=None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "room": self.room,
            "sender_node_id": self.sender_node_id,
            "sender_display": self.sender_display,
            "sender_kind": self.sender_kind.value,
            "sender_is_owner": self.sender_is_owner,
            "intent": self.intent.value,
            "mentions": list(self.mentions),
            "body": self.body,
            "hop": self.hop,
            "reply_to": self.reply_to,
            "ts": self.ts,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChatMessage":
        return cls(
            msg_id=str(d.get("msg_id") or uuid.uuid4().hex),
            room=str(d.get("room") or ""),
            sender_node_id=str(d.get("sender_node_id") or ""),
            sender_display=str(d.get("sender_display") or ""),
            sender_kind=SenderKind(d.get("sender_kind") or "user"),
            sender_is_owner=bool(d.get("sender_is_owner", False)),
            intent=MessageIntent(d.get("intent") or "chat"),
            mentions=[str(m) for m in (d.get("mentions") or [])],
            body=str(d.get("body") or ""),
            hop=int(d.get("hop") or 0),
            reply_to=(str(d["reply_to"]) if d.get("reply_to") else None),
            ts=float(d.get("ts") or time.time()),
            seq=(int(d["seq"]) if d.get("seq") is not None else None),
        )


@dataclass
class Participant:
    """A roster entry: an identity plus its live connection state."""

    node: NodeIdentity
    address: str = ""
    joined_at: float = field(default_factory=time.time)
    online: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "address": self.address,
            "joined_at": self.joined_at,
            "online": self.online,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Participant":
        return cls(
            node=NodeIdentity.from_dict(d.get("node") or {}),
            address=str(d.get("address") or ""),
            joined_at=float(d.get("joined_at") or time.time()),
            online=bool(d.get("online", True)),
        )


@dataclass
class Classification:
    """Receiver-relative address verdict for one message. Computed by
    :mod:`.router`.

    Chatroom is a dumb pipe — it does NOT decide "should my agent act on
    this?". That's the orchestrator's job (using its normal chat-vs-task
    intent classifier plus these address hints). Fields here answer only:
    "does this message concern me, and how?".
    """

    is_self: bool
    mentions_me: bool
    is_broadcast: bool
    directed_to_me: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_self": self.is_self,
            "mentions_me": self.mentions_me,
            "is_broadcast": self.is_broadcast,
            "directed_to_me": self.directed_to_me,
        }


@dataclass
class IncomingMessage:
    """What a callback receives: the message + the local verdict about it."""

    message: ChatMessage
    classification: Classification

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message.to_dict(),
            "classification": self.classification.to_dict(),
        }


@dataclass
class RoomInfo:
    """Returned by :meth:`ChatRoomService.host` / :meth:`join`."""

    room: str
    host: str
    port: int
    is_owner: bool
    node_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room": self.room,
            "host": self.host,
            "port": self.port,
            "is_owner": self.is_owner,
            "node_count": self.node_count,
        }
