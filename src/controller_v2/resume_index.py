"""ResumeIndex — in-memory BM25 + local-dense retrieval over destroyed-session
digests (Phase 2 of session-resume, docs/session_resume_design.md §6.3/§6.3.1).

Built by scanning ``digest.json`` files under
``%USERPROFILE%\\HandQ\\History\\`` (written by
:class:`~.session_digest.SessionDigest` at session destroy — see
``flow_controller.py``). Nothing is persisted here: the index is a pure,
disposable derivative of the on-disk digests, rebuilt from scratch on every
``build()`` call. This is a deliberate simplification over a persistent
``resume_index.db`` (§6.3): with a few hundred sessions, a full rescan is
tens of milliseconds once the embedding model is warm, and it makes an
entire class of DB-maintenance bugs (upsert, dangling rows, drift,
corruption) structurally impossible — the digest files are the only
source of truth.

The bridge calls ``build()`` twice: once at boot purely to warm the
embedding model + fill the caches (~5-7s cold — jieba dict load + onnx
model load + first-time read/parse/embed of every existing digest), and
again right before EVERY resume search (see
``stdio_bridge._search_resume_candidates``) so a session destroyed
earlier in the same bridge process is visible to resume without a
restart. A pre-warmed rebuild is cheap because BOTH the disk-scan and the
embedding are cached (see ``_digest_cache`` / ``_embed_cache``): a rebuild
only re-reads/re-parses/re-embeds NEW/changed digests — everything else is
one ``stat()`` per unchanged session dir — so the per-search cost is a
cheap scan + FTS5 insert + query embed (a few ms per session, not the
disk-read/JSON-parse cost), not a full re-scan of the whole corpus.
Confirmed live 2026-08-20: before the digest cache, a cold-OS-file-cache
rebuild took up to 28.6s at ~900 sessions (the ``digest.json`` re-read was
the dominant cost, not the embedding) — that was the "first message of a
new session feels frozen" bug.

Two retrieval legs, fused by RRF, exactly mirroring
``long_term_memory/recall.py``'s pattern but INTENTIONALLY not sharing its
runtime: LTM's dense leg calls the remote YOUR-AI-ENDPOINT gateway (cold-start
7.8-70s), which is unusable on resume's real-time first-message path. Both
legs here are fully local:

  - BM25 leg: an in-memory SQLite FTS5 table, tokenized with jieba (not
    ``unicode61``, see §6.3 — jieba is required to split CJK↔ASCII runs and
    segment Chinese content words; plain ``unicode61`` treats a whole CJK
    run as one token and misses everything).
  - Dense leg: ``BAAI/bge-small-zh-v1.5`` via ``fastembed`` (pure
    onnxruntime, no torch), selected in §6.3.1 after a 3-model + web
    shootout — best hit rate, smallest, fastest (2ms/query), zero cold
    start. Model + tokenizer files ship vendored in the repo under
    ``assets/models/bge-small-zh-v1.5/`` (committed to git, packaged via
    electron/package.json's build.extraFiles — same mechanism as Skill/
    and scripts/) so an offline/air-gapped install never touches
    HuggingFace Hub; see ``_vendored_model_dir()``/``_ensure_model()``.

Case-folding: BOTH legs need text lower-cased BEFORE tokenizing/embedding.
FTS5/jieba are already case-insensitive once lower-cased, but bge-small-zh
is NOT — it embeds "QPM" and "qpm" to measurably different vectors (verified
live, §6.3.1). Every tokenize()/embed call site here lower-cases first.

Not coupled to LTM's ``memory.db`` (§10): independent in-memory table, no
shared schema, no shared availability/failure semantics. The two systems
happen to use the same embedding model family purely as a resource-sharing
convenience (§12), not an architectural dependency.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .session_digest import SessionDigest

_logger = logging.getLogger("handq.controller_v2.resume_index")

# ── Tuning ───────────────────────────────────────────────────────────────────
# Deliberately independent of long_term_memory/_constants.py (§10 — resume
# does not couple to LTM). Values below are calibrated against real session
# data; see scratch_resume/06, /17, /19 for the calibration runs.

# RRF fusion constant — same value as LTM's C.RRF_K, chosen independently
# (RRF's k=60 is a standard IR default, not something we tuned).
RRF_K: int = 60

# Absolute gate on dense-cosine — applied PER CANDIDATE (see _search_sync),
# not just the top-1 hit. Below this on a candidate, it's dropped;
# EVERY candidate failing it means resume stays completely silent (§6.4
# step 5) — no candidate card, indistinguishable from a brand-new task.
#
# RECALIBRATED 2026-08-01 against a real 93-session corpus (the ~20-sample
# calibration below UNDER-ESTIMATED the false-positive rate at scale): a
# real repro query ("还记得QPM的下载任务吗" + "关于qprofiler的") hit 57-59/93
# sessions at 0.50 — half of them genuinely unrelated noise sharing no topic
# with the query at all (generic Chinese task-description sentences like
# "读一下C盘有多少文件夹" scored cos 0.60-0.66, "claim desktop_snapshot截图"
# scored 0.62). Swept 0.50/0.55/0.60/0.62/0.65/0.68/0.70/0.75 against that
# corpus + 2 query variants (scratch_resume/threshold_calibration.py +
# threshold_sanity.py, both since deleted): 0.68 is the crossover point —
# the highest value that still returns ≥1 hit for the WEAKEST-signal query
# variant tested, while returning ZERO noise across all three variants
# (0.70 already drops to 0 hits for the weak variant). The original 0.50
# calibration note below is kept for history but is NOT what's live.
#
# Original (2026-07 era) note, ~20-sample calibration — superseded above:
# "real should-hit queries clustered at cos 0.51-0.81, should-miss queries
# at 0.32-0.55 (overlap zone [0.51, 0.55])" — that overlap zone estimate
# did not hold up against a larger, more realistic corpus.
DENSE_COS_GATE: float = 0.68

# How many BM25 rows to pull a rank from for RRF fusion. Purely an ordering
# aid — NOT a cap on how many candidates search() can return (that's gated
# per-candidate on DENSE_COS_GATE alone, see _search_sync). Generous because
# it's an in-memory FTS5 query: cheap even at a few hundred rows.
_BM25_PREFILTER_LIMIT: int = 50

# bge-small-zh-v1.5: selected in §6.3.1 over 2 other local models + the web
# YOUR-AI-ENDPOINT embedder — best/tied-best hit rate, 90MB, 2ms/query, zero cold start.
EMBED_MODEL_NAME: str = "BAAI/bge-small-zh-v1.5"

# Batch size for every ``model.embed(...)`` call below. MUST stay small.
#
# fastembed's default batch_size is 256, which was the single largest memory
# regression in the bridge: embedding this corpus (865 digests, only 84KB of
# text in total) in 256-item batches drove RSS from a 285MB floor to 4724MB,
# and the onnxruntime CPU arena — which fastembed leaves ENABLED, unlike
# rapidocr — RETAINS that peak, so `del` + `gc.collect()` brought back nothing
# (measured 4723MB after both). Since build() runs at boot, that is a ~4.4GB
# spike minutes into every launch that then persists for the life of the
# process.
#
# The driver is padding, not text volume: each batch pads to its longest
# member, and one 4000-char digest (CORPUS_CHAR_CAP) is ~512 tokens, so a
# 256-item batch computes attention at 256 x heads x 512^2. Measured transient
# peak OVER the 285MB model-resident floor, on the real corpus:
#
#     batch_size=1      +31MB   13.4s   <- chosen (leanest; no time penalty)
#     batch_size=2      +60MB   14.6s
#     batch_size=4     +116MB   12.7s
#     batch_size=8     +229MB   14.3s
#     batch_size=256  +4439MB   47.8s   <- fastembed default
#
# batch_size=1 is chosen: on this corpus (many short, similar-length texts)
# the bottleneck is per-item model-invocation overhead, not batch throughput,
# so shrinking to 1 costs no measurable wall-clock (13.4s vs 12.7s is noise)
# while holding the transient embed peak to ~+31MB. The floor itself (~285MB:
# interpreter + numpy + onnxruntime + the resident bge model + loaded digests)
# is irreducible without unloading the model entirely — it is NOT the embed.
#
# Output is bit-identical across batch sizes (verified: max_abs_diff 0.0,
# cosine 1.0 against single-item embeds), so this is purely a resource knob —
# it cannot change retrieval behaviour. Raising it only trades RAM for nothing
# on this workload; lowering it below 1 is not possible.
EMBED_BATCH_SIZE: int = 1

# Per-entry corpus cap — a pathological session (39万字 final_answer, seen in
# real data during design research) must not dominate embedding/tokenize cost
# or make one row disproportionately large in the in-memory FTS5 table.
CORPUS_CHAR_CAP: int = 4000

# Resume-domain stopwords — transition/filler words a user says when
# continuing a task, NOT a general Chinese stopword list. Small and stable
# by construction (see §6.3); a missing entry shows up as noise in real
# queries, not as a crash. Verified in scratch_resume/04 and /17: jieba
# segmentation + this list took Chinese-query hit rate from 6/9 to 8/9 over
# the CJK-run-as-one-token status quo.
STOPWORDS: frozenset = frozenset("""
继续 接着 还是 上次 之前 那个 这个 那次 帮我 一下 一个 的 了 是 在 我 你 它
把 给 和 与 或 就 也 都 要 想 让 请 去 做 弄 看 有 个 些 吗 呢 啊 nbsp
task 任务 之类 什么 怎么 如何 请问 麻烦 现在 然后 以及 那 这 内容
""".split())

_NON_WORD_RE = re.compile(r"^[\s\W]+$")


# ── Per-user paths ───────────────────────────────────────────────────────────
# Mirrors bridge_main._user_handq_root() — independently, like
# browser_paths.py / skills.py / triage.py already do, so this module doesn't
# need to import the bridge entrypoint.

def _user_handq_root() -> Path:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _session_history_root() -> Path:
    return _user_handq_root() / "History"


def _install_dir() -> Path:
    """Directory next to the bridge entry point — same algorithm as
    ``bridge_main._INSTALL_DIR`` / ``skills._install_dir()``, independently
    implemented per this module's own layout (``src/controller_v2/`` is two
    levels under the repo root, same as ``src/infrastructure/``)."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(__file__).parent.parent.parent.resolve()


