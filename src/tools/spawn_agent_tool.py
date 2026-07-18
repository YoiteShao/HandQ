"""
spawn_agent — fork a bounded, read-only exploration sub-agent.

The main agent owns one long-lived context. Open-ended exploration ("scan the
codebase for every place X is configured", "figure out how the auth flow works")
can flood that context with dozens of file reads and grep dumps the agent will
never need again. `spawn_agent` runs that exploration in an ISOLATED sub-loop
that shares the parent's LLM services and read-only tools but keeps its own
message list — only a text summary comes back. The parent's context stays clean.

Design (deliberately minimal — NOT a second PersistentAgent):
  - Read-only tools only: read / grep / glob / shell (concurrent-safe probes).
    No write/edit/ssh/browser/desktop — a sub-agent explores, it does not act.
  - Bounded: a hard iteration cap; no task channel, no compaction.
  - Shares the parent's ctx (so tools reuse the session's file_state etc.) and
    the parent's interrupt_event, so cancelling the parent cancels the child.
  - Returns the sub-agent's final text as the tool_result — that summary is the
    only thing that enters the parent's context.

The sub-loop itself (``run_subagent_task``) is shared with ``fan_out_tool.py``,
which dispatches many of these concurrently instead of just one — same
isolation contract, same tool profiles, just fanned out.
"""
import json
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


# Tool profiles a sub-agent may run under. "explore" is read-only observation;
# "worker" adds write/edit for sub-agents that must produce file changes (used
# by fan_out_tool's parallel-work case). Neither ever includes spawn_agent/
# fan_out_agents itself — recursion is prevented structurally, not by a check.
_SUBAGENT_TOOL_PROFILES: Dict[str, tuple] = {
    "explore": ("read", "grep", "glob", "shell"),
    "worker": ("read", "grep", "glob", "shell", "write", "edit"),
}
_MAX_SUBAGENT_ITERS = 12

_EXPLORE_SYSTEM_PROMPT = """\
You are a focused exploration sub-agent. You were spawned by a parent agent to
answer ONE specific question by reading and searching — nothing else.

Rules:
- You have read-only tools: read, grep, glob, shell (use shell only for
  read-only probes like ls / cat / find / git log). You cannot write, edit, or
  touch anything outside reading. A high-risk shell command (rm, shutdown,
  etc. outside the working directory) is refused — you have no way to ask the
  user for confirmation, so don't attempt destructive commands.
- Converge fast: batch independent reads/greps in one turn. Do not wander.
- When you can answer the question, STOP calling tools and reply with a concise
  plain-text answer: the findings, with exact file paths / line numbers / values
  you actually observed. No preamble, no restating the question.
- Everything you report must trace to something a tool actually returned. Never
  guess a path or value you did not observe.
"""

_WORKER_SYSTEM_PROMPT = """\
You are a focused worker sub-agent. You were spawned by a parent agent to
complete ONE independent task — reading, searching, and writing/editing files
as needed to accomplish it.

Rules:
- You have read, grep, glob, shell (read-only probes), write, and edit. A
  high-risk shell command, or a write/edit outside the working directory, is
  refused — you have no way to ask the user for confirmation, so don't
  attempt destructive or out-of-scope actions.
- Converge fast: batch independent reads/greps in one turn. Do not wander.
- When the task is done (or you determine it cannot be done), STOP calling
  tools and reply with a concise plain-text answer: what you did or found,
  with exact file paths / line numbers / values you actually observed. No
  preamble, no restating the task.
- Everything you report must trace to something a tool actually returned or
  produced. Never guess a path or value you did not observe.
"""


async def run_subagent_task(
    prompt: str,
    *,
    tool_profile: str,
    services: list,
    tool_instances: Dict[str, BaseTool],
    ctx: Optional["SessionContext"],
) -> Dict[str, Any]:
    """Run one bounded, isolated sub-agent loop; return a summary dict.

    Shared core behind both ``spawn_agent`` (one call) and ``fan_out_agents``
    (many concurrent calls). Never raises — a failure inside the loop is
    captured and returned as ``{"ok": False, "error": ...}`` so a caller
    fanning out many of these can't have one exception take down the batch.
    """
    question = (prompt or "").strip()
    if not question:
        return {"ok": False, "error": "sub-agent task requires a non-empty prompt."}
    if not services or tool_instances is None:
        return {"ok": False, "error": "sub-agent is not wired to an LLM pool in this context."}

    allowed = _SUBAGENT_TOOL_PROFILES.get(tool_profile)
    if allowed is None:
        return {"ok": False, "error": f"unknown tool_profile '{tool_profile}'."}

    try:
        summary, iters = await _run_exploration(
            question, allowed=allowed, services=services,
            tool_instances=tool_instances, ctx=ctx,
        )
    except Exception as exc:
        return {"ok": False, "error": f"sub-agent failed: {exc}"}

    return {"ok": True, "summary": summary, "iterations": iters}


