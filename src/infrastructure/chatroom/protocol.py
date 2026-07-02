"""Wire protocol for the chat room: newline-delimited JSON frames.

We reuse the codebase's existing IPC style (JSON-per-line) rather than pulling
in websockets/aiohttp. Every frame is a dict with a ``t`` (type) discriminator;
the builders below are the single source of truth for the schema so encoders
and decoders never drift.

Frame types
-----------
``hello``    member -> owner   : {node, protocol_version}
``welcome``  owner  -> member   : {room, you, owner, roster, seq}
``roster``   owner  -> all      : {roster, seq}
``presence`` owner  -> all      : {event: join|leave, node}
``chat``     both directions    : {msg: <ChatMessage.to_dict()>}
``ping`` / ``pong``             : {ts}
``error``    owner  -> member   : {code, message}
``bye``      member -> owner    : {node_id}
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional

from ._constants import PROTOCOL_VERSION

# ── Frame type discriminators ────────────────────────────────────────────---
HELLO = "hello"
WELCOME = "welcome"
ROSTER = "roster"
PRESENCE = "presence"
CHAT = "chat"
PING = "ping"
PONG = "pong"
ERROR = "error"
BYE = "bye"


class ProtocolError(Exception):
    """Raised when a line can't be decoded as a valid frame."""


def encode(frame: Dict[str, Any]) -> bytes:
    """Serialize a frame to a single UTF-8 JSON line (with trailing ``\\n``)."""
    return (json.dumps(frame, ensure_ascii=False, default=str) + "\n").encode(
        "utf-8", errors="replace"
    )


def decode_line(line: str) -> Dict[str, Any]:
    """Parse one JSON line into a frame dict. Raises :class:`ProtocolError`."""
    line = line.strip()
    if not line:
        raise ProtocolError("empty line")
    try:
        obj = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(obj, dict) or "t" not in obj:
        raise ProtocolError("frame missing 't' discriminator")
    return obj


# ── Auth ────────────────────────────────────────────────────────────────────

def compute_auth_token(room: str, node_id: str, secret: str) -> str:
    """HMAC-SHA256 of room+node_id keyed by the shared secret."""
    msg = f"{room}:{node_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ── Builders ─────────────────────────────────────────────────────────────---

def make_hello(node: Dict[str, Any], *, auth: Optional[str] = None, last_seq: Optional[int] = None) -> Dict[str, Any]:
    frame: Dict[str, Any] = {"t": HELLO, "protocol_version": PROTOCOL_VERSION, "node": node}
    if auth is not None:
        frame["auth"] = auth
    if last_seq is not None:
        frame["last_seq"] = last_seq
    return frame


def make_welcome(
    *,
    room: str,
    you: Dict[str, Any],
    owner: Dict[str, Any],
    roster: List[Dict[str, Any]],
    seq: int,
) -> Dict[str, Any]:
    return {
        "t": WELCOME,
        "protocol_version": PROTOCOL_VERSION,
        "room": room,
        "you": you,
        "owner": owner,
        "roster": roster,
        "seq": seq,
    }


def make_roster(roster: List[Dict[str, Any]], *, seq: int) -> Dict[str, Any]:
    return {"t": ROSTER, "roster": roster, "seq": seq}


def make_presence(event: str, node: Dict[str, Any]) -> Dict[str, Any]:
    return {"t": PRESENCE, "event": event, "node": node}


def make_chat(msg: Dict[str, Any]) -> Dict[str, Any]:
    return {"t": CHAT, "msg": msg}


def make_ping(ts: float) -> Dict[str, Any]:
    return {"t": PING, "ts": ts}


def make_pong(ts: float) -> Dict[str, Any]:
    return {"t": PONG, "ts": ts}


def make_error(code: str, message: str = "") -> Dict[str, Any]:
    return {"t": ERROR, "code": code, "message": message}


def make_bye(node_id: Optional[str]) -> Dict[str, Any]:
    return {"t": BYE, "node_id": node_id or ""}
