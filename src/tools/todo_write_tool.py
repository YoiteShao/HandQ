"""
todo_write — the agent's own progress scratchpad.

Claude-Code-style TodoWrite: the AGENT writes and reads this to decompose its
current task into steps and track which are done. It is the worker's private
plan, surfaced to the UI so the user can watch progress — NOT a supervisor's
plan (that's TaskChannel, the Coordinator↔Agent IPC channel).

Semantics (mirrors TodoWrite):
  - The agent passes the FULL list each call; it replaces the stored list
    (last-write-wins), so the agent edits by re-emitting.
  - Each item: {content: str, status: "pending"|"in_progress"|"completed"}.
  - Stored on the SessionContext (ctx.agent_todo); surfaced to the UI via
    InteractionManager.notify_agent_todo_changed.

Use it for multi-step work so progress survives context compaction and the user
can see the plan. Skip it for trivial single-step tasks.
"""
import time
from typing import Any, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext

_VALID_STATUS = ("pending", "in_progress", "completed")


class TodoWriteTool(BaseTool):
    """Write the agent's own todo list (its plan for the current task)."""

    is_read_only = True  # touches only session-local todo state, no filesystem
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("todo_write", ctx=ctx)

    async def execute(self, todos: Any = None, **kwargs: Any) -> ToolResult:
        start = time.time()
        params = {"todos": todos}

        normalized = self._normalize(todos)
        if normalized is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "todo_write expects 'todos': a list of "
                    "{content, status} objects (status ∈ "
                    "pending|in_progress|completed)."
                ),
                execution_time=time.time() - start,
                tool_name=self.name,
                tool_parameters=params,
            )

        # Store on the session ctx (last-write-wins) and surface to the UI.
        if self.ctx is not None:
            try:
                self.ctx.agent_todo = normalized
            except Exception:
                pass
            im = getattr(self.ctx, "interaction_manager", None)
            if im is not None:
                try:
                    im.notify_agent_todo_changed(normalized)
                except Exception:
                    pass

        done = sum(1 for t in normalized if t["status"] == "completed")
        return ToolResult(
            success=True,
            output={
                "todos": normalized,
                "total": len(normalized),
                "completed": done,
            },
            execution_time=time.time() - start,
            tool_name=self.name,
            tool_parameters=params,
        )

    @staticmethod
    def _normalize(todos: Any) -> Optional[List[dict]]:
        """Coerce input into a clean [{content, status}] list, or None if invalid."""
        if not isinstance(todos, list):
            return None
        out: List[dict] = []
        for entry in todos:
            if isinstance(entry, str):
                content, status = entry.strip(), "pending"
            elif isinstance(entry, dict):
                content = str(entry.get("content", "")).strip()
                status = str(entry.get("status", "pending")).strip().lower()
            else:
                continue
            if not content:
                continue
            if status not in _VALID_STATUS:
                status = "pending"
            out.append({"content": content, "status": status})
        return out

    @classmethod
    def get_schema(cls):
        return {
            "todos": {
                "type": "array",
                "description": (
                    "Your full plan for the current task, re-emitted each call "
                    "(replaces the stored list). Use for multi-step work so "
                    "progress survives compaction and the user can watch it; "
                    "skip for trivial one-step tasks. Mark exactly one item "
                    "in_progress at a time; flip to completed as you finish."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "One concrete step.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(_VALID_STATUS),
                            "description": "pending | in_progress | completed",
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        }
