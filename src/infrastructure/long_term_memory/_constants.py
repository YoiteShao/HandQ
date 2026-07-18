"""Private tuning knobs for the long-term memory subsystem.

All parameters live here as module-level constants — NOT in handq_config.yaml.
The user-facing config exposes only LLM credentials and tool switches; LTM
tuning is an internal concern that should not be touched by end users (wrong
values silently degrade recall quality).

If a developer needs to override these for testing, edit this file directly.
We intentionally do not provide an env-var override path — the tuning surface
is expected to be stable across deployments.

Sections:
- 1. Embedding (which provider, which model, vector dims)
- 2. Reranker  (stage-3 cross-encoder; v1 disabled)
- 3. DreamWorker (background triage cadence, retry, batch size)
- 4. Recall    (k values, min_score thresholds, RRF k, kind / tag names)
- 5. PII       (always-on)
- 6. LLM tier  (which role pool the dream worker uses)
- 7. QGenie endpoint
- 8. Chunking
- 9. Candidate-status reason strings
"""
from __future__ import annotations

from enum import Enum


# ── 1. Embedding ────────────────────────────────────────────────────────────
#
# Provider-id constants. Use these (not raw strings) when comparing or
# branching on EMBEDDING_PROVIDER, so a typo would surface as a NameError
# rather than a silent fts_only fallback.
PROVIDER_FTS_ONLY: str = "fts_only"
PROVIDER_HTTP_API: str = "http_api"
PROVIDER_ONNX_LOCAL: str = "onnx_local"  # P2 placeholder

# Choice. See module docstring for tradeoffs.
EMBEDDING_PROVIDER: str = PROVIDER_HTTP_API

# Model identifier sent in `embeddings.create(model=...)`. Verified live
# against QGenie: `qgenie_embedd` resolves to ``Qwen/Qwen3-Embedding-0.6B``
# and returns 1024-dim vectors.
EMBEDDING_MODEL: str = "qgenie_embedd"

# Output vector dimensions. Qwen3-Embedding-0.6B = 1024.
EMBEDDING_DIMS: int = 1024

# Qwen3-Embedding is asymmetric: queries should be wrapped with an Instruct
# prefix while documents are embedded bare. Live testing showed instruct-
# prefixed queries widen the gap between the correct document and noise.
EMBEDDING_QUERY_INSTRUCTION: str = (
    "Given a user query, retrieve durable user preferences and reusable "
    "team/project knowledge that match the query"
)


# ── 2. Reranker (stage 3) ───────────────────────────────────────────────────
#
# Provider-id constants. Same rationale as embedding ids.
RERANKER_NOOP: str = "noop"
RERANKER_LLM: str = "llm"                    # LLM-as-reranker (uses helper pool)
RERANKER_CROSS_ENCODER: str = "cross_encoder_onnx"  # future placeholder

# Default: LLM rerank enabled at the RERANKER level; individual call sites
# (e.g. INTENT) opt out via format_context_block(rerank=False) to avoid
# 1 extra LLM call per user message.
RERANKER_PROVIDER: str = RERANKER_LLM

# Cap how many candidates we send to the rerank LLM. Beyond ~20 the
# scoring quality degrades (LLM gets overwhelmed) and the prompt grows.
# We over-fetch from stage 1 to ~k*3 so RERANK_INPUT_LIMIT >= that.
RERANKER_INPUT_LIMIT: int = 15

# Per-call timeout. The rerank prompt is short (one query + ~15 short
# summaries), so the LLM should respond fast — but the chat hot-path
# may still take a few seconds.
RERANKER_TIMEOUT_SECONDS: float = 30.0

# Ceiling on PersistentAgent's per-item recall call as a whole (BM25 + dense
# scan, rerank off at fast-tier — see RECALL_MIN_SCORE_FAST below). This is
# NOT the reranker's own timeout (that only fires when rerank=True, which
# fast-tier never is); it exists because the BM25/dense path itself has no
# per-call timeout of its own, so a stuck SQLite read (e.g. lock contention
# with the background DreamWorker) would otherwise block item startup
# indefinitely. On timeout, recall degrades to "no LTM context this item" —
# same behavior as any other recall failure, never a task failure.
RECALL_ITEM_TIMEOUT_SECONDS: float = 8.0


# ── 2b. Tiered recall (rerank policy per call site) ─────────────────────────
#
# Rerank is expensive (3-8s LLM call, 10+s with fallback retry). Prod profiling
# showed it dominates recall latency on chat-turn hot paths where the user is
# actively waiting. FAST tier skips rerank entirely (RRF+recency ordering
# only) for exactly that hot path. PRECISE tier runs rerank=True concurrently
# alongside the FAST-tier INTENT call (not on its critical path — see
# Orchestrator._gather_context_sections) so a queued task's Agent execution
# starts with a rerank-quality LTM block without the user waiting for it.
class RecallTier(str, Enum):
    FAST = "fast"        # no rerank, sub-second latency, chat-turn hot path
    PRECISE = "precise"  # rerank=True, run concurrently off the INTENT critical path



# ── 3. DreamWorker ──────────────────────────────────────────────────────────

DREAM_INTERVAL_SECONDS: float = 60.0
DREAM_BATCH_SIZE: int = 8
DREAM_MAX_RETRY: int = 5
DREAM_STUCK_SECONDS: int = 300

# How many batched candidates the dream worker triages concurrently per
# cycle. Each _triage_one is a helper-LLM round-trip + a few SQLite
# writes; serially that's ~5-30s per candidate, so a bursty 8-candidate
# batch could tie up the worker for minutes. Capping concurrent fan-out
# at 3 cuts batch time without overwhelming the helper LLM's per-key
# rate limit (most helper-tier providers tolerate single-digit
# concurrency comfortably).
DREAM_TRIAGE_CONCURRENCY: int = 3

# Adaptive idle backoff. The worker's loop interval doubles on every
# empty cycle (no pending candidates) up to ``DREAM_INTERVAL_MAX_SEC``,
# and snaps back to ``DREAM_INTERVAL_MIN_SEC`` the moment work appears.
#
# The 60s floor is a HARD contract — even when a fresh candidate has
# just been written, the worker waits at least one full minute before
# triaging. Triage is a background job; the user-visible flow has
# already returned. Capping wakeups at one-per-minute also avoids
# burst-of-submits → burst-of-LLM-calls amplification.
#
# Rationale for the 1h ceiling: HandQ is interactive, not a 24/7 daemon.
# A 60s poll on a laptop that's been idle for 2 hours produces 120
# wakeups for zero work. Backing off to a 1h ceiling cuts wakeups by
# 60x in that case while still giving any newly-written candidate a
# bounded delay.
DREAM_INTERVAL_MIN_SEC: float = 60.0
DREAM_INTERVAL_MAX_SEC: float = 3600.0

