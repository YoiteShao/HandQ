"""Compatibility stub — preserves the old ``audit.build()`` / ``modify.build()`` API.

Tests call ``audit.build(goal=..., executor=..., scanners=...)`` which the JSON
loader can't support (extra kwargs). This stub provides the full original Python
factories unchanged. The production Coordinator uses ``TemplateLoader`` directly
and never touches this module.
"""
from __future__ import annotations

from types import SimpleNamespace

from ..execution.validators import (
    ADVERSARIAL, CRITIC, REVIEWER, convergence_node, validator_node, verdict_gate,
)
from ..planning.workflow import END, AgentNode, GateNode, NodeResult, ParallelGroup, Workflow
from ...engine.blackboard import Blackboard
from ...engine.executor import ExecutorProtocol
from ...engine.findings import severity_rank


# ── audit template ────────────────────────────────────────────────────────────

DEFAULT_SCANNERS = (REVIEWER, ADVERSARIAL, CRITIC)


def _scan_join(results: list[NodeResult], bb: Blackboard) -> str:
    return "ok" if any(r.ok for r in results) else "fail"


def _verdict_gate(name: str, *, block_severity: str) -> GateNode:
    floor = severity_rank(block_severity)

    def predicate(bb: Blackboard) -> str:
        ranked = bb.state.get("converged_findings", [])
        blocking = any(severity_rank(f.severity) >= floor for f in ranked)
        return "findings" if blocking else "clean"

    return GateNode(name=name, predicate=predicate)


def _audit_build(*, goal, executor, scanners=DEFAULT_SCANNERS, block_severity="high"):
    scanner_nodes = [
        validator_node(role, name=f"scan_{role}", goal=goal, executor=executor)
        for role in scanners
    ]
    if not scanner_nodes:
        raise ValueError("audit needs at least one scanner role")
    scan = ParallelGroup(name="scan", children=scanner_nodes, join=_scan_join)
    converge = convergence_node(name="converge")
    verdict = _verdict_gate("verdict", block_severity=block_severity)
    nodes = {scan.name: scan, converge.name: converge, verdict.name: verdict}
    edges = {
        "scan": {"ok": "converge", "fail": "converge"},
        "converge": {"*": "verdict"},
        "verdict": {"clean": END, "findings": END},
    }
    return Workflow(nodes=nodes, edges=edges, entry="scan")


# ── modify template ──────────────────────────────────────────────────────────

def _modify_build(*, goal, executor, max_repair_attempts=1):
    locate = AgentNode(
        name="locate",
        sub_goal=f"Locate the code relevant to: {goal}. Report the files and symbols involved.",
        executor=executor, tools=["glob", "grep", "read"],
    )
    understand = AgentNode(
        name="understand",
        sub_goal=f"Understand how the located code works, in service of: {goal}.",
        executor=executor, tools=["read", "grep"], context_keys=["locate"],
    )
    change = AgentNode(
        name="change",
        sub_goal=f"Make the change: {goal}. Edit only what is necessary.",
        executor=executor, tools=["read", "edit", "write"],
        context_keys=["locate", "understand"],
    )
    verify = AgentNode(
        name="verify",
        sub_goal=f"Verify the change satisfies: {goal}. Run tests or checks if available.",
        executor=executor, tools=["shell", "read", "grep"], context_keys=["change"],
    )
    gate = verdict_gate(
        name="verify_gate",
        attempt_key="modify.repair_attempts",
        max_attempts=max_repair_attempts,
    )
    nodes = {n.name: n for n in (locate, understand, change, verify, gate)}
    edges = {
        "locate": {"ok": "understand", "fail": "understand"},
        "understand": {"ok": "change", "fail": "change"},
        "change": {"*": "verify"},
        "verify": {"*": "verify_gate"},
        "verify_gate": {"ok": END, "repair": "change", "give_up": END},
    }
    return Workflow(nodes=nodes, edges=edges, entry="locate")


# Expose as module-like objects matching old ``from backend.orchestration.templates import audit``
audit = SimpleNamespace(build=_audit_build, DEFAULT_SCANNERS=DEFAULT_SCANNERS)
modify = SimpleNamespace(build=_modify_build)

__all__ = ["audit", "modify"]
