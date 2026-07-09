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
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, List, Optional

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
from .prompts import (
    MERGE_ARBITER_SYSTEM,
    SKILL_EXTRACTION_SYSTEM,
    TRIAGE_SYSTEM,
    parse_skill_extraction,
    parse_verdict,
    render_user,
)
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


# Path-fragment regex used by the semantic-event promotion guard.
# Catches the common "I saw VSCode showing C:\foo\bar" type summaries
# that the LLM kept proposing as memory. Matches: drive-letter
# Windows paths, /Local/... POSIX-ish paths, and any backslash-heavy
# fragment.
import re as _re  # local import to keep top-of-file imports unchanged
_PATH_FRAGMENT_RE = _re.compile(
    r"(?:[A-Za-z]:[\\/][\w./\\\-]+|/[Ll]ocal/[\w./\\\-]+|[\w\-]+[\\/][\w\-]+[\\/][\w\-]+)"
)
# A summary is "path inventory" when path tokens cover most of its
# characters AND no preference / behaviour verb is present. Tuned
# against real data — see the rejected entries listed in the v1.0 LTM
# postmortem ("HandQ project at C:\CodeProject\HandQ" etc.).
_PATH_COVERAGE_BAR: float = 0.35
_BEHAVIOUR_VERBS_EN = (
    "use", "uses", "prefer", "prefers", "always", "never",
    "lint", "test", "deploy", "run", "skip", "switch",
)
# Word-boundary regex over the behaviour-verb list. The naive substring check
# used by ``_is_path_inventory`` catches "user" as containing "use" and thus
# spuriously exempts every "User ..." summary — masking exactly the generic
# noise the ``_is_generic_observation`` guard is meant to reject. \b anchors
# each token so "user" no longer swallows "use", "runs" no longer swallows
# "run" (well, "runs" also matches "run" via the list anyway — the point is
# that "user" cannot).
_BEHAVIOUR_VERB_RE = _re.compile(
    r"\b(?:" + "|".join(_BEHAVIOUR_VERBS_EN) + r")\b",
    _re.IGNORECASE,
)

# Generic-observation openers. Titles beginning with these tokens (case-
# insensitive, optional leading whitespace) are the "You actively manage
# HandQ" / "User edits note about X" / "Reviewing config in VS Code" class of
# summary — they describe the observation SURFACE rather than an insight, and
# in prod they were recalled 30+ times each because their genericness matched
# too many queries. English-only for now; the extractor prompt currently emits
# English titles, so this covers the observed noise. Extend with zh openers
# ("用户 ...", "开发者 ...") if the prompt language changes.
_GENERIC_OPENERS_RE = _re.compile(
    r"^\s*(?:"
    r"you\s+(?:are\s+|actively\s+|currently\s+)?"
    r"|the\s+user\s+"
    r"|user\s+"
    r"|developer\s+"
    r"|reviewing\s+"
    r"|monitoring\s+"
    r"|browsing\s+"
    r"|editing\s+"
    r"|reading\s+"
    r"|viewing\s+"
    r")",
    _re.IGNORECASE,
)


def _is_generic_observation(summary: str) -> bool:
    """True when *summary* begins with a known noise-opener pattern AND
    carries no behaviour-verb signal.

    Prod-observed generic entries ("You actively manage HandQ" recalled 34
    times; "User edits note about X" recalled 12+ times) inflate recall by
    matching almost any query. The extractor prompt (Fix 3) already tells the
    LLM to avoid these prefixes, but LLM regressions have to be caught
    server-side — this guard runs in ``_promote_one_event`` alongside
    ``_is_path_inventory`` to strip generic-opener summaries from BOTH the
    memory and knowledge tracks before the row is even written.

    Behaviour-verb exemption: a summary opened with a generic prefix but
    containing a durable-preference verb ("prefers", "always", "never",
    "uses", etc. — the ``_BEHAVIOUR_VERBS_EN`` list shared with the path-
    inventory guard) still carries real signal. "User always runs ruff before
    committing" is a real preference; "User edits note about X" is not.

    Returns True on empty / whitespace-only titles too so a malformed extract
    is treated as "kill both worth_* tracks" rather than silently writing an
    empty-title entry.
    """
    if not summary or not summary.strip():
        return True
    s = summary.strip()
    if not _GENERIC_OPENERS_RE.match(s):
        return False
    # Generic opener present. Grant a pass when a behaviour-verb signals
    # durable preference — same shape as _is_path_inventory's exemption, but
    # word-boundary'd so "user" cannot accidentally match "use".
    if _BEHAVIOUR_VERB_RE.search(s):
        return False
    return True


def _is_path_inventory(summary: str) -> bool:
    """True iff *summary* is mostly file-path tokens with no behaviour signal.

    The semantic-event LLM sometimes proposes entries like
    ``"HandQ project at C:\\CodeProject\\HandQ"`` — it saw a path on screen,
    decided it was an "insight", but a passively-observed path is not a
    durable signal (the user just had VSCode open). The guard rejects the
    entry rather than letting it pollute injection context.

    Behaviour verbs ("uses ... for X", "always run Y") earn the entry
    a pass even if it also names a path — those carry real preference
    signal beyond the path itself.
    """
    s = (summary or "").strip()
    if not s:
        return False
    matches = _PATH_FRAGMENT_RE.findall(s)
    if not matches:
        return False
    covered = sum(len(m) for m in matches)
    coverage = covered / max(len(s), 1)
    if coverage < _PATH_COVERAGE_BAR:
        return False
    lower = s.lower()
    if _BEHAVIOUR_VERB_RE.search(lower):
        return False
    return True


def _trigram_set(text: str) -> set:
    """Character trigram set for language-agnostic Jaccard similarity.

    Whitespace is collapsed and the string is lowercased before shingling so
    ``"IDLE  state" `` and ``"idle state"`` produce the same trigrams. Strings
    shorter than 3 chars degenerate to a single-token set (rare — summaries
    are always > 3 chars in practice).
    """
    normalized = _re.sub(r"\s+", " ", (text or "").lower().strip())
    if not normalized:
        return set()
    if len(normalized) < 3:
        return {normalized}
    return {normalized[i:i + 3] for i in range(len(normalized) - 2)}


