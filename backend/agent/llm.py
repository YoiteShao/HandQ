"""Native LLM client interface for the v2 subagent runtime.

The agent loop in ``subagent.py`` consumes one method: ``LLMClient.complete``
takes a conversation buffer and a list of tools, returns one assistant
message (text + optional tool calls). Concrete clients (Anthropic, QGenie,
fakes for tests) implement this Protocol and live outside ``backend/`` so
the orchestration stays decoupled from any specific provider.

Why a custom Protocol instead of reusing ``src.infrastructure.llm_service``:
the v1 LLMService API is shaped around v1's ``Step``/``Plan`` data contracts
and the FlowController hot path. Per the v2 mandate, those concepts are
explicitly old-thinking. The Protocol here speaks only the v2 contracts —
``Message``, ``ToolCall``, ``ToolSpec`` — and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import Message, ToolSpec


@dataclass
class LLMResponse:
    """One round-trip's outcome from the LLM.

    ``message`` is the next ASSISTANT message (text + optional tool_calls)
    that the agent loop appends to its conversation buffer. ``tokens``
    reports total input+output for budget accounting; ``stop_reason`` is
    the provider's reason string when available, opaque to the loop.
    ``raw`` carries the untouched provider response for debugging.
    """

    message: Message
    tokens: int = 0
    stop_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """Async LLM call boundary.

    Implementations stream / retry / authenticate however they like, but the
    contract above the line is just: take messages + advertise tools, return
    one assistant message. That is the only LLM-shaped object the v2
    orchestration trusts.
    """

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] = (),
    ) -> LLMResponse: ...