# How many missing-embedding chunks the dream worker backfills:
#   - DREAM_BACKFILL_STARTUP : on first loop iteration (catch up legacy data)
#   - DREAM_BACKFILL_CYCLE   : on every subsequent cycle (steady-state trickle)
#
# CYCLE was 10 originally; raised to 50 after a real-world incident where
# a multi-hour QGenie outage left ~30% of new chunks without embeddings,
# and 10/cycle could not catch up before the worker backed off to long
# idle intervals. 50 still fits in one /v1/embeddings batch round-trip.
DREAM_BACKFILL_STARTUP: int = 50
DREAM_BACKFILL_CYCLE: int = 50

# Sleep-on-error before resuming the main loop. Higher than the normal
# interval so a persistent failure doesn't spam logs.
DREAM_ERROR_SLEEP_SECONDS: int = 30

# Delay between bridge boot and the first embedding warmup. On a cold-
# booted laptop the corporate VPN / proxy / DNS often need 30-60s to
# come up; firing the warmup batch at boot+2s used to burn ~3.5min of
# retries against ConnectError before the network was reachable. A short
# upfront sleep gives the network a quiet window and turns "every cold
# boot logs an ERROR" into "first tick succeeds cleanly".
DREAM_STARTUP_DELAY_SECONDS: int = 30

# ── Merge / exact-group scanner ──────────────────────────────────────────
#
# Periodic post-hoc dedup pass: find pairs of entries whose chunk
# embeddings are too close to coexist. Runs as a DreamWorker job; brute-
# force pairwise cosine over all cached embeddings per kind.
#
# Why we need it (when triage already does pre-insert dedup):
#   - BM25 misses cross-language duplicates (existing-FTS in triage uses
#     BM25, so "代码 lint" + "ruff check" both pass triage).
#   - Different sessions on different days about the same project fact
#     produce two independent entries; triage on day-2 doesn't see day-1
#     unless BM25 happens to match.
#
# Two thresholds, three outcomes:
#   - EXACT  (>= MERGE_EXACT_THRESHOLD)   : auto-merge, archive older
#   - LLM    ([0.85, 0.90))               : helper-LLM arbiter decides
#                                            merge vs keep-distinct; verdict
#                                            persisted so the pair is never
#                                            re-judged (status merged /
#                                            kept_distinct). With no helper
#                                            LLM, falls back to a pending
#                                            review proposal (no signal lost).
#   - KEEP   (< 0.85)                     : both entries kept, nothing written
# A drop is also suppressed when the older (archived) entry was recalled
# within CORRECTION_RECALL_PRIORITY_DAYS — an actively-used entry is
# load-bearing, so keep both (the check re-runs each scan and expires
# naturally with the recall window).
MERGE_SCAN_EVERY_N_CYCLES: int = 15         # 15 × 60s = once per 15min
LTM_CLEANUP_EVERY_N_CYCLES: int = 100      # ~100 × 60s min = once per ~1.7h
LTM_CANDIDATE_RAWTEXT_TTL_DAYS: int = 90
LTM_RECALL_LOG_TTL_DAYS: int = 90
# Orphaned-proposal TTL. mem_correction_proposals rows left in status
# 'pending' are never consumed — HandQ is conversation-as-interface and has
# no review UI for correction/merge proposals. A 'pending' row therefore only
# accumulates. The cleanup pass DELETEs pending rows older than this; terminal
# rows (merged / kept_distinct / applied / stale) are the load-bearing
# decided-memo + audit trail and are never swept.
LTM_CORRECTION_PROPOSAL_TTL_DAYS: int = 30
LTM_OBS_SNAPSHOT_TTL_DAYS: int = 7       # raw frames; captured_at is unix MS
LTM_OBS_EVENT_TTL_DAYS: int = 7          # state-change stream; occurred_at unix MS
LTM_OBS_PIPELINE_RUN_TTL_DAYS: int = 30  # triage audit ledger; started_at seconds
# Intermediate observation rows. obs_sessions + obs_semantic_events are the
# scratch layer between raw snapshots and durable mem_entries: the aggregator
# groups snapshots into a session, the extractor abstracts it into a semantic
# event, triage distills the value into mem_entries. Once a session is fully
# processed (semantic_status done/skipped) and its event triaged
# (accepted_entries set), the intermediate rows are pure audit — the durable
# distillation already lives in mem_entries and the snapshots they reference
# are pruned at LTM_OBS_SNAPSHOT_TTL_DAYS. The cleanup pass sweeps ONLY
# fully-processed rows, so a slow/stalled pipeline never loses unprocessed
# work. obs_sessions.ended_at is unix MS; obs_semantic_events.extracted_at
# is seconds.
LTM_OBS_SESSION_TTL_DAYS: int = 30
LTM_OBS_SEMANTIC_EVENT_TTL_DAYS: int = 30
# A completed-task skill is only minted once its pattern RECURS this many
# times. The triage skill pass (_apply_session_skill) runs the extractor on
# every successful SESSION_COMPLETE, clusters by the LLM-normalized
# skill_fingerprint, and bumps skill_recurrence; only when the count reaches
# this threshold does it write a live (disabled) SKILL.md under the Skill root
# via SkillRegistry. One-off tasks stay below it forever and never surface a
# skill — which is the whole point: skills come from repeated, automatable
# workflows, not single runs.
SKILL_RECURRENCE_THRESHOLD: int = 3
# Bypass valve for SKILL_RECURRENCE_THRESHOLD: a session whose step_count (see
# Candidate.metadata, set by candidates.submit_session_complete) reaches this
# many steps is treated as high-complexity enough that the workflow is worth
# capturing (disabled, pending review) even the FIRST time it's seen — the
# recurrence gate exists to avoid minting skills from trivial one-offs, but a
# genuinely elaborate multi-step trajectory is not trivial just because it
# hasn't recurred yet, and fingerprint fragmentation (wording drift splitting
# the recurrence counter) means many valuable one-off procedures would
# otherwise never reach the threshold at all.
SKILL_HIGH_COMPLEXITY_STEP_COUNT: int = 15
# Recurrences only count as the "same habit" when they happen within this
# rolling window. When a new occurrence lands more than this many days after
# the previous one, bump_skill_recurrence RESETS the counter to 1 (a fresh
# streak) instead of accumulating. Without this, a task done 3 times over a
# year would eventually surface as a "skill" even though it isn't a current,
# repeated workflow — the counter is a lifetime tally otherwise. 45 days keeps
# the meaning of "recurring" honest and aligns with the proactive-engine TTL.
SKILL_RECURRENCE_WINDOW_DAYS: int = 45
# Cosine bar for the SEMANTIC recurrence clusterer (bump_skill_recurrence_semantic).
# A new skill-worthy session joins the nearest existing cluster when their
# embeddings are at least this similar; otherwise it starts a new cluster. This
# replaces exact-string fingerprint matching so trivial wording drift ("restart
# nginx" vs "bounce the web server") no longer fragments the recurrence counter
# — the dominant reason skills never minted. Anchored to the proven
# DREAM_L2_CLUSTER_THRESHOLD (0.55) used for memory synthesis on the same
# embedder, nudged up because a skill's name+description is shorter and more
# focused than a memory chunk. Tune via the nearest-cosine logging the bump emits.
SKILL_RECURRENCE_TAU: float = 0.60
MERGE_EXACT_THRESHOLD: float = 0.90          # auto-merge bar
MERGE_LLM_GATE_THRESHOLD: float = 0.85       # [0.85, 0.90) → helper-LLM arbiter
MERGE_LLM_MAX_PAIRS_PER_SCAN: int = 5        # cap helper-LLM merge calls per full scan
MERGE_MIN_PAIR_AGE_SECONDS: int = 300        # don't dedup same-batch entries


