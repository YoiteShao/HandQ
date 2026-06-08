"""Structured observability — the trace layer behind every workflow run (§8.8).

The report calls observability a first-class concern: a long-running orchestration
that you cannot *see into* is unoperable. v1 had only ad-hoc logging plus the
detector's minimal ``Trace`` (pattern + ok + steps). This module records the thing
you actually need to debug, audit, and drive a progress view from — a structured,
JSON-serializable record of **what the runner did, in order**:

* per node: step index, node name, node *type*, ok flag, the **edge label it
  routed on**, wall-clock latency, a bounded summary, and any error;
* per run: a stable ``run_id``, the goal, start/finish, ok/partial, and the full
  ordered event list — from which the **decision path** (which branch the graph
  actually took) falls out for free.

It is pure and deterministic: no LLM, no IO. Timing comes from an injectable
clock (mirrors ``BudgetManager``) so tests pin latencies with a fake clock. The
runner owns lifecycle calls (``begin_run`` → ``begin_node``/``end_node`` → ``end_run``);
``TraceRecorder`` only accumulates. ``RunTrace.to_dict`` is the persistence/handoff
boundary (a future store or progress UI consumes it; the detector keeps its own
slim ``Trace`` for pattern mining).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, Callable, Optional

# Module-level monotonic counter so run ids are unique + ordered within a process
# without pulling in uuid (deterministic and test-friendly).
_RUN_SEQ = count(1)


@dataclass(frozen=True)
class NodeEvent:
    """One node execution, as observed by the runner."""

    step: int            # 1-based position in the walk
    node: str            # node name
    node_type: str       # AgentNode / FuncNode / GateNode / ParallelGroup / ...
    ok: bool
    route: str           # the edge label this node routed on (ok/fail/repair/...)
    latency_s: float     # wall-clock for this node's run()
    summary: str = ""    # bounded — never the full free-text blob
    error: Optional[str] = None
    tokens: int = 0      # LLM tokens this node consumed (0 for non-LLM nodes)
    artifacts: int = 0   # count of artifacts this node produced

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunTrace:
    """The full structured record of one workflow walk."""

    run_id: str
    goal: str
    events: list[NodeEvent] = field(default_factory=list)
    ok: bool = False
    steps: int = 0
    partial: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def total_tokens(self) -> int:
        return sum(e.tokens for e in self.events)

    @property
    def total_artifacts(self) -> int:
        return sum(e.artifacts for e in self.events)

    def decision_path(self) -> list[str]:
        """The branch the graph actually took, as ``node --route-->`` segments.

        This is the audit answer to 'why did it end up here?' — read straight off
        the ordered events, no graph replay needed."""
        return [f"{e.node} --{e.route}-->" for e in self.events]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable view — the persistence / handoff boundary."""
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "ok": self.ok,
            "partial": self.partial,
            "steps": self.steps,
            "duration_s": self.duration_s,
            "total_tokens": self.total_tokens,
            "total_artifacts": self.total_artifacts,
            "decision_path": self.decision_path(),
            "events": [e.to_dict() for e in self.events],
        }

    def progress_view(self) -> str:
        """Human-readable per-node progress (the receptionist's 'how's it going?')."""
        if not self.events:
            return "(no nodes run)"
        lines = [
            f"{'✓' if e.ok else '✗'} [{e.step}] {e.node} ({e.node_type}) "
            f"-> {e.route} {e.latency_s:.2f}s"
            + (f" {e.tokens}tok" if e.tokens else "")
            for e in self.events
        ]
        tail = (f"=> ok={self.ok} partial={self.partial} steps={self.steps} "
                f"{self.duration_s:.2f}s {self.total_tokens}tok")
        return "\n".join(lines + [tail])


class TraceRecorder:
    """Accumulates ``NodeEvent``s for one run and emits a ``RunTrace``.

    Owns the clock so node latency is measured between ``begin_node`` and
    ``end_node`` — inject ``now`` (e.g. a fake clock) for deterministic tests.
    The runner drives the lifecycle; this object holds no graph knowledge.
    """

    def __init__(self, *, run_id: Optional[str] = None, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self.run_id = run_id or f"run-{next(_RUN_SEQ)}"
        self._events: list[NodeEvent] = []
        self._goal = ""
        self._run_started: Optional[float] = None
        self._node_started: Optional[float] = None

    def begin_run(self, goal: str) -> None:
        self._goal = goal
        self._run_started = self._now()

    def begin_node(self) -> None:
        self._node_started = self._now()

    def end_node(self, *, step: int, node: str, node_type: str, ok: bool,
                 route: str, summary: str = "", error: Optional[str] = None,
                 tokens: int = 0, artifacts: int = 0,
                 summary_cap: int = 200) -> NodeEvent:
        start = self._node_started if self._node_started is not None else self._now()
        event = NodeEvent(
            step=step,
            node=node,
            node_type=node_type,
            ok=ok,
            route=route,
            latency_s=max(0.0, self._now() - start),
            summary=(summary or "")[:summary_cap],
            error=error,
            tokens=tokens,
            artifacts=artifacts,
        )
        self._events.append(event)
        self._node_started = None
        return event

    def end_run(self, *, ok: bool, steps: int, partial: bool) -> RunTrace:
        finished = self._now()
        return RunTrace(
            run_id=self.run_id,
            goal=self._goal,
            events=list(self._events),
            ok=ok,
            steps=steps,
            partial=partial,
            started_at=self._run_started if self._run_started is not None else finished,
            finished_at=finished,
        )


class TraceStore:
    """Persists ``RunTrace``s as one JSON file per run under a directory.

    The handoff/audit boundary (§8.5/§8.8): a finished run's structured record
    is written to ``<root>/<run_id>.json`` so a progress UI, the detector's real
    trace source, or a post-mortem can read it without holding the live objects.
    Pure IO — no LLM. ``list_runs``/``load`` round-trip the dicts back.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def save(self, trace: RunTrace) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{trace.run_id}.json"
        path.write_text(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return path

    def load(self, run_id: str) -> dict[str, Any]:
        return json.loads((self._root / f"{run_id}.json").read_text(encoding="utf-8"))

    def list_runs(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.glob("*.json"))
