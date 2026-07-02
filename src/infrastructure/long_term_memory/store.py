"""SQLite-backed store for the LTM 2.0 long-term memory subsystem.

Single physical file at ``%USERPROFILE%\\HandQ\\personality\\memory.db``
hosting three logical namespaces:

    obs_*  observation layer (snapshots / OCR frames / events / sessions /
           semantic events / pipeline runs / summaries)
    mem_*  memory layer (mem_entries with kind ∈ memory|knowledge|skill_proposal
           plus chunks / FTS / versions / candidates / embedding_cache /
           recall_log / correction_proposals / dream_runs)
    ent_*  entity graph (principals / aliases / sightings)

Implementation notes
--------------------
* Pure stdlib: ``sqlite3`` + ``asyncio.to_thread`` — no aiosqlite dependency.
* WAL mode + single ``asyncio.Lock`` for writes; reads run lock-free under WAL.
* All public methods are ``async`` and dispatch the synchronous SQLite work
  to a thread pool; this keeps the asyncio event loop responsive while the
  bridge serves IPC.
* Force-reset on open() if a legacy v1-v4 DB is detected (memory_files
  present without obs_snapshots) — the LTM 2.0 redesign is destructive
  on existing user data by design (see plan).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from . import _constants as C
from .models import (
    Candidate,
    CandidateStatus,
    Chunk,
    CorrectionKind,
    CorrectionProposal,
    CorrectionStatus,
    Entry,
    EntryKind,
    KnowledgeCategory,
    MemoryDimension,
    Principal,
    PrincipalKind,
    SemanticEvent,
    SemanticStatus,
    Session,
    SkillProposal,
    SkillProposalStatus,
    Summary,
    TriggerKind,
)
from .schema import DDL_BOOTSTRAP, MIGRATIONS

_logger = logging.getLogger("handq.ltm.store")


class _SyncConn:
    """Thin shim exposing the migration-friendly subset of the connection API."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await asyncio.to_thread(self._raw.execute, sql, params)

    async def commit(self) -> None:
        await asyncio.to_thread(self._raw.commit)


def _now() -> int:
    return int(time.time())