# ── Dream synthesis (L2 / L3) ────────────────────────────────────────────
#
# Modeled after yansu's L1/L2/L3:
#   L1 : raw triage candidate → entry      (every cycle, already implemented)
#   L2 : cluster of L1 entries → pattern   (here)
#   L3 : cluster of L2 patterns → meta-insight  (here)
#
# Both run as periodic DreamWorker jobs. Frequency is tuned to give the
# LLM enough new material to find real patterns without re-extracting
# the same things every run.

# Run L2 every Nth main-loop cycle (cycle = DREAM_INTERVAL_SECONDS).
# 1440 × 60s = once per day. Yansu's "[dream] skipping L2/L3 ..." string
# implies it has gating logic too; we keep ours simple.
DREAM_L2_EVERY_N_CYCLES: int = 1440
# Run L3 less often — it operates on L2 patterns, which themselves change
# slowly. Once a week is plenty.
DREAM_L3_EVERY_N_CYCLES: int = 1440 * 7

# ── Idle-aware synthesis gating ──────────────────────────────────────────
#
# L2/L3 synthesis is CPU + LLM intensive (embedding comparisons + helper
# LLM calls per cluster). Running it while the user is active competes
# for both network bandwidth (LLM quota) and CPU (cosine pairwise). The
# idle gate defers synthesis until the user steps away — the same approach
# used for OCR drain (ACTIVITY_OCR_GATE_INPUT_IDLE_SEC).
#
# Three conditions must all be satisfied for synthesis to fire:
#   1. Wall-clock cadence met (DREAM_L2_EVERY_N_CYCLES, existing)
#   2. System input idle ≥ DREAM_SYNTHESIS_IDLE_SEC
#   3. Material gate: ≥ DREAM_SYNTHESIS_MIN_NEW_ENTRIES new source entries
#      since the last successful run
#
# Hard fallback: if the user NEVER goes idle long enough (power user who
# works 12h then shuts down), force synthesis after DREAM_SYNTHESIS_FORCE_
# AFTER_SEC regardless of idle state — otherwise L2/L3 never fire and
# memory grows unbounded without compression.

DREAM_SYNTHESIS_IDLE_SEC: float = 300.0
# ^ 5 minutes of input idle. Matches the WARM→COLD tier transition —
#   if the user is "cold" they won't notice background LLM work.

DREAM_SYNTHESIS_FORCE_AFTER_SEC: int = 7 * 86400
# ^ 7 days hard cap. If L2 hasn't run in a week (user never idles 5min
#   or bridge keeps restarting), just run it on the next eligible cycle.

DREAM_L2_MIN_NEW_ENTRIES: int = 5
# ^ Don't bother clustering if < 5 new L1 entries accumulated. Avoids
#   wasting an LLM call on too-small clusters that can't meet MIN_CLUSTER_SIZE.

DREAM_L3_MIN_NEW_ENTRIES: int = 3
# ^ L3 operates on L2 patterns which are already sparse. 3 new patterns
#   is a reasonable threshold before attempting meta-synthesis.

# Look-back window for "recent" entries fed into L2. We re-cluster the
# whole window every run — old entries can join new clusters as the
# corpus grows. 30 days balances signal collection vs prompt size.
DREAM_L2_WINDOW_SECONDS: int = 60 * 60 * 24 * 30        # 30 days
DREAM_L3_WINDOW_SECONDS: int = 60 * 60 * 24 * 90        # 90 days

# Greedy clustering thresholds. Lower threshold = larger / fuzzier clusters.
# 0.55 was chosen empirically from yansu's similarity-threshold heuristics
# (its merge bar is ~0.95 for exact, 0.85 for propose; pattern clusters
# are intentionally looser — we want "same theme" not "same content").
DREAM_L2_CLUSTER_THRESHOLD: float = 0.55
DREAM_L3_CLUSTER_THRESHOLD: float = 0.35    # patterns are already
                                            # synthesised, looser still

# A cluster needs at least this many members for the LLM to even consider
# extracting a pattern. Below that, "pattern" is just an artefact.
DREAM_L2_MIN_CLUSTER_SIZE: int = 3
DREAM_L3_MIN_CLUSTER_SIZE: int = 3

# Cap how many entries we feed to one L2/L3 LLM call. Past 12 the prompt
# gets noisy and the LLM struggles to find the central theme. We also
# slice clusters that exceed this so LLM cost stays bounded.
DREAM_L2_MAX_CLUSTER_SIZE: int = 12
DREAM_L3_MAX_CLUSTER_SIZE: int = 12

# Cap how many clusters one dream run processes. At HandQ's high-frequency
# user pace this caps L2 cost at ~30 LLM calls/day (~$0.15 amortised).
DREAM_L2_MAX_CLUSTERS_PER_RUN: int = 30
DREAM_L3_MAX_CLUSTERS_PER_RUN: int = 10


# ── 4. Recall ───────────────────────────────────────────────────────────────

RECALL_MEMORY_K: int = 5
RECALL_KNOWLEDGE_K: int = 5
# Dense-branch cosine floor. A coarse pre-filter that drops the obviously
# unrelated long tail of the brute-force cosine sweep before fusion. Kept
# the cosine pre-filter floor. Qwen3-Embedding clusters unrelated text around
# 0.2–0.35, and activity-snapshot noise lands in the 0.34–0.41 band, so 0.25
# was too permissive: same-domain-but-irrelevant rows slipped through into
# recall. 0.35 trims that noise band while the rerank gate below remains the
# authoritative relevance cutoff. Was 0.0 → 0.25 → 0.35.
#
# ``RECALL_MIN_SCORE`` still applies to rerank-on callers, where the rerank
# LLM enforces the final relevance decision at ``RECALL_RERANK_MIN_
# SCORE=0.30``. FAST tier callers (INTENT, PersistentAgent per-item) DO NOT
# rerank — without a final gate, top-K by RRF would surface the 0.34-0.41
# activity-snapshot band as "least bad" matches on chat turns even when the
# query has no real match in the corpus. FAST tier callers must pass
# ``min_score=RECALL_MIN_SCORE_FAST`` explicitly to close that hole. Empty
# recall is strictly better than misleading recall on the chat hot path.
RECALL_MIN_SCORE: float = 0.35
RECALL_MIN_SCORE_FAST: float = 0.45

