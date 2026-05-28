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

# Default: LLM rerank for high-stakes call sites (planner). Receptionist
# calls opt out via format_context_block(rerank=False) to avoid 1 extra
# LLM call per user message.
RERANKER_PROVIDER: str = RERANKER_LLM

# Cap how many candidates we send to the rerank LLM. Beyond ~20 the
# scoring quality degrades (LLM gets overwhelmed) and the prompt grows.
# We over-fetch from stage 1 to ~k*3 so RERANK_INPUT_LIMIT >= that.
RERANKER_INPUT_LIMIT: int = 15

# Per-call timeout. The rerank prompt is short (one query + ~15 short
# summaries), so the LLM should respond fast — but receptionist tier
# may still take a few seconds.
RERANKER_TIMEOUT_SECONDS: float = 30.0


# ── 3. DreamWorker ──────────────────────────────────────────────────────────

DREAM_INTERVAL_SECONDS: float = 60.0
DREAM_BATCH_SIZE: int = 8
DREAM_MAX_RETRY: int = 5
DREAM_STUCK_SECONDS: int = 300

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
DREAM_BACKFILL_STARTUP: int = 50
DREAM_BACKFILL_CYCLE: int = 10

# Sleep-on-error before resuming the main loop. Higher than the normal
# interval so a persistent failure doesn't spam logs.
DREAM_ERROR_SLEEP_SECONDS: int = 30

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
# Two thresholds:
#   - EXACT  (>= MERGE_EXACT_THRESHOLD)   : auto-merge, archive older
#   - PROPOSE(>= MERGE_PROPOSE_THRESHOLD) : write to merge_proposals,
#                                            surface to user for review
MERGE_SCAN_EVERY_N_CYCLES: int = 60         # 60 × 60s = once per hour
MERGE_EXACT_THRESHOLD: float = 0.95          # auto-merge bar
MERGE_PROPOSE_THRESHOLD: float = 0.85        # propose bar (< exact)
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
DREAM_L3_CLUSTER_THRESHOLD: float = 0.50    # patterns are already
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
RECALL_MIN_SCORE: float = 0.0
RECALL_FTS_OVERFETCH: int = 3

# Reciprocal-rank-fusion constant (Cormack et al. 2009). Lower = top ranks
# dominate more. 60 is the literature default; values in [40, 80] all behave
# similarly in practice.
RRF_K: int = 60

# Note: kind discriminators (KIND_MEMORY etc.) live as ``EntryKind`` in
# :mod:`models` — not duplicated here. Use ``EntryKind.MEMORY.value`` when
# you need the raw string.


# ── 5. PII ──────────────────────────────────────────────────────────────────

PII_ENABLED: bool = True


# ── 6. LLM tier for the DreamWorker ─────────────────────────────────────────

TIER_RECEPTIONIST: str = "receptionist"
TIER_FROM_DATA: str = "from_data"

TRIAGE_LLM_TIER: str = TIER_RECEPTIONIST


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

# Subdirectory name (inside the personality root) for the
# PersonalityMonitor's transient screenshot files. Kept literally
# "ephemeral" because that matches the user-facing description of
# how those frames behave: written, OCR'd, immediately deleted.
PERSONALITY_FRAMES_SUBDIR: str = "ephemeral"

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
# adaptive screen-sampler that feeds ACTIVITY_OBSERVER candidates into the
# LTM pipeline. They live here (not in a separate config or YAML) because:
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
ACTIVITY_TIER_WARM_INTERVAL_SEC: float = 30.0
ACTIVITY_TIER_COLD_INTERVAL_SEC: float = 120.0
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
#   3. otherwise → buffer in memory; flush to LTM when buffer fills
#      OR when the per-monitor flush deadline elapses
ACTIVITY_OCR_MIN_CHARS: int = 40
ACTIVITY_OCR_TEXT_JACCARD_BAR: float = 0.65  # >= bar means "same screen"
ACTIVITY_OCR_EXCERPT_MAX_CHARS: int = 600    # how much OCR text we keep
ACTIVITY_BUFFER_FLUSH_AFTER_N: int = 8
ACTIVITY_BUFFER_FLUSH_AFTER_SEC: int = 600   # 10 min

# ── Daily-summary cadence ──────────────────────────────────────────────────
#
# Once per day we emit a single ACTIVITY_OBSERVER candidate that summarises
# the whole day's accepted samples. The dream worker triages it like any
# candidate — produces 0..N INSIGHT/knowledge entries with a wider lens.
#
# We use a wall-clock hour-of-day rather than "every 24h after process
# start" so the rollover is predictable and survives restarts.
ACTIVITY_DAILY_SUMMARY_HOUR_LOCAL: int = 22  # 10pm local time
ACTIVITY_DAILY_SUMMARY_MAX_SAMPLES: int = 80  # cap prompt size

# ── Disk hygiene ──────────────────────────────────────────────────────────
#
# We aim for ZERO accumulating PNG files. Each capture writes to a single
# rotating filename per monitor; OCR runs against it; the file is unlinked
# immediately after. The activity tier of ScreenshotStore acts as a backstop
# (max_files / max_age_days from handq_config.yaml).
#
# Setting this False (the default) makes the activity_monitor unlink each
# frame the moment OCR returns. Flip to True only for debugging — it's a
# privacy footgun in production.
ACTIVITY_KEEP_FRAME_FILES: bool = False

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
