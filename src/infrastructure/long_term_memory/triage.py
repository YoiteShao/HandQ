"""DreamWorker — async background consumer of memory_candidates.

Loop responsibilities (every ``interval_seconds``):
1. Reset candidates stuck in ``triaging`` (worker died mid-call).
2. Pull up to ``batch_size`` pending candidates.
3. For each:
   a. PII pre-filter on raw_text.
   b. Find similar existing memory + knowledge entries (via FTS).
   c. Call the helper LLM with the triage prompt.
   d. PII post-filter on the verdict's content.
   e. Apply (insert / update / archive) per verdict.
   f. Mark candidate accepted / rejected / failed.

Every step has its own try/except so a single bad candidate cannot kill the
worker. The worker is a single asyncio.Task spawned inside
``LongTermMemory.init`` and cancelled on shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from .embedding import EmbeddingProvider, vec_to_bytes
from .embedding.base import cosine, vec_from_bytes
from .models import (
    ArchiveReason,
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
from .prompts import TRIAGE_SYSTEM, parse_verdict, render_user
from .store import SQLiteStore
from . import _constants as C

_logger = logging.getLogger("handq.ltm.dream")


def _extract_manual_text(raw_text: str) -> str:
    """Strip the boilerplate that ``submit_manual`` wraps around the
    user's actual /remember text, returning just the payload.

    submit_manual produces:
        # User explicitly asked to remember
        [SELF] <user text>

    We tolerate small drift in that template — if either the header
    line or the [SELF] tag is missing we still pull out the longest
    non-trivial line as a best-effort fallback so the override can
    always provide *something*.
    """
    if not raw_text:
        return ""
    text = raw_text.strip()
    # Most common path: split on '[SELF] ' and take the rest.
    marker = "[SELF] "
    idx = text.find(marker)
    if idx >= 0:
        return text[idx + len(marker):].strip()
    # Fallback: drop comment-style header lines and join.
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if lines:
        return " ".join(lines).strip()
    return text


def _looks_structured(text: str) -> bool:
    """Heuristic: does *text* look like a structured procedure?

    Returns True when it contains at least one markdown H2/H3 header
    OR a sufficient run of list items. Used to gate the verbatim
    bypass — long unstructured prose still goes through the LLM
    triage so it can be cleaned up rather than archived as-is.
    """
    headers = 0
    list_items = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## ") or line.startswith("### "):
            headers += 1
        elif (
            line.startswith("- ")
            or line.startswith("* ")
            or (len(line) > 2 and line[0].isdigit() and line[1] in ".)")
        ):
            list_items += 1
    if headers >= C.VERBATIM_MIN_STRUCTURE_HEADERS:
        return True
    if list_items >= C.VERBATIM_MIN_LIST_ITEMS:
        return True
    return False


def _user_handq_root() -> Path:
    """Mirror of bridge_main._user_handq_root (kept private here so the
    triage module doesn't import from bridge_main)."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _write_memory_note_file(entry_id: str, summary: str, content: str,
                            *, dimension: str, source: str) -> Optional[Path]:
    """Write a markdown mirror of a verbatim memory entry.

    Layout: ``%USERPROFILE%/HandQ/personality/memory_notes/<entry_id_short>.md``
    with a small YAML-ish frontmatter block so the file is
    self-describing if the user opens it standalone.

    The ``personality/`` parent groups every personalization artifact
    (memory.db, memory_notes/, ephemeral/) under one user-visible
    folder per ARCHITECTURE.md §1.5.

    Best-effort: any I/O error is logged and swallowed. The DB
    entry is the source of truth; failing to write the file is not
    a reason to fail the whole insert.
    """
    try:
        root = (
            _user_handq_root()
            / C.PERSONALITY_DATA_DIR
            / C.MANUAL_REMEMBER_MIRROR_DIR
        )
        root.mkdir(parents=True, exist_ok=True)
        # Use a short prefix of the uuid so the directory is
        # human-scannable; full id stays in the frontmatter for
        # traceability.
        path = root / f"{entry_id[:8]}.md"
        body = (
            f"---\n"
            f"id: {entry_id}\n"
            f"summary: {summary.replace(chr(10), ' ')}\n"
            f"dimension: {dimension}\n"
            f"source: {source}\n"
            f"created_at: {int(time.time())}\n"
            f"note: |\n"
            f"  Mirror of a verbatim /remember entry. The HandQ DB is\n"
            f"  the source of truth for recall; editing this file does\n"
            f"  NOT update the DB.\n"
            f"---\n\n"
            f"{content}\n"
        )
        path.write_text(body, encoding="utf-8")
        return path
    except Exception:
        _logger.exception("memory note mirror write failed for entry=%s", entry_id)
        return None