# FAST tier post-fusion cosine floor.
#
# ``RECALL_MIN_SCORE_FAST=0.45`` above is the PRE-fusion dense-branch cosine
# pre-filter — it drops entries whose embedding is obviously unrelated. But
# entries in the 0.45-0.49 band still pass the pre-filter, get boosted by RRF
# when BM25 also picks them up, and land in top-K without earning it.
#
# Observed in the 4-role live test (test_recall_quality_live.py): the noise
# entry "User reviews config in VS Code" surfaced at rank 3-4 for a
# ruff/pytest chat query — its dense cosine sat above 0.45 but well below any
# real match. In prod with 172 activity-tinted entries the tail-noise ratio is
# far worse.
#
# This gate runs AFTER RRF fusion on the same display_score the pre-filter
# used (dense cosine). Rows below the floor are dropped even if RRF ranked
# them high. Only applied on the FAST tier (rerank=False path) — QUALITY tier
# lets the rerank LLM's ``RECALL_RERANK_MIN_SCORE`` be the authoritative
# cutoff instead.
#
# BM25-only hits (display_score=None) are ALSO dropped on FAST tier — see
# ``_fast_gate`` docstring for the noise-heavy-corpus rationale (BM25-only
# matches in a low-quality corpus are dominated by coincidental keyword
# overlap; the rare real exact-keyword match also usually produces a decent
# dense cosine so nothing precise is actually lost).
RECALL_FAST_POST_FUSION_MIN_SCORE: float = 0.50
# Stage-3 rerank gate. The LLM reranker scores 0.0–1.0 and its own system
# prompt declares sub-0.3 candidates to be noise (reranker.py:_LLM_RERANK_SYSTEM);
# this is the consumer that enforces that contract. Rows scoring below this
# after rerank are dropped entirely (recall may legitimately return nothing).
RECALL_RERANK_MIN_SCORE: float = 0.30
RECALL_FTS_OVERFETCH: int = 3

# Recency half-life (days) for recall ordering. After the relevance gate, the
# surviving rows are re-sorted by ``relevance * 0.5 ** (age_days / halflife)``
# so fresh memories win the limited recall slots over equally-relevant stale
# ones. Ordering ONLY — the relevance gate (RECALL_RERANK_MIN_SCORE) still runs
# on the raw score, so a high-relevance old memory (e.g. a durable preference)
# is never culled just for being old. 45d ≈ a memory needs ~2x relevance to
# tie a fresh one after ~1.5 months.
RECALL_DECAY_HALFLIFE_DAYS: float = 45.0

# ── Identity ───────────────────────────────────────────────────────────────
IDENTITY_MAX_ENTRIES: int = 20
IDENTITY_TRIAGE_EXISTING_LIMIT: int = 20

# IDENTITY entries change only when the async dream worker accepts an
# identity-tagged candidate (cadence floor = DREAM_INTERVAL_MIN_SEC = 60s).
# Aligning this cache TTL to that cadence guarantees worst-case staleness of
# exactly one dream tick — no need for a manual invalidation hook beyond the
# explicit archive() path. Process-level cache on the LongTermMemory singleton.
IDENTITY_CACHE_TTL_SEC: float = 60.0

# ── Known entities (principal graph) ─────────────────────────────────────────
# Cap for the <known-entities> block rendered into the context. list_principals
# orders last_seen DESC, so this keeps the 20 most-recently-seen principals.
# Reuses IDENTITY_CACHE_TTL_SEC for its render cache (both change slowly).
KNOWN_ENTITIES_MAX: int = 20

# Reciprocal-rank-fusion constant (Cormack et al. 2009). Lower = top ranks
# dominate more. 60 is the literature default; values in [40, 80] all behave
# similarly in practice.
RRF_K: int = 60

# Note: kind discriminators (KIND_MEMORY etc.) live as ``EntryKind`` in
# :mod:`models` — not duplicated here. Use ``EntryKind.MEMORY.value`` when
# you need the raw string.


# ── 5. PII ──────────────────────────────────────────────────────────────────

PII_ENABLED: bool = True


# ── 6. LLM pool ─────────────────────────────────────────────────────────────
#
# Resolved at runtime from ``llm.helper_models`` / ``llm.models`` in the user
# config (see :mod:`infrastructure.role_resolver`). LTM has no separate tier
# knob — both the dream-worker triage pool and the retriage pool are built
# from those two YAML lists by their respective callers.


# ── 7. QGenie endpoint ──────────────────────────────────────────────────────

QGENIE_BASE_URL: str = "https://qgenie-api.qualcomm.com/v1"
QGENIE_VERIFY_SSL: bool = False

# Per-request timeout for embedding calls. Live-tested: cold-start
# qgenie_embedd takes 30–45s for the first request; bumping to 90 covers
# that and any 1-second jitter without making real failures hang too long.
EMBEDDING_TIMEOUT_SECONDS: float = 90.0


# ── 8. Chunking ─────────────────────────────────────────────────────────────

# Hard upper bound on the size of a single chunk. Beyond this, content is
# split first on H2 boundaries, then on paragraph boundaries. 800 chars
# matches yansu's empirical setting and keeps embeddings semantically
# focused (one section ≈ one idea).
CHUNK_MAX_CHARS: int = 800


# ── 9. Candidate-status reason strings ──────────────────────────────────────
#
# These end up in memory_candidates.reason and are surfaced in admin /
# debug views. Centralising them so we don't typo "sensitve_pre_filter"
# and quietly diverge from the prompt or test expectations.
#
# Note: archive reasons (user_request, auto_dedup, superseded, ...) live as
# :class:`models.ArchiveReason` enum — they describe persistent entry state
# and benefit from enum type-safety. The strings here describe *transient*
# triage outcomes that only show up in candidate.reason.
REASON_SENSITIVE_PRE: str = "sensitive_pre_filter"
REASON_POST_FILTER_MEMORY: str = "post_filter:memory"
REASON_POST_FILTER_KNOWLEDGE: str = "post_filter:knowledge"
REASON_GUARD_FAILED_NO_MEMORY: str = "guard:failed_no_agentic_memory"
REASON_MAX_RETRY: str = "max_retry"
REASON_TRIVIAL_SESSION: str = "trivial_session_skipped"