class SQLiteStore:
    """Async-friendly wrapper over a single sqlite3 connection (LTM 2.0)."""

    def __init__(self, raw: sqlite3.Connection, write_lock: asyncio.Lock) -> None:
        self._raw = raw
        self._write_lock = write_lock

    # ── Lifecycle ──────────────────────────────────────────────────────────

    @classmethod
    async def open(cls, db_path: Path) -> "SQLiteStore":
        db_path.parent.mkdir(parents=True, exist_ok=True)

        def _bootstrap(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA temp_store=MEMORY")
            for stmt in DDL_BOOTSTRAP:
                conn.execute(stmt)
            conn.commit()

        def _is_legacy_db(conn: sqlite3.Connection) -> bool:
            """Detect a legacy v1-v4 DB by table-presence (memory_files
            present and obs_snapshots absent → legacy → force-reset)."""
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_files', 'obs_snapshots')"
            ).fetchall()
            present = {r[0] for r in rows}
            return "memory_files" in present and "obs_snapshots" not in present

        def _force_reset_and_reopen() -> sqlite3.Connection:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db_path) + suffix)
                if not p.exists():
                    continue
                for attempt in range(5):
                    try:
                        p.unlink()
                        break
                    except PermissionError:
                        time.sleep(0.1)
                    except FileNotFoundError:
                        break
                else:
                    raise RuntimeError(
                        f"Force-reset failed: could not unlink {p} after 5 attempts"
                    )
            fresh = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
            _bootstrap(fresh)
            return fresh

        def _connect() -> sqlite3.Connection:
            raw = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
            _bootstrap(raw)
            if _is_legacy_db(raw):
                _logger.warning(
                    "LTM force-reset: legacy v1-v4 schema detected at %s "
                    "(memory_files present, obs_snapshots absent). Wiping and "
                    "re-bootstrapping under LTM 2.0 baseline.",
                    db_path,
                )
                raw.close()
                return _force_reset_and_reopen()
            return raw

        raw = await asyncio.to_thread(_connect)
        store = cls(raw, asyncio.Lock())
        await store._apply_migrations()
        return store

    async def close(self) -> None:
        await asyncio.to_thread(self._raw.close)

    async def _apply_migrations(self) -> None:
        cur_version = int(await self.get_meta("schema_version") or "0")
        if cur_version >= len(MIGRATIONS):
            return
        shim = _SyncConn(self._raw)
        for v, fn in enumerate(MIGRATIONS, start=1):
            if v <= cur_version:
                continue
            _logger.info("applying ltm migration v%d (%s)", v, fn.__name__)
            async with self._write_lock:
                await fn(shim)
                await asyncio.to_thread(
                    self._raw.execute,
                    "INSERT INTO migration_log(version, applied_at, note) VALUES (?, ?, ?)",
                    (v, _now(), fn.__name__),
                )
                await asyncio.to_thread(
                    self._raw.execute,
                    "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(v)),
                )
                await asyncio.to_thread(self._raw.commit)

    # ── Tiny helpers ───────────────────────────────────────────────────────

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._raw.execute, sql, params)
            await asyncio.to_thread(self._raw.commit)

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[tuple]:
        def _q() -> List[tuple]:
            cur = self._raw.execute(sql, params)
            try:
                return list(cur.fetchall())
            finally:
                cur.close()
        return await asyncio.to_thread(_q)

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[tuple]:
        def _q() -> Optional[tuple]:
            cur = self._raw.execute(sql, params)
            try:
                return cur.fetchone()
            finally:
                cur.close()
        return await asyncio.to_thread(_q)

    # ── Meta ───────────────────────────────────────────────────────────────

    async def get_meta(self, key: str) -> Optional[str]:
        row = await self._fetchone(
            "SELECT value FROM memory_meta WHERE key=?", (key,),
        )
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    # ── Candidates (non-observation submit queue) ──────────────────────────

    async def insert_candidate(
        self,
        *,
        source: str,
        raw_text: str,
        source_ref: Optional[str] = None,
        hint: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        cid = str(uuid.uuid4())
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        now = _now()
        await self._execute(
            """INSERT INTO mem_candidates
               (id, source, source_ref, raw_text, hint, metadata, status,
                retry_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (cid, source, source_ref, raw_text, hint, meta_json, now, now),
        )
        return cid

    async def next_pending_candidates(self, limit: int = 8) -> List[Candidate]:
        rows = await self._fetchall(
            """SELECT id, source, source_ref, raw_text, hint, metadata,
                      retry_count, created_at
               FROM mem_candidates
               WHERE status='pending'
               ORDER BY created_at ASC
               LIMIT ?""",
            (limit,),
        )
        out: List[Candidate] = []
        for r in rows:
            try:
                meta = json.loads(r[5]) if r[5] else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                meta = {}
            out.append(Candidate(
                id=r[0], source=r[1], source_ref=r[2], raw_text=r[3],
                hint=r[4], metadata=meta, status=CandidateStatus.PENDING,
                retry_count=int(r[6]), created_at=int(r[7]),
            ))
        return out

    async def set_candidate_status(
        self, cid: str, status, *, reason: Optional[str] = None,
    ) -> None:
        s = status.value if hasattr(status, "value") else str(status)
        await self._execute(
            "UPDATE mem_candidates SET status=?, reason=?, updated_at=? WHERE id=?",
            (s, reason, _now(), cid),
        )

    async def get_candidate_status(self, cid: str) -> Optional[str]:
        row = await self._fetchone(
            "SELECT status FROM mem_candidates WHERE id=?", (cid,),
        )
        return row[0] if row else None

    async def heartbeat_candidate(self, cid: str) -> None:
        await self._execute(
            "UPDATE mem_candidates SET updated_at=? WHERE id=? AND status='triaging'",
            (_now(), cid),
        )

    async def bump_candidate_retry(self, cid: str, *, error: str) -> int:
        async with self._write_lock:
            await asyncio.to_thread(
                self._raw.execute,
                """UPDATE mem_candidates
                   SET retry_count=retry_count+1, last_error=?, status='pending', updated_at=?
                   WHERE id=?""",
                (error[:200], _now(), cid),
            )
            await asyncio.to_thread(self._raw.commit)
            cur = await asyncio.to_thread(
                self._raw.execute,
                "SELECT retry_count FROM mem_candidates WHERE id=?",
                (cid,),
            )
            try:
                row = await asyncio.to_thread(cur.fetchone)
            finally:
                await asyncio.to_thread(cur.close)
        return int(row[0]) if row else 0

    async def reset_stuck_triaging(self, older_than: int = 300) -> int:
        cutoff = _now() - older_than
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                """UPDATE mem_candidates
                   SET status='pending', updated_at=?
                   WHERE status='triaging' AND updated_at < ?""",
                (_now(), cutoff),
            )
            try:
                rowcount = cur.rowcount
            finally:
                await asyncio.to_thread(cur.close)
            await asyncio.to_thread(self._raw.commit)
        return rowcount

    async def list_candidates(
        self,
        *,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
    ) -> List[Candidate]:
        sql = (
            "SELECT id, source, source_ref, raw_text, hint, metadata, retry_count, created_at, status "
            "FROM mem_candidates WHERE 1=1"
        )
        params: list = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, tuple(params))
        out: List[Candidate] = []
        for r in rows:
            try:
                meta = json.loads(r[5]) if r[5] else {}
            except json.JSONDecodeError:
                meta = {}
            try:
                cstatus = CandidateStatus(r[8])
            except ValueError:
                cstatus = CandidateStatus.PENDING
            out.append(Candidate(
                id=r[0], source=r[1], source_ref=r[2], raw_text=r[3],
                hint=r[4], metadata=meta, status=cstatus,
                retry_count=int(r[6]), created_at=int(r[7]),
            ))
        return out

    async def prune_candidate_raw_text(self, older_than_ts: int) -> int:
        """Clear raw_text for fully-processed candidates older than cutoff."""
        statuses = ("accepted_memory", "accepted_knowledge", "accepted_both", "rejected")
        placeholders = ",".join("?" * len(statuses))
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                f"UPDATE mem_candidates SET raw_text='' "
                f"WHERE status IN ({placeholders}) "
                f"AND created_at < ? AND raw_text != ''",
                (*statuses, older_than_ts),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    # ── mem_entries unified CRUD (memory + knowledge + skill_proposal) ─────

    async def insert_entry(
        self,
        *,
        kind: EntryKind,
        summary: str,
        content: str,
        dimension: Optional[MemoryDimension] = None,
        category: Optional[KnowledgeCategory] = None,
        candidate_id: Optional[str] = None,
        source: str = "",
        source_ref: Optional[str] = None,
        source_event_id: Optional[str] = None,
        frame: Optional[dict] = None,
        principal_ids: Optional[List[str]] = None,
        skill_status: Optional[SkillProposalStatus] = None,
        skill_signature: Optional[dict] = None,
        skill_fingerprint: Optional[str] = None,
        recurrence_count: int = 1,
        synthesis_level: int = 0,
        source_entry_ids: Optional[List[str]] = None,
        entry_id: Optional[str] = None,
    ) -> str:
        """Insert a new mem_entries row + chunked content into mem_chunks.

        ``kind`` selects the discriminator row. ``dimension`` is required for
        kind=memory; ``category`` for kind=knowledge; skill fields for
        kind=skill_proposal. Caller-supplied ``entry_id`` reserves the id
        BEFORE insert (for /remember mirror files etc.).
        """
        from .chunking import chunk_markdown
        eid = entry_id or str(uuid.uuid4())
        now = _now()
        chunks = chunk_markdown(content)
        frame_json = json.dumps(frame, ensure_ascii=False) if frame else None
        principal_json = json.dumps(principal_ids or [], ensure_ascii=False) if principal_ids else None
        source_entry_json = json.dumps(source_entry_ids or [], ensure_ascii=False) if source_entry_ids else None
        sig_json = json.dumps(skill_signature, ensure_ascii=False) if skill_signature else None
        skill_status_val = skill_status.value if skill_status else None

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")
                self._raw.execute(
                    """INSERT INTO mem_entries
                       (id, kind, dimension, category, skill_status, skill_signature,
                        skill_fingerprint, recurrence_count, summary, frame_json,
                        source_event_id, source, source_ref, archived, synthesis_level,
                        source_entry_ids, version, principal_ids, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?)""",
                    (eid, kind.value,
                     dimension.value if dimension else None,
                     category.value if category else None,
                     skill_status_val, sig_json, skill_fingerprint,
                     recurrence_count, summary[:120], frame_json,
                     source_event_id, source, source_ref, synthesis_level,
                     source_entry_json, principal_json, now, now),
                )
                for i, c in enumerate(chunks):
                    self._raw.execute(
                        """INSERT INTO mem_chunks
                           (id, entry_id, chunk_index, text, start_line, end_line, hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), eid, i, c.text,
                         c.start_line, c.end_line, c.hash),
                    )
                self._raw.commit()
            except Exception:
                self._raw.rollback()
                raise

        async with self._write_lock:
            await asyncio.to_thread(_txn)
        return eid

    # Backward-compat thin wrappers — preserve facade caller API surface.
    async def insert_memory_entry(
        self,
        *,
        dimension: MemoryDimension,
        summary: str,
        content: str,
        candidate_id: Optional[str],
        source: str,
        source_ref: Optional[str],
        frame: Optional[dict] = None,
    ) -> str:
        return await self.insert_entry(
            kind=EntryKind.MEMORY, dimension=dimension, summary=summary,
            content=content, candidate_id=candidate_id, source=source,
            source_ref=source_ref, frame=frame,
        )

    async def insert_memory_entry_with_id(
        self,
        *,
        entry_id: str,
        dimension: MemoryDimension,
        summary: str,
        content: str,
        candidate_id: Optional[str],
        source: str,
        source_ref: Optional[str],
        frame: Optional[dict] = None,
    ) -> str:
        return await self.insert_entry(
            entry_id=entry_id, kind=EntryKind.MEMORY, dimension=dimension,
            summary=summary, content=content, candidate_id=candidate_id,
            source=source, source_ref=source_ref, frame=frame,
        )

    async def insert_knowledge_entry(
        self,
        *,
        category: KnowledgeCategory,
        summary: str,
        content: str,
        candidate_id: Optional[str],
        source: str,
        source_ref: Optional[str],
    ) -> str:
        return await self.insert_entry(
            kind=EntryKind.KNOWLEDGE, category=category, summary=summary,
            content=content, candidate_id=candidate_id, source=source,
            source_ref=source_ref,
        )

    async def _update_entry_versioned(
        self,
        *,
        kind: EntryKind,
        entry_id: str,
        new_summary: str,
        new_content: str,
    ) -> None:
        """Bump version: archive old chunks via mem_versions, replace with new."""
        from .chunking import chunk_markdown
        now = _now()
        new_chunks = chunk_markdown(new_content)

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")
                cur = self._raw.execute(
                    "SELECT version, summary FROM mem_entries WHERE id=? AND kind=?",
                    (entry_id, kind.value),
                )
                row = cur.fetchone()
                cur.close()
                if not row:
                    raise ValueError(f"entry not found: {entry_id} (kind={kind.value})")
                old_version, old_summary = int(row[0]), row[1]

                cur2 = self._raw.execute(
                    "SELECT chunk_index, text, start_line, end_line "
                    "FROM mem_chunks WHERE entry_id=? ORDER BY chunk_index",
                    (entry_id,),
                )
                old_chunks = list(cur2.fetchall())
                cur2.close()
                chunks_json = json.dumps(
                    [{"i": c[0], "t": c[1], "sl": c[2], "el": c[3]} for c in old_chunks],
                    ensure_ascii=False,
                )

                self._raw.execute(
                    "INSERT INTO mem_versions "
                    "(id, entry_id, version, summary, chunks_json, archived_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), entry_id, old_version, old_summary,
                     chunks_json, now),
                )

                cur3 = self._raw.execute(
                    "SELECT id FROM mem_chunks WHERE entry_id=?", (entry_id,),
                )
                old_chunk_ids = [r[0] for r in cur3.fetchall()]
                cur3.close()
                if old_chunk_ids:
                    placeholders = ",".join(["?"] * len(old_chunk_ids))
                    self._raw.execute(
                        f"DELETE FROM mem_embedding_cache "
                        f"WHERE chunk_kind=? AND chunk_id IN ({placeholders})",
                        (kind.value,) + tuple(old_chunk_ids),
                    )

                self._raw.execute(
                    "DELETE FROM mem_chunks WHERE entry_id=?", (entry_id,),
                )
                for i, c in enumerate(new_chunks):
                    self._raw.execute(
                        "INSERT INTO mem_chunks "
                        "(id, entry_id, chunk_index, text, start_line, end_line, hash) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), entry_id, i, c.text,
                         c.start_line, c.end_line, c.hash),
                    )
                self._raw.execute(
                    "UPDATE mem_entries SET summary=?, version=version+1, updated_at=? WHERE id=?",
                    (new_summary[:120], now, entry_id),
                )
                self._raw.commit()
            except Exception:
                self._raw.rollback()
                raise

        async with self._write_lock:
            await asyncio.to_thread(_txn)

    async def update_memory_entry(
        self, entry_id: str, *, new_summary: str, new_content: str,
    ) -> None:
        await self._update_entry_versioned(
            kind=EntryKind.MEMORY, entry_id=entry_id,
            new_summary=new_summary, new_content=new_content,
        )

    async def update_knowledge_entry(
        self, entry_id: str, *, new_summary: str, new_content: str,
    ) -> None:
        await self._update_entry_versioned(
            kind=EntryKind.KNOWLEDGE, entry_id=entry_id,
            new_summary=new_summary, new_content=new_content,
        )

    async def archive_entry(
        self, entry_id: str, *, kind: EntryKind, reason: str,
    ) -> None:
        await self._execute(
            "UPDATE mem_entries SET archived=1, archived_reason=?, updated_at=? "
            "WHERE id=? AND kind=?",
            (reason, _now(), entry_id, kind.value),
        )

    async def archive_memory_entry(self, entry_id: str, *, reason: str) -> None:
        await self.archive_entry(entry_id, kind=EntryKind.MEMORY, reason=reason)

    async def archive_knowledge_entry(self, entry_id: str, *, reason: str) -> None:
        await self.archive_entry(entry_id, kind=EntryKind.KNOWLEDGE, reason=reason)

    async def set_superseded_by(
        self, *, kind: str, entry_id: str, superseded_by_id: str,
    ) -> None:
        await self._execute(
            "UPDATE mem_entries SET superseded_by=?, updated_at=? WHERE id=? AND kind=?",
            (superseded_by_id, _now(), entry_id, kind),
        )

    async def list_memory_entries(
        self,
        *,
        dimension: Optional[MemoryDimension] = None,
        archived: bool = False,
        limit: int = 50,
    ) -> List[Entry]:
        sql = (
            "SELECT id, dimension, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at, frame_json "
            "FROM mem_entries WHERE kind='memory' AND archived=?"
        )
        params: list = [1 if archived else 0]
        if dimension is not None:
            sql += " AND dimension=?"
            params.append(dimension.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, tuple(params))
        return [_row_to_memory_entry(r) for r in rows]

    async def list_knowledge_entries(
        self,
        *,
        category: Optional[KnowledgeCategory] = None,
        archived: bool = False,
        limit: int = 50,
    ) -> List[Entry]:
        sql = (
            "SELECT id, category, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at, frame_json "
            "FROM mem_entries WHERE kind='knowledge' AND archived=?"
        )
        params: list = [1 if archived else 0]
        if category is not None:
            sql += " AND category=?"
            params.append(category.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, tuple(params))
        return [_row_to_knowledge_entry(r) for r in rows]

    async def get_memory_entry_full(self, entry_id: str) -> Optional[Entry]:
        return await self._get_entry_full(entry_id, kind=EntryKind.MEMORY)

    async def get_knowledge_entry_full(self, entry_id: str) -> Optional[Entry]:
        return await self._get_entry_full(entry_id, kind=EntryKind.KNOWLEDGE)

    async def _get_entry_full(self, entry_id: str, *, kind: EntryKind) -> Optional[Entry]:
        row = await self._fetchone(
            "SELECT id, dimension, category, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at, frame_json "
            "FROM mem_entries WHERE id=? AND kind=?",
            (entry_id, kind.value),
        )
        if not row:
            return None
        entry = _row_to_entry_full(row, kind)
        chunk_rows = await self._fetchall(
            "SELECT id, chunk_index, text, hash, start_line, end_line "
            "FROM mem_chunks WHERE entry_id=? ORDER BY chunk_index",
            (entry_id,),
        )
        entry.chunks = [
            Chunk(id=cr[0], entry_id=entry_id, chunk_index=int(cr[1]),
                  text=cr[2], hash=cr[3], start_line=cr[4], end_line=cr[5])
            for cr in chunk_rows
        ]
        entry.content = "\n\n".join(c.text for c in entry.chunks)
        return entry

    # ── FTS search ─────────────────────────────────────────────────────────

    async def fts_search_memory(
        self,
        query: str,
        *,
        dimension: Optional[MemoryDimension] = None,
        limit: int = 15,
    ) -> List[tuple]:
        return await self._fts_search(
            kind=EntryKind.MEMORY.value, query=query,
            facet_value=dimension.value if dimension else None, limit=limit,
        )

    async def fts_search_knowledge(
        self,
        query: str,
        *,
        category: Optional[KnowledgeCategory] = None,
        limit: int = 15,
    ) -> List[tuple]:
        return await self._fts_search(
            kind=EntryKind.KNOWLEDGE.value, query=query,
            facet_value=category.value if category else None, limit=limit,
        )

    async def _fts_search(
        self, *, kind: str, query: str, facet_value: Optional[str], limit: int,
    ) -> List[tuple]:
        """Returns (entry_id, chunk_id, text, summary, facet, created_at, rank, frame_os)."""
        safe = self._sanitize_fts_query(query)
        facet_col = "dimension" if kind == EntryKind.MEMORY.value else "category"
        sql = (
            f"SELECT e.id, c.id, c.text, e.summary, e.{facet_col}, "
            f"       e.created_at, bm25(mem_chunks_fts) AS rank, e.frame_os "
            f"FROM mem_chunks_fts fts "
            f"JOIN mem_chunks c ON c.id = fts.chunk_id "
            f"JOIN mem_entries e ON e.id = c.entry_id "
            f"WHERE mem_chunks_fts MATCH ? AND e.archived=0 AND e.kind=?"
        )
        params: list = [safe, kind]
        if facet_value:
            sql += f" AND e.{facet_col}=?"
            params.append(facet_value)
        if C.RECALL_EXCLUDE_OBSERVATION_INSIGHTS:
            # Drop W-tier passive-observation activity-snapshot insights — they
            # surface as domain-similar-but-useless recall noise. Predicate is
            # safe for knowledge rows (e.dimension is NULL there → no match).
            sql += (
                " AND NOT (e.kind='memory' AND e.dimension='insight' "
                "AND e.source='semantic_event')"
            )
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            return await self._fetchall(sql, tuple(params))
        except sqlite3.OperationalError as exc:
            _logger.debug("FTS search failed (%s); returning empty", exc)
            return []

    @staticmethod
    def _sanitize_fts_query(q: str) -> str:
        """OR-of-quoted-tokens FTS5 query, safe against reserved words."""
        import re
        tokens = re.findall(r"[\w一-鿿]+", q.lower())
        if not tokens:
            return '""'
        return " OR ".join(f'"{t}"' for t in tokens[:32])

    # ── Embedding cache ────────────────────────────────────────────────────

    async def get_embedding(
        self, chunk_id: str, kind: str, provider: str, model: str,
    ) -> Optional[bytes]:
        row = await self._fetchone(
            "SELECT embedding FROM mem_embedding_cache "
            "WHERE chunk_id=? AND chunk_kind=? AND provider=? AND model=?",
            (chunk_id, kind, provider, model),
        )
        return row[0] if row else None

    async def put_embedding(
        self,
        *,
        chunk_id: str,
        kind: str,
        provider: str,
        model: str,
        dims: int,
        hash_: str,
        embedding: bytes,
    ) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO mem_embedding_cache "
            "(chunk_id, chunk_kind, provider, model, dims, hash, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, kind, provider, model, dims, hash_, embedding, _now()),
        )

    async def list_embedded_chunks(
        self, *, kind: str, provider: str, model: str,
    ) -> List[Tuple]:
        """Returns (entry_id, chunk_id, text, summary, facet, created_at, hash, embedding, frame_os)."""
        facet_col = "dimension" if kind == EntryKind.MEMORY.value else "category"
        sql = (
            f"SELECT e.id, c.id, c.text, e.summary, e.{facet_col}, e.created_at, "
            f"       ec.hash, ec.embedding, e.frame_os "
            f"FROM mem_chunks c "
            f"JOIN mem_entries e ON e.id = c.entry_id "
            f"JOIN mem_embedding_cache ec ON ec.chunk_id = c.id "
            f"  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            f"WHERE e.archived=0 AND e.kind=?"
        )
        if C.RECALL_EXCLUDE_OBSERVATION_INSIGHTS:
            # Mirror the _fts_search exclusion on the dense branch so W-tier
            # passive-observation insights are dropped from both recall paths.
            sql += (
                " AND NOT (e.kind='memory' AND e.dimension='insight' "
                "AND e.source='semantic_event')"
            )
        return await self._fetchall(sql, (kind, provider, model, kind))

    async def list_chunks_missing_embedding(
        self, *, kind: str, provider: str, model: str, limit: int = 100,
    ) -> List[Tuple]:
        """Chunks that lack a cached embedding for (provider, model)."""
        sql = (
            "SELECT c.id, c.entry_id, c.text, c.hash "
            "FROM mem_chunks c "
            "JOIN mem_entries e ON e.id = c.entry_id "
            "WHERE e.archived=0 AND e.kind=? "
            "  AND NOT EXISTS ("
            "     SELECT 1 FROM mem_embedding_cache ec "
            "     WHERE ec.chunk_id = c.id "
            "       AND ec.chunk_kind=? AND ec.provider=? AND ec.model=?"
            "  ) LIMIT ?"
        )
        return await self._fetchall(sql, (kind, kind, provider, model, limit))

    # ── Correction proposals (retriage outputs) ────────────────────────────

    async def insert_correction_proposal(
        self,
        *,
        kind: CorrectionKind,
        target_kind: EntryKind,
        target_entry_id: str,
        target_version: int,
        target_archived: bool,
        payload: Optional[dict],
        confidence: Optional[float],
        rule_version: int,
        parent_run_id: Optional[str],
        rationale: str,
        rationale_pii_scrubbed: bool,
    ) -> str:
        pid = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        await self._execute(
            "INSERT INTO mem_correction_proposals "
            "(id, kind, target_entry_id, target_version, target_archived, "
            " payload, confidence, rule_version, parent_run_id, rationale, "
            " rationale_pii_scrubbed, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (pid, kind.value, target_entry_id,
             int(target_version), 1 if target_archived else 0,
             payload_json, confidence, int(rule_version), parent_run_id,
             rationale[:2000], 1 if rationale_pii_scrubbed else 0, _now()),
        )
        return pid

    async def prune_correction_proposals(self, older_than_ts: int) -> int:
        """Delete orphaned ``status='pending'`` correction/merge proposals
        older than *older_than_ts* (unix seconds).

        Pending rows are never consumed — there is no review UI — so they
        only accumulate. The merge no-helper fallback used to re-stage the
        same pair every scan, and retriage leaves low-confidence 'archive'
        proposals pending; both are dead weight. We sweep ONLY 'pending':
        terminal rows ('merged' / 'kept_distinct') are the decided-memo the
        merge scanner reads to skip already-judged pairs, and 'applied' /
        'stale' are the audit trail of actions actually taken.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM mem_correction_proposals "
                "WHERE status='pending' AND created_at < ?",
                (older_than_ts,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    async def get_retriage_progress(self, rule_version: int) -> Optional[str]:
        return await self.get_meta(f"retriage_progress_v{int(rule_version)}")
    async def set_retriage_progress(
        self, rule_version: int, last_entry_id: str,
    ) -> None:
        await self.set_meta(
            f"retriage_progress_v{int(rule_version)}", last_entry_id,
        )

    async def clear_retriage_progress(self, rule_version: int) -> None:
        await self._execute(
            "DELETE FROM memory_meta WHERE key=?",
            (f"retriage_progress_v{int(rule_version)}",),
        )

    # ── Recall log ─────────────────────────────────────────────────────────

    async def insert_mem_recall_log_batch(
        self, rows: List[Tuple[str, str, int]],
    ) -> None:
        if not rows:
            return
        async with self._write_lock:
            await asyncio.to_thread(
                self._raw.executemany,
                "INSERT INTO mem_recall_log (entry_id, kind, recalled_at) VALUES (?, ?, ?)",
                rows,
            )
            await asyncio.to_thread(self._raw.commit)

    async def count_recent_recalls(
        self, *, entry_id: str, kind: str, since_seconds: int,
    ) -> int:
        cutoff = _now() - int(since_seconds)
        row = await self._fetchone(
            "SELECT COUNT(*) FROM mem_recall_log "
            "WHERE entry_id=? AND kind=? AND recalled_at >= ?",
            (entry_id, kind, cutoff),
        )
        return int(row[0]) if row else 0

    async def prune_mem_recall_log(self, older_than_ts: int) -> int:
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM mem_recall_log WHERE recalled_at < ?",
                (older_than_ts,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    # ── obs_snapshots ──────────────────────────────────────────────────────

    async def insert_obs_snapshot(
        self,
        *,
        snapshot_id: Optional[str] = None,
        captured_at: int,
        monitor_index: int,
        monitor_label: str = "",
        window_title: Optional[str] = None,
        process_name: Optional[str] = None,
        browser_url: Optional[str] = None,
        top_window_titles: Optional[List[str]] = None,
        ax_text: Optional[str] = None,
        parsed_json: Optional[dict] = None,
        frame: Optional[dict] = None,
        focus_rect: Optional[Tuple[int, int, int, int]] = None,
        ocr_used_focus_rect: bool = False,
        system_idle_sec: Optional[int] = None,
        novelty_score: float = 1.0,
        tier: str = "hot",
    ) -> str:
        sid = snapshot_id or str(uuid.uuid4())
        fx, fy, fw, fh = (focus_rect or (None, None, None, None))
        await self._execute(
            """INSERT INTO obs_snapshots
               (id, captured_at, monitor_index, monitor_label, window_title,
                process_name, browser_url, top_window_titles, ax_text, parsed_json,
                frame_json, focus_rect_x, focus_rect_y, focus_rect_w, focus_rect_h,
                ocr_used_focus_rect, system_idle_sec, novelty_score, tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, captured_at, monitor_index, monitor_label, window_title,
             process_name, browser_url,
             json.dumps(top_window_titles, ensure_ascii=False) if top_window_titles else None,
             ax_text,
             json.dumps(parsed_json, ensure_ascii=False) if parsed_json else None,
             json.dumps(frame, ensure_ascii=False) if frame else None,
             fx, fy, fw, fh, 1 if ocr_used_focus_rect else 0,
             system_idle_sec, novelty_score, tier),
        )
        return sid

    async def assign_snapshot_to_session(
        self, snapshot_id: str, session_id: str,
    ) -> None:
        await self._execute(
            "UPDATE obs_snapshots SET session_id=? WHERE id=?",
            (session_id, snapshot_id),
        )

    async def list_unassigned_snapshots(self, limit: int = 200) -> List[tuple]:
        """Snapshots with session_id IS NULL (awaiting aggregation)."""
        return await self._fetchall(
            "SELECT id, captured_at, monitor_index, process_name, window_title, "
            "frame_json, system_idle_sec, tier FROM obs_snapshots "
            "WHERE session_id IS NULL "
            "ORDER BY captured_at ASC LIMIT ?",
            (limit,),
        )

    async def prune_obs_snapshots(self, older_than_ms: int) -> int:
        """Delete raw snapshots older than cutoff. captured_at is unix MS.

        Cascades to obs_ocr_frames (FK ON DELETE CASCADE) and fires the
        obs_snapshots_ad trigger to evict obs_snapshots_fts rows.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM obs_snapshots WHERE captured_at < ?",
                (older_than_ms,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    # ── obs_ocr_frames ─────────────────────────────────────────────────────

    async def insert_obs_ocr_frame(
        self,
        *,
        snapshot_id: str,
        text: str,
        confidence: Optional[float] = None,
        pipeline_version: str = "",
        is_focus_rect: bool = False,
        embedding: Optional[bytes] = None,
    ) -> str:
        fid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO obs_ocr_frames
               (id, snapshot_id, text, confidence, embedding, pipeline_version,
                captured_at, is_focus_rect)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (fid, snapshot_id, text, confidence, embedding, pipeline_version,
             _now(), 1 if is_focus_rect else 0),
        )
        return fid

    # ── obs_events ─────────────────────────────────────────────────────────

    async def insert_obs_event(
        self,
        *,
        session_id: Optional[str],
        kind: str,
        data: Optional[dict] = None,
        sort_order: int = 0,
        occurred_at: Optional[int] = None,
    ) -> str:
        eid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO obs_events (id, session_id, kind, data, sort_order, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (eid, session_id, kind,
             json.dumps(data, ensure_ascii=False) if data else None,
             sort_order, occurred_at or int(time.time() * 1000)),
        )
        return eid

    async def prune_obs_events(self, older_than_ms: int) -> int:
        """Delete state-change events older than cutoff. occurred_at is unix MS.

        The only writer (SessionAggregator) stamps occurred_at from the
        snapshot's captured_at, which is unix milliseconds; the default-write
        path above also uses ms, so the cutoff must be ms.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM obs_events WHERE occurred_at < ?",
                (older_than_ms,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    # ── obs_sessions ───────────────────────────────────────────────────────

    async def insert_obs_session(
        self,
        *,
        session_key: str,
        trigger_kind: str,
        started_at: int,
        frame_os: Optional[str] = None,
        frame_host: Optional[str] = None,
        primary_process: Optional[str] = None,
        primary_window_title: Optional[str] = None,
    ) -> str:
        sid = str(uuid.uuid4())
        try:
            await self._execute(
                """INSERT INTO obs_sessions
                   (id, session_key, trigger_kind, started_at, frame_os, frame_host,
                    primary_process, primary_window_title)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, session_key, trigger_kind, started_at,
                 frame_os, frame_host, primary_process, primary_window_title),
            )
        except sqlite3.IntegrityError:
            # Idempotent re-aggregation: same session_key already exists.
            row = await self._fetchone(
                "SELECT id FROM obs_sessions WHERE session_key=?", (session_key,),
            )
            return row[0] if row else sid
        return sid

    async def close_obs_session(
        self,
        session_id: str,
        *,
        ended_at: int,
        snapshot_count: int,
        apps_seen: List[str],
        principal_ids: Optional[List[str]] = None,
    ) -> None:
        await self._execute(
            """UPDATE obs_sessions
               SET ended_at=?, snapshot_count=?, apps_seen=?, principal_ids=?
               WHERE id=?""",
            (ended_at, snapshot_count,
             json.dumps(apps_seen, ensure_ascii=False),
             json.dumps(principal_ids or [], ensure_ascii=False),
             session_id),
        )

    async def set_obs_session_status(
        self,
        session_id: str,
        *,
        semantic_status: Optional[str] = None,
    ) -> None:
        sets, params = [], []
        if semantic_status:
            sets.append("semantic_status=?")
            params.append(semantic_status)
        if not sets:
            return
        params.append(session_id)
        await self._execute(
            f"UPDATE obs_sessions SET {', '.join(sets)} WHERE id=?",
            tuple(params),
        )

    async def list_sessions_pending_extraction(self, limit: int = 8) -> List[tuple]:
        return await self._fetchall(
            "SELECT id, trigger_kind, started_at, ended_at, frame_os, frame_host, "
            "primary_process, primary_window_title, snapshot_count, apps_seen "
            "FROM obs_sessions "
            "WHERE semantic_status='pending' AND ended_at IS NOT NULL "
            "ORDER BY started_at ASC LIMIT ?",
            (limit,),
        )

    # ── obs_semantic_events ────────────────────────────────────────────────

    async def insert_obs_semantic_event(
        self,
        *,
        session_id: Optional[str] = None,
        synthetic_origin: Optional[str] = None,
        title: str,
        description: str = "",
        category: Optional[str] = None,
        entities: Optional[List[str]] = None,
        apps: Optional[List[str]] = None,
        time_range_start: int = 0,
        time_range_end: int = 0,
        task_worthy: bool = False,
        worth_memory: bool = False,
        worth_knowledge: bool = False,
        worth_skill: bool = False,
        frame_os: Optional[str] = None,
        frame_host: Optional[str] = None,
        frame_confidence: Optional[float] = None,
    ) -> str:
        eid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO obs_semantic_events
               (id, session_id, synthetic_origin, extracted_at, title, description,
                category, entities, apps, time_range_start, time_range_end,
                task_worthy, worth_memory, worth_knowledge, worth_skill,
                frame_os, frame_host, frame_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, session_id, synthetic_origin, _now(), title[:200], description,
             category,
             json.dumps(entities, ensure_ascii=False) if entities else None,
             json.dumps(apps, ensure_ascii=False) if apps else None,
             time_range_start, time_range_end,
             1 if task_worthy else 0,
             1 if worth_memory else 0,
             1 if worth_knowledge else 0,
             1 if worth_skill else 0,
             frame_os, frame_host, frame_confidence),
        )
        return eid

    async def list_semantic_events_pending_triage(self, limit: int = 8) -> List[tuple]:
        # Pending = not yet referenced from accepted_entries (proxy: triage hasn't run)
        return await self._fetchall(
            "SELECT id, session_id, synthetic_origin, title, description, category, "
            "       entities, apps, frame_os, frame_host, frame_confidence, task_worthy "
            "FROM obs_semantic_events "
            "WHERE accepted_entries IS NULL "
            "ORDER BY extracted_at ASC LIMIT ?",
            (limit,),
        )

    async def set_semantic_event_accepted(
        self, event_id: str, accepted_entries: List[dict],
    ) -> None:
        await self._execute(
            "UPDATE obs_semantic_events SET accepted_entries=? WHERE id=?",
            (json.dumps(accepted_entries, ensure_ascii=False), event_id),
        )

    # ── obs_pipeline_runs ──────────────────────────────────────────────────

    async def insert_obs_pipeline_run(
        self,
        *,
        parent_session_id: Optional[str] = None,
        semantic_event_id: Optional[str] = None,
        prefilter_pass: Optional[bool] = None,
        prefilter_reason: Optional[str] = None,
        triage_status: Optional[str] = None,
        triage_reason: Optional[str] = None,
        llm_tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> str:
        rid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO obs_pipeline_runs
               (id, started_at, finished_at, parent_session_id, semantic_event_id,
                prefilter_pass, prefilter_reason, triage_status, triage_reason,
                llm_tokens, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rid, _now(), _now(), parent_session_id, semantic_event_id,
             None if prefilter_pass is None else (1 if prefilter_pass else 0),
             prefilter_reason, triage_status, triage_reason,
             llm_tokens, duration_ms),
        )
        return rid

    async def prune_obs_pipeline_runs(self, older_than_ts: int) -> int:
        """Delete triage audit-ledger rows older than cutoff. started_at is seconds."""
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM obs_pipeline_runs WHERE started_at < ?",
                (older_than_ts,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    async def prune_obs_sessions(self, older_than_ms: int) -> int:
        """Delete FULLY-PROCESSED sessions whose work ended before cutoff.

        Only ended + extracted sessions are swept (``semantic_status`` in
        done/skipped AND ``ended_at`` NOT NULL AND ``ended_at`` < cutoff). A
        session still pending extraction — or still open — is left alone so a
        slow pipeline never loses unprocessed work. ``ended_at`` is unix
        MILLISECONDS (the aggregator stamps it from a snapshot's captured_at).
        The durable distillation already lives in mem_entries; the snapshots
        this row points at are pruned at LTM_OBS_SNAPSHOT_TTL_DAYS, so a kept
        session is just stale metadata.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM obs_sessions "
                "WHERE semantic_status IN ('done','skipped') "
                "AND ended_at IS NOT NULL AND ended_at < ?",
                (older_than_ms,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    async def prune_obs_semantic_events(self, older_than_ts: int) -> int:
        """Delete TRIAGED semantic events extracted before cutoff.

        Only events triage has already consumed are swept (``accepted_entries``
        IS NOT NULL — triage writes the accepted-entry audit list, even an
        empty ``[]``, once it processes an event). Events still pending triage
        (``accepted_entries`` IS NULL) are left for the queue. ``extracted_at``
        is unix SECONDS. Whatever the event produced already lives in
        mem_entries; this row is post-triage audit.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM obs_semantic_events "
                "WHERE accepted_entries IS NOT NULL AND extracted_at < ?",
                (older_than_ts,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    async def archive_stale_skill_proposals(self, older_than_ts: int) -> List[str]:
        """Archive un-acted skill proposals created before cutoff.

        Returns the archived entry ids so the caller can clean up their on-disk
        staging dirs. Targets only ``kind='skill_proposal'`` rows that are
        still archived=0 AND ``skill_status='proposed'`` (un-acted) — approved
        / rejected proposals have already left the active set. Marks
        ``archived=1, archived_reason='auto_expired'`` rather than deleting:
        the audit row survives, the partial-UNIQUE dedup slot frees (so a later
        recurrence can re-propose), and the IPC list (archived=0 filter) drops
        it. ``created_at`` is unix SECONDS.
        """
        def _txn() -> List[str]:
            rows = self._raw.execute(
                "SELECT id FROM mem_entries "
                "WHERE kind='skill_proposal' AND archived=0 "
                "AND skill_status='proposed' AND created_at < ?",
                (older_than_ts,),
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._raw.execute(
                    "UPDATE mem_entries "
                    "SET archived=1, archived_reason='auto_expired', updated_at=? "
                    "WHERE kind='skill_proposal' AND archived=0 "
                    "AND skill_status='proposed' AND created_at < ?",
                    (_now(), older_than_ts),
                )
            return ids

        async with self._write_lock:
            ids = await asyncio.to_thread(_txn)
            await asyncio.to_thread(self._raw.commit)
            return ids

    async def bump_skill_recurrence(
        self, fingerprint: str, *, category: Optional[str], title: str,
    ) -> int:
        """Find-or-create a skill_recurrence row by fingerprint; increment its
        occurrence_count. Returns the NEW count (1 on first sight).

        Mirrors ``upsert_principal``'s find-or-create-increment, but uses an
        UPSERT … RETURNING so the new count comes back in one round trip. The
        count gates skill-proposal promotion: a task pattern must recur
        ``SKILL_RECURRENCE_THRESHOLD`` times before ``_apply_session_skill``
        writes a proposal — one-off tasks stay at count 1/2 forever and never
        surface a skill.
        """
        now = _now()

        def _txn() -> int:
            try:
                cur = self._raw.execute(
                    """INSERT INTO skill_recurrence
                           (fingerprint, occurrence_count, category, last_title,
                            first_seen, last_seen)
                       VALUES (?, 1, ?, ?, ?, ?)
                       ON CONFLICT(fingerprint) DO UPDATE SET
                           occurrence_count = occurrence_count + 1,
                           category   = excluded.category,
                           last_title = excluded.last_title,
                           last_seen  = excluded.last_seen
                       RETURNING occurrence_count""",
                    (fingerprint, category, title[:120], now, now),
                )
                row = cur.fetchone()
                cur.close()
                self._raw.commit()
                return int(row[0]) if row else 1
            except Exception:
                self._raw.rollback()
                raise

        async with self._write_lock:
            return await asyncio.to_thread(_txn)


    # ── obs_summaries ──────────────────────────────────────────────────────

    async def upsert_obs_summary(
        self,
        *,
        date: str,
        type_: str,
        language: str = "en",
        moments: Optional[List[dict]] = None,
        summary_text: str = "",
        generated_model: str = "",
    ) -> None:
        await self._execute(
            """INSERT INTO obs_summaries (date, type, language, moments_json,
                  summary_text, generated_model, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, type, language) DO UPDATE SET
                  moments_json=excluded.moments_json,
                  summary_text=excluded.summary_text,
                  generated_model=excluded.generated_model,
                  generated_at=excluded.generated_at""",
            (date, type_, language,
             json.dumps(moments or [], ensure_ascii=False),
             summary_text, generated_model, _now()),
        )

    async def get_obs_summary(
        self, *, date: str, type_: str, language: str = "en",
    ) -> Optional[tuple]:
        return await self._fetchone(
            "SELECT date, type, language, moments_json, summary_text, "
            "       generated_model, generated_at FROM obs_summaries "
            "WHERE date=? AND type=? AND language=?",
            (date, type_, language),
        )

    # ── ent_principals / ent_aliases / ent_sightings ───────────────────────

    async def upsert_principal(
        self,
        *,
        kind: str,
        canonical_name: str,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
        host_kind: Optional[str] = None,
        os: Optional[str] = None,
        project_root: Optional[str] = None,
        description: Optional[str] = None,
    ) -> str:
        """Find-or-create a principal by (kind, canonical_name). Returns id."""
        existing = await self._fetchone(
            "SELECT id FROM ent_principals WHERE kind=? AND canonical_name=?",
            (kind, canonical_name),
        )
        now = _now()
        if existing:
            pid = existing[0]
            await self._execute(
                "UPDATE ent_principals SET last_seen=?, sighting_count=sighting_count+1 "
                "WHERE id=?",
                (now, pid),
            )
            return pid
        pid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO ent_principals
               (id, kind, canonical_name, display_name, email, host_kind, os,
                project_root, description, first_seen, last_seen, sighting_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (pid, kind, canonical_name, display_name, email, host_kind, os,
             project_root, description, now, now),
        )
        return pid

    async def add_principal_alias(self, principal_id: str, alias: str) -> None:
        try:
            await self._execute(
                "INSERT INTO ent_aliases (principal_id, alias) VALUES (?, ?)",
                (principal_id, alias),
            )
        except sqlite3.IntegrityError:
            pass

    async def insert_sighting(
        self,
        *,
        principal_id: str,
        source_kind: str,
        source_id: str,
        context: Optional[dict] = None,
    ) -> str:
        sid = str(uuid.uuid4())
        await self._execute(
            """INSERT INTO ent_sightings
               (id, principal_id, source_kind, source_id, sighted_at, context)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, principal_id, source_kind, source_id, _now(),
             json.dumps(context, ensure_ascii=False) if context else None),
        )
        return sid

    async def list_principals(
        self, *, kind: Optional[str] = None, archived: bool = False,
        limit: int = 200,
    ) -> List[tuple]:
        sql = (
            "SELECT id, kind, canonical_name, display_name, email, host_kind, os, "
            "       project_root, first_seen, last_seen, sighting_count "
            "FROM ent_principals WHERE archived=?"
        )
        params: list = [1 if archived else 0]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        return await self._fetchall(sql, tuple(params))

    # ── Dataclass getters (typed view over the same rows) ──────────────────
    #
    # The bulk SELECT/INSERT methods above operate on tuples for SQL speed
    # and minimal allocation. These thin adapters materialize one row into
    # the dataclass surface from models.py for callers that want type
    # safety (admin tooling, IPC handlers, the future migration scripts).
    # If you're iterating thousands of rows, prefer the tuple variants.

    async def get_obs_session(self, session_id: str) -> Optional[Session]:
        row = await self._fetchone(
            "SELECT id, session_key, trigger_kind, started_at, ended_at, "
            "frame_os, frame_host, primary_process, primary_window_title, "
            "semantic_status, snapshot_count, apps_seen, "
            "principal_ids FROM obs_sessions WHERE id=?",
            (session_id,),
        )
        if not row:
            return None
        try:
            tk = TriggerKind(row[2])
        except ValueError:
            tk = TriggerKind.APP_SWITCH
        try:
            ss = SemanticStatus(row[9])
        except ValueError:
            ss = SemanticStatus.PENDING
        apps = []
        if row[11]:
            try:
                v = json.loads(row[11])
                if isinstance(v, list):
                    apps = v
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        principals = []
        if row[12]:
            try:
                v = json.loads(row[12])
                if isinstance(v, list):
                    principals = v
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return Session(
            id=row[0], session_key=row[1], trigger_kind=tk,
            started_at=int(row[3]),
            ended_at=int(row[4]) if row[4] is not None else None,
            frame_os=row[5], frame_host=row[6],
            primary_process=row[7], primary_window_title=row[8],
            semantic_status=ss,
            snapshot_count=int(row[10] or 0),
            apps_seen=apps, principal_ids=principals,
        )

    async def get_obs_semantic_event(self, event_id: str) -> Optional[SemanticEvent]:
        row = await self._fetchone(
            "SELECT id, session_id, synthetic_origin, extracted_at, title, "
            "description, category, entities, apps, time_range_start, "
            "time_range_end, task_worthy, worth_memory, worth_knowledge, "
            "worth_skill, frame_os, frame_host, frame_confidence, accepted_entries "
            "FROM obs_semantic_events WHERE id=?",
            (event_id,),
        )
        if not row:
            return None
        from .models import SyntheticOrigin
        synth: Optional[SyntheticOrigin] = None
        if row[2]:
            try:
                synth = SyntheticOrigin(row[2])
            except ValueError:
                synth = None
        def _json_list(raw):
            if not raw:
                return []
            try:
                v = json.loads(raw)
                return v if isinstance(v, list) else []
            except (json.JSONDecodeError, TypeError, ValueError):
                return []
        return SemanticEvent(
            id=row[0], session_id=row[1], synthetic_origin=synth,
            extracted_at=int(row[3]), title=row[4],
            description=row[5] or "", category=row[6],
            entities=_json_list(row[7]), apps=_json_list(row[8]),
            time_range_start=int(row[9]), time_range_end=int(row[10]),
            task_worthy=bool(row[11]), worth_memory=bool(row[12]),
            worth_knowledge=bool(row[13]), worth_skill=bool(row[14]),
            frame_os=row[15], frame_host=row[16],
            frame_confidence=float(row[17]) if row[17] is not None else None,
            accepted_entries=_json_list(row[18]),
        )

    async def get_principal(self, principal_id: str) -> Optional[Principal]:
        row = await self._fetchone(
            "SELECT id, kind, canonical_name, display_name, email, host_kind, "
            "os, project_root, description, first_seen, last_seen, "
            "sighting_count, archived FROM ent_principals WHERE id=?",
            (principal_id,),
        )
        if not row:
            return None
        try:
            pk = PrincipalKind(row[1])
        except ValueError:
            pk = PrincipalKind.MACHINE
        from .models import HostKind
        hk: Optional[HostKind] = None
        if row[5]:
            try:
                hk = HostKind(row[5])
            except ValueError:
                hk = None
        return Principal(
            id=row[0], kind=pk, canonical_name=row[2],
            display_name=row[3], email=row[4],
            host_kind=hk, os=row[6], project_root=row[7],
            description=row[8],
            first_seen=int(row[9]), last_seen=int(row[10]),
            sighting_count=int(row[11] or 0),
            archived=bool(row[12]),
        )

    async def get_skill_proposal(self, proposal_id: str) -> Optional[SkillProposal]:
        row = await self._fetchone(
            "SELECT id, skill_status, skill_signature, skill_fingerprint, "
            "recurrence_count, summary, source_event_id, created_at, updated_at "
            "FROM mem_entries "
            "WHERE id=? AND kind='skill_proposal'",
            (proposal_id,),
        )
        if not row:
            return None
        try:
            sps = SkillProposalStatus(row[1]) if row[1] else SkillProposalStatus.PROPOSED
        except ValueError:
            sps = SkillProposalStatus.PROPOSED
        signature: dict = {}
        if row[2]:
            try:
                v = json.loads(row[2])
                signature = v if isinstance(v, dict) else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                signature = {}
        # Pull the body content from mem_chunks
        chunk_rows = await self._fetchall(
            "SELECT text FROM mem_chunks WHERE entry_id=? ORDER BY chunk_index",
            (proposal_id,),
        )
        body_md = "\n\n".join(r[0] for r in chunk_rows)
        from pathlib import Path as _Path
        staging = _Path.home() / "HandQ" / "Skill" / ".proposed" / proposal_id[:8]
        return SkillProposal(
            id=row[0], skill_status=sps,
            skill_signature=signature,
            skill_fingerprint=row[3] or "",
            summary=row[5] or "",
            body_md=body_md,
            recurrence_count=int(row[4] or 1),
            staging_path=str(staging) if staging.exists() else None,
            source_event_id=row[6],
            created_at=int(row[7]), updated_at=int(row[8]),
        )

    async def get_obs_summary_dataclass(
        self, *, date: str, type_: str, language: str = "en",
    ) -> Optional[Summary]:
        row = await self.get_obs_summary(date=date, type_=type_, language=language)
        if not row:
            return None
        moments: list = []
        if row[3]:
            try:
                v = json.loads(row[3])
                moments = v if isinstance(v, list) else []
            except (json.JSONDecodeError, TypeError, ValueError):
                moments = []
        return Summary(
            date=row[0], type=row[1], language=row[2],
            moments=moments,
            summary_text=row[4] or "",
            generated_model=row[5] or "",
            generated_at=int(row[6] or 0),
        )

    # ── L2/L3 dream synthesis: real impls (LTM 2.0) ────────────────────────
    #
    # The original triage.py L2/L3 path called these methods; before LTM 2.0
    # they targeted memory_files / knowledge_files. We rebuilt them on
    # mem_entries unified table — kind discriminator separates memory vs
    # knowledge, synthesis_level distinguishes L0/L2/L3 levels.

    async def list_in_flight_dream_run_sources(self) -> List[str]:
        """Entry ids that an actively-running L2/L3 synthesis is touching.

        Returns the union of source_entry_ids across all non-archived
        synthesis-level entries (source='dream_l*'). RetriageWorker skips
        these so it doesn't archive a source mid-synthesis.
        """
        rows = await self._fetchall(
            "SELECT source_entry_ids FROM mem_entries "
            "WHERE source LIKE 'dream_l%' AND archived=0 AND source_entry_ids IS NOT NULL",
        )
        out: List[str] = []
        for (raw,) in rows:
            if not raw:
                continue
            try:
                ids = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(ids, list):
                out.extend(str(x) for x in ids if x)
        return out

    async def count_entries_since(
        self, *, kind: str, since_ts: int, synthesis_level: int = 0,
    ) -> int:
        """Count non-archived entries created/updated after *since_ts* at
        the given synthesis_level. Used by the idle-aware synthesis gate
        to decide if enough new material has accumulated.
        """
        row = await self._fetchone(
            "SELECT COUNT(*) FROM mem_entries "
            "WHERE kind=? AND archived=0 AND synthesis_level=? AND updated_at >= ?",
            (kind, synthesis_level, since_ts),
        )
        return int(row[0]) if row else 0

    async def list_entries_by_synthesis_level(
        self,
        *,
        kind: str,
        synthesis_level: int,
        provider: str,
        model: str,
        since_seconds: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Tuple]:
        """Return (entry_id, dim_or_cat, summary, content_text, embedding_bytes,
        frame_os) for non-archived entries at a given synthesis_level whose
        chunk-0 embedding is cached. IDENTITY entries are excluded so they
        never participate in L2/L3 (they're injected unconditionally).

        The trailing ``frame_os`` element is new vs the v4 row shape — the
        L2 clustering code uses it to partition INSIGHT entries by frame.
        """
        facet_col = "dimension" if kind == EntryKind.MEMORY.value else "category"
        sql = (
            f"SELECT e.id, e.{facet_col}, e.summary, c.text, ec.embedding, e.frame_os "
            f"FROM mem_entries e "
            f"JOIN mem_chunks c ON c.entry_id = e.id AND c.chunk_index = 0 "
            f"JOIN mem_embedding_cache ec ON ec.chunk_id = c.id "
            f"  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            f"WHERE e.kind=? AND e.archived=0 AND e.synthesis_level=?"
        )
        params: list = [kind, provider, model, kind, synthesis_level]
        if kind == EntryKind.MEMORY.value:
            sql += f" AND e.{facet_col} != ?"
            params.append(MemoryDimension.IDENTITY.value)
        if since_seconds is not None:
            sql += " AND e.updated_at >= ?"
            params.append(_now() - since_seconds)
        sql += " ORDER BY e.updated_at DESC LIMIT ?"
        params.append(limit)
        return await self._fetchall(sql, tuple(params))

    async def insert_synthesis_entry(
        self,
        *,
        kind: str,
        target_facet: str,
        summary: str,
        content: str,
        synthesis_level: int,
        source_entry_ids: List[str],
        source_run_id: Optional[str] = None,
        frame: Optional[dict] = None,
    ) -> str:
        """Insert a synthesised mem_entry. Mirrors insert_entry but with
        synthesis_level + source_entry_ids + source='dream_l<N>'."""
        kind_enum = EntryKind(kind)
        dimension = None
        category = None
        if kind_enum == EntryKind.MEMORY:
            try:
                dimension = MemoryDimension(target_facet)
            except ValueError:
                dimension = None
        elif kind_enum == EntryKind.KNOWLEDGE:
            try:
                category = KnowledgeCategory(target_facet)
            except ValueError:
                category = None
        return await self.insert_entry(
            kind=kind_enum,
            dimension=dimension,
            category=category,
            summary=summary,
            content=content,
            source=f"dream_l{int(synthesis_level)}",
            source_ref=source_run_id,
            frame=frame,
            synthesis_level=synthesis_level,
            source_entry_ids=source_entry_ids,
        )

    async def list_entry_centroids(
        self, *, kind: str, provider: str, model: str,
    ) -> List[Tuple]:
        """One chunk-0 row per non-archived entry with its embedding.

        Returned shape: (entry_id, summary, created_at, updated_at,
        embedding_bytes, frame_os).
        """
        sql = (
            "SELECT e.id, e.summary, e.created_at, e.updated_at, ec.embedding, e.frame_os "
            "FROM mem_entries e "
            "JOIN mem_chunks c ON c.entry_id = e.id AND c.chunk_index = 0 "
            "JOIN mem_embedding_cache ec ON ec.chunk_id = c.id "
            "  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            "WHERE e.kind=? AND e.archived=0"
        )
        params: list = [kind, provider, model, kind]
        if kind == EntryKind.MEMORY.value:
            sql += " AND e.dimension != ?"
            params.append(MemoryDimension.IDENTITY.value)
        return await self._fetchall(sql, tuple(params))

    async def insert_merge_proposal(
        self,
        *,
        kind: str,
        entry_a_id: str,
        entry_b_id: str,
        similarity: float,
    ) -> str:
        """Record a merge candidate via mem_correction_proposals.

        v4 had a dedicated merge_proposals table; LTM 2.0 consolidates the
        review surface into mem_correction_proposals (kind='merge'). The
        canonical pair order (sorted ids) is stored in the payload (and the
        first id in target_entry_id) purely for stable reads — there is NO
        UNIQUE constraint on the pair, so this is a plain INSERT that can
        write the same pair more than once. Re-scan duplication is avoided at
        a higher level instead: ``list_decided_merge_pairs`` feeds the merge
        scanner a set of already-judged pairs (status 'merged'/'kept_distinct')
        so a settled pair is never re-sent to the arbiter, and the periodic
        ``prune_correction_proposals`` sweep clears any stale 'pending' rows.
        """
        a, b = sorted([entry_a_id, entry_b_id])
        pid = str(uuid.uuid4())
        payload = {"entry_a_id": a, "entry_b_id": b, "similarity": similarity}
        await self._execute(
            "INSERT INTO mem_correction_proposals "
            "(id, kind, target_entry_id, target_version, target_archived, "
            " payload, confidence, rule_version, parent_run_id, rationale, "
            " rationale_pii_scrubbed, status, created_at) "
            "VALUES (?, 'merge', ?, 0, 0, ?, ?, 0, NULL, "
            " 'merge scanner pair candidate', 0, 'pending', ?)",
            (pid, a, json.dumps(payload, ensure_ascii=False),
             float(similarity), _now()),
        )
        return pid

    async def list_merge_proposals(
        self, *, status: str = "pending", limit: int = 100,
    ) -> List[Tuple]:
        rows = await self._fetchall(
            "SELECT id, payload, confidence, created_at FROM mem_correction_proposals "
            "WHERE kind='merge' AND status=? ORDER BY confidence DESC LIMIT ?",
            (status, limit),
        )
        out: List[Tuple] = []
        for r in rows:
            try:
                p = json.loads(r[1]) if r[1] else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                p = {}
            out.append((
                r[0], "memory" if p.get("kind") == "memory" else "memory",
                p.get("entry_a_id", ""), p.get("entry_b_id", ""),
                float(r[2] or 0.0), int(r[3]),
            ))
        return out

    async def resolve_merge_proposal(
        self, *, proposal_id: str, status: str,
    ) -> None:
        await self._execute(
            "UPDATE mem_correction_proposals SET status=?, resolved_at=? "
            "WHERE id=? AND kind='merge'",
            (status, _now(), proposal_id),
        )

    async def list_decided_merge_pairs(self) -> set:
        """Return ``{frozenset({a_id, b_id}), ...}`` for merge pairs already
        given a terminal verdict (``merged`` or ``kept_distinct``).

        The merge scanner uses this to skip pairs the LLM arbiter (or
        auto-merge) has already judged, so a borderline pair is never
        re-sent to the helper LLM on every 15-minute scan.
        """
        rows = await self._fetchall(
            "SELECT payload FROM mem_correction_proposals "
            "WHERE kind='merge' AND status IN ('merged', 'kept_distinct')",
            (),
        )
        out: set = set()
        for r in rows:
            try:
                p = json.loads(r[0]) if r[0] else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            a = p.get("entry_a_id")
            b = p.get("entry_b_id")
            if a and b:
                out.add(frozenset((a, b)))
        return out

    async def insert_dream_run(self, *, level: int, kind: str) -> str:
        rid = str(uuid.uuid4())
        await self._execute(
            "INSERT INTO mem_dream_runs (id, level, kind, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (rid, int(level), kind, _now()),
        )
        return rid

    async def update_dream_run(
        self,
        run_id: str,
        *,
        status: str,
        source_count: Optional[int] = None,
        cluster_count: Optional[int] = None,
        accepted_count: Optional[int] = None,
        skipped_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        await self._execute(
            "UPDATE mem_dream_runs "
            "SET status=?, ended_at=?, source_count=?, cluster_count=?, "
            "    accepted_count=?, skipped_count=?, error=? WHERE id=?",
            (status, _now(), source_count, cluster_count,
             accepted_count, skipped_count, error, run_id),
        )

    async def get_last_dream_run(
        self, *, level: int, kind: str,
    ) -> Optional[Tuple]:
        return await self._fetchone(
            "SELECT id, started_at, ended_at, status FROM mem_dream_runs "
            "WHERE level=? AND kind=? ORDER BY started_at DESC LIMIT 1",
            (int(level), kind),
        )

    async def apply_archive_correction(
        self, pid: str, *, resolved_by: str,
    ) -> bool:
        """Apply an archive correction proposal with staleness check."""
        prop = await self.get_correction_proposal(pid)
        if prop is None or prop.kind != CorrectionKind.ARCHIVE:
            return False
        if prop.status != CorrectionStatus.PENDING:
            return False
        reason = f"correction_v{prop.rule_version}_{prop.kind.value}"
        superseded_by_id = (
            (prop.payload or {}).get("superseded_by_id") if prop.payload else None
        )
        async with self._write_lock:
            try:
                cur = await asyncio.to_thread(
                    self._raw.execute,
                    "SELECT version, archived FROM mem_entries WHERE id=?",
                    (prop.target_entry_id,),
                )
                row = await asyncio.to_thread(cur.fetchone)
                await asyncio.to_thread(cur.close)
                if not row:
                    await asyncio.to_thread(
                        self._raw.execute,
                        "UPDATE mem_correction_proposals SET status='stale', "
                        "resolved_at=?, resolved_by=? WHERE id=?",
                        (_now(), resolved_by[:80], pid),
                    )
                    await asyncio.to_thread(self._raw.commit)
                    return False
                cur_version, cur_archived = int(row[0]), bool(row[1])
                if cur_version != prop.target_version or cur_archived != prop.target_archived:
                    await asyncio.to_thread(
                        self._raw.execute,
                        "UPDATE mem_correction_proposals SET status='stale', "
                        "resolved_at=?, resolved_by=? WHERE id=?",
                        (_now(), resolved_by[:80], pid),
                    )
                    await asyncio.to_thread(self._raw.commit)
                    return False
                sets = ["archived=1", "archived_reason=?", "updated_at=?"]
                params = [reason, _now()]
                if superseded_by_id:
                    sets.append("superseded_by=?")
                    params.append(superseded_by_id)
                params.append(prop.target_entry_id)
                await asyncio.to_thread(
                    self._raw.execute,
                    f"UPDATE mem_entries SET {', '.join(sets)} WHERE id=?",
                    tuple(params),
                )
                await asyncio.to_thread(
                    self._raw.execute,
                    "UPDATE mem_correction_proposals SET status='applied', "
                    "resolved_at=?, resolved_by=? WHERE id=?",
                    (_now(), resolved_by[:80], pid),
                )
                await asyncio.to_thread(self._raw.commit)
                return True
            except Exception:
                await asyncio.to_thread(self._raw.rollback)
                raise

    async def get_correction_proposal(
        self, pid: str,
    ) -> Optional[CorrectionProposal]:
        row = await self._fetchone(
            "SELECT id, kind, target_entry_id, target_version, "
            "       target_archived, payload, confidence, rule_version, parent_run_id, "
            "       rationale, rationale_pii_scrubbed, status, created_at, "
            "       resolved_at, resolved_by "
            "FROM mem_correction_proposals WHERE id=?",
            (pid,),
        )
        if not row:
            return None
        try:
            kind = CorrectionKind(row[1])
        except ValueError:
            kind = CorrectionKind.ARCHIVE
        try:
            status = CorrectionStatus(row[11])
        except ValueError:
            status = CorrectionStatus.PENDING
        payload: Optional[dict] = None
        if row[5]:
            try:
                payload = json.loads(row[5])
                if not isinstance(payload, dict):
                    payload = None
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = None
        return CorrectionProposal(
            id=row[0], kind=kind,
            target_kind=EntryKind.MEMORY,  # mem_entries unified — keep MEMORY as default
            target_entry_id=row[2],
            target_version=int(row[3]),
            target_archived=bool(row[4]),
            payload=payload,
            confidence=float(row[6]) if row[6] is not None else None,
            rule_version=int(row[7]),
            parent_run_id=row[8],
            rationale=row[9] or "",
            rationale_pii_scrubbed=bool(row[10]),
            status=status,
            created_at=int(row[12]),
            resolved_at=int(row[13]) if row[13] is not None else None,
            resolved_by=row[14],
        )


# ── Row mapping helpers ────────────────────────────────────────────────────

def _frame_dict(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _row_to_memory_entry(r: Tuple) -> Entry:
    """Map (id, dimension, summary, source, source_ref, version, archived,
    archived_reason, created_at, updated_at, frame_json) → Entry."""
    try:
        dim = MemoryDimension(r[1]) if r[1] else None
    except ValueError:
        dim = None
    return Entry(
        id=r[0], kind=EntryKind.MEMORY, dimension=dim, summary=r[2],
        source=r[3] or "", source_ref=r[4], version=int(r[5]),
        archived=bool(r[6]), archived_reason=r[7],
        created_at=int(r[8]), updated_at=int(r[9]),
        frame_json=_frame_dict(r[10]) if len(r) > 10 else None,
    )


def _row_to_knowledge_entry(r: Tuple) -> Entry:
    try:
        cat = KnowledgeCategory(r[1]) if r[1] else None
    except ValueError:
        cat = None
    return Entry(
        id=r[0], kind=EntryKind.KNOWLEDGE, category=cat, summary=r[2],
        source=r[3] or "", source_ref=r[4], version=int(r[5]),
        archived=bool(r[6]), archived_reason=r[7],
        created_at=int(r[8]), updated_at=int(r[9]),
        frame_json=_frame_dict(r[10]) if len(r) > 10 else None,
    )


def _row_to_entry_full(r: Tuple, kind: EntryKind) -> Entry:
    """Map (id, dimension, category, summary, source, source_ref, version,
    archived, archived_reason, created_at, updated_at, frame_json) → Entry."""
    dim = None
    cat = None
    if kind == EntryKind.MEMORY and r[1]:
        try:
            dim = MemoryDimension(r[1])
        except ValueError:
            pass
    elif kind == EntryKind.KNOWLEDGE and r[2]:
        try:
            cat = KnowledgeCategory(r[2])
        except ValueError:
            pass
    return Entry(
        id=r[0], kind=kind, dimension=dim, category=cat, summary=r[3],
        source=r[4] or "", source_ref=r[5], version=int(r[6]),
        archived=bool(r[7]), archived_reason=r[8],
        created_at=int(r[9]), updated_at=int(r[10]),
        frame_json=_frame_dict(r[11]) if len(r) > 11 else None,
    )
