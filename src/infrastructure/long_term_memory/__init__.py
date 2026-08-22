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
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

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
    format_identity_block as _fmt_id,
    format_knowledge_block as _fmt_kn,
    format_known_entities_block as _fmt_ent,
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
    # Set when init() begins, fired when init() completes. Lets callers
    # distinguish "no init yet" (event is None) from "init in progress"
    # (event exists but not set) from "ready" (event is set).
    _init_event: Optional[asyncio.Event] = None

    def __init__(
        self,
        store: SQLiteStore,
        embedder: EmbeddingProvider,
        reranker: Reranker,
        pii_filter: PIIFilter,
        config: dict,
        emit: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._pii = pii_filter
        self._config = config
        # Outbound notification channel into the chat feed. Injected by the
        # bridge at init time (stdio_bridge._emit); None in tests / headless
        # runs. A generic seam for background workers to push a hint to the
        # user. No live producer today — its only past user, the retired
        # skill-proposal staging hint, is gone now that skills are minted
        # directly as disabled files.
        self._emit = emit
        self._dream_task: Optional[asyncio.Task] = None
        self._retriage_task: Optional[asyncio.Task] = None
        # Flipped True at the very start of shutdown() so any in-flight
        # submit_candidate / archive call returning from the coordinator's
        # per-message capture can detect the closed state and skip the write
        # rather than hitting "database is closed" on the SQLite handle.
        self._shutting_down: bool = False
        # IDENTITY block is recomputed on every recall today. The dream
        # worker can't write a new IDENTITY entry faster than once per
        # DREAM_INTERVAL_MIN_SEC (60s), so a TTL-cached pair
        # (rendered_block, frozenset_of_ids) serves every caller within
        # that window from memory. ``archive()`` nulls the cache on the
        # explicit-removal path so user-initiated changes show up
        # without waiting for the TTL.
        self._identity_cache: Optional[Tuple[str, frozenset]] = None
        self._identity_cache_ts: float = 0.0
        # Known-entities (principal graph) render cache — same slow-change
        # rationale as identity; reuses IDENTITY_CACHE_TTL_SEC. No id-set is
        # needed (principals aren't in mem recall, so there's nothing to dedup).
        self._known_entities_cache: Optional[str] = None
        self._known_entities_cache_ts: float = 0.0
        # Guards the render-cache check→fetch→write sequences below (N1). The
        # LTM singleton is shared by every session; with concurrent per-session
        # dispatch two recalls can interleave a stale write over an
        # ``archive()`` invalidation. Holding this across the check, the store
        # read, and the write makes each refresh atomic w.r.t. invalidation:
        # archive's null can only land before or after a complete refresh,
        # never mid-flight, so the visible cache only flips between consistent
        # snapshots. Contention is negligible — both caches are TTL'd and
        # change slowly, so the guarded store read is rare.
        self._cache_lock = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    async def init(
        cls,
        *,
        db_path: Path,
        config_path: Path,
        emit: Optional[Callable[[dict], None]] = None,
    ) -> "LongTermMemory":
        if cls._instance is not None:
            return cls._instance

        # Mark init in progress so concurrent get_async() callers can wait
        # rather than picking up the null fallback. Only the first caller
        # creates the event; subsequent concurrent init() calls await it.
        if cls._init_event is None:
            cls._init_event = asyncio.Event()
        elif not cls._init_event.is_set():
            await cls._init_event.wait()
            if cls._instance is not None:
                return cls._instance

        # We still load yaml because the LLM section (API_KEY, model pools)
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
            cls._init_event.set()
            return inst  # type: ignore[return-value]

        # One-shot cleanup: archive entries whose dimension has been removed
        # from the enum (see _constants.LEGACY_DIMENSIONS). Deterministic +
        # idempotent; runs before DreamWorker spawns so no race with L2/L3.
        try:
            from . import _constants as _C
            n_archived = await store.archive_legacy_dimensions(_C.LEGACY_DIMENSIONS)
            if n_archived:
                _logger.info(
                    "Archived %d entries with legacy dimensions %s",
                    n_archived, _C.LEGACY_DIMENSIONS,
                )
        except Exception:
            _logger.warning("Legacy dimension archive failed", exc_info=True)

        embedder = _embedder_from_config(cfg)
        reranker = _reranker_from_config(cfg)
        pii = PIIFilter()
        ltm = cls(store, embedder, reranker, pii, cfg, emit=emit)

        worker = DreamWorker(
            store=store, embedder=embedder, pii_filter=pii, config=cfg,
            emit=emit,
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

        # LTM 2.0 observation pipeline workers — spawned alongside the dream
        # worker. SessionAggregator groups raw obs_snapshots into sessions;
        # ArcAggregator groups sessions into activity arcs (cut on 20min idle);
        # SemanticExtractor LLM-abstracts each closed arc into a semantic event
        # that DreamWorker then promotes to mem_entries.
        try:
            from .session_aggregator import SessionAggregator
            session_agg = SessionAggregator(store=store)
            ltm._session_agg = session_agg
            ltm._session_agg_task = asyncio.create_task(
                session_agg.run(), name="ltm-session-aggregator",
            )
        except Exception:
            _logger.exception(
                "SessionAggregator failed to start; obs_snapshots will not be aggregated",
            )
            ltm._session_agg = None
            ltm._session_agg_task = None

        try:
            from .arc_aggregator import ArcAggregator
            arc_agg = ArcAggregator(store=store)
            ltm._arc_agg = arc_agg
            ltm._arc_agg_task = asyncio.create_task(
                arc_agg.run(), name="ltm-arc-aggregator",
            )
        except Exception:
            _logger.exception(
                "ArcAggregator failed to start; sessions will not be grouped into arcs",
            )
            ltm._arc_agg = None
            ltm._arc_agg_task = None

        try:
            from .semantic_extractor import SemanticExtractor
            sem_extractor = SemanticExtractor(
                store=store,
                llm_services=retriage_helper_services or None,
                pii_filter=pii,
            )
            ltm._semantic_extractor = sem_extractor
            ltm._semantic_extractor_task = asyncio.create_task(
                sem_extractor.run(), name="ltm-semantic-extractor",
            )
        except Exception:
            _logger.exception(
                "SemanticExtractor failed to start; closed arcs will not be abstracted",
            )
            ltm._semantic_extractor = None
            ltm._semantic_extractor_task = None

        cls._instance = ltm
        cls._init_event.set()
        _logger.info(
            "LongTermMemory initialised: db=%s embedder=%s reranker=%s",
            db_path, embedder.provider, reranker.provider,
        )
        # Baseline principals (machines from ~/.ssh/handq_*.yaml, self from
        # git config user.email). Fire-and-forget — failures must not block
        # bridge boot.
        try:
            from .principals import populate_baseline
            asyncio.create_task(
                populate_baseline(store, working_directory=None),
                name="ltm-principals-baseline",
            )
        except Exception:
            _logger.exception("principals baseline scheduling failed")
        return ltm

    @classmethod
    def get(cls) -> "LongTermMemory":
        if cls._instance is None:
            # Soft-fail: the bridge entrypoint should always init() first, but
            # tests / standalone tooling may import without init. Returning a
            # null instance keeps callers honest without forcing every call
            # site to wrap in try/except.
            if cls._init_event is not None and not cls._init_event.is_set():
                _logger.warning(
                    "LongTermMemory.get() called while init() in progress; "
                    "returning null instance — submissions will be dropped. "
                    "Use get_async() in async paths to wait for ready state.",
                )
            else:
                _logger.debug("LongTermMemory.get() before init; returning null instance")
            return _NullLongTermMemory({})  # type: ignore[return-value]
        return cls._instance

    @classmethod
    async def get_async(cls, *, timeout: float = 30.0) -> "LongTermMemory":
        """Async variant of get() that waits for in-progress init() to finish.

        Closes the startup-race window where a caller hits get() between
        ``init()`` starting and ``cls._instance`` being assigned, getting a
        ``_NullLongTermMemory`` and silently dropping the submission.

        Returns the null instance if init was never started (test / standalone
        path) or if the wait times out.
        """
        if cls._instance is not None:
            return cls._instance
        if cls._init_event is None:
            _logger.debug("get_async() before init; returning null instance")
            return _NullLongTermMemory({})  # type: ignore[return-value]
        try:
            await asyncio.wait_for(cls._init_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _logger.warning(
                "get_async() waited %.1fs for init() but it never completed; "
                "returning null instance", timeout,
            )
            return _NullLongTermMemory({})  # type: ignore[return-value]
        return cls._instance if cls._instance is not None else _NullLongTermMemory({})  # type: ignore[return-value]

    async def shutdown(self) -> None:
        # Set the flag FIRST so any concurrent submit_candidate / archive
        # call sees it and bails out before touching the store. This closes
        # the "submit during shutdown" data-loss window where the SQLite
        # connection has already been closed by close() but the caller is
        # mid-await on insert_candidate.
        self._shutting_down = True
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
        # LTM 2.0 observation pipeline workers — same cancel + bounded wait.
        for task_attr, label in (
            ("_session_agg_task", "session_aggregator"),
            ("_arc_agg_task", "arc_aggregator"),
            ("_semantic_extractor_task", "semantic_extractor"),
        ):
            task = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    _logger.warning(
                        "%s task did not finish within 5s of cancel", label,
                    )
                except Exception:
                    _logger.exception("%s task shutdown error", label)
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
        if self._shutting_down:
            _logger.warning(
                "submit_candidate dropped (shutting down) source=%s ref=%s",
                source, source_ref,
            )
            return ""
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
        if self._shutting_down:
            _logger.warning(
                "archive dropped (shutting down) entry_id=%s kind=%s",
                entry_id, kind,
            )
            return
        try:
            if kind == EntryKind.MEMORY:
                await self._store.archive_memory_entry(entry_id, reason=reason)
                # IDENTITY entries are a subset of MEMORY. Invalidating on
                # every memory archive is cheaper than reading the row to
                # check its dimension first — the rebuild is one bounded
                # SQLite query against an indexed column. Null UNDER the cache
                # lock (N1) so it can't land between a concurrent refresh's
                # store read and its write — that would otherwise leave the
                # just-archived entry visible in the cache until the TTL.
                async with self._cache_lock:
                    self._identity_cache = None
                    self._identity_cache_ts = 0.0
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
        current_frame: Optional[dict] = None,
    ) -> List[Entry]:
        try:
            return await recall_memory_impl(
                self._store, self._embedder, self._reranker, query,
                dimension=dimension, k=k, min_score=min_score, rerank=rerank,
                current_frame=current_frame,
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
        current_frame: Optional[dict] = None,
    ) -> List[Entry]:
        try:
            return await recall_knowledge_impl(
                self._store, self._embedder, self._reranker, query,
                category=category, k=k, min_score=min_score, rerank=rerank,
                current_frame=current_frame,
            )
        except Exception:
            _logger.exception("recall_knowledge failed")
            return []

    def format_memory_block(self, entries: List[Entry]) -> str:
        return _fmt_mem(entries)

    def format_knowledge_block(self, entries: List[Entry]) -> str:
        return _fmt_kn(entries)

    async def _get_identity_cached(self) -> Tuple[str, frozenset]:
        """Return ``(rendered_xml_block, frozenset_of_entry_ids)``.

        Cached for :data:`_constants.IDENTITY_CACHE_TTL_SEC`; on miss,
        re-reads identity entries from the store and renders the block.
        Returning both the rendered string AND the id set lets
        :meth:`format_context_block` skip the per-call SQLite read AND
        the dedup-loop re-derivation of ids — both served from cache.

        Cache is invalidated by:
          - elapsed TTL (covers dream-worker writes within ~one tick)
          - explicit :meth:`archive` of any memory entry (covers user-
            initiated IDENTITY removal without waiting for TTL)
        """
        now = time.monotonic()
        cached = self._identity_cache
        if cached is not None and (
            now - self._identity_cache_ts
        ) < C.IDENTITY_CACHE_TTL_SEC:
            return cached
        async with self._cache_lock:
            # Re-check under the lock: a concurrent recall may have refreshed
            # while we awaited the lock (N1 double-checked locking).
            now = time.monotonic()
            if self._identity_cache is not None and (
                now - self._identity_cache_ts
            ) < C.IDENTITY_CACHE_TTL_SEC:
                return self._identity_cache
            identity_entries = await self._store.list_memory_entries(
                dimension=MemoryDimension.IDENTITY, archived=False,
                limit=C.IDENTITY_MAX_ENTRIES,
            )
            block = _fmt_id(identity_entries)
            ids = frozenset(e.id for e in identity_entries)
            self._identity_cache = (block, ids)
            self._identity_cache_ts = now
            return self._identity_cache

    async def _get_known_entities_cached(self) -> str:
        """Return the rendered ``<known-entities>`` block, TTL-cached.

        Mirrors :meth:`_get_identity_cached` but returns only the string —
        principals aren't part of memory recall, so there's no id-set to
        dedup against. Cached for :data:`_constants.IDENTITY_CACHE_TTL_SEC`
        (principals change only at boot / post-commit, both slow); worst-case
        staleness is one TTL window.
        """
        now = time.monotonic()
        cached = self._known_entities_cache
        if cached is not None and (
            now - self._known_entities_cache_ts
        ) < C.IDENTITY_CACHE_TTL_SEC:
            return cached
        async with self._cache_lock:
            now = time.monotonic()
            if self._known_entities_cache is not None and (
                now - self._known_entities_cache_ts
            ) < C.IDENTITY_CACHE_TTL_SEC:
                return self._known_entities_cache
            rows = await self._store.list_principals(limit=C.KNOWN_ENTITIES_MAX)
            block = _fmt_ent(rows)
            self._known_entities_cache = block
            self._known_entities_cache_ts = now
            return block

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
        include_identity: bool = True,
        include_known_entities: bool = True,
        include_header: bool = True,
        current_frame: Optional[dict] = None,
    ) -> str:
        """Recall both tracks against *query* and render a single context block.

        One-shot helper for callers that just want "the long-term context
        string for this query" — keeps the memory/knowledge split inside
        LongTermMemory rather than leaking the two tracks into caller
        signatures.

        ``rerank`` default True (full quality, ~3s LLM-rerank cost). Set
        False on hot per-message paths (INTENT) where precision matters
        less than latency.

        ``include_identity`` defaults True so the IDENTITY directive block is
        injected alongside the memory + knowledge recall. Set False when
        IDENTITY is already present in the same prompt's other context
        sections (e.g. PersistentAgent's stagnation refresh — IDENTITY is
        in ``_current_ltm_block`` from item-start, no need to repeat it).

        ``include_known_entities`` defaults True so the ``<known-entities>``
        block (people / machines / projects from the principal graph) is
        injected unconditionally, like identity. It is frame-agnostic (not
        os-filtered — SSH machines and people are cross-environment). Set
        False to suppress it (e.g. when already present elsewhere in the prompt).

        ``include_header`` defaults True (adds the ``[Long-Term Context]``
        wrapper + description prefix). Set False when the caller is going to
        concatenate multiple recall blocks under a single shared header.

        ``current_frame`` (LTM 2.0): dict of {os, host, confidence, ...}
        describing the caller's execution environment. When set, recall
        filters insight entries whose frame_os doesn't match (frame-agnostic
        AGENTIC / KNOWLEDGE / IDENTITY entries always pass through). Pass
        None to skip frame filtering (back-compat default).

        Returns "" when all tracks are empty so the caller can do a plain
        truthy check before injecting.
        """
        if include_identity:
            id_block, id_ids = await self._get_identity_cached()
        else:
            id_block, id_ids = "", frozenset()
        # Run the two tracks concurrently. Each track's rerank=True path makes
        # its own LLM rerank call; awaiting them sequentially would stack two
        # LLM round-trips on the caller's critical path. gather collapses
        # that to one round-trip of wall-clock.
        mem_entries, kn_entries = await asyncio.gather(
            self.recall_memory(
                query, dimension=memory_dimension, k=memory_k,
                min_score=min_score, rerank=rerank,
                current_frame=current_frame,
            ),
            self.recall_knowledge(
                query, category=knowledge_category, k=knowledge_k,
                min_score=min_score, rerank=rerank,
                current_frame=current_frame,
            ),
        )
        # Deduplicate: identity entries are unconditionally injected in their
        # own block, so strip them from query-based recall to avoid wasting
        # context tokens on a repeated directive.
        if id_ids and mem_entries:
            mem_entries = [e for e in mem_entries if e.id not in id_ids]
        parts: List[str] = []
        if id_block:
            parts.append(id_block)
        if include_known_entities:
            ent_block = await self._get_known_entities_cached()
            if ent_block:
                parts.append(ent_block)
        mem_block = _fmt_mem(mem_entries)
        if mem_block:
            parts.append(mem_block)
        kn_block = _fmt_kn(kn_entries)
        if kn_block:
            parts.append(kn_block)
        if not parts:
            return ""
        body = "\n".join(parts) + "\n"
        if not include_header:
            return body
        return f"{_LTM_HEADER}\n{_LTM_DESC}\n{body}"

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
        self._emit = None
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

    Uses the main ``llm.models`` pool because retriage rule-based
    migrations need the higher-quality reasoning that the helper pool
    isn't sized for. Returns ``[]`` on missing API_KEY / config so the
    worker's LLM-based migrations halt rather than silently no-op.
    """
    try:
        from src.infrastructure.role_resolver import resolve_models_and_helper
        from src.infrastructure.llm_service_factory import create_llm_service
    except Exception:
        _logger.exception("retriage: failed to import LLM stack")
        return []
    llm_cfg = cfg.get("llm") or {}
    api_key = llm_cfg.get("API_KEY")
    if not api_key:
        _logger.warning("retriage: llm.API_KEY missing; LLM migrations will halt")
        return []
    main_models, _helper = resolve_models_and_helper(llm_cfg)
    if not main_models:
        _logger.warning(
            "retriage: llm.models is empty; LLM migrations will halt",
        )
        return []
    return [
        create_llm_service(model=m, api_key=api_key) for m in main_models
    ]