# ── 10. Trivial-session pre-filter ──────────────────────────────────────────
#
# session_complete candidates that look like one-off / trivial tasks are
# auto-rejected at submit time, before the LLM ever sees them. Saves cost
# AND prevents the LTM from accumulating "fixed typo in line 42" noise that
# would dilute genuine project knowledge.
#
# We use ONE structural signal — the count of completed steps that produced
# substantive output (artifacts, factual_outcome, or key_findings). Goal-text
# patterns and summary-length thresholds were tried and rejected as too
# fragile (English-specific; gameable; trip on legitimate short summaries).
# Step count is language-independent and reflects actual work delivered.
#
# Penalty for a false positive (rejecting a session that did have value):
# silently dropping it. Penalty for a false negative (letting noise through):
# the LLM stage rejects it (Example 10 in the triage prompt). Both are
# acceptable, so we tune the bar conservatively.

# A session must have at least this many completed steps with structured
# output to pass. 0–2 step sessions are almost always trivial (typo fix,
# rename, single-file edit).
TRIVIAL_MIN_STEP_COUNT: int = 3

# Multi-signal scoring threshold for the trivial-session pre-filter.
# A session passes if its weighted score reaches this value. The
# weighting (see candidates._is_trivial_session) gives more credit to
# write/edit steps and to distinct artifacts than to read/search work,
# because the LTM-worthy signal almost always lives in what was
# *produced* rather than what was *read*. 3.5 is calibrated so:
#   - 1 write + 1 artifact + 200-char outcome → 2.9   (REJECT, trivial)
#   - 1 write + 3 artifacts                   → 3.7   (KEEP, multi-file work)
#   - 2 writes + 2 artifacts + 1KB outcome    → ~7    (KEEP)
#   - 5 search/read steps, no writes          → 1.5   (REJECT)
TRIVIAL_SCORE_THRESHOLD: float = 3.5


# ── 10b. /remember verbatim threshold ────────────────────────────────────
#
# /remember candidates whose user payload exceeds this character count
# bypass the triage LLM entirely and are inserted verbatim into memory.
# The motivation: long experiential content (a detailed sequential
# procedure, a multi-step debugging recipe) MUST be preserved word-for-
# word — letting the helper LLM rewrite it through the 2000-char
# memory_content rule loses the fine-grained detail the user wanted to
# keep.
#
# Below the threshold, the normal triage path is preserved because the
# LLM's cleanup (concise summary, dimension assignment, dedup against
# existing entries) is genuinely valuable for short prefs/facts.
#
# 800 chars matches CHUNK_MAX_CHARS so a freshly-bypassed entry chunks
# cleanly into ~1+ chunks at the natural boundary.
MANUAL_REMEMBER_VERBATIM_THRESHOLD: int = 800

# Root subdirectory under %USERPROFILE%\HandQ\ that holds every
# personalization-related artifact. Three things live here so the user
# sees one cohesive "this is what HandQ has learned about me" folder:
#
#   personality\
#     memory.db          (LTM SQLite — long-term memory + knowledge)
#     memory_notes\      (long /remember .md mirrors)
#     ephemeral\         (PersonalityMonitor screenshots — written and
#                         unlinked sub-second; almost always empty)
#
# The Scheduler's ``scheduled_tasks.json`` deliberately stays one
# level up (it's recurring task automation, not personalization).
PERSONALITY_DATA_DIR: str = "personality"

# When the verbatim path fires, we ALSO write the user's text out to a
# markdown file under %USERPROFILE%\HandQ\personality\memory_notes\.
# The DB remains the source of truth for recall (chunks + embeddings
# live there); the file is for user-side ergonomics:
#   - editor-friendly: open in VSCode / Notepad / whatever
#   - backup-friendly: drop the folder into git, OneDrive, anything
#   - inspection-friendly: ``ls`` shows what HandQ has actually
#     committed to long-term memory
#
# The file is a one-shot mirror at insert time. Editing the file later
# does NOT update the DB (no watcher, no re-index) — that path is
# reserved for a future "managed notes folder" feature.
MANUAL_REMEMBER_MIRROR_DIR: str = "memory_notes"

# Strict indexing criteria for verbatim path. We only allow long
# /remember to bypass the LLM and become a permanent injectable entry
# when the content looks structured enough to benefit from the
# preservation. Otherwise the LLM should still mediate (forcing the
# user to either rewrite it or break it into shorter pieces).
#
# A "structured" payload satisfies AT LEAST ONE of:
#   - has >= 1 markdown H2 / H3 header line (## or ###)
#   - has >= 3 bullet / numbered list items
# Falling back to short-form triage is a feature, not a bug — it
# protects against accidentally archiving a ramble as immutable.
VERBATIM_MIN_STRUCTURE_HEADERS: int = 1
VERBATIM_MIN_LIST_ITEMS: int = 3


# ── 11. Activity Monitor (daily / desktop activity capture) ────────────────────
#
# These knobs drive ``src/infrastructure/activity_monitor`` — the per-monitor
# adaptive screen-sampler that feeds observations into the LTM pipeline
# (obs_snapshots + obs_ocr_frames). They live here (not in a separate
# config or YAML) because:
#
#   * The cadence is tightly coupled to LTM behaviour — too aggressive and
#     the dream worker drowns; too slow and we miss everything.
#   * Wrong values silently degrade quality / disk pressure / CPU; not a
#     surface end users should touch by hand.
#   * Keeping the surface in ONE constants file makes "what's tunable for
#     the long-term memory subsystem?" answerable by `ls`.
#
# The activity_monitor package imports only from this file (plus models /
# candidates) — no yaml lookups in the hot path.

# Enable/disable the whole subsystem. OFF by default because activity capture
# is privacy-sensitive; the user (or a future Settings UI) flips it on. We
# still let users disable mid-session by sending a `activity_monitor_pause`
# IPC envelope.
ACTIVITY_MONITOR_ENABLED: bool = True

# ── Adaptive sampling tiers (per monitor) ──────────────────────────────────
#
# Each monitor runs its own state machine. The state determines the cadence:
#
#   HOT      — user input or visible content change in the last
#              ACTIVITY_HOT_RECENCY_SEC. Sample every HOT_INTERVAL_SEC.
#   WARM     — activity within WARM_RECENCY_SEC but not HOT. Sample slower.
#   COLD     — activity within COLD_RECENCY_SEC. Sample much slower.
#   DORMANT  — no activity past COLD_RECENCY_SEC. Stop sampling content;
#              just probe occasionally so we notice when the monitor wakes
#              up. This is critical for multi-monitor setups where one
#              screen sits idle most of the day.
#
# We deliberately use input-driven recency rather than a fixed cron — a
# user who doesn't touch the keyboard for 2 hours does NOT need 2400
# screenshots. A user who power-codes for 10 minutes DOES need that
# burst captured.
ACTIVITY_TIER_HOT_INTERVAL_SEC: float = 8.0
ACTIVITY_TIER_WARM_INTERVAL_SEC: float = 60.0
ACTIVITY_TIER_COLD_INTERVAL_SEC: float = 300.0
ACTIVITY_TIER_DORMANT_INTERVAL_SEC: float = 300.0

