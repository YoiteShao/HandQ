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
from .recall_logger import RecallLogger
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

def _frame_compatible(entry_frame_os: Optional[str], current_frame: Optional[dict]) -> bool:
    """Return True if an entry's frame_os is compatible with current_frame.

    Compatibility rules (deliberately permissive — we'd rather show a
    possibly-irrelevant entry than hide a relevant one):
      - No current_frame supplied → always compatible.
      - current_frame.confidence < 0.6 → bypass filter (low-confidence frame
        is worse than no filter; would suppress real signal).
      - entry_frame_os is NULL → frame-agnostic, always compatible.
      - entry_frame_os in {'any', 'unknown'} → universally compatible.
        ('unknown' is frame_inference's "couldn't classify" output — e.g.
        a custom shell not in the heuristic tables. Treating it as
        incompatible would silently hide those memories from recall.)
      - entry_frame_os == current_frame['os'] → exact match.
      - Otherwise → incompatible (filter out).
    """
    if not current_frame:
        return True
    confidence = float(current_frame.get("confidence", 1.0))
    if confidence < 0.6:
        return True
    if not entry_frame_os or entry_frame_os in ("any", "unknown"):
        return True
    target = current_frame.get("os")
    if not target:
        return True
    return entry_frame_os == target


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
    dynamic_k: bool = False,
    current_frame: Optional[dict] = None,
) -> List[Entry]:
    effective_k = C.RECALL_PLANNER_OVERFETCH_K if dynamic_k else k
    rows = await _hybrid_recall(
        store, embedder, query,
        kind=EntryKind.MEMORY.value,
        facet_value=dimension.value if dimension else None,
        k=effective_k,
        min_score=min_score,
        current_frame=current_frame,
    )
    if not rows:
        return []
    # Identity entries are injected unconditionally via a separate block;
    # exclude them from query-based recall so they don't waste limited slots.
    if dimension is None:
        rows = [r for r in rows if r[4] != MemoryDimension.IDENTITY.value]
        if not rows:
            return []
    if rerank and reranker.available:
        rows = await _ml_rerank(reranker, query, rows)
        rows = _rerank_gate(rows)
        if not rows:
            return []
    entries = [_row_to_memory_entry(r) for r in rows[:effective_k]]
    if dynamic_k:
        entries = _score_gap_trim(
            entries,
            min_k=C.RECALL_PLANNER_MIN_K,
            max_k=C.RECALL_PLANNER_MAX_K,
            gap=C.RECALL_SCORE_GAP_THRESHOLD,
        )
    else:
        entries = entries[:k]
    RecallLogger.get().record(
        [e.id for e in entries], kind=EntryKind.MEMORY.value,
    )
    return entries


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
    dynamic_k: bool = False,
    current_frame: Optional[dict] = None,
) -> List[Entry]:
    effective_k = C.RECALL_PLANNER_OVERFETCH_K if dynamic_k else k
    rows = await _hybrid_recall(
        store, embedder, query,
        kind=EntryKind.KNOWLEDGE.value,
        facet_value=category.value if category else None,
        k=effective_k,
        min_score=min_score,
        current_frame=current_frame,
    )
    if not rows:
        return []
    if rerank and reranker.available:
        rows = await _ml_rerank(reranker, query, rows)
        rows = _rerank_gate(rows)
        if not rows:
            return []
    entries = [_row_to_knowledge_entry(r) for r in rows[:effective_k]]
    if dynamic_k:
        entries = _score_gap_trim(
            entries,
            min_k=C.RECALL_PLANNER_MIN_K,
            max_k=C.RECALL_PLANNER_MAX_K,
            gap=C.RECALL_SCORE_GAP_THRESHOLD,
        )
    else:
        entries = entries[:k]
    RecallLogger.get().record(
        [e.id for e in entries], kind=EntryKind.KNOWLEDGE.value,
    )
    return entries


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


IDENTITY_CONTEXT_TAG: str = "identity"


def format_identity_block(entries: List[Entry]) -> str:
    if not entries:
        return ""
    lines = [f"<{IDENTITY_CONTEXT_TAG}>"]
    for e in entries:
        lines.append(f"- {_escape_xml(e.summary)}")
    lines.append(f"</{IDENTITY_CONTEXT_TAG}>")
    return "\n".join(lines)


KNOWN_ENTITIES_CONTEXT_TAG: str = "known-entities"


