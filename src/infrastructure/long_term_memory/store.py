"""SQLite-backed store for the long-term memory subsystem.

Implementation notes
--------------------
* Pure stdlib: ``sqlite3`` + ``asyncio.to_thread`` — no aiosqlite dependency.
* WAL mode + single ``asyncio.Lock`` for writes; reads run lock-free under WAL.
* All public methods are ``async`` and dispatch the synchronous SQLite work
  to a thread pool; this keeps the asyncio event loop responsive while the
  bridge serves IPC.
* Schema migrations are defined in :mod:`schema`; ``open()`` runs them in
  order, recording each in ``migration_log`` + ``memory_meta.schema_version``.
* The same candidate row drives both memory and knowledge triage — see
  ``02_handq_design.md §3.3`` for why.
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

from .models import (
    ArchiveReason,
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
)
from .schema import DDL_BOOTSTRAP, MIGRATIONS
from . import _constants as C

_logger = logging.getLogger("handq.ltm.store")


class _SyncConn:
    """Thin shim that exposes the subset of the connection API a migration
    needs (``execute``, ``commit``) as ``async`` calls.

    Migrations are written ``async def m(conn): await conn.execute(...)``
    so they look like the rest of the codebase even though the underlying
    sqlite3 connection is synchronous.
    """

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await asyncio.to_thread(self._raw.execute, sql, params)

    async def commit(self) -> None:
        await asyncio.to_thread(self._raw.commit)


def _now() -> int:
    return int(time.time())


class SQLiteStore:
    """Async-friendly wrapper over a single sqlite3 connection."""

    def __init__(self, raw: sqlite3.Connection, write_lock: asyncio.Lock) -> None:
        self._raw = raw
        self._write_lock = write_lock

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    async def open(cls, db_path: Path) -> "SQLiteStore":
        db_path.parent.mkdir(parents=True, exist_ok=True)

        def _connect() -> sqlite3.Connection:
            # check_same_thread=False so asyncio.to_thread workers can use the
            # connection. We serialize all writes through self._write_lock,
            # so concurrent access is safe.
            conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA temp_store=MEMORY")
            for stmt in DDL_BOOTSTRAP:
                conn.execute(stmt)
            conn.commit()
            return conn

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

    # ── Tiny helpers ────────────────────────────────────────────────────────

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

    # ── Meta ────────────────────────────────────────────────────────────────

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

    # ── Candidates ──────────────────────────────────────────────────────────

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
            """INSERT INTO memory_candidates
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
               FROM memory_candidates
               WHERE status='pending'
               ORDER BY created_at ASC
               LIMIT ?""",
            (limit,),
        )
        out: List[Candidate] = []
        for r in rows:
            try:
                meta = json.loads(r[5]) if r[5] else {}
            except json.JSONDecodeError:
                meta = {}
            out.append(Candidate(
                id=r[0],
                source=r[1],
                source_ref=r[2],
                raw_text=r[3],
                hint=r[4],
                metadata=meta,
                status=CandidateStatus.PENDING,
                retry_count=int(r[6]),
                created_at=int(r[7]),
            ))
        return out

    async def set_candidate_status(
        self, cid: str, status, *, reason: Optional[str] = None,
    ) -> None:
        s = status.value if hasattr(status, "value") else str(status)
        await self._execute(
            "UPDATE memory_candidates SET status=?, reason=?, updated_at=? WHERE id=?",
            (s, reason, _now(), cid),
        )

    async def bump_candidate_retry(self, cid: str, *, error: str) -> int:
        """Increment retry_count, set last_error, return the new count."""
        async with self._write_lock:
            await asyncio.to_thread(
                self._raw.execute,
                """UPDATE memory_candidates
                   SET retry_count=retry_count+1, last_error=?, status='pending', updated_at=?
                   WHERE id=?""",
                (error[:200], _now(), cid),
            )
            await asyncio.to_thread(self._raw.commit)
            cur = await asyncio.to_thread(
                self._raw.execute,
                "SELECT retry_count FROM memory_candidates WHERE id=?",
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
                """UPDATE memory_candidates
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
            "FROM memory_candidates WHERE 1=1"
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
                id=r[0],
                source=r[1],
                source_ref=r[2],
                raw_text=r[3],
                hint=r[4],
                metadata=meta,
                status=cstatus,
                retry_count=int(r[6]),
                created_at=int(r[7]),
            ))
        return out

    # ── Memory entries ──────────────────────────────────────────────────────

    async def insert_memory_entry(
        self,
        *,
        dimension: MemoryDimension,
        summary: str,
        content: str,
        candidate_id: Optional[str],
        source: str,
        source_ref: Optional[str],
    ) -> str:
        return await self.insert_memory_entry_with_id(
            entry_id=str(uuid.uuid4()),
            dimension=dimension,
            summary=summary,
            content=content,
            candidate_id=candidate_id,
            source=source,
            source_ref=source_ref,
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
    ) -> str:
        """Same as ``insert_memory_entry`` but the caller supplies the
        entry id. Used by the verbatim /remember path which needs the
        id reserved BEFORE inserting (so the .md mirror file's name
        and the DB row's id agree).
        """
        from .chunking import chunk_markdown
        eid = entry_id
        now = _now()
        chunks = chunk_markdown(content)

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")
                self._raw.execute(
                    """INSERT INTO memory_files
                       (id, dimension, summary, candidate_id, source, source_ref,
                        archived, version, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                    (eid, dimension.value, summary[:120], candidate_id,
                     source, source_ref, now, now),
                )
                for i, c in enumerate(chunks):
                    self._raw.execute(
                        """INSERT INTO memory_chunks
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

    async def update_memory_entry(
        self,
        entry_id: str,
        *,
        new_summary: str,
        new_content: str,
    ) -> None:
        await self._update_entry_versioned(
            files_table="memory_files",
            chunks_table="memory_chunks",
            versions_table="memory_versions",
            kind=EntryKind.MEMORY.value,
            entry_id=entry_id,
            new_summary=new_summary,
            new_content=new_content,
        )

    async def archive_memory_entry(self, entry_id: str, *, reason: str) -> None:
        await self._execute(
            "UPDATE memory_files SET archived=1, archived_reason=?, updated_at=? WHERE id=?",
            (reason, _now(), entry_id),
        )

    async def set_superseded_by(
        self, *, kind: str, entry_id: str, superseded_by_id: str,
    ) -> None:
        """Stamp the FK that links an archived entry to whatever replaced it.

        Used by L2/L3 dream synthesis to mark each source entry with the
        synthesis entry that subsumed it. ``entry_id`` is the entry being
        retired; ``superseded_by_id`` is the new synthesis entry. The
        column already exists in the schema (memory_files / knowledge_files).
        """
        if kind == EntryKind.MEMORY.value:
            table = "memory_files"
        elif kind == EntryKind.KNOWLEDGE.value:
            table = "knowledge_files"
        else:
            raise ValueError(f"unknown kind: {kind!r}")
        await self._execute(
            f"UPDATE {table} SET superseded_by=?, updated_at=? WHERE id=?",
            (superseded_by_id, _now(), entry_id),
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
            "archived, archived_reason, created_at, updated_at "
            "FROM memory_files WHERE archived=?"
        )
        params: list = [1 if archived else 0]
        if dimension is not None:
            sql += " AND dimension=?"
            params.append(dimension.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, tuple(params))
        return [_row_to_memory_entry(r) for r in rows]

    async def get_memory_entry_full(self, entry_id: str) -> Optional[Entry]:
        row = await self._fetchone(
            "SELECT id, dimension, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at "
            "FROM memory_files WHERE id=?",
            (entry_id,),
        )
        if not row:
            return None
        entry = _row_to_memory_entry(row)
        chunk_rows = await self._fetchall(
            "SELECT id, chunk_index, text, hash, start_line, end_line "
            "FROM memory_chunks WHERE entry_id=? ORDER BY chunk_index",
            (entry_id,),
        )
        entry.chunks = [
            Chunk(id=cr[0], entry_id=entry_id, chunk_index=int(cr[1]),
                  text=cr[2], hash=cr[3], start_line=cr[4], end_line=cr[5])
            for cr in chunk_rows
        ]
        entry.content = "\n\n".join(c.text for c in entry.chunks)
        return entry

    # ── Knowledge entries (parallel to memory) ──────────────────────────────

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
        from .chunking import chunk_markdown
        eid = str(uuid.uuid4())
        now = _now()
        chunks = chunk_markdown(content)

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")
                self._raw.execute(
                    """INSERT INTO knowledge_files
                       (id, category, summary, candidate_id, source, source_ref,
                        archived, version, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                    (eid, category.value, summary[:120], candidate_id,
                     source, source_ref, now, now),
                )
                for i, c in enumerate(chunks):
                    self._raw.execute(
                        """INSERT INTO knowledge_chunks
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

    async def update_knowledge_entry(
        self,
        entry_id: str,
        *,
        new_summary: str,
        new_content: str,
    ) -> None:
        await self._update_entry_versioned(
            files_table="knowledge_files",
            chunks_table="knowledge_chunks",
            versions_table="knowledge_versions",
            kind=EntryKind.KNOWLEDGE.value,
            entry_id=entry_id,
            new_summary=new_summary,
            new_content=new_content,
        )

    async def archive_knowledge_entry(self, entry_id: str, *, reason: str) -> None:
        await self._execute(
            "UPDATE knowledge_files SET archived=1, archived_reason=?, updated_at=? WHERE id=?",
            (reason, _now(), entry_id),
        )

    async def list_knowledge_entries(
        self,
        *,
        category: Optional[KnowledgeCategory] = None,
        archived: bool = False,
        limit: int = 50,
    ) -> List[Entry]:
        sql = (
            "SELECT id, category, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at "
            "FROM knowledge_files WHERE archived=?"
        )
        params: list = [1 if archived else 0]
        if category is not None:
            sql += " AND category=?"
            params.append(category.value)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._fetchall(sql, tuple(params))
        return [_row_to_knowledge_entry(r) for r in rows]

    async def get_knowledge_entry_full(self, entry_id: str) -> Optional[Entry]:
        row = await self._fetchone(
            "SELECT id, category, summary, source, source_ref, version, "
            "archived, archived_reason, created_at, updated_at "
            "FROM knowledge_files WHERE id=?",
            (entry_id,),
        )
        if not row:
            return None
        entry = _row_to_knowledge_entry(row)
        chunk_rows = await self._fetchall(
            "SELECT id, chunk_index, text, hash, start_line, end_line "
            "FROM knowledge_chunks WHERE entry_id=? ORDER BY chunk_index",
            (entry_id,),
        )
        entry.chunks = [
            Chunk(id=cr[0], entry_id=entry_id, chunk_index=int(cr[1]),
                  text=cr[2], hash=cr[3], start_line=cr[4], end_line=cr[5])
            for cr in chunk_rows
        ]
        entry.content = "\n\n".join(c.text for c in entry.chunks)
        return entry

    # ── Versioned update helper ─────────────────────────────────────────────

    async def _update_entry_versioned(
        self,
        *,
        files_table: str,
        chunks_table: str,
        versions_table: str,
        kind: str,
        entry_id: str,
        new_summary: str,
        new_content: str,
    ) -> None:
        from .chunking import chunk_markdown
        now = _now()
        new_chunks = chunk_markdown(new_content)

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")

                cur = self._raw.execute(
                    f"SELECT version, summary FROM {files_table} WHERE id=?",
                    (entry_id,),
                )
                row = cur.fetchone()
                cur.close()
                if not row:
                    raise ValueError(f"entry not found in {files_table}: {entry_id}")
                old_version, old_summary = int(row[0]), row[1]

                cur2 = self._raw.execute(
                    f"SELECT chunk_index, text, start_line, end_line "
                    f"FROM {chunks_table} WHERE entry_id=? ORDER BY chunk_index",
                    (entry_id,),
                )
                old_chunks = list(cur2.fetchall())
                cur2.close()
                chunks_json = json.dumps(
                    [{"i": c[0], "t": c[1], "sl": c[2], "el": c[3]} for c in old_chunks],
                    ensure_ascii=False,
                )

                self._raw.execute(
                    f"INSERT INTO {versions_table} "
                    f"(id, entry_id, version, summary, chunks_json, archived_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), entry_id, old_version, old_summary,
                     chunks_json, now),
                )

                # ── Clean up embedding_cache for soon-to-be-deleted chunks.
                # The cache table has no FK to chunks_table (cross-kind
                # discriminator key + intentional decoupling so the cache
                # is independently restartable). Without this DELETE, the
                # rows orphan and pile up indefinitely as users update
                # memories — slow disk leak that was easy to miss because
                # recall still returned correct results.
                cur3 = self._raw.execute(
                    f"SELECT id FROM {chunks_table} WHERE entry_id=?",
                    (entry_id,),
                )
                old_chunk_ids = [r[0] for r in cur3.fetchall()]
                cur3.close()
                if old_chunk_ids:
                    placeholders = ",".join(["?"] * len(old_chunk_ids))
                    self._raw.execute(
                        f"DELETE FROM embedding_cache "
                        f"WHERE chunk_kind=? AND chunk_id IN ({placeholders})",
                        (kind,) + tuple(old_chunk_ids),
                    )

                self._raw.execute(
                    f"DELETE FROM {chunks_table} WHERE entry_id=?", (entry_id,),
                )
                for i, c in enumerate(new_chunks):
                    self._raw.execute(
                        f"INSERT INTO {chunks_table} "
                        f"(id, entry_id, chunk_index, text, start_line, end_line, hash) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), entry_id, i, c.text,
                         c.start_line, c.end_line, c.hash),
                    )

                self._raw.execute(
                    f"UPDATE {files_table} "
                    f"SET summary=?, version=version+1, updated_at=? WHERE id=?",
                    (new_summary[:120], now, entry_id),
                )
                self._raw.commit()
            except Exception:
                self._raw.rollback()
                raise

        async with self._write_lock:
            await asyncio.to_thread(_txn)

    # ── FTS search ──────────────────────────────────────────────────────────

    async def fts_search_memory(
        self,
        query: str,
        *,
        dimension: Optional[MemoryDimension] = None,
        limit: int = 15,
    ) -> List[tuple]:
        return await self._fts_search(
            kind=EntryKind.MEMORY.value,
            query=query,
            facet_value=dimension.value if dimension else None,
            limit=limit,
        )

    async def fts_search_knowledge(
        self,
        query: str,
        *,
        category: Optional[KnowledgeCategory] = None,
        limit: int = 15,
    ) -> List[tuple]:
        return await self._fts_search(
            kind=EntryKind.KNOWLEDGE.value,
            query=query,
            facet_value=category.value if category else None,
            limit=limit,
        )

    async def _fts_search(
        self,
        *,
        kind: str,
        query: str,
        facet_value: Optional[str],
        limit: int,
    ) -> List[tuple]:
        """Returns rows shaped as (entry_id, chunk_id, text, summary, facet, created_at, rank)."""
        safe = self._sanitize_fts_query(query)
        if kind == EntryKind.MEMORY.value:
            facet_col = "dimension"
            files = "memory_files"
            chunks = "memory_chunks"
            fts = "memory_chunks_fts"
        elif kind == EntryKind.KNOWLEDGE.value:
            facet_col = "category"
            files = "knowledge_files"
            chunks = "knowledge_chunks"
            fts = "knowledge_chunks_fts"
        else:
            raise ValueError(f"unknown kind: {kind!r}")

        sql = (
            f"SELECT e.id, c.id, c.text, e.summary, e.{facet_col}, "
            f"       e.created_at, bm25({fts}) AS rank "
            f"FROM {fts} fts "
            f"JOIN {chunks} c ON c.id = fts.chunk_id "
            f"JOIN {files} e ON e.id = c.entry_id "
            f"WHERE {fts} MATCH ? AND e.archived=0"
        )
        params: list = [safe]
        if facet_value:
            sql += f" AND e.{facet_col}=?"
            params.append(facet_value)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        try:
            return await self._fetchall(sql, tuple(params))
        except sqlite3.OperationalError as exc:
            # An empty / weird query can still slip past sanitize when every
            # token is an FTS reserved word — return empty rather than crash.
            _logger.debug("FTS search failed (%s); returning empty", exc)
            return []

    @staticmethod
    def _sanitize_fts_query(q: str) -> str:
        """Reduce *q* to a safe FTS5 OR-of-quoted-tokens query.

        FTS5 has its own mini-language (NEAR, AND, OR, parens, column refs).
        Free-text user input regularly trips that parser, so we extract word
        tokens with a regex and re-quote them. Cap at 32 tokens to avoid
        runaway queries on multi-page candidates.
        """
        import re
        tokens = re.findall(r"[\w一-鿿]+", q.lower())
        # FTS5 reserved bareword AND/OR/NOT/NEAR get coerced into quoted tokens
        # by virtue of the wrapping double-quotes below.
        if not tokens:
            return '""'
        return " OR ".join(f'"{t}"' for t in tokens[:32])

    # ── Embedding cache ─────────────────────────────────────────────────────

    async def get_embedding(
        self, chunk_id: str, kind: str, provider: str, model: str,
    ) -> Optional[bytes]:
        row = await self._fetchone(
            "SELECT embedding FROM embedding_cache "
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
            "INSERT OR REPLACE INTO embedding_cache "
            "(chunk_id, chunk_kind, provider, model, dims, hash, embedding, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, kind, provider, model, dims, hash_, embedding, _now()),
        )

    # ── Dense-recall helpers (hybrid retrieval) ─────────────────────────────

    async def list_embedded_chunks(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
    ) -> List[Tuple]:
        """Return all chunks of *kind* that have a cached embedding for
        (provider, model), joined with their parent entry's metadata.

        Used by stage 1b (dense brute-force recall) of the hybrid retrieval
        pipeline. The corpus is small enough (~hundreds of chunks) that a
        Python-side cosine sweep per query is cheap (~50ms for 1500 × 1024).

        Returned tuple shape (8-tuple, matching FTS row contract + embedding):
            (entry_id, chunk_id, text, summary, facet, created_at, hash, embedding_bytes)
        """
        if kind == EntryKind.MEMORY.value:
            files_table, chunks_table, facet_col = "memory_files", "memory_chunks", "dimension"
        elif kind == EntryKind.KNOWLEDGE.value:
            files_table, chunks_table, facet_col = "knowledge_files", "knowledge_chunks", "category"
        else:
            raise ValueError(f"unknown kind: {kind!r}")
        sql = (
            f"SELECT e.id, c.id, c.text, e.summary, e.{facet_col}, e.created_at, "
            f"       ec.hash, ec.embedding "
            f"FROM {chunks_table} c "
            f"JOIN {files_table} e ON e.id = c.entry_id "
            f"JOIN embedding_cache ec ON ec.chunk_id = c.id "
            f"  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            f"WHERE e.archived=0"
        )
        return await self._fetchall(sql, (kind, provider, model))

    async def list_chunks_missing_embedding(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        limit: int = 100,
    ) -> List[Tuple]:
        """Chunks that lack a cached embedding for (provider, model).

        Returns (chunk_id, entry_id, text, hash) tuples. Used by the
        DreamWorker's startup backfill to embed legacy chunks (entries
        inserted before P2 / when the embedder was unavailable).
        """
        if kind == EntryKind.MEMORY.value:
            files_table, chunks_table = "memory_files", "memory_chunks"
        elif kind == EntryKind.KNOWLEDGE.value:
            files_table, chunks_table = "knowledge_files", "knowledge_chunks"
        else:
            raise ValueError(f"unknown kind: {kind!r}")
        sql = (
            f"SELECT c.id, c.entry_id, c.text, c.hash "
            f"FROM {chunks_table} c "
            f"JOIN {files_table} e ON e.id = c.entry_id "
            f"WHERE e.archived=0 "
            f"  AND NOT EXISTS ("
            f"     SELECT 1 FROM embedding_cache ec "
            f"     WHERE ec.chunk_id = c.id "
            f"       AND ec.chunk_kind=? AND ec.provider=? AND ec.model=?"
            f"  ) "
            f"LIMIT ?"
        )
        return await self._fetchall(sql, (kind, provider, model, limit))

    # ── Merge / dedup scanner ───────────────────────────────────────────────

    async def list_entry_centroids(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
    ) -> List[Tuple]:
        """One representative-chunk row per non-archived entry, with its
        cached embedding.

        Why "centroid" rather than literal mean: most entries are small
        enough to fit in 1 chunk, and even multi-chunk entries have very
        similar chunk embeddings (they're sentences of the same idea).
        Using chunk 0's embedding as the entry's representative is cheap
        and accurate enough for the dedup-pair scanner.

        Returned row shape:
            (entry_id, summary, created_at, updated_at, embedding_bytes)
        """
        if kind == EntryKind.MEMORY.value:
            files_table, chunks_table = "memory_files", "memory_chunks"
        elif kind == EntryKind.KNOWLEDGE.value:
            files_table, chunks_table = "knowledge_files", "knowledge_chunks"
        else:
            raise ValueError(f"unknown kind: {kind!r}")
        sql = (
            f"SELECT e.id, e.summary, e.created_at, e.updated_at, ec.embedding "
            f"FROM {files_table} e "
            f"JOIN {chunks_table} c ON c.entry_id = e.id AND c.chunk_index = 0 "
            f"JOIN embedding_cache ec ON ec.chunk_id = c.id "
            f"  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            f"WHERE e.archived=0"
        )
        return await self._fetchall(sql, (kind, provider, model))

    async def insert_merge_proposal(
        self,
        *,
        kind: str,
        entry_a_id: str,
        entry_b_id: str,
        similarity: float,
    ) -> str:
        """Record a pending merge candidate. Idempotent on ordered pair —
        re-scanning the same pair simply updates similarity and bumps
        scanned_at.
        """
        # Canonicalise pair order so (a,b) and (b,a) collapse.
        a, b = sorted([entry_a_id, entry_b_id])
        # Look up an existing pending proposal for this pair.
        existing = await self._fetchone(
            "SELECT id FROM merge_proposals "
            "WHERE entry_a_id=? AND entry_b_id=? AND status='pending'",
            (a, b),
        )
        if existing:
            await self._execute(
                "UPDATE merge_proposals SET similarity=?, scanned_at=? WHERE id=?",
                (similarity, _now(), existing[0]),
            )
            return existing[0]
        pid = str(uuid.uuid4())
        await self._execute(
            "INSERT INTO merge_proposals "
            "(id, kind, entry_a_id, entry_b_id, similarity, status, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (pid, kind, a, b, similarity, _now()),
        )
        return pid

    async def list_merge_proposals(
        self, *, status: str = "pending", limit: int = 100,
    ) -> List[Tuple]:
        """Return (id, kind, entry_a_id, entry_b_id, similarity, scanned_at)."""
        return await self._fetchall(
            "SELECT id, kind, entry_a_id, entry_b_id, similarity, scanned_at "
            "FROM merge_proposals WHERE status=? "
            "ORDER BY similarity DESC LIMIT ?",
            (status, limit),
        )

    async def resolve_merge_proposal(
        self, *, proposal_id: str, status: str,
    ) -> None:
        """Set status to 'merged' / 'dismissed' / 'stale'."""
        await self._execute(
            "UPDATE merge_proposals SET status=?, resolved_at=? WHERE id=?",
            (status, _now(), proposal_id),
        )

    # ── L2 / L3 dream synthesis ─────────────────────────────────────────────

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
        """Return (entry_id, dim_or_cat, summary, content_text, embedding_bytes)
        for non-archived entries at a given synthesis_level whose chunk-0
        embedding is cached.

        Used by both L2 (level=0 → look at raw entries) and L3
        (level=2 → look at L2 patterns).

        ``since_seconds`` (optional) filters by ``updated_at >= now - X``,
        so the dream worker can do incremental synthesis (only look at
        what changed since last run).
        """
        if kind == EntryKind.MEMORY.value:
            files_table, chunks_table, facet_col = "memory_files", "memory_chunks", "dimension"
        elif kind == EntryKind.KNOWLEDGE.value:
            files_table, chunks_table, facet_col = "knowledge_files", "knowledge_chunks", "category"
        else:
            raise ValueError(f"unknown kind: {kind!r}")

        sql = (
            f"SELECT e.id, e.{facet_col}, e.summary, c.text, ec.embedding "
            f"FROM {files_table} e "
            f"JOIN {chunks_table} c ON c.entry_id = e.id AND c.chunk_index = 0 "
            f"JOIN embedding_cache ec ON ec.chunk_id = c.id "
            f"  AND ec.chunk_kind=? AND ec.provider=? AND ec.model=? "
            f"WHERE e.archived=0 AND e.synthesis_level=?"
        )
        params: list = [kind, provider, model, synthesis_level]
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
        target_facet: str,                # 'agentic'/'insight' or 'domain'/'people'/...
        summary: str,
        content: str,
        synthesis_level: int,             # 2 (pattern) or 3 (meta_insight)
        source_entry_ids: List[str],
        source_run_id: Optional[str] = None,
    ) -> str:
        """Insert a synthesised entry. Mirrors insert_*_entry but stamps
        synthesis_level + source_entry_ids JSON.
        """
        from .chunking import chunk_markdown

        if kind == EntryKind.MEMORY.value:
            files_table, chunks_table = "memory_files", "memory_chunks"
            facet_col = "dimension"
        elif kind == EntryKind.KNOWLEDGE.value:
            files_table, chunks_table = "knowledge_files", "knowledge_chunks"
            facet_col = "category"
        else:
            raise ValueError(f"unknown kind: {kind!r}")

        eid = str(uuid.uuid4())
        now = _now()
        chunks = chunk_markdown(content)
        source_json = json.dumps(source_entry_ids, ensure_ascii=False)

        def _txn() -> None:
            try:
                self._raw.execute("BEGIN IMMEDIATE")
                self._raw.execute(
                    f"INSERT INTO {files_table} "
                    f"(id, {facet_col}, summary, candidate_id, source, source_ref, "
                    f" archived, version, created_at, updated_at, "
                    f" synthesis_level, source_entry_ids) "
                    f"VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?)",
                    (eid, target_facet, summary[:120], None,
                     f"dream_l{synthesis_level}", source_run_id,
                     now, now, synthesis_level, source_json),
                )
                for i, c in enumerate(chunks):
                    self._raw.execute(
                        f"INSERT INTO {chunks_table} "
                        f"(id, entry_id, chunk_index, text, start_line, end_line, hash) "
                        f"VALUES (?, ?, ?, ?, ?, ?, ?)",
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

    async def insert_dream_run(
        self,
        *,
        level: int,
        kind: str,
    ) -> str:
        rid = str(uuid.uuid4())
        await self._execute(
            "INSERT INTO dream_runs (id, level, kind, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (rid, level, kind, _now()),
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
            "UPDATE dream_runs "
            "SET status=?, ended_at=?, source_count=?, cluster_count=?, "
            "    accepted_count=?, skipped_count=?, error=? WHERE id=?",
            (status, _now(), source_count, cluster_count,
             accepted_count, skipped_count, error, run_id),
        )

    async def get_last_dream_run(
        self, *, level: int, kind: str,
    ) -> Optional[Tuple]:
        """Return (id, started_at, ended_at, status) of the most recent
        dream run for (level, kind), or None if none yet.
        """
        return await self._fetchone(
            "SELECT id, started_at, ended_at, status FROM dream_runs "
            "WHERE level=? AND kind=? ORDER BY started_at DESC LIMIT 1",
            (level, kind),
        )

    async def list_in_flight_dream_run_sources(self) -> List[str]:
        """Entry ids that an actively-running L2/L3 synthesis is touching.

        Used by RetriageWorker to skip entries that the dream worker is
        about to consume (or just consumed). Without this, retriage might
        archive a source mid-way through synthesis and leave the new
        synthesis entry pointing at an already-archived source. A short
        list — at most ``DREAM_L2_MAX_CLUSTERS_PER_RUN * cluster_size``
        ids during a worst-case run.
        """
        rows = await self._fetchall(
            "SELECT source_entry_ids FROM memory_files WHERE source LIKE 'dream_l%' "
            "AND archived=0 AND source_entry_ids IS NOT NULL",
        )
        rows += await self._fetchall(
            "SELECT source_entry_ids FROM knowledge_files WHERE source LIKE 'dream_l%' "
            "AND archived=0 AND source_entry_ids IS NOT NULL",
        )
        out: List[str] = []
        for (raw,) in rows:
            if not raw:
                continue
            try:
                ids = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(ids, list):
                out.extend(str(x) for x in ids if x)
        return out

    # ── Correction proposals (v4) ───────────────────────────────────────────

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
            "INSERT INTO correction_proposals "
            "(id, kind, target_kind, target_entry_id, target_version, target_archived, "
            " payload, confidence, rule_version, parent_run_id, rationale, "
            " rationale_pii_scrubbed, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (pid, kind.value, target_kind.value, target_entry_id,
             int(target_version), 1 if target_archived else 0,
             payload_json, confidence, int(rule_version), parent_run_id,
             rationale[:2000], 1 if rationale_pii_scrubbed else 0,
             _now()),
        )
        return pid

    async def get_correction_proposal(
        self, pid: str,
    ) -> Optional[CorrectionProposal]:
        """Single proposal lookup. Used by ``apply_archive_correction`` to
        re-fetch the proposal under the write lock for staleness check.
        """
        row = await self._fetchone(
            "SELECT id, kind, target_kind, target_entry_id, target_version, "
            "       target_archived, payload, confidence, rule_version, parent_run_id, "
            "       rationale, rationale_pii_scrubbed, status, created_at, "
            "       resolved_at, resolved_by "
            "FROM correction_proposals WHERE id=?",
            (pid,),
        )
        return _row_to_correction_proposal(row) if row else None

    # ── Apply corrections (transactional, with staleness check) ─────────────

    async def apply_archive_correction(
        self, pid: str, *, resolved_by: str,
    ) -> bool:
        """Apply an ``archive`` proposal: archive target with a correction
        reason; if payload carries ``superseded_by_id`` set the FK.

        Returns False (and marks the proposal ``stale``) if the target's
        ``version`` or ``archived`` drifted from the snapshot —
        DreamWorker may have already changed the entry.
        """
        prop = await self.get_correction_proposal(pid)
        if prop is None or prop.kind != CorrectionKind.ARCHIVE:
            return False
        if prop.status != CorrectionStatus.PENDING:
            return False
        if prop.target_kind == EntryKind.MEMORY:
            files_table = "memory_files"
        elif prop.target_kind == EntryKind.KNOWLEDGE:
            files_table = "knowledge_files"
        else:
            return False

        reason = f"correction_v{prop.rule_version}_{prop.kind.value}"
        superseded_by_id = (
            (prop.payload or {}).get("superseded_by_id") if prop.payload else None
        )

        async with self._write_lock:
            try:
                cur = await asyncio.to_thread(
                    self._raw.execute,
                    f"SELECT version, archived FROM {files_table} WHERE id=?",
                    (prop.target_entry_id,),
                )
                row = await asyncio.to_thread(cur.fetchone)
                await asyncio.to_thread(cur.close)
                if not row:
                    await asyncio.to_thread(
                        self._raw.execute,
                        "UPDATE correction_proposals SET status='stale', "
                        "resolved_at=?, resolved_by=? WHERE id=?",
                        (_now(), resolved_by[:80], pid),
                    )
                    await asyncio.to_thread(self._raw.commit)
                    return False
                cur_version, cur_archived = int(row[0]), bool(row[1])
                if cur_version != prop.target_version or cur_archived != prop.target_archived:
                    await asyncio.to_thread(
                        self._raw.execute,
                        "UPDATE correction_proposals SET status='stale', "
                        "resolved_at=?, resolved_by=? WHERE id=?",
                        (_now(), resolved_by[:80], pid),
                    )
                    await asyncio.to_thread(self._raw.commit)
                    return False

                if not cur_archived:
                    sets = ["archived=1", "archived_reason=?", "updated_at=?"]
                    params = [reason, _now()]
                    if superseded_by_id:
                        sets.append("superseded_by=?")
                        params.append(superseded_by_id)
                    params.append(prop.target_entry_id)
                    await asyncio.to_thread(
                        self._raw.execute,
                        f"UPDATE {files_table} SET {', '.join(sets)} WHERE id=?",
                        tuple(params),
                    )
                await asyncio.to_thread(
                    self._raw.execute,
                    "UPDATE correction_proposals SET status='applied', "
                    "resolved_at=?, resolved_by=? WHERE id=?",
                    (_now(), resolved_by[:80], pid),
                )
                await asyncio.to_thread(self._raw.commit)
                return True
            except Exception:
                await asyncio.to_thread(self._raw.rollback)
                raise

    # ── Recall log (v4) ─────────────────────────────────────────────────────

    async def insert_recall_log_batch(
        self, rows: List[Tuple[str, str, int]],
    ) -> None:
        """Bulk insert (entry_id, kind, recalled_at) rows. ``executemany``
        for one prepared statement → many params, much cheaper than N
        separate writes.
        """
        if not rows:
            return
        async with self._write_lock:
            await asyncio.to_thread(
                self._raw.executemany,
                "INSERT INTO recall_log (entry_id, kind, recalled_at) VALUES (?, ?, ?)",
                rows,
            )
            await asyncio.to_thread(self._raw.commit)

    async def count_recent_recalls(
        self, *, entry_id: str, kind: str, since_seconds: int,
    ) -> int:
        cutoff = _now() - int(since_seconds)
        row = await self._fetchone(
            "SELECT COUNT(*) FROM recall_log "
            "WHERE entry_id=? AND kind=? AND recalled_at >= ?",
            (entry_id, kind, cutoff),
        )
        return int(row[0]) if row else 0

    async def prune_candidate_raw_text(self, older_than_ts: int) -> int:
        """Clear raw_text for fully-processed candidates older than cutoff.

        Only touches accepted_* and rejected rows — pending/triaging/failed
        rows still need their raw_text for the next triage attempt.
        Returns the number of rows updated.
        """
        statuses = ("accepted_memory", "accepted_knowledge", "accepted_both", "rejected")
        placeholders = ",".join("?" * len(statuses))
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                f"UPDATE memory_candidates SET raw_text='' "
                f"WHERE status IN ({placeholders}) "
                f"AND created_at < ? AND raw_text != ''",
                (*statuses, older_than_ts),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    async def prune_recall_log(self, older_than_ts: int) -> int:
        """Delete recall_log rows older than cutoff.

        Returns the number of rows deleted.
        """
        async with self._write_lock:
            cur = await asyncio.to_thread(
                self._raw.execute,
                "DELETE FROM recall_log WHERE recalled_at < ?",
                (older_than_ts,),
            )
            await asyncio.to_thread(self._raw.commit)
            return cur.rowcount

    # ── Retriage progress (resumable migration scans) ───────────────────────

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