ACTIVITY_HOT_RECENCY_SEC: int = 30
ACTIVITY_WARM_RECENCY_SEC: int = 300
ACTIVITY_COLD_RECENCY_SEC: int = 1800

# Global system-idle floor. If GetLastInputInfo says the user hasn't touched
# any input device for this long, every monitor goes DORMANT regardless of
# what's on its screen — there's nobody to observe, no point burning CPU.
ACTIVITY_GLOBAL_IDLE_PAUSE_SEC: int = 1800

# Smoothing. We don't want a single jitter to ping-pong tiers. The state
# machine demands ``ACTIVITY_TIER_DEMOTE_GRACE_SEC`` of quiet before
# downgrading; promotion is immediate (a click is a click).
ACTIVITY_TIER_DEMOTE_GRACE_SEC: int = 15

# ── Frame de-duplication ───────────────────────────────────────────────────
#
# Most consecutive screenshots on a given monitor are identical (user is
# reading, idle cursor on document). We hash a tiny downsample of each
# capture; if the hash matches the previous one within a Hamming bound,
# we skip OCR entirely AND delete the file immediately. This is the main
# cost-saver — real burn happens at OCR-call latency, not capture latency.
ACTIVITY_FRAME_HASH_DOWNSAMPLE_PX: int = 16          # 16x16 perceptual hash
ACTIVITY_FRAME_HASH_DELTA_THRESHOLD: int = 12        # Hamming distance bar

# ── OCR / interesting-frame detection ──────────────────────────────────────
#
# After OCR, we decide whether to forward the sample to LTM. Three filters:
#   1. text length too short → likely UI chrome only, skip
#   2. text identical (Jaccard) to last forwarded sample → skip (still
#      same window/content)
#   3. otherwise → write to obs_snapshots + obs_ocr_frames
#
# ACTIVITY_OCR_MIN_CHARS was 40 originally, which passed dialog title bars
# ("Save As", "File Edit View Help Tools Options") into the snapshot pipeline.
# Even after downstream generic-observation / worth_storing guards, those
# tiny snapshots still burned an extraction LLM call per aggregated session.
# Raised to 100 chars — that's a small paragraph, enough to carry an actual
# insight. Real-content sessions blow past 100 chars in one screen; only
# chrome-only captures are filtered out. Trade-off (loses legit short
# observations) is acceptable in noise-heavy-corpus mode.
ACTIVITY_OCR_MIN_CHARS: int = 100
ACTIVITY_OCR_TEXT_JACCARD_BAR: float = 0.65  # >= bar means "same screen"
ACTIVITY_OCR_EXCERPT_MAX_CHARS: int = 600    # how much OCR text we keep

# Per-monitor history of accepted OCR texts. Used by the dedup gate so a
# brief alt-tab to a different window doesn't make the original screen
# look "novel" again on its next capture (which would re-forward an
# already-known window into LTM as a fresh observation).
# Set to 8 so a typical Mon→Tue alt-tab pattern stays inside the ring;
# bigger values suppress legitimate context shifts.
ACTIVITY_TEXT_HISTORY_SIZE: int = 8

# ── Disk hygiene ──────────────────────────────────────────────────────────
#
# We aim for ZERO accumulating PNG files. Each capture writes to a single
# rotating filename per monitor; OCR runs against it; the file is unlinked
# immediately after. The activity tier of ScreenshotStore acts as a backstop
# (LRU + age cap) for the case where ACTIVITY_KEEP_FRAME_FILES is flipped on
# for debugging. Values live here (not handq_config.yaml) — this is a
# debug-only knob, not a user-facing tuning surface.
#
# Setting ACTIVITY_KEEP_FRAME_FILES to False (the default) makes the
# activity_monitor unlink each frame the moment OCR returns. Flip to True
# only for debugging — it's a privacy footgun in production.
ACTIVITY_KEEP_FRAME_FILES: bool = False
ACTIVITY_SCREENSHOT_MAX_FILES: int = 100
ACTIVITY_SCREENSHOT_MAX_AGE_DAYS: float = 1.0

# ── §11.7 OCR deferral (defer-when-busy) ──────────────────────────────────
#
# Inline OCR was the dominant cause of "HandQ makes my whole machine slow":
# RapidOCR burns ~7-8 cores for ~2.5s per call, and on multi-monitor setups
# concurrent OCR pegged all cores + spiked RSS to ~1.4 GB. The fix is to
# capture into a per-monitor in-memory ring during user activity and only
# fire OCR when a global "user idle" gate opens.
#
# Gate opens on EITHER:
#   1. Session locked (Win+L / auto-lock / screensaver).
#   2. Keyboard/mouse idle ≥ ACTIVITY_OCR_GATE_INPUT_IDLE_SEC AND every
#      monitor has been visually quiet ≥ ACTIVITY_OCR_GATE_SCREEN_QUIET_SEC.
# Two-signal soft-idle path so we don't OCR while a video plays unattended
# (input idle but screen still changing — would compete with hardware
# decode and stutter playback).

ACTIVITY_OCR_GATE_INPUT_IDLE_SEC: float = 60.0
ACTIVITY_OCR_GATE_SCREEN_QUIET_SEC: float = 30.0

# Per-monitor in-memory ring buffer for deferred frames. Stores JPEG-encoded
# bytes (~300 KB / frame at quality=85) so the worst-case ceiling stays
# bounded. 128 × 300 KB × 3 monitors ≈ 115 MB ceiling; typical busy-period
# occupation is single-digit MB because perceptual_hash dedup drops most
# captures before they reach the ring. Quality=85 chosen over 70 to keep
# OCR accuracy close to lossless on small UI text — the extra ~100 KB / frame
# is paid in encode time (~12 ms vs 10 ms) and disk write speed (negligible
# on SSD); files stay AV-friendly (well below 1 MB).
ACTIVITY_RING_MAXLEN: int = 128
ACTIVITY_RING_JPEG_QUALITY: int = 85

# Drain worker poll cadence. While the gate is closed (user busy) the worker
# wakes every ACTIVITY_OCR_DRAIN_POLL_SEC to re-check; while open it pulls
# entries as fast as OCR completes (Semaphore(1) caps at one OCR at a time).
ACTIVITY_OCR_DRAIN_POLL_SEC: float = 1.0

