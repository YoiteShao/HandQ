"""Prompts and JSON parsing for the dream-synthesis layers (L2 / L3).

L2: cluster of L1 entries → one user-profile / project-fact point that helps
    an agent execute future tasks well. Design principle: memory exists to
    serve session tasks, not to catalogue activity.

L3: cluster of L2 points → one decision principle that PREDICTS what the
    user/team wants in situations no L2 point directly covers.

These are kept in their own module (not in :mod:`prompts`) so the main
triage prompt stays focused on the per-candidate decision and the dream
prompts stay focused on cross-entry synthesis.

Schema for both:

    {
      "worth_synth"   : <bool>,
      "target_facet"  : "agentic"   (memory) or
                        "domain"|"people"|"process"|"coding"|"other"  (knowledge),
      "summary"       : "<single line, max 120 chars>",
      "content"       : "<markdown body, <=800 chars>",
      "reason"        : "<short rationale, max 100 chars>"
    }
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from ..utils import try_parse_json
from .models import KnowledgeCategory, MemoryDimension

_logger = logging.getLogger("handq.ltm.dream_prompts")


# ── L2 (pattern) ────────────────────────────────────────────────────────────

L2_SYSTEM_MEMORY = """\
You are distilling a cluster of memory entries into ONE actionable user-profile point.

Context: An AI agent will read this point before planning/executing tasks for
the user. The point should help the agent make better decisions about HOW to
work. Every synthesised point must earn its recall slot.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "agentic" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## KEEP (worth_synth=true) when the cluster expresses:

- A durable PREFERENCE (coding style, tool choice with a stated reason, review
  expectations, communication style)
- An environment CONSTRAINT the agent must respect (OS, paths, remote hosts,
  infra layout)
- A recurring MISTAKE PATTERN the agent should avoid
- A stable working-style trait (batch vs incremental, verbose vs terse, etc.)

Test: "Would this still guide an agent 3 months from now?"

## REJECT (worth_synth=false) when:

- ACTIVITY LOG: merely records what the user did (browsed X, opened Y,
  attended Z meeting) without extracting a preference or constraint.
  Example: "User browses ML artifacts in File Explorer" → reject.
