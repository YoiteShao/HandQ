"""
RewindStore — per-session file-state checkpoints for undo + change auditing.

Purpose (two consumers):
  1. **User-facing undo** (Tier-1.3): the agent runs on the user's real machine
     and writes/edits real files. Before every write/edit the store snapshots
     the file's PRE-operation content; at item boundaries it snapshots the
     POST state. A user can then ask to undo a task's file changes, and the
     store restores the pre-item content — with EXTERNAL-modification conflict
     detection (compare current disk vs the recorded after-snapshot) so a file
     the agent (or the user) touched again after the item is never silently
     clobbered.
  2. **Evidence for completion verification** (Tier-1.1): ``capture_diff``
     turns the accumulated snapshots into a unified-diff + changed-file list so
     the standing-goal judge audits the agent's REAL file output against the
     objective, not just the agent's self-reported prose.

Design decisions (why it looks like this):
  - **Not git.** Most HandQ workspaces are desktop task dirs, not repos. The
    store keys on absolute paths and snapshots content directly — it never
    shells out to git. (Grok's checkpoint store leans on a git baseline; we
    can't assume one exists.)
  - **before = first-write-wins.** ``capture_before`` uses ``setdefault`` so the
    EARLIEST pre-item content is what a rewind restores — if the agent edits a
    file three times in one item, undo takes it back to before edit #1, not
    edit #2. Mirrors Grok's ``RewindPoint.file_snapshots`` (``or_insert``).
  - **Bounded memory.** Files over ``MAX_TRACKED_BYTES`` (1 MB), binary,
    symlinks, and unreadable paths are recorded as a typed *marker* (see
    ``SnapshotKind``) rather than their bytes — so undo refuses to "restore" a
    file it never truly captured, instead of corrupting it or blowing up RAM.
    Borrowed from Grok's ``FileContentState`` enum.
  - **Per-session, keyed by item_id.** One ``RewindPoint`` per task item. Lives
    on ``SessionContext``; dropped when the session closes. Optional JSONL
    persistence to the session dir so an undo survives a UI reconnect.

This module is pure/sync and holds no OS resources beyond the snapshot bytes;
callers off-load the (blocking) disk reads via ``run_in_executor`` where they
already have an executor (write/edit tools), and the store's own file I/O is
small and bounded.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# Files larger than this are marked TooLarge and never held in memory. 1 MB
# matches the edit/grep ceilings already used elsewhere in the tool layer and
# Grok's MAX_TRACKED_TEXT_BYTES — big enough for any real source/config file,
# small enough that a session's distinct write set can't exhaust RAM.
MAX_TRACKED_BYTES = 1_000_000

# Cap on the unified diff produced by capture_diff (fed to an LLM judge). The
# changed-file LIST is always complete; only the inline diff body is truncated.
MAX_DIFF_BYTES = 200_000


class SnapshotKind(str, Enum):
    """What we captured for a path — mirrors Grok's FileContentState so undo
    can refuse to act on content it never truly held rather than corrupting it.
    """

    ABSENT = "absent"        # File did not exist at snapshot time.
    FULL = "full"            # Full text content captured (the normal case).
    BINARY = "binary"        # Non-UTF-8 content; not restorable via text path.
    TOO_LARGE = "too_large"  # Exceeded MAX_TRACKED_BYTES; content not held.
    SYMLINK = "symlink"      # Symlink; we do not follow/restore link targets.
    UNREADABLE = "unreadable"  # OSError on read (permissions, lock, …).


@dataclass
class FileSnapshot:
    """One file's state at one instant. ``content`` is populated only for
    ``SnapshotKind.FULL``; for every other kind it is ``None`` and the kind
    records WHY, so a restore can make a safe, explicit decision."""

    path: str                       # absolute, os.path.realpath-normalized
    kind: SnapshotKind
    content: Optional[str] = None   # text, only when kind == FULL
    sha256: Optional[str] = None    # hex digest of the captured bytes (FULL)
    size: Optional[int] = None
    captured_at: float = field(default_factory=time.time)

    @property
    def restorable(self) -> bool:
        """True when this snapshot can be written back to disk verbatim.

        FULL restores the captured text; ABSENT restores by DELETING the file
        (it did not exist before, so undo removes an agent-created file). The
        marker kinds (binary/too_large/symlink/unreadable) are NOT restorable —
        we never held enough to recreate them faithfully.
        """
        return self.kind in (SnapshotKind.FULL, SnapshotKind.ABSENT)


@dataclass
class RewindPoint:
    """All file snapshots for a single task item.

    ``before``: earliest pre-operation state per path (first-write-wins).
    ``after``:  post-item state per path, used ONLY for external-modification
                detection at rewind time (did disk change since the item ended?).
    """

    item_id: str
    before: Dict[str, FileSnapshot] = field(default_factory=dict)
    after: Dict[str, FileSnapshot] = field(default_factory=dict)
    finalized: bool = False


class RewindConflict(str, Enum):
    """Why a single path could not be cleanly restored."""

    NONE = "none"
    MODIFIED_EXTERNALLY = "modified_externally"   # disk != recorded after-state
    NOT_RESTORABLE = "not_restorable"             # marker snapshot (binary/…)
    RESTORE_FAILED = "restore_failed"             # OSError writing/deleting


@dataclass
class FileRewindResult:
    path: str
    restored: bool
    conflict: RewindConflict = RewindConflict.NONE
    detail: str = ""
    # True when the pre-item snapshot kind was ABSENT — i.e. the file did not
    # exist before the item started, and a successful "restore" DELETED it
    # from disk. The UI needs this to distinguish "put content back" (leaf
    # stays in the tree, ↺ button suppressed) from "file no longer exists"
    # (leaf must be removed from the tree entirely). Meaningful only when
    # restored=True.
    was_absent: bool = False


@dataclass
class RewindReport:
    """Outcome of a rewind_item call — surfaced to the user, never auto-applied
    silently over a conflict."""

    item_id: str
    results: List[FileRewindResult] = field(default_factory=list)

    @property
    def restored_paths(self) -> List[str]:
        return [r.path for r in self.results if r.restored]

    @property
    def conflicts(self) -> List[FileRewindResult]:
        return [r for r in self.results if r.conflict != RewindConflict.NONE]

    @property
    def clean(self) -> bool:
        return not self.conflicts


def _snapshot_path(abs_path: str) -> FileSnapshot:
    """Capture a single path's current state as a FileSnapshot (sync)."""
    real = os.path.realpath(abs_path)
    try:
        if os.path.islink(abs_path):
            return FileSnapshot(path=real, kind=SnapshotKind.SYMLINK)
        if not os.path.exists(abs_path):
            return FileSnapshot(path=real, kind=SnapshotKind.ABSENT)
        size = os.path.getsize(abs_path)
        if size > MAX_TRACKED_BYTES:
            return FileSnapshot(path=real, kind=SnapshotKind.TOO_LARGE, size=size)
        with open(abs_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return FileSnapshot(path=real, kind=SnapshotKind.UNREADABLE)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileSnapshot(path=real, kind=SnapshotKind.BINARY, size=len(raw))

    return FileSnapshot(
        path=real,
        kind=SnapshotKind.FULL,
        content=text,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )


class RewindStore:
    """Per-session file checkpoints. Constructed in FlowControllerV2.start and
    held on SessionContext. All methods are sync (the small disk reads are
    fast + bounded); the write/edit tools that call ``capture_before`` already
    run inside an executor, and item begin/end runs on the agent loop where a
    sub-millisecond snapshot is negligible.
    """

    def __init__(self) -> None:
        self._points: Dict[str, RewindPoint] = {}   # item_id -> point
        self._order: List[str] = []                 # item_ids in creation order
        self._current: Optional[RewindPoint] = None

    @property
    def current_item_id(self) -> Optional[str]:
        """Item id of the currently-open rewind point, or None between items.

        Tools that emit per-file-touch events to the UI bus read this to tag
        events with the task item they belong to. Safe to read from any thread
        under the GIL — begin_item/end_item swap ``_current`` atomically.
        """
        return self._current.item_id if self._current is not None else None

    # ── Item lifecycle (called by PersistentAgent._execute_item) ─────────────

    def begin_item(self, item_id: str) -> None:
        """Open a fresh RewindPoint for *item_id* and make it current."""
        point = RewindPoint(item_id=item_id)
        self._points[item_id] = point
        self._order.append(item_id)
        self._current = point

    def end_item(self) -> None:
        """Finalize the current point: snapshot the AFTER state of every touched
        path (for later external-modification detection) and close it."""
        point = self._current
        if point is None:
            return
        for path in list(point.before.keys()):
            point.after[path] = _snapshot_path(path)
        point.finalized = True
        self._current = None

    # ── Pre-write capture (called by write_tool / edit_tool) ─────────────────

    def capture_before(self, abs_path: str) -> None:
        """Record the pre-operation state of *abs_path* under the current item.

        First-write-wins: repeated captures within one item keep the EARLIEST
        state, so undo takes the file back to before the item started touching
        it. No-op when no item is active (defensive — e.g. a tool call outside
        the normal item loop in a test).
        """
        point = self._current
        if point is None:
            return
        real = os.path.realpath(abs_path)
        if real in point.before:
            return  # earliest state already captured
        point.before[real] = _snapshot_path(abs_path)

    # ── Undo (called via the bridge on explicit user request) ────────────────

    def can_rewind(self, item_id: str) -> bool:
        point = self._points.get(item_id)
        return bool(point and point.before)

    def last_item_id(self) -> Optional[str]:
        """Most recently begun item that captured at least one file (for a
        bare 'undo the last change' request)."""
        for item_id in reversed(self._order):
            if self.can_rewind(item_id):
                return item_id
        return None

    def paths_for_item(self, item_id: str) -> List[str]:
        """Absolute paths this item captured a pre-state for (undo targets).

        Used by the undo orchestration to acquire the per-path write locks
        BEFORE calling rewind_item, so a restore never races an in-flight
        write/edit to the same path.
        """
        point = self._points.get(item_id)
        return list(point.before.keys()) if point else []

    def rewind_item(self, item_id: str) -> RewindReport:
        """Restore every file touched during *item_id* to its pre-item state.

        For each captured path:
          - Compare current disk content to the recorded AFTER snapshot. If they
            differ, something changed the file after the item ended (the agent
            in a later item, or the user) → report MODIFIED_EXTERNALLY and DO
            NOT overwrite. The caller surfaces this for user confirmation.
          - Otherwise restore the BEFORE state: write FULL content back, or
            delete the file if it was ABSENT before the item.
          - Marker snapshots (binary/too_large/symlink/unreadable) are reported
            NOT_RESTORABLE and skipped.

        Does not clear the point — a rewind is itself auditable and a user may
        re-inspect. Returns a RewindReport; never raises for per-file errors.
        """
        point = self._points.get(item_id)
        report = RewindReport(item_id=item_id)
        if point is None:
            return report

        for path, before in point.before.items():
            if not before.restorable:
                report.results.append(FileRewindResult(
                    path=path, restored=False,
                    conflict=RewindConflict.NOT_RESTORABLE,
                    detail=f"snapshot kind={before.kind.value}",
                ))
                continue

            # External-modification detection: current disk vs recorded after.
            after = point.after.get(path)
            current = _snapshot_path(path)
            if after is not None and not _same_state(current, after):
                report.results.append(FileRewindResult(
                    path=path, restored=False,
                    conflict=RewindConflict.MODIFIED_EXTERNALLY,
                    detail=(
                        "file changed since the task ended; not overwriting "
                        "without confirmation"
                    ),
                ))
                continue

            try:
                if before.kind == SnapshotKind.ABSENT:
                    if os.path.exists(path):
                        os.remove(path)
                else:  # FULL
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(before.content or "")
                report.results.append(FileRewindResult(
                    path=path, restored=True,
                    was_absent=(before.kind == SnapshotKind.ABSENT),
                ))
            except OSError as exc:
                report.results.append(FileRewindResult(
                    path=path, restored=False,
                    conflict=RewindConflict.RESTORE_FAILED, detail=str(exc),
                ))

        return report

    # ── Diff / evidence (called by the completion verifier, Tier-1.1) ────────

    def capture_diff(self, item_ids: Optional[List[str]] = None) -> "DiffEvidence":
        """Build a unified-diff + changed-file list from before→current state
        across the given items (default: all items in creation order).

        This is the ground-truth evidence the standing-goal judge audits: the
        agent SAYS it produced X; this shows what the files ACTUALLY became. The
        changed-file list is always complete; the inline diff body is truncated
        at MAX_DIFF_BYTES (with an explicit marker) so a huge change can't blow
        the judge's context — but the truncation note is appended AFTER the cap
        so it is never itself elided (borrowed from Grok's evidence packet).
        """
        import difflib

        ids = item_ids if item_ids is not None else list(self._order)
        # Earliest before-state per path across the selected items.
        earliest: Dict[str, FileSnapshot] = {}
        for item_id in ids:
            point = self._points.get(item_id)
            if not point:
                continue
            for path, snap in point.before.items():
                earliest.setdefault(path, snap)

        changed_files: List[str] = []
        chunks: List[str] = []
        for path, before in earliest.items():
            current = _snapshot_path(path)
            if _same_state(before, current):
                continue
            changed_files.append(path)
            before_text = before.content if before.kind == SnapshotKind.FULL else ""
            current_text = current.content if current.kind == SnapshotKind.FULL else ""
            # Non-text before/after (binary/too_large/absent) → summarize, don't
            # attempt a line diff we can't faithfully render.
            if before.kind not in (SnapshotKind.FULL, SnapshotKind.ABSENT) or \
               current.kind not in (SnapshotKind.FULL, SnapshotKind.ABSENT):
                chunks.append(
                    f"# {path}: {before.kind.value} -> {current.kind.value} "
                    f"(non-text; content diff omitted)\n"
                )
                continue
            diff = difflib.unified_diff(
                before_text.splitlines(keepends=True),
                current_text.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
            )
            chunks.append("".join(diff))

        body = "\n".join(c for c in chunks if c)
        truncated = False
        if len(body) > MAX_DIFF_BYTES:
            body = body[:MAX_DIFF_BYTES]
            truncated = True
        return DiffEvidence(
            changed_files=changed_files,
            diff=body,
            truncated=truncated,
        )


def _same_state(a: FileSnapshot, b: FileSnapshot) -> bool:
    """Two snapshots represent the same on-disk state.

    FULL vs FULL compares sha256; anything involving a marker kind compares the
    kind alone (we can't diff content we didn't hold). ABSENT vs ABSENT matches.
    """
    if a.kind != b.kind:
        return False
    if a.kind == SnapshotKind.FULL:
        return a.sha256 == b.sha256
    return True  # same non-FULL kind → treat as unchanged for diff/conflict


@dataclass
class DiffEvidence:
    """Ground-truth file changes for completion verification."""

    changed_files: List[str]
    diff: str
    truncated: bool = False

    def render(self) -> str:
        """Format as the CHANGED_FILES / DIFF evidence block consumed by the
        goal-verifier prompt. Empty-safe: an all-no-op task renders an explicit
        'no file changes' line so the judge can distinguish 'nothing changed'
        from 'evidence unavailable'."""
        if not self.changed_files:
            return "CHANGED_FILES: (none — this task produced no file changes)\n"
        lines = ["CHANGED_FILES:"]
        lines.extend(f"  {p}" for p in self.changed_files)
        lines.append("")
        lines.append("DIFF:")
        lines.append(self.diff or "(no textual diff)")
        if self.truncated:
            lines.append(
                f"\n[diff truncated at {MAX_DIFF_BYTES} bytes; "
                f"{len(self.changed_files)} file(s) changed in total]"
            )
        return "\n".join(lines)
