"""SessionDigest — the small, structured trace a destroyed session leaves
behind so a later message can resume it (see docs/session_resume_design.md).

Deliberately NOT a belief cache: no `_turns`, no live OS handles (browser /
ssh / shell / background task_id). Every field here is either verbatim user
text or a plain dataclass the Coordinator/Agent already produce
(TaskResult, TaskSpec, GoalState) — nothing is re-narrated by an LLM.

Field-length caps exist because one pathological session's `final_answer`
or `conversation` entry was observed at ~390K chars during real-data
validation (scratch_resume/14_digest_stats.py) — without a cap, a single
outlier session could make its digest disproportionately expensive to
load/index at startup.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("handq.controller_v2.session_digest")

# Per-field character cap applied to free-text content (conversation turns,
# final_answer, key_findings, issues). Keeps one outlier session's digest
# from ballooning — see module docstring.
MAX_FIELD_CHARS = 2000


def _cap(text: Any) -> str:
    s = text if isinstance(text, str) else str(text or "")
    if len(s) <= MAX_FIELD_CHARS:
        return s
    return s[:MAX_FIELD_CHARS] + f"…[+{len(s) - MAX_FIELD_CHARS}]"


@dataclass
class SessionDigest:
    """Schema v1. See docs/session_resume_design.md §6.1 for the design
    rationale behind each field."""

    schema_version: int = 1
    session_id: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    workspace_dir: str = ""
    # Top-level filenames only (one listdir at destroy time) — fills the
    # gap between what the agent registered as an artifact and what it
    # actually left on disk. See §6.3 "workspace 的双角色".
    workspace_files: List[str] = field(default_factory=list)
    status: str = "destroyed"  # "destroyed" | "crashed"
    conversation: List[Dict[str, str]] = field(default_factory=list)
    completed: List[Dict[str, Any]] = field(default_factory=list)
    current: Optional[Dict[str, Any]] = None
    pending: List[Dict[str, Any]] = field(default_factory=list)
    active_tools: List[str] = field(default_factory=list)
    active_goal: Optional[Dict[str, Any]] = None
    # The one piece of "belief" carried over — the summary the agent was
    # already operating on at close time, not a fresh re-narration.
    agent_summary: Optional[str] = None

    # ── Serialisation ────────────────────────────────────────────────────

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "SessionDigest":
        """Defensive parse: a missing/malformed sub-field falls back to its
        default rather than failing the whole digest (mirrors
        ScheduledTask.from_dict's per-field .get() tolerance)."""
        d: Dict[str, Any] = json.loads(raw)
        return cls(
            schema_version=int(d.get("schema_version") or 1),
            session_id=str(d.get("session_id") or ""),
            title=str(d.get("title") or ""),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            workspace_dir=str(d.get("workspace_dir") or ""),
            workspace_files=list(d.get("workspace_files") or []),
            status=str(d.get("status") or "destroyed"),
            conversation=list(d.get("conversation") or []),
            completed=list(d.get("completed") or []),
            current=d.get("current"),
            pending=list(d.get("pending") or []),
            active_tools=list(d.get("active_tools") or []),
            active_goal=d.get("active_goal"),
            agent_summary=d.get("agent_summary"),
        )

    # ── Field capping (applied by callers before construction, kept here
    #    so both FlowController and tests share one definition) ─────────

    @staticmethod
    def cap(text: Any) -> str:
        return _cap(text)

    # ── Atomic disk I/O ──────────────────────────────────────────────────

    # Deliberately NOT "session_state.json" — that name collides with a
    # pre-existing legacy per-session file (old {"steps": [...]} format,
    # written by a now-removed mechanism, still present on disk for
    # historical sessions). from_json()'s defensive per-field .get() means
    # loading a legacy file never crashes (it just yields an empty-looking
    # digest), but the same filename for two incompatible schemas is a
    # trap for anyone inspecting a session directory by hand. "digest.json"
    # is unambiguous and cannot collide with the legacy name.
    DIGEST_FILENAME = "digest.json"

    def save(self, session_dir: Path) -> None:
        """Atomic write via tmp-file + os.replace (mirrors
        infrastructure/scheduler/store.py's ScheduleStore._flush_locked).

        Best-effort by design: callers (FlowControllerV2.destroy /
        _checkpoint_digest) must not let a failed digest write derail
        session teardown or item completion. Errors are logged, not
        raised. Windows note: os.replace on a target held open by another
        process can raise — logged and swallowed for the same reason.
        """
        target = Path(session_dir) / self.DIGEST_FILENAME
        payload = self.to_json()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".digest-", suffix=".json",
                dir=str(target.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, target)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            _logger.warning(
                "[SessionDigest] save failed for %s (status=%s); "
                "continuing without a persisted digest for this checkpoint",
                target, self.status, exc_info=True,
            )

    @classmethod
    def load(cls, session_dir: Path) -> Optional["SessionDigest"]:
        """Returns None if no digest exists or it fails to parse. A
        corrupt file is left in place (not deleted) so it can be inspected
        — resume degrades to "no digest found", same as a fresh session."""
        target = Path(session_dir) / cls.DIGEST_FILENAME
        if not target.exists():
            return None
        try:
            raw = target.read_text(encoding="utf-8")
            return cls.from_json(raw)
        except Exception:
            _logger.warning(
                "[SessionDigest] load failed for %s; treating as absent",
                target, exc_info=True,
            )
            return None
