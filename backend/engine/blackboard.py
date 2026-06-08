"""Blackboard — shared typed state passed between workflow nodes.

This fixes v1's seam-induced context loss: instead of re-instantiating a
fresh RuntimeAgent per batch (losing cross-step findings), every node reads
the keys it declares from a single shared object and writes its findings
back. The runner threads one Blackboard through the whole graph.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .findings import Finding


@dataclass
class Blackboard:
    """Shared state for one workflow run.

    Attributes
    ----------
    goal:
        The original user goal driving this run.
    working_dir:
        Working directory passed down to every AgentNode's executor.
    state:
        Free-form typed key/value findings. Node ``foo`` conventionally writes
        its summary under key ``foo``; nodes declare ``context_keys`` to read a
        scoped slice (not the whole blackboard) — keeping each node's context
        bounded.
    artifacts:
        Files / resources produced (path -> metadata). Accumulated across nodes.
    history:
        Append-only log of (node_name, ok, summary) for progress queries and
        the recurrence detector.
    """

    goal: str
    working_dir: Optional[str] = None
    state: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    history: list[tuple[str, bool, str]] = field(default_factory=list)
    # Structured, aggregatable results (artifact-class — NOT auto-folded into any
    # agent's context; converge() them explicitly when a phase needs the merged
    # ranked list). See engine/findings.py.
    findings: list["Finding"] = field(default_factory=list)
    # The Summary store (report §8.5): per-phase controlled summaries — the ONLY
    # layer meant to enter a downstream agent's context. Distinct from raw state
    # so we fold bounded summaries forward, never the whole history/artifacts.
    summaries: dict[str, str] = field(default_factory=dict)

    def pick(self, keys: list[str]) -> dict[str, Any]:
        """Return a scoped slice of state. Empty ``keys`` = full isolation."""
        return {k: self.state[k] for k in keys if k in self.state}

    def merge(self, data: dict[str, Any]) -> None:
        if data:
            self.state.update(data)

    def add_artifacts(self, artifacts: dict[str, Any]) -> None:
        if artifacts:
            self.artifacts.update(artifacts)

    def record(self, node_name: str, ok: bool, summary: str) -> None:
        self.history.append((node_name, ok, summary))

    def add_findings(self, items: list["Finding"]) -> None:
        if items:
            self.findings.extend(items)

    def converged_findings(self) -> list["Finding"]:
        """Merged → deduped → ranked view of every finding recorded so far."""
        from .findings import converge
        return converge([self.findings])

    def record_summary(self, phase: str, text: str) -> None:
        """Write a phase's controlled summary — the bounded thing allowed to flow
        into a downstream agent's context (report §8.5 Summary Store)."""
        self.summaries[phase] = text

    def summary_context(self, phases: Optional[list[str]] = None) -> str:
        """Joined summaries for the given phases (all, in record order, if None).

        This is what a downstream node folds into its goal text — never the raw
        state dict or artifact blobs."""
        keys = phases if phases is not None else list(self.summaries)
        return "\n".join(f"{k}: {self.summaries[k]}" for k in keys if k in self.summaries)

    def progress_text(self) -> str:
        """Human-readable progress for the receptionist's 'how's it going?'."""
        if not self.history:
            return "Not started yet."
        lines = [f"{'✓' if ok else '✗'} {name}: {summary[:120]}"
                 for name, ok, summary in self.history]
        return "\n".join(lines)

    # ── persistence (pause / resume / continuation, report §8.5) ──────────────
    # The Blackboard is the entire mutable state of a run; the graph itself is
    # code/draft (rebuilt on resume, never serialized). Snapshot is therefore the
    # whole resumable payload. JSON-safety of ``state``/``artifacts`` values is
    # the caller's contract — same as TraceStore.

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable copy of all run state, for persistence/resume."""
        from dataclasses import asdict
        return {
            "goal": self.goal,
            "working_dir": self.working_dir,
            "state": dict(self.state),
            "artifacts": dict(self.artifacts),
            "history": [list(h) for h in self.history],
            "summaries": dict(self.summaries),
            "findings": [asdict(f) for f in self.findings],
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "Blackboard":
        """Rebuild a Blackboard from ``snapshot()`` output (tuples/Findings revived)."""
        from .findings import Finding
        bb = cls(goal=data["goal"], working_dir=data.get("working_dir"))
        bb.state = dict(data.get("state", {}))
        bb.artifacts = dict(data.get("artifacts", {}))
        bb.history = [tuple(h) for h in data.get("history", [])]
        bb.summaries = dict(data.get("summaries", {}))
        bb.findings = [Finding(**f) for f in data.get("findings", [])]
        return bb
