"""Permission manager interface (report §8.7 / §11.5).

Per the user's direction this module is *interface-only* — the v2 backend
stays decoupled from any specific permission UI / policy engine. A concrete
``PermissionManager`` implementation lives above the orchestration layer
(in the frontend, in a server-side policy file, etc.). The orchestration
talks to it through the Protocol below; that is the entirety of the seam.

Three pieces:

  * ``PermissionAction`` — the kinds of things the orchestration asks about
    (run a tool, write a file, send a message, etc.). Free-form string under
    the hood so a higher layer can extend without touching this module.
  * ``PermissionRequest`` — the payload the orchestration hands to the
    manager: action + resource + the calling node's id + opaque metadata.
  * ``PermissionDecision`` — allow / deny / escalate. Escalate is for
    "ask the user" — the manager returns it when it can't decide
    autonomously and the orchestration must surface a confirmation gate.
  * ``PermissionManager`` — the Protocol with one async ``check`` method.

Scope: this is *just* the contract. No default implementation, no caching,
no policy DSL. Those are policy concerns and don't belong in the
orchestration core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class PermissionDecision(str, Enum):
    """The three answers a manager can give to a permission check."""

    ALLOW = "allow"        # proceed without asking the user
    DENY = "deny"          # refuse; the orchestration routes a fail edge
    ESCALATE = "escalate"  # the manager couldn't decide — ask the user


@dataclass
class PermissionRequest:
    """What the orchestration asks about.

    ``action`` is a free-form verb namespaced however the higher layer
    wants ("tool.shell", "patch.apply", "network.fetch"). ``resource`` is
    the target the action affects ("/etc/passwd", "https://x", a tool name).
    ``node_id`` and ``run_id`` let the manager attribute the request to the
    workflow node that triggered it.
    """

    action: str
    resource: str = ""
    node_id: str = ""
    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PermissionManager(Protocol):
    """The single seam between the orchestration and any permission policy.

    Implementations decide locally (a static allowlist, a config file) or
    interactively (round-tripping to the frontend). The orchestration is
    told a binary outcome via the returned ``PermissionDecision``; it does
    not see how the decision was reached.
    """

    async def check(self, request: PermissionRequest) -> PermissionDecision: ...
