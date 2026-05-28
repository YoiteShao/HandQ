"""Prompts and JSON parsing for the dream-synthesis layers (L2 / L3).

L2: a cluster of similar L1 entries → one synthesised "pattern" entry.
L3: a cluster of L2 patterns → one synthesised "meta-insight" entry.

These are kept in their own module (not in :mod:`prompts`) so the main
triage prompt stays focused on the per-candidate decision and the dream
prompts stay focused on cross-entry synthesis.

Schema for both:

    {
      "worth_synth"   : <bool>,
      "target_facet"  : "agentic"|"insight"   (memory) or
                        "domain"|"people"|"process"|"coding"|"other"  (knowledge),
      "summary"       : "<single line, max 120 chars>",
      "content"       : "<markdown body, <=800 chars>",
      "reason"        : "<short rationale, max 100 chars>"
    }
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple

from .models import KnowledgeCategory, MemoryDimension

_logger = logging.getLogger("handq.ltm.dream_prompts")


# ── L2 (pattern) ────────────────────────────────────────────────────────────

L2_SYSTEM_MEMORY = """\
You are extracting a recurring PATTERN from a cluster of related memory entries.

The cluster contains multiple memory points that the embedding layer judged
similar. Your job: decide whether they DO express a single recurring user
preference / habit, and if so, write ONE synthesised memory point that
captures the pattern at the right level of abstraction.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "agentic" | "insight" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## Decision rules

KEEP (worth_synth=true) when:
- The cluster expresses a coherent, repeated preference or habit.
- A synthesised summary is more useful than the individual entries.
- At least 3 entries reinforce the same theme.

REJECT (worth_synth=false) when:
- Entries are similar in topic but contradictory in conclusion.
- The cluster is just two near-duplicates already covered by merge dedup.
- Writing the synthesis would lose meaningful detail vs the individuals.
- The pattern only makes sense for one specific past task (one-off).

## Style

- Use second-person ("you ...") or imperative ("Always ...").
- target_facet=agentic when the pattern describes how the user wants the
  agent to BEHAVE; target_facet=insight when it describes a STABLE FACT
  about the user / their environment.
- content uses "## Memory Points" markdown with one bullet per point.
- Cite a concrete signal (file, command, config key) where possible.
- DO NOT mention this is a "pattern" or "synthesis" inside content — the
  recall layer renders it the same way as any other memory point.
"""

L2_SYSTEM_KNOWLEDGE = """\
You are extracting a recurring PATTERN from a cluster of related knowledge entries.

The cluster contains multiple knowledge points that the embedding layer judged
similar. Your job: decide whether they DO express a single reusable team /
project / domain insight, and if so, write ONE synthesised knowledge point.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "domain" | "people" | "process" | "coding" | "other" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## Decision rules

KEEP (worth_synth=true) when:
- The cluster expresses a coherent reusable fact about team / project.
- The synthesised version generalises across the source entries.
- At least 3 entries support the same conclusion.

REJECT (worth_synth=false) when:
- Entries cover the same topic but disagree on the fact.
- The cluster is near-duplicates that merge dedup should handle.
- Synthesis would be vaguer than each individual.

## Style

- Use third-person factual prose ("Team X uses Y", "Service Z lives at W").
- content uses "## Description" + "## Key Insights" markdown.
- Pick the most specific facet that fits.
- DO NOT mention this is a "pattern" or "synthesis" inside content.
"""


L2_USER_TEMPLATE = """\
Cluster ({n} entries):

{numbered_entries}

Output the JSON object only."""


# ── L3 (meta-insight) ───────────────────────────────────────────────────────

L3_SYSTEM_MEMORY = """\
You are extracting a META-INSIGHT from a cluster of L2 patterns.

L2 patterns each describe ONE recurring preference. Your job: look at
multiple patterns and decide whether they share a HIGHER-LEVEL theme
that none of them captures alone.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "agentic" | "insight" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## Decision rules

KEEP (worth_synth=true) when:
- The patterns reveal a deeper consistency the individuals don't capture
  (e.g. "fast feedback loops" across "use ruff", "use pytest -xvs",
  "small commits").
- The meta-insight gives planner / agent useful predictive signal.

REJECT (worth_synth=false) when:
- Patterns are merely related-by-topic, not by deeper theme.
- The proposed meta-insight restates what one of the patterns already says.
- Fewer than 3 patterns reinforce the theme.

## Style

- Same conventions as L2 memory prompt.
- Be conservative — meta-insights replace many patterns in recall, so a
  bad one drowns out specific signal.
"""

L3_SYSTEM_KNOWLEDGE = """\
You are extracting a META-INSIGHT from a cluster of L2 knowledge patterns.

Same idea as the memory variant, but operating on team/project knowledge
instead of user preferences. Look for higher-level themes that span
multiple specific patterns.

Output a SINGLE JSON object. No prose, no markdown fences.

## Schema

{
  "worth_synth"   : <bool>,
  "target_facet"  : "domain" | "people" | "process" | "coding" | "other" | null,
  "summary"       : "<single line, max 120 chars>",
  "content"       : "<markdown body, <=800 chars>",
  "reason"        : "<short rationale, max 100 chars>"
}

## Decision rules

Same as L3 memory — be conservative; reject unless the theme genuinely
adds value beyond the individual patterns.
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
    s = (json_str or "").strip()
    if not s:
        return {"worth_synth": False, "reason": "empty_response"}

    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()

    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j <= i:
            return {"worth_synth": False, "reason": "no_json_in_response"}
        s = s[i:j + 1]

    try:
        d = json.loads(s)
    except json.JSONDecodeError as exc:
        _logger.warning("synth verdict JSON decode failed: %s", str(exc)[:80])
        return {"worth_synth": False, "reason": "invalid_json"}

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
