"""Workflow primitives — a DAG of nodes executed by deterministic code.

A Workflow is the MACRO layer (docs/ARCHITECTURE.md §2/§3.2). It coordinates
single-loop AgentNodes; all branching is code, never a per-step LLM call.

Node types
----------
  AgentNode      — runs ONE scoped RuntimeAgent loop (the only LLM-bearing node)
  FuncNode       — deterministic Python, no LLM
  GateNode       — routing decision in code (returns an edge label)
  ParallelGroup  — fan-out via asyncio.gather, fan-in via a join function

Edges map ``node_name -> {outcome_label: next_node_name}``. The reserved
target ``"END"`` terminates the walk. Conventional labels: ``"ok"`` / ``"fail"``
plus any custom label a GateNode returns.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from ...engine.blackboard import Blackboard
from ...engine.executor import ExecutorProtocol
from ...engine.findings import Finding

END = "END"


@dataclass
class NodeResult:
    """What a node returns. ``route`` overrides the default ok/fail label."""
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)       # merged into bb.state
    artifacts: dict[str, Any] = field(default_factory=dict)  # merged into bb.artifacts
    route: Optional[str] = None
    error: Optional[str] = None
    summary: str = ""
    tokens: int = 0  # LLM tokens consumed by this node (0 for non-LLM nodes)
    # Typed findings emitted by this node — the runner folds them into bb.findings
    # for later merge→dedup→rank. Empty for nodes that produce no audit findings.
    findings: list[Finding] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.route or ("ok" if self.ok else "fail")


@runtime_checkable
class Node(Protocol):
    name: str
    async def run(self, bb: Blackboard) -> NodeResult: ...


@dataclass
class AgentNode:
    """Runs one scoped single-loop agent. The only node type that calls a model."""
    name: str
    sub_goal: str
    executor: ExecutorProtocol
    tools: list[str] = field(default_factory=list)
    context_keys: list[str] = field(default_factory=list)

    async def run(self, bb: Blackboard) -> NodeResult:
        out = await self.executor.run(
            sub_goal=self.sub_goal,
            tools=self.tools,
            context=bb.pick(self.context_keys),
            working_dir=bb.working_dir,
        )
        return NodeResult(
            ok=out.ok,
            data={self.name: out.summary, **{f"{self.name}.{k}": v
                                             for k, v in out.findings.items()}},
            artifacts=out.artifacts,
            error=out.error,
            summary=out.summary,
            tokens=out.tokens,
            findings=list(out.structured_findings),
        )


@dataclass
class FuncNode:
    """Deterministic Python step — no LLM. ``fn`` may be sync or async."""
    name: str
    fn: Callable[[Blackboard], Any]

    async def run(self, bb: Blackboard) -> NodeResult:
        try:
            res = self.fn(bb)
            if asyncio.iscoroutine(res):
                res = await res
        except Exception as exc:  # deterministic code failing is a real failure
            return NodeResult(ok=False, error=str(exc), summary=f"{self.name} raised {exc!r}")
        data = res if isinstance(res, dict) else ({self.name: res} if res is not None else {})
        return NodeResult(ok=True, data=data, summary=f"{self.name} ok")


@dataclass
class GateNode:
    """Routing decision in code. ``predicate(bb)`` returns the next edge label."""
    name: str
    predicate: Callable[[Blackboard], str]

    async def run(self, bb: Blackboard) -> NodeResult:
        label = self.predicate(bb)
        return NodeResult(ok=True, route=label, summary=f"{self.name} -> {label}")


@dataclass
class ParallelGroup:
    """Fan-out children concurrently, then fan-in with ``join``.

    ``join(results, bb)`` returns an edge label (e.g. "ok" if all succeeded).
    Children run against the SAME blackboard; their data is merged after join.
    """
    name: str
    children: list[Node]
    join: Callable[[list[NodeResult], Blackboard], str]

    async def run(self, bb: Blackboard) -> NodeResult:
        results = await asyncio.gather(*(c.run(bb) for c in self.children))
        merged: dict[str, Any] = {}
        artifacts: dict[str, Any] = {}
        findings: list[Finding] = []
        for r in results:
            merged.update(r.data)
            artifacts.update(r.artifacts)
            findings.extend(r.findings)
        label = self.join(list(results), bb)
        ok = label != "fail"
        return NodeResult(ok=ok, data=merged, artifacts=artifacts, route=label,
                          findings=findings,
                          summary=f"{self.name}: {len(results)} children -> {label}")


@dataclass
class ForEachNode:
    """Iterate over ``bb.state[iter_key]`` and run ``inner`` once per item.

    The ``map`` half of the report's map-reduce primitive (§8.3). Each
    iteration stashes the current item under ``bb.state[item_scope_key]`` so
    an inner AgentNode (or any inner Node) can read it via ``context_keys``.
    After the loop the per-item key is cleared, so the post-foreach state
    only carries what the inner node merged via its own data dict.

    Bounded by ``max_items`` to keep a malformed ``iter_key`` (or a planner
    over-eager fan-out) from exploding the run. ``ok`` is True only when
    *every* item completed successfully — otherwise the route is "fail" so
    a downstream gate can react. Findings from each iteration accumulate
    into the parent ``Blackboard`` via the standard runner fold.
    """

    name: str
    inner: Node
    iter_key: str
    item_scope_key: str = "item"
    max_items: int = 32

    async def run(self, bb: Blackboard) -> NodeResult:
        items = bb.state.get(self.iter_key)
        if items is None:
            items = []
        if not isinstance(items, list):
            return NodeResult(
                ok=False,
                error=f"iter_key {self.iter_key!r} is not a list "
                      f"(got {type(items).__name__})",
                summary=f"{self.name}: invalid iter_key",
            )
        if len(items) > self.max_items:
            return NodeResult(
                ok=False,
                error=f"too many items ({len(items)} > max {self.max_items})",
                summary=f"{self.name}: max_items exceeded",
            )

        merged: dict[str, Any] = {}
        artifacts: dict[str, Any] = {}
        findings: list[Finding] = []
        all_ok = True

        prior = bb.state.get(self.item_scope_key)  # preserve any prior value
        try:
            for item in items:
                bb.state[self.item_scope_key] = item
                res = await self.inner.run(bb)
                merged.update(res.data)
                artifacts.update(res.artifacts)
                findings.extend(res.findings)
                if not res.ok:
                    all_ok = False
        finally:
            # Restore (or clear) the item slot so the post-loop state isn't
            # accidentally carrying the last item into downstream nodes.
            if prior is None:
                bb.state.pop(self.item_scope_key, None)
            else:
                bb.state[self.item_scope_key] = prior

        label = "ok" if all_ok else "fail"
        return NodeResult(
            ok=all_ok, data=merged, artifacts=artifacts, route=label,
            findings=findings,
            summary=f"{self.name}: {len(items)} item(s) -> {label}",
        )


@dataclass
class RetryNode:
    """Re-run ``inner`` up to ``max_attempts`` until it succeeds.

    Distinct from ``verdict_gate`` — that routes failures via the graph to a
    repair node; ``RetryNode`` is *automatic* retry of the same inner node,
    useful when a phase is known to be flaky (LLM hallucination, transient
    network blip) and a clean re-try usually fixes it.

    The result is annotated with ``{name}.attempt`` (the 1-based winning
    attempt) on success or ``{name}.attempts`` (total attempts taken) on
    final failure, so a downstream report can see whether this was a
    first-try or retry win without inspecting the runner's history.
    """

    name: str
    inner: Node
    max_attempts: int = 3

    async def run(self, bb: Blackboard) -> NodeResult:
        if self.max_attempts < 1:
            return NodeResult(
                ok=False, error="max_attempts must be >= 1",
                summary=f"{self.name}: invalid max_attempts",
            )
        last: Optional[NodeResult] = None
        for attempt in range(1, self.max_attempts + 1):
            result = await self.inner.run(bb)
            last = result
            if result.ok:
                meta = {f"{self.name}.attempt": attempt}
                return NodeResult(
                    ok=True,
                    data={**result.data, **meta},
                    artifacts=dict(result.artifacts),
                    route=result.route,
                    error=result.error,
                    summary=f"{self.name}: ok on attempt {attempt}",
                    tokens=result.tokens,
                    findings=list(result.findings),
                )
        # All attempts exhausted — surface the last failure with retry context.
        meta = {f"{self.name}.attempts": self.max_attempts}
        return NodeResult(
            ok=False,
            data={**(last.data if last else {}), **meta},
            artifacts=dict(last.artifacts if last else {}),
            route="fail",
            error=(last.error if last else "no inner result"),
            summary=f"{self.name}: failed after {self.max_attempts} attempt(s)",
            tokens=(last.tokens if last else 0),
            findings=list(last.findings if last else ()),
        )


@dataclass
class Workflow:
    nodes: dict[str, Node]
    edges: dict[str, dict[str, str]]
    entry: str

    def next_of(self, node_name: str, label: str) -> str:
        table = self.edges.get(node_name, {})
        # Fall through: explicit label -> "*" wildcard -> END.
        return table.get(label) or table.get("*") or END

    def to_ascii(self) -> str:
        """Render the DAG as a readable ASCII outline for debugging.

        Lists each node with its concrete type, marks the entry, and shows
        outgoing edges (label → target). Nothing is executed; this is just
        a static dump of the topology so you can eyeball what the planner
        / template / promoted draft actually built.

            entry → discover
              [AgentNode]    discover     --*--> scan
              [AgentNode]    audit_a      --*--> END
              [AgentNode]    audit_b      --*--> END
              [ParallelGroup] scan        --*--> merge
              [FuncNode]     merge        --*--> END
        """
        out: list[str] = [f"entry → {self.entry}"]
        # Stable ordering: entry first, then declaration order from nodes dict.
        ordered = ([self.entry] if self.entry in self.nodes else []) + [
            n for n in self.nodes if n != self.entry
        ]
        type_w = max((len(type(self.nodes[n]).__name__) for n in ordered), default=0)
        name_w = max((len(n) for n in ordered), default=0)
        for name in ordered:
            node = self.nodes[name]
            ntype = type(node).__name__
            edges = self.edges.get(name, {})
            if not edges:
                out.append(f"  [{ntype:<{type_w}}] {name:<{name_w}}  (no outgoing edges)")
                continue
            first = True
            for label, dst in edges.items():
                prefix = (f"  [{ntype:<{type_w}}] {name:<{name_w}}"
                          if first else
                          f"  {' ' * (type_w + 2)}  {' ' * name_w}")
                out.append(f"{prefix}  --{label}--> {dst}")
                first = False
        return "\n".join(out)
