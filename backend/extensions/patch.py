"""Patch staging + conflict detection + approval gate (report §8.9 / §9.3).

The Phase 3 closing piece: code-modifying workflows must default to
**read-only audit → propose patch → user approval → apply → run tests →
rollback on failure**. The pieces of that pipeline live across several
modules; this one fills the staging / approval / conflict-detection gap so a
template (or a planned draft) can wire them together.

Three types and one workflow node:

  * ``Patch``        — one proposed change to one file (or a delete).
  * ``Conflict``     — two patches that disagree about the same file.
  * ``PatchStager``  — collects patches, detects conflicts, exposes the
                       staged set for approval and apply.
  * ``ApprovalGate`` — a ``Node`` that calls a frontend approval callback,
                       routing to ``approved`` / ``rejected`` / ``conflict``.

Plus two helpers:

  * ``make_stage_patch_tool`` — a ``ToolSpec`` an audit-mode subagent calls
    to *propose* a write. Means we can run the existing modify pattern in
    staged mode (tool list excludes ``write``/``edit``, includes
    ``stage_patch``) without touching the subagent code.
  * ``apply_patches`` — writes the staged set to disk, optionally taking a
    checkpoint snapshot first so a downstream test failure can roll back
    in one call (``engine.checkpoint``).

Pure orchestration: no LLM. Filesystem effects are gated by the approval
callback and the checkpoint manager — the report's "safety boundaries
first" principle (§11.5) is enforced at this layer rather than smeared
into every subagent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..agent.contracts import ToolResult, ToolSpec
from ..engine.blackboard import Blackboard
from .checkpoint import CheckpointManager
from ..orchestration.planning.workflow import NodeResult


# ── data types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Patch:
    """One proposed change to one file.

    ``new_content=None`` is a delete — the staged set will remove the file
    on apply. ``rationale`` and ``source`` are bookkeeping (which subagent
    proposed this and why) so the approval UI can surface authorship.
    """

    path: str
    new_content: Optional[str]
    rationale: str = ""
    source: str = ""

    @property
    def is_delete(self) -> bool:
        return self.new_content is None


@dataclass(frozen=True)
class Conflict:
    """Two staged patches that disagree about the same file."""

    path: str
    sources: tuple[str, ...]
    detail: str = ""


class PatchStager:
    """Collects proposed patches and detects conflicts.

    Per-path *replacement* semantics: if the same source restages a file
    with new content, the latest wins (it's a single subagent revising its
    own proposal). A *different* source restaging the same path with
    different content is a conflict — recorded but not erased; the
    approval gate decides what to do.
    """

    def __init__(self) -> None:
        self._patches: dict[str, Patch] = {}
        self._conflicts: list[Conflict] = []

    def stage(self, patch: Patch) -> None:
        existing = self._patches.get(patch.path)
        if existing is not None and existing.source != patch.source \
                and existing.new_content != patch.new_content:
            self._conflicts.append(Conflict(
                path=patch.path,
                sources=(existing.source or "?", patch.source or "?"),
                detail=(f"different content staged by {existing.source!r} and "
                        f"{patch.source!r}"),
            ))
        self._patches[patch.path] = patch

    def patches(self) -> list[Patch]:
        """Patches in stage-order, deduped by path (last write wins)."""
        return list(self._patches.values())

    def conflicts(self) -> list[Conflict]:
        return list(self._conflicts)

    @property
    def has_conflicts(self) -> bool:
        return bool(self._conflicts)

    def __len__(self) -> int:
        return len(self._patches)


# ── stage_patch tool (subagent-callable) ───────────────────────────────────


def make_stage_patch_tool(
    stager: PatchStager, *, source: str = "",
) -> ToolSpec:
    """Build a ``ToolSpec`` that stages a patch instead of writing it.

    A "staged-mode" template gives subagents this tool in place of
    ``write``/``edit`` so all proposed changes flow through the stager. The
    tool itself doesn't touch the filesystem — it only records the proposal
    and reports back to the model so it can keep planning. Apply happens
    later, behind the approval gate.
    """

    async def runner(*, path: str, content: Optional[str] = None,
                     rationale: str = "") -> ToolResult:
        stager.stage(Patch(
            path=path, new_content=content,
            rationale=rationale, source=source,
        ))
        action = "delete" if content is None else "rewrite"
        return ToolResult(
            call_id="",
            ok=True,
            output=f"staged {action} of {path}",
            metadata={"path": path, "is_delete": content is None},
        )

    return ToolSpec(
        name="stage_patch",
        description="Propose a change to a file. Staged for review, not applied yet.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {
                    "type": ["string", "null"],
                    "description": "New file content. Pass null to delete the file.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this change satisfies the goal.",
                },
            },
            "required": ["path"],
        },
        run=runner,
        # The tool itself only mutates an in-memory stager; the on-disk
        # mutation happens in apply_patches under the approval gate.
        mutating=False,
        concurrency_safe=False,
    )


# ── approval gate (workflow node) ──────────────────────────────────────────


ApprovalCallback = Callable[[PatchStager], Awaitable[bool]]


class ApprovalGate:
    """``Node`` that pauses for frontend approval of the staged patch set.

    Routing labels (the runner uses these to pick the next node):
      * ``approved`` — callback returned True; downstream ``apply`` node runs.
      * ``rejected`` — callback returned False; downstream ``cleanup`` runs.
      * ``conflict`` — stager found conflicts; the user shouldn't see a
                       broken proposal, so we route to a repair branch
                       before re-asking for approval.

    The stager lives on the Blackboard under ``state_key`` (default
    ``"patches"``). Wiring is the template's job — the gate only observes.
    """

    def __init__(
        self,
        name: str,
        *,
        callback: ApprovalCallback,
        state_key: str = "patches",
    ) -> None:
        self.name = name
        self._callback = callback
        self._key = state_key

    async def run(self, bb: Blackboard) -> NodeResult:
        stager = bb.state.get(self._key)
        if not isinstance(stager, PatchStager):
            return NodeResult(
                ok=False, route="rejected",
                summary=f"no PatchStager at bb.state[{self._key!r}]",
            )
        if stager.has_conflicts:
            return NodeResult(
                ok=False, route="conflict",
                summary=f"{len(stager.conflicts())} conflict(s) detected",
                data={"conflicts": [c.path for c in stager.conflicts()]},
            )
        approved = await self._callback(stager)
        if approved:
            return NodeResult(
                ok=True, route="approved",
                summary=f"approved {len(stager)} patch(es)",
            )
        return NodeResult(
            ok=False, route="rejected",
            summary=f"user rejected {len(stager)} patch(es)",
        )


# ── apply ──────────────────────────────────────────────────────────────────


@dataclass
class ApplyOutcome:
    """Result of applying a staged set."""

    ok: bool
    applied: list[str] = field(default_factory=list)
    error: Optional[str] = None
    checkpoint_id: Optional[str] = None


async def apply_patches(
    stager: PatchStager,
    *,
    checkpoint: Optional[CheckpointManager] = None,
    label: str = "patch_apply",
    root: Optional[Path | str] = None,
) -> ApplyOutcome:
    """Write the staged set to disk, optionally snapshotted for rollback.

    With a ``CheckpointManager`` set, the pre-image of every affected path is
    captured first; a later test-phase failure rolls the apply back via
    ``checkpoint.rollback_to(outcome.checkpoint_id)``. Without a checkpoint
    we still apply, but rollback becomes the caller's problem.

    ``root`` lets a template confine writes to a subtree (e.g. a worktree).
    Paths outside the root are rejected before any write happens, so a stray
    absolute path in a Patch can't escape the sandbox.
    """
    root_path = Path(root).resolve() if root else None
    paths = [p.path for p in stager.patches()]

    if root_path is not None:
        for path in paths:
            if not Path(path).resolve().is_relative_to(root_path):
                return ApplyOutcome(
                    ok=False,
                    error=f"path {path!r} escapes root {str(root_path)!r}",
                )

    cp_id: Optional[str] = None
    if checkpoint is not None and paths:
        cp_id = checkpoint.capture(paths, label=label)

    applied: list[str] = []
    try:
        for patch in stager.patches():
            target = Path(patch.path)
            if patch.is_delete:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(patch.new_content or "", encoding="utf-8")
            applied.append(patch.path)
    except Exception as exc:
        # On any write failure, immediately roll back what we already did.
        if cp_id is not None and checkpoint is not None:
            checkpoint.rollback_to(cp_id)
        return ApplyOutcome(
            ok=False, applied=applied, error=str(exc), checkpoint_id=cp_id,
        )

    return ApplyOutcome(ok=True, applied=applied, checkpoint_id=cp_id)