# The onnx model + tokenizer files, vendored into the repo (assets/models/) so
# an offline/air-gapped install never needs to reach HuggingFace Hub. Ships
# with the build the same way Skill/ and scripts/ do (see
# electron/package.json build.extraFiles) — committed to git, not downloaded
# at build time or runtime.
_VENDORED_MODEL_DIRNAME = "bge-small-zh-v1.5"


def _vendored_model_dir() -> Path:
    return _install_dir() / "assets" / "models" / _VENDORED_MODEL_DIRNAME


def _model_cache_dir() -> Path:
    """Fallback cache_dir for fastembed's own HuggingFace-Hub download path —
    only reached when the vendored copy (_vendored_model_dir) is absent, e.g.
    a dev checkout that hasn't pulled assets/models/ yet. fastembed's default
    cache_dir is %TEMP%\\fastembed_cache — a temp directory the OS/user may
    clear, which would force a re-download at the worst possible time (first
    message of a session). Pin it under the HandQ root instead, alongside
    browser_profile/ and History/, so a dev-mode download persists across
    HandQ upgrades like other user-owned data (per the classification in
    ARCHITECTURE.md §1.5)."""
    return _user_handq_root() / "models"


# ── Tokenization (BM25 leg) ──────────────────────────────────────────────────

def tokenize(text: str) -> List[str]:
    """jieba segmentation + resume-stopword filter.

    Lower-cases BEFORE jieba (both sides symmetric — see module docstring).
    jieba, unlike FTS5's ``unicode61``, splits CJK runs into real content
    words AND splits CJK↔ASCII boundaries ("QPM软件" -> "QPM", "软件") —
    both are required for resume queries to hit (§6.3).
    """
    import jieba

    out: List[str] = []
    for w in jieba.cut((text or "").lower()):
        w = w.strip()
        if not w or w in STOPWORDS or _NON_WORD_RE.match(w):
            continue
        out.append(w)
    return out


