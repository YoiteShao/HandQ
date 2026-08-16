"""
spawn_agent — fork the agent itself into a bounded, concurrent-safe sub-task.

The main agent owns one long-lived context. Open-ended exploration ("scan the
codebase for every place X is configured", "figure out how the auth flow works")
or independent parallel work (check N hosts, review N files) can flood that
context with dozens of file reads and grep dumps the agent will never need
again, or simply can't run at once inside one turn's serial tool calls.
`spawn_agent` runs the SAME agent — same behavioral prompt, same conversation
context, same iteration patience — in an ISOLATED sub-loop with its own
message list; only a text summary comes back. The parent's context stays clean.

Design principle: a sub-agent IS the main agent, not a smaller one.
  - Capability is defined ENTIRELY by which tools are in its list — never by
    prompt wording, description narrowing, or a runtime "you can't do that"
    refusal. See ``_SUBAGENT_TOOLS`` below: every tool on it is safe for
    concurrent use (shared, lockable, or side-effect-free); every tool NOT on
    it is excluded because its underlying resource cannot be safely shared
    across concurrent identities (a browser/desktop display, a persistent
    live_shell session, the session's own task-channel/todo panel) — not
    because a sub-agent is "not trusted" with it. The main agent already runs
    multiple tool calls in one turn concurrently (see agent_prompts.py's
    "Parallelize independent work"); those single-instance tools simply stay
    with it rather than needing a second, weaker permission tier.
  - Same system prompt as the main agent, rendered with the sub-agent's own
    tool set — see agent_prompts._generate_system_prompt(available_tool_names).
    Sections describing a capability the sub-agent's tool list doesn't
    include (e.g. claim_tool) are omitted, never left in as dead text.
  - Same context: the parent's compacted history / current-task block / LTM
    hints are seeded into the sub-agent's first message, and its iteration
    budget defaults to the parent's own — a sub-agent is exactly as patient
    as the agent spawning it, not an arbitrarily-shortened stand-in.
  - Shares the parent's ctx (so tools reuse the session's file_state, write
    locks, ssh locks, etc.) and the parent's interrupt_event, checked once per
    loop iteration (and inside shell's/ssh's own blocking waits) so cancelling
    the parent cancels the child between LLM turns too, not only while a tool
    happens to be mid-flight.
  - Returns the sub-agent's final text as the tool_result — that summary is
    the only thing that enters the parent's context.

The sub-loop itself (``run_subagent_task``) is shared with ``fan_out_tool.py``,
which dispatches many of these concurrently instead of just one — same tool
list, same prompt/context inheritance, just fanned out. ``spawn_agent`` is the
N=1 convenience entry point into the same mechanism.
"""
import json
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .base_tool import BaseTool, ToolResult

if TYPE_CHECKING:
    from ..controller_v2.session_context import SessionContext


# The one fixed tool list every spawn_agent/fan_out_agents sub-task runs
# with. Not tiered by "trust level" — every entry here is safe for CONCURRENT
# use across the parent and any number of siblings, either because it's
# read-only, or because its shared resource is covered by a per-key lock
# (SessionContext.write_lock_for / ssh_lock_for).
#
# Excluded, and why (never "sub-agent is weaker" — always "this resource
# can't be safely shared across concurrent identities" or "this IS the
# session/main-agent's own single-instance state"):
#   browser_*, desktop_*     — physical single-instance display; concurrent
#                              control of one screen/browser is meaningless,
#                              not just unsafe. Stays with the main agent,
#                              which can already dispatch several tool calls
#                              in one turn when it genuinely needs to run
#                              more than one thing "at once".
#   live_shell_*             — persistent per-shell-id session state; two
#                              identities touching the same shell_id would
#                              corrupt each other's session, and there is no
#                              per-shell_id lock primitive (unlike files/ssh
#                              hosts) that would make sharing safe.
#   remote_handq             — remote-machine-wide heavy state, same bucket
#                              as live_shell_*.
#   todo_write               — writes to the session's single shared
#                              ctx.agent_todo (rendered as one UI panel); a
#                              sub-agent writing to it would stomp the
#                              parent's own plan, not maintain a separate one.
#   ask_human                — deliberately excluded from this allowlist:
#                              even though ask_human is now always-registered
#                              (on_demand=False), a sub-task has no
#                              InteractionManager/UI delegate of its own to
#                              route a question through, and blocking a
#                              sub-agent's bounded loop on a human reply
#                              would stall the parent's iter_budget for
#                              nothing the sub-agent's caller asked for.
#   schedule_wakeup          — re-queues a brand-new TaskSpec onto the
#                              session's shared _task_channel (the same
#                              channel the PARENT polls via
#                              wait_for_current_item()) — a session-level
#                              mutation, not a self-scoped wait. A sub-agent
#                              waiting mid-task should use wait_interval,
#                              which blocks in place with no shared side
#                              effect, instead.
#   spawn_agent/fan_out_agents — recursion is prevented structurally by
#                              simply never including these in this list.
#   claim_tool/release_tool  — would let a sub-agent expand its OWN tool list
#                              at runtime, which is precisely the "capability
#                              is decided by whoever spawned you" invariant
#                              this list exists to enforce.
_SUBAGENT_TOOLS: tuple = (
    "read", "grep", "glob", "read_skill",
    "write", "edit", "notebook_edit",
    "shell", "ssh",
    "wait_interval",
)