# ── Row mapping helpers ─────────────────────────────────────────────────────

def _row_to_memory_entry(r: Tuple) -> Entry:
    try:
        dim = MemoryDimension(r[1])
    except ValueError:
        dim = None
    return Entry(
        id=r[0],
        kind=EntryKind.MEMORY,
        dimension=dim,
        summary=r[2],
        source=r[3] or "",
        source_ref=r[4],
        version=int(r[5]),
        archived=bool(r[6]),
        archived_reason=r[7],
        created_at=int(r[8]),
        updated_at=int(r[9]),
    )


def _row_to_knowledge_entry(r: Tuple) -> Entry:
    try:
        cat = KnowledgeCategory(r[1])
    except ValueError:
        cat = None
    return Entry(
        id=r[0],
        kind=EntryKind.KNOWLEDGE,
        category=cat,
        summary=r[2],
        source=r[3] or "",
        source_ref=r[4],
        version=int(r[5]),
        archived=bool(r[6]),
        archived_reason=r[7],
        created_at=int(r[8]),
        updated_at=int(r[9]),
    )


def _row_to_correction_proposal(r: Tuple) -> CorrectionProposal:
    """Map a 16-column SELECT row into the dataclass.

    Column order MUST match the SELECT in ``list_correction_proposals`` /
    ``get_correction_proposal`` — change one, change both.
    """
    try:
        kind = CorrectionKind(r[1])
    except ValueError:
        kind = CorrectionKind.ARCHIVE  # safest fallback
    try:
        target_kind = EntryKind(r[2])
    except ValueError:
        target_kind = EntryKind.MEMORY
    try:
        status = CorrectionStatus(r[12])
    except ValueError:
        status = CorrectionStatus.PENDING
    payload: Optional[dict] = None
    if r[6]:
        try:
            payload = json.loads(r[6])
            if not isinstance(payload, dict):
                payload = None
        except json.JSONDecodeError:
            payload = None
    return CorrectionProposal(
        id=r[0],
        kind=kind,
        target_kind=target_kind,
        target_entry_id=r[3],
        target_version=int(r[4]),
        target_archived=bool(r[5]),
        payload=payload,
        confidence=float(r[7]) if r[7] is not None else None,
        rule_version=int(r[8]),
        parent_run_id=r[9],
        rationale=r[10] or "",
        rationale_pii_scrubbed=bool(r[11]),
        status=status,
        created_at=int(r[13]),
        resolved_at=int(r[14]) if r[14] is not None else None,
        resolved_by=r[15],
    )
