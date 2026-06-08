"""Native v2 subagent runtime — bounded, scope-aware, schema-validating loop.

This is the **Meso** layer per the report's Macro/Meso/Micro split (§10): the
single self-planning loop each ``AgentNode`` runs. v2 owns its own loop here
instead of reusing ``src.agent.runtime_agent.RuntimeAgent`` — that was v1
thinking around ``Step`` / ``Plan`` / a ``Memory`` object the executor
threaded around. The report's framing makes those concepts unnecessary:

    Each subagent invocation gets a *global goal slice*, a *local scope*,
    a *bounded tool set*, and a *required output schema*. It loops
    Think → Dispatch → Observe with a step budget; converges when the
    model returns no tool_calls; validates the final output against the
    schema.   (report §8.4 / §11.1)

The loop is intentionally minimal — orchestration logic (decomposition,
fan-out, convergence, validation) lives in the workflow runner above this.
A subagent does ONE local job, returns a structured result, and dies.

Failure modes turn into structured ``SubagentResult.ok=False`` outcomes (step
budget exhausted / tool call to an unknown tool / output schema violation /
LLM call failure) — never raised exceptions, so the runner above can route
on the result instead of stack-unwinding.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .contracts import Message, MessageRole, ToolCall, ToolResult, ToolSpec
from .llm import LLMClient


@dataclass
class SubagentSpec:
    """Everything one subagent invocation needs.

    Defaults match the read-only audit shape so an MVP fan-out (§9.1) can be
    constructed without spelling every knob.
    """

    goal: str                                       # the local sub-goal text
    scope: dict[str, Any] = field(default_factory=dict)  # e.g. allowed files / dimensions
    tools: list[ToolSpec] = field(default_factory=list)  # the bounded tool set
    output_schema: Optional[dict[str, Any]] = None  # JSON-schema-ish; minimal validate
    forbidden: list[str] = field(default_factory=list)   # explicit "do not do" lines
    max_steps: int = 12                             # hard upper bound on loop iters
    system_prompt: Optional[str] = None             # override the default system block


@dataclass
class SubagentResult:
    """Outcome of one subagent run.

    ``ok=True`` ⇒ ``output`` is the converged structured answer (already
    schema-validated when a schema was provided).
    ``ok=False`` ⇒ ``error`` explains why; ``messages`` carries the
    conversation buffer at the point of failure so the runner above can
    inspect / log / replay.
    """

    ok: bool
    output: Any = None
    error: Optional[str] = None
    messages: list[Message] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0


# Optional interrupt callback: runner above polls between turns. Returning a
# non-None message breaks the loop and fails the run with that message.
InterruptCheck = Callable[[], Optional[str]]
# Optional amendment drain: runner above polls between turns. Returns any
# follow-up notes the user injected mid-run; each is folded into the buffer as a
# USER turn and the loop continues (additive — never fails the run).
DrainAmendments = Callable[[], list[str]]
# Optional async pre-dispatch hook: runner can take a checkpoint snapshot
# (engine.checkpoint) for mutating tools before they run. Receives the call.
BeforeDispatch = Callable[[ToolCall, ToolSpec], Awaitable[None]]


class Subagent:
    """One scope-bounded self-planning loop.

    Owns nothing across runs — instantiate once with an LLMClient, call
    ``run(spec)`` per task. Concurrency-safe tool calls within a turn are
    fanned out via ``asyncio.gather`` automatically (the ToolSpec carries
    the safety flag); unsafe calls run sequentially.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        check_interrupt: Optional[InterruptCheck] = None,
        drain_amendments: Optional[DrainAmendments] = None,
        before_dispatch: Optional[BeforeDispatch] = None,
    ) -> None:
        self._llm = llm
        self._check_interrupt = check_interrupt
        self._drain_amendments = drain_amendments
        self._before_dispatch = before_dispatch

    async def run(self, spec: SubagentSpec) -> SubagentResult:
        messages: list[Message] = self._seed_messages(spec)
        tokens = 0
        for step in range(1, spec.max_steps + 1):
            interrupt = self._check_interrupt() if self._check_interrupt else None
            if interrupt is not None:
                return SubagentResult(
                    ok=False, error=f"interrupted: {interrupt}",
                    messages=messages, steps=step - 1, tokens=tokens,
                )
            # Mid-node amendments: fold any follow-up instructions the user
            # injected since the last turn into the buffer as USER messages, then
            # keep looping. STOP is checked first so a hard stop always wins over
            # a pending amendment. Additive — this never fails the run.
            if self._drain_amendments is not None:
                for note in self._drain_amendments():
                    messages.append(Message(role=MessageRole.USER, content=note))
            try:
                response = await self._llm.complete(messages, tools=spec.tools)
            except Exception as exc:
                return SubagentResult(
                    ok=False, error=f"LLM call failed: {exc!s}",
                    messages=messages, steps=step - 1, tokens=tokens,
                )
            tokens += response.tokens
            assistant = response.message
            if assistant.role is not MessageRole.ASSISTANT:
                return SubagentResult(
                    ok=False, error=f"LLM returned non-assistant role {assistant.role}",
                    messages=messages, steps=step, tokens=tokens,
                )
            messages.append(assistant)

            # Convergence: no tool_calls means the model is done.
            if not assistant.tool_calls:
                output, err = self._finalize(assistant.content, spec)
                return SubagentResult(
                    ok=err is None, output=output, error=err,
                    messages=messages, steps=step, tokens=tokens,
                )

            tool_results = await self._dispatch(assistant.tool_calls, spec)
            for tr in tool_results:
                messages.append(Message(
                    role=MessageRole.TOOL,
                    content=_stringify(tr.output if tr.ok else tr.error),
                    tool_call_id=tr.call_id,
                ))

        return SubagentResult(
            ok=False, error=f"step budget exhausted ({spec.max_steps})",
            messages=messages, steps=spec.max_steps, tokens=tokens,
        )

    # ── internal ──────────────────────────────────────────────────────────

    def _seed_messages(self, spec: SubagentSpec) -> list[Message]:
        """Initial buffer: SYSTEM with the scope contract, USER with the goal.

        Mirrors the report §8.4 prompt shape — global goal, local scope, tool
        list, forbidden actions, output schema. Kept as a single SYSTEM
        message so the prefix stays cache-friendly across turns within one run.
        """
        sys = spec.system_prompt or _default_system_prompt(spec)
        return [
            Message(role=MessageRole.SYSTEM, content=sys),
            Message(role=MessageRole.USER, content=spec.goal),
        ]

    async def _dispatch(
        self,
        calls: list[ToolCall],
        spec: SubagentSpec,
    ) -> list[ToolResult]:
        """Run a turn's tool calls, parallelizing when the tool opts in.

        A call to a tool not on the allowed list comes back as a synthetic
        failed ToolResult (so the model sees the error and can try again),
        rather than crashing the run.
        """
        by_name = {t.name: t for t in spec.tools}

        async def run_one(call: ToolCall) -> ToolResult:
            tool = by_name.get(call.name)
            if tool is None:
                return ToolResult(
                    call_id=call.call_id, ok=False,
                    error=f"tool {call.name!r} not in this subagent's allowed set "
                          f"({sorted(by_name)})",
                )
            if self._before_dispatch is not None:
                try:
                    await self._before_dispatch(call, tool)
                except Exception as exc:  # pragma: no cover - defensive
                    return ToolResult(call_id=call.call_id, ok=False,
                                      error=f"pre-dispatch hook failed: {exc!s}")
            try:
                raw = await tool.run(**call.arguments)
            except Exception as exc:
                return ToolResult(
                    call_id=call.call_id, ok=False,
                    error=f"{call.name} raised {type(exc).__name__}: {exc!s}",
                )
            # The tool doesn't know which assistant ToolCall.call_id it is
            # answering — only the dispatcher does. We copy the outcome
            # fields and stamp the correct call_id so the next TOOL message
            # pairs with its assistant call.
            return ToolResult(
                call_id=call.call_id,
                ok=raw.ok,
                output=raw.output,
                error=raw.error,
                metadata=dict(raw.metadata),
            )

        # Concurrency safety: a turn fans out parallel calls only if EVERY
        # call is safe; one unsafe call serializes the whole turn. This is
        # the conservative read of the spec — a parallel-safe read mixed
        # with an unsafe write is run sequentially in declared order.
        all_safe = all((by_name.get(c.name) and by_name[c.name].concurrency_safe)
                       for c in calls)
        if all_safe and len(calls) > 1:
            return list(await asyncio.gather(*(run_one(c) for c in calls)))
        results: list[ToolResult] = []
        for c in calls:
            results.append(await run_one(c))
        return results

    def _finalize(
        self, content: str, spec: SubagentSpec
    ) -> tuple[Any, Optional[str]]:
        """Validate the converged output against the optional schema.

        No schema ⇒ output is the raw text. Schema present ⇒ parse JSON, then
        check required keys. We deliberately keep the validator small (no
        jsonschema dependency); richer validation can be plugged in later
        without changing the contract.
        """
        if spec.output_schema is None:
            return content, None
        try:
            doc = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, f"output is not JSON: {exc.msg}"
        required = list(spec.output_schema.get("required") or ())
        missing = [r for r in required if r not in doc]
        if missing:
            return None, f"output missing required keys: {missing}"
        return doc, None


