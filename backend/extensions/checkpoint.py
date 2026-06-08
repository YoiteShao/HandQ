"""Checkpoint / per-operation rollback (00-SYSTEM-DESIGN §4.3 "最重要的设计缺口";
report §8.7/§9.3 staged-diff-review → rollback).

Yansu's granular-control pillar we were missing: every mutating tool_use first
saves a **pre-image** of the files it is about to touch; an undo pops that
pre-image and restores the bytes (or deletes the file if it did not exist). This
is *逐工具回滚* — per-operation undo, not a single coarse "reset the run".

Design (deliberately minimal, fully deterministic — no git, no LLM, no daemon):

  * ``capture(paths, label=...)`` reads the current bytes of each path (marking
    ones that don't exist) and pushes a ``Checkpoint`` onto a LIFO stack. Call it
    immediately BEFORE a mutating op.
  * ``undo()`` pops the latest checkpoint and restores every path to its
    pre-image — re-creating deleted files, deleting created ones, rewriting
    changed ones.
  * ``rollback_to(id)`` repeatedly undoes down to AND including the named
    checkpoint, so a whole phase can be reverted atomically.

The stack is the authority; nothing here mutates a tool — callers (the tool
dispatcher / ProgrammaticTool) wrap their writes with ``capture`` so undo is
exact regardless of what the op did to the bytes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class FileSnapshot:
    """Pre-image of one path at checkpoint time.

    ``existed`` distinguishes "file was there with these bytes" from "file did
    not exist" — restoring the latter means *deleting* whatever now sits there.
    """

    path: str               # absolute, normalized
    existed: bool
    content: Optional[bytes]  # the bytes before the op; None iff not existed


@dataclass
class Checkpoint:
    id: str
    label: str
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)


@dataclass
class RollbackReport:
    """What an undo actually did, for the trace / receptionist."""

    ok: bool
    checkpoints_undone: int = 0
    restored: list[str] = field(default_factory=list)   # paths rewritten/recreated
    deleted: list[str] = field(default_factory=list)     # created-then-removed paths
    error: Optional[str] = None


class CheckpointManager:
    """LIFO stack of file pre-images for per-operation undo.

    ``working_dir`` (when set) is the root relative paths resolve against, so a
    caller can capture ``"notes.txt"`` and undo restores
    ``<working_dir>/notes.txt``.
    """

    def __init__(self, *, working_dir: Optional[str] = None) -> None:
        self._working_dir = working_dir
        self._stack: list[Checkpoint] = []
        self._counter = 0

    # ── capture ────────────────────────────────────────────────────────────--
    def _resolve(self, path: str) -> str:
        if self._working_dir and not os.path.isabs(path):
            path = os.path.join(self._working_dir, path)
        return os.path.normpath(os.path.abspath(path))

    def _read_preimage(self, abs: str) -> FileSnapshot:
        if os.path.isfile(abs_path := abs):
            with open(abs_path, "rb") as fh:
                return FileSnapshot(path=abs_path, existed=True, content=fh.read())
        return FileSnapshot(path=abs, existed=False, content=None)

    def capture(self, paths: Iterable[str], *, label: str = "") -> str:
        """Push a checkpoint holding the current pre-image of each path.

        De-duplicates paths within a checkpoint (first read wins, which is the
        true pre-image for that op). Returns the new checkpoint id."""
        self._counter += 1
        cp_id = f"cp-{self._counter}"
        snaps: dict[str, FileSnapshot] = {}
        for p in paths:
            abs_path = self._resolve(p)
            if abs_path not in snaps:
                snaps[abs_path] = self._read_preimage(abs_path)
        self._stack.append(Checkpoint(id=cp_id, label=label, snapshots=snaps))
        return cp_id

    # ── undo ───────────────────────────────────────────────────────────────--
    def _restore_one(self, snap: FileSnapshot, report: RollbackReport) -> None:
        if snap.existed:
            os.makedirs(os.path.dirname(snap.path) or ".", exist_ok=True)
            with open(snap.path, "wb") as fh:
                fh.write(snap.content or b"")
            report.restored.append(snap.path)
        else:
            # Did not exist at capture → the op created it → delete to undo.
            if os.path.isfile(snap.path):
                os.remove(snap.path)
                report.deleted.append(snap.path)

    def _apply(self, cp: Checkpoint, report: RollbackReport) -> None:
        for snap in cp.snapshots.values():
            self._restore_one(snap, report)
        report.checkpoints_undone += 1

    def undo(self) -> RollbackReport:
        """Pop and revert the most recent checkpoint. No-op if the stack empty."""
        report = RollbackReport(ok=True)
        if not self._stack:
            return report
        self._apply(self._stack.pop(), report)
        return report

    def rollback_to(self, checkpoint_id: str) -> RollbackReport:
        """Undo every checkpoint down to AND including ``checkpoint_id``.

        Reverts newest-first so overlapping pre-images compose correctly: the
        oldest pre-image of a path is applied last and therefore wins."""
        report = RollbackReport(ok=True)
        if all(cp.id != checkpoint_id for cp in self._stack):
            report.ok = False
            report.error = f"unknown checkpoint {checkpoint_id!r}"
            return report
        while self._stack:
            cp = self._stack.pop()
            self._apply(cp, report)
            if cp.id == checkpoint_id:
                break
        return report

    # ── introspection ────────────────────────────────────────────────────────
    @property
    def depth(self) -> int:
        return len(self._stack)

    def history(self) -> list[tuple[str, str]]:
        """(id, label) of live checkpoints, oldest first — for progress views."""
        return [(cp.id, cp.label) for cp in self._stack]

    def clear(self) -> None:
        """Drop all pre-images (e.g. once a run is committed/accepted)."""
        self._stack.clear()
