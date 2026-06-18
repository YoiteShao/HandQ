"""
controller_v2 — Persistent Agent + CheckList architecture.

This package implements the v2 orchestration layer:
  - SharedCheckList: thread-safe shared state between Agent and Planner
  - Orchestrator: CheckList-driven planning + receptionist + planning intelligence
  - PersistentAgent: long-lived agent loop driven by CheckList items
  - FlowControllerV2: thin session orchestrator
  - PlannerMixin: loop detection, epistemic inventory, structural guards,
                  goal-level acceptance synthesis (B1 verification gate)
"""
from .shared_checklist import SharedCheckList, CheckListItem, ItemResult, INTERRUPTED_BY_PLANNER
from .orchestrator import Orchestrator
from .persistent_agent import PersistentAgent
from .flow_controller import FlowControllerV2
from .planner_mixin import AcceptanceVerdict

__all__ = [
    "SharedCheckList",
    "CheckListItem",
    "ItemResult",
    "INTERRUPTED_BY_PLANNER",
    "Orchestrator",
    "PersistentAgent",
    "FlowControllerV2",
    "AcceptanceVerdict",
]
