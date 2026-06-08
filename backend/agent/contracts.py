"""Native message + tool data contracts for the v2 agent.

This module replaces three legacy pieces in one place:
  * ``src.models.plan.Step`` / ``Plan`` / ``AgentResult`` / ``TokenUsage`` —
    old dataclasses around the FlowController planner. Agent loops in v2
    speak Messages, not Steps.
  * ``src.tools.base_tool.ToolResult`` — carried v1-era display fields
    (diff_output, lines_written) we no longer need at the contract layer.
  * ``src.tools.tool_registry.ToolMetadata`` — split spec from dispatch and
    leaked the "registry vs class" duality. ``ToolSpec`` collapses both:
    the agent-facing description and the async ``run`` callable in one
    object so registration is a single value, not a (metadata, class) pair.

Five types, all immutable-ish dataclasses with explicit ``to_dict``/``from_dict``
boundary so they stay JSON-portable for persistence and IPC. No LLM API
shape is hard-coded here — ``ToolSpec.to_api_dict`` is the only adapter
that names a wire format, and even that is one method, not a class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


class MessageRole(str, Enum):
    """Roles modeled after the major chat-completions APIs.

    SYSTEM   — assistant configuration / persona; lives at the top of the buffer.
    USER     — human input; also where the orchestration injects sub-goals.
    ASSISTANT— model output; the only role that may carry ``tool_calls``.
    TOOL     — outcome of a tool call; ``tool_call_id`` pairs it with its caller.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """One tool invocation requested by the assistant during a turn.

    Lives inside an assistant Message's ``tool_calls`` list. The runtime
    dispatches each call to the registry; the resulting ``ToolResult`` is
    folded back as a TOOL message keyed by ``call_id``. ``call_id`` must be
    unique inside the buffer, not just the turn — otherwise a re-running
    agent (resume from a saved state) could pair an old result with a new
    call by accident.
    """

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        if not isinstance(d, dict):
            raise TypeError(f"ToolCall.from_dict expects dict, got {type(d).__name__}")
        for key in ("call_id", "name"):
            if key not in d:
                raise ValueError(f"ToolCall missing required field: {key!r}")
        return cls(
            call_id=str(d["call_id"]),
            name=str(d["name"]),
            arguments=dict(d.get("arguments") or {}),
        )


@dataclass
class ToolResult:
    """Outcome of one tool call.

    Trimmed from ``src.tools.base_tool.ToolResult``: dropped the v1 display
    fields (diff_output / lines_written / display_info / get_obs_dict) which
    confuse "data the agent needs" with "data a UI might want." UI-formatting
    belongs above this layer, never on the contract.

    ``ok=True`` ⇒ ``output`` is the value to feed back to the model.
    ``ok=False`` ⇒ ``error`` is the failure reason; ``output`` is ignored.
    ``metadata`` is the open extension slot for execution telemetry (tokens,
    exit_code, latency_ms, etc.) without growing the contract.
    """

    call_id: str
    ok: bool
    output: Any = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"call_id": self.call_id, "ok": self.ok}
        if self.ok:
            d["output"] = self.output
        else:
            d["error"] = self.error or ""
        if self.metadata:
            d["metadata"] = dict(self.metadata)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolResult":
        if not isinstance(d, dict):
            raise TypeError(f"ToolResult.from_dict expects dict, got {type(d).__name__}")
        for key in ("call_id", "ok"):
            if key not in d:
                raise ValueError(f"ToolResult missing required field: {key!r}")
        return cls(
            call_id=str(d["call_id"]),
            ok=bool(d["ok"]),
            output=d.get("output"),
            error=d.get("error"),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass
class Message:
    """One entry in the agent's conversation buffer.

    Validation is enforced in ``__post_init__`` so an invalid Message can
    never sit in the buffer:
      * TOOL messages MUST carry ``tool_call_id`` (otherwise the model can't
        match the result to its caller).
      * Only ASSISTANT messages may carry ``tool_calls``.
      * String role values are normalized to ``MessageRole`` so callers can
        pass either form (matches how raw API payloads usually arrive).
    """

    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            self.role = MessageRole(self.role)
        if self.role is MessageRole.TOOL and self.tool_call_id is None:
            raise ValueError("TOOL messages must carry tool_call_id")
        if self.role is not MessageRole.ASSISTANT and self.tool_calls:
            raise ValueError(
                f"tool_calls only allowed on ASSISTANT messages, got {self.role.value}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        if not isinstance(d, dict):
            raise TypeError(f"Message.from_dict expects dict, got {type(d).__name__}")
        if "role" not in d:
            raise ValueError("Message missing required field: 'role'")
        return cls(
            role=MessageRole(d["role"]),
            content=str(d.get("content", "")),
            tool_calls=[ToolCall.from_dict(tc) for tc in (d.get("tool_calls") or [])],
            tool_call_id=d.get("tool_call_id"),
        )


# A tool's dispatch is just an async callable returning a ToolResult; no
# elaborate Protocol class needed. The agent loop type-checks this nominally
# at registry registration, not statically.
ToolRunner = Callable[..., Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    """Declarative spec PLUS dispatch for one agent-callable tool.

    Replaces the (ToolMetadata + tool_class) dance: one value carries the
    name, the description and JSON-schema the model sees, the prose usage
    guide that goes verbatim into the system prompt's tools block, and the
    async ``run`` to invoke. The runtime only ever holds a ``dict[str,
    ToolSpec]`` — there is no separate "registry of classes" to keep in
    sync.

    ``mutating=True`` flags filesystem-changing tools so the agent loop can
    snapshot via ``engine/checkpoint.py`` before dispatch and roll back on
    a turn-level failure. ``concurrency_safe=True`` lets the loop fan out
    parallel calls (read/grep/glob) safely; default is False so a tool
    that hasn't opted in is treated as serializing.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolRunner
    usage_guide: str = ""
    mutating: bool = False
    concurrency_safe: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        """Wire shape for tool advertisement (Anthropic-style ``input_schema``).

        The only API-shape leak in this module. Wire formats vary by provider,
        so additional adapters belong on the LLM client, not here — this one
        is just the most common shape.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
