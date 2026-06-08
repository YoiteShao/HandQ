"""WorkflowRunner — deterministic graph walk.

Replaces ``FlowController._planner_loop``. The runner contains NO LLM call:
it walks the DAG, evaluates edge labels in code, and threads one Blackboard
through every node. Re-planning happens only when a node fails AND the graph
routes the ``fail`` label to a repair node — not on every step.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Callable, Optional

from ...engine.blackboard import Blackboard
from ...engine.budget import BudgetManager
from ...engine.observability import RunTrace, TraceRecorder
from ..planning.workflow import END, AgentNode, NodeResult, ParallelGroup, Workflow


@dataclass
class RunReport:
    ok: bool
    steps: int
    last_summary: str
    blackboard: Blackboard
    # True when the walk stopped early on a budget/abort limit but still carries
    # the work done so far — a graceful partial result, not a clean failure.
    partial: bool = False
    # Structured per-node record of the walk (§8.8). Present when an observer was
    # supplied; None otherwise so the lightweight path stays zero-overhead.
    trace: Optional[RunTrace] = None
    # The resolved workflow pattern this run executed ("modify"/"audit"/"freeform"/a
    # promoted pattern id). The runner itself doesn't know it — the Coordinator sets
    # it after building the graph. Informational / observability only.
    pattern: Optional[str] = None


class WorkflowRunner:
    def __init__(
        self,
        *,
        max_steps: int = 100,
        on_node_done: Optional[Callable[[str, NodeResult], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
        budget: Optional[BudgetManager] = None,
        observer: Optional[TraceRecorder] = None,
    ) -> None:
        self._max_steps = max_steps
        self._on_node_done = on_node_done
        # should_abort lets the control channel interrupt the walk between
        # nodes (the interrupt-arbiter role, ARCHITECTURE §4).
        self._should_abort = should_abort or (lambda: False)
        # Optional budget manager: bounds invocations + wall-clock and lets the
        # walk degrade to a partial report instead of running unbounded (§8.6).
        self._budget = budget
        # Optional trace recorder: structured observability (§8.8). When set, the
        # runner emits a per-node event stream and attaches a RunTrace to the report.
        self._observer = observer

    async def run(self, wf: Workflow, bb: Blackboard) -> RunReport:
        cur = wf.entry
        steps = 0
        last: NodeResult = NodeResult(ok=True, summary="(no nodes run)")
        if self._observer is not None:
            self._observer.begin_run(bb.goal)

        def report(ok: bool, last_summary: str, *, partial: bool = False) -> RunReport:
            trace = (self._observer.end_run(ok=ok, steps=steps, partial=partial)
                     if self._observer is not None else None)
            return RunReport(ok=ok, steps=steps, last_summary=last_summary,
                             blackboard=bb, partial=partial, trace=trace)

        while cur != END:
            if self._should_abort():
                return report(False, "aborted by user", partial=True)
            if self._budget is not None:
                status = self._budget.check()
                if status.exceeded:
                    return report(False, f"budget exceeded: {status.reason}", partial=True)
            if steps >= self._max_steps:
                return report(False, "max_steps exceeded", partial=True)
            node = wf.nodes.get(cur)
            if node is None:
                return report(False, f"missing node {cur!r}")

            if self._budget is not None and isinstance(node, (AgentNode, ParallelGroup)):
                self._budget.note_agent_invocation()
            if self._observer is not None:
                self._observer.begin_node()
            last = await self._run_node(node, bb)
            steps += 1
            bb.merge(last.data)
            bb.add_artifacts(last.artifacts)
            bb.add_findings(last.findings)
            bb.record(node.name, last.ok, last.summary)
            if self._observer is not None:
                self._observer.end_node(
                    step=steps, node=node.name, node_type=type(node).__name__,
                    ok=last.ok, route=last.label, summary=last.summary, error=last.error,
                    tokens=last.tokens, artifacts=len(last.artifacts),
                )
            if self._on_node_done:
                self._on_node_done(node.name, last)

            cur = wf.next_of(node.name, last.label)

        return report(last.ok, last.summary)

    async def _run_node(self, node, bb: Blackboard) -> NodeResult:
        """Run a node, enforcing the per-node hard timeout when a budget is set.

        A timeout is a graceful node failure (ok=False) — the graph's fail edge
        handles it (e.g. modify's verify→repair), so a single stuck node never
        hangs the whole walk."""
        timeout = self._budget.per_node_timeout_s if self._budget is not None else 0.0
        if not (timeout and timeout > 0 and math.isfinite(timeout)):
            return await node.run(bb)
        try:
            return await asyncio.wait_for(node.run(bb), timeout=timeout)
        except asyncio.TimeoutError:
            return NodeResult(ok=False, error="node timeout",
                              summary=f"{node.name} exceeded per-node timeout ({timeout:.0f}s)")