# Absolute ceiling on iterations regardless of what the parent's own budget
# is — a runaway-loop backstop, not the normal operating limit. The normal
# limit is inherited from the parent (see run_subagent_task's iter_budget
# param) so a sub-agent is exactly as patient as the agent spawning it.
_MAX_SUBAGENT_ITERS_CEILING = 60
_DEFAULT_SUBAGENT_ITERS = 12  # fallback only when no parent budget is supplied


async def run_subagent_task(
    prompt: str,
    *,
    services: list,
    tool_instances: Dict[str, BaseTool],
    ctx: Optional["SessionContext"],
    iter_budget: Optional[int] = None,
    inherited_context_block: Optional[str] = None,
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

    budget = min(int(iter_budget or _DEFAULT_SUBAGENT_ITERS), _MAX_SUBAGENT_ITERS_CEILING)

    try:
        summary, iters = await _run_exploration(
            question, services=services,
            tool_instances=tool_instances, ctx=ctx,
            iter_budget=budget, inherited_context_block=inherited_context_block,
        )
    except Exception as exc:
        return {"ok": False, "error": f"sub-agent failed: {exc}"}

    return {"ok": True, "summary": summary, "iterations": iters}


async def _run_exploration(
    question: str,
    *,
    services: list,
    tool_instances: Dict[str, BaseTool],
    ctx: Optional["SessionContext"],
    iter_budget: int,
    inherited_context_block: Optional[str],
) -> tuple:
    """Bounded loop with an isolated message list, scoped to ``_SUBAGENT_TOOLS``.

    Returns (summary_text, iterations_used). Falls back to whatever text the
    model last produced if it hits the iteration cap without a clean answer.
    """
    from ..infrastructure.llm_pool import call_with_fallback
    from ..tools.tool_registry import ToolRegistry
    from ..controller_v2.agent_prompts import _generate_system_prompt

    allowed = _SUBAGENT_TOOLS
    # extra_tool_names is required here: ssh is registered on_demand=True (it
    # only enters the MAIN agent's schema after claim_tool), so without this
    # generate_tools_for_api()'s default on_demand-exclusion would silently
    # drop ssh from a sub-agent's tool schema even though it's in
    # _SUBAGENT_TOOLS — a claim_tool-shaped gate the sub-agent has no
    # claim_tool to open.
    all_tools = ToolRegistry.generate_tools_for_api(extra_tool_names=list(allowed))
    tools = [
        t for t in all_tools
        if t.get("function", {}).get("name") in allowed
    ]
    system_prompt = _generate_system_prompt(set(allowed))

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if inherited_context_block:
        messages.append({"role": "user", "content": inherited_context_block})
    messages.append({"role": "user", "content": f"[Sub-task]\n{question}"})

    last_text = ""
    for i in range(iter_budget):
        # Checked once per iteration (not just inside shell's/ssh's own
        # subprocess wait) so a coordinator/session-teardown interrupt breaks
        # the loop between LLM turns too, not only while a tool happens to be
        # mid-flight.
        if ctx is not None and ctx.interrupt_event.is_set():
            return (last_text or "(cancelled: session interrupted)"), i

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
        return (final.content or last_text or "(no findings)"), iter_budget
    except Exception:
        return (last_text or "(no findings; budget exhausted)"), iter_budget


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
    # is_read_only by construction). write/edit/notebook_edit get the
    # equivalent working-dir scoping below. None of these paths has an
    # InteractionManager to request confirmation from, so unlike the main
    # agent loop (persistent_agent._check_before_act) there is no "ask the
    # user" fallback — an out-of-scope call fails closed instead.
    if tool_name in ("shell", "ssh") and ctx is not None:
        try:
            from ..controller_v2.risk_check import is_high_risk
            wd = ctx.working_directory or ctx.storage_directory
            if is_high_risk(tool_name, args, wd, ctx.config_manager):
                return json.dumps(
                    {
                        "ok": False,
                        "err": (
                            f"Refused: this {tool_name} command is high-risk and a "
                            "sub-agent cannot request user confirmation. Report "
                            "this back to the parent agent instead of running it."
                        ),
                    },
                    ensure_ascii=False,
                )
        except Exception:
            pass
    # write/edit key their target path as "path"; notebook_edit uses
    # "notebook_path" — normalize once so the working-dir check and the lock
    # acquisition below share one code path instead of three near-duplicates.
    _PATH_KEY = {"write": "path", "edit": "path", "notebook_edit": "notebook_path"}
    if tool_name in _PATH_KEY and ctx is not None:
        try:
            from ..controller_v2.risk_check import is_path_within_working_dir
            wd = ctx.working_directory or ctx.storage_directory
            file_path = args.get(_PATH_KEY[tool_name], "")
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
        target_path = args.get(_PATH_KEY[tool_name]) if tool_name in _PATH_KEY else None
        if target_path and ctx is not None:
            # Same path-level lock the parent agent uses (SessionContext.
            # write_lock_for) — serializes this sub-task's write against ANY
            # other concurrent writer to the same path in this session: the
            # parent agent's own tool calls, or a sibling sub-task in the
            # same fan_out_agents batch. Different paths never contend.
            async with ctx.write_lock_for(target_path):
                tr = await tool.execute(**args)
        else:
            tr = await tool.execute(**args)
    except Exception as exc:
        return json.dumps({"ok": False, "err": str(exc)}, ensure_ascii=False)
    return tr.to_tool_result_json()


class SpawnAgentTool(BaseTool):
    """Fork the agent itself into one bounded sub-task; return its text summary."""

    is_read_only = False  # its fixed tool list includes write/edit/notebook_edit
    is_concurrency_safe = True

    def __init__(self, ctx: Optional["SessionContext"] = None):
        super().__init__("spawn_agent", ctx=ctx)
        # Injected by PersistentAgent after construction (the sub-agent reuses
        # the parent's LLM pool and tool instances). When unset, the tool
        # reports a clean error rather than guessing.
        self._services: Optional[list] = None
        self._tool_instances: Optional[Dict[str, BaseTool]] = None
        # Parent's live iteration budget + context-block PROVIDERS — callables,
        # not frozen values, because _max_item_iterations / _conversation_
        # summary / _current_item_block all change over the parent's session
        # lifetime (new item, compaction). Calling these at spawn-time (not
        # bind-time) means every sub-agent sees what the parent knows RIGHT
        # NOW, not a stale snapshot from whenever bind_context happened to run.
        self._parent_iter_budget_fn: Optional[Any] = None
        self._parent_context_block_fn: Optional[Any] = None

    def bind_runtime(self, services: list, tool_instances: Dict[str, BaseTool]) -> None:
        """Wire the parent's LLM services + tool instances into the sub-agent.

        Called once by PersistentAgent so the sub-loop can reuse the same
        credentials-bearing tool objects and LLM fallback chain without
        re-instantiating them.
        """
        self._services = services
        self._tool_instances = tool_instances

    def bind_context(self, iter_budget_fn, context_block_fn) -> None:
        """Wire providers for the parent's live iteration budget + inheritable
        context block. Called once by PersistentAgent right after bind_runtime.

        Both args are zero-arg callables invoked fresh on every execute() call
        (not read once at bind-time) — see persistent_agent._current_iter_
        budget() / _render_subagent_context_block().
        """
        self._parent_iter_budget_fn = iter_budget_fn
        self._parent_context_block_fn = context_block_fn

    async def execute(
        self,
        prompt: str = "",
        inherit_context: bool = True,
        iter_budget: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.time()
        params = {"prompt": prompt, "inherit_context": inherit_context, "iter_budget": iter_budget}
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
            question,
            services=self._services, tool_instances=self._tool_instances,
            ctx=self.ctx,
            iter_budget=iter_budget or (self._parent_iter_budget_fn() if self._parent_iter_budget_fn else None),
            inherited_context_block=(
                self._parent_context_block_fn() if (inherit_context and self._parent_context_block_fn) else None
            ),
        )
        if not result.get("ok"):
            return self._err(result.get("error", "sub-agent failed."), params, start)

        return ToolResult(
            success=True,
            output={
                "summary": result["summary"],
                "iterations": result["iterations"],
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
                    "The task/question for the sub-agent to work on (e.g. "
                    "'find every place the retry count is configured and report "
                    "file:line', or 'rename all occurrences of X to Y across the "
                    "module'). The sub-agent is a fork of YOU — same behavioral "
                    "prompt, same conversation context by default — running in an "
                    "isolated message list so its intermediate tool calls don't "
                    "flood your own context; only its final text summary comes "
                    "back."
                ),
            },
            "inherit_context": {
                "type": "boolean",
                "description": (
                    "Default true: seed the sub-agent's first message with your "
                    "current session progress / task / LTM context, so it starts "
                    "already knowing what you know. Set false only when you want "
                    "a deliberately blank-slate investigation."
                ),
            },
            "iter_budget": {
                "type": "integer",
                "description": (
                    "Max iterations for the sub-agent's own loop. Defaults to "
                    "YOUR OWN current iteration budget (a sub-agent is exactly "
                    "as patient as you are) — set this only to explicitly "
                    "shorten a quick sub-task."
                ),
            },
        }
