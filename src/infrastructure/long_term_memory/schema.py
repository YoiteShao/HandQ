"""SQLite DDL + migrations for the long-term memory system.

Design source: docs/long_term_memory/03_schema.md.

Bootstrap (always runs first):
    memory_meta + migration_log

Migrations (numbered, run in order, recorded in migration_log):
    v1: memory + knowledge dual-track entries / chunks / fts / versions /
        candidates  +  embedding_cache  (the v1 minimum-viable shape)

Future migrations (placeholders, not yet implemented):
    v2: sessions_index               (P3)
    v3: procedure third track        (P6)

Each migration is an ``async`` callable receiving a Connection wrapper
exposing ``execute(...)`` and ``commit()`` — see ``store._SyncConn``.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, List


# Each statement runs unconditionally on every open(). They are CREATE … IF
# NOT EXISTS so they are idempotent and tolerate schema_version=0 cold starts.
DDL_BOOTSTRAP: List[str] = [
    """CREATE TABLE IF NOT EXISTS memory_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS migration_log (
        version    INTEGER PRIMARY KEY,
        applied_at INTEGER NOT NULL,
        note       TEXT
    )""",
]


# ── v1 ─ memory + knowledge dual-track minimum-viable schema ────────────────

_V1_STATEMENTS: List[str] = [
    # ── memory_files / chunks / fts / versions ──────────────────────────────
    """CREATE TABLE IF NOT EXISTS memory_files (
        id              TEXT PRIMARY KEY,
        dimension       TEXT NOT NULL,
        summary         TEXT NOT NULL,
        candidate_id    TEXT,
        source          TEXT NOT NULL,
        source_ref      TEXT,
        archived        INTEGER NOT NULL DEFAULT 0,
        archived_reason TEXT,
        superseded_by   TEXT REFERENCES memory_files(id) ON DELETE SET NULL,
        lang            TEXT NOT NULL DEFAULT 'auto',
        version         INTEGER NOT NULL DEFAULT 1,
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_files_active "
    "ON memory_files(dimension, archived, updated_at DESC)",

    """CREATE TABLE IF NOT EXISTS memory_chunks (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        text        TEXT NOT NULL,
        start_line  INTEGER,
        end_line    INTEGER,
        hash        TEXT NOT NULL,
        UNIQUE(entry_id, chunk_index)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_chunks_entry "
    "ON memory_chunks(entry_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS idx_memory_chunks_hash "
    "ON memory_chunks(hash)",

    """CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
        chunk_id  UNINDEXED,
        entry_id  UNINDEXED,
        dimension UNINDEXED,
        text,
        summary,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",

    # FTS sync triggers — chunk row is the source of truth, FTS is a view.
    """CREATE TRIGGER IF NOT EXISTS memory_chunks_ai
       AFTER INSERT ON memory_chunks BEGIN
         INSERT INTO memory_chunks_fts(chunk_id, entry_id, dimension, text, summary)
         SELECT new.id, new.entry_id, f.dimension, new.text, f.summary
         FROM memory_files f WHERE f.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS memory_chunks_au
       AFTER UPDATE ON memory_chunks BEGIN
         DELETE FROM memory_chunks_fts WHERE chunk_id = old.id;
         INSERT INTO memory_chunks_fts(chunk_id, entry_id, dimension, text, summary)
         SELECT new.id, new.entry_id, f.dimension, new.text, f.summary
         FROM memory_files f WHERE f.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS memory_chunks_ad
       AFTER DELETE ON memory_chunks BEGIN
         DELETE FROM memory_chunks_fts WHERE chunk_id = old.id;
       END""",

    """CREATE TABLE IF NOT EXISTS memory_versions (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES memory_files(id) ON DELETE CASCADE,
        version     INTEGER NOT NULL,
        summary     TEXT NOT NULL,
        chunks_json TEXT NOT NULL,
        archived_at INTEGER NOT NULL,
        UNIQUE(entry_id, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_versions_entry "
    "ON memory_versions(entry_id, version DESC)",

    # ── candidates (single table drives BOTH memory and knowledge triage) ───
    """CREATE TABLE IF NOT EXISTS memory_candidates (
        id           TEXT PRIMARY KEY,
        source       TEXT NOT NULL,
        source_ref   TEXT,
        raw_text     TEXT NOT NULL,
        hint         TEXT,
        metadata     TEXT,
        status       TEXT NOT NULL,
        reason       TEXT,
        retry_count  INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memory_candidates_pending "
    "ON memory_candidates(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_candidates_source "
    "ON memory_candidates(source, source_ref)",

    # ── knowledge_files / chunks / fts / versions (parallel to memory) ──────
    """CREATE TABLE IF NOT EXISTS knowledge_files (
        id              TEXT PRIMARY KEY,
        category        TEXT NOT NULL,
        summary         TEXT NOT NULL,
        candidate_id    TEXT,
        source          TEXT NOT NULL,
        source_ref      TEXT,
        archived        INTEGER NOT NULL DEFAULT 0,
        archived_reason TEXT,
        superseded_by   TEXT REFERENCES knowledge_files(id) ON DELETE SET NULL,
        lang            TEXT NOT NULL DEFAULT 'auto',
        version         INTEGER NOT NULL DEFAULT 1,
        project_root    TEXT,
        fs_mirror_path  TEXT,
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_files_active "
    "ON knowledge_files(category, archived, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_files_project "
    "ON knowledge_files(project_root, archived)",

    """CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES knowledge_files(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        text        TEXT NOT NULL,
        start_line  INTEGER,
        end_line    INTEGER,
        hash        TEXT NOT NULL,
        UNIQUE(entry_id, chunk_index)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_entry "
    "ON knowledge_chunks(entry_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_hash "
    "ON knowledge_chunks(hash)",

    """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
        chunk_id UNINDEXED,
        entry_id UNINDEXED,
        category UNINDEXED,
        text,
        summary,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ai
       AFTER INSERT ON knowledge_chunks BEGIN
         INSERT INTO knowledge_chunks_fts(chunk_id, entry_id, category, text, summary)
         SELECT new.id, new.entry_id, f.category, new.text, f.summary
         FROM knowledge_files f WHERE f.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_au
       AFTER UPDATE ON knowledge_chunks BEGIN
         DELETE FROM knowledge_chunks_fts WHERE chunk_id = old.id;
         INSERT INTO knowledge_chunks_fts(chunk_id, entry_id, category, text, summary)
         SELECT new.id, new.entry_id, f.category, new.text, f.summary
         FROM knowledge_files f WHERE f.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ad
       AFTER DELETE ON knowledge_chunks BEGIN
         DELETE FROM knowledge_chunks_fts WHERE chunk_id = old.id;
       END""",

    """CREATE TABLE IF NOT EXISTS knowledge_versions (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES knowledge_files(id) ON DELETE CASCADE,
        version     INTEGER NOT NULL,
        summary     TEXT NOT NULL,
        chunks_json TEXT NOT NULL,
        archived_at INTEGER NOT NULL,
        UNIQUE(entry_id, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_versions_entry "
    "ON knowledge_versions(entry_id, version DESC)",

    # ── embedding_cache (shared across kinds via chunk_kind discriminator) ──
    """CREATE TABLE IF NOT EXISTS embedding_cache (
        chunk_id    TEXT NOT NULL,
        chunk_kind  TEXT NOT NULL,
        provider    TEXT NOT NULL,
        model       TEXT NOT NULL,
        dims        INTEGER NOT NULL,
        hash        TEXT NOT NULL,
        embedding   BLOB NOT NULL,
        updated_at  INTEGER NOT NULL,
        PRIMARY KEY (chunk_id, chunk_kind, provider, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_embedding_cache_hash "
    "ON embedding_cache(hash, provider, model)",
    "CREATE INDEX IF NOT EXISTS idx_embedding_cache_updated "
    "ON embedding_cache(updated_at)",
]


async def _migration_v1(conn) -> None:
    """v1: memory + knowledge dual-track minimum-viable schema."""
    for stmt in _V1_STATEMENTS:
        await conn.execute(stmt)
    # Mark default config in meta so reads can short-circuit.
    await conn.execute(
        "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
        ("created_at", str(int(time.time()))),
    )


# ── v2 ─ merge_proposals (post-hoc dedup) ────────────────────────────────────

_V2_STATEMENTS: List[str] = [
    """CREATE TABLE IF NOT EXISTS merge_proposals (
        id           TEXT PRIMARY KEY,
        kind         TEXT NOT NULL,           -- 'memory' | 'knowledge'
        entry_a_id   TEXT NOT NULL,           -- weak FK; kept after archive
        entry_b_id   TEXT NOT NULL,
        similarity   REAL NOT NULL,           -- cosine(emb_a, emb_b) at scan time
        status       TEXT NOT NULL,
            -- 'pending'   : waiting for user review
            -- 'merged'    : user accepted, executed (or auto-merged at exact bar)
            -- 'dismissed' : user said keep both
            -- 'stale'     : one of the entries archived for other reason
        scanned_at   INTEGER NOT NULL,
        resolved_at  INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_merge_proposals_pending "
    "ON merge_proposals(status, scanned_at)",
    "CREATE INDEX IF NOT EXISTS idx_merge_proposals_pair "
    "ON merge_proposals(entry_a_id, entry_b_id)",
]


async def _migration_v2(conn) -> None:
    """v2: merge_proposals — post-hoc dedup pairs awaiting user review."""
    for stmt in _V2_STATEMENTS:
        await conn.execute(stmt)


# ── v3 ─ Dream synthesis (L2 patterns + L3 meta-insights) ───────────────────
#
# L2/L3 outputs are stored *in the same memory_files / knowledge_files
# tables* as raw L1 entries — they are still memory or knowledge points,
# just synthesised from multiple sources. Two new columns track provenance:
#
#   synthesis_level     : 0 = raw L1, 2 = L2 pattern, 3 = L3 meta-insight
#   source_entry_ids    : JSON list of the lower-level entry ids this row
#                         was synthesised from (NULL for raw)
#
# A separate ``dream_runs`` table records each L2/L3 batch run so we can
# answer "when did the worker last try?" without scanning entries.

_V3_STATEMENTS: List[str] = [
    "ALTER TABLE memory_files ADD COLUMN synthesis_level INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE memory_files ADD COLUMN source_entry_ids TEXT",
    "ALTER TABLE knowledge_files ADD COLUMN synthesis_level INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE knowledge_files ADD COLUMN source_entry_ids TEXT",
    """CREATE TABLE IF NOT EXISTS dream_runs (
        id              TEXT PRIMARY KEY,
        level           INTEGER NOT NULL,         -- 2 or 3
        kind            TEXT NOT NULL,             -- 'memory' | 'knowledge'
        started_at      INTEGER NOT NULL,
        ended_at        INTEGER,
        source_count    INTEGER,                   -- entries fed in
        cluster_count   INTEGER,                   -- clusters produced
        accepted_count  INTEGER,                   -- LLM-judged worth-keeping
        skipped_count   INTEGER,                   -- LLM-rejected
        status          TEXT NOT NULL,             -- 'running' | 'complete' | 'failed'
        error           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dream_runs_level "
    "ON dream_runs(level, started_at DESC)",
    """CREATE INDEX IF NOT EXISTS idx_memory_files_synthesis
       ON memory_files(synthesis_level, archived, updated_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_files_synthesis
       ON knowledge_files(synthesis_level, archived, updated_at DESC)""",
]


async def _migration_v3(conn) -> None:
    """v3: dream synthesis L2/L3 — adds synthesis_level columns to entry
    tables and creates dream_runs ledger.
    """
    for stmt in _V3_STATEMENTS:
        # ALTER TABLE ADD COLUMN is idempotent against fresh DBs (the table
        # was created in v1) but raises 'duplicate column' on re-run. We
        # tolerate that so re-applying the migration on a partially-upgraded
        # DB doesn't block.
        try:
            await conn.execute(stmt)
        except Exception as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise


# ── v4 ─ correction_proposals + recall_log ──────────────────────────────────
#
# correction_proposals generalises ``merge_proposals`` to four kinds:
# 'archive' / 'rewrite' / 'merge' / 'synthesis'. Produced by the
# RetriageWorker when a rule_migration audits the existing corpus against
# the current TRIAGE_SYSTEM. Default behaviour is propose-only — the
# RetriageWorker auto-applies only the deterministic kinds (e.g. archiving
# L2-orphan source entries that the synthesis row already supersedes).
#
# Staleness: ``target_version`` + ``target_archived`` snapshot the entry's
# state at proposal time. Apply path re-checks both inside a transaction;
# if either drifted (DreamWorker updated/archived it concurrently) the
# proposal flips to status='stale' instead of overwriting.
#
# recall_log is a thin append-only ledger of recall hits. Used by the
# LLM retriage prompt as a priority signal — entries the user actually
# recalled get their proposals surfaced more prominently rather than
# auto-suppressed (yansu philosophy: never silently drop user-relevant
# data; surface it for review).
_V4_STATEMENTS: List[str] = [
    """CREATE TABLE IF NOT EXISTS correction_proposals (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
            -- 'archive' | 'rewrite' | 'merge' | 'synthesis'
        target_kind     TEXT NOT NULL,             -- 'memory' | 'knowledge'
        target_entry_id TEXT NOT NULL,
        target_version  INTEGER NOT NULL,          -- staleness snapshot
        target_archived INTEGER NOT NULL,          -- staleness snapshot
        payload         TEXT,
            -- JSON: {new_summary, new_content_md} for rewrite,
            --       {superseded_by_id} for archive,
            --       {keep_id} for merge, etc.
        confidence      REAL,                       -- 0..1, LLM-emitted
        rule_version    INTEGER NOT NULL,          -- which migration produced this
        parent_run_id   TEXT,                       -- ties to one RetriageWorker run
        rationale       TEXT,                       -- PII-scrubbed
        rationale_pii_scrubbed INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'pending',
            -- pending | applied | dismissed | superseded | stale
        created_at      INTEGER NOT NULL,
        resolved_at     INTEGER,
        resolved_by     TEXT
            -- 'user' | 'auto_deterministic' | 'cli' | 'rule_migration_vN'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_corr_pending "
    "ON correction_proposals(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_corr_target "
    "ON correction_proposals(target_kind, target_entry_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_corr_run "
    "ON correction_proposals(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_corr_rule_version "
    "ON correction_proposals(rule_version, status)",

    # recall_log is intentionally narrow — three columns, two indexes.
    # Append-only; no FK to entries because the entry may be archived
    # later and we still want the recall history. Pruning policy lives
    # in the dream worker (TODO future: drop rows older than 90 days).
    """CREATE TABLE IF NOT EXISTS recall_log (
        entry_id    TEXT NOT NULL,
        kind        TEXT NOT NULL,         -- 'memory' | 'knowledge'
        recalled_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recall_entry "
    "ON recall_log(entry_id, recalled_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_recall_time "
    "ON recall_log(recalled_at DESC)",
]


async def _migration_v4(conn) -> None:
    """v4: correction_proposals (generalised merge_proposals) + recall_log.

    merge_proposals is preserved as-is for one release cycle so existing
    scan history isn't lost; future migration will copy 'merge' kind
    rows in and deprecate the old table.
    """
    for stmt in _V4_STATEMENTS:
        await conn.execute(stmt)


# Ordered list — index N -> migration_v(N+1). New migrations append; never edit
# in place once shipped (would silently desync existing user databases).
MIGRATIONS: List[Callable[[object], Awaitable[None]]] = [
    _migration_v1,
    _migration_v2,
    _migration_v3,
    _migration_v4,
]
