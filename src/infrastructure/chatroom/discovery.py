"""LAN discovery via UDP broadcast beacons.

The owner periodically broadcasts a small beacon so members can find the room
without being told an IP. This is *best-effort*: some networks drop broadcast
traffic, so :meth:`ChatRoomService.join` also accepts an explicit ``host:port``
which always works. Discovery is a convenience, never a hard dependency.

Beacon payload (UDP datagram, JSON)::

    {"magic": "HANDQ-ROOM", "room": "...", "host": "192.168.x.y",
     "tcp_port": 48611, "owner": "win-pc-1", "node_id": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass
from typing import Dict, List, Optional

from ._constants import BEACON_INTERVAL_SEC, DISCOVERY_MAGIC, DISCOVERY_PORT

_logger = logging.getLogger("handq.chatroom.discovery")


@dataclass
class RoomBeacon:
    room: str
    host: str
    tcp_port: int
    owner: str
    node_id: str

    @classmethod
    def from_payload(cls, d: dict) -> Optional["RoomBeacon"]:
        if d.get("magic") != DISCOVERY_MAGIC:
            return None
        port = d.get("tcp_port")
        if port is None:
            return None
        try:
            return cls(
                room=str(d.get("room") or ""),
                host=str(d.get("host") or ""),
                tcp_port=int(port),
                owner=str(d.get("owner") or ""),
                node_id=str(d.get("node_id") or ""),
            )
        except (TypeError, ValueError):
            return None


def get_local_ip() -> str:
    """Best-effort primary LAN IPv4 address of this machine.

    Uses the "connect a UDP socket and read back the chosen source" trick —
    no packets are actually sent. Falls back to loopback if offline.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _beacon_payload(
    *, room: str, host: str, tcp_port: int, owner: str, node_id: str
) -> bytes:
    return json.dumps(
        {
            "magic": DISCOVERY_MAGIC,
            "room": room,
            "host": host,
            "tcp_port": tcp_port,
            "owner": owner,
            "node_id": node_id,
        },
        ensure_ascii=False,
    ).encode("utf-8", errors="replace")


class _BeaconListener(asyncio.DatagramProtocol):
    """Collect unique beacons keyed by ``(room, node_id)``."""

    def __init__(self) -> None:
        self.beacons: Dict[tuple, RoomBeacon] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return
        beacon = RoomBeacon.from_payload(payload)
        if beacon is None:
            return
        # Trust the datagram's source IP over a self-reported host that may be
        # 0.0.0.0 / stale (e.g. multi-homed sender).
        if not beacon.host or beacon.host in ("0.0.0.0", "127.0.0.1"):
            beacon.host = addr[0]
        self.beacons[(beacon.room, beacon.node_id)] = beacon


async def announce(
    *,
    room: str,
    tcp_port: int,
    owner_display: str,
    node_id: str,
    stop_event: asyncio.Event,
    host: Optional[str] = None,
    interval: float = BEACON_INTERVAL_SEC,
) -> None:
    """Broadcast a beacon every *interval* seconds until *stop_event* is set.

    Runs as a background task on the owner. All socket errors are swallowed and
    retried — a transient failure to broadcast must never crash the room.
    """
    ip = host or get_local_ip()
    loop = asyncio.get_running_loop()
    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
            family=socket.AF_INET,
        )
        payload = _beacon_payload(
            room=room, host=ip, tcp_port=tcp_port,
            owner=owner_display, node_id=node_id,
        )
        _logger.info(
            "announcing room=%r at %s:%d on udp/%d", room, ip, tcp_port, DISCOVERY_PORT
        )
        while not stop_event.is_set():
            try:
                transport.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except OSError as exc:
                _logger.debug("beacon sendto failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception("beacon announcer crashed")
    finally:
        if transport is not None:
            transport.close()


async def discover(*, timeout: float = 3.0) -> List[RoomBeacon]:
    """Listen for beacons for *timeout* seconds; return unique rooms found."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # SO_REUSEPORT lets several HandQ instances on one machine each receive
    # beacons (handy for local testing). Not present on Windows — ignore.
    _reuseport = getattr(socket, "SO_REUSEPORT", None)
    if _reuseport is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, _reuseport, 1)
        except OSError:
            pass
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        _logger.warning("cannot bind discovery port %d: %s", DISCOVERY_PORT, exc)
        sock.close()
        return []
    sock.setblocking(False)

    listener = _BeaconListener()
    transport, _ = await loop.create_datagram_endpoint(lambda: listener, sock=sock)
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(listener.beacons.values())
