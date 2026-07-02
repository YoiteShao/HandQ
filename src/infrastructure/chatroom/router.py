"""Addressing helpers + a stateless address classifier.

This module is pure logic (no IO, no asyncio) so its output is trivially
testable in isolation.

**What this module decides**: for one incoming :class:`ChatMessage` and one
receiver identity, what's the *addressing* relationship — is it mine, a
broadcast, from me, aimed at me? See :class:`Classification`.

**What this module does NOT decide**: whether the receiver's agent should
*act* on the message. Chatroom is a dumb pipe: the orchestrator sees every
message (with the address hints below) and makes that decision using its
normal chat-vs-task classifier plus context like sender_kind. Keeping the
policy on the orchestrator side lets a single knob govern "when do I act",
whether the trigger is a local user message, a scheduler tick, or a
chatroom mention.
"""
from __future__ import annotations

import re
from typing import List

from ._constants import BROADCAST_TOKENS
from .models import ChatMessage, Classification, NodeIdentity

# Grab ``@token`` runs. Allow word chars plus the few punctuation marks that
# show up in hostnames / node ids / role suffixes: - _ . /
_MENTION_RE = re.compile(r"@([A-Za-z0-9_./\-]+)")

# node_id is a uuid4 hex (32 chars); allow a short prefix to still match so a
# user can type ``@3f9a1c`` instead of the whole thing.
_MIN_ID_PREFIX = 6


def normalize_token(token: str) -> str:
    """Lower-case, strip a leading ``@`` and any ``/role`` suffix.

    ``@Win-PC-2/handq`` -> ``win-pc-2``. The role suffix is accepted for
    ergonomics but doesn't change targeting: addressing a node addresses the
    node, and the orchestrator on that node decides what to do.
    """
    t = token.strip()
    if t.startswith("@"):
        t = t[1:]
    t = t.split("/", 1)[0]
    return t.lower()


def parse_mentions(body: str) -> List[str]:
    """Extract ``@mention`` tokens from a message body, in order, de-duped.

    Returns normalized tokens. Empty list == broadcast.
    """
    seen: List[str] = []
    for raw in _MENTION_RE.findall(body or ""):
        tok = normalize_token(raw)
        if tok and tok not in seen:
            seen.append(tok)
    return seen


def matches_identity(token: str, me: NodeIdentity) -> bool:
    """True if *token* names *me* (by display name or node-id prefix)."""
    tok = normalize_token(token)
    if not tok or tok in BROADCAST_TOKENS:
        return False
    if tok == me.display_name.strip().lower():
        return True
    node_id = me.node_id.lower()
    if tok == node_id:
        return True
    if len(tok) >= _MIN_ID_PREFIX and node_id.startswith(tok):
        return True
    return False


def classify(msg: ChatMessage, me: NodeIdentity) -> Classification:
    """Compute the receiver-relative address verdict of *msg* for node *me*.

    Fields:
      * ``is_self``        — I authored this (never react to yourself).
      * ``is_broadcast``   — no mentions, or an @all-style token present.
      * ``directed_to_me`` — I am named *explicitly* (not merely via @all).
      * ``mentions_me``    — directed_to_me OR broadcast (I should read it).

    Whether the orchestrator should act on a directed TASK is decided by the
    orchestrator, not here.
    """
    tokens = [normalize_token(m) for m in (msg.mentions or [])]

    is_self = bool(msg.sender_node_id) and msg.sender_node_id == me.node_id
    has_broadcast_tok = any(t in BROADCAST_TOKENS for t in tokens)
    is_broadcast = (not tokens) or has_broadcast_tok

    directed_to_me = any(matches_identity(t, me) for t in tokens)
    mentions_me = directed_to_me or is_broadcast

    return Classification(
        is_self=is_self,
        mentions_me=mentions_me,
        is_broadcast=is_broadcast,
        directed_to_me=directed_to_me,
    )