- TOOL INVENTORY without preference signal. "Team uses WindTerm SSH" is only
  worth keeping if it expresses a CHOICE ("prefers WindTerm over PuTTY
  because..."). Bare tool usage is not a preference.
- STALE/TEMPORAL: bound to a specific project phase already complete
  (past UI experiments, deprecated modules, finished migrations).
- DERIVABLE: the agent could get this by reading CLAUDE.md, package.json,
  or the codebase directly. Don't synthesise infrastructure.
- CONTRADICTORY: entries disagree; the cluster reflects evolving state, not
  a settled preference.
- NEAR-DUPLICATES only two entries strong — merge dedup handles these.

## Style

- Second person ("you ...") or imperative ("Always ...").
- target_facet = "agentic" for all memory patterns.
- Content uses "## Key Points" with one bullet per actionable signal.
- Be concrete: cite a tool, flag, pattern name, or file path where possible.
- DO NOT mention "pattern" or "synthesis" inside content.
"""

L2_SYSTEM_KNOWLEDGE = """\
You are consolidating a cluster of knowledge entries into ONE reusable project fact.

Context: An AI agent will recall this fact during task planning/execution.
The fact should help the agent understand the project/team/domain well enough
to avoid mistakes and produce work that fits the codebase.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "domain" | "people" | "process" | "coding" | "other" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## KEEP (worth_synth=true) when the cluster expresses:

- Architecture/design DECISIONS an agent must respect (module boundaries,
  data flow, ownership rules)
- Known PITFALLS or failure modes in this codebase
- Team WORKFLOW conventions (CI gates, review requirements, release cadence)
- API CONTRACTS or integration points between subsystems
- Stable naming/convention rules ("all skills go under Skill/", etc.)

Test: "Would an agent executing a related task make a mistake without this?"

## REJECT (worth_synth=false) when:

- ACTIVITY LOG: records that someone reviewed/read/browsed something.
  Example: "Team regularly reviews Continuous Batching design doc" → reject.
  The DOCUMENT'S content might be worth keeping — the READING isn't.
- DERIVABLE from current source code, README, or config files.
- EVOLVING STATE: entries disagree or represent understanding still in flux.
- PAST INCIDENT with no forward-looking implication (a specific bug that got
  fixed and won't recur is history, not knowledge).
- TOOL USAGE without design implication ("Team uses SSH" — but for what,
  and why does the agent need to know?).

## Style

- Third-person factual prose.
- Content uses "## Description" + "## Implications for Tasks" markdown.
- Pick the most specific facet that fits.
- DO NOT mention "pattern" or "synthesis" inside content.
"""


L2_USER_TEMPLATE = """\
Cluster ({n} entries):

{numbered_entries}

Output the JSON object only."""


# ── L3 (decision principle) ────────────────────────────────────────────────

L3_SYSTEM_MEMORY = """\
You are extracting a DECISION PRINCIPLE from a cluster of L2 user-profile points.

Context: Multiple L2 points each describe one specific preference or constraint.
Your job: identify whether they share a deeper principle that would help an
agent PREDICT what the user wants in NOVEL situations — situations none of the
L2 points directly cover.

A good decision principle is:
- PREDICTIVE: given a new task, it tells the agent which direction the user
  would prefer.
- CONCISE: one clear rule, not a list.
- FALSIFIABLE: you can imagine a scenario that would violate it.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "agentic" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## KEEP (worth_synth=true) when:

- The principle EXPLAINS multiple L2 points as consequences of one deeper
  value. Example: L2 points {prefers ruff, uses pytest -xvs, keeps commits
  small} → "Values fast feedback loops."
- The principle would GUIDE the agent in a task none of the L2 points cover.
  Example: applying "fast feedback" to a new task → agent picks incremental
  test runs, live reload, smaller PR splits.
- At least 3 L2 points genuinely reinforce it (not restate it).

## REJECT (worth_synth=false) when:

- TOPICAL cluster only (all L2 points mention "SSH", but each for a
  different reason — no shared principle).
- VAGUE / unfalsifiable ("User likes good tools", "Prefers efficiency").
  A principle that fits any user is useless.
- The proposed principle is already SAID by one of the L2 points — nothing
  is being extracted, just restated at a higher level.
- Fewer than 3 L2 points genuinely support it.

## Style

- Summary: one imperative or declarative sentence stating the principle.
- Content: state the principle, then list the 2-3 strongest L2 evidences.
- Be conservative — a bad L3 crowds out specific L2 signal in recall.
- DO NOT mention "L2" or "synthesis" inside content.
"""

L3_SYSTEM_KNOWLEDGE = """\
You are extracting a DESIGN PRINCIPLE from a cluster of L2 project-knowledge points.

Context: Multiple L2 points each describe one specific project fact or
convention. Your job: identify whether they share a deeper architectural or
process principle that would help an agent make decisions in NOVEL situations
this project hasn't yet encountered.

A good design principle is:
- PREDICTIVE: given a new design choice, it points to the "team-shaped" answer.
- CONCISE: one clear rule.
- FALSIFIABLE: you can imagine a design that would violate it.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "domain" | "people" | "process" | "coding" | "other" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## KEEP (worth_synth=true) when:

- The principle EXPLAINS multiple L2 facts as consequences of one design idea.
  Example: L2 facts {proactive engine uses lightweight signals, LTM avoids
  LLM-per-session scoring, ArcAggregator batches events} → "Separates cheap
  detection from expensive analysis."
- The principle would GUIDE a NEW design choice this project hasn't made yet.

## REJECT (worth_synth=false) when:

- TOPICAL clustering only (all L2 points about "Skills", but no shared design
  principle).
- Trivially generic ("Team values quality", "Prefers clean code").
- The proposed principle just restates one of the L2 facts.
- Fewer than 3 L2 facts genuinely support it.

## Style

- Third-person, one-sentence principle as summary.
- Content: state the principle, then list evidences.
- Pick the most specific facet that fits.
- DO NOT mention "L2" or "synthesis" inside content.
"""

L3_USER_TEMPLATE = """\
Patterns ({n}):

{numbered_entries}

Output the JSON object only."""


# ── Render + parse ──────────────────────────────────────────────────────────

def render_cluster(entries: List[Tuple[str, str, str]]) -> str:
    """Format a cluster for the L2/L3 user prompt.

    Each entry: (id, facet, summary). We do NOT include full content —
    summary is the canonical 1-liner and gives the LLM enough signal
    while keeping the prompt small.
    """
    lines = []
    for i, (eid, facet, summary) in enumerate(entries, start=1):
        lines.append(f"{i}. [id={eid}] ({facet}) {summary}")
    return "\n".join(lines)


def parse_synth_verdict(json_str: str) -> dict:
    """Parse the L2/L3 LLM output into a dict.

    Always returns a dict with at least worth_synth=False on parse error
    (so the caller can blanket-skip without raising). Schema-fields with
    bad / missing values get safe defaults.
    """
    if not json_str or not json_str.strip():
        return {"worth_synth": False, "reason": "empty_response"}

    parsed = try_parse_json(json_str)
    if not isinstance(parsed, dict):
        return {"worth_synth": False, "reason": "no_json_in_response"}

    d = parsed
    worth = bool(d.get("worth_synth", False))
    facet = d.get("target_facet")
    summary = (d.get("summary") or "").strip()[:120]
    content = (d.get("content") or "").strip()[:1200]
    reason = (d.get("reason") or "")[:200]

    if worth and not (facet and summary and content):
        worth = False
        reason = (reason + " | missing_required_fields")[:200]

    return {
        "worth_synth": worth,
        "target_facet": facet,
        "summary": summary,
        "content": content,
        "reason": reason,
    }


def validate_facet_for_kind(*, kind: str, facet: Optional[str]) -> Optional[str]:
    """Coerce facet string to a valid enum value for the given kind.
    Returns the canonical lowercase string, or None if invalid.
    """
    if not facet:
        return None
    f = str(facet).lower()
    from .models import EntryKind
    if kind == EntryKind.MEMORY.value:
        try:
            return MemoryDimension(f).value
        except ValueError:
            return None
    if kind == EntryKind.KNOWLEDGE.value:
        try:
            return KnowledgeCategory(f).value
        except ValueError:
            return None
    return None