def _default_system_prompt(spec: SubagentSpec) -> str:
    """Render the §8.4 system prompt from a SubagentSpec.

    Stable wording — small changes to this string move the cache prefix and
    cost a fresh prompt cache every run, so every line earns its keep.
    """
    parts: list[str] = [
        "You are a subagent in a larger workflow.",
        "Stay strictly inside the local scope and tool set. Output your final",
        "answer when (and only when) you have gathered enough evidence.",
    ]
    if spec.scope:
        parts.append("\nLocal scope:")
        for k, v in sorted(spec.scope.items()):
            parts.append(f"  - {k}: {v}")
    if spec.tools:
        parts.append("\nAllowed tools (call by name):")
        for t in spec.tools:
            line = f"  - {t.name}: {t.description}"
            if t.usage_guide:
                line += f" — {t.usage_guide}"
            parts.append(line)
    if spec.forbidden:
        parts.append("\nDo NOT:")
        for f in spec.forbidden:
            parts.append(f"  - {f}")
    if spec.output_schema is not None:
        parts.append("\nFinal answer JSON schema (return JSON only):")
        parts.append(json.dumps(spec.output_schema, ensure_ascii=False, indent=2))
    return "\n".join(parts)


def _stringify(value: Any) -> str:
    """Coerce any tool output to a string the LLM can read.

    Strings pass through; everything else is JSON-stringified with a fallback
    to ``repr`` for non-JSON-able values. The agent loop never feeds the model
    a raw Python object — that's the kind of thing that crashes a stream a
    thousand turns deep into a long-running run.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)
