"""
fan_out_agents — dispatch many independent sub-agent tasks concurrently.

Same isolation contract as `spawn_agent` (own message list, no task channel,
no compaction, only a text summary returns) but for N tasks at once instead of
one. Value is NOT execution speed — it's two things the main agent otherwise
can't get on its own:

  1. Independent processing of independent items (e.g. check 20 hosts) without
     dumping 20 items' worth of raw tool output into its own context.
  2. A genuinely independent second (third, fourth...) opinion on the same
     question — spawn N tasks that each look at the same claim from a
     different angle, get back N separate judgments instead of one pass
     re-read by the same context that produced it.

The tool provides only the atomic primitive (isolation + concurrency). How
many tasks to spawn, how to phrase them, and how to weigh/reconcile the
returned summaries is entirely the calling agent's judgment — not encoded
here.
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
    """Run N independent sub-agent tasks concurrently; return N summaries."""

    is_read_only = False  # a "worker" profile task can write/edit
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("fan_out_agents", ctx=ctx)
        self._services: Optional[list] = None
        self._tool_instances: Optional[Dict[str, BaseTool]] = None

    def bind_runtime(self, services: list, tool_instances: Dict[str, BaseTool]) -> None:
        """Wire the parent's LLM services + tool instances (see spawn_agent)."""
        self._services = services
        self._tool_instances = tool_instances

    async def execute(
        self,
        tasks: Optional[List[Dict[str, Any]]] = None,
        tool_profile: str = "explore",
        max_concurrency: int = _DEFAULT_CONCURRENCY,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {"tasks": tasks, "tool_profile": tool_profile, "max_concurrency": max_concurrency}

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

        if tool_profile not in ("explore", "worker"):
            return self._err(
                f"tool_profile must be 'explore' or 'worker' (got {tool_profile!r}).",
                params, start,
            )
        if not self._services or self._tool_instances is None:
            return self._err(
                "fan_out_agents is not wired to an LLM pool in this context.",
                params, start,
            )

        concurrency = max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, int(max_concurrency or _DEFAULT_CONCURRENCY)))
        semaphore = asyncio.Semaphore(concurrency)

        async def _run_one(p: str) -> Dict[str, Any]:
            async with semaphore:
                try:
                    result = await run_subagent_task(
                        p, tool_profile=tool_profile,
                        services=self._services, tool_instances=self._tool_instances,
                        ctx=self.ctx,
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
                    "{\"prompt\": \"...\"}. Each runs in its own isolated context "
                    "(own message history, no shared state between tasks) and "
                    "returns only a text summary — the parent's context only "
                    "sees the summaries, not the intermediate tool calls."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The task/question for this sub-agent, self-contained (it has no memory of this conversation).",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            "tool_profile": {
                "type": "string",
                "enum": ["explore", "worker"],
                "description": (
                    "'explore' (default) — read-only tools (read/grep/glob/shell "
                    "probes). 'worker' — adds write/edit for tasks that must "
                    "produce file changes; a write/edit outside the working "
                    "directory is refused (no way to ask the user mid-task)."
                ),
            },
            "max_concurrency": {
                "type": "integer",
                "description": f"Max tasks running at once. Default {_DEFAULT_CONCURRENCY}, clamped to [{_MIN_CONCURRENCY}, {_MAX_CONCURRENCY}].",
            },
        }
