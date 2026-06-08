"""Backend configuration — tunables in one place.

Defaults encode the ARCHITECTURE.md decisions (esp. §7 reactive compaction and
§5 routing thresholds) so behavior is reviewable without spelunking the code.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompactionConfig:
    """§7 reactive-hysteresis policy (replaces v1's timed 80% drop)."""

    trigger_pct: float = 0.60   # compact when context reaches this fraction
    target_pct: float = 0.35    # compact down to this (hysteresis band)
    hard_drop_pct: float = 0.90  # summarizer-failed backstop: half-drop here
    # v1 dropped on a per-iteration timer; v2 is purely size-driven.
    use_iteration_timer: bool = False


@dataclass(frozen=True)
class RouterConfig:
    """§5 routing thresholds."""

    embed_threshold: float = 0.75  # min cosine for a high-confidence pattern match
    enable_classifier: bool = True  # tier-2 cheap LLM classifier
    # Tier-3 fail-safe (single loop) is always on by design — not configurable.


@dataclass(frozen=True)
class RunnerConfig:
    max_steps: int = 100  # graph-walk safety bound (WorkflowRunner)


@dataclass(frozen=True)
class BudgetConfig:
    """§8.6 budget control. Principle: BudgetExceeded → graceful degradation →
    partial report, never a crash. 0 / inf disables a given limit."""

    max_agent_invocations: int = 50      # LLM-bearing node runs per workflow
    total_timeout_s: float = 1800.0      # whole-workflow wall-clock ceiling (30 min)
    per_node_timeout_s: float = 600.0    # single node hard cancel (10 min)
    max_repair_attempts: int = 1         # repair-loop ceiling templates consult


@dataclass(frozen=True)
class BackendConfig:
    compaction: CompactionConfig = CompactionConfig()
    router: RouterConfig = RouterConfig()
    runner: RunnerConfig = RunnerConfig()
    budget: BudgetConfig = BudgetConfig()
    storage_directory: str = ".handq"
