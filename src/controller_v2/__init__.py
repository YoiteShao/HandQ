"""
controller_v2 — Persistent Agent + TaskChannel architecture.

This package implements the v2 orchestration layer:
  - TaskChannel: Coordinator↔Agent IPC channel + sync primitive
  - Orchestrator: the Coordinator — INTENT triage + mechanical queueing +
                  completion relay (no item-splitting, no LLM acceptance
                  grading, no second planning LLM call)
  - PersistentAgent: long-lived agent loop driven by TaskChannel items
  - FlowControllerV2: thin session orchestrator
"""
from .task_channel import TaskChannel, TaskSpec, TaskResult, INTERRUPTED_BY_COORDINATOR
from .orchestrator import Orchestrator
from .persistent_agent import PersistentAgent
from .flow_controller import FlowControllerV2

__all__ = [
    "TaskChannel",
    "TaskSpec",
    "TaskResult",
    "INTERRUPTED_BY_COORDINATOR",
    "Orchestrator",
    "PersistentAgent",
    "FlowControllerV2",
]
