"""Dataclasses + enums for the long-term memory system.

Also hosts the small data-shapes used by the activity monitor and the
scheduler subsystems. Both of those write into LTM (as candidates), so
their lifetime is bounded by LTM's; co-locating their model layer here
keeps the surface area small and avoids a parallel ``schema.py`` set.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class CandidateStatus(str, Enum):
    PENDING = "pending"
    TRIAGING = "triaging"
    ACCEPTED_MEMORY = "accepted_memory"
    ACCEPTED_KNOWLEDGE = "accepted_knowledge"
    ACCEPTED_BOTH = "accepted_both"
    # A SESSION_COMPLETE candidate produced no memory/knowledge but its task
    # pattern recurred enough times to be minted as a live (disabled) skill
    # under the Skill root. The skill is a side-channel artifact (a SKILL.md
    # written via SkillRegistry), so this status is only set when nothing else
    # was written.
    ACCEPTED_SKILL = "accepted_skill"
    REJECTED = "rejected"
    FAILED = "failed"


class CandidateSource(str, Enum):
    """Origin of a memory_candidates row.

    The triage prompt and the dream worker's defensive guards both branch on
    this value, so we keep it as an enum rather than free-form strings:

    - ``SESSION_COMPLETE``  : a task ended successfully. Mine durable user
      preferences (memory) and reusable team/project conventions (knowledge).
    - ``SESSION_FAILED``    : a task ended in failure (planner gave up, or
      progress tracker aborted). Knowledge ONLY; memory is hard-blocked
      because user actions on a failed run are not consent.
    - ``RECEPTIONIST_TURN`` : per-message capture from receptionist eval.
      Most user messages are skipped by triage; explicit preferences pass.
    - ``MANUAL_REMEMBER``   : explicit ``/remember <text>`` command. P4.
      Higher trust than ambient sources; bias toward action='create'.
    - ``POST_COMMIT``       : git post-commit hook (P4). Captures project
      conventions from commit msg + diff stat.
    - ``ACTIVITY_OBSERVER`` : background activity capture (P5+, not on
      HandQ's roadmap currently — listed for forward compatibility).
    """
    SESSION_COMPLETE = "session_complete"
    SESSION_FAILED = "session_failed"
    RECEPTIONIST_TURN = "receptionist_turn"
    MANUAL_REMEMBER = "manual_remember"
    POST_COMMIT = "post_commit"
    ACTIVITY_OBSERVER = "activity_observer"


class MemoryDimension(str, Enum):
    AGENTIC = "agentic"   # user's behaviour patterns / preferences / work style (any source)
    IDENTITY = "identity"  # unconditional cross-session directives (always injected)


class KnowledgeCategory(str, Enum):
    DOMAIN = "domain"
    PEOPLE = "people"
    PROCESS = "process"
    CODING = "coding"
    OTHER = "other"


class EntryKind(str, Enum):
    """Discriminator used in embedding_cache.chunk_kind, recall pipeline,
    and archive routing. The enum's values are the canonical strings —
    do NOT introduce parallel KIND_* constants elsewhere.
    """
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    PROCEDURE = "procedure"  # P6 — not implemented in v1


class VerdictAction(str, Enum):
    """The triage LLM's decision per track.

    Mirrors the prompt schema in :mod:`prompts`: each track (memory /
    knowledge) reports one of these four actions independently.

    - ``CREATE``  : insert a new entry. ``content`` is the body.
    - ``UPDATE``  : merge into an existing entry referenced by *_update_id.
                    ``content`` is the COMPLETE merged body, not a diff.
                    Triggers ``version++`` — used as the long-tail
                    protection signal in retriage.
    - ``ARCHIVE`` : the user explicitly contradicted an existing entry.
                    Soft-archives the entry referenced by *_archive_id
                    with ``archived_reason='user_contradicted'``. Reversible
                    via the standard restore path. This is what makes
                    "I don't use ruff anymore" actually delete the
                    "prefer ruff" memory instead of stacking a new one.
    - ``SKIP``    : no new signal. ``worth_*`` must be False.
    """
    CREATE = "create"
    UPDATE = "update"
    ARCHIVE = "archive"
    SKIP = "skip"


class ArchiveReason(str, Enum):
    """Why an entry was soft-deleted (memory_files.archived_reason or
    knowledge_files.archived_reason). Centralised here so the dream-worker
    cleanup paths (auto_dedup, superseded) and the user-driven path
    (user_request) all draw from the same vocabulary.
    """
    USER_REQUEST = "user_request"      # explicit user delete
    AUTO_DEDUP = "auto_dedup"          # merge scanner found a duplicate
    SUPERSEDED = "superseded"          # newer version replaced this one
    SUPERSEDED_BY_SYNTHESIS = "superseded_by_synthesis"  # rolled into an L2/L3 synthesis entry
    CORRUPTED = "corrupted"            # data integrity check failed
    SENSITIVE = "sensitive"            # post-hoc PII detection


class CorrectionKind(str, Enum):
    """The action a correction proposal asks to perform on its target entry.

    Currently a single kind: ``ARCHIVE``. The retroactive correction
    pipeline is intentionally minimal — it can only flag entries to
    soft-delete (with audit-trail reason) so a future SQL prefix restore
    can undo a bad migration in one query. Rewrites and merges were
    designed for a review UI; with conversation-as-interface there is
    no review surface, so they were removed. If a future migration ever
    needs them, append new kinds here and map them in
    ``store.apply_*_correction``.
    """
    ARCHIVE = "archive"


class CorrectionStatus(str, Enum):
    """Lifecycle states for ``correction_proposals.status``.

    - ``PENDING`` : transient — between insert and apply during a single
                    RetriageWorker pass. Should not normally be observed
                    after the worker exits because we auto-apply
                    high-confidence proposals immediately and drop
                    low-confidence ones without persisting.
    - ``APPLIED`` : executed. The target entry is archived; the row is
                    the audit trail.
    - ``STALE``   : when apply ran, the target's ``version`` /
                    ``archived`` had drifted from the snapshot — we
                    refused to overwrite. A future migration can issue
                    a fresh proposal.
    """
    PENDING = "pending"
    APPLIED = "applied"
    STALE = "stale"


@dataclass
class Candidate:
    id: str
    source: str
    source_ref: Optional[str]
    raw_text: str
    hint: Optional[str]
    metadata: dict
    status: CandidateStatus
    retry_count: int
    created_at: int


@dataclass
class Chunk:
    id: str
    entry_id: str
    chunk_index: int
    text: str
    hash: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class Entry:
    id: str
    kind: EntryKind
    summary: str = ""
    content: str = ""                          # joined chunks
    chunks: List[Chunk] = field(default_factory=list)
    dimension: Optional[MemoryDimension] = None       # memory only
    category: Optional[KnowledgeCategory] = None      # knowledge only
    archived: bool = False
    archived_reason: Optional[str] = None
    version: int = 1
    source: str = ""
    source_ref: Optional[str] = None
    created_at: int = 0
    updated_at: int = 0
    score: Optional[float] = None              # filled at recall time
    # LTM 2.0 additions:
    frame_json: Optional[dict] = None          # {os, host, confidence, evidence}
    source_event_id: Optional[str] = None      # → obs_semantic_events(id)
    superseded_by: Optional[str] = None
    synthesis_level: int = 0
    source_entry_ids: List[str] = field(default_factory=list)
    recall_count_30d: int = 0
    principal_ids: List[str] = field(default_factory=list)
    recurrence_count: int = 1


@dataclass
class TriageVerdict:
    """One triage call may emit memory and/or knowledge verdicts."""
    worth_memory: bool = False
    worth_knowledge: bool = False

    # Action strings come from VerdictAction (kept as str for JSON-friendliness;
    # the parser in prompts.py validates them against the enum).
    memory_action: str = VerdictAction.SKIP.value     # 'create' | 'update' | 'archive' | 'skip'
    memory_dimension: Optional[MemoryDimension] = None
    memory_summary: str = ""
    memory_content: str = ""
    memory_update_id: Optional[str] = None
    # Set when memory_action='archive': id of the existing memory entry the
    # user just contradicted. Distinguishes "this is the same topic, refine
    # the existing entry" (update) from "the existing entry is now wrong,
    # remove it" (archive).
    memory_archive_id: Optional[str] = None

    knowledge_action: str = VerdictAction.SKIP.value
    knowledge_category: Optional[KnowledgeCategory] = None
    knowledge_summary: str = ""
    knowledge_content: str = ""
    knowledge_update_id: Optional[str] = None
    knowledge_archive_id: Optional[str] = None

    reason: str = ""


@dataclass
class CorrectionProposal:
    """One proposed correction against an existing entry.

    Generated by ``RetriageWorker`` via ``rule_migrations``; persisted to
    ``correction_proposals``; consumed by IPC / admin CLI / future UI.

    ``payload`` is shape-by-kind JSON (decoded into a dict):
    - ``ARCHIVE``  : ``{"superseded_by_id": str | null}``
    - ``REWRITE``  : ``{"new_summary": str, "new_content_md": str}``
    - ``MERGE``    : ``{"keep_id": str}``
    - ``SYNTHESIS``: reserved.

    ``rationale`` is short LLM-emitted prose explaining WHY. Pass through
    PIIFilter before persisting; ``rationale_pii_scrubbed`` records whether
    a redaction happened.
    """
    id: str
    kind: CorrectionKind
    target_kind: EntryKind                    # MEMORY | KNOWLEDGE
    target_entry_id: str
    target_version: int                       # snapshot at proposal time
    target_archived: bool
    payload: Optional[dict]                   # decoded JSON
    confidence: Optional[float]
    rule_version: int
    parent_run_id: Optional[str]
    rationale: str
    rationale_pii_scrubbed: bool
    status: CorrectionStatus
    created_at: int
    resolved_at: Optional[int] = None
    resolved_by: Optional[str] = None


# ── Activity monitor models ─────────────────────────────────────────────────


class MonitorTier(str, Enum):
    """Per-monitor sampling tier — drives capture cadence.

    The ActivityMonitor's adaptive loop owns one ``MonitorTier`` per physical
    display. Promotion is immediate (any input event near the monitor or
    visible content change → HOT). Demotion is delayed by
    ``ACTIVITY_TIER_DEMOTE_GRACE_SEC`` so a single read-and-think pause
    doesn't ping-pong back to WARM.
    """
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DORMANT = "dormant"


@dataclass
class MonitorInfo:
    """Static-ish description of a physical display.

    The monitor enumerator builds these once (or once per topology change).
    ``index`` is a stable ordinal across the session — we stamp candidates
    with it so the dream worker can correlate samples to a specific
    physical display. ``primary`` is True for the OS-designated primary;
    handy for the UI when surfacing "main monitor" status.
    """
    index: int
    bbox: Tuple[int, int, int, int]   # (left, top, right, bottom) in virtual-screen px
    primary: bool = False
    label: str = ""                    # e.g. "Display 1 (1920x1080, primary)"


@dataclass
class ActivitySample:
    """One accepted observation of a monitor at a point in time.

    A transient in-memory carrier built in ``PersonalityMonitor._ocr_one``
    once an OCR'd frame survives the Jaccard dedup. It is consumed
    immediately to derive the LTM 2.0 frame (via ``_infer_sample_frame``)
    and to write the ``obs_snapshots`` + ``obs_ocr_frames`` rows that feed
    the observation pipeline. It is not persisted or buffered as its own
    record.

    We deliberately do NOT keep the screenshot bytes here — by the time a
    sample exists, the source PNG has been unlinked. The OCR text excerpt
    is the only durable artefact.
    """
    monitor_index: int
    timestamp: int
    foreground_window: str = ""        # window title at capture time, may be ""
    foreground_app: str = ""           # process name, may be ""
    text_excerpt: str = ""             # OCR-derived, capped to OCR_EXCERPT_MAX_CHARS
    tier: MonitorTier = MonitorTier.HOT
    novelty: float = 1.0               # 0..1; how different from previous accepted sample
    sample_kind: str = "activity"     # "activity" | "daily_summary"


# ── Scheduler models ────────────────────────────────────────────────────────


class SchedulerTaskStatus(str, Enum):
    """Last-known terminal state of a scheduled task fire."""
    IDLE = "idle"               # never fired or last firing succeeded
    RUNNING = "running"         # currently in flight
    OK = "ok"                   # last fire completed without raising
    FAILED = "failed"           # last fire raised; ``last_error`` populated
    PENDING = "pending"         # firing time hit, bridge busy; next_run_at unchanged, retried on idle wakeup
    CANCELLED = "cancelled"     # user clicked New Session while a scheduled fire was in flight


@dataclass
class ScheduledTask:
    """One persistent scheduled task — pinned prompt + cadence.

    Persisted to ``%USERPROFILE%\\HandQ\\scheduled_tasks.json`` as a plain
    JSON array via :class:`scheduler.store.ScheduleStore`. Field
    descriptions:

    - ``id``        : opaque uuid string; immutable.
    - ``name``      : human-readable label shown in the UI.
    - ``prompt``    : the original user-typed goal text. Shown verbatim
                      in the UI so users see what they wrote.
    - ``dispatch_prompt`` : optional cleaned variant fed to the agent at
                      fire time. The LLM scheduler-inferer strips
                      relative time language ("一分钟后…") that has
                      already been absorbed into ``schedule``, so the
                      agent doesn't re-interpret it as a second delay.
                      Empty string means "use ``prompt`` as the dispatch
                      text" — back-compat path for tasks created before
                      this field existed.
    - ``schedule``  : grammar string parsed by ``scheduler.schedule``:
                          - "every 30 seconds" / "every 5 minutes" / "every 2 hours"
                          - "daily 09:30" / "daily 09:30:15"
                          - "weekly mon 09:30"
                          - "once at 2026-06-02 14:30" (one-shot — disabled after firing)
    - ``enabled``   : flag; if False the task is skipped without bumping
                      run-counters.
    - ``last_run_at``        : unix seconds — fire-START time of the most
                      recent fire; 0 if never.
    - ``next_run_at``        : unix seconds — when the next fire is due.
                      Pinned at fire-START by ``mark_running`` to enable
                      no-skip catch-up of missed triggers.
    - ``run_count``          : monotonic count of fires.
    - ``failure_count``      : consecutive failures since the last success;
                      auto-disable kicks in at SCHEDULER_MAX_FAILURES_BEFORE_DISABLE.
    - ``last_status`` / ``last_error`` : populated post-fire for the UI.
    - ``created_at`` / ``updated_at``  : audit timestamps. Not used by
                      the runtime; kept for human inspection of the JSON
                      file.
    """
    id: str
    name: str
    prompt: str
    schedule: str
    enabled: bool = True
    last_run_at: int = 0
    next_run_at: int = 0
    run_count: int = 0
    failure_count: int = 0
    last_status: SchedulerTaskStatus = SchedulerTaskStatus.IDLE
    last_error: str = ""
    dispatch_prompt: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_status": self.last_status.value,
            "last_error": self.last_error,
            "dispatch_prompt": self.dispatch_prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScheduledTask":
        try:
            status = SchedulerTaskStatus(d.get("last_status") or "idle")
        except ValueError:
            status = SchedulerTaskStatus.IDLE
        return cls(
            id=str(d["id"]),
            name=str(d.get("name") or ""),
            prompt=str(d.get("prompt") or ""),
            schedule=str(d.get("schedule") or ""),
            enabled=bool(d.get("enabled", True)),
            last_run_at=int(d.get("last_run_at") or 0),
            next_run_at=int(d.get("next_run_at") or 0),
            run_count=int(d.get("run_count") or 0),
            failure_count=int(d.get("failure_count") or 0),
            last_status=status,
            last_error=str(d.get("last_error") or ""),
            dispatch_prompt=str(d.get("dispatch_prompt") or ""),
            created_at=int(d.get("created_at") or int(time.time())),
            updated_at=int(d.get("updated_at") or int(time.time())),
        )


# ── LTM 2.0 observation / entity layer enums ───────────────────────────────


class TriggerKind(str, Enum):
    """What event opened an obs_sessions row."""
    APP_SWITCH = "app_switch"
    SSH_START = "ssh_start"
    RDP_START = "rdp_start"
    IDLE_RESUME = "idle_resume"
    LONG_RUN = "long_run"          # forced rollover after MAX_OPEN_SESSION_MS


class SemanticStatus(str, Enum):
    PENDING = "pending"          # session ended, extractor not yet run
    EXTRACTING = "extracting"    # extractor in flight
    DONE = "done"
    SKIPPED = "skipped"          # too-small / trivial session


class PrincipalKind(str, Enum):
    PERSON = "person"
    MACHINE = "machine"
    PROJECT = "project"


class HostKind(str, Enum):
    SSH = "ssh"
    RDP = "rdp"
    LOCAL = "local"
    FTP = "ftp"


class ObsEventKind(str, Enum):
    FOREGROUND_CHANGE = "foreground_change"
    IDLE_ENTER = "idle_enter"
    IDLE_RESUME = "idle_resume"
    APP_OPEN = "app_open"
    APP_CLOSE = "app_close"
    BROWSER_NAVIGATE = "browser_navigate"
    SSH_CONNECT = "ssh_connect"
    SSH_DISCONNECT = "ssh_disconnect"
    RDP_CONNECT = "rdp_connect"
    RDP_DISCONNECT = "rdp_disconnect"
    NOTIFICATION = "notification"


class SyntheticOrigin(str, Enum):
    """Source of an obs_semantic_events row when no obs_session backs it."""
    USER_MESSAGE = "user_message"
    MANUAL = "manual"
    POST_COMMIT = "post_commit"
    SESSION_COMPLETE = "session_complete"
    SESSION_FAILED = "session_failed"


# ── LTM 2.0 observation / entity dataclasses ────────────────────────────────


@dataclass
class Frame:
    """Execution-environment frame attached to observations and entries.

    A frame answers: "Where (OS, host) was this observed? How confident
    are we?" Encoded as JSON in obs_snapshots.frame_json /
    mem_entries.frame_json; the os/host fields are exposed as STORED
    generated columns so SQL WHERE clauses hit an index.

    Confidence levels:
      0.9-1.0 : high — process signal unambiguous (PowerShell on Windows)
      0.6-0.9 : medium — process + title agree (mintty + user@host)
      0.3-0.6 : low — only weak signal (browser, generic editor)
      <0.3    : ambient — drop or quarantine
    """
    os: str                              # 'windows'|'linux'|'remote'|'unknown'|'any'
    host: str = "unknown"                # 'local'|<hostname>|'unknown'
    confidence: float = 0.0
    evidence: str = ""

    def to_json(self) -> dict:
        return {"os": self.os, "host": self.host,
                "confidence": self.confidence, "evidence": self.evidence}

    @classmethod
    def from_json(cls, d: Optional[dict]) -> Optional["Frame"]:
        if not d:
            return None
        return cls(
            os=str(d.get("os") or "unknown"),
            host=str(d.get("host") or "unknown"),
            confidence=float(d.get("confidence") or 0.0),
            evidence=str(d.get("evidence") or ""),
        )


@dataclass
class Snapshot:
    """One captured frame from PersonalityMonitor → obs_snapshots row.

    Replaces the older flat ActivitySample model. Carries everything the
    observation pipeline needs to flow through session aggregation, semantic
    extraction, and triage: OCR text (separately in obs_ocr_frames), UIA
    structured text (ax_text/parsed_json), window/process metadata, frame
    inference output, and focus-rect optimization markers.
    """
    id: str
    captured_at: int                            # unix ms
    monitor_index: int
    monitor_label: str = ""
    window_title: str = ""
    process_name: str = ""
    browser_url: Optional[str] = None
    top_window_titles: List[str] = field(default_factory=list)
    content_type: Optional[str] = None
    ax_text: Optional[str] = None
    parsed_json: Optional[dict] = None
    frame: Optional[Frame] = None
    focus_rect: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h)
    ocr_used_focus_rect: bool = False
    system_idle_sec: Optional[int] = None
    novelty_score: float = 1.0
    tier: str = "hot"
    storage_tier: str = "hot"
    session_id: Optional[str] = None
    semantic_event_id: Optional[str] = None
    pii_redacted: bool = False
    discarded: bool = False
    discarded_reason: Optional[str] = None


@dataclass
class OcrFrame:
    """One OCR pass result attached to a Snapshot.

    Multiple OcrFrames may share a snapshot_id when both full-monitor and
    focus-rect OCR were run on the same capture (is_focus_rect distinguishes
    them). The embedding column is BLOB-encoded and populated lazily by the
    OCR drain worker after RapidOCR returns.
    """
    id: str
    snapshot_id: str
    text: str
    confidence: Optional[float] = None
    embedding: Optional[bytes] = None
    pipeline_version: str = ""
    captured_at: int = 0
    is_focus_rect: bool = False


@dataclass
class ObsEvent:
    """Discrete state-change event (foreground app changed, idle entered,
    SSH connected, ...). Separate from Snapshot/OcrFrame because events are
    instantaneous markers, not periodic captures.
    """
    id: str
    session_id: Optional[str]
    kind: ObsEventKind
    data: dict
    sort_order: int
    occurred_at: int


@dataclass
class Session:
    """A continuous span of user work with one ``trigger_kind`` (PowerShell
    foreground, SSH'd into longjian6, RDP to APT-LV-SH186, ...). Built by
    SessionAggregator by grouping consecutive Snapshots; closed when input
    idle > 10min OR foreground process changes for >3s. One Session may
    span dozens of Snapshots; one Snapshot belongs to exactly one Session.
    """
    id: str
    session_key: str                            # idempotency token
    trigger_kind: TriggerKind
    started_at: int
    ended_at: Optional[int] = None
    frame_os: Optional[str] = None
    frame_host: Optional[str] = None
    primary_process: Optional[str] = None
    primary_window_title: Optional[str] = None
    semantic_status: SemanticStatus = SemanticStatus.PENDING
    snapshot_count: int = 0
    apps_seen: List[str] = field(default_factory=list)
    principal_ids: List[str] = field(default_factory=list)


@dataclass
class SemanticEvent:
    """LLM-abstracted "what the user did" record. The bridge between raw
    Snapshots/Sessions and durable mem_entries. SemanticEvents are the
    sole input to the triage gate — both observation-origin (real Session
    sessions extracted by LLM) and synthetic-origin (user_message,
    manual_remember, post_commit) events flow through this single shape.
    """
    id: str
    session_id: Optional[str]
    synthetic_origin: Optional[SyntheticOrigin]
    extracted_at: int
    title: str
    description: str = ""
    category: Optional[str] = None
    entities: List[str] = field(default_factory=list)
    apps: List[str] = field(default_factory=list)
    time_range_start: int = 0
    time_range_end: int = 0
    task_worthy: bool = False
    worth_memory: bool = False
    worth_knowledge: bool = False
    worth_skill: bool = False
    frame_os: Optional[str] = None
    frame_host: Optional[str] = None
    frame_confidence: Optional[float] = None
    accepted_entries: List[dict] = field(default_factory=list)


@dataclass
class Principal:
    """One canonical entity (person, machine, project) the user interacts
    with. Populated by:
      - SSH credentials registry (~/.ssh/handq_<host>.yaml → machine)
      - git author email (post_commit → person)
      - @-mentions in user messages (→ person/project)
      - SemanticExtractor LLM entity extraction (→ any)
    """
    id: str
    kind: PrincipalKind
    canonical_name: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    host_kind: Optional[HostKind] = None
    os: Optional[str] = None
    project_root: Optional[str] = None
    description: Optional[str] = None
    first_seen: int = 0
    last_seen: int = 0
    sighting_count: int = 0
    archived: bool = False


@dataclass
class Alias:
    principal_id: str
    alias: str


@dataclass
class Sighting:
    """When and where a Principal was seen."""
    id: str
    principal_id: str
    source_kind: str                            # 'session'|'semantic_event'|'mem_entry'
    source_id: str
    sighted_at: int
    context: Optional[dict] = None
