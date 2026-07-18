"""
claim_tool / release_tool — real structured tool_use for self-extension.

Confirmed bug (2026-07-14 API-compliance audit): claim_tool/release_tool used
to be a convention, not a tool — the model was expected to embed a JSON
object INSIDE its free-text `reasoning` on a tool-call turn, which
PersistentAgent._think_streaming then tried to json.loads() as a whole and
pull `claim_tool`/`release_tool` fields out of. A model that writes prose
reasoning (the normal, correct thing to do on a tool-call turn) has NO path
for that intent to reach the message structure, and gets zero feedback that
its stated intent ("I need to claim schedule_create") silently went nowhere.

Fix: claim_tool/release_tool are now real, always-visible tools (like
todo_write — on_demand=False, never gated behind claim_tool itself). Calling
one is a genuine Anthropic tool_use block that gets a real tool_result, so
the model's intent is structurally guaranteed to enter the conversation —
and if the name is invalid, it gets told so IN the tool_result instead of
silently vanishing.

execute() does NOT flip activation state itself — it stores the requested
names on the SessionContext (mirroring todo_write_tool's ctx.agent_todo
pattern), and PersistentAgent reads them back after the tool result comes
in, routing into the existing, unchanged _apply_self_extension(claim,
release). This keeps ToolRegistry.get_tool_names() / _hidden_tools /
_task_channel.activate_tools as the single source of truth for activation,
with the tool itself only being the model-facing intent channel.

The completion-turn claim_tool/release_tool path (TurnOutcome.
from_completion_text parsing the completion JSON envelope) is UNCHANGED —
that JSON is the documented completion schema, not free text, so parsing it
for these fields is structurally correct and not part of this bug.
"""
import time
from typing import Any, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


def _normalize_names(names: Any) -> List[str]:
    if not isinstance(names, list):
        return []
    return [str(n).strip() for n in names if str(n).strip()]


def _tool_registry():
    # Deferred import: tool_registry.py imports THIS module to register
    # ClaimToolTool/ReleaseToolTool, so importing ToolRegistry at module
    # level here would be circular.
    from .tool_registry import ToolRegistry
    return ToolRegistry


class ClaimToolTool(BaseTool):
    """Activate one or more hidden/on-demand tools for the rest of this task."""

    is_read_only = True  # only flips visibility state, no filesystem/network effect
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("claim_tool", ctx=ctx)

    async def execute(self, names: Any = None, **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"names": names}
        requested = _normalize_names(names)
        if not requested:
            return ToolResult(
                success=False, output=None,
                error="claim_tool expects 'names': a non-empty list of exact tool names.",
                execution_time=time.time() - start,
                tool_name=self.name, tool_parameters=params,
            )

        known = set(_tool_registry().get_tool_names())
        valid = [n for n in requested if n in known]
        unknown = [n for n in requested if n not in known]

        if self.ctx is not None:
            try:
                self.ctx.pending_claim_tool.extend(valid)
            except Exception:
                pass

        output = {"claimed": valid}
        if unknown:
            output["unknown"] = unknown
        return ToolResult(
            success=bool(valid),
            output=output,
            error=(
                f"Unknown tool name(s), not claimed: {unknown}. "
                f"Call an exact name from the [Available Tools] menu."
                if not valid and unknown else None
            ),
            execution_time=time.time() - start,
            tool_name=self.name, tool_parameters=params,
        )

    @classmethod
    def get_schema(cls):
        return {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact tool name(s) to activate, e.g. "
                    "[\"schedule_create\", \"schedule_list\", \"schedule_delete\"]. "
                    "Must match a name from the [Available Tools] menu exactly — "
                    "no wildcards/family names."
                ),
            },
        }


class ReleaseToolTool(BaseTool):
    """Hide one or more tools from the visible tool list for the rest of this task.

    The tool's loaded instance stays warm — releasing then re-claiming later
    is free (no re-instantiation), mirroring _apply_self_extension.
    """

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("release_tool", ctx=ctx)

    async def execute(self, names: Any = None, **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"names": names}
        requested = _normalize_names(names)
        if not requested:
            return ToolResult(
                success=False, output=None,
                error="release_tool expects 'names': a non-empty list of exact tool names.",
                execution_time=time.time() - start,
                tool_name=self.name, tool_parameters=params,
            )

        if self.ctx is not None:
            try:
                self.ctx.pending_release_tool.extend(requested)
            except Exception:
                pass

        return ToolResult(
            success=True,
            output={"released": requested},
            execution_time=time.time() - start,
            tool_name=self.name, tool_parameters=params,
        )

    @classmethod
    def get_schema(cls):
        return {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact tool name(s) to hide from the visible tool list.",
            },
        }
