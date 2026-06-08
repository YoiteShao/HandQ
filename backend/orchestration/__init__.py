"""Orchestration layer — the MACRO granularity (ARCHITECTURE.md §2/§3).

Deterministic code over single loops: a Workflow DAG walked by the runner, a
router that picks a pattern (or fails safe to single-loop), and a detector that
grows templates from observed recurrence.

Sub-packages:
  routing/    — goal classification (Router, patterns, exemplars, detector)
  planning/   — workflow construction (Workflow, DAGDraft, Planner, templates)
  execution/  — workflow running (Runner, validators)
"""
from ..engine.blackboard import Blackboard
from .planning.workflow import (
    END,
    AgentNode,
    FuncNode,
    GateNode,
    Node,
    NodeResult,
    ParallelGroup,
    Workflow,
)
from .planning.dag_draft import DAGDraft, DAGDraftError
from .planning.planner import WorkflowPlanner, PlanResult
from .execution.runner import RunReport, WorkflowRunner
from .execution.validators import (
    verdict_gate, convergence_node, self_critique_gate, validator_node,
)
from .routing.router import RouteDecision, Router
from .routing.detector import Candidate, TemplateDetector, Trace
from .routing import patterns

__all__ = [
    "Blackboard",
    "END",
    "AgentNode",
    "FuncNode",
    "GateNode",
    "Node",
    "NodeResult",
    "ParallelGroup",
    "Workflow",
    "DAGDraft",
    "DAGDraftError",
    "WorkflowPlanner",
    "PlanResult",
    "RunReport",
    "WorkflowRunner",
    "verdict_gate",
    "convergence_node",
    "self_critique_gate",
    "validator_node",
    "RouteDecision",
    "Router",
    "Candidate",
    "TemplateDetector",
    "Trace",
    "patterns",
]
