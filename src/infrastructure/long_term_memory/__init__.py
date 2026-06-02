"""LongTermMemory — cross-session memory + knowledge layer.

Lifecycle (bridge_main.py):
    ltm = await LongTermMemory.init(db_path=..., config_path=...)
    try:
        await stdio_bridge.run()
    finally:
        await ltm.shutdown()

Anywhere else:
    ltm = LongTermMemory.get()
    entries = await ltm.recall_memory(query, k=5)
    block = ltm.format_memory_block(entries)

Always-on policy
----------------
LongTermMemory is **always enabled** at the API level. There is no
``memory.enabled: false`` switch in user-facing config — disabling
memory was a footgun (one accidental toggle and an entire session of
collected preferences silently vanishes).

If the underlying SQLite cannot be opened (db corruption, permission
denied), ``init()`` returns a ``_NullLongTermMemory`` so the bridge
keeps booting; the user can fix or delete ``%USERPROFILE%\\HandQ\\memory.db``
without losing core flows. Same fallback if the user calls ``get()``
before ``init()`` completed.

All tunable parameters (intervals, k values, embedding choice, etc.)
live in :mod:`_constants` rather than the user's yaml — see that module
for the rationale.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional

import yaml

from . import _constants as C
from .candidates import (
    submit_manual,
    submit_post_commit,
    submit_session_complete,
    submit_user_turn,
)
from .embedding import EmbeddingProvider, from_config as _embedder_from_config
from .models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    Entry,
    EntryKind,
    KnowledgeCategory,
    MemoryDimension,
    TriageVerdict,
    VerdictAction,
)
from .pii import PIIFilter
from .recall import (
    LTM_BLOCK_DESCRIPTION as _LTM_DESC,
    LTM_BLOCK_HEADER as _LTM_HEADER,
    format_knowledge_block as _fmt_kn,
    format_memory_block as _fmt_mem,
    recall_knowledge_impl,
    recall_memory_impl,
)
from .recall_logger import RecallLogger
from .reranker import Reranker, from_config as _reranker_from_config
from .retriage_worker import RetriageWorker
from .store import SQLiteStore
from .triage import DreamWorker

_logger = logging.getLogger("handq.ltm")

__all__ = [
    "LongTermMemory",
    "Candidate",
    "CandidateSource",
    "CandidateStatus",
    "Entry",
    "EntryKind",
    "KnowledgeCategory",
    "MemoryDimension",
    "TriageVerdict",
    "VerdictAction",
    "submit_manual",
    "submit_post_commit",
    "submit_session_complete",
    "submit_user_turn",
]


class LongTermMemory:
    """Singleton facade. Exactly one instance per process."""

    _instance: Optional["LongTermMemory"] = None

    def __init__(
        self,
        store: SQLiteStore,
        embedder: EmbeddingProvider,
        reranker: Reranker,
        pii_filter: PIIFilter,
        config: dict,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._pii = pii_filter
        self._config = config
        self._dream_task: Optional[asyncio.Task] = None
        self._retriage_task: Optional[asyncio.Task] = None

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    async def init(
        cls,
        *,
        db_path: Path,
        config_path: Path,
    ) -> "LongTermMemory":
        if cls._instance is not None:
            return cls._instance

        # We still load yaml because the LLM section (API_KEY, role pools)
        # has to come from the user. The `memory:` section no longer
        # exists — every LTM-specific knob is in :mod:`_constants`.
        cfg = _load_config(config_path)

        try:
            store = await SQLiteStore.open(db_path)
        except Exception:
            _logger.exception(
                "LongTermMemory.init: failed to open SQLite at %s; "
                "falling back to null instance — recall will be empty "
                "and submits silently dropped until the next bridge "
                "restart with a working db", db_path,
            )
            inst = _NullLongTermMemory(cfg)  # type: ignore[assignment]
            cls._instance = inst  # type: ignore[assignment]
            return inst  # type: ignore[return-value]

        embedder = _embedder_from_config(cfg)
        reranker = _reranker_from_config(cfg)
        pii = PIIFilter()
        ltm = cls(store, embedder, reranker, pii, cfg)

        worker = DreamWorker(
            store=store, embedder=embedder, pii_filter=pii, config=cfg,
        )
        ltm._dream_task = asyncio.create_task(worker.run(), name="ltm-dream")

        # Spawn the one-shot RetriageWorker. It exits as soon as
        # triage_rules_version reaches the head of RULE_MIGRATIONS, so
        # there's no long-lived task to manage on the steady-state path.
        # We share the dream worker's helper LLM pool resolution by
        # constructing it lazily on first run (see RetriageWorker).
        retriage_helper_services = await _resolve_retriage_helpers(cfg)
        retriage = RetriageWorker(
            store=store,
            llm_services=retriage_helper_services,
            pii_filter=pii,
            config=cfg,
        )
        ltm._retriage_task = asyncio.create_task(
            retriage.run(), name="ltm-retriage",
        )

        cls._instance = ltm
        _logger.info(
            "LongTermMemory initialised: db=%s embedder=%s reranker=%s",
            db_path, embedder.provider, reranker.provider,
        )
        return ltm

    @classmethod
    def get(cls) -> "LongTermMemory":
        if cls._instance is None:
            # Soft-fail: the bridge entrypoint should always init() first, but
            # tests / standalone tooling may import without init. Returning a
            # null instance keeps callers honest without forcing every call
            # site to wrap in try/except.
            _logger.debug("LongTermMemory.get() before init; returning null instance")
            return _NullLongTermMemory({})  # type: ignore[return-value]
        return cls._instance

    async def shutdown(self) -> None:
        if self._dream_task and not self._dream_task.done():
            self._dream_task.cancel()
            try:
                # Bound the wait — a wedged LLM HTTP call inside the
                # worker should NOT keep the bridge from exiting. 5s
                # is generous for a normal cancel path (just stops
                # the asyncio.sleep loop); a longer wait would only
                # help if we wanted to drain in-flight LLM calls,
                # which we don't.
                await asyncio.wait_for(self._dream_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                _logger.warning(
                    "dream task did not finish within 5s of cancel; "
                    "leaving as background"
                )
            except Exception:
                _logger.exception("dream task shutdown error")
        if self._retriage_task and not self._retriage_task.done():
            # RetriageWorker is one-shot but may still be mid-LLM call
            # at shutdown time. Cancel + bounded wait, same envelope as
            # the dream task. Pending proposals it already wrote stay
            # in the DB (committed); on next startup the migration
            # picks up where it left off via retriage_progress_v{N}.
            self._retriage_task.cancel()
            try:
                await asyncio.wait_for(self._retriage_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                _logger.warning(
                    "retriage task did not finish within 5s of cancel; "
                    "leaving as background"
                )
            except Exception:
                _logger.exception("retriage task shutdown error")
        # Final flush of recall_log buffer so the very last few hits
        # (between the dream tick and shutdown) aren't lost.
        try:
            n = await RecallLogger.get().flush(self._store)
            if n:
                _logger.info("shutdown: flushed %d recall_log rows", n)
        except Exception:
            _logger.exception("recall_log final flush error")
        # If the embedder holds an open httpx client (HttpApiEmbedder),
        # close it so the bridge exits cleanly. Other providers ignore.
        aclose = getattr(self._embedder, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                _logger.exception("embedder aclose error")
        try:
            await self._store.close()
        except Exception:
            _logger.exception("ltm store close error")
        _logger.info("LongTermMemory shut down")
        type(self)._instance = None

    # ── Write API ───────────────────────────────────────────────────────────

    async def submit_candidate(
        self,
        *,
        source: str,
        raw_text: str,
        source_ref: Optional[str] = None,
        hint: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        if self._pii.has_secret(raw_text):
            _logger.info(
                "submit_candidate dropped at boundary (sensitive) source=%s ref=%s",
                source, source_ref,
            )
            return ""
        try:
            return await self._store.insert_candidate(
                source=source, raw_text=raw_text,
                source_ref=source_ref, hint=hint,
                metadata=metadata or {},
            )
        except Exception:
            _logger.exception("submit_candidate failed source=%s", source)
            return ""

    async def archive(
        self, *, entry_id: str, kind: EntryKind, reason: str,
    ) -> None:
        try:
            if kind == EntryKind.MEMORY:
                await self._store.archive_memory_entry(entry_id, reason=reason)
            elif kind == EntryKind.KNOWLEDGE:
                await self._store.archive_knowledge_entry(entry_id, reason=reason)
            else:  # PROCEDURE — P6, ignore for now
                _logger.warning("archive: unsupported kind %s", kind)
        except Exception:
            _logger.exception("archive failed entry_id=%s kind=%s", entry_id, kind)

    # ── Read API ────────────────────────────────────────────────────────────

    async def recall_memory(
        self,
        query: str,
        *,
        dimension: Optional[MemoryDimension] = None,
        k: int = C.RECALL_MEMORY_K,
        min_score: float = C.RECALL_MIN_SCORE,
        rerank: bool = True,
    ) -> List[Entry]:
        try:
            return await recall_memory_impl(
                self._store, self._embedder, self._reranker, query,
                dimension=dimension, k=k, min_score=min_score, rerank=rerank,
            )
        except Exception:
            _logger.exception("recall_memory failed")
            return []

    async def recall_knowledge(
        self,
        query: str,
        *,
        category: Optional[KnowledgeCategory] = None,
        k: int = C.RECALL_KNOWLEDGE_K,
        min_score: float = C.RECALL_MIN_SCORE,
        rerank: bool = True,
    ) -> List[Entry]:
        try:
            return await recall_knowledge_impl(
                self._store, self._embedder, self._reranker, query,
                category=category, k=k, min_score=min_score, rerank=rerank,
            )
        except Exception:
            _logger.exception("recall_knowledge failed")
            return []

    def format_memory_block(self, entries: List[Entry]) -> str:
        return _fmt_mem(entries)

    def format_knowledge_block(self, entries: List[Entry]) -> str:
        return _fmt_kn(entries)

    async def format_context_block(
        self,
        query: str,
        *,
        memory_k: int = C.RECALL_MEMORY_K,
        knowledge_k: int = C.RECALL_KNOWLEDGE_K,
        memory_dimension: Optional[MemoryDimension] = None,
        knowledge_category: Optional[KnowledgeCategory] = None,
        min_score: float = C.RECALL_MIN_SCORE,
        rerank: bool = True,
    ) -> str:
        """Recall both tracks against *query* and render a single context block.

        One-shot helper for callers that just want "the long-term context
        string for this query" — keeps the memory/knowledge split inside
        LongTermMemory rather than leaking the two tracks into Planner /
        Receptionist signatures.

        ``rerank`` default True (full quality, ~3s LLM-rerank cost). Set
        False on hot per-message paths (receptionist) where precision
        matters less than latency.

        Returns "" when both tracks are empty so the caller can do a plain
        truthy check before injecting.
        """
        mem_entries = await self.recall_memory(
            query, dimension=memory_dimension, k=memory_k,
            min_score=min_score, rerank=rerank,
        )
        kn_entries = await self.recall_knowledge(
            query, category=knowledge_category, k=knowledge_k,
            min_score=min_score, rerank=rerank,
        )
        parts: List[str] = []
        mem_block = _fmt_mem(mem_entries)
        if mem_block:
            parts.append(mem_block)
        kn_block = _fmt_kn(kn_entries)
        if kn_block:
            parts.append(kn_block)
        if not parts:
            return ""
        return (
            f"{_LTM_HEADER}\n"
            f"{_LTM_DESC}\n"
            + "\n".join(parts) + "\n"
        )

    # ── Admin / debug ───────────────────────────────────────────────────────

    async def list_active_memory(
        self, *, dimension: Optional[MemoryDimension] = None, limit: int = 50,
    ) -> List[Entry]:
        return await self._store.list_memory_entries(
            dimension=dimension, archived=False, limit=limit,
        )

    async def list_active_knowledge(
        self, *, category: Optional[KnowledgeCategory] = None, limit: int = 50,
    ) -> List[Entry]:
        return await self._store.list_knowledge_entries(
            category=category, archived=False, limit=limit,
        )

    async def list_pending_candidates(self, limit: int = 20) -> List[Candidate]:
        return await self._store.list_candidates(status="pending", limit=limit)

    async def triage_stats(self) -> dict:
        """Return per-source acceptance / rejection counts for observability.

        Lets the user see at a glance whether the triage bar is too strict
        (most things rejected) or too loose (most things accepted). Read-only
        — does not mutate any tables.

        Returns shape:
            {
              "by_source": {
                "session_complete": {"pending": int, "accepted_*": int, ...},
                "receptionist_turn": {...},
                ...
              },
              "totals": {"pending": int, "accepted": int, "rejected": int, "failed": int}
            }
        """
        from .models import CandidateStatus
        out_by_source: dict = {}
        totals = {"pending": 0, "accepted": 0, "rejected": 0, "failed": 0}
        # One pass per status — each is bounded by the table size and
        # status is indexed.
        for status in CandidateStatus:
            cands = await self._store.list_candidates(
                status=status.value, limit=10_000,
            )
            for c in cands:
                src_bucket = out_by_source.setdefault(c.source, {})
                src_bucket[status.value] = src_bucket.get(status.value, 0) + 1
                if status == CandidateStatus.PENDING or status == CandidateStatus.TRIAGING:
                    totals["pending"] += 1
                elif status in (
                    CandidateStatus.ACCEPTED_BOTH,
                    CandidateStatus.ACCEPTED_MEMORY,
                    CandidateStatus.ACCEPTED_KNOWLEDGE,
                ):
                    totals["accepted"] += 1
                elif status == CandidateStatus.REJECTED:
                    totals["rejected"] += 1
                elif status == CandidateStatus.FAILED:
                    totals["failed"] += 1
        return {"by_source": out_by_source, "totals": totals}


# ── Null instance (disabled / pre-init) ─────────────────────────────────────

class _NullLongTermMemory(LongTermMemory):
    """No-op stand-in. All read/write methods short-circuit to empty values."""

    def __init__(self, config: dict) -> None:  # noqa: D401  (intentional shadow)
        # Intentionally skip super().__init__ — there is no store, embedder,
        # reranker, or pii filter behind this instance. The base class never
        # reads those attrs without being overridden here.
        self._config = config
        self._store = None        # type: ignore[assignment]
        self._embedder = None     # type: ignore[assignment]
        self._reranker = None     # type: ignore[assignment]
        self._pii = None          # type: ignore[assignment]
        self._dream_task = None

    async def submit_candidate(self, **_) -> str:  # type: ignore[override]
        return ""

    async def archive(self, **_) -> None:  # type: ignore[override]
        return None

    async def recall_memory(self, *_args, **_kwargs) -> List[Entry]:  # type: ignore[override]
        return []

    async def recall_knowledge(self, *_args, **_kwargs) -> List[Entry]:  # type: ignore[override]
        return []

    def format_memory_block(self, _entries) -> str:  # type: ignore[override]
        return ""

    def format_knowledge_block(self, _entries) -> str:  # type: ignore[override]
        return ""

    async def format_context_block(self, *_args, **_kwargs) -> str:  # type: ignore[override]
        return ""

    async def list_active_memory(self, **_) -> List[Entry]:  # type: ignore[override]
        return []

    async def list_active_knowledge(self, **_) -> List[Entry]:  # type: ignore[override]
        return []

    async def list_pending_candidates(self, **_) -> List[Candidate]:  # type: ignore[override]
        return []

    async def triage_stats(self) -> dict:  # type: ignore[override]
        return {"by_source": {}, "totals": {"pending": 0, "accepted": 0, "rejected": 0, "failed": 0}}

    async def shutdown(self) -> None:  # type: ignore[override]
        type(self)._instance = None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_config(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        _logger.warning("config not found at %s; LTM running with defaults", path)
        return {}
    except Exception:
        _logger.exception("failed to read config %s; LTM running with defaults", path)
        return {}


async def _resolve_retriage_helpers(cfg: dict) -> List:
    """Build the LLM service list for the RetriageWorker.

    Independent of DreamWorker's helper pool because the two have
    different quality bars: DreamWorker's triage is per-candidate noise
    classification (receptionist-tier OK), while retriage re-judges
    durable entries that the user has been living with — we want the
    strongest model the bridge has.

    Tier choice driven by ``CORRECTION_RETRIAGE_LLM_TIER`` (defaults to
    "agent"). Returns ``[]`` on missing API_KEY / config so the worker's
    LLM-based migrations halt the chain rather than silently no-op.
    """
    try:
        from src.infrastructure.role_resolver import resolve_role_models
        from src.infrastructure.anthropic_streaming_service import (
            AnthropicStreamingService,
        )
    except Exception:
        _logger.exception("retriage: failed to import LLM stack")
        return []
    llm_cfg = cfg.get("llm") or {}
    api_key = llm_cfg.get("API_KEY")
    if not api_key:
        _logger.warning("retriage: llm.API_KEY missing; LLM migrations will halt")
        return []
    roles = resolve_role_models(llm_cfg)
    models = list(roles.get(C.CORRECTION_RETRIAGE_LLM_TIER) or [])
    if not models:
        _logger.warning(
            "retriage: tier=%s has no models; LLM migrations will halt",
            C.CORRECTION_RETRIAGE_LLM_TIER,
        )
        return []
    return [
        AnthropicStreamingService(model=m, api_key=api_key) for m in models
    ]
