"""Shared constants for the LAN chat-room subsystem.

Kept in one place (mirroring ``long_term_memory/_constants.py``) so the wire
protocol version, ports, and echo-prevention knobs have a single source of
truth across ``protocol`` / ``transport`` / ``discovery`` / ``service``.
"""
from __future__ import annotations

# ── Wire protocol ───────────────────────────────────────────────────────────
# Bumped whenever the envelope schema changes incompatibly. hello/welcome
# refuse a mismatch so a half-upgraded LAN fails loud instead of silently
# mis-parsing frames.
PROTOCOL_VERSION = 1

# ── Ports ─────────────────────────────────────────────────────────────────--
# UDP beacon port the owner broadcasts on and members listen on for discovery.
DISCOVERY_PORT = 48610
# Default TCP port the owner's relay server binds. ``host(tcp_port=0)`` lets
# the OS pick a free port instead (used by tests / when the default is taken).
DEFAULT_TCP_PORT = 48611

# Beacon payload marker so we ignore unrelated UDP traffic on the port.
DISCOVERY_MAGIC = "HANDQ-ROOM"

# ── Timers (seconds) ─────────────────────────────────────────────────────---
BEACON_INTERVAL_SEC = 2.0
HEARTBEAT_INTERVAL_SEC = 15.0
CONNECT_TIMEOUT_SEC = 10.0
HELLO_TIMEOUT_SEC = 10.0

# ── Body / transcript bounds ─────────────────────────────────────────────---
# Hard cap on a single chat body so a peer can't wedge the relay with a
# multi-megabyte line. Enforced defensively at send time.
MAX_BODY_BYTES = 64 * 1024

TRANSCRIPT_MAX_LEN = 2000

# Tokens that mean "address everyone". A message with one of these in its
# mentions is a pure broadcast; if the mentions list is empty it's also a
# broadcast. Used by ``router.classify`` for the ``is_broadcast`` verdict.
BROADCAST_TOKENS = frozenset({"all", "everyone", "here", "channel", "room"})

# ── Guardrails (owner-enforced, handq-only) ─────────────────────────────────
# Chatroom is otherwise a dumb pipe — the orchestrator decides whether a
# message is a chat or a task. These two guardrails exist because LLM chatter
# happens faster than a human can react, so we need a machine-speed brake.
# Both apply ONLY to ``sender_kind == HANDQ`` messages. Human messages are
# never rate-limited or counted.
#
# R2 · Pair cooldown
#     If any two agents' combined handq-message count within ECHO_WINDOW_SEC
#     reaches ECHO_MAX_PAIR_MSGS, the pair enters ECHO_COOLDOWN_SEC of
#     silence: subsequent handq messages from either party are dropped and
#     replaced by a SYSTEM notice so the room sees the block happen.
ECHO_WINDOW_SEC = 60.0
ECHO_MAX_PAIR_MSGS = 4
ECHO_COOLDOWN_SEC = 60.0

# R3 · Room-level handq budget (defense-in-depth for when R2 has a bug or
# when many agents chat in parallel).
BUDGET_WINDOW_SEC = 300.0
BUDGET_MAX_HANDQ_MSGS = 60

# ── Broadcast fan-out ────────────────────────────────────────────────────---
# Per-connection deadline for owner→member fan-out. One slow member can't
# stall the relay because each send is awaited concurrently with this cap.
BROADCAST_SEND_TIMEOUT_SEC = 5.0

# ── Auto-reconnect (member) ───────────────────────────────────────────────
RECONNECT_BASE_SEC = 1.0
RECONNECT_MAX_SEC = 30.0
RECONNECT_MAX_ATTEMPTS = 10
