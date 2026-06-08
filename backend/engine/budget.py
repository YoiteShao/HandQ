"""Budget manager — bound a workflow's cost and degrade gracefully (report §8.6).

The report's hard rule: a workflow that hits a limit must **degrade to a partial
report**, never crash. So this object never raises into the runner; it answers a
cooperative ``check()`` between nodes and exposes a per-node timeout the runner
wraps each node in. When a limit trips, the runner returns the Blackboard built
so far (findings/summaries intact) as a partial result.

Time is read through an injectable ``now`` clock so the wall-clock limits are
testable without sleeping.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..config import BudgetConfig


@dataclass(frozen=True)
class BudgetStatus:
    exceeded: bool
    reason: str = ""


class BudgetManager:
    """Tracks agent invocations + wall-clock for one workflow run.

    Construct one per ``handle_goal`` (the clock starts at construction). Limits
    of 0 (counts) or non-positive / non-finite (timeouts) are treated as
    disabled.
    """

    def __init__(self, config: BudgetConfig, *, now: Callable[[], float] = time.monotonic) -> None:
        self._cfg = config
        self._now = now
        self._start = now()
        self._agent_invocations = 0

    def note_agent_invocation(self) -> None:
        self._agent_invocations += 1

    @property
    def agent_invocations(self) -> int:
        return self._agent_invocations

    def elapsed(self) -> float:
        return self._now() - self._start

    @property
    def per_node_timeout_s(self) -> float:
        return self._cfg.per_node_timeout_s

    def check(self) -> BudgetStatus:
        """Cooperative between-node check. Returns exceeded=True with a reason
        when a limit is hit; the runner converts that into a partial report."""
        cap = self._cfg.max_agent_invocations
        if cap and self._agent_invocations >= cap:
            return BudgetStatus(True, f"agent invocations {self._agent_invocations} >= cap {cap}")
        budget_s = self._cfg.total_timeout_s
        if budget_s and budget_s > 0:
            e = self.elapsed()
            if e >= budget_s:
                return BudgetStatus(True, f"elapsed {e:.0f}s >= total timeout {budget_s:.0f}s")
        return BudgetStatus(False)
