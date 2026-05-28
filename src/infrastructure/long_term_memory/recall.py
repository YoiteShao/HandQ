"""Hybrid recall pipeline + XML block formatting.

Pipeline (mirroring yansu's three-stage idea but fixing yansu's BM25-funnel
limitation by adding a parallel dense branch):

    ┌───────────────────┐     ┌───────────────────┐
    │ stage 1a: BM25    │     │ stage 1b: dense   │
    │  fts_search_*     │     │  cosine over all  │
    │  (keyword recall) │     │  cached chunks    │
    └─────────┬─────────┘     └─────────┬─────────┘
              │                         │
              └────────┬────────────────┘
                       ▼
              ┌────────────────────┐
              │ stage 1: RRF fuse  │
              │ (reciprocal-rank   │
              │  fusion, k=60)     │
              └─────────┬──────────┘
                        ▼
              ┌────────────────────┐
              │ stage 2: ML rerank │
              │ (optional, v1 noop)│
              └─────────┬──────────┘
                        ▼
                    top-k Entry

Why hybrid (vs yansu's BM25 → dense rerank cascade):
- Pure-keyword BM25 returns NOTHING when the query and document share
  zero tokens (e.g. Chinese query against English memory, or
  paraphrased intent like "lint code" vs "ruff check"). Cascade
  rerank cannot recover from an empty stage-1.
- Pure-dense skips the precision boost that BM25 gives on exact-token
  matches (file paths, command names, version strings).
- RRF fusion combines both rankings without needing calibrated scores
  on either side, since it only uses positions. k=60 is the literature
  default (Cormack et al. 2009).

Internal row shape (8-tuple) flowing between stages:

    (entry_id, chunk_id, text, summary, facet, created_at, sortkey, display_score)

- ``sortkey``: ascending = better; either bm25 rank, ``-cosine``, or
  ``-rrf_score`` depending on stage.
- ``display_score``: positive [0,1] cosine after stage 1b/2; positive
  RRF score after stage 1 fuse. None only if no scoring stage ever ran
  (BM25-only path with empty dense candidates).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from . import _constants as C
from .embedding import EmbeddingProvider, cosine, vec_from_bytes, vec_to_bytes
from .models import Entry, EntryKind, KnowledgeCategory, MemoryDimension
from .reranker import Reranker

_logger = logging.getLogger("handq.ltm.recall")


# ── Render-shape constants ──────────────────────────────────────────────────
#
# These describe the XML wrapper format we produce for prompt injection.
# They live here (not in _constants) because they are a property of the
# render layer, tightly coupled with format_*_block below — moving them
# changes the prompt's parser surface, so the change site = the format
# function.
LTM_BLOCK_HEADER: str = "[Long-Term Context]"
LTM_BLOCK_DESCRIPTION: str = (
    "(Cross-session memory and knowledge — durable user preferences "
    "and reusable team/project facts.)"
)
MEMORY_CONTEXT_TAG: str = "memory-context"
KNOWLEDGE_CONTEXT_TAG: str = "knowledge-context"


# ── Public API (used by LongTermMemory) ─────────────────────────────────────

async def recall_memory_impl(
    store,
    embedder: EmbeddingProvider,
    reranker: Reranker,
    query: str,
    *,
    dimension: Optional[MemoryDimension] = None,
    k: int = 5,
    min_score: float = 0.0,
    rerank: bool = True,
) -> List[Entry]:
    rows = await _hybrid_recall(
        store, embedder, query,
        kind=EntryKind.MEMORY.value,
        facet_value=dimension.value if dimension else None,
        k=k,
        min_score=min_score,
    )
    if not rows:
        return []
    # Stage 3 only when caller wants it AND a real reranker is available.
    # Receptionist passes rerank=False to dodge the ~3s-per-call LLM
    # roundtrip (it doesn't need that much precision per message).
    if rerank and reranker.available:
        rows = await _ml_rerank(reranker, query, rows)
    return [_row_to_memory_entry(r) for r in rows[:k]]


async def recall_knowledge_impl(
    store,
    embedder: EmbeddingProvider,
    reranker: Reranker,
    query: str,
    *,
    category: Optional[KnowledgeCategory] = None,
    k: int = 5,
    min_score: float = 0.0,
    rerank: bool = True,
) -> List[Entry]:
    rows = await _hybrid_recall(
        store, embedder, query,
        kind=EntryKind.KNOWLEDGE.value,
        facet_value=category.value if category else None,
        k=k,
        min_score=min_score,
    )
    if not rows:
        return []
    if rerank and reranker.available:
        rows = await _ml_rerank(reranker, query, rows)
    return [_row_to_knowledge_entry(r) for r in rows[:k]]


def format_memory_block(entries: List[Entry]) -> str:
    if not entries:
        return ""
    lines = [f"<{MEMORY_CONTEXT_TAG}>"]
    for e in entries:
        score_attr = f' score="{e.score:.2f}"' if e.score is not None else ""
        dim = e.dimension.value if e.dimension else EntryKind.MEMORY.value
        lines.append(f'  <{dim}{score_attr}>{_escape_xml(e.summary)}</{dim}>')
    lines.append(f"</{MEMORY_CONTEXT_TAG}>")
    return "\n".join(lines)


def format_knowledge_block(entries: List[Entry]) -> str:
    if not entries:
        return ""
    lines = [f"<{KNOWLEDGE_CONTEXT_TAG}>"]
    for e in entries:
        score_attr = f' score="{e.score:.2f}"' if e.score is not None else ""
        cat = e.category.value if e.category else EntryKind.KNOWLEDGE.value
        lines.append(f'  <{cat}{score_attr}>{_escape_xml(e.summary)}</{cat}>')
    lines.append(f"</{KNOWLEDGE_CONTEXT_TAG}>")
    return "\n".join(lines)


# ── Hybrid stage 1 ──────────────────────────────────────────────────────────

async def _hybrid_recall(
    store,
    embedder: EmbeddingProvider,
    query: str,
    *,
    kind: str,
    facet_value: Optional[str],
    k: int,
    min_score: float,
) -> List[tuple]:
    """Run BM25 and dense recall in parallel, fuse with RRF.

    Each branch over-fetches relative to k so RRF has enough overlap to
    work with. We do them sequentially (not asyncio.gather) because both
    are short and SQLite reads aren't network-bound; sequential keeps
    error handling simple.
    """
    overfetch = max(k * 3, k)
    rows_bm25 = await _bm25_recall(store, query, kind, facet_value, overfetch)

    rows_dense: List[tuple] = []
    if embedder.available:
        try:
            rows_dense = await _dense_recall(
                store, embedder, query,
                kind=kind, facet_value=facet_value,
                limit=overfetch, min_score=min_score,
            )
        except Exception:
            _logger.exception("dense recall failed; using BM25 only")

    if not rows_bm25 and not rows_dense:
        return []

    # Always merge through RRF — even when one branch is empty — so the
    # display_score stays on a consistent scale across queries. Otherwise
    # the score reported to the caller would silently switch between RRF
    # and raw cosine depending on whether BM25 happened to hit, which
    # makes scores from different queries incomparable in the LTM block.
    if not rows_dense:
        rows_bm25_8 = [r + (None,) for r in rows_bm25]
        return _rrf_merge(rows_bm25_8, [])
    if not rows_bm25:
        return _rrf_merge([], rows_dense)

    return _rrf_merge(rows_bm25, rows_dense)


async def _bm25_recall(
    store,
    query: str,
    kind: str,
    facet_value: Optional[str],
    limit: int,
) -> List[tuple]:
    """Stage 1a: keyword recall via FTS5 BM25.

    Falls back to empty list (rather than raise) when the FTS5 query
    parser chokes on the sanitized input — recall stays usable on dense
    branch alone.
    """
    if kind == EntryKind.MEMORY.value:
        from .models import MemoryDimension
        facet = MemoryDimension(facet_value) if facet_value else None
        rows = await store.fts_search_memory(query, dimension=facet, limit=limit)
    elif kind == EntryKind.KNOWLEDGE.value:
        from .models import KnowledgeCategory
        facet = KnowledgeCategory(facet_value) if facet_value else None
        rows = await store.fts_search_knowledge(query, category=facet, limit=limit)
    else:
        raise ValueError(f"unknown kind: {kind!r}")
    return _dedup_by_entry(rows)


async def _dense_recall(
    store,
    embedder: EmbeddingProvider,
    query: str,
    *,
    kind: str,
    facet_value: Optional[str],
    limit: int,
    min_score: float,
) -> List[tuple]:
    """Stage 1b: dense recall via brute-force cosine over cached embeddings.

    Corpus is small (~hundreds of chunks) so a full sweep is cheaper than
    maintaining an ANN index. Chunks without a cached embedding are
    skipped — the DreamWorker backfill loop ensures coverage over time.

    Output rows are 8-tuples with display_score = cosine in [0, 1].
    """
    rows = await store.list_embedded_chunks(
        kind=kind,
        provider=embedder.provider,
        model=embedder.model,
    )
    if not rows:
        return []

    q_emb = await embedder.embed_query(query)
    if not q_emb:
        return []

    scored: List[tuple] = []
    for entry_id, chunk_id, text, summary, facet, created_at, _hash, emb_bytes in rows:
        if facet_value and facet != facet_value:
            continue
        emb = vec_from_bytes(emb_bytes)
        if not emb:
            continue
        s = cosine(q_emb, emb)
        if s < min_score:
            continue
        # 8-tuple: (entry_id, chunk_id, text, summary, facet, created_at, sortkey, display_score)
        scored.append((entry_id, chunk_id, text, summary, facet, created_at, -s, s))

    scored.sort(key=lambda r: r[6])
    return _dedup_by_entry_8(scored[: limit * 2])[:limit]


def _rrf_merge(rows_bm25: List[tuple], rows_dense: List[tuple]) -> List[tuple]:
    """Reciprocal-rank-fusion of two ranked lists for ORDERING; preserve
    cosine for SCORING.

    Ordering: each entry's RRF score is Σ 1/(k + rank_i + 1) over the
    branches it appears in. Sorted descending. RRF is rank-based, so a
    "rank 0 in only-branch" entry can outrank a "rank 1 in both-branches"
    entry — but at small corpus sizes RRF magnitudes are uninformative
    so we don't expose them.

    Scoring (the ``display_score`` field): use the dense cosine when
    available (a calibrated [0, 1] relevance signal), else None for
    BM25-only paths. Callers can safely show this number as
    "how related is this memory to the query".

    Either input may be empty.
    """
    bm_index: Dict[str, int] = {r[0]: rank for rank, r in enumerate(rows_bm25)}
    de_index: Dict[str, int] = {r[0]: rank for rank, r in enumerate(rows_dense)}

    # Pull the dense cosine out of the dense rows so we can preserve it
    # as display_score regardless of fusion order.
    dense_cosine: Dict[str, Optional[float]] = {
        r[0]: (r[7] if len(r) >= 8 else None)
        for r in rows_dense
    }

    seen: Dict[str, tuple] = {}
    for r in rows_dense:
        seen[r[0]] = r if len(r) == 8 else r + (None,)
    for r in rows_bm25:
        promoted = r if len(r) == 8 else r + (None,)
        seen.setdefault(r[0], promoted)

    rrf: Dict[str, float] = {}
    for eid in seen:
        s = 0.0
        if eid in bm_index:
            s += 1.0 / (C.RRF_K + bm_index[eid] + 1)
        if eid in de_index:
            s += 1.0 / (C.RRF_K + de_index[eid] + 1)
        rrf[eid] = s

    fused = sorted(seen.values(), key=lambda r: rrf[r[0]], reverse=True)
    out: List[tuple] = []
    for r in fused:
        eid = r[0]
        # Prefer dense cosine for display; fall back to None (BM25-only).
        display = dense_cosine.get(eid)
        # Keep sortkey as -RRF so any later code that re-sorts ascending
        # honours the RRF order.
        out.append((eid, r[1], r[2], r[3], r[4], r[5], -rrf[eid], display))
    return out


# ── Stage 2 (ML rerank) ─────────────────────────────────────────────────────

async def _ml_rerank(
    reranker: Reranker,
    query: str,
    rows: List[tuple],
) -> List[tuple]:
    """Score each surviving row with the cross-encoder, sort descending."""
    texts = [r[2] for r in rows]
    try:
        scores = await reranker.rerank(query, texts)
    except Exception:
        _logger.exception("ml rerank failed; returning fused order")
        return rows
    if len(scores) != len(rows):
        _logger.warning(
            "rerank score count %d != row count %d; ignoring rerank",
            len(scores), len(rows),
        )
        return rows
    paired = [
        (r[0], r[1], r[2], r[3], r[4], r[5], -s, s)
        for r, s in zip(rows, scores)
    ]
    paired.sort(key=lambda r: r[6])
    return paired


# ── Helpers ─────────────────────────────────────────────────────────────────

def _dedup_by_entry(rows: List[tuple]) -> List[tuple]:
    """Keep the best-ranked chunk per entry from a 7-tuple FTS list
    (rank ascending = better).
    """
    best: dict = {}
    for r in rows:
        eid = r[0]
        if eid not in best or r[6] < best[eid][6]:
            best[eid] = r
    return list(best.values())


def _dedup_by_entry_8(rows: List[tuple]) -> List[tuple]:
    """Same as _dedup_by_entry but for 8-tuples (sortkey at index 6)."""
    best: dict = {}
    for r in rows:
        eid = r[0]
        if eid not in best or r[6] < best[eid][6]:
            best[eid] = r
    return list(best.values())


def _row_to_memory_entry(r: tuple) -> Entry:
    eid, _chunk_id, _text, summary, dim, created, _sortkey, display_score = r
    try:
        dim_enum = MemoryDimension(dim) if dim else None
    except ValueError:
        dim_enum = None
    return Entry(
        id=eid,
        kind=EntryKind.MEMORY,
        dimension=dim_enum,
        summary=summary,
        created_at=int(created or 0),
        score=display_score,
    )


def _row_to_knowledge_entry(r: tuple) -> Entry:
    eid, _chunk_id, _text, summary, cat, created, _sortkey, display_score = r
    try:
        cat_enum = KnowledgeCategory(cat) if cat else None
    except ValueError:
        cat_enum = None
    return Entry(
        id=eid,
        kind=EntryKind.KNOWLEDGE,
        category=cat_enum,
        summary=summary,
        created_at=int(created or 0),
        score=display_score,
    )


def _escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
