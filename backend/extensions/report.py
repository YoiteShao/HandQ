"""Markdown report renderer — turns a converged run's outputs into a Markdown report.

Closes the MVP loop the report calls out as §9.1: once a workflow has fanned
out, found issues, converged them, and produced a structured findings list,
the user needs a *consumable artifact* — typically a Markdown blob they can
paste into a PR, an issue, or a chat message. This renderer produces it.

Pure deterministic: no LLM, no IO. It takes artifacts that already live on the
run (``RunTrace`` + converged ``Finding`` list + optional ``Blackboard.summaries``)
and renders a section-structured Markdown string. Tests pin the exact shape so
downstream consumers (a frontend, a notification bot, a regression diff) can
rely on it.

Sections, in order:

  1. Header          — goal, status, duration, run id
  2. Findings        — grouped by severity (critical → info), one table per
                       severity, with optional Evidence + Recommendation rows
  3. Decision Path   — which branch the graph actually took
  4. Phase Summaries — per-phase summaries from Blackboard.summaries (only
                       when set; this is the "Summary Store" controlled-context
                       layer described in report §8.5)
  5. Footer          — steps / tokens / artifacts totals
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..engine.findings import Finding, rank_findings
from ..engine.observability import RunTrace


SEVERITY_ORDER_DESC = ("critical", "high", "medium", "low", "info")


def render_report(
    *,
    trace: RunTrace,
    findings: Iterable[Finding] = (),
    summaries: Optional[dict[str, str]] = None,
) -> str:
    """Render a converged run's outputs as one Markdown string.

    The caller is expected to have already run ``converge()`` (or equivalent)
    on whatever raw findings the run produced; this function does not dedup or
    re-rank — it only formats. ``rank_findings`` is applied as a defensive
    sort so an unsorted list still renders in worst-first order.
    """
    ranked = rank_findings(list(findings or ()))
    parts: list[str] = [
        _render_header(trace),
        _render_findings(ranked),
        _render_decision_path(trace),
    ]
    if summaries:
        parts.append(_render_summaries(summaries))
    parts.append(_render_footer(trace))
    return "\n\n".join(p for p in parts if p)


def _render_header(trace: RunTrace) -> str:
    status = "OK" if trace.ok else ("PARTIAL" if trace.partial else "FAILED")
    return (
        f"# Workflow Report\n\n"
        f"**Goal**: {trace.goal}\n\n"
        f"**Status**: {status}\n\n"
        f"**Run ID**: `{trace.run_id}`\n\n"
        f"**Duration**: {trace.duration_s:.2f}s"
    )


def _render_findings(findings: list[Finding]) -> str:
    if not findings:
        return "## Findings\n\n_No findings._"

    # Bucket by severity, preserving rank within each bucket. Unknown severity
    # values (a typo or a forward-compat addition) collapse into "info" so they
    # still surface but with the lowest priority.
    buckets: dict[str, list[Finding]] = {sev: [] for sev in SEVERITY_ORDER_DESC}
    for f in findings:
        sev = f.severity.lower() if f.severity.lower() in buckets else "info"
        buckets[sev].append(f)

    sections: list[str] = ["## Findings"]
    for sev in SEVERITY_ORDER_DESC:
        items = buckets[sev]
        if not items:
            continue
        sections.append(f"### {sev.capitalize()} ({len(items)})")
        sections.append("| Category | Location | Summary | Source | Confidence |")
        sections.append("|----------|----------|---------|--------|------------|")
        for f in items:
            sections.append(
                f"| {_cell(f.category)} | {_cell(f.location or '—')} | "
                f"{_cell(f.summary)} | {_cell(f.source or '—')} | "
                f"{f.confidence:.2f} |"
            )
        # Detail block per finding — only when at least one item carries
        # evidence or a recommendation, so a minimal finding stays compact.
        details = []
        for f in items:
            if not (f.evidence or f.recommendation):
                continue
            details.append(f"**{f.category}** at `{f.location or '—'}`:")
            if f.evidence:
                details.append(f"- _Evidence_: {f.evidence}")
            if f.recommendation:
                details.append(f"- _Recommendation_: {f.recommendation}")
            details.append("")  # blank line between detail blocks
        if details:
            sections.append("")
            sections.extend(details)

    return "\n".join(sections)


def _render_decision_path(trace: RunTrace) -> str:
    """Render which branch the graph actually walked."""
    if not trace.events:
        return ""
    arrows = " ".join(
        f"{e.node}--{e.route}-->"
        for e in trace.events
    )
    return "## Decision Path\n\n```\n" + arrows + " END\n```"


def _render_summaries(summaries: dict[str, str]) -> str:
    sections = ["## Phase Summaries"]
    for phase, body in summaries.items():
        sections.append(f"### {phase}")
        sections.append(body.rstrip())
    return "\n\n".join(sections)


def _render_footer(trace: RunTrace) -> str:
    return (
        f"---\n"
        f"_steps={trace.steps} tokens={trace.total_tokens} "
        f"artifacts={trace.total_artifacts}_"
    )


def _cell(text: str) -> str:
    """Escape pipe and newline so a Finding field can't break the table."""
    return text.replace("|", "\\|").replace("\n", "<br>")
