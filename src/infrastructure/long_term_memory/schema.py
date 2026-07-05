"""SQLite DDL + migrations for the long-term memory system.

LTM 2.0 baseline schema. Three logical namespaces:

    obs_*  — observation layer (snapshots, OCR frames, events, sessions,
             semantic events, pipeline runs, daily/weekly summaries)
    mem_*  — unified memory layer (mem_entries with kind=memory|knowledge;
             supporting chunks/FTS/versions/candidates/
             embedding_cache/recall_log/correction_proposals/dream_runs)
    ent_*  — entity graph (principals: people/machines/projects; aliases;
             sightings ledger)

Bootstrap (DDL_BOOTSTRAP) creates the minimum scaffolding (memory_meta +
migration_log). The single migration ``_migration_baseline`` then creates
all of the above tables in one go.

Legacy v1-v4 schema (``memory_files`` / ``knowledge_files`` /
``memory_candidates`` / etc.) is no longer created. Existing user DBs
that carry those tables are detected by the force-reset trigger in
``store.py:SQLiteStore.open()`` (which checks table presence, not version
number) and wiped on first boot of this version. After that one-shot
reset, this schema is the only schema.

Migrations:
    v1: baseline (this file)
    v2+: additive only (never edit baseline once shipped — would silently
         desync existing user databases).
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


# ── v1 baseline: full LTM 2.0 schema in one migration ───────────────────────

_BASELINE_STATEMENTS: List[str] = [
    # ── obs_snapshots — every captured frame ──────────────────────────────
    """CREATE TABLE IF NOT EXISTS obs_snapshots (
        id                  TEXT PRIMARY KEY,
        captured_at         INTEGER NOT NULL,
        monitor_index       INTEGER NOT NULL,
        monitor_label       TEXT,
        window_title        TEXT,
        process_name        TEXT,
        browser_url         TEXT,
        top_window_titles   TEXT,
        content_type        TEXT,
        ax_text             TEXT,
        parsed_json         TEXT,
        frame_json          TEXT,
        frame_os            TEXT GENERATED ALWAYS AS (json_extract(frame_json, '$.os')) STORED,
        frame_host          TEXT GENERATED ALWAYS AS (json_extract(frame_json, '$.host')) STORED,
        focus_rect_x        INTEGER,
        focus_rect_y        INTEGER,
        focus_rect_w        INTEGER,
        focus_rect_h        INTEGER,
        ocr_used_focus_rect INTEGER NOT NULL DEFAULT 0,
        system_idle_sec     INTEGER,
        novelty_score       REAL,
        tier                TEXT,
        storage_tier        TEXT NOT NULL DEFAULT 'hot',
        session_id          TEXT,
        semantic_event_id   TEXT,
        pii_redacted        INTEGER NOT NULL DEFAULT 0,
        discarded           INTEGER NOT NULL DEFAULT 0,
        discarded_reason    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_snapshots_session  ON obs_snapshots(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_snapshots_captured ON obs_snapshots(captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_obs_snapshots_frame    ON obs_snapshots(frame_os)",

    """CREATE VIRTUAL TABLE IF NOT EXISTS obs_snapshots_fts USING fts5(
        snapshot_id  UNINDEXED,
        window_title,
        process_name,
        ax_text,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS obs_snapshots_ai
       AFTER INSERT ON obs_snapshots BEGIN
         INSERT INTO obs_snapshots_fts(snapshot_id, window_title, process_name, ax_text)
         VALUES (new.id, new.window_title, new.process_name, new.ax_text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS obs_snapshots_au
       AFTER UPDATE ON obs_snapshots BEGIN
         DELETE FROM obs_snapshots_fts WHERE snapshot_id = old.id;
         INSERT INTO obs_snapshots_fts(snapshot_id, window_title, process_name, ax_text)
         VALUES (new.id, new.window_title, new.process_name, new.ax_text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS obs_snapshots_ad
       AFTER DELETE ON obs_snapshots BEGIN
         DELETE FROM obs_snapshots_fts WHERE snapshot_id = old.id;
       END""",

    # ── obs_ocr_frames — 1..N OCR passes per snapshot ─────────────────────
    """CREATE TABLE IF NOT EXISTS obs_ocr_frames (
        id               TEXT PRIMARY KEY,
        snapshot_id      TEXT NOT NULL REFERENCES obs_snapshots(id) ON DELETE CASCADE,
        text             TEXT NOT NULL,
        confidence       REAL,
        embedding        BLOB,
        pipeline_version TEXT,
        captured_at      INTEGER NOT NULL,
        is_focus_rect    INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_ocr_frames_snapshot ON obs_ocr_frames(snapshot_id)",

    """CREATE VIRTUAL TABLE IF NOT EXISTS obs_ocr_frames_fts USING fts5(
        ocr_id      UNINDEXED,
        snapshot_id UNINDEXED,
        text,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS obs_ocr_frames_ai
       AFTER INSERT ON obs_ocr_frames BEGIN
         INSERT INTO obs_ocr_frames_fts(ocr_id, snapshot_id, text)
         VALUES (new.id, new.snapshot_id, new.text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS obs_ocr_frames_au
       AFTER UPDATE ON obs_ocr_frames BEGIN
         DELETE FROM obs_ocr_frames_fts WHERE ocr_id = old.id;
         INSERT INTO obs_ocr_frames_fts(ocr_id, snapshot_id, text)
         VALUES (new.id, new.snapshot_id, new.text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS obs_ocr_frames_ad
       AFTER DELETE ON obs_ocr_frames BEGIN
         DELETE FROM obs_ocr_frames_fts WHERE ocr_id = old.id;
       END""",

    # ── obs_events — discrete state-change event stream ───────────────────
    """CREATE TABLE IF NOT EXISTS obs_events (
        id          TEXT PRIMARY KEY,
        session_id  TEXT,
        kind        TEXT NOT NULL,
        data        TEXT,
        sort_order  INTEGER NOT NULL,
        occurred_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_events_session ON obs_events(session_id, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_obs_events_time    ON obs_events(occurred_at DESC)",

    # ── obs_sessions — continuous work group aggregation ──────────────────
    """CREATE TABLE IF NOT EXISTS obs_sessions (
        id                   TEXT PRIMARY KEY,
        session_key          TEXT NOT NULL,
        trigger_kind         TEXT NOT NULL,
        started_at           INTEGER NOT NULL,
        ended_at             INTEGER,
        frame_os             TEXT,
        frame_host           TEXT,
        primary_process      TEXT,
        primary_window_title TEXT,
        semantic_status      TEXT NOT NULL DEFAULT 'pending',
        triage_status        TEXT NOT NULL DEFAULT 'pending',
        snapshot_count       INTEGER NOT NULL DEFAULT 0,
        apps_seen            TEXT,
        principal_ids        TEXT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_sessions_key ON obs_sessions(session_key)",
    "CREATE INDEX IF NOT EXISTS idx_obs_sessions_status ON obs_sessions(semantic_status, triage_status)",

    # ── obs_semantic_events — LLM-abstracted "what user did" ──────────────
    """CREATE TABLE IF NOT EXISTS obs_semantic_events (
        id               TEXT PRIMARY KEY,
        session_id       TEXT,
        synthetic_origin TEXT,
        extracted_at     INTEGER NOT NULL,
        title            TEXT NOT NULL,
        description      TEXT,
        category         TEXT,
        entities         TEXT,
        apps             TEXT,
        time_range_start INTEGER NOT NULL,
        time_range_end   INTEGER NOT NULL,
        task_worthy      INTEGER NOT NULL DEFAULT 0,
        worth_memory     INTEGER NOT NULL DEFAULT 0,
        worth_knowledge  INTEGER NOT NULL DEFAULT 0,
        worth_skill      INTEGER NOT NULL DEFAULT 0,
        frame_os         TEXT,
        frame_host       TEXT,
        frame_confidence REAL,
        accepted_entries TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_semantic_session ON obs_semantic_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_obs_semantic_time    ON obs_semantic_events(time_range_end DESC)",

    # ── obs_pipeline_runs — triage audit ledger ───────────────────────────
    """CREATE TABLE IF NOT EXISTS obs_pipeline_runs (
        id                TEXT PRIMARY KEY,
        started_at        INTEGER NOT NULL,
        finished_at       INTEGER,
        parent_session_id TEXT,
        semantic_event_id TEXT,
        prefilter_pass    INTEGER,
        prefilter_reason  TEXT,
        triage_status     TEXT,
        triage_reason     TEXT,
        llm_tokens        INTEGER,
        duration_ms       INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_obs_pipeline_started ON obs_pipeline_runs(started_at DESC)",

    # ── mem_entries — unified memory / knowledge ──────────────────────────
    """CREATE TABLE IF NOT EXISTS mem_entries (
        id                TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,
        dimension         TEXT,
        category          TEXT,
        recurrence_count  INTEGER NOT NULL DEFAULT 1,
        summary           TEXT NOT NULL,
        frame_json        TEXT,
        frame_os          TEXT GENERATED ALWAYS AS (json_extract(frame_json, '$.os')) STORED,
        source_event_id   TEXT,
        source            TEXT NOT NULL,
        source_ref        TEXT,
        archived          INTEGER NOT NULL DEFAULT 0,
        archived_reason   TEXT,
        superseded_by     TEXT REFERENCES mem_entries(id) ON DELETE SET NULL,
        synthesis_level   INTEGER NOT NULL DEFAULT 0,
        source_entry_ids  TEXT,
        version           INTEGER NOT NULL DEFAULT 1,
        recall_count_30d  INTEGER NOT NULL DEFAULT 0,
        principal_ids     TEXT,
        created_at        INTEGER NOT NULL,
        updated_at        INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_entries_lookup ON mem_entries(kind, archived, dimension, category)",
    "CREATE INDEX IF NOT EXISTS idx_mem_entries_frame  ON mem_entries(kind, archived, frame_os)",
    "CREATE INDEX IF NOT EXISTS idx_mem_entries_recent ON mem_entries(updated_at DESC)",

    # ── mem_chunks — chunked content + embedding source ───────────────────
    """CREATE TABLE IF NOT EXISTS mem_chunks (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES mem_entries(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL,
        text        TEXT NOT NULL,
        start_line  INTEGER,
        end_line    INTEGER,
        hash        TEXT NOT NULL,
        UNIQUE(entry_id, chunk_index)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_chunks_entry ON mem_chunks(entry_id, chunk_index)",
    "CREATE INDEX IF NOT EXISTS idx_mem_chunks_hash  ON mem_chunks(hash)",

    """CREATE VIRTUAL TABLE IF NOT EXISTS mem_chunks_fts USING fts5(
        chunk_id  UNINDEXED,
        entry_id  UNINDEXED,
        kind      UNINDEXED,
        dimension UNINDEXED,
        category  UNINDEXED,
        text,
        summary,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS mem_chunks_ai
       AFTER INSERT ON mem_chunks BEGIN
         INSERT INTO mem_chunks_fts(chunk_id, entry_id, kind, dimension, category, text, summary)
         SELECT new.id, new.entry_id, e.kind, e.dimension, e.category, new.text, e.summary
         FROM mem_entries e WHERE e.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS mem_chunks_au
       AFTER UPDATE ON mem_chunks BEGIN
         DELETE FROM mem_chunks_fts WHERE chunk_id = old.id;
         INSERT INTO mem_chunks_fts(chunk_id, entry_id, kind, dimension, category, text, summary)
         SELECT new.id, new.entry_id, e.kind, e.dimension, e.category, new.text, e.summary
         FROM mem_entries e WHERE e.id = new.entry_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS mem_chunks_ad
       AFTER DELETE ON mem_chunks BEGIN
         DELETE FROM mem_chunks_fts WHERE chunk_id = old.id;
       END""",

    # ── mem_versions — entry version history ──────────────────────────────
    """CREATE TABLE IF NOT EXISTS mem_versions (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL REFERENCES mem_entries(id) ON DELETE CASCADE,
        version     INTEGER NOT NULL,
        summary     TEXT NOT NULL,
        chunks_json TEXT NOT NULL,
        archived_at INTEGER NOT NULL,
        UNIQUE(entry_id, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_versions_entry ON mem_versions(entry_id, version DESC)",

    # ── mem_candidates — queue for non-observation submits ────────────────
    """CREATE TABLE IF NOT EXISTS mem_candidates (
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
    "CREATE INDEX IF NOT EXISTS idx_mem_candidates_pending ON mem_candidates(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_mem_candidates_source  ON mem_candidates(source, source_ref)",

    # ── mem_embedding_cache — chunk → vector ──────────────────────────────
    """CREATE TABLE IF NOT EXISTS mem_embedding_cache (
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
    "CREATE INDEX IF NOT EXISTS idx_mem_embedding_cache_hash ON mem_embedding_cache(hash, provider, model)",

    # ── mem_recall_log — append-only recall hit ledger ────────────────────
    """CREATE TABLE IF NOT EXISTS mem_recall_log (
        entry_id    TEXT NOT NULL,
        kind        TEXT NOT NULL,
        recalled_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_recall_entry ON mem_recall_log(entry_id, recalled_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mem_recall_time  ON mem_recall_log(recalled_at DESC)",

    # ── mem_correction_proposals — retriage outputs ───────────────────────
    """CREATE TABLE IF NOT EXISTS mem_correction_proposals (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        target_entry_id TEXT NOT NULL,
        target_version  INTEGER NOT NULL,
        target_archived INTEGER NOT NULL,
        payload         TEXT,
        confidence      REAL,
        rule_version    INTEGER NOT NULL,
        parent_run_id   TEXT,
        rationale       TEXT,
        rationale_pii_scrubbed INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'pending',
        created_at      INTEGER NOT NULL,
        resolved_at     INTEGER,
        resolved_by     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_corr_pending ON mem_correction_proposals(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_mem_corr_target  ON mem_correction_proposals(target_entry_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_corr_run     ON mem_correction_proposals(parent_run_id)",

    # ── mem_dream_runs — L2/L3 synthesis batch ledger ─────────────────────
    """CREATE TABLE IF NOT EXISTS mem_dream_runs (
        id              TEXT PRIMARY KEY,
        level           INTEGER NOT NULL,
        kind            TEXT NOT NULL,
        started_at      INTEGER NOT NULL,
        ended_at        INTEGER,
        source_count    INTEGER,
        cluster_count   INTEGER,
        accepted_count  INTEGER,
        skipped_count   INTEGER,
        status          TEXT NOT NULL,
        error           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mem_dream_runs_level ON mem_dream_runs(level, started_at DESC)",

    # ── ent_principals — canonical entity registry (people/machines/projects)
    """CREATE TABLE IF NOT EXISTS ent_principals (
        id              TEXT PRIMARY KEY,
        kind            TEXT NOT NULL,
        canonical_name  TEXT NOT NULL,
        display_name    TEXT,
        email           TEXT,
        host_kind       TEXT,
        os              TEXT,
        project_root    TEXT,
        description     TEXT,
        first_seen      INTEGER NOT NULL,
        last_seen       INTEGER NOT NULL,
        sighting_count  INTEGER NOT NULL DEFAULT 0,
        archived        INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ent_principals_canon ON ent_principals(kind, canonical_name)",
    "CREATE INDEX IF NOT EXISTS idx_ent_principals_last_seen ON ent_principals(last_seen DESC)",

    # ── ent_aliases — alias → principal mapping ───────────────────────────
    """CREATE TABLE IF NOT EXISTS ent_aliases (
        principal_id TEXT NOT NULL REFERENCES ent_principals(id) ON DELETE CASCADE,
        alias        TEXT NOT NULL,
        PRIMARY KEY (principal_id, alias)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ent_aliases_alias ON ent_aliases(alias)",

    # ── ent_sightings — when/where principal appeared ─────────────────────
    """CREATE TABLE IF NOT EXISTS ent_sightings (
        id           TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL REFERENCES ent_principals(id) ON DELETE CASCADE,
        source_kind  TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        sighted_at   INTEGER NOT NULL,
        context      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ent_sightings_principal ON ent_sightings(principal_id, sighted_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ent_sightings_source ON ent_sightings(source_kind, source_id)",
]


async def _migration_baseline(conn) -> None:
    """v1 baseline: all observation, memory, and entity tables in one shot.

    Sole migration in the new LTM 2.0 lineage. Legacy v1-v4 schema
    (memory_files / knowledge_files / memory_candidates / merge_proposals
    / dream_runs / correction_proposals / recall_log / etc.) is no longer
    created — existing user DBs that carry those tables are wiped by
    store.py's force-reset trigger before this migration ever runs.
    """
    for stmt in _BASELINE_STATEMENTS:
        await conn.execute(stmt)
    # Mark "this DB was created under LTM 2.0 lineage" in memory_meta so
    # legacy-detection code can distinguish "fresh new install" from
    # "needs reset".
    await conn.execute(
        "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
        ("created_at", str(int(time.time()))),
    )
    await conn.execute(
        "INSERT OR REPLACE INTO memory_meta(key, value) VALUES (?, ?)",
        ("lineage", "ltm_2.0"),
    )


async def _migration_drop_session_triage_status(conn) -> None:
    """v2: drop the vestigial ``obs_sessions.triage_status`` column.

    Session-level triage was the original design, but triage was reworked to
    operate at the SEMANTIC-EVENT grain (it consumes ``obs_semantic_events``,
    keyed on ``accepted_entries IS NULL``). Nothing ever advanced
    ``obs_sessions.triage_status`` off its ``'pending'`` default and nothing
    read it — a dead column. The composite index ``idx_obs_sessions_status``
    referenced it, so we drop the index first, drop the column, then recreate
    the index on ``semantic_status`` alone. (SQLite >= 3.35 supports
    ``ALTER TABLE ... DROP COLUMN``; the deployment ships 3.49+.)
    """
    await conn.execute("DROP INDEX IF EXISTS idx_obs_sessions_status")
    await conn.execute("ALTER TABLE obs_sessions DROP COLUMN triage_status")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_sessions_status "
        "ON obs_sessions(semantic_status)"
    )


async def _migration_unique_session_extraction(conn) -> None:
    """v3: enforce single semantic event per (session, non-synthetic).

    SemanticExtractor previously did ``insert_obs_semantic_event`` then
    ``set_obs_session_status('done')`` as two non-atomic steps. A crash
    between them re-extracted the same session on the next tick, producing
    duplicate ``obs_semantic_events`` rows that then fed duplicate
    ``mem_entries`` (INSIGHT/Knowledge tracks lack fingerprint dedup).

    Solution: a partial UNIQUE on ``session_id`` (skipping synthetic events
    where ``synthetic_origin`` is non-NULL — L2/L3 synthesis legitimately
    produces many such rows with NULL ``session_id``). The second insert
    raises ``IntegrityError``; the worker swallows it and treats the
    session as already-extracted.

    Defensive cleanup: pre-migration duplicates are unlikely (force-reset
    already wiped legacy DBs and the duplicate-event window only opened
    after the rebuild shipped), but if any exist we keep the most-recently-
    extracted row per ``session_id`` so the index creation succeeds.
    """
    await conn.execute(
        """DELETE FROM obs_semantic_events
           WHERE id IN (
               SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY extracted_at DESC
                   ) AS rn
                   FROM obs_semantic_events
                   WHERE session_id IS NOT NULL
                     AND synthetic_origin IS NULL
               ) WHERE rn > 1
           )"""
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_semantic_session_unique "
        "ON obs_semantic_events(session_id) "
        "WHERE session_id IS NOT NULL AND synthetic_origin IS NULL"
    )


async def _migration_v4(conn) -> None:
    """v4: retired placeholder — intentionally a no-op.

    Slot 4 was originally occupied by a migration that some user DBs recorded
    in ``migration_log`` as ``_migration_v4``. That slot was later replaced
    *in place* by ``_migration_skill_recurrence`` — a violation of the
    append-only rule below. Because ``store._apply_migrations`` gates purely on
    the positional ``schema_version`` (``cur_version >= len(MIGRATIONS)``), any
    DB that had already advanced to v4 saw "4 of 4 applied" and never ran the
    replacement, so the ``skill_recurrence`` table was never created and every
    skill-extraction attempt failed silently (no such table).

    The fix keeps this slot as a no-op to preserve the historical v4 identity
    and moves the table creation to a fresh v5 slot
    (``_migration_skill_recurrence``), which every DB — old or new — now runs.
    Do NOT repurpose this slot; append new migrations at the end instead.
    """
    return


async def _migration_skill_recurrence(conn) -> None:
    """v5: add the ``skill_recurrence`` counter table.

    Skills are minted from successful HandQ task sessions, but only once a
    task *pattern* recurs (see ``triage._apply_session_skill`` +
    ``SKILL_RECURRENCE_THRESHOLD``). This table is the recurrence counter:
    one row per LLM-normalized ``skill_fingerprint``, incremented on every
    skill-worthy ``SESSION_COMPLETE``. Sub-threshold patterns live here only;
    once the count reaches the threshold the skill is written directly into
    the unified Skill root (``%USERPROFILE%\\HandQ\\Skill\\<slug>\\SKILL.md`` on
    Windows) with ``enabled: false`` via ``SkillRegistry`` — there is no
    proposal row and ``mem_entries`` is never touched.

    Kept deliberately separate from ``mem_entries`` so one-off / building
    task patterns don't pollute the live memory table or its dedup index.
    Additive (new table only) — safe to (re)run on existing DBs.
    """
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_recurrence (
            fingerprint      TEXT PRIMARY KEY,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            category         TEXT,
            last_title       TEXT,
            first_seen       INTEGER NOT NULL,
            last_seen        INTEGER NOT NULL
        )"""
    )


async def _migration_skill_recurrence_centroid(conn) -> None:
    """v6: give ``skill_recurrence`` a semantic centroid.

    The recurrence counter originally clustered occurrences by an exact-match
    string fingerprint (normalized token set / ``verb:object``). Synonymous
    phrasings of the same task ("restart the nginx" / "bounce the web server")
    hash differently, so the counter fragmented and never reached
    ``SKILL_RECURRENCE_THRESHOLD`` — the dominant reason no skill ever minted.

    ``triage._apply_session_skill`` now clusters by embedding similarity instead
    (``store.bump_skill_recurrence_semantic``): a skill-worthy session joins the
    nearest existing cluster when cosine >= ``SKILL_RECURRENCE_TAU``. These three
    columns persist that cluster state:

      * ``centroid`` — running-mean vector of the cluster's members (float32
        bytes via ``embedding.vec_to_bytes``); NULL on rows created by the
        lexical fallback, which the semantic matcher simply skips.
      * ``provider`` / ``model`` — the embedding space the centroid lives in.
        Vectors are only ever compared within the same (provider, model) so a
        model swap can't corrupt distances (mirrors ``mem_embedding_cache``).

    Additive ``ADD COLUMN`` — safe on existing DBs; old rows keep NULLs and age
    out via the recurrence window. Appended as a fresh slot per the append-only
    rule (see ``_migration_v4``); never edit a shipped slot in place.
    """
    await conn.execute("ALTER TABLE skill_recurrence ADD COLUMN centroid BLOB")
    await conn.execute("ALTER TABLE skill_recurrence ADD COLUMN provider TEXT")
    await conn.execute("ALTER TABLE skill_recurrence ADD COLUMN model TEXT")


async def _migration_activity_arcs(conn) -> None:
    """v7: add obs_arcs table + obs_sessions.arc_id column.

    Activity arcs aggregate multiple obs_sessions into a single continuous
    work period (cut only by >= 20 min idle). They give SemanticExtractor
    enough cross-app context to distill genuine workflow patterns rather
    than operating on single-app fragments with <5% hit rate.
    """
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS obs_arcs (
            id              TEXT PRIMARY KEY,
            started_at      INTEGER NOT NULL,
            ended_at        INTEGER,
            session_count   INTEGER NOT NULL DEFAULT 0,
            frame_os        TEXT,
            frame_host      TEXT,
            semantic_status TEXT NOT NULL DEFAULT 'pending'
        )"""
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_arcs_status "
        "ON obs_arcs(semantic_status, ended_at)"
    )
    await conn.execute(
        "ALTER TABLE obs_sessions ADD COLUMN arc_id TEXT"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_sessions_arc "
        "ON obs_sessions(arc_id)"
    )


# Ordered list — index N -> migration applied as version (N+1). New migrations
# append; never edit the baseline in place once shipped.
MIGRATIONS: List[Callable[[object], Awaitable[None]]] = [
    _migration_baseline,                    # v1
    _migration_drop_session_triage_status,  # v2
    _migration_unique_session_extraction,   # v3
    _migration_v4,                          # v4 — retired no-op (see docstring)
    _migration_skill_recurrence,            # v5 — creates skill_recurrence
    _migration_skill_recurrence_centroid,   # v6 — adds semantic centroid columns
    _migration_activity_arcs,              # v7 — obs_arcs + obs_sessions.arc_id
]