def _corpus_text(digest: SessionDigest) -> str:
    """Assemble the BM25/dense corpus for one digest (§6.3's corpus table):
    title (= the user's first verbatim message), completed-task artifacts /
    key_findings / final_answer, and the destroy-time workspace listing.

    Deliberately NOT the full conversation — later turns are agent chatter
    the user does not re-type when referring back to a session; the
    signal-bearing text is what the user originally asked for and what the
    session produced.
    """
    parts: List[str] = [digest.title or ""]
    for c in digest.completed or []:
        parts.extend(a for a in (c.get("artifacts") or []) if a)
        parts.extend(k for k in (c.get("key_findings") or []) if k)
        final_answer = c.get("final_answer") or ""
        if final_answer:
            parts.append(final_answer[:500])
    parts.extend(f for f in (digest.workspace_files or []) if f)
    text = " ".join(p for p in parts if p)
    return text[:CORPUS_CHAR_CAP]


@dataclass
class ResumeCandidate:
    """One search() hit — enough to gate on and, later (Phase 4), render a
    candidate card without re-touching disk."""

    session_dir: Path
    digest: SessionDigest
    dense_cos: float
    rrf_score: float


class ResumeIndex:
    """In-memory BM25 (SQLite FTS5) + dense (bge-small-zh) retrieval over
    every ``digest.json`` found under History/.

    Not thread-per-call safe by construction: every method that touches the
    SQLite connection or the embedding model runs under ``self._lock`` (an
    ``asyncio.Lock``), same pattern as
    ``infrastructure/scheduler/store.py``'s ``ScheduleStore``. This also
    sidesteps sqlite3's same-thread restriction cleanly: the connection is
    opened with ``check_same_thread=False`` but is provably never touched
    from two threads at once, because ``asyncio.to_thread`` calls that read
    or write it are always serialized by the lock first.
    """

    def __init__(self) -> None:
        self._entries: List[Tuple[Path, SessionDigest]] = []
        self._dense_vecs: List[List[float]] = []
        self._conn: Optional[sqlite3.Connection] = None
        self._model = None  # lazy TextEmbedding; see _ensure_model
        self._lock = asyncio.Lock()
        # corpus-text-hash -> embedding vector. digests are immutable once
        # written (one save at destroy), so a corpus's embedding never
        # changes — cache it across build() calls so a rebuild only embeds
        # NEW/changed digests, not the whole corpus every time. Without this
        # every search re-embedded all N digests (~4.8s at 83, measured),
        # which is what created the multi-second "dead zone" after a message.
        # Keyed on the corpus text itself (not updated_at) so it's correct by
        # construction — same text always maps to the same vector. Unbounded
        # by design: distinct corpora ≈ session count (small); each entry is
        # ~512 floats (~2KB), so even hundreds of sessions is a few MB.
        self._embed_cache: Dict[str, List[float]] = {}
        # session_dir -> (digest.json mtime_ns, parsed SessionDigest). The
        # embed cache above only saves the (already cheap) re-embed step —
        # the disk-scan itself was still a full read+JSON-parse of every
        # digest.json on EVERY build() call (900+ files measured live),
        # which is the actual dominant cost (18-28s cold-cache vs. <1s warm,
        # confirmed against real boot logs 2026-08-20). Keyed on mtime_ns
        # (not "already in cache") because a LIVE session's digest is
        # rewritten repeatedly at every item boundary while status="crashed"
        # (FlowControllerV2._on_item_done_checkpoint) — presence alone would
        # wrongly freeze that session's card at its first-ever checkpoint.
        self._digest_cache: Dict[Path, Tuple[int, SessionDigest]] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    # ── Build ────────────────────────────────────────────────────────────────

    async def build(self, history_root: Optional[Path] = None) -> None:
        """Scan history_root (default %USERPROFILE%\\HandQ\\History) for
        digest.json files and rebuild the index from scratch.

        Safe to call repeatedly (e.g. periodic refresh) — each call fully
        replaces the prior entries/vectors/connection. Both the scan and the
        FTS/embed rebuild run under ``self._lock`` via ``asyncio.to_thread``
        so this never blocks the bridge's event loop, and so two overlapping
        build() calls (from two sessions' messages) never race on
        ``self._digest_cache``.
        """
        root = history_root or _session_history_root()
        async with self._lock:
            self._entries = await asyncio.to_thread(self._scan_entries_sync, root)
            await asyncio.to_thread(self._build_indices_sync)

    def _scan_entries_sync(self, root: Path) -> List[Tuple[Path, SessionDigest]]:
        """Blocking half of build(): directory scan + digest load.

        Reuses ``self._digest_cache`` keyed on the digest.json's mtime_ns —
        a cache hit is one ``stat()`` call, a miss is the full read+parse
        that ``SessionDigest.load`` does. Must only be called while
        ``self._lock`` is held (see build()).
        """
        entries: List[Tuple[Path, SessionDigest]] = []
        if not root.exists():
            self._digest_cache.clear()
            return entries

        seen: set = set()
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            target = d / SessionDigest.DIGEST_FILENAME
            try:
                mtime_ns = target.stat().st_mtime_ns
            except OSError:
                continue
            seen.add(d)
            cached = self._digest_cache.get(d)
            if cached is not None and cached[0] == mtime_ns:
                entries.append((d, cached[1]))
                continue
            digest = SessionDigest.load(d)
            if digest is not None:
                self._digest_cache[d] = (mtime_ns, digest)
                entries.append((d, digest))
            else:
                self._digest_cache.pop(d, None)

        # Drop cache entries for session dirs that no longer exist/qualify —
        # unbounded growth otherwise across a long-running bridge process
        # as old History/ dirs get cleaned up externally.
        for stale in set(self._digest_cache) - seen:
            del self._digest_cache[stale]
        return entries

    def _build_indices_sync(self) -> None:
        """Blocking half of build(): FTS5 insert + batch dense embed.
        Must only be called while self._lock is held."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("CREATE VIRTUAL TABLE resume_fts USING fts5(idx UNINDEXED, corpus)")
        corpora = [_corpus_text(d) for _, d in self._entries]
        for i, corpus in enumerate(corpora):
            conn.execute(
                "INSERT INTO resume_fts (idx, corpus) VALUES (?, ?)",
                (i, " ".join(tokenize(corpus))),
            )
        conn.commit()
        self._conn = conn

        if corpora:
            # Incremental embed via _embed_cache: only NEW/changed corpora
            # hit the model; unchanged ones (the overwhelming common case,
            # since digests are immutable) reuse their cached vector. This
            # is what keeps a rebuild-before-every-search cheap (~100ms)
            # instead of re-embedding all N digests (~4.8s at 83, measured).
            corpora_lc = [c.lower() for c in corpora]
            keys = [
                hashlib.sha1(c.encode("utf-8")).hexdigest() for c in corpora_lc
            ]
            missing_idx = [i for i, k in enumerate(keys) if k not in self._embed_cache]
            if missing_idx:
                model = self._ensure_model()  # only load/call the model on a miss
                new_vecs = list(model.embed(
                    [corpora_lc[i] for i in missing_idx],
                    batch_size=EMBED_BATCH_SIZE,
                ))
                for i, v in zip(missing_idx, new_vecs):
                    self._embed_cache[keys[i]] = list(v)
            self._dense_vecs = [self._embed_cache[k] for k in keys]
        else:
            self._dense_vecs = []

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            vendored = _vendored_model_dir()
            # specific_model_path (a public fastembed kwarg) makes
            # download_model() return that path directly — no HuggingFace
            # cache-layout matching, no network reachability check at all,
            # even to probe a local cache. This is the offline/air-gapped
            # path: assets/models/bge-small-zh-v1.5/ ships with the build
            # (see electron/package.json build.extraFiles).
            if vendored.is_dir():
                self._model = TextEmbedding(
                    model_name=EMBED_MODEL_NAME,
                    specific_model_path=str(vendored),
                )
            else:
                # Dev-mode fallback — assets/models/ not pulled yet (or a
                # checkout that predates vendoring). fastembed downloads
                # from HuggingFace Hub into cache_dir on first call.
                _logger.warning(
                    "resume_index: vendored model not found at %s; "
                    "falling back to fastembed's HuggingFace-Hub download "
                    "path (requires network on first call)", vendored,
                )
                self._model = TextEmbedding(
                    model_name=EMBED_MODEL_NAME,
                    cache_dir=str(_model_cache_dir()),
                )
        return self._model

    # ── Search ───────────────────────────────────────────────────────────────

    async def search(self, query: str) -> List[ResumeCandidate]:
        """BM25 ∥ dense -> RRF fuse (ordering) -> per-candidate dense-cos
        gate (hit/miss decision, §6.4 steps 1-5, revised: no fixed result
        count — every candidate that individually clears DENSE_COS_GATE is
        returned, not just the top-N by rank).

        Returns [] when the index is empty or not even the best (top-1)
        match clears the gate — resume stays completely silent in that
        case, indistinguishable from a fresh task (§6.4 step 5).
        """
        async with self._lock:
            if not self._entries or self._conn is None:
                return []
            return await asyncio.to_thread(self._search_sync, query)

    def _search_sync(self, query: str) -> List[ResumeCandidate]:
        dense_scores = self._dense_scores_sync(query)
        if not dense_scores:
            return []
        _top1_idx, top1_cos = dense_scores[0]
        if top1_cos < DENSE_COS_GATE:
            return []

        bm_rank = self._bm25_rank_sync(query)
        de_rank = {idx: rank for rank, (idx, _cos) in enumerate(dense_scores)}
        dense_cos_by_idx = dict(dense_scores)

        candidate_idxs = set(bm_rank) | set(de_rank)
        rrf: Dict[int, float] = {}
        for idx in candidate_idxs:
            score = 0.0
            if idx in bm_rank:
                score += 1.0 / (RRF_K + bm_rank[idx] + 1)
            if idx in de_rank:
                score += 1.0 / (RRF_K + de_rank[idx] + 1)
            rrf[idx] = score

        # Per-candidate gate (not just top-1): every RETURNED candidate must
        # individually clear DENSE_COS_GATE on its own dense_cos — "the best
        # one cleared it" no longer implies the others are shown too. No
        # count cap: a query that genuinely matches 7 old sessions surfaces
        # all 7 (the renderer scrolls a long list; it does not silently
        # drop real matches to stay short — see the plan's "不需要 top3
        # 的结果，符合阈值的都应该拉出来").
        gated_idxs = [i for i in candidate_idxs if dense_cos_by_idx.get(i, 0.0) >= DENSE_COS_GATE]
        ordered = sorted(gated_idxs, key=lambda i: rrf[i], reverse=True)
        return [
            ResumeCandidate(
                session_dir=self._entries[i][0],
                digest=self._entries[i][1],
                dense_cos=dense_cos_by_idx.get(i, 0.0),
                rrf_score=rrf[i],
            )
            for i in ordered
        ]

    def _bm25_rank_sync(self, query: str) -> Dict[int, int]:
        terms = tokenize(query)
        if not terms or self._conn is None:
            return {}
        match = " OR ".join(f'"{t}"' for t in terms[:32])
        try:
            rows = self._conn.execute(
                "SELECT idx, bm25(resume_fts) FROM resume_fts WHERE resume_fts MATCH ? "
                "ORDER BY bm25(resume_fts) LIMIT ?",
                (match, _BM25_PREFILTER_LIMIT),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {int(idx): rank for rank, (idx, _score) in enumerate(rows)}

    def _dense_scores_sync(self, query: str) -> List[Tuple[int, float]]:
        """Returns [(entry_idx, cosine)], sorted by cosine descending."""
        if not self._dense_vecs:
            return []
        model = self._ensure_model()
        qv = list(list(model.embed([(query or "").lower()], batch_size=EMBED_BATCH_SIZE))[0])
        scored = [(self._cosine(qv, dv), i) for i, dv in enumerate(self._dense_vecs)]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(i, cos) for cos, i in scored]

    @staticmethod
    def _cosine(a, b) -> float:
        from ..infrastructure.long_term_memory.embedding.base import cosine

        return cosine(a, b)