class DreamWorker:
    """Single-task background consumer."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        embedder: EmbeddingProvider,
        pii_filter: PIIFilter,
        config: dict,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._pii = pii_filter
        self._config = config
        self._helper_services: List = []
        # Adaptive cadence (see _constants.py §3):
        # - 60s floor — even when ``submit_candidate`` writes a fresh
        #   row, the worker waits at least one full minute before
        #   processing. This is deliberate: triage is a background
        #   job, the user-visible flow already returned, and a 60s
        #   dampener avoids hammering the LLM stack on bursty input.
        # - 3600s ceiling — empty cycles double the interval up to one
        #   hour, then plateau there.
        self._loop_interval: float = C.DREAM_INTERVAL_MIN_SEC
        self._batch_size: int = C.DREAM_BATCH_SIZE
        self._max_retry: int = C.DREAM_MAX_RETRY
        self._stuck_seconds: int = C.DREAM_STUCK_SECONDS
        # Counts main-loop iterations so the merge scanner can run on
        # every Nth cycle without needing a separate timer.
        self._cycle_count: int = 0

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._init_helper_pool()
        if not self._helper_services:
            _logger.warning(
                "no helper LLM services configured; dream worker idle "
                "(candidates will accumulate as 'pending')",
            )
            # Don't return — a future config reload might enable helpers; but
            # for v1 we just sleep forever, only waking on cancellation.
            while True:
                try:
                    await asyncio.sleep(self._loop_interval)
                except asyncio.CancelledError:
                    return

        _logger.info(
            "dream worker online: interval=%.1fs batch=%d max_retry=%d",
            self._loop_interval, self._batch_size, self._max_retry,
        )

        # Startup: backfill embeddings for any chunks that lack them.
        # Recovers from old dbs (entries inserted before P2 / when the
        # embedder was unavailable) and from inline warmup failures. Bounded
        # so we never spend too long blocking the first triage cycle.
        await self._backfill_embeddings(max_per_kind=C.DREAM_BACKFILL_STARTUP)

        while True:
            try:
                # Sleep at least 60s every iteration. The interval is
                # adaptive (geometric backoff up to 1h on idle), but the
                # 60s floor is a hard contract: triage is a background
                # job and we don't want bursts of submit_candidate calls
                # to translate into bursts of LLM work.
                await asyncio.sleep(self._loop_interval)
                self._cycle_count += 1
                resetn = await self._store.reset_stuck_triaging(self._stuck_seconds)
                if resetn:
                    _logger.info("reset %d stuck triaging candidates", resetn)
                cands = await self._store.next_pending_candidates(self._batch_size)
                for c in cands:
                    try:
                        await self._triage_one(c)
                    except Exception:
                        _logger.exception("triage failed cid=%s", c.id)
                # Adaptive interval update: snap back to MIN whenever we
                # found work; otherwise grow geometrically up to MAX.
                if cands:
                    if self._loop_interval != C.DREAM_INTERVAL_MIN_SEC:
                        _logger.debug(
                            "dream interval reset %.0f→%.0fs (work found)",
                            self._loop_interval, C.DREAM_INTERVAL_MIN_SEC,
                        )
                    self._loop_interval = C.DREAM_INTERVAL_MIN_SEC
                else:
                    new_interval = min(
                        self._loop_interval * 2.0,
                        C.DREAM_INTERVAL_MAX_SEC,
                    )
                    if new_interval != self._loop_interval:
                        _logger.debug(
                            "dream interval backoff %.0f→%.0fs (idle)",
                            self._loop_interval, new_interval,
                        )
                    self._loop_interval = new_interval
                # Opportunistic backfill: small slice per cycle so a slow
                # trickle of new entries with failed inline warmup get
                # caught up without spiking helper-LLM cost.
                await self._backfill_embeddings(max_per_kind=C.DREAM_BACKFILL_CYCLE)
                # Periodic post-hoc dedup. Every Nth cycle so the scan cost
                # is amortised; the dedup itself is cheap (pairwise cosine
                # over cached embeddings — no LLM calls).
                if self._cycle_count % C.MERGE_SCAN_EVERY_N_CYCLES == 0:
                    try:
                        await self._run_merge_scan()
                    except Exception:
                        _logger.exception("merge scan failed; will retry next cycle")
                # L2 / L3 dream synthesis. Gating uses WALL-CLOCK time,
                # not in-memory cycle counts: the bridge is interactive
                # and may be restarted multiple times a day, so a pure
                # cycle-count gate would cause synthesis to never fire
                # for users who don't keep HandQ running 24/7. We check
                # against the most recent ``dream_runs`` row instead —
                # that survives restarts, and a fresh DB just means
                # "fire on the next eligible cycle".
                if await self._should_run_synthesis(level=2):
                    try:
                        await self._run_dream_synthesis(level=2)
                    except Exception:
                        _logger.exception("L2 dream synthesis failed")
                if await self._should_run_synthesis(level=3):
                    try:
                        await self._run_dream_synthesis(level=3)
                    except Exception:
                        _logger.exception("L3 dream synthesis failed")
            except asyncio.CancelledError:
                _logger.info("dream worker cancelled")
                return
            except Exception:
                _logger.exception("dream-worker outer loop error; sleeping %ds",
                                  C.DREAM_ERROR_SLEEP_SECONDS)
                try:
                    await asyncio.sleep(C.DREAM_ERROR_SLEEP_SECONDS)
                except asyncio.CancelledError:
                    return

    # ── Per-candidate ───────────────────────────────────────────────────────

    async def _triage_one(self, c: Candidate) -> None:
        await self._store.set_candidate_status(c.id, CandidateStatus.TRIAGING)

        if self._pii.has_secret(c.raw_text):
            await self._store.set_candidate_status(
                c.id, CandidateStatus.REJECTED, reason=C.REASON_SENSITIVE_PRE,
            )
            return

        # ── /remember verbatim fast-path ──────────────────────────────
        # Long /remember payloads bypass the triage LLM entirely so the
        # user's text is preserved character-for-character. Two
        # criteria together gate this path: length >= threshold AND
        # the content "looks structured" (markdown headers OR a
        # multi-item list). Both checks together prevent an
        # accidental ramble from becoming a permanent immutable entry.
        # Short OR unstructured /remember still goes through normal
        # triage where the LLM does dedup and cleanup.
        if c.source == CandidateSource.MANUAL_REMEMBER.value:
            user_text = _extract_manual_text(c.raw_text)
            if (
                len(user_text) >= C.MANUAL_REMEMBER_VERBATIM_THRESHOLD
                and _looks_structured(user_text)
            ):
                try:
                    await self._apply_verbatim_remember(c, user_text)
                except Exception:
                    _logger.exception(
                        "verbatim remember insert failed cid=%s", c.id,
                    )
                    await self._store.set_candidate_status(
                        c.id, CandidateStatus.REJECTED,
                        reason="verbatim_insert_failed",
                    )
                return

        existing_mem = await self._fetch_similar(c.raw_text, kind=EntryKind.MEMORY.value)
        existing_kn = await self._fetch_similar(c.raw_text, kind=EntryKind.KNOWLEDGE.value)

        try:
            verdict = await self._call_llm(c, existing_mem, existing_kn)
        except Exception as exc:
            new_count = await self._store.bump_candidate_retry(c.id, error=str(exc))
            if new_count >= self._max_retry:
                await self._store.set_candidate_status(
                    c.id, CandidateStatus.FAILED, reason=C.REASON_MAX_RETRY,
                )
            return

        # ── Source-specific accept-override ───────────────────────────
        # MANUAL_REMEMBER candidates come from an explicit user
        # ``/remember <text>`` action. The LLM still gets to do its
        # triage (good summary extraction, dimension picking, dedup
        # via update vs create), but if it decided to ``skip``
        # entirely we override: the user said REMEMBER, so we
        # remember. PII safety is preserved — Step 5's post-filter
        # still runs on whatever content we end up writing.
        if c.source == CandidateSource.MANUAL_REMEMBER.value:
            if not verdict.worth_memory and not verdict.worth_knowledge:
                user_text = _extract_manual_text(c.raw_text)
                if user_text:
                    verdict.worth_memory = True
                    verdict.memory_action = VerdictAction.CREATE.value
                    verdict.memory_dimension = (
                        verdict.memory_dimension or MemoryDimension.AGENTIC
                    )
                    verdict.memory_summary = (
                        verdict.memory_summary or user_text[:120]
                    )
                    verdict.memory_content = (
                        verdict.memory_content or user_text
                    )
                    verdict.reason = (
                        (verdict.reason or "")
                        + " | manual_remember_force_accepted"
                    )[:200]

        # PII post-filter (trim per-track, never expose raw secrets)
        if verdict.worth_memory and self._pii.has_secret(verdict.memory_content):
            verdict.worth_memory = False
            verdict.reason = (verdict.reason + " | " + C.REASON_POST_FILTER_MEMORY)[:200]
        if verdict.worth_knowledge and self._pii.has_secret(verdict.knowledge_content):
            verdict.worth_knowledge = False
            verdict.reason = (verdict.reason + " | " + C.REASON_POST_FILTER_KNOWLEDGE)[:200]

        # Defensive guard: failed sessions never promote AGENTIC memory,
        # even if the LLM ignored the prompt rule. INSIGHT memory is fine
        # (a stable environment fact doesn't become false because a task
        # failed), and knowledge is also allowed (env constraints are
        # often the reason for the failure). Mirrors the source-specific
        # rule in TRIAGE_SYSTEM but enforced in code so misbehaving
        # models cannot poison the user-preference (agentic) track.
        if (
            c.source == CandidateSource.SESSION_FAILED.value
            and verdict.worth_memory
            and verdict.memory_dimension == MemoryDimension.AGENTIC
        ):
            verdict.worth_memory = False
            verdict.memory_action = VerdictAction.SKIP.value
            verdict.memory_dimension = None
            verdict.reason = (verdict.reason + " | " + C.REASON_GUARD_FAILED_NO_MEMORY)[:200]

        # Defensive guard: ACTIVITY_OBSERVER (background observation, not
        # user consent) cannot promote AGENTIC memory. Watching the user
        # type into VSCode is NOT evidence they prefer VSCode — they could
        # be debugging someone else's setup. INSIGHT memory (stable env
        # facts: "primary editor in use is VSCode") and knowledge
        # ("project rooted at C:\\HandQ") are fine.
        if (
            c.source == CandidateSource.ACTIVITY_OBSERVER.value
            and verdict.worth_memory
            and verdict.memory_dimension == MemoryDimension.AGENTIC
        ):
            verdict.worth_memory = False
            verdict.memory_action = VerdictAction.SKIP.value
            verdict.memory_dimension = None
            verdict.reason = (
                verdict.reason + " | guard:activity_no_agentic_memory"
            )[:200]

        wrote_mem = wrote_kn = False
        if verdict.worth_memory:
            try:
                await self._apply_memory(c, verdict)
                wrote_mem = True
            except Exception:
                _logger.exception("apply_memory failed cid=%s", c.id)
        if verdict.worth_knowledge:
            try:
                await self._apply_knowledge(c, verdict)
                wrote_kn = True
            except Exception:
                _logger.exception("apply_knowledge failed cid=%s", c.id)

        if wrote_mem and wrote_kn:
            status = CandidateStatus.ACCEPTED_BOTH
        elif wrote_mem:
            status = CandidateStatus.ACCEPTED_MEMORY
        elif wrote_kn:
            status = CandidateStatus.ACCEPTED_KNOWLEDGE
        else:
            status = CandidateStatus.REJECTED
        await self._store.set_candidate_status(
            c.id, status, reason=(verdict.reason or "")[:80],
        )

    async def _apply_memory(self, c: Candidate, v: TriageVerdict) -> None:
        if not v.memory_dimension:
            return
        if v.memory_action == VerdictAction.UPDATE.value and v.memory_update_id:
            await self._store.update_memory_entry(
                v.memory_update_id,
                new_summary=v.memory_summary,
                new_content=v.memory_content,
            )
            return
        entry_id = await self._store.insert_memory_entry(
            dimension=v.memory_dimension,
            summary=v.memory_summary,
            content=v.memory_content,
            candidate_id=c.id,
            source=c.source,
            source_ref=c.source_ref,
        )
        await self._maybe_warmup_embedding(entry_id, kind=EntryKind.MEMORY.value)

    async def _apply_verbatim_remember(self, c: Candidate, user_text: str) -> None:
        """Insert a /remember candidate as INSIGHT memory verbatim,
        skipping the LLM triage stage.

        Why INSIGHT and not AGENTIC: long procedures are usually FACTS
        about how to do something ("here's the workflow…") rather than
        agent behaviour rules ("always do X"). INSIGHT keeps them
        injectable as background context without polluting the agentic
        track. Users who want a long agentic rule can /remember the
        short imperative form (which goes through normal triage).

        Storage layout:
          1. The full user_text is written to a .md file under
             %USERPROFILE%/HandQ/memory_notes/<entry_id_short>.md.
             That file is the canonical user-facing artifact —
             editor-friendly, backup-friendly, git-friendly.
          2. The DB entry stores the same text in chunks (for FTS +
             embedding recall) AND points ``source_ref`` at the file
             path so the admin panel / future tooling can open the
             file directly. The chunked DB copy is a derived index;
             editing the file later does NOT update the DB (no
             watcher in v1).

        Summary derivation: take the first non-empty stripped line
        truncated to 120 chars. Procedural texts almost always start
        with a meaningful header line ("How to deploy", "Setup steps:"),
        so this is robust in practice.
        """
        first_line = next(
            (ln.strip() for ln in user_text.splitlines() if ln.strip()),
            user_text[:120],
        )
        # Strip a leading markdown header marker so the summary doesn't
        # carry "## " etc. — looks awkward in the recall block.
        summary = first_line.lstrip("#").strip()[:120] or user_text[:120]

        # We need an id BEFORE the file write so the file name and the
        # entry's id agree. Generate up-front and pass through.
        import uuid
        entry_id = str(uuid.uuid4())

        # Step 1: write the canonical .md file. Best-effort — if the
        # filesystem rejects, we still proceed with the DB-only path
        # so the user's intent is preserved.
        file_path = await asyncio.to_thread(
            _write_memory_note_file,
            entry_id, summary, user_text,
            dimension=MemoryDimension.INSIGHT.value,
            source=c.source,
        )
        # Step 2: insert DB entry. ``source_ref`` becomes the file path
        # (or the original ref if file write failed) so admin tooling
        # can find the .md.
        ref = str(file_path) if file_path is not None else c.source_ref
        actual_id = await self._store.insert_memory_entry_with_id(
            entry_id=entry_id,
            dimension=MemoryDimension.INSIGHT,
            summary=summary,
            content=user_text,
            candidate_id=c.id,
            source=c.source,
            source_ref=ref,
        )
        await self._maybe_warmup_embedding(actual_id, kind=EntryKind.MEMORY.value)
        await self._store.set_candidate_status(
            c.id, CandidateStatus.ACCEPTED_MEMORY,
            reason="verbatim_remember",
        )
        _logger.info(
            "verbatim /remember accepted cid=%s entry=%s len=%d file=%s",
            c.id[:8], actual_id[:8], len(user_text), file_path,
        )

    async def _apply_knowledge(self, c: Candidate, v: TriageVerdict) -> None:
        if not v.knowledge_category:
            return
        if v.knowledge_action == VerdictAction.UPDATE.value and v.knowledge_update_id:
            await self._store.update_knowledge_entry(
                v.knowledge_update_id,
                new_summary=v.knowledge_summary,
                new_content=v.knowledge_content,
            )
            return
        entry_id = await self._store.insert_knowledge_entry(
            category=v.knowledge_category,
            summary=v.knowledge_summary,
            content=v.knowledge_content,
            candidate_id=c.id,
            source=c.source,
            source_ref=c.source_ref,
        )
        await self._maybe_warmup_embedding(entry_id, kind=EntryKind.KNOWLEDGE.value)

    async def _maybe_warmup_embedding(self, entry_id: str, *, kind: str) -> None:
        if not self._embedder.available:
            return
        full = (
            await self._store.get_memory_entry_full(entry_id) if kind == EntryKind.MEMORY.value
            else await self._store.get_knowledge_entry_full(entry_id)
        )
        if not full:
            return
        for chunk in full.chunks:
            cached = await self._store.get_embedding(
                chunk.id, kind, self._embedder.provider, self._embedder.model,
            )
            if cached:
                continue
            try:
                vec = (await self._embedder.embed([chunk.text]))[0]
            except Exception:
                _logger.exception(
                    "embedding warmup failed entry=%s chunk=%s", entry_id, chunk.id,
                )
                return
            await self._store.put_embedding(
                chunk_id=chunk.id, kind=kind,
                provider=self._embedder.provider, model=self._embedder.model,
                dims=self._embedder.dims, hash_=chunk.hash,
                embedding=vec_to_bytes(vec),
            )

    async def _backfill_embeddings(self, *, max_per_kind: int) -> None:
        """Embed and cache chunks that lack an embedding for the current
        (provider, model). Caps work per cycle so the dream loop stays
        responsive when there's a large legacy corpus.

        Embeds the whole missing batch in a single ``embed(...)`` call —
        the OpenAI-compatible /v1/embeddings endpoint accepts an array
        input, so N chunks = 1 roundtrip = 1 cold-start cost. One-per-
        chunk would multiply the cold-start cost by N.

        No-op when the embedder is unavailable (FTS-only fallback path);
        recall still works on the BM25 branch alone in that mode.
        """
        if not self._embedder.available:
            return
        for kind in (EntryKind.MEMORY.value, EntryKind.KNOWLEDGE.value):
            try:
                rows = await self._store.list_chunks_missing_embedding(
                    kind=kind,
                    provider=self._embedder.provider,
                    model=self._embedder.model,
                    limit=max_per_kind,
                )
            except Exception:
                _logger.exception(
                    "backfill: list_chunks_missing_embedding failed kind=%s", kind,
                )
                continue
            if not rows:
                continue

            texts = [r[2] for r in rows]
            try:
                vecs = await self._embedder.embed(texts)
            except Exception:
                _logger.exception(
                    "backfill: batch embed failed kind=%s n=%d", kind, len(texts),
                )
                continue
            if len(vecs) != len(rows):
                _logger.warning(
                    "backfill: embed returned %d vecs for %d chunks; skipping kind=%s",
                    len(vecs), len(rows), kind,
                )
                continue

            for (chunk_id, _entry_id, _text, hash_), vec in zip(rows, vecs):
                try:
                    await self._store.put_embedding(
                        chunk_id=chunk_id, kind=kind,
                        provider=self._embedder.provider,
                        model=self._embedder.model,
                        dims=self._embedder.dims,
                        hash_=hash_ or "",
                        embedding=vec_to_bytes(vec),
                    )
                except Exception:
                    _logger.exception(
                        "backfill put_embedding failed kind=%s chunk=%s", kind, chunk_id,
                    )
            _logger.info("backfilled %d %s chunks", len(rows), kind)

    # ── Existing-similar fetch ──────────────────────────────────────────────

    async def _fetch_similar(
        self, raw_text: str, *, kind: str, limit: int = 3,
    ) -> List[Entry]:
        if kind == EntryKind.MEMORY.value:
            rows = await self._store.fts_search_memory(raw_text, limit=limit)
            entries: List[Entry] = []
            for r in rows:
                eid, _chunk_id, _text, summary, dim, _created, _rank = r
                try:
                    dim_enum = MemoryDimension(dim) if dim else None
                except ValueError:
                    dim_enum = None
                entries.append(Entry(
                    id=eid, kind=EntryKind.MEMORY,
                    dimension=dim_enum, summary=summary,
                ))
            return entries
        if kind == EntryKind.KNOWLEDGE.value:
            rows = await self._store.fts_search_knowledge(raw_text, limit=limit)
            entries = []
            for r in rows:
                eid, _chunk_id, _text, summary, cat, _created, _rank = r
                try:
                    cat_enum = KnowledgeCategory(cat) if cat else None
                except ValueError:
                    cat_enum = None
                entries.append(Entry(
                    id=eid, kind=EntryKind.KNOWLEDGE,
                    category=cat_enum, summary=summary,
                ))
            return entries
        return []

    # ── LLM ─────────────────────────────────────────────────────────────────

    async def _call_llm(
        self,
        c: Candidate,
        existing_mem: List[Entry],
        existing_kn: List[Entry],
    ) -> TriageVerdict:
        from src.infrastructure.llm_pool import call_with_fallback

        user_prompt = render_user(c, existing_mem, existing_kn)
        result = await call_with_fallback(
            self._helper_services,
            dict(
                messages=[
                    {"role": "system", "content": TRIAGE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                json_mode=True,
                max_tokens=900,
            ),
        )
        return await parse_verdict(
            result.content or "",
            llm_services=self._helper_services,
        )

    async def _init_helper_pool(self) -> None:
        """Build the LLM service list the dream worker calls.

        Tier choice (per ``_constants.TRIAGE_LLM_TIER``):
          - "receptionist" (default): same model class the user-facing
            receptionist uses. Triage is async background so latency
            doesn't matter; quality of the worth-memory / worth-knowledge
            classification does, and matches receptionist intent
            classification in difficulty.
          - "from_data": cheapest/lowest-priority slice. Use only when
            cost dominates quality (very high LTM volume).
        """
        try:
            from src.infrastructure.role_resolver import resolve_role_models
            from src.infrastructure.llm_pool import make_from_data_services
            from src.infrastructure.anthropic_streaming_service import (
                AnthropicStreamingService,
            )
            from src.infrastructure.llm_service import LLMService
        except Exception:
            _logger.exception("dream worker: failed to import LLM stack")
            return

        llm_cfg = self._config.get("llm") or {}
        roles = resolve_role_models(llm_cfg)
        api_key = llm_cfg.get("API_KEY")
        if not api_key:
            _logger.warning("llm.API_KEY missing; dream worker has no helper services")
            return

        tier = C.TRIAGE_LLM_TIER
        if tier == C.TIER_RECEPTIONIST:
            models: List[str] = list(roles.get(C.TIER_RECEPTIONIST) or [])
            tier_label = C.TIER_RECEPTIONIST
        elif tier == C.TIER_FROM_DATA:
            models = list(roles.get(C.TIER_FROM_DATA) or [])
            tier_label = C.TIER_FROM_DATA
        else:
            _logger.warning("unknown TRIAGE_LLM_TIER=%r; defaulting to receptionist", tier)
            models = list(roles.get(C.TIER_RECEPTIONIST) or [])
            tier_label = C.TIER_RECEPTIONIST

        if not models:
            # Tier-specific list empty → fall back through the chain:
            #   receptionist → from_data → derive from agent pool.
            _logger.info(
                "triage tier %s has no models; falling back to from_data/agent",
                tier_label,
            )
            from_data = list(roles.get(C.TIER_FROM_DATA) or [])
            if from_data:
                models = from_data
                tier_label = f"{C.TIER_FROM_DATA} (fallback)"
            else:
                agent_models = list(roles.get("agent") or [])
                # Annotate as List[LLMService] (the supertype) so List
                # invariance doesn't reject this where make_from_data_services
                # expects List[LLMService]. AnthropicStreamingService IS-A
                # LLMService, but the literal list-comp is inferred as
                # List[AnthropicStreamingService].
                services: List[LLMService] = [
                    AnthropicStreamingService(model=m, api_key=api_key)
                    for m in agent_models
                ]
                self._helper_services = (
                    make_from_data_services(services) if services else []
                )
                if self._helper_services:
                    _logger.info(
                        "triage tier: agent-derived fallback (%d service(s))",
                        len(self._helper_services),
                    )
                return

        self._helper_services = [
            AnthropicStreamingService(model=m, api_key=api_key)
            for m in models
        ]
        if self._helper_services:
            _logger.info(
                "triage tier=%s: %d service(s)",
                tier_label, len(self._helper_services),
            )

    # ── Merge / exact-group scanner (post-hoc dedup) ────────────────────────

    async def _run_merge_scan(self) -> None:
        """Run dedup scan on both memory and knowledge entries.

        For each kind: pull every non-archived entry's representative
        embedding (chunk 0), brute-force cosine over all pairs, classify:
          - similarity >= MERGE_EXACT_THRESHOLD   → auto-merge
          - similarity >= MERGE_PROPOSE_THRESHOLD → propose to user

        Skips pairs younger than ``MERGE_MIN_PAIR_AGE_SECONDS`` so two
        candidates from the same triage batch (which the LLM already
        decided are distinct) aren't second-guessed.
        """
        if not self._embedder.available:
            return  # No vectors → nothing to compare
        for kind in (EntryKind.MEMORY.value, EntryKind.KNOWLEDGE.value):
            try:
                await self._run_merge_scan_kind(kind)
            except Exception:
                _logger.exception("merge scan failed for kind=%s", kind)

    async def _run_merge_scan_kind(self, kind: str) -> None:
        rows = await self._store.list_entry_centroids(
            kind=kind,
            provider=self._embedder.provider,
            model=self._embedder.model,
        )
        if len(rows) < 2:
            return  # Nothing to compare against

        # Decode once up-front so we don't unpack bytes inside the inner loop.
        decoded = []
        now = int(time.time())
        for entry_id, summary, created_at, updated_at, emb_bytes in rows:
            vec = vec_from_bytes(emb_bytes)
            if vec:
                decoded.append((
                    entry_id, summary, int(created_at or 0),
                    int(updated_at or now), vec,
                ))
        if len(decoded) < 2:
            return

        exact_pairs = []
        propose_pairs = []
        # O(n^2) pairwise. At ~1500 entries × ~1024 dim that's
        # ~1.1M comparisons × cheap cosine ≈ <2s in pure Python.
        # When we cross 5000 entries we'll need an ANN structure.
        for i in range(len(decoded)):
            ai, asum, ac, au, av = decoded[i]
            for j in range(i + 1, len(decoded)):
                bi, bsum, bc, bu, bv = decoded[j]
                # Skip same-batch pairs the triage LLM just decided about.
                age_seconds = now - max(ac, bc)
                if age_seconds < C.MERGE_MIN_PAIR_AGE_SECONDS:
                    continue
                sim = cosine(av, bv)
                if sim >= C.MERGE_EXACT_THRESHOLD:
                    exact_pairs.append((ai, bi, au, bu, sim))
                elif sim >= C.MERGE_PROPOSE_THRESHOLD:
                    propose_pairs.append((ai, bi, sim))

        # Auto-merge: keep newer (higher updated_at), archive older.
        for ai, bi, au, bu, sim in exact_pairs:
            keep, drop = (ai, bi) if au >= bu else (bi, ai)
            try:
                await self._auto_merge(
                    kind=kind, keep_id=keep, drop_id=drop, similarity=sim,
                )
            except Exception:
                _logger.exception(
                    "auto-merge failed kind=%s keep=%s drop=%s",
                    kind, keep[:8], drop[:8],
                )

        # Propose: write to merge_proposals; bridge / future UI surfaces.
        for ai, bi, sim in propose_pairs:
            try:
                await self._store.insert_merge_proposal(
                    kind=kind, entry_a_id=ai, entry_b_id=bi, similarity=sim,
                )
            except Exception:
                _logger.exception(
                    "insert_merge_proposal failed kind=%s a=%s b=%s",
                    kind, ai[:8], bi[:8],
                )

        if exact_pairs or propose_pairs:
            _logger.info(
                "merge scan kind=%s: auto-merged=%d, proposed=%d",
                kind, len(exact_pairs), len(propose_pairs),
            )

    async def _auto_merge(
        self,
        *,
        kind: str,
        keep_id: str,
        drop_id: str,
        similarity: float,
    ) -> None:
        """Archive *drop_id* with reason='auto_dedup'; keep *keep_id*.

        We do NOT physically combine content — the kept entry already covers
        the topic; the dropped one's archived row remains queryable for
        history. Future versions could merge text via LLM if the kept entry
        is missing detail from the dropped one.
        """
        if kind == EntryKind.MEMORY.value:
            await self._store.archive_memory_entry(
                drop_id, reason=ArchiveReason.AUTO_DEDUP.value,
            )
        elif kind == EntryKind.KNOWLEDGE.value:
            await self._store.archive_knowledge_entry(
                drop_id, reason=ArchiveReason.AUTO_DEDUP.value,
            )
        else:
            return
        # Record the merge action so it's auditable / undoable later.
        await self._store.insert_merge_proposal(
            kind=kind, entry_a_id=keep_id, entry_b_id=drop_id,
            similarity=similarity,
        )
        # Immediately resolve as 'merged' since we already executed.
        # Find the proposal we just wrote (it's the most recent pending pair).
        for pid, _k, ea, eb, _sim, _ts in await self._store.list_merge_proposals(
            status="pending", limit=10,
        ):
            if {ea, eb} == {keep_id, drop_id}:
                await self._store.resolve_merge_proposal(
                    proposal_id=pid, status="merged",
                )
                break

    # ── L2 / L3 dream synthesis ─────────────────────────────────────────────

    async def _should_run_synthesis(self, *, level: int) -> bool:
        """Wall-clock gate for L2/L3 dream synthesis.

        Returns True when the most recent successful run for *level*
        (across BOTH memory and knowledge kinds) is older than the
        configured cadence. Surviving restarts is the whole point: a
        cycle-counter gate would reset on every bridge launch, so a
        user who closes HandQ before 24h is up would never see L2.

        Cadence:
          - level 2  →  ``DREAM_L2_EVERY_N_CYCLES * DREAM_INTERVAL_SECONDS``
                        (default 24h)
          - level 3  →  ``DREAM_L3_EVERY_N_CYCLES * DREAM_INTERVAL_SECONDS``
                        (default 7d)

        Failure-mode handling: if a previous run is still ``running`` or
        ended in ``failed`` we still allow a new attempt past the cadence
        so a transient failure can't permanently block synthesis.
        """
        if level == 2:
            min_age_seconds = (
                C.DREAM_L2_EVERY_N_CYCLES * C.DREAM_INTERVAL_SECONDS
            )
        elif level == 3:
            min_age_seconds = (
                C.DREAM_L3_EVERY_N_CYCLES * C.DREAM_INTERVAL_SECONDS
            )
        else:
            return False

        now = int(time.time())
        # Look at both memory and knowledge runs; pick the more recent
        # of the two as "level last fired". Either one being recent is
        # enough to skip — the *_run_dream_synthesis* call itself loops
        # over both kinds anyway.
        latest = 0
        for kind in (EntryKind.MEMORY.value, EntryKind.KNOWLEDGE.value):
            try:
                row = await self._store.get_last_dream_run(level=level, kind=kind)
            except Exception:
                _logger.debug("get_last_dream_run failed L%d/%s", level, kind,
                              exc_info=True)
                continue
            if row is None:
                continue
            # row = (id, started_at, ended_at, status)
            started = int(row[1] or 0)
            if started > latest:
                latest = started
        if latest == 0:
            # No run yet on this DB → fire as soon as we have material.
            return True
        return (now - latest) >= int(min_age_seconds)

    async def _run_dream_synthesis(self, *, level: int) -> None:
        """Run L2 (level=2) or L3 (level=3) synthesis on both memory and
        knowledge entries.

        L2 input  : raw L1 entries (synthesis_level=0) from the recent window
        L3 input  : L2 patterns    (synthesis_level=2) from the recent window

        For each kind, this is one ``dream_runs`` row that records the
        cluster / accept / skip counts so the user can audit what happened.
        """
        if level not in (2, 3):
            return
        if not self._embedder.available:
            _logger.info(
                "skipping L%d synthesis: embedder unavailable (no clustering possible)",
                level,
            )
            return
        if not self._helper_services:
            _logger.info("skipping L%d synthesis: no helper LLM services", level)
            return

        for kind in (EntryKind.MEMORY.value, EntryKind.KNOWLEDGE.value):
            try:
                await self._run_dream_synthesis_kind(level=level, kind=kind)
            except Exception:
                _logger.exception(
                    "L%d synthesis failed for kind=%s", level, kind,
                )

    async def _run_dream_synthesis_kind(self, *, level: int, kind: str) -> None:
        """Run one L-level synthesis for one kind."""
        # ── 1. Pull source entries
        if level == 2:
            source_level = 0
            window_seconds = C.DREAM_L2_WINDOW_SECONDS
            cluster_threshold = C.DREAM_L2_CLUSTER_THRESHOLD
            min_size = C.DREAM_L2_MIN_CLUSTER_SIZE
            max_size = C.DREAM_L2_MAX_CLUSTER_SIZE
            max_clusters = C.DREAM_L2_MAX_CLUSTERS_PER_RUN
        else:
            source_level = 2
            window_seconds = C.DREAM_L3_WINDOW_SECONDS
            cluster_threshold = C.DREAM_L3_CLUSTER_THRESHOLD
            min_size = C.DREAM_L3_MIN_CLUSTER_SIZE
            max_size = C.DREAM_L3_MAX_CLUSTER_SIZE
            max_clusters = C.DREAM_L3_MAX_CLUSTERS_PER_RUN

        rows = await self._store.list_entries_by_synthesis_level(
            kind=kind,
            synthesis_level=source_level,
            provider=self._embedder.provider,
            model=self._embedder.model,
            since_seconds=window_seconds,
        )
        if len(rows) < min_size:
            # Record the skip in dream_runs so _should_run_synthesis can
            # latch the cadence gate. Without this row the gate sees
            # latest=0 forever and keeps re-entering every cycle, which
            # spams "L%d/%s skipped" logs (especially L3, whose source is
            # synthesis_level=2 — empty until L2 has produced patterns).
            run_id = await self._store.insert_dream_run(level=level, kind=kind)
            await self._store.update_dream_run(
                run_id, status="skipped",
                source_count=len(rows),
                cluster_count=0,
                accepted_count=0,
                skipped_count=0,
            )
            _logger.info(
                "L%d/%s skipped: only %d source entries (need >=%d)",
                level, kind, len(rows), min_size,
            )
            return

        run_id = await self._store.insert_dream_run(level=level, kind=kind)

        try:
            # ── 2. Decode embeddings + greedy cluster
            decoded = []
            for entry_id, facet, summary, content_text, emb_bytes in rows:
                vec = vec_from_bytes(emb_bytes)
                if vec:
                    decoded.append({
                        "id": entry_id,
                        "facet": facet,
                        "summary": summary,
                        "content": content_text,
                        "vec": vec,
                    })

            clusters = self._greedy_cluster(
                decoded, threshold=cluster_threshold,
                min_size=min_size,
            )
            # ── 3. Slice + cap
            clusters = clusters[:max_clusters]

            # ── 4. For each cluster, call LLM and apply verdict
            accepted = 0
            skipped = 0
            for cluster in clusters:
                if len(cluster) > max_size:
                    cluster = cluster[:max_size]
                try:
                    ok = await self._apply_synth_cluster(
                        level=level, kind=kind,
                        cluster=cluster, run_id=run_id,
                    )
                except Exception:
                    _logger.exception(
                        "L%d/%s cluster apply failed", level, kind,
                    )
                    skipped += 1
                    continue
                if ok:
                    accepted += 1
                else:
                    skipped += 1

            await self._store.update_dream_run(
                run_id, status="complete",
                source_count=len(decoded),
                cluster_count=len(clusters),
                accepted_count=accepted,
                skipped_count=skipped,
            )
            _logger.info(
                "L%d/%s synthesis: sources=%d clusters=%d accepted=%d skipped=%d",
                level, kind, len(decoded), len(clusters), accepted, skipped,
            )
        except Exception as exc:
            await self._store.update_dream_run(
                run_id, status="failed", error=str(exc)[:500],
            )
            raise

    @staticmethod
    def _greedy_cluster(
        items: List[dict], *, threshold: float, min_size: int,
    ) -> List[List[dict]]:
        """Single-pass greedy clustering on already-decoded embeddings.

        For each item, find the existing cluster whose centroid (= first
        member's vector — cheap proxy that works because clusters end up
        small and tight) is within ``threshold`` cosine. If found, append.
        Else, start a new singleton cluster.

        Returns clusters with size >= min_size, ordered by descending size
        (larger clusters processed first because they have stronger signal).

        This is O(n^2) worst case but n is bounded by the recent-window
        query (~hundreds), so cheap. For >5000 entries we'd need an
        actual ANN structure.
        """
        clusters: List[List[dict]] = []
        for it in items:
            placed = False
            v = it["vec"]
            for cluster in clusters:
                if cosine(v, cluster[0]["vec"]) >= threshold:
                    cluster.append(it)
                    placed = True
                    break
            if not placed:
                clusters.append([it])
        clusters = [c for c in clusters if len(c) >= min_size]
        clusters.sort(key=len, reverse=True)
        return clusters

    async def _apply_synth_cluster(
        self,
        *,
        level: int,
        kind: str,
        cluster: List[dict],
        run_id: str,
    ) -> bool:
        """Call the L2/L3 LLM, write a synthesis entry on accept.

        Returns True iff a synthesised entry was written.
        """
        from src.infrastructure.llm_pool import call_with_fallback
        from .dream_prompts import (
            L2_SYSTEM_KNOWLEDGE,
            L2_SYSTEM_MEMORY,
            L2_USER_TEMPLATE,
            L3_SYSTEM_KNOWLEDGE,
            L3_SYSTEM_MEMORY,
            L3_USER_TEMPLATE,
            parse_synth_verdict,
            render_cluster,
            validate_facet_for_kind,
        )

        if level == 2:
            system_prompt = (
                L2_SYSTEM_MEMORY if kind == EntryKind.MEMORY.value
                else L2_SYSTEM_KNOWLEDGE
            )
            user_template = L2_USER_TEMPLATE
        else:
            system_prompt = (
                L3_SYSTEM_MEMORY if kind == EntryKind.MEMORY.value
                else L3_SYSTEM_KNOWLEDGE
            )
            user_template = L3_USER_TEMPLATE

        rendered = render_cluster([
            (it["id"], it.get("facet") or "", it["summary"]) for it in cluster
        ])
        user_msg = user_template.format(
            n=len(cluster), numbered_entries=rendered,
        )

        try:
            # Bound the synthesis LLM call — without a timeout a wedged
            # helper service could pin the dream loop for hours, blocking
            # every other tick. 120s covers worst-case cold-start +
            # ~12-entry cluster prompt; well below DREAM_INTERVAL_MAX.
            result = await asyncio.wait_for(
                call_with_fallback(
                    self._helper_services,
                    dict(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        json_mode=True,
                        max_tokens=900,
                    ),
                ),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            _logger.warning("L%d/%s LLM call timed out", level, kind)
            return False
        except Exception:
            _logger.exception("L%d/%s LLM call failed", level, kind)
            return False

        verdict = parse_synth_verdict(result.content or "")
        if not verdict.get("worth_synth"):
            return False

        facet = validate_facet_for_kind(
            kind=kind, facet=verdict.get("target_facet"),
        )
        if not facet:
            _logger.info(
                "L%d/%s: invalid facet %r in verdict; skipping",
                level, kind, verdict.get("target_facet"),
            )
            return False

        await self._store.insert_synthesis_entry(
            kind=kind,
            target_facet=facet,
            summary=verdict["summary"],
            content=verdict["content"],
            synthesis_level=level,
            source_entry_ids=[it["id"] for it in cluster],
            source_run_id=run_id,
        )
        # Embed the new synthesis entry so it shows up in dense recall
        # immediately (no need to wait for backfill).
        # We need to find the new entry's chunk_id; simplest path is to
        # let the next cycle's backfill catch it.
        return True