def format_known_entities_block(principals: List[tuple]) -> str:
    """Render the principal graph (people / machines / projects) as a block.

    Input rows come from ``store.list_principals`` (11-tuples):
    ``(id, kind, canonical_name, display_name, email, host_kind, os,
    project_root, first_seen, last_seen, sighting_count)``.

    Frame-agnostic by design: unlike memory/knowledge recall, principals are
    NOT filtered by ``current_frame``. An SSH ``machine`` (os='linux') is most
    relevant *while the bridge runs on Windows*, and ``person``/``project`` are
    cross-environment — so we mirror the permissive philosophy of
    ``_frame_compatible`` ("rather show than hide") instead of an os-equality
    check that would suppress exactly the useful rows.
    """
    if not principals:
        return ""
    lines = [f"<{KNOWN_ENTITIES_CONTEXT_TAG}>"]
    for row in principals:
        kind = row[1]
        name = _escape_xml(row[3] or row[2] or "")
        if not name:
            continue
        if kind == "person":
            email = row[4]
            detail = f" {_escape_xml(email)}" if email else ""
            lines.append(f"- [person] {name}{detail}")
        elif kind == "machine":
            bits = [b for b in (row[5], row[6]) if b]  # host_kind, os
            detail = f" ({_escape_xml(', '.join(bits))})" if bits else ""
            lines.append(f"- [machine] {name}{detail}")
        elif kind == "project":
            root = row[7]
            detail = f" ({_escape_xml(root)})" if root else ""
            lines.append(f"- [project] {name}{detail}")
        else:
            lines.append(f"- [{_escape_xml(str(kind))}] {name}")
    if len(lines) == 1:  # header only — every row was nameless
        return ""
    lines.append(f"</{KNOWN_ENTITIES_CONTEXT_TAG}>")
    return "\n".join(lines)


def _score_gap_trim(
    entries: List[Entry], *, min_k: int, max_k: int, gap: float,
) -> List[Entry]:
    """Trim entries at the first score gap exceeding *gap*.

    Respects *min_k* floor and *max_k* ceiling. Returns the original list
    unchanged when scores are unavailable or the list is too short.
    """
    if len(entries) <= min_k:
        return entries
    entries = entries[:max_k]
    for i in range(min_k - 1, len(entries) - 1):
        curr = entries[i].score
        nxt = entries[i + 1].score
        if curr is not None and nxt is not None and (curr - nxt) > gap:
            return entries[: i + 1]
    return entries


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
    current_frame: Optional[dict] = None,
) -> List[tuple]:
    """Run BM25 and dense recall in parallel, fuse with RRF.

    Each branch over-fetches relative to k so RRF has enough overlap to
    work with. We do them sequentially (not asyncio.gather) because both
    are short and SQLite reads aren't network-bound; sequential keeps
    error handling simple.
    """
    overfetch = max(k * 3, k)
    rows_bm25 = await _bm25_recall(store, query, kind, facet_value, overfetch, current_frame)

    rows_dense: List[tuple] = []
    if embedder.available:
        try:
            rows_dense = await _dense_recall(
                store, embedder, query,
                kind=kind, facet_value=facet_value,
                limit=overfetch, min_score=min_score,
                current_frame=current_frame,
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
    current_frame: Optional[dict] = None,
) -> List[tuple]:
    """Stage 1a: keyword recall via FTS5 BM25.

    Falls back to empty list (rather than raise) when the FTS5 query
    parser chokes on the sanitized input — recall stays usable on dense
    branch alone.

    fts_search_* returns 8-tuples: (entry_id, chunk_id, text, summary,
    facet, created_at, rank, frame_os). We filter by current_frame here,
    then drop frame_os to maintain the 7-tuple shape downstream code
    expects.
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
    # Filter by current_frame (frame_os is at index 7), then drop frame_os.
    if current_frame:
        rows = [r for r in rows if _frame_compatible(r[7] if len(r) > 7 else None, current_frame)]
    # Strip frame_os back to 7-tuple to preserve downstream contracts.
    rows = [r[:7] for r in rows]
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
    current_frame: Optional[dict] = None,
) -> List[tuple]:
    """Stage 1b: dense recall via brute-force cosine over cached embeddings.

    Corpus is small (~hundreds of chunks) so a full sweep is cheaper than
    maintaining an ANN index. Chunks without a cached embedding are
    skipped — the DreamWorker backfill loop ensures coverage over time.

    Output rows are 8-tuples with display_score = cosine in [0, 1].

    list_embedded_chunks now returns 9-tuples (added frame_os at index 8).
    We filter by current_frame, then build the 8-tuple downstream shape.
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
    for row in rows:
        # 9-tuple: (entry_id, chunk_id, text, summary, facet, created_at, hash, embedding, frame_os)
        entry_id, chunk_id, text, summary, facet, created_at = row[0], row[1], row[2], row[3], row[4], row[5]
        emb_bytes = row[7]
        entry_frame_os = row[8] if len(row) > 8 else None
        if facet_value and facet != facet_value:
            continue
        if not _frame_compatible(entry_frame_os, current_frame):
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


def _rerank_gate(rows: List[tuple]) -> List[tuple]:
    """Drop rows the reranker scored below ``RECALL_RERANK_MIN_SCORE``.

    Runs only after a successful ``_ml_rerank`` (display_score at index 7 is
    then the LLM relevance score). Two rows are kept regardless of the floor:

    - ``display_score is None`` — a BM25-only hit that never received a cosine
      or rerank score. These are exact-keyword matches; dropping them on a
      relevance-score floor they were never measured against would silently
      lose precise term hits.

    On rerank failure ``_ml_rerank`` returns the fused rows unchanged (score =
    dense cosine), and applying the same floor there is a sane secondary
    cull — cosine and rerank scores share the [0, 1] relevance scale.
    """
    return [
        r for r in rows
        if r[7] is None or r[7] >= C.RECALL_RERANK_MIN_SCORE
    ]


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