# ── §11.7.2 OCR drain thread budget ────────────────────────────────────────
#
# ONNX Runtime defaults its thread pool to physical-core count, which lets a
# single RapidOCR call peg 7-8 cores for ~2.5s. That latency is fine for the
# interactive desktop_tool path (one-shot, user is waiting on a click target),
# but it's wrong for the OCR drain: when the IDLE gate opens, drain pulls
# back-to-back frames from the ring and consumes the entire CPU for 5-10
# minutes after the user steps away. Worse, any in-flight call cannot be
# cancelled, so a user who returns mid-drain perceives a ~2.5s "stuck" tail
# before the next gate re-check shuts things down.
#
# We give the drain its own LocalOCR instance with these thread caps. The
# interactive path (desktop_tool) keeps using the full-fat singleton so
# find_element latency is unaffected. Trade-off for the drain on a typical
# 8-physical-core box: per-frame OCR goes from ~3.4s (default) to ~3.6s
# (intra=4) — measured against a representative 1920x1080 text-dense frame.
# The 4-thread cap leaves 4 physical cores free for the user; CNN ops
# saturate hard past 4 threads, so dropping to 2 cost an extra ~1.3s/frame
# for almost no UX gain (mouse/foreground stayed responsive either way).
# Drain window stretches only marginally — daily generation (~400-800
# novel frames after perceptual_hash dedup) clears in any 30-60 min idle
# window.
#
# ``intra_op`` is op-level parallelism (the dominant CNN convs/matmuls).
# ``inter_op`` is op-graph parallelism, less useful for these small graphs;
# we keep it at 1 to avoid extra thread-pool overhead.
OCR_DRAIN_INTRA_OP_NUM_THREADS: int = 4
OCR_DRAIN_INTER_OP_NUM_THREADS: int = 1

# ── §11.7.1 Spillover (ring-overflow + monitor-disconnect bounded fallback) ──
#
# Two boundary cases require a small disk-backed safety net so we don't
# silently lose frames the user genuinely captured:
#
#   1. Ring full (deque(maxlen) overflow). A user who is actively
#      switching screens for 4h+ without an idle window will eventually
#      fill 128 slots per monitor. Without spillover the oldest novel
#      frames are dropped silently, biasing LTM toward "what user did
#      most recently" only.
#   2. Monitor disconnect. When the user unplugs an external display
#      _reconcile_monitors detects the vanished corner; the in-memory
#      ring for that monitor is then unreachable. Without spillover its
#      contents are lost.
#
# Spillover writes one (jpeg, meta.json) pair per frame into
# %USERPROFILE%\HandQ\personality\spillover\ — co-located with memory.db
# under the user's "what HandQ has learned about me" root (see
# ARCHITECTURE.md §1.5). The drain worker reads them after the in-memory
# rings are empty, OCRs them serially, and unlinks both files.
#
# The file count cap is the disk-side equivalent of the ring's maxlen:
# once exceeded, the OLDEST spilled pair is dropped (FIFO). The age cap
# is a backstop that purges anything left behind by a previous run that
# never came back to drain it (e.g. user uninstalls then reinstalls).

PERSONALITY_SPILLOVER_SUBDIR: str = "spillover"
ACTIVITY_SPILL_MAX_FILES: int = 256       # pairs (.jpg + .meta.json)
ACTIVITY_SPILL_MAX_AGE_HOURS: float = 24.0
# Truncation for the recent_texts snapshot we ship inside meta.json so
# orphan frames (monitor gone) can still run the Jaccard text dedup. We
# keep at most TEXT_HISTORY_SIZE entries × this many chars each — bounded
# at ~6 KB per meta.json even in the worst case.
ACTIVITY_SPILL_RECENT_TEXT_MAX_CHARS: int = 800

# Sensitive-window patterns for the activity monitor's foreground-app probe.
# These complement the desktop_tool list (handq_config.yaml :: desktop ::
# sensitive_window_patterns) — when the foreground window matches, we skip
# capture for the monitor that owns it. Matched against window title.
ACTIVITY_SENSITIVE_WINDOW_PATTERNS: tuple = (
    r"(?i)bitwarden|1password|keepass|lastpass|dashlane",
    r"(?i)bank|wallet|crypto|trading",
    r"(?i)password|passphrase|2fa|otp",
    r"(?i)private[\s\-_]?(window|browsing|tab)",
)


# ── 12. Scheduler (固化脚本 / 定时任务) ────────────────────────────────────────
#
# The scheduler lets the user pin a frequently-used HandQ prompt to a
# schedule and have the bridge auto-fire it. The fired task runs through
# the SAME FlowController flow as a user-initiated request, so:
#
#   * It inherits every confirmation gate (interaction_switches in
#     handq_config.yaml).
#   * Result lands in the UI exactly like a manual request.
#   * No separate "script execution" sandbox to worry about.
#
# Persistence file: %USERPROFILE%\HandQ\scheduled_tasks.json
# Schedule grammar (parsed in scheduler/schedule.py):
#   - "every <N> minutes"  /  "every <N> hours"
#   - "daily HH:MM"        (local time)
#   - "weekly <DOW> HH:MM" (DOW = mon|tue|wed|thu|fri|sat|sun)

# How often the scheduler thread wakes up to look for due tasks. 30s is
# the resolution floor — schedules accurate to ±30s.
SCHEDULER_TICK_SEC: float = 30.0

# Below this, the user gets an error on create. The scheduler is the
# user's tool — they own the consequences of a tight cadence (LLM
# spend, rate limits, runtime overlap). We just refuse 0/negative as
# nonsense; everything else is the user's call.
SCHEDULER_MIN_INTERVAL_SEC: int = 1

# How many consecutive failures before a task is auto-disabled. The user
# can re-enable from the UI / cron_set_enabled IPC.
SCHEDULER_MAX_FAILURES_BEFORE_DISABLE: int = 3

# Hard cap on how long a scheduled task can run before the scheduler
# considers it "stuck". Right now we just log; a future version can cancel.
SCHEDULER_TASK_TIMEOUT_SEC: int = 1800

# When a schedule fires while a session is already active, do we
#   * "skip"  — log + bump next_run; preserves user-in-the-loop,
#   * "queue" — wait until the active session ends, then fire,
#   * "kill"  — abort active session and run the scheduled one.
# We hard-code "skip" as the only safe default; "kill" would require
# explicit user opt-in per task.
SCHEDULER_BUSY_POLICY: str = "skip"

# Recurring tasks auto-expire this long after creation — they fire one final
# time on/after the deadline, then are disabled. Bounds the lifetime of a
# session that leaves a loop running. Mirrors Claude Code's 7-day hard cap on
# recurring cron jobs. One-shot ("once at ...") tasks are unaffected (they
# already disable themselves after firing).
SCHEDULER_RECURRING_MAX_AGE_SEC: int = 7 * 24 * 3600

