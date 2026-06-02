"""Rule migrations — retroactive memory hygiene declarative pipeline.

Mental model
------------
SQLite gets ``schema.py:MIGRATIONS`` for STRUCTURE changes (new tables,
new columns). This file is its sibling for RULE changes (the triage
prompt got stricter, a new code-side guard was added, a duplicate
pattern needs cleanup).

Each :class:`RuleMigration` is a versioned, append-only audit pass over
existing entries. It emits :class:`CorrectionProposal` rows; the
:class:`RetriageWorker` decides which to auto-apply (per
``CORRECTION_AUTO_APPLY_TIER``) and writes the rest to
``correction_proposals`` for explicit review.

Append-only invariant
---------------------
Once a migration has shipped (a release went out with version N), do not
edit ``_mNNN_*``. Different users may be at different ``triage_rules_version``
values; rewriting an old migration silently rebuilds their corpus
differently from what the audit log says happened. Add ``_m{N+1}_*``
instead and let the new pass take over.

Auto-apply tiers
----------------
A migration declares ``auto_apply_kinds`` — the subset of
``CorrectionKind`` whose proposals the RetriageWorker auto-applies for
this migration. Convention:

- {ARCHIVE} for *deterministic* corrections (math says it; no judgment).
  Example: archiving an L2 source whose synthesis row already cites it.
- ``set()`` for everything else — propose-only, accumulating until a
  user (via UI or admin CLI) explicitly approves.

Even when a kind IS in ``auto_apply_kinds``, the global setting
``CORRECTION_AUTO_APPLY_TIER`` can downgrade to PROPOSAL_ONLY. The
migration's declaration is the upper bound; runtime config is the
ceiling.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, List, Optional, Set

from .models import (
    ArchiveReason,
    CorrectionKind,
    CorrectionProposal,
    CorrectionStatus,
    EntryKind,
)

_logger = logging.getLogger("handq.ltm.rule_migrations")


# A migration's audit() is an async generator yielding **unsaved**
# CorrectionProposal instances. The worker handles persistence + PII
# scrub + auto-apply gating uniformly.
AuditFn = Callable[..., AsyncIterator[CorrectionProposal]]


@dataclass
class RuleMigration:
    """One versioned data-hygiene rule. Append to ``RULE_MIGRATIONS``.

    Fields
    ------
    version
        1-indexed. MUST be unique and monotonic across the list. Stored
        in ``correction_proposals.rule_version`` for traceability.
    name
        Short stable slug (snake_case). Embedded in
        ``archived_reason='correction_v{N}_{name}'`` so SQL rollback can
        target a specific migration.
    is_llm
        True if ``audit`` calls a helper LLM. The worker wires the LLM
        services in only when needed (no LLM cost for pure-Python passes).
    auto_apply_kinds
        Set of :class:`CorrectionKind` whose proposals can be auto-applied
        for this migration. Empty set = propose-only.
    description
        One-line human-readable purpose; surfaces in IPC / admin output.
    audit
        Async generator. Signature: ``audit(store, llm_services, **kwargs)
        -> AsyncIterator[CorrectionProposal]``. The worker passes
        ``llm_services=None`` when ``is_llm`` is False.
    """
    version: int
    name: str
    is_llm: bool
    auto_apply_kinds: Set[CorrectionKind]
    description: str
    audit: AuditFn


# ── _m001_l2_orphan_archive ─────────────────────────────────────────────────
# Pure-Python deterministic migration: any L2/L3 dream synthesis entry
# already cites the source entries it subsumed via ``source_entry_ids``.
# The synthesis row IS the canonical fact now; the sources are
# permanently redundant. Auto-apply this kind of archive is
# mathematically safe — the synthesis entry's content is by construction
# a superset of each source's content.
#
# Why this even needs a migration: prior to v1.1.2, the L2 dream synthesis
# code wrote the synthesis entry but did NOT archive its sources. Users
# upgrading from v1.1.1 have orphan source entries cluttering recall.
# The current code path archives them inline (see triage.py
# _apply_synth_cluster) — this migration retroactively fixes the gap.

async def _m001_l2_orphan_archive(
    store, llm_services, *, run_id, in_flight_entry_ids, config,
) -> AsyncIterator[CorrectionProposal]:
    """Archive raw entries that an active L2/L3 synthesis row already cites.

    Iterates ``memory_files`` and ``knowledge_files`` for synthesis rows
    (``synthesis_level >= 2``, archived=0). For each, it parses
    ``source_entry_ids`` JSON and yields one ARCHIVE proposal per source
    that is still active. Confidence is 1.0 — by construction the source's
    content is already represented in the synthesis row.

    ``payload`` carries ``superseded_by_id=<synthesis_row_id>`` so the
    archive apply path stamps the FK; recall queries can later trace why
    a given source was archived without re-reading dream_runs.
    """
    for kind in (EntryKind.MEMORY, EntryKind.KNOWLEDGE):
        files_table = (
            "memory_files" if kind == EntryKind.MEMORY else "knowledge_files"
        )
        rows = await store._fetchall(
            f"SELECT id, source_entry_ids FROM {files_table} "
            f"WHERE archived=0 AND synthesis_level >= 2 "
            f"AND source_entry_ids IS NOT NULL AND source_entry_ids != ''",
        )
        for synth_id, sids_json in rows:
            try:
                source_ids = json.loads(sids_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(source_ids, list):
                continue
            for src_id in source_ids:
                src_id = str(src_id)
                if not src_id or src_id == synth_id:
                    continue
                # Skip if already archived (no work to do) or in-flight
                # in another active dream_runs entry.
                src_row = await store._fetchone(
                    f"SELECT version, archived FROM {files_table} WHERE id=?",
                    (src_id,),
                )
                if not src_row:
                    continue
                version, archived = int(src_row[0]), bool(src_row[1])
                if archived:
                    continue
                yield CorrectionProposal(
                    id="",  # filled by store.insert_correction_proposal
                    kind=CorrectionKind.ARCHIVE,
                    target_kind=kind,
                    target_entry_id=src_id,
                    target_version=version,
                    target_archived=archived,
                    payload={
                        "superseded_by_id": synth_id,
                        "rule": "l2_orphan_archive",
                    },
                    confidence=1.0,
                    rule_version=1,
                    parent_run_id=run_id,
                    rationale=(
                        f"Source entry already represented in L2/L3 synthesis "
                        f"{synth_id[:8]}; safe to archive."
                    ),
                    rationale_pii_scrubbed=False,
                    status=CorrectionStatus.PENDING,
                    created_at=0,  # filled by store.insert_correction_proposal
                )

# ── _m002_path_inventory_audit ──────────────────────────────────────────────
# Pure-Python *judgment* migration: applies the v1.1.2 path-inventory
# guard retroactively. Hits any active activity_observer INSIGHT memory
# whose summary is mostly file-path tokens with no behaviour verb (e.g.
# "HandQ project at C:\\CodeProject\\HandQ"). These were accepted by an
# earlier (looser) triage prompt but the new guard would reject them.
#
# Auto-apply tier: ``set()`` — propose-only. Even though the rule itself
# is deterministic, "is this entry valuable to keep" is a judgment call
# we want a human (or future UI) to confirm. Recall recency on the
# entry surfaces priority but does NOT block the proposal.

async def _m002_path_inventory_audit(
    store, llm_services, *, run_id, in_flight_entry_ids, config,
) -> AsyncIterator[CorrectionProposal]:
    """Propose archiving activity_observer INSIGHT entries that look like
    pure path inventories (no behaviour signal).

    Implementation reuses :func:`triage._is_path_inventory` so the
    runtime guard and this retroactive audit always agree on what counts.
    """
    from .triage import _is_path_inventory

    rows = await store._fetchall(
        "SELECT id, summary, version, source FROM memory_files "
        "WHERE archived=0 AND dimension='insight' AND source='activity_observer'",
    )
    for entry_id, summary, version, source in rows:
        if entry_id in in_flight_entry_ids:
            continue
        if not _is_path_inventory(summary or ""):
            continue
        # Recall priority signal: tag rationale so review UI can sort.
        recall_count = await store.count_recent_recalls(
            entry_id=entry_id, kind=EntryKind.MEMORY.value,
            since_seconds=30 * 24 * 3600,
        )
        priority_note = (
            f" (recently recalled {recall_count}x — review carefully)"
            if recall_count > 0 else ""
        )
        yield CorrectionProposal(
            id="",
            kind=CorrectionKind.ARCHIVE,
            target_kind=EntryKind.MEMORY,
            target_entry_id=entry_id,
            target_version=int(version),
            target_archived=False,
            payload={"rule": "path_inventory_audit"},
            confidence=0.85,  # high but judgment, hence propose-only
            rule_version=2,
            parent_run_id=run_id,
            rationale=(
                f"Summary matches retroactive path-inventory guard "
                f"(no behaviour verb, mostly path tokens){priority_note}."
            ),
            rationale_pii_scrubbed=False,
            status=CorrectionStatus.PENDING,
            created_at=0,
        )

# ── _m003_full_retriage_audit ───────────────────────────────────────────────
# LLM-driven full corpus audit. Re-judges every active entry against the
# CURRENT TRIAGE_SYSTEM prompt — the same prompt that judges new
# candidates — so "would this entry be accepted today?" is answered
# consistently with the live triage path.
#
# Output:
#   keep    → no proposal
#   archive → CorrectionProposal(kind=ARCHIVE, confidence, rationale)
#   rewrite → CorrectionProposal(kind=REWRITE, payload={new_summary,
#                                new_content_md}, confidence, rationale)
#
# Auto-apply tier: ``set()`` — propose-only. Even when LLM confidence is
# 0.99, judging that an existing durable entry should be removed touches
# user-relevant data; explicit consent (or HIGH_CONF tier opt-in) is
# required.
#
# Resumability: progress is checkpointed every
# ``CORRECTION_RETRIAGE_CHECKPOINT_EVERY`` entries via
# ``store.set_retriage_progress(version, last_id)``. After a crash the
# next launch reads the marker and skips already-processed ids.
#
# Recall priority signal (yansu inversion): entries recalled in the
# last ``CORRECTION_RECALL_PRIORITY_DAYS`` are flagged in the prompt so
# the LLM sees them as "actively used" and the rationale carries a
# priority badge for the review UI. We do NOT auto-suppress proposals
# on recalled entries — that would silently keep bad data.

_AUDIT_SYSTEM = """\
You audit one existing memory or knowledge entry against the current
triage rules. Output JSON only:

