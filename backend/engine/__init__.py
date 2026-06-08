"""Engine layer: shared state + the single-loop executor primitives."""
from .blackboard import Blackboard
from .executor import (
    AgentRunOutput,
    ExecutorProtocol,
    StubExecutor,
)

__all__ = [
    "Blackboard",
    "AgentRunOutput",
    "ExecutorProtocol",
    "StubExecutor",
]