async def _run_exploration(
    question: str,
    *,
    allowed: tuple,
    services: list,
    tool_instances: Dict[str, BaseTool],
    ctx: Optional["SessionContext"],
) -> tuple:
    """Bounded OTA loop with an isolated message list, scoped to *allowed*.

    Returns (summary_text, iterations_used). Falls back to whatever text the
    model last produced if it hits the iteration cap without a clean answer.
    """
    from ..infrastructure.llm_pool import call_with_fallback
    from ..tools.tool_registry import ToolRegistry

    all_tools = ToolRegistry.generate_tools_for_api()
    tools = [
        t for t in all_tools
        if t.get("function", {}).get("name") in allowed
    ]
    system_prompt = _WORKER_SYSTEM_PROMPT if "write" in allowed else _EXPLORE_SYSTEM_PROMPT

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"[Exploration task]\n{question}"},
    ]

    last_text = ""
    for i in range(_MAX_SUBAGENT_ITERS):
        result = await call_with_fallback(
            services, dict(messages=messages, tools=tools),
        )
        if result.content:
            last_text = result.content

        if not result.tool_calls:
            # No tool call → the model is answering. Done.
            return (result.content or last_text or "(no findings)"), i + 1

        # Record the assistant turn (with its tool calls) then execute each.
        messages.append({
            "role": "assistant",
            "content": result.content or "",
            "tool_calls": [
                {
                    "id": tc.call_id,
                    "type": "function",
                    "function": {"name": tc.tool_name, "arguments": tc.tool_arguments},
                }
                for tc in result.tool_calls
            ],
        })
        for tc in result.tool_calls:
            obs = await _run_one_tool(
                tc.tool_name, tc.tool_arguments,
                allowed=allowed, tool_instances=tool_instances, ctx=ctx,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc.call_id,
                "content": obs,
            })

    # Hit the cap — ask once for a final summary with no tools.
    messages.append({
        "role": "user",
        "content": (
            "Iteration budget reached. Reply now with your best plain-text "
            "answer from what you have observed so far."
        ),
    })
    try:
        final = await call_with_fallback(services, dict(messages=messages))
        return (final.content or last_text or "(no findings)"), _MAX_SUBAGENT_ITERS
    except Exception:
        return (last_text or "(no findings; budget exhausted)"), _MAX_SUBAGENT_ITERS


