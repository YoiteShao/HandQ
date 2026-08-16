"""
todo_write — the agent's own progress scratchpad.

Claude-Code-style TodoWrite: the AGENT writes and reads this to decompose its
current task into steps and track which are done. It is the worker's private
plan, surfaced to the UI so the user can watch progress — NOT a supervisor's
plan (that's TaskChannel, the Coordinator↔Agent IPC channel).

Semantics (mirrors TodoWrite):
  - The agent passes the FULL list each call; it replaces the stored list
    (last-write-wins), so the agent edits by re-emitting.
  - Each item: {content: str, status: "pending"|"in_progress"|"completed",
    verify?: str, evidence?: str}.
  - ``verify`` is the oracle: how the agent will KNOW the step worked, chosen
    before acting. ``evidence`` is what it actually observed, and is MANDATORY
    to set status="completed" — a call that completes an item without evidence
    is rejected. See the block comment in ``execute`` for the two 2026-08-03
    failures this exists to stop.
  - Evidence already recorded for an item is carried forward automatically
    across re-emissions (matched by ``content``).
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

        # Carry forward evidence already recorded for a completed item, keyed by
        # content. The agent re-emits the whole list every call, so without this
        # it would have to retype every past item's evidence forever — friction
        # that would push it straight back to evidence-free completions.
        prior: dict = {}
        if self.ctx is not None:
            for entry in (getattr(self.ctx, "agent_todo", None) or []):
                if isinstance(entry, dict):
                    key = str(entry.get("content", "")).strip()
                    ev = str(entry.get("evidence", "")).strip()
                    if key and ev:
                        prior[key] = ev

        normalized = self._normalize(todos, prior)
        if normalized is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "todo_write expects 'todos': a list of "
                    "{content, status, verify, evidence} objects (status ∈ "
                    "pending|in_progress|completed)."
                ),
                execution_time=time.time() - start,
                tool_name=self.name,
                tool_parameters=params,
            )

        # A step is not done because you decided it is done. Completing an item
        # requires naming what you OBSERVED that proves it.
        #
        # 2026-08-03 flash-meta run, the two failures this blocks:
        #   * "Set active partition = 1" was marked complete. The value appears
        #     with a value NOWHERE in the 867KB trace — a buggy selector had
        #     matched a container blob and reported ALREADY_ON for a different
        #     toggle, and the =1 half never executed.
        #   * "Boot SS EDL" was declared done ("🎉 Boot SS EDL 成功!") off a
        #     script's own print("...sent!"). The agent had ALREADY written the
        #     real oracle down at turn 34 ("QDLoader 9008 COM5 Status: OK") and
        #     then read Status: Unknown eleven more times without acting on it.
        # In both cases the agent had to state nothing observable to close the
        # item. Now it does.
        missing = [
            t["content"] for t in normalized
            if t["status"] == "completed" and not t.get("evidence")
        ]
        if missing:
            listed = "; ".join(f'"{c[:70]}"' for c in missing[:5])
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"Refusing the update: {len(missing)} item(s) are marked "
                    f"completed with no `evidence` — {listed}. "
                    "For each, either (a) add `evidence` quoting the concrete "
                    "thing you OBSERVED that proves it (a tool result value, a "
                    "log line, a read-back setting, an exit status — not "
                    "'clicked it', not 'sent the command', not a success string "
                    "a script you wrote printed for itself), or (b) set the "
                    "status back to in_progress and go verify it. "
                    "An action succeeding is not the goal being met."
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
        unverified = [
            t["content"] for t in normalized
            if t["status"] != "completed" and not t.get("verify")
        ]
        out: dict = {
            "todos": normalized,
            "total": len(normalized),
            "completed": done,
        }
        if unverified:
            out["hint"] = (
                f"{len(unverified)} open item(s) have no `verify` oracle yet. "
                "Decide NOW how each will be checked — deciding afterwards is "
                "how 'it probably worked' gets recorded as done."
            )
        return ToolResult(
            success=True,
            output=out,
            execution_time=time.time() - start,
            tool_name=self.name,
            tool_parameters=params,
        )

    @staticmethod
    def _normalize(todos: Any, prior: Optional[dict] = None) -> Optional[List[dict]]:
        """Coerce input into a clean [{content, status, verify?, evidence?}] list.

        Returns None if the input isn't a list. ``prior`` maps content →
        previously-recorded evidence and is used to carry evidence forward
        across re-emissions.
        """
        if not isinstance(todos, list):
            return None
        prior = prior or {}
        out: List[dict] = []
        for entry in todos:
            verify = ""
            evidence = ""
            if isinstance(entry, str):
                content, status = entry.strip(), "pending"
            elif isinstance(entry, dict):
                content = str(entry.get("content", "")).strip()
                status = str(entry.get("status", "pending")).strip().lower()
                verify = str(entry.get("verify", "") or "").strip()
                evidence = str(entry.get("evidence", "") or "").strip()
            else:
                continue
            if not content:
                continue
            if status not in _VALID_STATUS:
                status = "pending"
            if not evidence:
                evidence = prior.get(content, "")
            item: dict = {"content": content, "status": status}
            if verify:
                item["verify"] = verify
            if evidence:
                item["evidence"] = evidence
            out.append(item)
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
                    "in_progress at a time. An item can only be flipped to "
                    "completed if it carries `evidence` — the call is REJECTED "
                    "otherwise."
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
                        "verify": {
                            "type": "string",
                            "description": (
                                "How you will KNOW this step worked — the "
                                "observable you intend to check, decided before "
                                "you act. Name the specific signal, e.g. "
                                "\"device list shows QDLoader 9008 with "
                                "Status: OK\", \"activity log contains "
                                "'Status: SUCCESS'\", \"re-reading the field "
                                "returns 1\". Not \"the click succeeds\" — a "
                                "click succeeding is not the goal being met."
                            ),
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "REQUIRED to set status=completed. The concrete "
                                "thing you actually OBSERVED, quoted: a value "
                                "read back, a log line, an exit status. "
                                "NOT accepted as evidence: that a tool returned "
                                "success, that a command was sent, or a success "
                                "message printed by a script you wrote yourself "
                                "— those say the action ran, not that it worked."
                            ),
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        }
