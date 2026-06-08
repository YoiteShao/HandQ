"""Structured findings + deterministic convergence (report §3.5, §11.4).

The report's sharpest correction (PROGRESS.md §5, C2): *concurrency is not the
goal — convergence is*. Fan-out only raises coverage; the hard, value-bearing
part is merging, de-duplicating, ranking and judging completeness. None of that
is possible if subagents return free text, so their results must be a **typed,
aggregatable Finding** rather than prose.

This module is pure and deterministic — no LLM, no IO. It defines the schema and
the merge → dedup → rank pipeline that turns N subagents' findings into one
ranked list the main model can act on. The validator gallery (validators.py)
and any future ParallelGroup fan-in build on this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Ordered worst-first so ranking and "keep the worse duplicate" are trivial.
SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity.lower(), 0)


@dataclass(frozen=True)
class Finding:
    """One typed, aggregatable observation from a node/subagent.

    Frozen so it is hashable and safe to dedup. Only ``category`` and ``summary``
    are required; everything else has a sensible empty default so a cheap node
    can emit a minimal finding and a thorough one can fill the schema.
    """

    category: str            # stable bucket, e.g. "path_traversal" / "bug" / "missing_test"
    summary: str             # one-line human description
    severity: str = "info"   # critical|high|medium|low|info
    location: str = ""       # "path/to/file.py:128" or a symbol name
    evidence: str = ""       # why we believe it (quote, taint path, failing output)
    recommendation: str = "" # what to do about it
    source: str = ""         # which node/subagent produced it
    confidence: float = 1.0  # 0..1

    def dedup_key(self) -> tuple[str, str]:
        """Two findings collide when they name the same issue at the same place.

        Location is the strong signal; when absent (e.g. a repo-wide observation)
        we fall back to the summary text so distinct issues stay distinct.
        """
        return (self.category.lower(), (self.location or self.summary).lower())


def merge_findings(groups: Iterable[Iterable[Finding]]) -> list[Finding]:
    """Flatten several findings groups (e.g. one per fan-out subagent)."""
    out: list[Finding] = []
    for g in groups:
        out.extend(g)
    return out


def dedup_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Collapse same-issue findings, keeping the most severe / most confident.

    Order-independent: the surviving representative of each dedup key is the one
    with the highest (severity, confidence). Insertion order of distinct keys is
    preserved so output is stable.
    """
    best: dict[tuple[str, str], Finding] = {}
    order: list[tuple[str, str]] = []
    for f in findings:
        key = f.dedup_key()
        cur = best.get(key)
        if cur is None:
            best[key] = f
            order.append(key)
        else:
            incoming = (severity_rank(f.severity), f.confidence)
            existing = (severity_rank(cur.severity), cur.confidence)
            if incoming > existing:
                best[key] = f
    return [best[k] for k in order]


def rank_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Sort worst-first: severity desc, then confidence desc, then location."""
    return sorted(
        findings,
        key=lambda f: (-severity_rank(f.severity), -f.confidence, f.location),
    )


def converge(groups: Iterable[Iterable[Finding]]) -> list[Finding]:
    """The full fan-in: merge → dedup → rank. Deterministic and order-stable."""
    return rank_findings(dedup_findings(merge_findings(groups)))
