"""Template detector — mines execution traces for recurring patterns.

Templates are *grown, not authored*. Cold start = zero templates, everything
single-loop. As the router classifies real goals, their pattern + outcome flow
here. When a pattern recurs enough (and succeeds reliably), the detector
surfaces it as a template candidate — the same ``worthAutomation`` triage
applied to our own orchestration.

This is the seam where the proactivity layer *produces* macro structure. The
detector only proposes; promotion to a usable draft is a separate, deliberate
step (today: human-reviewed via ``Coordinator.promote_ready_candidates``).

**Persistence**: ``to_dict`` / ``from_dict`` round-trip the recorded traces
so a host (typically ``Coordinator``) can save the detector's state across
process restarts. Without persistence, every restart starts from zero traces
and the proactivity loop never closes — a real session would lose the
information that "research-pattern goals have shown up 4 times today" between
runs.

**User-defined patterns**: by default the detector resolves ``candidate.pattern_id``
through ``patterns.get`` (builtin only). Pass a ``pattern_resolver`` callable to
extend that — typically ``ExemplarStore.get_pattern`` so user-defined patterns
are also promotable into runnable draft.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import patterns
from .patterns import FREEFORM, Pattern


@dataclass
class Trace:
    """One observed run, recorded after the runner finishes."""

    pattern_id: str
    goal: str
    ok: bool
    steps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "goal": self.goal,
            "ok": self.ok,
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trace":
        return cls(
            pattern_id=str(d["pattern_id"]),
            goal=str(d.get("goal", "")),
            ok=bool(d["ok"]),
            steps=int(d.get("steps", 0)),
        )


@dataclass
class Candidate:
    """A pattern the detector thinks is worth promoting to a template."""

    pattern_id: str
    occurrences: int
    success_rate: float
    sample_goals: list[str] = field(default_factory=list)


class TemplateDetector:
    """Accumulates traces and reports patterns that clear promotion thresholds.

    Thresholds are deliberately conservative: a template is a commitment, and a
    wrong one is worse than none (the single loop is always correct). ``freeform``
    runs are counted for context but never proposed — by definition they have no
    recognized pattern to freeze.
    """

    def __init__(
        self,
        *,
        min_occurrences: int = 5,
        min_success_rate: float = 0.8,
        pattern_resolver: Optional[Callable[[str], Optional[Pattern]]] = None,
    ) -> None:
        self._min_occurrences = min_occurrences
        self._min_success_rate = min_success_rate
        self._traces: dict[str, list[Trace]] = defaultdict(list)
        # Resolves a pattern_id to a Pattern (builtin OR user overlay). Defaults
        # to the builtin-only ``patterns.get`` so the detector remains useful
        # in test paths that don't wire an ExemplarStore.
        self._pattern_resolver: Callable[[str], Optional[Pattern]] = (
            pattern_resolver or patterns.get
        )

    def record(self, trace: Trace) -> None:
        self._traces[trace.pattern_id].append(trace)

    def candidates(self) -> list[Candidate]:
        out: list[Candidate] = []
        for pattern_id, traces in self._traces.items():
            if pattern_id == FREEFORM or not traces:
                continue
            if len(traces) < self._min_occurrences:
                continue
            rate = sum(1 for t in traces if t.ok) / len(traces)
            if rate < self._min_success_rate:
                continue
            out.append(
                Candidate(
                    pattern_id=pattern_id,
                    occurrences=len(traces),
                    success_rate=rate,
                    sample_goals=[t.goal for t in traces[:3]],
                )
            )
        return sorted(out, key=lambda c: c.occurrences, reverse=True)

    def build_draft(self, pattern_id: str, *, goal: str):
        """Render a pattern's skeleton into a runnable ``DAGDraft``.

        Pure factory — does NOT mutate any state. Called per-goal at run
        time (so the draft carries the current goal text), and once with
        ``goal=""`` from ``promote_ready_candidates`` purely as a sanity
        check that the skeleton is non-empty before committing the pattern
        id to ``Coordinator._promoted``.

        Promotion is *data, not code*: the pattern skeleton becomes a
        linear agent DAG via the restricted draft, deserializable by
        ``dag_draft.build`` into ``workflow.py`` primitives. This closes the
        detector→template loop without hand-authoring ``templates/*.py``.
        """
        from ..planning.dag_draft import linear_draft_from_phases

        pattern = self._pattern_resolver(pattern_id)
        if pattern is None or not pattern.skeleton:
            raise ValueError(f"cannot build draft for pattern {pattern_id!r}: no skeleton")
        return linear_draft_from_phases(list(pattern.skeleton), goal)

    # Back-compat alias — old name still works but new code should use
    # ``build_draft(pattern_id, goal=...)``. Kept thin so removing it later
    # is a one-line change.
    def promote(self, candidate: "Candidate", *, goal: str):
        return self.build_draft(candidate.pattern_id, goal=goal)

    # ── persistence ────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-portable snapshot of the recorded traces.

        Thresholds are NOT serialized — they're deployment policy, not
        runtime state, so a config bump shouldn't get pinned by an old
        snapshot. Restoring a detector reads thresholds from the live
        constructor and the trace counts from the snapshot.
        """
        return {
            "traces": {
                pattern_id: [t.to_dict() for t in traces]
                for pattern_id, traces in self._traces.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        *,
        min_occurrences: int = 5,
        min_success_rate: float = 0.8,
        pattern_resolver: Optional[Callable[[str], Optional[Pattern]]] = None,
    ) -> "TemplateDetector":
        det = cls(
            min_occurrences=min_occurrences,
            min_success_rate=min_success_rate,
            pattern_resolver=pattern_resolver,
        )
        for pattern_id, traces in (d.get("traces") or {}).items():
            for raw in traces:
                det._traces[pattern_id].append(Trace.from_dict(raw))
        return det

    def total_traces(self) -> int:
        """Convenience for telemetry / debug."""
        return sum(len(v) for v in self._traces.values())