{
  "action": "keep" | "archive",
  "confidence": <0..1>,
  "rationale": "<short reason, max 200 chars>"
}

## Decision rules

- "keep": entry IS still acceptable under the current rules. Default
  for entries that look like genuine, sustained user signal.
- "archive": entry should NOT have been accepted under the current
  rules (e.g. one-off observation, persona/vibe content, pure path
  inventory with no behaviour signal, third-party opinion about the
  user).

There is intentionally no "rewrite" action — the conversation interface
handles rephrasing. If an entry is wrong, archive; the user will
restate the correct version in conversation and triage will
re-accept it then.

## Conservative bias

The default is "keep". Only archive when there is clear evidence the
entry violates the current rules. When the entry is "actively used"
(see priority badge), require even stronger evidence to archive.

## Confidence calibration

- 0.90+ : pure noise (path inventory, single-incident observation, persona)
- 0.80–0.89 : likely noise, mild ambiguity
- 0.70–0.79 : leaning archive but real uncertainty
- below 0.70 : do not commit; just say keep

The runtime applies a confidence floor; verdicts below it are dropped
without persisting. So emitting accurate confidence matters — don't
inflate to push something across the floor.
"""

_AUDIT_USER_TEMPLATE = """\
## Entry under audit

- id: {entry_id}
- kind: {kind}
- {facet_label}: {facet_value}
- source: {source}
- synthesis_level: {synthesis_level}
- archived_reason: {archived_reason}
- version (= 1 + times user reinforced this entry): {version}
- recall_count_30d: {recall_count}{priority_note}

