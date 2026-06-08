"""Native v2 executor — bridge ``ExecutorProtocol`` → native ``Subagent``.

The production executor for AgentNode bodies in the v2 backbone. Each node
invocation hands its sub-goal, scope slice, and allowed-tool list to a
bounded ``Subagent`` loop running on the v2 contracts (``Message`` /
``ToolCall`` / ``ToolSpec``). No ``src/`` import: the LLM client and the
tool registry are *injected* so tests stay deterministic and the production
stack can plug whichever model client it needs.

The seam is intentionally narrow — this executor does NOT know about
templates, the runner, the Blackboard, or convergence. It only knows: take a
sub-goal + scope + tool subset → run one self-planning loop → map the result
back to ``AgentRunOutput`` so the existing graph runner sees the contract it
always has.

When the subagent returns a structured (JSON) output that carries a
``findings`` key, the executor lifts those into ``AgentRunOutput.structured_findings``
so the workflow runner's existing fan-in (Blackboard.findings → convergence
→ verdict) sees them. That's the seam the report's audit pattern (§3.5)
relies on — subagents emit typed findings; the workflow merges them.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..engine.executor import AgentRunOutput
from ..engine.findings import Finding
from .contracts import ToolSpec
from .llm import LLMClient
from .subagent import DrainAmendments, InterruptCheck, Subagent, SubagentResult, SubagentSpec


class SubagentExecutor:
    """``ExecutorProtocol`` impl backed by the native v2 ``Subagent``.

    Construct once with an LLM client and the full tool registry the
    subagents are allowed to draw from; each AgentNode then runs against a
    scoped subset (the names declared in the node's ``tools`` list).
    Unknown tool names are silently dropped — a missing tool surfaces as a
    model error the next time the LLM tries to call it, rather than as an
    exception that crashes the whole run.

    The control hooks are the seam to the ``ControlChannel``: ``check_interrupt``
    (the channel's ``check_stop``) lets a hard stop break each subagent loop, and
    ``drain_amendments`` (the channel's ``drain_amendments``) feeds mid-node
    follow-up instructions into the running loop. A frontend builds the executor
    around its channel; tests leave both None.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: dict[str, ToolSpec],
        max_steps: int = 12,
        check_interrupt: Optional[InterruptCheck] = None,
        drain_amendments: Optional[DrainAmendments] = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._max_steps = max_steps
        self._check_interrupt = check_interrupt
        self._drain_amendments = drain_amendments

    async def run(
        self,
        *,
        sub_goal: str,
        tools: list[str],
        context: dict[str, Any],
        working_dir: Optional[str],
    ) -> AgentRunOutput:
        scoped = [self._tools[name] for name in tools if name in self._tools]
        scope = dict(context)
        if working_dir:
            # Surface the cwd inside the scope dict so the subagent system
            # prompt picks it up alongside any Blackboard-slice keys.
            scope.setdefault("working_dir", working_dir)
        spec = SubagentSpec(
            goal=sub_goal,
            scope=scope,
            tools=scoped,
            max_steps=self._max_steps,
        )
        result: SubagentResult = await Subagent(
            self._llm,
            check_interrupt=self._check_interrupt,
            drain_amendments=self._drain_amendments,
        ).run(spec)
        return AgentRunOutput(
            ok=result.ok,
            summary=_summarize(result),
            error=result.error,
            tokens=result.tokens,
            structured_findings=_extract_findings(result),
        )


def _summarize(result: SubagentResult) -> str:
    """Coerce the subagent output to a free-text summary the runner can show.

    The Blackboard's ``state`` and ``history`` carry summary text per node;
    that's the value that lands in the receptionist progress view. A
    structured (JSON) output gets stringified once here so it can ride the
    same channel without forcing the runner to special-case dicts."""
    out = result.output
    if isinstance(out, str):
        return out
    if out is None:
        return result.error or ""
    return str(out)


def _extract_findings(result: SubagentResult) -> list[Finding]:
    """Lift ``output["findings"]`` (when present) into typed ``Finding`` objects.

    Subagents emit findings either as a parsed dict (when an ``output_schema``
    was set on the SubagentSpec, ``output`` is already a dict) or as a JSON
    string (the schema-less convergence path — subagents in MVP-shape audit
    flows return JSON text directly). We accept both: try to interpret a
    string as JSON; otherwise treat it as plain text and return no findings.

    Encoding this in the executor (rather than per-template glue) means *any*
    subagent that emits a ``findings`` array gets fed into the runner's
    fan-in pipeline automatically. Anything else (string output without
    findings, missing required fields per item) silently produces no
    findings — the run still completes, the model just didn't surface any.
    """
    payload = _coerce_to_dict(result.output)
    if payload is None:
        return []
    raw = payload.get("findings")
    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(Finding(
                category=str(item.get("category", "")),
                summary=str(item.get("summary", "")),
                severity=str(item.get("severity", "info")),
                location=str(item.get("location", "")),
                evidence=str(item.get("evidence", "")),
                recommendation=str(item.get("recommendation", "")),
                source=str(item.get("source", "")),
                confidence=float(item.get("confidence", 1.0)),
            ))
        except (TypeError, ValueError):
            # A malformed entry doesn't poison the rest of the list — drop
            # just that one and keep going. The model will see no error
            # for its other findings, which is what we want.
            continue
    return findings


def _coerce_to_dict(out: Any) -> Optional[dict[str, Any]]:
    """Normalize a subagent output to a dict, or None if it isn't shaped like one."""
    if isinstance(out, dict):
        return out
    if isinstance(out, str):
        text = out.strip()
        if not text or text[0] != "{":
            return None
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None
    return None


