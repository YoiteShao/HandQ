"""
fan_out_agents — dispatch many independent sub-agent tasks concurrently.

Same mechanism as `spawn_agent` (fork of the agent itself, same tool list,
same prompt/context inheritance — see spawn_agent_tool.py's design note) but
for N tasks at once instead of one. Value is NOT execution speed for its own
sake — it's processing independent items (e.g. check 20 hosts) in parallel
without dumping 20 items' worth of raw tool output into the parent's own
context; each task's intermediate reads stay in its own isolated message
list, and only its final summary comes back.

The tool provides only the atomic primitive (isolation + concurrency). How
many tasks to spawn and how to phrase them is entirely the calling agent's
judgment — not encoded here.
"""
import asyncio
import os
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult
from .spawn_agent_tool import run_subagent_task

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


_MAX_TASKS = 30
_MIN_CONCURRENCY = 1


def _compute_max_concurrency(cpu_count: Optional[int]) -> int:
    """Adaptive ceiling instead of a flat constant: each sub-task is a full
    sub-agent session (LLM calls + tool execution), heavier than a Workflow-
    engine step, so this stays capped at 10 even on machines with many cores
    rather than mirroring Claude Code's uncapped ``min(16, cpu-2)``. Machines
    with few cores (small VMs, containers) get a lower ceiling so fan-out
    doesn't oversubscribe a constrained host. Extracted as a pure function
    (instead of computed inline at import time) so it's testable without
    reloading the module or fighting other modules' cached class references.
    """
    return max(_MIN_CONCURRENCY, min(10, (cpu_count or 4)))


_MAX_CONCURRENCY = _compute_max_concurrency(os.cpu_count())
_DEFAULT_CONCURRENCY = min(6, _MAX_CONCURRENCY)
_SUMMARY_CHAR_LIMIT = 3000


class FanOutAgentsTool(BaseTool):
    """Run N forks of the agent concurrently; return N summaries."""

    is_read_only = False  # its fixed tool list includes write/edit/notebook_edit
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("fan_out_agents", ctx=ctx)
        self._services: Optional[list] = None
        self._tool_instances: Optional[Dict[str, BaseTool]] = None
        # Providers, not frozen values — see SpawnAgentTool's __init__ for why
        # (parent's iteration budget / context block change over its
        # lifetime; calling these at spawn-time keeps every task fresh).
        self._parent_iter_budget_fn: Optional[Any] = None
        self._parent_context_block_fn: Optional[Any] = None

    def bind_runtime(self, services: list, tool_instances: Dict[str, BaseTool]) -> None:
        """Wire the parent's LLM services + tool instances (see spawn_agent)."""
        self._services = services
        self._tool_instances = tool_instances

    def bind_context(self, iter_budget_fn, context_block_fn) -> None:
        """Wire providers for the parent's live iteration budget + inheritable
        context block (see SpawnAgentTool.bind_context — same contract,
        shared by both)."""
        self._parent_iter_budget_fn = iter_budget_fn
        self._parent_context_block_fn = context_block_fn

    async def execute(
        self,
        tasks: Optional[List[Dict[str, Any]]] = None,
        inherit_context: bool = True,
        iter_budget: Optional[int] = None,
        max_concurrency: int = _DEFAULT_CONCURRENCY,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {
            "tasks": tasks, "inherit_context": inherit_context,
            "iter_budget": iter_budget, "max_concurrency": max_concurrency,
        }

        if not isinstance(tasks, list) or not tasks:
            return self._err("fan_out_agents requires a non-empty 'tasks' list.", params, start)
        if len(tasks) > _MAX_TASKS:
            return self._err(
                f"fan_out_agents accepts at most {_MAX_TASKS} tasks per call "
                f"(got {len(tasks)}). Split into multiple calls.",
                params, start,
            )
        prompts: List[str] = []
        for i, t in enumerate(tasks):
            p = (t or {}).get("prompt", "") if isinstance(t, dict) else ""
            p = (p or "").strip()
            if not p:
                return self._err(f"tasks[{i}] is missing a non-empty 'prompt'.", params, start)
            prompts.append(p)

        if not self._services or self._tool_instances is None:
            return self._err(
                "fan_out_agents is not wired to an LLM pool in this context.",
                params, start,
            )

        concurrency = max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, int(max_concurrency or _DEFAULT_CONCURRENCY)))
        semaphore = asyncio.Semaphore(concurrency)
        # Resolved ONCE for the whole batch (not per-task) — every task in
        # one fan_out_agents call shares the same snapshot of "what the
        # parent currently knows", consistent with them all being dispatched
        # from the same parent turn.
        effective_budget = iter_budget or (self._parent_iter_budget_fn() if self._parent_iter_budget_fn else None)
        effective_context = (
            self._parent_context_block_fn() if (inherit_context and self._parent_context_block_fn) else None
        )

        async def _run_one(p: str) -> Dict[str, Any]:
            async with semaphore:
                try:
                    result = await run_subagent_task(
                        p,
                        services=self._services, tool_instances=self._tool_instances,
                        ctx=self.ctx,
                        iter_budget=effective_budget,
                        inherited_context_block=effective_context,
                    )
                except Exception as exc:
                    # run_subagent_task already catches its own internal
                    # failures — this is a last-resort guard so one task's
                    # unexpected exception can never take down the batch.
                    return {"ok": False, "error": f"unexpected sub-agent failure: {exc}"}
                if not result.get("ok"):
                    return {"ok": False, "error": result.get("error", "sub-agent failed.")}
                summary = result["summary"]
                truncated = False
                if len(summary) > _SUMMARY_CHAR_LIMIT:
                    summary = summary[:_SUMMARY_CHAR_LIMIT] + "\n...[truncated]"
                    truncated = True
                return {
                    "ok": True,
                    "summary": summary,
                    "iterations": result["iterations"],
                    "truncated": truncated,
                }

        results = await asyncio.gather(*(_run_one(p) for p in prompts))

        succeeded = sum(1 for r in results if r.get("ok"))
        return ToolResult(
            success=succeeded > 0,
            output={
                "results": [
                    {"prompt": prompt, **r} for prompt, r in zip(prompts, results)
                ],
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
            },
            error=None if succeeded > 0 else "all sub-agent tasks failed",
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
            "tasks": {
                "type": "array",
                "description": (
                    f"1-{_MAX_TASKS} independent tasks to run concurrently, each as "
                    "{\"prompt\": \"...\"}. Each is a fork of YOU — same tool list, "
                    "same behavioral prompt, and (by default) the same session "
                    "context — running in its own isolated message history; only "
                    "its final text summary comes back to you."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The task/question for this sub-agent.",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            "inherit_context": {
                "type": "boolean",
                "description": (
                    "Default true: seed every task's first message with your "
                    "current session progress / task / LTM context. Set false "
                    "only when you want deliberately blank-slate tasks."
                ),
            },
            "iter_budget": {
                "type": "integer",
                "description": (
                    "Max iterations per task's own loop. Defaults to YOUR OWN "
                    "current iteration budget — set this only to explicitly "
                    "shorten quick tasks."
                ),
            },
            "max_concurrency": {
                "type": "integer",
                "description": f"Max tasks running at once. Default {_DEFAULT_CONCURRENCY}, clamped to [{_MIN_CONCURRENCY}, {_MAX_CONCURRENCY}].",
            },
        }