## Summary
{summary}

## Content (markdown)
{content}

Output the JSON object only. No prose, no markdown fences.
"""


# Long-tail protection threshold. Entries the user has explicitly reinforced
# at least this many times (counted via the version field, which increments
# on every triage 'update' action) are NEVER auto-archived by retriage —
# the LLM doesn't even get to see them. This is the bright line that keeps
# strong, repeatedly-stated preferences safe from any retroactive judgment
# call. Set to 3: version=1 (single mention) and version=2 (one
# reinforcement) still go through the audit; version>=3 is protected.
_VERSION_PROTECTION_THRESHOLD: int = 3


async def _m003_full_retriage_audit(
    store, llm_services, *, run_id, in_flight_entry_ids, config,
) -> AsyncIterator[CorrectionProposal]:
    """LLM-driven retriage of every active memory + knowledge entry.

    Skips entries the dream worker is currently consuming (in_flight) and
    entries already covered by an earlier migration's pending proposal
    for the same target — duplicate proposals across migrations would
    confuse the review UI.
    """
    if not llm_services:
        _logger.warning(
            "_m003_full_retriage_audit: no LLM services; halting (will retry next startup)"
        )
        return

    from . import _constants as C
    from ..utils import try_parse_json
    from src.infrastructure.llm_pool import call_with_fallback

    started_at = int(__import__("time").time())
    last_progress = await store.get_retriage_progress(3)
    resume_from = last_progress or ""

    for kind in (EntryKind.MEMORY, EntryKind.KNOWLEDGE):
        files_table = (
            "memory_files" if kind == EntryKind.MEMORY else "knowledge_files"
        )
        facet_col = "dimension" if kind == EntryKind.MEMORY else "category"
        # Snapshot at started_at so new candidates created after retriage
        # started don't get scanned (they'll be triaged under current
        # rules anyway).
        rows = await store._fetchall(
            f"SELECT id, {facet_col}, summary, source, synthesis_level, "
            f"       archived_reason, version, updated_at "
            f"FROM {files_table} "
            f"WHERE archived=0 AND updated_at <= ? AND id > ? "
            f"ORDER BY id ASC",
            (started_at, resume_from),
        )
        processed = 0
        for entry_id, facet, summary, source, synth_level, arch_reason, version, _ut in rows:
            if entry_id in in_flight_entry_ids:
                continue
            # Long-tail protection: entries the user has reinforced ≥2 times
            # (version >= 3) are skipped entirely. We don't even pay the LLM
            # cost to audit them — by definition the user keeps stating them,
            # so any "should we archive?" question is decided in advance:
            # NO. Cheaper than running the LLM and discarding the verdict.
            if int(version or 1) >= _VERSION_PROTECTION_THRESHOLD:
                continue

            # Build full content for the prompt.
            full_entry = (
                await store.get_memory_entry_full(entry_id)
                if kind == EntryKind.MEMORY
                else await store.get_knowledge_entry_full(entry_id)
            )
            if full_entry is None:
                continue

            recall_count = await store.count_recent_recalls(
                entry_id=entry_id, kind=kind.value,
                since_seconds=C.CORRECTION_RECALL_PRIORITY_DAYS * 24 * 3600,
            )
            priority_note = (
                "  (PRIORITY: recently recalled — require strong evidence to archive)"
                if recall_count > 0 else ""
            )

            user_msg = _AUDIT_USER_TEMPLATE.format(
                entry_id=entry_id,
                kind=kind.value,
                facet_label=facet_col,
                facet_value=facet or "?",
                source=source or "?",
                synthesis_level=int(synth_level or 0),
                archived_reason=arch_reason or "-",
                version=int(version or 1),
                recall_count=recall_count,
                priority_note=priority_note,
                summary=(summary or "")[:200],
                content=(full_entry.content or "")[:2500],
            )
            try:
                result = await call_with_fallback(
                    llm_services,
                    dict(
                        messages=[
                            {"role": "system", "content": _AUDIT_SYSTEM},
                            {"role": "user", "content": user_msg},
                        ],
                        json_mode=True,
                        max_tokens=600,
                    ),
                )
            except Exception:
                _logger.exception(
                    "retriage v3: LLM call failed for entry=%s; skipping",
                    entry_id[:8],
                )
                continue

            verdict = try_parse_json(result.content or "")
            if not isinstance(verdict, dict):
                continue
            action = str(verdict.get("action") or "keep").lower()
            if action != "archive":
                # "keep" or anything we don't recognise → leave entry alone.
                continue
            try:
                confidence = float(verdict.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))
            # Conversation-as-interface principle: proposals nobody can
            # review are dead data. Either auto-apply (confidence high
            # enough) or skip entirely. We never write a "pending"
            # proposal that just sits in the DB forever — if the LLM
            # isn't sure, the right answer is "leave the entry alone".
            if confidence < C.CORRECTION_HIGH_CONF_FLOOR:
                continue
            rationale = str(verdict.get("rationale") or "")[:200]
            if priority_note:
                rationale = f"[priority] {rationale}"

            yield CorrectionProposal(
                id="",
                kind=CorrectionKind.ARCHIVE,
                target_kind=kind,
                target_entry_id=entry_id,
                target_version=int(version),
                target_archived=False,
                payload={"rule": "full_retriage_audit_v1"},
                confidence=confidence,
                rule_version=3,
                parent_run_id=run_id,
                rationale=rationale,
                rationale_pii_scrubbed=False,
                status=CorrectionStatus.PENDING,
                created_at=0,
            )

            processed += 1
            if processed % C.CORRECTION_RETRIAGE_CHECKPOINT_EVERY == 0:
                try:
                    await store.set_retriage_progress(3, entry_id)
                except Exception:
                    _logger.exception("retriage v3: checkpoint write failed")
        # End of one kind — checkpoint at the last id so resume picks
        # up the next kind cleanly.
        if rows:
            await store.set_retriage_progress(3, rows[-1][0])
    # Migration done — clear progress marker so a future re-run starts fresh.
    await store.clear_retriage_progress(3)


# Append-only list. Each entry's ``version`` MUST be its 1-based index.
RULE_MIGRATIONS: List[RuleMigration] = [
    RuleMigration(
        version=1,
        name="l2_orphan_archive",
        is_llm=False,
        auto_apply_kinds={CorrectionKind.ARCHIVE},
        description=(
            "Archive raw entries that an active L2/L3 dream synthesis row "
            "already cites in source_entry_ids. Deterministic; auto-applies "
            "because the synthesis row is by construction a superset."
        ),
        audit=_m001_l2_orphan_archive,
    ),
    RuleMigration(
        version=2,
        name="path_inventory_audit",
        is_llm=False,
        auto_apply_kinds={CorrectionKind.ARCHIVE},
        description=(
            "Retroactively apply the v1.1.2 path-inventory guard to existing "
            "activity_observer INSIGHT memory. Auto-applies under high_conf "
            "tier because confidence=0.85 ≥ floor; the entries it targets "
            "are the same ones the live triage path now rejects."
        ),
        audit=_m002_path_inventory_audit,
    ),
    RuleMigration(
        version=3,
        name="full_retriage_audit_v1",
        is_llm=True,
        auto_apply_kinds={CorrectionKind.ARCHIVE},
        description=(
            "LLM full-corpus audit: re-judge every active entry against the "
            "current TRIAGE_SYSTEM. Auto-applies archives at confidence ≥ floor; "
            "rewrites stay as proposals (those are content edits, higher "
            "judgment bar). Entries with version >= 3 (user reinforced ≥2 "
            "times) are protected — the audit skips them so strong user "
            "signals never silently disappear."
        ),
        audit=_m003_full_retriage_audit,
    ),
]
