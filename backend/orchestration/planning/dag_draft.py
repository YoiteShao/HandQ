"""Restricted JSON draft for workflows (report §8.1 / §11.2; PROGRESS §1).

The full-system ambition is *dynamic workflow synthesis*: the model produces a
task-specific DAG instead of us hand-authoring every template. The report is
explicit about how to do this **safely** — never execute model-generated code.
Instead the model (or the detector) emits a constrained JSON spec, and we
deserialize it into the existing ``workflow.py`` primitives. The draft is that spec.

Safety by construction:
  * An ``agent`` node is fully data-described (sub_goal / tools / context_keys)
    and instantiated as an ``AgentNode`` bound to the caller's executor.
  * Every other node type references a **registered builder by name** with plain
    params — a whitelist. ``build`` only ever calls pre-vetted Python factories;
    it cannot call an arbitrary symbol, eval a string, or import anything.
  * ``from_dict``/``build`` validate the graph (entry exists, edges resolve to a
    declared node or END, node types are known, compound-node children
    resolve to declared nodes) before anything runs.

This is the natural landing format for both dynamic synthesis and the detector's
template promotion (a ``Candidate`` becomes a stored ``DAGDraft``, not new .py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ...engine.executor import ExecutorProtocol
from ...engine.findings import severity_rank
from ..execution.validators import convergence_node, self_critique_gate, verdict_gate
from .workflow import (
    END,
    AgentNode,
    ForEachNode,
    GateNode,
    Node,
    ParallelGroup,
    RetryNode,
    Workflow,
)

NODE_AGENT = "agent"
NODE_PARALLEL_GROUP = "parallel_group"
NODE_PREDICATE_GATE = "predicate_gate"
NODE_FOREACH = "foreach"
NODE_RETRY = "retry"

# A builder turns ``(name, params, nodes_so_far)`` into a non-agent Node.
# ``nodes_so_far`` is the partially-built node dict — leaf builders ignore
# it; compound builders (``parallel_group``) use it to resolve child names.
# The registry below is the whitelist — only these names are constructible.
Builder = Callable[[str, dict[str, Any], dict[str, "Node"]], "Node"]

# Compound node types depend on other nodes already being built — they are
# constructed in a second pass after every leaf node is in the registry.
_COMPOUND_TYPES: frozenset[str] = frozenset({NODE_PARALLEL_GROUP, NODE_FOREACH, NODE_RETRY})


def _build_verdict_gate(name: str, params: dict[str, Any], _nodes: dict[str, Node]) -> Node:
    return verdict_gate(
        name=name,
        attempt_key=params["attempt_key"],
        max_attempts=int(params.get("max_attempts", 1)),
        pass_label=params.get("pass_label", "ok"),
        repair_label=params.get("repair_label", "repair"),
        give_up_label=params.get("give_up_label", "give_up"),
    )


def _build_convergence(name: str, params: dict[str, Any], _nodes: dict[str, Node]) -> Node:
    return convergence_node(name=name)


def _build_self_critique(
    name: str, params: dict[str, Any], _nodes: dict[str, Node],
) -> Node:
    """Build the terminal self-critique gate (default deterministic critic).

    The draft layer can't safely accept a custom critic callable from the model,
    so this builder always uses the default Blackboard-inspecting critic
    (findings-or-history → ok). Templates that want a richer critic
    (e.g. an LLM-backed CRITIC validator) should wire it directly in code,
    not via the draft.
    """
    return self_critique_gate(name=name)


# Join strategy whitelist — the names a parallel_group's ``join`` parameter
# may reference. New strategies land here, never as raw Python in the draft.
def _join_all_ok(results, _bb) -> str:
    return "ok" if all(r.ok for r in results) else "fail"


def _join_any_ok(results, _bb) -> str:
    return "ok" if any(r.ok for r in results) else "fail"


def _join_majority_ok(results, _bb) -> str:
    if not results:
        return "fail"
    oks = sum(1 for r in results if r.ok)
    return "ok" if oks * 2 >= len(results) else "fail"


JOIN_STRATEGIES: dict[str, Callable[[list, Any], str]] = {
    "all_ok": _join_all_ok,
    "any_ok": _join_any_ok,
    "majority_ok": _join_majority_ok,
}


def _build_parallel_group(
    name: str, params: dict[str, Any], nodes: dict[str, Node],
) -> Node:
    """Construct a ParallelGroup that fans out to already-built children.

    ``params['children']`` is a list of names declared elsewhere in the same
    draft; the draft validator already ensured each resolves. The ``join``
    parameter names a strategy in ``JOIN_STRATEGIES`` — anything outside the
    whitelist is rejected, so a planner can't smuggle arbitrary Python via
    a join string.
    """
    child_names = list(params.get("children") or ())
    children = [nodes[c] for c in child_names]
    join_name = params.get("join", "all_ok")
    if join_name not in JOIN_STRATEGIES:
        raise DAGDraftError(
            f"parallel_group {name!r}: unknown join strategy {join_name!r}; "
            f"allowed: {sorted(JOIN_STRATEGIES)}"
        )
    return ParallelGroup(name=name, children=children, join=JOIN_STRATEGIES[join_name])


# ── predicate gate (conditional branch) ──────────────────────────────────────
# Whitelist of predicate factories. A planner-generated draft can choose ONE
# predicate by name; arbitrary Python (eval / lambda strings) is forbidden.
# Each factory takes the draft ``params`` dict, returns a predicate function
# bound to those params. The predicate returns a *label* the workflow runner
# uses to pick the next edge — same contract as a hand-authored GateNode.


def _pred_findings_present(_params: dict[str, Any]):
    """`yes` if any finding has been produced this run, else `no`."""
    def _p(bb) -> str:
        return "yes" if bb.findings else "no"
    return _p


def _pred_severity_at_least(params: dict[str, Any]):
    """`yes` if any converged finding meets ``min_severity``; else `no`.

    Reads the ranked list ``convergence_node`` writes under
    ``converged_findings``; falls back to the raw ``bb.findings`` list when
    convergence hasn't run yet (the predicate stays useful even mid-walk).
    """
    floor = severity_rank(str(params.get("min_severity", "high")))

    def _p(bb) -> str:
        ranked = bb.state.get("converged_findings") or list(bb.findings)
        return "yes" if any(severity_rank(f.severity) >= floor for f in ranked) else "no"

    return _p


def _pred_key_present(params: dict[str, Any]):
    """`yes` if ``params['key']`` exists in ``bb.state``."""
    key = str(params["key"])

    def _p(bb) -> str:
        return "yes" if key in bb.state else "no"

    return _p


def _pred_key_equals(params: dict[str, Any]):
    """`yes` if ``bb.state[key] == value`` (string-compared)."""
    key = str(params["key"])
    value = params["value"]

    def _p(bb) -> str:
        return "yes" if str(bb.state.get(key)) == str(value) else "no"

    return _p


PREDICATES: dict[str, Any] = {
    "findings_present": _pred_findings_present,
    "severity_at_least": _pred_severity_at_least,
    "key_present": _pred_key_present,
    "key_equals": _pred_key_equals,
}


def _build_predicate_gate(
    name: str, params: dict[str, Any], _nodes: dict[str, Node],
) -> Node:
    """Build a GateNode that routes on a *whitelisted* predicate.

    ``params['predicate']`` selects the predicate by name; the rest of
    ``params`` is passed to the factory as predicate-specific arguments.
    Anything outside the whitelist is rejected — a planner can never invent
    a new predicate by emitting a string.
    """
    pred_name = params.get("predicate")
    if pred_name not in PREDICATES:
        raise DAGDraftError(
            f"predicate_gate {name!r}: unknown predicate {pred_name!r}; "
            f"allowed: {sorted(PREDICATES)}"
        )
    factory = PREDICATES[pred_name]
    try:
        predicate = factory(params)
    except KeyError as exc:
        raise DAGDraftError(
            f"predicate_gate {name!r}: predicate {pred_name!r} missing required "
            f"param {exc.args[0]!r}"
        ) from exc
    return GateNode(name=name, predicate=predicate)


def _build_foreach(
    name: str, params: dict[str, Any], nodes: dict[str, Node],
) -> Node:
    """Build a ForEachNode bound to an already-declared inner node.

    ``params['inner']`` names another node in the same draft; the validator
    ensures it exists. ``params['iter_key']`` is the Blackboard key holding
    the list to iterate over (default ``"items"``). ``params['max_items']``
    bounds the loop so a malformed input can't fan out unboundedly.
    """
    inner_name = params.get("inner")
    if not inner_name:
        raise DAGDraftError(f"foreach {name!r}: needs 'inner' param")
    if inner_name not in nodes:
        raise DAGDraftError(
            f"foreach {name!r}: inner {inner_name!r} is not a declared node"
        )
    return ForEachNode(
        name=name,
        inner=nodes[inner_name],
        iter_key=str(params.get("iter_key", "items")),
        item_scope_key=str(params.get("item_scope_key", "item")),
        max_items=int(params.get("max_items", 32)),
    )


def _build_retry(
    name: str, params: dict[str, Any], nodes: dict[str, Node],
) -> Node:
    """Build a RetryNode wrapping an already-declared inner node.

    Phase-level retry: if the inner node fails (transient flake), retry up
    to ``max_attempts`` times before giving up. Distinct from
    ``verdict_gate`` (which routes via graph edges) — retry is automatic
    and stays inside one node's execution.
    """
    inner_name = params.get("inner")
    if not inner_name:
        raise DAGDraftError(f"retry {name!r}: needs 'inner' param")
    if inner_name not in nodes:
        raise DAGDraftError(
            f"retry {name!r}: inner {inner_name!r} is not a declared node"
        )
    max_attempts = int(params.get("max_attempts", 3))
    if max_attempts < 1:
        raise DAGDraftError(f"retry {name!r}: max_attempts must be >= 1, got {max_attempts}")
    return RetryNode(name=name, inner=nodes[inner_name], max_attempts=max_attempts)


# Default whitelist. Extend deliberately; every entry is a reviewed factory.
DEFAULT_BUILDERS: dict[str, Builder] = {
    "verdict_gate": _build_verdict_gate,
    "convergence": _build_convergence,
    "self_critique": _build_self_critique,
    NODE_PARALLEL_GROUP: _build_parallel_group,
    NODE_PREDICATE_GATE: _build_predicate_gate,
    NODE_FOREACH: _build_foreach,
    NODE_RETRY: _build_retry,
}


class DAGDraftError(ValueError):
    """Raised when a draft document is malformed or references unknown pieces."""


@dataclass
class NodeDraft:
    name: str
    type: str = NODE_AGENT
    sub_goal: str = ""               # agent nodes only
    tools: list[str] = field(default_factory=list)         # agent nodes only
    context_keys: list[str] = field(default_factory=list)  # agent nodes only
    params: dict[str, Any] = field(default_factory=dict)   # builder nodes only
    children: list[str] = field(default_factory=list)      # parallel_group only

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.type == NODE_AGENT:
            d["sub_goal"] = self.sub_goal
            if self.tools:
                d["tools"] = list(self.tools)
            if self.context_keys:
                d["context_keys"] = list(self.context_keys)
        else:
            if self.params:
                d["params"] = dict(self.params)
            if self.children:
                d["children"] = list(self.children)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NodeDraft":
        if "name" not in d:
            raise DAGDraftError("node missing 'name'")
        return cls(
            name=d["name"],
            type=d.get("type", NODE_AGENT),
            sub_goal=d.get("sub_goal", ""),
            tools=list(d.get("tools", [])),
            context_keys=list(d.get("context_keys", [])),
            params=dict(d.get("params", {})),
            children=list(d.get("children", [])),
        )


@dataclass
class DAGDraft:
    entry: str
    nodes: list[NodeDraft]
    edges: dict[str, dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": self.edges,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DAGDraft":
        if not isinstance(d, dict):
            raise DAGDraftError("draft document must be an object")
        for key in ("entry", "nodes", "edges"):
            if key not in d:
                raise DAGDraftError(f"draft missing required key {key!r}")
        nodes = [NodeDraft.from_dict(n) for n in d["nodes"]]
        draft = cls(entry=d["entry"], nodes=nodes, edges=dict(d["edges"]))
        draft.validate(builders=DEFAULT_BUILDERS)
        return draft

    def validate(self, *, builders: dict[str, Builder]) -> None:
        """Structural checks before any node is constructed or run."""
        names = [n.name for n in self.nodes]
        if len(names) != len(set(names)):
            raise DAGDraftError("duplicate node names")
        nameset = set(names)
        if not self.nodes:
            raise DAGDraftError("workflow has no nodes")
        if self.entry not in nameset:
            raise DAGDraftError(f"entry {self.entry!r} is not a declared node")
        for n in self.nodes:
            if n.type == NODE_AGENT:
                if not n.sub_goal:
                    raise DAGDraftError(f"agent node {n.name!r} needs a sub_goal")
                if n.children:
                    raise DAGDraftError(f"agent node {n.name!r} cannot have children")
            elif n.type not in builders:
                raise DAGDraftError(
                    f"node {n.name!r} has unknown type {n.type!r}; "
                    f"allowed: {[NODE_AGENT, *sorted(builders)]}"
                )
            elif n.type == NODE_PARALLEL_GROUP:
                # Compound nodes must reference declared children, but those
                # children are *internal* to the group and shouldn't be the
                # graph's entry (they're not run by the top-level walk).
                if not n.children:
                    raise DAGDraftError(f"parallel_group {n.name!r} needs at least one child")
                for child in n.children:
                    if child not in nameset:
                        raise DAGDraftError(
                            f"parallel_group {n.name!r} references unknown child {child!r}"
                        )
                    if child == self.entry:
                        raise DAGDraftError(
                            f"parallel_group {n.name!r} cannot include the entry node {child!r}"
                        )
                # ``join`` is validated at build-time by _build_parallel_group
                # against JOIN_STRATEGIES; checking here would duplicate that.
            elif n.type == NODE_FOREACH:
                # foreach references a single inner node by name. Same rule as
                # parallel_group: the inner is internal to the loop and must
                # not also be the entry of the top-level walk.
                inner = n.params.get("inner")
                if not inner:
                    raise DAGDraftError(f"foreach {n.name!r} needs 'inner' param")
                if inner not in nameset:
                    raise DAGDraftError(
                        f"foreach {n.name!r} references unknown inner {inner!r}"
                    )
                if inner == self.entry:
                    raise DAGDraftError(
                        f"foreach {n.name!r} cannot use the entry node {inner!r} as inner"
                    )
            elif n.type == NODE_RETRY:
                # retry wraps a single inner node — same internal-only rule.
                inner = n.params.get("inner")
                if not inner:
                    raise DAGDraftError(f"retry {n.name!r} needs 'inner' param")
                if inner not in nameset:
                    raise DAGDraftError(
                        f"retry {n.name!r} references unknown inner {inner!r}"
                    )
                if inner == self.entry:
                    raise DAGDraftError(
                        f"retry {n.name!r} cannot use the entry node {inner!r} as inner"
                    )
        for src, table in self.edges.items():
            if src not in nameset:
                raise DAGDraftError(f"edge source {src!r} is not a declared node")
            for label, dst in table.items():
                if dst != END and dst not in nameset:
                    raise DAGDraftError(f"edge {src!r}--{label}-->{dst!r} targets unknown node")


def to_draft(wf: Workflow) -> DAGDraft:
    """Best-effort serialization of a live Workflow.

    AgentNodes round-trip fully. Non-agent nodes built from closures (a live
    GateNode/FuncNode) cannot be reversed to params, so this is intended for
    inspection/diffing of agent-only graphs; the authoritative direction is
    ``build`` (spec → runnable)."""
    nodes: list[NodeDraft] = []
    for name, node in wf.nodes.items():
        if isinstance(node, AgentNode):
            nodes.append(NodeDraft(name=name, type=NODE_AGENT, sub_goal=node.sub_goal,
                                tools=list(node.tools), context_keys=list(node.context_keys)))
        else:
            nodes.append(NodeDraft(name=name, type=type(node).__name__))
    return DAGDraft(entry=wf.entry, nodes=nodes, edges=dict(wf.edges))


def build(
    draft: DAGDraft,
    *,
    executor: ExecutorProtocol,
    builders: dict[str, Builder] | None = None,
) -> Workflow:
    """Deserialize a draft spec into a runnable Workflow. Validates first.

    Two passes so compound nodes (parallel_group) can resolve their children:
    first pass builds every leaf type (agent, verdict_gate, convergence, …),
    second pass builds compound types using the leaf registry.
    """
    builders = builders or DEFAULT_BUILDERS
    draft.validate(builders=builders)
    nodes: dict[str, Node] = {}
    # Pass 1: leaves only.
    for n in draft.nodes:
        if n.type == NODE_AGENT:
            nodes[n.name] = AgentNode(
                name=n.name, sub_goal=n.sub_goal, executor=executor,
                tools=list(n.tools), context_keys=list(n.context_keys),
            )
        elif n.type not in _COMPOUND_TYPES:
            nodes[n.name] = builders[n.type](n.name, n.params, nodes)
    # Pass 2: compounds, now able to look up their already-built children.
    for n in draft.nodes:
        if n.type in _COMPOUND_TYPES:
            params = dict(n.params)
            params.setdefault("children", n.children)
            nodes[n.name] = builders[n.type](n.name, params, nodes)
    return Workflow(nodes=nodes, edges=dict(draft.edges), entry=draft.entry)


def linear_draft_from_phases(phases: list[str], goal: str) -> DAGDraft:
    """Turn an ordered list of phase labels into a linear agent-node draft.

    This is the detector's promotion target: a recurring pattern's ``skeleton``
    (e.g. modify's locate→understand→change→verify) becomes a runnable multi-node
    template *as data*, with each downstream node scoped to read the prior phase
    — no hand-authored ``templates/*.py`` required. Tools stay unset so the agent
    self-selects; a later refinement can attach per-phase tool scopes."""
    if not phases:
        raise DAGDraftError("cannot promote an empty skeleton")
    if len(phases) != len(set(phases)):
        raise DAGDraftError("skeleton phases must be unique to serve as node names")
    nodes: list[NodeDraft] = []
    edges: dict[str, dict[str, str]] = {}
    for i, phase in enumerate(phases):
        prior = [phases[i - 1]] if i > 0 else []
        nodes.append(NodeDraft(
            name=phase, type=NODE_AGENT,
            sub_goal=f"{phase.capitalize()} — in service of the goal: {goal}",
            context_keys=prior,
        ))
        nxt = phases[i + 1] if i + 1 < len(phases) else END
        edges[phase] = {"*": nxt}
    return DAGDraft(entry=phases[0], nodes=nodes, edges=edges)
