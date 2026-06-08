"""Single-loop executor primitives — protocol + stub.

The orchestration runner expects every ``AgentNode`` body to satisfy
``ExecutorProtocol``. Two implementations live in the v2 backbone:

  * ``StubExecutor`` (this module)        — deterministic no-LLM stand-in for
    workflow tests; success unless the sub-goal contains a fail-token.
  * ``backend.agent.SubagentExecutor``    — the production executor backed by
    the native v2 ``Subagent`` loop and an injected ``LLMClient``.

``AgentRunOutput`` is the wire shape every executor returns and the runner
folds into ``NodeResult`` / Blackboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

if TYPE_CHECKING:
    from .findings import Finding


@dataclass
class AgentRunOutput:
    """Normalized result of running one scoped agent loop."""
    ok: bool
    summary: str = ""
    findings: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    # Cumulative LLM tokens for this agent run (input+output); 0 for the stub /
    # non-LLM paths. Surfaced so the observability trace can attribute cost.
    tokens: int = 0
    # Typed, aggregatable findings (engine/findings.py) — the convergence-bearing
    # output. Distinct from the free-form ``findings`` dict (which lands in
    # bb.state); these flow into bb.findings for merge→dedup→rank. Empty for the
    # stub / non-auditing paths.
    structured_findings: list["Finding"] = field(default_factory=list)


class ExecutorProtocol(Protocol):
    async def run(
        self,
        *,
        sub_goal: str,
        tools: list[str],
        context: dict[str, Any],
        working_dir: Optional[str],
    ) -> AgentRunOutput:
        ...


class StubExecutor:
    """No-LLM executor for exercising the workflow graph in tests.

    Returns a deterministic success so Workflow/Runner/Router logic can be
    validated end-to-end without touching a model or the filesystem.

    ``drain_amendments`` is the stub's stand-in for the real subagent's
    mid-node amendment hook: the production ``Subagent`` loop drains follow-up
    notes between steps, but the stub has no loop, so it drains once per node
    run and reflects the notes in its summary + ``findings["amendments"]``.
    That lets the deterministic frontend prove an amendment was absorbed by a
    running goal without aborting it.
    """

    def __init__(
        self,
        *,
        fail_nodes: Optional[set[str]] = None,
        drain_amendments: Optional[Callable[[], list[str]]] = None,
    ) -> None:
        self._fail_nodes = fail_nodes or set()
        self._drain_amendments = drain_amendments

    async def run(self, *, sub_goal, tools, context, working_dir) -> AgentRunOutput:
        amendments = self._drain_amendments() if self._drain_amendments else []
        # A node whose sub_goal contains a marked fail-token returns failure,
        # so gate/repair edges can be tested.
        if any(tok in sub_goal for tok in self._fail_nodes):
            return AgentRunOutput(ok=False, error="stub forced failure",
                                  summary=f"[stub] FAILED: {sub_goal[:60]}")
        summary = f"[stub] done: {sub_goal[:60]}"
        if amendments:
            summary += f" (+{len(amendments)} amendment(s): {'; '.join(amendments)})"
        return AgentRunOutput(
            ok=True,
            summary=summary,
            findings={"stub_context_seen": list(context.keys()),
                      "amendments": amendments},
        )
