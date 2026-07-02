"""LAN chat room for HandQ instances.

A standalone networking subsystem that lets multiple HandQ instances on the
same local network join a shared "room" and exchange messages. The room has a
single *owner* (the hosting HandQ + its user) that runs a TCP relay; other
HandQ instances join as *members*.

Design (see module docstrings for detail):

* **Identity** — every message says who authored it (``SenderKind.user`` vs
  ``SenderKind.handq``) and from which node, so anyone can tell humans from
  agents and one machine from another.
* **Addressing** — ``@node`` mentions target a specific node; no mention (or
  ``@all``) is a broadcast. :mod:`.router` classifies the address relationship
  ("is this mine? a broadcast? from me?") but does NOT decide actionability —
  the orchestrator downstream of ``on_message`` uses its native chat-vs-task
  intent classifier plus these hints to decide whether to act.
* **Guardrails on the wire** — only two, and both apply only to
  ``sender_kind == HANDQ`` messages (human messages flow freely):

    - **R2 · Pair cooldown** — if two agents' combined handq-msg count in
      ``ECHO_WINDOW_SEC`` reaches ``ECHO_MAX_PAIR_MSGS``, the pair enters
      ``ECHO_COOLDOWN_SEC`` of silence.
    - **R3 · Room budget** — if the room-wide handq-msg count in
      ``BUDGET_WINDOW_SEC`` reaches ``BUDGET_MAX_HANDQ_MSGS``, further handq
      messages are dropped until :meth:`ChatRoomService.reset_guardrails`.

  These exist because LLM chatter happens at machine speed; a human in the
  room can intervene on anything else. Blocked messages are replaced with a
  SYSTEM notice so the room (and its humans) see the block happen.

This package is self-contained and NOT yet wired into the bridge. Drive it via
:class:`ChatRoomService` or the ``chatroom_demo.py`` CLI at the repo root.
"""
from __future__ import annotations

from .models import (
    ChatMessage,
    Classification,
    IncomingMessage,
    MessageIntent,
    NodeIdentity,
    NodeRole,
    Participant,
    RoomInfo,
    SenderKind,
)
from .router import classify, matches_identity, parse_mentions
from .service import ChatRoomError, ChatRoomService

__all__ = [
    "ChatRoomService",
    "ChatRoomError",
    "ChatMessage",
    "Classification",
    "IncomingMessage",
    "MessageIntent",
    "NodeIdentity",
    "NodeRole",
    "Participant",
    "RoomInfo",
    "SenderKind",
    "classify",
    "matches_identity",
    "parse_mentions",
]
