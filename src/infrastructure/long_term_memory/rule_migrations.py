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

from dataclasses import dataclass
from typing import AsyncIterator, Callable, List, Set

from .models import CorrectionKind, CorrectionProposal


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


# Append-only list. Each entry's ``version`` MUST be its 1-based index.
#
# Currently empty. The original v1-v3 migrations targeted the pre-LTM-2.0
# ``memory_files``/``knowledge_files`` tables, which the force-clean upgrade
# dropped — every user-facing DB now starts fresh at the v5 schema, so there
# was nothing for those passes to replay and they have been removed. New
# migrations against the ``obs_*``/``mem_*``/``ent_*`` schema get appended
# here as needed.
RULE_MIGRATIONS: List[RuleMigration] = []