# Anti-thundering-herd jitter. Everyone who asks for "9am" gets the same
# 0-minute mark, so absent jitter every client fires the same instant. We add
# a DETERMINISTIC per-task offset (derived from the task id, NOT random, so
# next_run_at stays reproducible and testable):
#   * recurring: fire up to min(10% of period, cap) LATER.
#   * one-shot landing exactly on :00 / :30 : fire up to 90s EARLIER.
SCHEDULER_JITTER_MAX_FRACTION: float = 0.10      # ≤10% of the period
SCHEDULER_JITTER_MAX_SEC: int = 15 * 60          # …capped at 15 minutes
SCHEDULER_ONESHOT_JITTER_EARLY_SEC: int = 90     # one-shot on :00/:30 nudge


# ── 13. Correction pipeline (retroactive memory hygiene) ────────────────────
#
# When a triage rule changes between releases, existing entries that were
# accepted under the OLD rule won't get re-evaluated unless we ship a
# data-hygiene migration. The :class:`RetriageWorker` runs once per
# rule_version bump and produces ``correction_proposals`` rows; only the
# DETERMINISTIC subset is auto-applied — judgment calls accumulate as
# proposals for explicit review (yansu philosophy: "automatic capture is
# one-sided, automatic forgetting needs mutual consent").
#
# Tier semantics:
#   - DETERMINISTIC : math says it; no judgment. Auto-apply.
#                     (e.g. archive an L2 source whose synthesis row already
#                      cites it; archive an exact-hash duplicate)
#   - HIGH_CONF     : LLM emitted confidence ≥ 0.95. Off by default —
#                     opt-in for users who trust the audit pipeline.
#   - PROPOSAL_ONLY : everything else. Surfaced via IPC, applied only on
#                     explicit user / admin action.
CORRECTION_TIER_DETERMINISTIC: str = "deterministic"
CORRECTION_TIER_HIGH_CONF: str = "high_conf"
CORRECTION_TIER_PROPOSAL_ONLY: str = "proposal_only"

# Default policy: auto-apply both deterministic AND high-confidence LLM
# proposals. Tightening to PROPOSAL_ONLY restores the strict "every
# correction needs review" mode; loosening to a lower CORRECTION_HIGH_CONF_FLOOR
# (e.g. 0.80) trades more silent cleanup for more potential mis-archives —
# both are recoverable via 'correction restore --reason-prefix correction_v*_'.
CORRECTION_AUTO_APPLY_TIER: str = CORRECTION_TIER_HIGH_CONF

# Confidence floor for HIGH_CONF tier auto-apply. 0.80 is empirically
# calibrated against the v3 LLM audit: LLM judgments on clear-cut path
# inventory entries cluster at 0.82, so 0.85 was just-too-strict and
# left nearly all "obviously deletable" proposals stranded. 0.80 catches
# the natural cluster while still excluding LLM uncertainty (most
# judgment-call entries land 0.65–0.75). Mis-archive remains recoverable
# via 'correction restore --reason-prefix correction_v*_'.
CORRECTION_HIGH_CONF_FLOOR: float = 0.80

# How many entries the LLM-retriage path commits progress every N entries.
# Smaller = more SQLite writes but tighter resume granularity after crash.
CORRECTION_RETRIAGE_CHECKPOINT_EVERY: int = 20

# Recall priority window: entries recalled within this many days are
# tagged "actively used" in the LLM retriage prompt. Yansu philosophy
# inversion — frequently-used entries are NOT silently protected from
# archive; instead their proposals get a "high-priority review" badge so
# the user actively decides. Default 30d covers a normal sprint cadence.
CORRECTION_RECALL_PRIORITY_DAYS: int = 30

# Retriage worker pool is built from ``llm.models`` (the main pool) directly;
# see ``LongTermMemory._build_llm_services_for_retriage``. LLM-based audit
# migrations want the strongest reasoning available, not the helper pool.


# ── Legacy dimension registry ─────────────────────────────────────────────
#
# Dimension values removed from the MemoryDimension enum. On startup,
# archive_legacy_dimensions() sets archived=1 for any entry still carrying
# one of these. Append here when deprecating a dimension; the cleanup is
# deterministic, idempotent, and runs before DreamWorker.
LEGACY_DIMENSIONS: list = ["insight"]


# ── 14. Pre-insert dedup gate (trigram Jaccard) ─────────────────────────────
#
# Catastrophic duplication (prod-observed: "IDLE state" ×6, "SAP Concur" ×7,
# "multi-session" ×25) came from the LLM producing near-identical summaries on
# adjacent triage runs about the same topic. The existing post-hoc merge scan
# (MERGE_EXACT_THRESHOLD=0.90 cosine) eventually collapses these, but only after
# the noise has already occupied recall slots for hours. The pre-insert gate is
# the immediate stop, complementary to the async merge scan.
#
# Gate operates on the LLM's verdict after triage, before insert_entry(). It
# BM25-fetches the top-K FTS candidates, computes trigram-Jaccard over summary
# (and content when summary is borderline), and returns one of:
#   >= DROP     — literal near-duplicate; verdict discarded, candidate REJECTED
#   [UPDATE, DROP) — same topic, refresh existing entry via update_entry_versioned
#   < UPDATE    — genuinely new, normal insert path
#
# Trigram Jaccard chosen over Levenshtein or token-overlap because:
#   1. Language-agnostic (works for zh/en mixed content).
#   2. Punctuation-robust (the LLM's summary phrasing wobbles here).
#   3. O(n) to compute; the whole gate is <5ms overhead.
DEDUP_JACCARD_DROP_THRESHOLD: float = 0.70
DEDUP_JACCARD_UPDATE_THRESHOLD: float = 0.40
DEDUP_FTS_CANDIDATES: int = 5


# ── 15. Activity Arc Aggregation ───────────────────────────────────────────
#
# The ArcAggregator sits above SessionAggregator: sessions group snapshots
# by app window; arcs group sessions by continuous user activity. An arc
# closes only on a real idle gap (user left the desk / locked screen for
# ≥ ARC_IDLE_GAP_MS). Continuous app switching (VS Code → Browser → Terminal)
# stays within one arc.
#
# Closed arcs with ≥ ARC_MIN_SESSIONS are the input unit for the
# SemanticExtractor — enough cross-app context to distill genuine workflow
# patterns rather than single-app fragments.

ARC_IDLE_GAP_MS: int = 20 * 60 * 1000        # 20 min idle closes an arc
ARC_MIN_SESSIONS: int = 2                      # min sessions for LLM processing
ARC_MAX_DURATION_MS: int = 120 * 60 * 1000    # 2h hard cap prevents unbounded growth
ARC_AGGREGATOR_TICK_SEC: float = 60.0         # worker poll interval
ARC_MAX_SNAPSHOTS_PER_PROMPT: int = 30        # cap LLM input size per arc
