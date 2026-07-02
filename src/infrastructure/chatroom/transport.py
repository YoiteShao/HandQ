"""Transport primitives: a JSON-line connection over an asyncio stream.

One :class:`JsonlConnection` wraps a ``(StreamReader, StreamWriter)`` pair and
is used identically by both sides — the owner holds one per connected member,
each member holds one to the owner. Writes are serialized behind a per-conn
lock so concurrent ``send`` calls can't interleave half-lines on the wire.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Optional

from . import protocol
from ._constants import CONNECT_TIMEOUT_SEC

_logger = logging.getLogger("handq.chatroom.transport")


class JsonlConnection:
    """A framed, JSON-per-line duplex connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._closed = False
        try:
            self.peername = writer.get_extra_info("peername")
        except Exception:
            self.peername = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, frame: Dict[str, Any]) -> None:
        """Encode and write a single frame. Raises on a dead connection."""
        if self._closed:
            raise ConnectionError("connection closed")
        data = protocol.encode(frame)
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def recv(self, *, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Read one frame. Returns ``None`` on clean EOF (peer hung up)."""
        try:
            if timeout is not None:
                line = await asyncio.wait_for(self._reader.readline(), timeout)
            else:
                line = await self._reader.readline()
        except asyncio.TimeoutError:
            raise
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            return None
        if not line:  # EOF
            return None
        try:
            return protocol.decode_line(line.decode("utf-8", errors="replace"))
        except protocol.ProtocolError as exc:
            _logger.warning("dropping malformed frame from %s: %s", self.peername, exc)
            # Skip the bad line and keep the connection alive.
            return await self.recv(timeout=timeout)

    async def messages(self) -> AsyncIterator[Dict[str, Any]]:
        """Async-iterate frames until the peer disconnects."""
        while True:
            frame = await self.recv()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            return
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


async def connect_to_hub(
    host: str,
    port: int,
    *,
    timeout: float = CONNECT_TIMEOUT_SEC,
) -> JsonlConnection:
    """Open a TCP connection to the owner's relay and wrap it."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout
    )
    return JsonlConnection(reader, writer)