def _jaccard_similarity(a: str, b: str) -> float:
    """Trigram-Jaccard similarity in [0.0, 1.0]. Returns 0.0 on empty inputs.

    Chosen for the pre-insert dedup gate (see ``_dedup_gate``) because it is
    language-agnostic, punctuation-robust, and O(n) — the whole gate adds
    <5ms overhead to a triage cycle. Deliberately NOT tuned for semantic
    similarity: the post-hoc merge scan (cosine over embeddings) handles that.
    """
    set_a = _trigram_set(a)
    set_b = _trigram_set(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    if inter == 0:
        return 0.0
    return inter / len(set_a | set_b)


def _user_handq_root() -> Path:
    """Mirror of bridge_main._user_handq_root (kept private here so the
    triage module doesn't import from bridge_main)."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ"


def _extract_frame(metadata: Optional[dict]) -> Optional[dict]:
    """Extract LTM 2.0 frame info from candidate metadata.

    Activity-observer submissions (and any future source) embed frame
    info as ``metadata['frame'] = {'os': ..., 'host': ..., 'confidence': ...}``.
    Returns the dict or None when absent/malformed.
    """
    if not metadata:
        return None
    f = metadata.get("frame")
    if not isinstance(f, dict):
        return None
    if not f.get("os"):
        return None
    return f


# SemanticExtractor labels a session with a *content-type* category
# (ssh_session / editing / browsing / ...), which is a different vocabulary
# from the KnowledgeCategory enum (domain / people / process / coding /
# other) that mem_entries(kind='knowledge') stores. Without an explicit
# bridge, only the lone shared value "other" would ever match and every
# observation-sourced knowledge row would land uncategorized — defeating
# category-scoped recall. This table maps the content-type vocabulary onto
# the closest KnowledgeCategory.
_CONTENT_TYPE_TO_KNOWLEDGE: dict = {
    "ssh_session": KnowledgeCategory.PROCESS,
    "remote_desktop": KnowledgeCategory.PROCESS,
    "editing": KnowledgeCategory.CODING,
    "debugging": KnowledgeCategory.CODING,
    "browsing": KnowledgeCategory.DOMAIN,
    "meeting": KnowledgeCategory.PEOPLE,
    "other": KnowledgeCategory.OTHER,
}


def _map_knowledge_category(category: Optional[str]) -> Optional[KnowledgeCategory]:
    """Resolve a semantic-event category string to a KnowledgeCategory.

    Honors a value that is *already* in the KnowledgeCategory vocabulary
    (future-proofs against the SemanticExtractor prompt being upgraded to
    emit knowledge categories directly), then falls back to the
    content-type → knowledge mapping. Returns None for anything unknown
    so the entry is stored uncategorized rather than mis-categorized.
    """
    if not category:
        return None
    c = category.strip().lower()
    try:
        return KnowledgeCategory(c)
    except ValueError:
        pass
    return _CONTENT_TYPE_TO_KNOWLEDGE.get(c)


# Function words stripped from a skill title before fingerprinting, so that
# phrasing differences ("open the settings page" vs "opens settings page")
# collapse to the same dedup key. Deliberately small — only high-frequency
# connectives/articles, never domain nouns or verbs that carry the skill's
# meaning.
_SKILL_FP_STOPWORDS: frozenset = frozenset({
    "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with",
    "and", "or", "is", "are", "be", "this", "that", "it", "as", "from",
    "into", "via", "how", "when", "then", "your", "you",
})

_SKILL_FP_TOKEN_RE = _re.compile(r"[a-z0-9]+")

# Minimum length of the *stem* that must remain after stripping a suffix.
# Guards against mangling short words ("used"→"us", "using"→"us", "ring"→"r").
# Only tokens whose stem stays >= this length get normalized.
_SKILL_FP_MIN_STEM = 3


def _normalize_skill_token(token: str) -> str:
    """Collapse common inflectional suffixes so verb/plural forms share a stem.

    Deliberately tiny and conservative — checks a handful of suffixes in
    priority order, applies at most one strip, and only when the remaining
    stem stays >= ``_SKILL_FP_MIN_STEM`` chars. It is NOT a real stemmer:
    the goal is only to keep "deploy"/"deploys"/"deployed"/"deploying" (and
    "service"/"services") on one fingerprint so phrasing drift doesn't reset
    the recurrence counter. Edge cases that under-merge (e.g. "code"/"coding")
    are accepted; over-merging across genuinely different words is avoided by
    the min-stem guard and the never-strip-"ss" rule.
    """
    # "ing" / "ed": verb tense drift ("restarting"→"restart", "deployed"→"deploy")
    for suffix in ("ing", "ed"):
        if token.endswith(suffix) and len(token) - len(suffix) >= _SKILL_FP_MIN_STEM:
            return token[: -len(suffix)]
    # trailing "s": plural / 3rd-person ("deploys"→"deploy", "services"→"service").
    # Never touch "ss" endings ("class", "process", "access").
    if (
        token.endswith("s")
        and not token.endswith("ss")
        and len(token) - 1 >= _SKILL_FP_MIN_STEM
    ):
        return token[:-1]
    return token


def _skill_fingerprint_tokens(title: str) -> List[str]:
    """Normalize a skill title to a sorted, de-duplicated token list.

    Lowercase → split on non-alphanumeric runs → drop stopwords → light suffix
    normalization (see ``_normalize_skill_token``) → sort+dedup. This is the
    phrasing-insensitive component of the skill recurrence fingerprint: two
    titles describing the same action with reordered, reworded, or differently-
    inflected wording collapse to one token set, hence one ``skill_fingerprint``.
    """
    tokens = _SKILL_FP_TOKEN_RE.findall(title.lower())
    return sorted(
        {
            _normalize_skill_token(t)
            for t in tokens
            if t and t not in _SKILL_FP_STOPWORDS
        }
    )


def _normalize_action_key(raw: object) -> Optional[str]:
    """Canonicalize the LLM's controlled ``action_key`` ('<verb>:<object>').

    Returns a normalized ``verb:object`` slug (each side lowercased, tokenized,
    light-stemmed via ``_normalize_skill_token``, and joined with ``-``) or None
    when the value is missing or unusable (no colon, or a side that reduces to
    nothing). The controlled key is far more phrasing-stable across independent
    LLM calls than a free-text ``name``, so when present it — not the toy token
    stemmer over the title — anchors the recurrence fingerprint. Each side gets
    the SAME normalization as the title path (stopword drop + light stem), so
    "deploy:the services" and "deploys:service" both converge to
    ``deploy:service``.
    """
    if not isinstance(raw, str) or ":" not in raw:
        return None
    verb, obj = raw.strip().lower().split(":", 1)

    def _slug(part: str) -> str:
        toks = _SKILL_FP_TOKEN_RE.findall(part)
        # Drop stopwords (same set as the title path) so an article the LLM
        # slips into the object — "the services" — collapses to "service"
        # instead of splitting the fingerprint. Fall back to the raw tokens if
        # stopword-dropping would empty the side (e.g. a degenerate key).
        kept = [t for t in toks if t not in _SKILL_FP_STOPWORDS]
        return "-".join(_normalize_skill_token(t) for t in (kept or toks))

    verb_slug, obj_slug = _slug(verb), _slug(obj)
    if not verb_slug or not obj_slug:
        return None
    return f"{verb_slug}:{obj_slug}"


def _skill_fingerprint(title: str, *, action_key: Optional[str] = None) -> str:
    """sha256 over canonical JSON of the recurrence key.

    This is the recurrence-cluster key (``skill_recurrence.fingerprint``):
    it decides what counts as "the same skill" when answering "have we seen
    this task pattern ``SKILL_RECURRENCE_THRESHOLD`` times yet?".

    Preferred key: the LLM's controlled ``action_key`` (``verb:object``, see
    ``_normalize_action_key``) — a short, machine-stable phrase that survives
    the wording drift a free-text title suffers across independent runs. Falls
    back to the sorted, light-stemmed token *set* of the ``title`` (see
    ``_skill_fingerprint_tokens``) only when no usable action_key is supplied,
    so an occasional parse miss still clusters approximately instead of
    splitting the counter completely. The two payload shapes are namespaced
    (``action_key`` vs ``tokens``) so they can never collide by coincidence.
    """
    import hashlib
    ak = _normalize_action_key(action_key)
    if ak is not None:
        payload: dict = {"action_key": ak}
    else:
        payload = {"tokens": _skill_fingerprint_tokens(title)}
    canonical = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _skill_cluster_text(verdict: dict) -> str:
    """Task-identity text embedded for SEMANTIC recurrence clustering.

    Joins the skill's name + description — the *what task is this* — and
    deliberately NOT the steps, which are procedure detail that varies run to
    run and would blur the cluster. Falls back to whatever field is present.
    """
    parts = [str(verdict.get("name") or ""), str(verdict.get("description") or "")]
    return ". ".join(p.strip() for p in parts if p and p.strip())


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
        emit: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._pii = pii_filter
        self._config = config
        # Outbound chat-feed notification channel (stdio_bridge._emit),
        # injected by LongTermMemory.init; None in tests / headless runs, so
        # every call site must guard on None. A generic seam for background
        # workers to push a hint into the chat feed. No live producer today —
        # its only past user, the retired skill-proposal staging hint, is gone
        # now that skills are minted directly as disabled files (see
        # _write_live_skill).
        self._emit = emit
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

        # Cold-boot network-not-ready window. On a freshly-booted laptop
        # the corporate VPN / proxy / DNS commonly need 30-60s before
        # qgenie-api.qualcomm.com becomes reachable. Without this delay
        # the first warmup ran at boot+2s and ate the full embedder retry
        # budget (~3.5min of ConnectError) before the network came up.
        try:
            await asyncio.sleep(C.DREAM_STARTUP_DELAY_SECONDS)
        except asyncio.CancelledError:
            return

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
                if cands:
                    # Process the batch concurrently with a small semaphore so
                    # we don't slam the helper LLM. Bounded fanout (3) keeps
                    # the LLM-side load tame while still cutting total batch
                    # time roughly 3x vs a strict serial loop — which is a real
                    # bottleneck when background observation produces candidate
                    # bursts faster than 1 / 5-30s.
                    sem = asyncio.Semaphore(C.DREAM_TRIAGE_CONCURRENCY)

                    async def _guarded(cand):
                        async with sem:
                            try:
                                await self._triage_one(cand)
                            except Exception:
                                _logger.exception("triage failed cid=%s", cand.id)

                    await asyncio.gather(*(_guarded(c) for c in cands))
                # LTM 2.0 second consumer: obs_semantic_events from the
                # observation pipeline. These are LLM-abstracted "what user
                # did" events; we promote them to mem_entries based on the
                # SemanticExtractor's worth_memory/worth_knowledge flags.
                # Frame info on the parent session is propagated onto
                # mem_entries.frame_json so recall can later filter by os/host.
                try:
                    n_evt = await self._process_semantic_events_batch()
                    if n_evt:
                        _logger.debug("processed %d semantic events", n_evt)
                except Exception:
                    _logger.exception("_process_semantic_events_batch failed")
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
                # Drain the recall_log in-memory deque on the same tick.
                # We hold the write lock anyway during this cycle's other
                # writes, so flushing here costs nothing extra and keeps
                # the recall hot path lock-free.
                try:
                    from .recall_logger import RecallLogger
                    await RecallLogger.get().flush(self._store)
                except Exception:
                    _logger.exception("recall_log flush failed; will retry next cycle")
                # Periodic post-hoc dedup. Every Nth cycle so the scan cost
                # is amortised; the dedup itself is cheap (pairwise cosine
                # over cached embeddings — no LLM calls).
                if self._cycle_count % C.MERGE_SCAN_EVERY_N_CYCLES == 0:
                    try:
                        await self._run_merge_scan()
                    except Exception:
                        _logger.exception("merge scan failed; will retry next cycle")
                if self._cycle_count % C.LTM_CLEANUP_EVERY_N_CYCLES == 0:
                    try:
                        await self._run_ltm_cleanup()
                    except Exception:
                        _logger.exception("ltm cleanup failed; will retry next cycle")
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

    # ── LTM 2.0 semantic-event promotion ──────────────────────────────────

    async def _process_semantic_events_batch(self) -> int:
        """LTM 2.0 path: promote obs_semantic_events into mem_entries.

        Reads pending events (accepted_entries IS NULL), then for each event:
          - if worth_memory: insert mem_entries(kind='memory', dimension=AGENTIC)
            with frame_json from the parent session
          - if worth_knowledge: insert mem_entries(kind='knowledge')
          - worth_skill is inert here: skills are minted ONLY from recurring
            successful task sessions (see _apply_session_skill), never from
            passive observation.

        Returns the count of events processed (with at least one mem_entry
        produced). Events with all worth_* flags false are still marked
        ``accepted_entries=[]`` so they don't get rescanned forever.
        """
        events = await self._store.list_semantic_events_pending_triage(limit=8)
        if not events:
            return 0

        processed = 0
        for ev in events:
            (eid, session_id, synthetic_origin, title, description, category,
             entities_json, apps_json, frame_os, frame_host, frame_conf,
             task_worthy) = ev
            try:
                entries = await self._promote_one_event(
                    event_id=eid,
                    session_id=session_id,
                    synthetic_origin=synthetic_origin,
                    title=title,
                    description=description,
                    category=category,
                    entities_json=entities_json,
                    frame_os=frame_os, frame_host=frame_host,
                    frame_confidence=frame_conf,
                    task_worthy=bool(task_worthy),
                )
                await self._store.set_semantic_event_accepted(eid, entries)
                if entries:
                    processed += 1
            except Exception:
                _logger.exception(
                    "promote_semantic_event failed eid=%s", eid[:8],
                )
        return processed

    async def _promote_one_event(
        self,
        *,
        event_id: str,
        session_id: Optional[str],
        synthetic_origin: Optional[str],
        title: str,
        description: str,
        category: Optional[str],
        entities_json: Optional[str],
        frame_os: Optional[str],
        frame_host: Optional[str],
        frame_confidence: Optional[float],
        task_worthy: bool,
    ) -> list:
        """Apply worth_* flags from one semantic event → mem_entries rows.

        Returns the list of ``{kind, id}`` dicts written to obs_semantic_events
        .accepted_entries — the audit trail for what this event produced.
        """
        # Re-fetch the worth flags + skill fields directly (the listing query
        # didn't return them).
        full = await self._store._fetchone(
            "SELECT worth_memory, worth_knowledge, worth_skill "
            "FROM obs_semantic_events WHERE id=?",
            (event_id,),
        )
        if not full:
            return []
        # _worth_skill is intentionally unused. Skills are minted ONLY from
        # recurring successful HandQ task sessions (see _apply_session_skill),
        # not from passive activity observation. The obs_semantic_events
        # .worth_skill column is left inert rather than removed — dropping it
        # would touch insert_obs_semantic_event + the schema for no real gain.
        worth_memory, worth_knowledge, _worth_skill = bool(full[0]), bool(full[1]), bool(full[2])

        if not (worth_memory or worth_knowledge):
            return []  # caller still marks accepted_entries=[] so we skip next time

        frame: Optional[dict] = None
        if frame_os:
            frame = {
                "os": frame_os,
                "host": frame_host or "unknown",
                "confidence": float(frame_confidence) if frame_confidence is not None else 0.5,
                "evidence": f"semantic_event:{event_id[:8]}",
            }

        content_lines = [
            f"## {title}",
            description or "",
        ]
        if entities_json:
            try:
                ents = json.loads(entities_json)
                if isinstance(ents, list) and ents:
                    content_lines.append("**Entities**: " + ", ".join(str(e) for e in ents))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        content = "\n\n".join(filter(None, content_lines))

        # PII gate. This content is derived from raw OCR excerpts + window
        # titles (see SemanticExtractor) — the single highest-risk on-screen
        # secret source: tokens echoed in terminals, tokenized URLs in title
        # bars, env-var assignments. Every observation→memory write must pass
        # self._pii. Frame anchoring does NOT close this hole (a frame scopes
        # *relevance*, not *secrecy*). Drop ALL tracks on a hit — a secret-
        # bearing observation produces no durable memory. We still return []
        # so the event is marked processed and not rescanned forever.
        if self._pii.has_secret(content):
            _logger.info(
                "semantic event %s dropped: PII detected in promoted content",
                event_id[:8],
            )
            return []

        accepted: list = []

        if (worth_memory or worth_knowledge) and _is_generic_observation(title):
            # Generic-opener titles ("You actively manage HandQ", "User edits
            # note about X") describe the observation surface, not an insight —
            # in prod they were recalled 30+ times each because their
            # genericness matched too many queries. Kill BOTH tracks: a
            # generic-observation title on the memory side is worthless, and
            # the same title on the knowledge side would be recalled just as
            # spuriously. Fix 3's prompt discourages this, but the guard runs
            # server-side to catch LLM regressions.
            _logger.info(
                "semantic event %s: worth_memory/knowledge skipped "
                "(generic-observation title=%r)",
                event_id[:8], title[:60],
            )
            worth_memory = False
            worth_knowledge = False

        if worth_memory and _is_path_inventory(title):
            # Passively-observed file paths ("HandQ project at C:\\...")
            # are not durable preference signal — the user just had an
            # editor open. Skip the memory track but still let the
            # knowledge / skill tracks run (they carry reusable facts).
            _logger.info(
                "semantic event %s: worth_memory skipped (path-inventory title=%r)",
                event_id[:8], title[:60],
            )
            worth_memory = False

        if worth_memory:
            try:
                verdict, dup_id = await self._dedup_gate(
                    new_summary=title[:120], new_content=content,
                    kind=EntryKind.MEMORY.value,
                )
                if verdict == "drop":
                    _logger.debug("promote eid=%s: memory dedup→drop (dup=%s)",
                                  event_id[:8], dup_id and dup_id[:8])
                else:
                    source = "personality_arc" if synthetic_origin else "semantic_event"
                    mem_id = await self._store.insert_entry(
                        kind=EntryKind.MEMORY,
                        dimension=MemoryDimension.AGENTIC,
                        summary=title[:120],
                        content=content,
                        source=source,
                        source_event_id=event_id,
                        frame=frame,
                    )
                    accepted.append({"kind": "memory", "id": mem_id})
            except Exception:
                _logger.exception("worth_memory promote failed eid=%s", event_id[:8])

        if worth_knowledge:
            try:
                verdict, dup_id = await self._dedup_gate(
                    new_summary=title[:120], new_content=content,
                    kind=EntryKind.KNOWLEDGE.value,
                )
                if verdict == "drop":
                    _logger.debug("promote eid=%s: knowledge dedup→drop (dup=%s)",
                                  event_id[:8], dup_id and dup_id[:8])
                else:
                    cat = _map_knowledge_category(category)
                    kn_id = await self._store.insert_entry(
                        kind=EntryKind.KNOWLEDGE,
                        category=cat,
                        summary=title[:120],
                        content=content,
                        source="semantic_event",
                        source_event_id=event_id,
                    )
                    accepted.append({"kind": "knowledge", "id": kn_id})
            except Exception:
                _logger.exception("worth_knowledge promote failed eid=%s", event_id[:8])

        return accepted

    async def _write_live_skill(
        self, *, title: str, description: str, content: str,
    ) -> bool:
        """Write (or update) a live, DISABLED skill under the unified Skill root.

        Once a task pattern recurs enough times we mint the skill directly into
        the unified Skill root (``%USERPROFILE%\\HandQ\\Skill\\<slug>\\SKILL.md``
        on Windows) with ``enabled: false``. The disable flag is the review
        mechanism — the user notices new skills in the control panel and flips
        them on. A disabled skill is invisible to every role (absent from the
        menu, and ``read_skill`` refuses it) until the user enables it — the
        file simply exists on disk in the meantime (see ``SkillRegistry``).

        Generate-or-update, with origin protection: if a skill already lives at
        the slug, we refresh it in place ONLY when it is auto-owned (minted by a
        previous triage run). If the user created, imported, or hand-edited a
        skill at that slug, its ``origin`` is ``user`` and we back off entirely
        — a coincidental name collision (the LLM titles a fresh session the same
        as an existing hand-tuned skill) must never silently overwrite the
        user's content. New skills are written ``origin=auto`` so a later run
        may refresh them.

        Runs in the shared backend process, so ``SkillRegistry.get()`` is the
        same singleton the agent reads; the write is visible immediately.
        """
        from ..skills import (
            SKILL_ORIGIN_AUTO,
            SkillRegistry,
            slugify_skill_name,
        )

        slug = slugify_skill_name(title, fallback="skill")

        def _apply() -> dict:
            reg = SkillRegistry.get()
            existing = reg.get_any(slug)
            if existing is not None:
                # Origin protection — never clobber user-owned content.
                if existing.origin != SKILL_ORIGIN_AUTO:
                    return {"ok": False, "reason": "user_owned", "name": slug}
                return reg.update_skill(
                    slug, description=description, body=content,
                    origin=SKILL_ORIGIN_AUTO,
                )
            return reg.create_skill(
                slug, description, content, enabled=False, origin=SKILL_ORIGIN_AUTO,
            )

        res = await asyncio.to_thread(_apply)
        if not res.get("ok"):
            if res.get("reason") == "user_owned":
                _logger.info(
                    "session skill skipped: slug=%s is user-owned "
                    "(origin protection — not overwriting hand-tuned content)",
                    slug,
                )
            else:
                _logger.info("session skill not written slug=%s res=%s", slug, res)
            return False
        _logger.info("session skill written slug=%s (disabled)", res.get("name", slug))
        return True

    async def _extract_skill_from_session(self, c: Candidate) -> Optional[dict]:
        """Run the dedicated skill-extraction LLM pass on one task trajectory.

        Returns the parsed verdict dict (see ``parse_skill_extraction``) or
        None when no helper service is configured. The verdict's
        ``worth_skill`` is the gate the caller checks; this is a SEPARATE LLM
        call from the main triage verdict so the 500-line TRIAGE_SYSTEM stays
        focused on memory/knowledge and the skill prompt can be specialized.
        """
        if not self._helper_services:
            return None
        from src.infrastructure.llm_pool import call_with_fallback

        result = await call_with_fallback(
            self._helper_services,
            dict(
                messages=[
                    {"role": "system", "content": SKILL_EXTRACTION_SYSTEM},
                    {"role": "user", "content": c.raw_text},
                ],
                json_mode=True,
                max_tokens=600,
            ),
        )
        return parse_skill_extraction(result.content or "")

    async def _bump_skill_recurrence(
        self, verdict: dict, *, title: str, cid: str,
    ) -> Optional[int]:
        """Bump the recurrence counter for a skill-worthy session; return the new
        count (``None`` only on hard failure → caller aborts).

        Prefers SEMANTIC clustering (embedding + cosine, via
        ``store.bump_skill_recurrence_semantic``) so synonymous phrasings of the
        same task share one counter — the whole reason skills reach threshold at
        all. Falls back to the exact-string lexical fingerprint when the embedder
        is unavailable or embedding fails, mirroring the "embedder down →
        degrade" pattern used by dream synthesis.
        """
        if self._embedder is not None and self._embedder.available:
            text = _skill_cluster_text(verdict)
            if text:
                try:
                    vec = (await self._embedder.embed([text]))[0]
                    if vec:
                        return await self._store.bump_skill_recurrence_semantic(
                            vec, title=title,
                            category=verdict.get("category"),
                            provider=self._embedder.provider,
                            model=self._embedder.model,
                        )
                except Exception:
                    _logger.exception(
                        "semantic skill recurrence failed cid=%s; "
                        "falling back to lexical", cid[:8],
                    )
        fp = _skill_fingerprint(title, action_key=verdict.get("action_key"))
        try:
            return await self._store.bump_skill_recurrence(fp, title=title)
        except Exception:
            _logger.exception("bump_skill_recurrence failed cid=%s", cid[:8])
            return None

    async def _apply_session_skill(self, c: Candidate) -> bool:
        """Extract a candidate skill from a successful task session and, only
        once the same task pattern has recurred enough times (or the session
        is complex enough to be worth capturing on its own), write it as a
        live (disabled) skill.

        Flow: extract (LLM) → PII gate → fingerprint → bump recurrence counter
        → gate on ``SKILL_RECURRENCE_THRESHOLD`` OR
        ``SKILL_HIGH_COMPLEXITY_STEP_COUNT``. Below both thresholds we ONLY
        increment the counter and return False (nothing written) — a one-off
        trivial task never becomes a skill. At/above either threshold we
        write (or update) the skill directly into the unified Skill root with
        ``enabled: false`` — no approval step. Returns True only when a skill
        file was actually written.
        """
        verdict = await self._extract_skill_from_session(c)
        if not verdict or verdict.get("worth_skill") is not True:
            return False

        title = verdict["name"]
        content_lines = [f"# {title}"]
        when = verdict.get("when_to_use")
        if when:
            content_lines.append(f"**When to use**: {when}")
        steps = verdict.get("steps_md")
        if steps:
            content_lines.append(steps)
        content = "\n\n".join(filter(None, content_lines))

        # PII post-filter. The LLM-generated steps can surface a secret that
        # the raw trajectory's pre-filter didn't (e.g. a token paraphrased
        # into a step). Mirror the memory/knowledge post-filter: a secret-
        # bearing skill is dropped and never written.
        if self._pii.has_secret(content):
            _logger.info(
                "session skill dropped: PII detected in extracted steps cid=%s",
                c.id[:8],
            )
            return False

        count = await self._bump_skill_recurrence(verdict, title=title, cid=c.id)
        if count is None:
            return False

        step_count = (c.metadata or {}).get("step_count", 0) or 0
        is_high_complexity = step_count >= C.SKILL_HIGH_COMPLEXITY_STEP_COUNT

        if count < C.SKILL_RECURRENCE_THRESHOLD and not is_high_complexity:
            _logger.info(
                "session skill below recurrence threshold cid=%s count=%d/%d steps=%d",
                c.id[:8], count, C.SKILL_RECURRENCE_THRESHOLD, step_count,
            )
            return False

        if count < C.SKILL_RECURRENCE_THRESHOLD:
            _logger.info(
                "session skill high-complexity bypass cid=%s count=%d/%d steps=%d/%d",
                c.id[:8], count, C.SKILL_RECURRENCE_THRESHOLD,
                step_count, C.SKILL_HIGH_COMPLEXITY_STEP_COUNT,
            )

        description = (verdict.get("description") or title).strip()
        return await self._write_live_skill(
            title=title, description=description, content=content,
        )


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

        # Heartbeat after the FTS phase so a multi-second helper-LLM call
        # below doesn't push us past the stuck-triaging threshold while
        # we're still actively working. The reset_stuck_triaging gate is
        # currently 300s; long triage paths (slow LLM + reranker) can
        # legitimately exceed that, and getting reset mid-flight produces
        # duplicate entries via the secondary triage path.
        await self._store.heartbeat_candidate(c.id)

        try:
            verdict = await self._call_llm(c, existing_mem, existing_kn)
        except Exception as exc:
            new_count = await self._store.bump_candidate_retry(c.id, error=str(exc))
            if new_count >= self._max_retry:
                await self._store.set_candidate_status(
                    c.id, CandidateStatus.FAILED, reason=C.REASON_MAX_RETRY,
                )
            return

        # Heartbeat again after the LLM call. The remaining work
        # (PII post-filter, guards, _apply_memory chunking + insert,
        # embedding warmup) is bounded but non-trivial.
        await self._store.heartbeat_candidate(c.id)

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
        # even if the LLM ignored the prompt rule. Knowledge is allowed
        # (env constraints are often the reason for the failure). Passive
        # personality_arc observations bypass this (they're not derived
        # from the failed session's preferences). Enforced in code so
        # misbehaving models cannot poison the user-preference track.
        if (
            c.source == CandidateSource.SESSION_FAILED.value
            and verdict.worth_memory
            and verdict.memory_dimension == MemoryDimension.AGENTIC
        ):
            verdict.worth_memory = False
            verdict.memory_action = VerdictAction.SKIP.value
            verdict.memory_dimension = None
            verdict.reason = (verdict.reason + " | " + C.REASON_GUARD_FAILED_NO_MEMORY)[:200]

        # Defensive guard: ARCHIVE is destructive. Only direct user channels
        # may archive existing entries — observation / batched derivations
        # are NOT consent. Without this guard, a background-observed OCR
        # capture that happens to contain "stopped using X" prose would
        # archive a real preference based on environmental noise.
        _archive_allowed = c.source in (
            CandidateSource.RECEPTIONIST_TURN.value,
            CandidateSource.MANUAL_REMEMBER.value,
        )
        if not _archive_allowed:
            if verdict.memory_action == VerdictAction.ARCHIVE.value:
                verdict.memory_action = VerdictAction.SKIP.value
                verdict.memory_archive_id = None
                verdict.worth_memory = False
                verdict.reason = (
                    verdict.reason + " | guard:archive_only_from_user_channels"
                )[:200]
            if verdict.knowledge_action == VerdictAction.ARCHIVE.value:
                verdict.knowledge_action = VerdictAction.SKIP.value
                verdict.knowledge_archive_id = None
                verdict.worth_knowledge = False
                verdict.reason = (
                    verdict.reason + " | guard:archive_only_from_user_channels"
                )[:200]

        # Defensive guard: IDENTITY dimension is restricted to direct user
        # channels only. Background sources (session_*, post_commit) observing
        # "respond in Chinese" on-screen is NOT the user's cross-session
        # directive — it might be a colleague's setting or an example in
        # documentation. Downgrade to AGENTIC so the signal is still preserved
        # but won't be unconditionally injected.
        if (
            verdict.worth_memory
            and verdict.memory_dimension == MemoryDimension.IDENTITY
            and c.source not in (
                CandidateSource.RECEPTIONIST_TURN.value,
                CandidateSource.MANUAL_REMEMBER.value,
            )
        ):
            verdict.memory_dimension = MemoryDimension.AGENTIC
            verdict.reason = (
                verdict.reason + " | guard:identity_downgrade_to_agentic"
            )[:200]

        # Identity entries do NOT support in-place update — the risk of
        # silently mutating an always-injected directive is too high. Convert
        # update(id) to archive(old) + create(new) so the old directive is
        # explicitly retired and the new one stands alone.
        if (
            verdict.worth_memory
            and verdict.memory_dimension == MemoryDimension.IDENTITY
            and verdict.memory_action == VerdictAction.UPDATE.value
            and verdict.memory_update_id
        ):
            verdict.memory_action = VerdictAction.CREATE.value
            verdict.memory_archive_id = verdict.memory_update_id
            verdict.memory_update_id = None
            verdict.reason = (
                verdict.reason + " | identity_update_split_to_archive+create"
            )[:200]

        wrote_mem = wrote_kn = False
        # Defensive CAS: if reset_stuck_triaging fired during this triage
        # (e.g. slow LLM pushed total work past 300s), the candidate is now
        # back in 'pending' and the next dream cycle will re-triage it.
        # Skipping the writes here prevents duplicate entries — the new
        # triage will produce its own (possibly different) verdict.
        current_status = await self._store.get_candidate_status(c.id)
        if current_status != CandidateStatus.TRIAGING.value:
            _logger.warning(
                "triage cid=%s status changed to %s mid-flight (likely "
                "reset_stuck_triaging); skipping writes to avoid duplicates",
                c.id[:8], current_status,
            )
            return
        if verdict.worth_memory:
            try:
                # _apply_memory returns True only when it ACTUALLY wrote (insert
                # / update / archive), False when it skipped (dedup drop, missing
                # dimension). Without this, the dedup gate's silent drop would
                # still show up as CandidateStatus.ACCEPTED_MEMORY — hiding the
                # drop from audit and making dedup ineffectiveness invisible.
                wrote_mem = await self._apply_memory(c, verdict)
            except Exception:
                _logger.exception("apply_memory failed cid=%s", c.id)
        if verdict.worth_knowledge:
            try:
                wrote_kn = await self._apply_knowledge(c, verdict)
            except Exception:
                _logger.exception("apply_knowledge failed cid=%s", c.id)

        # Skill side-channel: a successful task trajectory may also be a
        # reusable skill. This runs INDEPENDENTLY of mem/kn (a session can
        # produce both a memory and a skill) and only for SESSION_COMPLETE —
        # failed trajectories are not reusable procedures. The skill is written
        # by _apply_session_skill itself; here we only capture whether one was,
        # to pick a terminal status when nothing else was written.
        wrote_skill = False
        if c.source == CandidateSource.SESSION_COMPLETE.value:
            try:
                wrote_skill = await self._apply_session_skill(c)
            except Exception:
                _logger.exception("apply_session_skill failed cid=%s", c.id)

        if wrote_mem and wrote_kn:
            status = CandidateStatus.ACCEPTED_BOTH
        elif wrote_mem:
            status = CandidateStatus.ACCEPTED_MEMORY
        elif wrote_kn:
            status = CandidateStatus.ACCEPTED_KNOWLEDGE
        elif wrote_skill:
            status = CandidateStatus.ACCEPTED_SKILL
        else:
            status = CandidateStatus.REJECTED
        await self._store.set_candidate_status(
            c.id, status, reason=(verdict.reason or "")[:80],
        )

    async def _apply_memory(self, c: Candidate, v: TriageVerdict) -> bool:
        """Apply the memory-track verdict. Returns True iff an entry was written.

        Written = insert / update / archive was actually executed. Skipped
        (dedup drop, malformed verdict without dimension) returns False so the
        caller can accurately set the candidate status to REJECTED instead of
        ACCEPTED_MEMORY.
        """
        # ARCHIVE: user explicitly contradicted an existing memory entry.
        # Soft-archive with a reason that the retroactive correction CLI
        # can target (`correction_v0_user_contradicted` — v0 because this
        # is live triage, not a rule_migration). No content to write.
        if v.memory_action == VerdictAction.ARCHIVE.value and v.memory_archive_id:
            try:
                await self._store.archive_memory_entry(
                    v.memory_archive_id,
                    reason="user_contradicted",
                )
                _logger.info(
                    "triage archived memory cid=%s entry=%s reason=user_contradicted",
                    c.id[:8], v.memory_archive_id[:8],
                )
            except Exception:
                _logger.exception(
                    "archive_memory_entry failed cid=%s entry=%s",
                    c.id, v.memory_archive_id,
                )
                raise
            return True
        if not v.memory_dimension:
            # Malformed verdict: worth_memory=True but no dimension specified.
            # Nothing to write — return False so the candidate is not falsely
            # marked ACCEPTED_MEMORY.
            return False
        # UPDATE with a hallucinated id is a real failure mode: the LLM
        # sometimes invents a UUID that looks like one of the existing
        # ids in the prompt. Validate the id exists before committing;
        # if it doesn't, fall back to CREATE so we don't lose the
        # extracted signal. The candidate's content is already in
        # v.memory_content, so a CREATE keeps the user's preference.
        if v.memory_action == VerdictAction.UPDATE.value and v.memory_update_id:
            existing = await self._store.get_memory_entry_full(v.memory_update_id)
            if existing is not None:
                await self._store.update_memory_entry(
                    v.memory_update_id,
                    new_summary=v.memory_summary,
                    new_content=v.memory_content,
                )
                return True
            _logger.info(
                "triage memory UPDATE id=%s not found; falling back to CREATE cid=%s",
                v.memory_update_id[:8], c.id[:8],
            )
            # fall through to insert path
        # Identity split: CREATE with a pending archive (old entry to retire).
        # The identity guard above converts UPDATE→CREATE+archive_id; here we
        # honour that by archiving before inserting the replacement.
        # Gated on IDENTITY dimension so a hallucinated archive_id on a
        # non-identity CREATE can never silently archive an unrelated entry.
        # If the archive fails, we abort the entire operation — creating the
        # new entry without retiring the old would leave two contradictory
        # identity directives both unconditionally injected.
        if (
            v.memory_action == VerdictAction.CREATE.value
            and v.memory_archive_id
            and v.memory_dimension == MemoryDimension.IDENTITY
        ):
            try:
                await self._store.archive_memory_entry(
                    v.memory_archive_id,
                    reason="superseded",
                )
                _logger.info(
                    "identity split: archived old entry=%s before create cid=%s",
                    v.memory_archive_id[:8], c.id[:8],
                )
            except Exception:
                _logger.exception(
                    "identity split: archive old entry=%s failed cid=%s; "
                    "aborting create to prevent duplicate identity directives",
                    v.memory_archive_id[:8], c.id[:8],
                )
                raise
        # Pre-insert dedup gate. Skips only the identity-split path (there the
        # old directive was just archived and the new one is an intentional
        # replacement — dedup against an unrelated identity is a poor signal).
        # For everything else (pure CREATE, UPDATE fall-through, non-identity
        # CREATE-with-archive_id), a literal near-duplicate summary means the
        # LLM already produced this entry earlier — silently drop or coalesce.
        if not (
            v.memory_archive_id
            and v.memory_dimension == MemoryDimension.IDENTITY
        ):
            dedup_action, dedup_eid = await self._dedup_gate(
                new_summary=v.memory_summary,
                new_content=v.memory_content,
                kind=EntryKind.MEMORY.value,
            )
            if dedup_action == "drop":
                _logger.info(
                    "dedup_gate: dropped memory CREATE cid=%s summary=%r",
                    c.id[:8], (v.memory_summary or "")[:60],
                )
                return False
            if dedup_action == "update" and dedup_eid:
                await self._store.update_memory_entry(
                    dedup_eid,
                    new_summary=v.memory_summary,
                    new_content=v.memory_content,
                )
                _logger.info(
                    "dedup_gate: coalesced CREATE→UPDATE cid=%s entry=%s",
                    c.id[:8], dedup_eid[:8],
                )
                return True
        entry_id = await self._store.insert_memory_entry(
            dimension=v.memory_dimension,
            summary=v.memory_summary,
            content=v.memory_content,
            candidate_id=c.id,
            source=c.source,
            source_ref=c.source_ref,
            frame=_extract_frame(c.metadata),
        )
        await self._maybe_warmup_embedding(entry_id, kind=EntryKind.MEMORY.value)
        return True

    async def _apply_verbatim_remember(self, c: Candidate, user_text: str) -> None:
        """Insert a /remember candidate as AGENTIC memory verbatim,
        skipping the LLM triage stage.

        Long procedures are usually FACTS about how to do something
        ("here's the workflow…") or explicit user preferences. AGENTIC
        dimension is the catch-all for user-personal information.

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
            dimension=MemoryDimension.AGENTIC.value,
            source=c.source,
        )
        # Step 2: insert DB entry. ``source_ref`` becomes the file path
        # (or the original ref if file write failed) so admin tooling
        # can find the .md.
        ref = str(file_path) if file_path is not None else c.source_ref
        actual_id = await self._store.insert_memory_entry_with_id(
            entry_id=entry_id,
            dimension=MemoryDimension.AGENTIC,
            summary=summary,
            content=user_text,
            candidate_id=c.id,
            source=c.source,
            source_ref=ref,
            frame=_extract_frame(c.metadata),
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

    async def _apply_knowledge(self, c: Candidate, v: TriageVerdict) -> bool:
        """Apply the knowledge-track verdict. Returns True iff an entry was written.

        Same contract as ``_apply_memory``: True on insert / update / archive,
        False on dedup drop or missing category.
        """
        # ARCHIVE: user explicitly contradicted an existing knowledge entry.
        if v.knowledge_action == VerdictAction.ARCHIVE.value and v.knowledge_archive_id:
            try:
                await self._store.archive_knowledge_entry(
                    v.knowledge_archive_id,
                    reason="user_contradicted",
                )
                _logger.info(
                    "triage archived knowledge cid=%s entry=%s reason=user_contradicted",
                    c.id[:8], v.knowledge_archive_id[:8],
                )
            except Exception:
                _logger.exception(
                    "archive_knowledge_entry failed cid=%s entry=%s",
                    c.id, v.knowledge_archive_id,
                )
                raise
            return True
        if not v.knowledge_category:
            # Malformed verdict: worth_knowledge=True but no category.
            return False
        # UPDATE-with-hallucinated-id fallback (see _apply_memory for
        # rationale). Validate the target exists; if not, CREATE instead
        # so the LLM's extracted signal isn't dropped.
        if v.knowledge_action == VerdictAction.UPDATE.value and v.knowledge_update_id:
            existing = await self._store.get_knowledge_entry_full(v.knowledge_update_id)
            if existing is not None:
                await self._store.update_knowledge_entry(
                    v.knowledge_update_id,
                    new_summary=v.knowledge_summary,
                    new_content=v.knowledge_content,
                )
                return True
            _logger.info(
                "triage knowledge UPDATE id=%s not found; falling back to CREATE cid=%s",
                v.knowledge_update_id[:8], c.id[:8],
            )
            # fall through to insert path
        # Pre-insert dedup gate. Runs unconditionally on the knowledge insert
        # path (no identity concept here). Same three-way outcome as the
        # memory gate — see ``_dedup_gate`` docstring for details.
        dedup_action, dedup_eid = await self._dedup_gate(
            new_summary=v.knowledge_summary,
            new_content=v.knowledge_content,
            kind=EntryKind.KNOWLEDGE.value,
        )
        if dedup_action == "drop":
            _logger.info(
                "dedup_gate: dropped knowledge CREATE cid=%s summary=%r",
                c.id[:8], (v.knowledge_summary or "")[:60],
            )
            return False
        if dedup_action == "update" and dedup_eid:
            await self._store.update_knowledge_entry(
                dedup_eid,
                new_summary=v.knowledge_summary,
                new_content=v.knowledge_content,
            )
            _logger.info(
                "dedup_gate: coalesced knowledge CREATE→UPDATE cid=%s entry=%s",
                c.id[:8], dedup_eid[:8],
            )
            return True
        entry_id = await self._store.insert_knowledge_entry(
            category=v.knowledge_category,
            summary=v.knowledge_summary,
            content=v.knowledge_content,
            candidate_id=c.id,
            source=c.source,
            source_ref=c.source_ref,
        )
        await self._maybe_warmup_embedding(entry_id, kind=EntryKind.KNOWLEDGE.value)
        return True

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
                eid, _chunk_id, _text, summary, dim, _created, _rank, _frame_os = r
                try:
                    dim_enum = MemoryDimension(dim) if dim else None
                except ValueError:
                    dim_enum = None
                entries.append(Entry(
                    id=eid, kind=EntryKind.MEMORY,
                    dimension=dim_enum, summary=summary,
                ))
            # Always include identity entries so the triage LLM can see
            # them for archive/dedup decisions regardless of FTS match.
            seen_ids = {e.id for e in entries}
            identity_entries = await self._store.list_memory_entries(
                dimension=MemoryDimension.IDENTITY,
                archived=False,
                limit=C.IDENTITY_TRIAGE_EXISTING_LIMIT,
            )
            for ie in identity_entries:
                if ie.id not in seen_ids:
                    entries.append(ie)
                    seen_ids.add(ie.id)
            return entries
        if kind == EntryKind.KNOWLEDGE.value:
            rows = await self._store.fts_search_knowledge(raw_text, limit=limit)
            entries = []
            for r in rows:
                eid, _chunk_id, _text, summary, cat, _created, _rank, _frame_os = r
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

    # ── Pre-insert dedup gate ───────────────────────────────────────────────

    async def _dedup_gate(
        self,
        *,
        new_summary: str,
        new_content: str,
        kind: str,
    ) -> tuple:
        """Trigram-Jaccard gate over the top-K FTS candidates.

        Runs immediately BEFORE ``insert_memory_entry`` / ``insert_knowledge_entry``
        (see ``_apply_memory`` / ``_apply_knowledge``). Purpose: stop the
        catastrophic duplication class ("IDLE state" ×6, "SAP Concur" ×7) at
        the write path, where the LLM has emitted a fresh CREATE verdict for a
        topic that already has an entry. Complementary to the async merge scan
        (which is cosine-based, embedding-driven, and only runs every 15 min).

        Returns one of:
          - ``("new", None)``  — no near-duplicate; caller proceeds with insert
          - ``("drop", None)`` — literal near-duplicate; caller returns without writing
          - ``("update", entry_id)`` — same topic; caller updates ``entry_id``

        Fails open: any FTS error returns ``("new", None)`` so a gate hiccup
        never blocks legitimate writes. Thresholds live in ``_constants.py``.
        """
        if not new_summary:
            return ("new", None)
        try:
            if kind == EntryKind.MEMORY.value:
                rows = await self._store.fts_search_memory(
                    new_summary, limit=C.DEDUP_FTS_CANDIDATES,
                )
            elif kind == EntryKind.KNOWLEDGE.value:
                rows = await self._store.fts_search_knowledge(
                    new_summary, limit=C.DEDUP_FTS_CANDIDATES,
                )
            else:
                return ("new", None)
        except Exception:
            _logger.debug("dedup_gate FTS lookup failed kind=%s", kind, exc_info=True)
            return ("new", None)

        for row in rows:
            # fts_search_* returns (entry_id, chunk_id, chunk_text, summary, facet,
            # created_at, rank, frame_os); first chunk text is a reasonable
            # proxy for full content (chunks are sequential slices of the same entry).
            eid = row[0]
            chunk_text = row[2] or ""
            summary = row[3] or ""
            s_sim = _jaccard_similarity(new_summary, summary)
            if s_sim >= C.DEDUP_JACCARD_DROP_THRESHOLD:
                return ("drop", None)
            if s_sim >= C.DEDUP_JACCARD_UPDATE_THRESHOLD:
                c_sim = _jaccard_similarity(new_content, chunk_text)
                if c_sim >= C.DEDUP_JACCARD_DROP_THRESHOLD:
                    return ("drop", None)
                return ("update", eid)
        return ("new", None)

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

        Uses ``llm.helper_models`` (cheap pool for background classification)
        with fallback to ``llm.models`` when ``helper_models`` is empty. Triage
        is async background so latency doesn't matter; quality of the
        worth-memory / worth-knowledge classification does — but the helper
        pool is sized for exactly this kind of cheap classification work.
        """
        try:
            from src.infrastructure.role_resolver import resolve_models_and_helper
            from src.infrastructure.llm_pool import make_from_data_services
            from src.infrastructure.anthropic_streaming_service import (
                AnthropicStreamingService,
            )
            from src.infrastructure.llm_service import LLMService
        except Exception:
            _logger.exception("dream worker: failed to import LLM stack")
            return

        llm_cfg = self._config.get("llm") or {}
        api_key = llm_cfg.get("API_KEY")
        if not api_key:
            _logger.warning("llm.API_KEY missing; dream worker has no helper services")
            return

        # Use the helper pool (cheap models for background classification);
        # fall back to the main pool when no helper is configured. As a last
        # resort, derive from the main pool with the make_from_data_services
        # helper which downgrades the chain to the cheaper-end models.
        main_models, helper_models = resolve_models_and_helper(llm_cfg)
        if helper_models:
            models: List[str] = list(helper_models)
            tier_label = "helper"
        elif main_models:
            _logger.info(
                "triage: helper_models empty; deriving from main pool",
            )
            services: List[LLMService] = [
                AnthropicStreamingService(model=m, api_key=api_key)
                for m in main_models
            ]
            self._helper_services = (
                make_from_data_services(services) if services else []
            )
            if self._helper_services:
                _logger.info(
                    "triage tier: main-pool-derived fallback (%d service(s))",
                    len(self._helper_services),
                )
            return
        else:
            _logger.warning("triage: no models in helper or main pool")
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

    async def _run_ltm_cleanup(self) -> None:
        """Prune stale LTM and observation rows on a periodic pass.

        Called every LTM_CLEANUP_EVERY_N_CYCLES from the main loop. All
        operations are pure SQL (no LLM calls) so they complete in
        milliseconds even on large tables. TTLs differ per table:
          - mem_candidates.raw_text / mem_recall_log : 90 days (seconds)
          - mem_correction_proposals (status=pending): 30 days (seconds)
          - obs_sessions (done/skipped, ended)       : 30 days (unix MS)
          - obs_semantic_events (triaged)            : 30 days (seconds)
          - obs_snapshots                            : 7 days (unix MS)
          - obs_events                               : 7 days (unix MS)
          - obs_pipeline_runs                        : 30 days (seconds)

        obs_snapshots.captured_at, obs_events.occurred_at AND
        obs_sessions.ended_at are in unix MILLISECONDS (the SessionAggregator
        stamps them from the snapshot's captured_at), so those cutoffs are
        multiplied by 1000; the other cutoffs stay in seconds. Deleting a
        snapshot cascades to obs_ocr_frames and evicts its FTS row. Everything
        here is a hard DELETE.
        """
        now_s = int(time.time())
        cutoff_raw = now_s - C.LTM_CANDIDATE_RAWTEXT_TTL_DAYS * 86400
        n_raw = await self._store.prune_candidate_raw_text(cutoff_raw)
        cutoff_log = now_s - C.LTM_RECALL_LOG_TTL_DAYS * 86400
        n_log = await self._store.prune_mem_recall_log(cutoff_log)
        cutoff_corr = now_s - C.LTM_CORRECTION_PROPOSAL_TTL_DAYS * 86400
        n_corr = await self._store.prune_correction_proposals(cutoff_corr)
        cutoff_sess_ms = (now_s - C.LTM_OBS_SESSION_TTL_DAYS * 86400) * 1000
        n_sess = await self._store.prune_obs_sessions(cutoff_sess_ms)
        cutoff_sem = now_s - C.LTM_OBS_SEMANTIC_EVENT_TTL_DAYS * 86400
        n_sem = await self._store.prune_obs_semantic_events(cutoff_sem)
        cutoff_snap_ms = (now_s - C.LTM_OBS_SNAPSHOT_TTL_DAYS * 86400) * 1000
        n_snap = await self._store.prune_obs_snapshots(cutoff_snap_ms)
        cutoff_evt_ms = (now_s - C.LTM_OBS_EVENT_TTL_DAYS * 86400) * 1000
        n_evt = await self._store.prune_obs_events(cutoff_evt_ms)
        cutoff_run = now_s - C.LTM_OBS_PIPELINE_RUN_TTL_DAYS * 86400
        n_run = await self._store.prune_obs_pipeline_runs(cutoff_run)
        if (n_raw or n_log or n_corr or n_sess or n_sem
                or n_snap or n_evt or n_run):
            _logger.info(
                "ltm cleanup: raw_text=%d candidates, recall_log=%d rows, "
                "correction_proposals=%d pending, obs_sessions=%d, "
                "obs_semantic_events=%d, "
                "obs_snapshots=%d, obs_events=%d, obs_pipeline_runs=%d",
                n_raw, n_log, n_corr, n_sess, n_sem,
                n_snap, n_evt, n_run,
            )

    async def _run_merge_scan(self) -> None:
        """Run dedup scan on both memory and knowledge entries.

        For each kind: pull every non-archived entry's representative
        embedding (chunk 0), brute-force cosine over all pairs, classify:
          - similarity >= MERGE_EXACT_THRESHOLD     → auto-merge
          - similarity in [LLM_GATE, EXACT)         → helper-LLM arbiter
          - similarity < LLM_GATE                   → keep both (no write)

        A merge is suppressed when the older (to-be-archived) entry was
        recalled within CORRECTION_RECALL_PRIORITY_DAYS: an actively-used
        entry is load-bearing, so both are kept and the pair is re-checked
        on the next scan (protection expires with the recall window).

        Skips pairs younger than ``MERGE_MIN_PAIR_AGE_SECONDS`` so two
        candidates from the same triage batch (which the LLM already
        decided are distinct) aren't second-guessed.

        The LLM-arbiter band shares one ``MERGE_LLM_MAX_PAIRS_PER_SCAN``
        budget across both kinds, and skips pairs already judged (their
        verdict is persisted as merged / kept_distinct), so a borderline
        pair costs at most one helper-LLM call ever — not one per scan.
        """
        if not self._embedder.available:
            return  # No vectors → nothing to compare
        decided = await self._store.list_decided_merge_pairs()
        llm_budget = C.MERGE_LLM_MAX_PAIRS_PER_SCAN
        for kind in (EntryKind.MEMORY.value, EntryKind.KNOWLEDGE.value):
            try:
                llm_budget = await self._run_merge_scan_kind(
                    kind, decided=decided, llm_budget=llm_budget,
                )
            except Exception:
                _logger.exception("merge scan failed for kind=%s", kind)

    async def _run_merge_scan_kind(
        self, kind: str, *, decided: set, llm_budget: int,
    ) -> int:
        rows = await self._store.list_entry_centroids(
            kind=kind,
            provider=self._embedder.provider,
            model=self._embedder.model,
        )
        if len(rows) < 2:
            return llm_budget  # Nothing to compare against

        # Decode once up-front so we don't unpack bytes inside the inner loop.
        decoded = []
        now = int(time.time())
        for row in rows:
            # LTM 2.0: list_entry_centroids returns 6-tuple
            # (entry_id, summary, created_at, updated_at, emb_bytes, frame_os)
            entry_id = row[0]
            summary = row[1]
            created_at = row[2]
            updated_at = row[3]
            emb_bytes = row[4]
            vec = vec_from_bytes(emb_bytes)
            if vec:
                decoded.append((
                    entry_id, summary, int(created_at or 0),
                    int(updated_at or now), vec,
                ))
        if len(decoded) < 2:
            return llm_budget

        exact_pairs = []
        arbiter_pairs = []
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
                elif sim >= C.MERGE_LLM_GATE_THRESHOLD:
                    arbiter_pairs.append((ai, bi, au, bu, asum, bsum, sim))
                # sim < MERGE_LLM_GATE_THRESHOLD → keep both, write nothing.

        # Auto-merge: keep newer (higher updated_at), archive older.
        for ai, bi, au, bu, sim in exact_pairs:
            keep, drop = (ai, bi) if au >= bu else (bi, ai)
            # Recall protection: don't archive an actively-recalled entry.
            if await self._recently_recalled(drop, kind):
                continue
            try:
                await self._auto_merge(
                    kind=kind, keep_id=keep, drop_id=drop, similarity=sim,
                )
            except Exception:
                _logger.exception(
                    "auto-merge failed kind=%s keep=%s drop=%s",
                    kind, keep[:8], drop[:8],
                )

        # LLM arbiter for the [0.85, 0.90) band. Without a helper LLM we
        # cannot judge the band, so skip it entirely (write nothing). The
        # pre-LLM behaviour staged a 'pending' review proposal "so no signal
        # is lost", but HandQ has no review surface — that row was never
        # consumed, and because a pending pair is NOT in the decided-memo
        # (list_decided_merge_pairs returns only merged/kept_distinct) the
        # same pair was re-staged on every scan → unbounded growth. Skipping
        # is lossless: the pair is re-evaluated on the next scan once a helper
        # pool is wired.
        if not self._helper_services:
            arbiter_pairs = []

        # Judge most-similar pairs first so a tight budget spends on the
        # likeliest duplicates.
        arbiter_pairs.sort(key=lambda t: t[6], reverse=True)
        arbiter_merged = 0
        arbiter_distinct = 0
        for ai, bi, au, bu, asum, bsum, sim in arbiter_pairs:
            if llm_budget <= 0:
                break
            pair = frozenset((ai, bi))
            if pair in decided:
                continue
            # Recall protection: the older entry is the merge's drop target.
            # If it's actively recalled, keep both and skip the LLM call so no
            # budget is spent; the pair is re-checked next scan and protection
            # expires with the recall window.
            drop_candidate = ai if au < bu else bi
            if await self._recently_recalled(drop_candidate, kind):
                continue
            llm_budget -= 1  # an attempted call counts against the cap
            try:
                same = await self._arbitrate_merge(
                    asum or "", bsum or "", sim,
                )
            except Exception:
                _logger.exception(
                    "merge arbiter LLM failed kind=%s a=%s b=%s",
                    kind, ai[:8], bi[:8],
                )
                continue  # leave undecided; retry next scan
            decided.add(pair)  # judged → never re-send to the LLM
            if same:
                keep, drop = (ai, bi) if au >= bu else (bi, ai)
                try:
                    await self._auto_merge(
                        kind=kind, keep_id=keep, drop_id=drop, similarity=sim,
                    )
                    arbiter_merged += 1
                except Exception:
                    _logger.exception(
                        "arbiter auto-merge failed kind=%s keep=%s drop=%s",
                        kind, keep[:8], drop[:8],
                    )
            else:
                # Persist 'keep both' so the pair is skipped on future scans.
                try:
                    pid = await self._store.insert_merge_proposal(
                        kind=kind, entry_a_id=ai, entry_b_id=bi, similarity=sim,
                    )
                    await self._store.resolve_merge_proposal(
                        proposal_id=pid, status="kept_distinct",
                    )
                    arbiter_distinct += 1
                except Exception:
                    _logger.exception(
                        "arbiter kept_distinct record failed kind=%s a=%s b=%s",
                        kind, ai[:8], bi[:8],
                    )

        if exact_pairs or arbiter_pairs:
            _logger.info(
                "merge scan kind=%s: auto-merged=%d, arbiter(merged=%d, "
                "distinct=%d)",
                kind, len(exact_pairs), arbiter_merged, arbiter_distinct,
            )
        return llm_budget

    async def _arbitrate_merge(
        self, summary_a: str, summary_b: str, similarity: float,
    ) -> bool:
        """Ask the helper LLM whether two near-duplicate entries are the
        same memory. Returns True (merge) or False (keep distinct).

        Only called for the [0.85, 0.90) similarity band: >=0.90 auto-merges
        and <0.85 keeps both silently. Unparseable / ambiguous
        replies fall back to False — keeping both entries never loses data.
        """
        from src.infrastructure.llm_pool import call_with_fallback

        user_prompt = (
            "Two long-term memory entries scored embedding cosine similarity "
            f"{similarity:.3f}.\n\n"
            f"ENTRY A:\n{summary_a}\n\n"
            f"ENTRY B:\n{summary_b}\n\n"
            "Are they the same memory (one redundant), or distinct? Reply JSON."
        )
        result = await call_with_fallback(
            self._helper_services,
            dict(
                messages=[
                    {"role": "system", "content": MERGE_ARBITER_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                json_mode=True,
                max_tokens=200,
            ),
        )
        try:
            data = json.loads((result.content or "").strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            return False  # conservative: keep both
        return data.get("same") is True

    async def _recently_recalled(self, entry_id: str, kind: str) -> bool:
        """True if *entry_id* was recalled within CORRECTION_RECALL_PRIORITY_DAYS.

        Protects actively-used entries from auto-merge archival: an entry the
        user keeps pulling up is load-bearing, so we keep both near-duplicates
        rather than silently dropping the older one. Fails open (returns
        False) so a recall-log hiccup never blocks dedup.
        """
        try:
            n = await self._store.count_recent_recalls(
                entry_id=entry_id,
                kind=kind,
                since_seconds=C.CORRECTION_RECALL_PRIORITY_DAYS * 86400,
            )
        except Exception:
            return False
        return n > 0

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
        for pid, _, ea, eb, _, _ in await self._store.list_merge_proposals(
            status="pending", limit=10,
        ):
            if {ea, eb} == {keep_id, drop_id}:
                await self._store.resolve_merge_proposal(
                    proposal_id=pid, status="merged",
                )
                break

    # ── L2 / L3 dream synthesis ─────────────────────────────────────────────

    async def _should_run_synthesis(self, *, level: int) -> bool:
        """Idle-aware gate for L2/L3 dream synthesis.

        Three conditions must ALL be met:
          1. Wall-clock cadence: time since last run ≥ configured interval
          2. Idle gate: system input idle ≥ DREAM_SYNTHESIS_IDLE_SEC
          3. Material gate: enough new source entries accumulated

        Hard fallback: if the cadence gap exceeds DREAM_SYNTHESIS_FORCE_
        AFTER_SEC (7 days), conditions 2 and 3 are bypassed — synthesis
        runs regardless, preventing unbounded memory growth for users who
        never stay idle long enough.

        Surviving restarts: the gate uses the wall-clock timestamp of the
        most recent ``dream_runs`` row, not in-memory cycle counts.
        """
        if level == 2:
            min_age_seconds = (
                C.DREAM_L2_EVERY_N_CYCLES * C.DREAM_INTERVAL_SECONDS
            )
            min_new_entries = C.DREAM_L2_MIN_NEW_ENTRIES
        elif level == 3:
            min_age_seconds = (
                C.DREAM_L3_EVERY_N_CYCLES * C.DREAM_INTERVAL_SECONDS
            )
            min_new_entries = C.DREAM_L3_MIN_NEW_ENTRIES
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
            # No run yet on this DB → fire as soon as we have material
            # (still respect idle gate for first run).
            pass
        elif (now - latest) < int(min_age_seconds):
            # Cadence not met — too soon since last run.
            return False

        # ── Hard fallback: force after 7 days regardless of idle/material
        gap = now - latest if latest > 0 else C.DREAM_SYNTHESIS_FORCE_AFTER_SEC
        force = gap >= C.DREAM_SYNTHESIS_FORCE_AFTER_SEC
        if force:
            _logger.info(
                "L%d synthesis force-firing: %dd since last run (cap=%dd)",
                level, gap // 86400, C.DREAM_SYNTHESIS_FORCE_AFTER_SEC // 86400,
            )
            return True

        # ── Idle gate: don't compete with user activity
        from ..personality.input_idle import system_idle_seconds
        idle = system_idle_seconds()
        if idle is None:
            # Non-Windows or GetLastInputInfo failed — skip the idle gate
            # so synthesis still works (degrades to cadence-only).
            pass
        elif idle < C.DREAM_SYNTHESIS_IDLE_SEC:
            return False

        # ── Material gate: enough new entries to justify clustering
        try:
            since_ts = latest if latest > 0 else 0
            new_count = await self._store.count_entries_since(
                kind=EntryKind.MEMORY.value,
                since_ts=since_ts,
                synthesis_level=(0 if level == 2 else 2),
            )
            # Also count knowledge entries — synthesis runs on both kinds
            new_count += await self._store.count_entries_since(
                kind=EntryKind.KNOWLEDGE.value,
                since_ts=since_ts,
                synthesis_level=(0 if level == 2 else 2),
            )
        except Exception:
            _logger.debug("count_entries_since failed L%d", level, exc_info=True)
            # On failure, don't block synthesis — fall through.
            return True

        if new_count < min_new_entries:
            return False

        return True

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
            for row in rows:
                # LTM 2.0: list_entries_by_synthesis_level returns 6-tuple
                # (entry_id, facet, summary, content_text, emb_bytes, frame_os).
                # frame_os is None for frame-agnostic entries.
                entry_id = row[0]
                facet = row[1]
                summary = row[2]
                content_text = row[3]
                emb_bytes = row[4]
                frame_os = row[5] if len(row) > 5 else None
                vec = vec_from_bytes(emb_bytes)
                if vec:
                    decoded.append({
                        "id": entry_id,
                        "facet": facet,
                        "summary": summary,
                        "content": content_text,
                        "vec": vec,
                        "frame_os": frame_os,
                    })

            # All memory entries are now AGENTIC and frame-agnostic;
            # knowledge is also frame-agnostic. Single unpartitioned
            # clustering pass.
            clusters = self._cluster_with_frame_partition(
                decoded, kind=kind,
                threshold=cluster_threshold, min_size=min_size,
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

    @classmethod
    def _cluster_with_frame_partition(
        cls, items: List[dict], *, kind: str,
        threshold: float, min_size: int,
    ) -> List[List[dict]]:
        """LTM 2.0 frame-aware clustering.

        All memory entries (AGENTIC / IDENTITY) and knowledge are
        frame-agnostic — clustering is unpartitioned.
        """
        if kind != EntryKind.MEMORY.value:
            # knowledge: no partition
            return cls._greedy_cluster(
                items, threshold=threshold, min_size=min_size,
            )
        # Memory: all AGENTIC entries clustered without frame partition.
        all_clusters: List[List[dict]] = []
        for c in cls._greedy_cluster(
            items, threshold=threshold, min_size=min_size,
        ):
            all_clusters.append(c)
        all_clusters.sort(key=len, reverse=True)
        return all_clusters

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

        synth_frame = None

        synth_id = await self._store.insert_synthesis_entry(
            kind=kind,
            target_facet=facet,
            summary=verdict["summary"],
            content=verdict["content"],
            synthesis_level=level,
            source_entry_ids=[it["id"] for it in cluster],
            source_run_id=run_id,
            frame=synth_frame,
        )
        # Archive the source entries that the synthesis subsumed.
        # Without this, recall returns BOTH the cluster originals AND the
        # synthesis entry, polluting context with N+1 near-duplicates.
        # We archive (not delete) so the audit trail survives and a future
        # "restore" UI can reverse the synthesis.
        for src_id in (it["id"] for it in cluster):
            try:
                if kind == EntryKind.MEMORY.value:
                    await self._store.archive_memory_entry(
                        src_id,
                        reason=ArchiveReason.SUPERSEDED_BY_SYNTHESIS.value,
                    )
                else:
                    await self._store.archive_knowledge_entry(
                        src_id,
                        reason=ArchiveReason.SUPERSEDED_BY_SYNTHESIS.value,
                    )
                await self._store.set_superseded_by(
                    kind=kind,
                    entry_id=src_id,
                    superseded_by_id=synth_id,
                )
            except Exception:
                _logger.exception(
                    "L%d/%s: archive source %s after synth failed",
                    level, kind, src_id[:8],
                )
        # Embed the new synthesis entry so it shows up in dense recall
        # immediately (no need to wait for backfill).
        # We need to find the new entry's chunk_id; simplest path is to
        # let the next cycle's backfill catch it.
        return True