async def _run_one_tool(
    tool_name: str,
    raw_args: str,
    *,
    allowed: tuple,
    tool_instances: Dict[str, BaseTool],
    ctx: Optional["SessionContext"],
) -> str:
    """Dispatch one tool call within *allowed*; return a compact JSON observation."""
    if tool_name not in allowed:
        return json.dumps(
            {"ok": False, "err": f"tool '{tool_name}' not allowed in this sub-agent"},
            ensure_ascii=False,
        )
    tool = (tool_instances or {}).get(tool_name)
    if tool is None:
        return json.dumps(
            {"ok": False, "err": f"tool '{tool_name}' unavailable"},
            ensure_ascii=False,
        )
    try:
        args = json.loads(raw_args) if raw_args else {}
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}

    # shell is the one read-side tool that can mutate state (it is NOT
    # is_read_only by construction — the sub-agent's "read-only" guarantee for
    # it rests entirely on this check). write/edit are only reachable under
    # the "worker" profile and get the equivalent working-dir scoping. Neither
    # path has an InteractionManager to request confirmation from, so unlike
    # the main agent loop (persistent_agent._check_before_act) there is no
    # "ask the user" fallback — an out-of-scope call fails closed instead.
    if tool_name == "shell" and ctx is not None:
        try:
            from ..controller_v2.risk_check import is_high_risk
            wd = ctx.working_directory or ctx.storage_directory
            if is_high_risk(tool_name, args, wd, ctx.config_manager):
                return json.dumps(
                    {
                        "ok": False,
                        "err": (
                            "Refused: this shell command is high-risk and a "
                            "sub-agent cannot request user confirmation. Report "
                            "this back to the parent agent instead of running it."
                        ),
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass
    if tool_name in ("write", "edit") and ctx is not None:
        try:
            from ..controller_v2.risk_check import is_path_within_working_dir
            wd = ctx.working_directory or ctx.storage_directory
            file_path = args.get("path", "")
            if not file_path or not is_path_within_working_dir(file_path, wd):
                return json.dumps(
                    {
                        "ok": False,
                        "err": (
                            f"Refused: {tool_name} outside the working directory "
                            "and a sub-agent cannot request user confirmation. "
                            "Report this back to the parent agent instead."
                        ),
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass
    try:
        if tool_name in ("write", "edit") and ctx is not None and args.get("path"):
            # Same path-level lock the parent agent uses (SessionContext.
            # write_lock_for) — serializes this sub-task's write against
            # ANY other concurrent writer to the same path in this session:
            # the parent agent's own tool calls, or a sibling sub-task in
            # the same fan_out_agents batch. Different paths never contend.
            async with ctx.write_lock_for(args["path"]):
                tr = await tool.execute(**args)
        else:
            tr = await tool.execute(**args)
    except Exception as exc:
        return json.dumps({"ok": False, "err": str(exc)}, ensure_ascii=False)
    return tr.to_tool_result_json()


class SpawnAgentTool(BaseTool):
    """Fork a read-only exploration sub-agent; return its text summary."""

    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("spawn_agent", ctx=ctx)
        # Injected by PersistentAgent after construction (the sub-agent reuses
        # the parent's LLM pool and read-only tool instances). When unset, the
        # tool reports a clean error rather than guessing.
        self._services: Optional[list] = None
        self._tool_instances: Optional[Dict[str, BaseTool]] = None

    def bind_runtime(self, services: list, tool_instances: Dict[str, BaseTool]) -> None:
        """Wire the parent's LLM services + tool instances into the sub-agent.

        Called once by PersistentAgent so the sub-loop can reuse the same
        credentials-bearing tool objects and LLM fallback chain without
        re-instantiating them.
        """
        self._services = services
        self._tool_instances = tool_instances

    async def execute(
        self, prompt: str = "", agent_type: str = "explore", **kwargs: Any
    ) -> ToolResult:
        start = time.time()
        params = {"prompt": prompt, "agent_type": agent_type}
        question = (prompt or "").strip()

        if not question:
            return self._err("spawn_agent requires a non-empty 'prompt'.", params, start)
        # `_tool_instances` may be an empty dict (a sub-agent with no tools is
        # still valid — it just can't call any). Only an UNBOUND runtime (None)
        # is an error, so guard on identity, not falsiness.
        if not self._services or self._tool_instances is None:
            return self._err(
                "spawn_agent is not wired to an LLM pool in this context.",
                params, start,
            )

        result = await run_subagent_task(
            question, tool_profile="explore",
            services=self._services, tool_instances=self._tool_instances,
            ctx=self.ctx,
        )
        if not result.get("ok"):
            return self._err(result.get("error", "sub-agent failed."), params, start)

        return ToolResult(
            success=True,
            output={
                "summary": result["summary"],
                "iterations": result["iterations"],
                "agent_type": agent_type,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    def _err(self, msg: str, params: dict, start: float) -> ToolResult:
        return ToolResult(
            success=False, output=None, error=msg,
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - start,
        )

    @classmethod
    def get_schema(cls):
        return {
            "prompt": {
                "type": "string",
                "description": (
                    "The exploration question for the sub-agent to answer by "
                    "reading/searching (e.g. 'find every place the retry count is "
                    "configured and report file:line'). The sub-agent runs in an "
                    "isolated context and returns only a text summary — use it to "
                    "keep bulky exploration out of your own context."
                ),
            },
            "agent_type": {
                "type": "string",
                "enum": ["explore", "general"],
                "description": (
                    "'explore' (default) — read-only investigation. Reserved for "
                    "future variants; both currently run the read-only loop."
                ),
            },
        }
