"""Native v2 agent runtime — package marker.

This package owns the v2 single-loop agent and its data contracts. It is
deliberately self-contained: nothing in ``backend/agent/`` imports from
``src/agent`` or ``src/models``. ``SubagentExecutor`` is the production
executor the orchestration runner uses for ``AgentNode`` bodies.
"""
from __future__ import annotations

from .contracts import Message, MessageRole, ToolCall, ToolResult, ToolSpec
from .llm import LLMClient, LLMResponse
from .subagent import Subagent, SubagentResult, SubagentSpec

__all__ = [
    "Message", "MessageRole", "ToolCall", "ToolResult", "ToolSpec",
    "LLMClient", "LLMResponse",
    "Subagent", "SubagentSpec", "SubagentResult",
]
