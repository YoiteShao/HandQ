"""One-shot RetriageWorker — runs rule migrations on startup.

Lifecycle
---------
Spawned as a regular asyncio.Task by ``LongTermMemory.init`` alongside
the (long-running) ``DreamWorker``. RetriageWorker terminates as soon
as ``triage_rules_version`` reaches the head of ``RULE_MIGRATIONS``.

Why not piggyback on DreamWorker
--------------------------------
DreamWorker is an infinite loop with adaptive backoff; folding a
one-shot pass into it would entangle two unrelated lifecycles and force
the cleanup pass behind the worker's idle backoff. Independent task is
simpler, observably distinct in logs, and trivially cancelled at
shutdown.

Concurrency
-----------
RetriageWorker shares ``SQLiteStore._write_lock`` with everything else.
Inside one rule migration's ``audit()`` we additionally:

1. Skip entries whose ids appear in ``list_in_flight_dream_run_sources``
   so a migration can't archive a source that an active L2/L3 synthesis
   is about to point at.
2. Use ``apply_archive_correction`` etc., which re-check
   ``target_version`` + ``target_archived`` inside the apply transaction
   — if DreamWorker raced us, the proposal flips to ``stale`` and a
   subsequent cycle can issue a fresh one.

Resumability
------------
Per-migration progress is stored as a ``memory_meta`` key
``retriage_progress_v{N}=last_entry_id``. The migration's audit() is
expected to read the key on entry and skip already-processed ids. This
keeps a long LLM-driven pass cheap to resume after a crash / restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, List, Optional

from . import _constants as C
from .models import (
    CorrectionKind,
    CorrectionProposal,
    CorrectionStatus,
)
from .pii import PIIFilter
from .rule_migrations import RULE_MIGRATIONS, RuleMigration

_logger = logging.getLogger("handq.ltm.retriage")


class RetriageWorker:
    """One-shot rule migration runner.

    Parameters
    ----------
    store
        :class:`SQLiteStore` (already opened by LongTermMemory.init).
    llm_services
        Helper LLM service list — same shape as DreamWorker's
        ``self._helper_services``. Only used by migrations with
        ``is_llm=True``. May be empty; a migration that needs LLM
        will fail-soft (logs + halts the chain at this version).
    pii_filter
        Used to scrub LLM-emitted ``rationale`` before persisting.
    config
        Full handq_config.yaml dict — passed through to rule migrations
        that need it (e.g. embedding model for re-clustering).
    """

    def __init__(
        self,
        *,
        store,
        llm_services: List[Any],
        pii_filter: PIIFilter,
        config: dict,
    ) -> None:
        self._store = store
        self._llm = llm_services
        self._pii = pii_filter
        self._config = config

    async def run(self) -> None:
        try:
            cur_v = int(await self._store.get_meta("triage_rules_version") or "0")
        except Exception:
            _logger.exception("retriage: failed to read triage_rules_version; aborting")
            return

        target_v = len(RULE_MIGRATIONS)
        if cur_v >= target_v:
            _logger.info(
                "retriage: nothing to do (rule_version=%d at head)", cur_v,
            )
            return

        _logger.info(
            "retriage: rule_version %d → %d; %d migrations pending",
            cur_v, target_v, target_v - cur_v,
        )

        for mig in RULE_MIGRATIONS:
            if mig.version <= cur_v:
                continue
            if mig.is_llm and not self._llm:
                _logger.warning(
                    "retriage: migration v%d (%s) needs LLM but none configured; "
                    "halting chain (will retry next startup)",
                    mig.version, mig.name,
                )
                break
            try:
                run_id = await self._run_migration(mig)
                await self._store.set_meta(
                    "triage_rules_version", str(mig.version),
                )
                _logger.info(
                    "retriage: migration v%d (%s) complete (run=%s)",
                    mig.version, mig.name, run_id[:8] if run_id else "-",
                )
            except asyncio.CancelledError:
                _logger.info("retriage: cancelled")
                return
            except Exception:
                _logger.exception(
                    "retriage: migration v%d (%s) failed; halting chain",
                    mig.version, mig.name,
                )
                # Don't bump version → next startup retries this migration.
                # Pending proposals it already wrote stay queued (they're
                # idempotent: re-running won't double-insert because
                # migrations track their own progress markers).
                break

    async def _run_migration(self, mig: RuleMigration) -> str:
        run_id = str(uuid.uuid4())
        in_flight = set(
            await self._store.list_in_flight_dream_run_sources()
        )
        n_proposed = 0
        n_auto_applied = 0
        n_stale_on_apply = 0

        # Pass run_id + in_flight set + worker handle through kwargs so
        # the migration's audit() doesn't need to know about
        # RetriageWorker internals.
        kwargs = {
            "run_id": run_id,
            "in_flight_entry_ids": in_flight,
            "config": self._config,
        }

        async for proposal in mig.audit(self._store, self._llm, **kwargs):
            try:
                # PII scrub on rationale before persistence.
                if proposal.rationale and self._pii.has_secret(proposal.rationale):
                    proposal.rationale = self._pii.redact(proposal.rationale)
                    proposal.rationale_pii_scrubbed = True

                pid = await self._store.insert_correction_proposal(
                    kind=proposal.kind,
                    target_kind=proposal.target_kind,
                    target_entry_id=proposal.target_entry_id,
                    target_version=proposal.target_version,
                    target_archived=proposal.target_archived,
                    payload=proposal.payload,
                    confidence=proposal.confidence,
                    rule_version=mig.version,
                    parent_run_id=run_id,
                    rationale=proposal.rationale,
                    rationale_pii_scrubbed=proposal.rationale_pii_scrubbed,
                )
                n_proposed += 1

                if self._should_auto_apply(mig, proposal):
                    ok = await self._apply_proposal(pid, proposal.kind)
                    if ok:
                        n_auto_applied += 1
                    else:
                        n_stale_on_apply += 1
            except Exception:
                _logger.exception(
                    "retriage: failed processing proposal for entry=%s",
                    proposal.target_entry_id,
                )

        _logger.info(
            "retriage v%d (%s): proposed=%d auto_applied=%d stale=%d",
            mig.version, mig.name, n_proposed, n_auto_applied, n_stale_on_apply,
        )
        return run_id

    def _should_auto_apply(
        self, mig: RuleMigration, proposal: CorrectionProposal,
    ) -> bool:
        """Combine migration's declared auto_apply_kinds with the global
        runtime tier setting. Both must agree → auto-apply. Either says
        no → leave as ``pending`` for explicit review.
        """
        if proposal.kind not in mig.auto_apply_kinds:
            return False
        tier = C.CORRECTION_AUTO_APPLY_TIER
        if tier == C.CORRECTION_TIER_PROPOSAL_ONLY:
            return False
        if tier == C.CORRECTION_TIER_DETERMINISTIC:
            return True  # the migration claims this is deterministic
        if tier == C.CORRECTION_TIER_HIGH_CONF:
            if proposal.confidence is None:
                return False
            return proposal.confidence >= C.CORRECTION_HIGH_CONF_FLOOR
        return False

    async def _apply_proposal(self, pid: str, kind: CorrectionKind) -> bool:
        # ARCHIVE is the only kind in production: rewrites and merges
        # were UI-driven actions that we removed when committing to
        # "conversation as interface". Future kinds: add a branch here.
        if kind == CorrectionKind.ARCHIVE:
            return await self._store.apply_archive_correction(
                pid, resolved_by="auto_deterministic",
            )
        return False
