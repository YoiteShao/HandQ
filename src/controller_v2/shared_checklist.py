"""
SharedCheckList — thread-safe shared state between Agent and Planner.

Agent:   reads current_item, marks done, reports result; reads active_tools
         and loads newly added tool implementations into its registry.
Planner: writes items, modifies future items, reads results, signals completion;
         writes to active_tools (append-only; never deactivates).
"""
import asyncio
import collections
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AbstractSet, Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

from ..models.token_usage import TokenUsage
from .agent_utils import ProgressConcern, TurnDigest


# Canonical interrupt tag — used by PersistentAgent when writing ItemResult.issues
# and by PlannerMixin structural guard when detecting interrupt-driven exits.
INTERRUPTED_BY_PLANNER = "Interrupted by planner"

# Cap on how many completed ItemResults are rendered into the planner context.
# _results itself stays append-only (other readers depend on the full list);
# only the rendered tail is bounded so planner context does not grow O(n) with
# item count on long sessions.
PLANNER_RESULT_RENDER_LIMIT = 20


@dataclass
class CheckListItem:
    """A single unit of work in the CheckList — analogous to the old Step."""

    item_id: str
    instruction: str
    expected_outcomes: List[str] = field(default_factory=list)
    supplement: str = ""
    planner_reasoning: str = ""
    # Planner-only metadata (agent does not see these via to_agent_message;
    # ssh_target is surfaced separately via the per-item host hint provider)
    risk_assessment: str = ""
    ssh_target: str = ""
    # Advisory constraints derived from user directives + prior lessons +
    # skill non-negotiables. Injected into the agent's per-turn reminder
    # (via IterationAdvisor) as an always-visible "警钟长鸣" block. Weak
    # constraint: the agent may violate, but must bail out with plan_feedback
    # (early-end + trigger replan) rather than silently push through.
    directives: List[str] = field(default_factory=list)

    @classmethod
    def from_planner_dict(cls, data: dict) -> "CheckListItem":
        def _coerce_list(v: Any) -> List[str]:
            if isinstance(v, list):
                return [str(x) for x in v if x is not None]
            if v is None:
                return []
            return [str(v)]

        item_id = data.get("item_id") or data.get("step_id") or str(_uuid.uuid4())
        return cls(
            item_id=item_id,
            instruction=data.get("instruction", ""),
            expected_outcomes=_coerce_list(data.get("expected_outcomes")),
            supplement=data.get("supplement", data.get("step_supplement", "")),
            planner_reasoning=data.get("planner_reasoning", ""),
            risk_assessment=data.get("risk_assessment", ""),
            ssh_target=data.get("ssh_target", ""),
            directives=_coerce_list(data.get("directives")),
        )

    def to_agent_message(self) -> str:
        """Format this item as a user message for the agent."""
        # Surface the planner's decision rationale FIRST so the agent reads it
        # before forming a plan from the instruction. Anchoring on a previous
        # item's behaviour is the most common drift source — putting the
        # planner's "do/do-not" guidance at the top (instead of trailing the
        # instruction) gives it the best chance of being honoured.
        parts: List[str] = []
        reasoning = (self.planner_reasoning or "").strip()
        if reasoning:
            parts.append(f"[Planner Note — READ FIRST]\n{reasoning}")
        parts.append(f"[New Task]\n{self.instruction}")
        if self.supplement:
            parts.append(f"[Input]\n{self.supplement}")
        if self.expected_outcomes:
            outcomes = "\n".join(f"  - {e}" for e in self.expected_outcomes)
            parts.append(f"[Expected Outcomes]\n{outcomes}")
        return "\n\n".join(parts)


@dataclass
class ItemResult:
    """Structured result produced by Agent when an item completes."""

    item_id: str
    success: bool
    factual_outcome: List[str] = field(default_factory=list)
    key_findings: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    # Optional agent→planner advisory (see TurnOutcome.plan_feedback). Non-empty
    # when the agent — having read a skill body or discovered a fact this item —
    # judges the planner's REMAINING items should change. Surfaced to the
    # planner via get_checklist_context_for_planner on both success and failure.
    plan_feedback: str = ""
    iterations: int = 0
    token_usage: Optional[TokenUsage] = None
    completed_at: Optional[datetime] = None


class SharedCheckList:
    """Thread-safe shared state between Agent and Planner.

    Concurrency model:
      - replace_post_current is async and holds asyncio.Lock during its
        critical section (no awaits inside)
      - Agent blocks on _item_available (asyncio.Event) when no current item
      - Planner signals _item_available after appending/inserting
      - Interrupt uses a separate asyncio.Event (no lock needed for set/clear)
      - mark_current_done is sync + lock-free (single-threaded asyncio model
        means it cannot be interleaved by another coroutine)
    """

    def __init__(self) -> None:
        self._items: List[CheckListItem] = []
        self._results: List[ItemResult] = []
        self._current_index: int = 0

        # Pending items dropped by replace_post_current when the planner
        # re-emits a shorter/different tail (see planner_prompts.py's
        # "re-emit the whole post-current list" contract — anything NOT
        # re-included is intentionally abandoned, not failed). UI-only
        # bookkeeping: never read by planner/agent-facing surfaces
        # (items/total_items/get_pending_items/etc.), only by
        # get_ui_snapshot. Each entry is (anchor_item_id, item) where
        # anchor_item_id is head[-1].item_id at drop time — stable forever
        # since head only grows by append.
        self._skipped: List[Tuple[str, CheckListItem]] = []
        self._lock = asyncio.Lock()
        self._item_available = asyncio.Event()
        self._interrupt_event = asyncio.Event()
        self._interrupt_acked = asyncio.Event()
        self._interrupt_acked.set()  # initially no pending interrupt
        self._interrupt_reason: str = ""  # set alongside _interrupt_event
        self._on_item_done_callbacks: List[Callable[[ItemResult], Any]] = []

        # UI-facing checklist snapshot bus. Fired on every checklist mutation
        # (item completed, pending tail replaced) so a UI delegate can render a
        # live task panel. Callbacks receive the rendered snapshot (list of
        # {item_id, instruction, status}) — see get_ui_snapshot. Append-only
        # fan-out with per-callback exception isolation, same as on_item_done.
        self._on_checklist_changed_callbacks: List[Callable[[List[Dict[str, Any]]], Any]] = []

        # Tools are append-only session state. Planner declares activations;
        # agent reacts via on_tools_changed (loads tool registry + runs
        # session-level provider prep). Per-host prep (e.g. SSH for a specific
        # ssh_target) is item-driven separately.
        #
        # Skills are NOT tracked here: the progressive-disclosure model injects
        # the enabled menu + standing bodies live from the SkillRegistry every
        # turn, and the agent pulls non-standing bodies on demand via read_skill.
        # There is no activate_skills path.
        self._active_tools: Set[str] = set()
        self._on_tools_changed_callbacks: List[Callable[[List[str]], Any]] = []

        # Latest user message — verbatim. Written by Orchestrator on every
        # user turn; read by PersistentAgent to inject a `[User Original
        # Request]` grounding block so the agent can resolve translation
        # nuance the planner's item instruction may have flattened. Single-
        # slot, last-write-wins; no history is needed because the planner
        # already owns the full conversation_history.
        self._latest_user_message: str = ""

        # ── Progress-sense bus (Tier-0 digests + Tier-1 concern) ─────────────
        # Bounded ring of per-turn mechanical digests written by the agent each
        # turn; the planner reads the in-flight tail and the watcher snapshots
        # it. maxlen caps memory regardless of how long an item runs.
        self._turn_digests: Deque[TurnDigest] = collections.deque(maxlen=24)
        # Single-slot watcher verdict, last-write-wins (same lock-free rationale
        # as mark_current_done). Cleared at each item boundary so a stale verdict
        # never leaks into the next item.
        self._progress_concern: Optional[ProgressConcern] = None
        self._on_progress_concern_callbacks: List[Callable[[ProgressConcern], Any]] = []

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def total_items(self) -> int:
        return len(self._items)

    @property
    def completed_count(self) -> int:
        return len(self._results)

    @property
    def has_pending(self) -> bool:
        """True iff there are pending items beyond the current one."""
        return self._current_index + 1 < len(self._items)

    @property
    def items(self) -> List[CheckListItem]:
        return list(self._items)

    @property
    def active_tools(self) -> AbstractSet[str]:
        """Read-only snapshot of currently activated tool names."""
        return frozenset(self._active_tools)

    # ── Tools (planner writes, agent reads diff) ─────────────────────────────

    def activate_tools(self, names: Iterable[str]) -> List[str]:
        """Append-only tool activation. Returns names that were newly added.

        Tools accumulate over the session, never deactivate. Agent reacts to
        the diff by loading new tools into its registry and running session-
        level provider prep (one prep per tool).

        Caller is responsible for filtering against the tool registry; this
        method does not validate names.
        """
        new = [n for n in names if n and n not in self._active_tools]
        if not new:
            return []
        self._active_tools.update(new)
        for cb in self._on_tools_changed_callbacks:
            try:
                cb(new)
            except Exception:
                pass
        return new

    # ── Latest user message (orchestrator writes, agent reads) ──────────────

    def set_latest_user_message(self, text: str) -> None:
        """Record the latest verbatim user message.

        Sync, lock-free — same single-threaded asyncio assumption as
        ``mark_current_done``. Called by Orchestrator on every user turn.
        Empty string is a valid input (clears the field).
        """
        self._latest_user_message = text or ""

    def get_latest_user_message(self) -> str:
        """Return the most recently recorded user message verbatim.

        Empty string when no user message has been recorded yet (e.g. agent
        driven without a user turn in tests).
        """
        return self._latest_user_message

    # ── Agent interface ──────────────────────────────────────────────────────

    async def wait_for_current_item(self) -> CheckListItem:
        """Block until a current item is available. Never returns None.

        The agent calls this in a loop. When all items are consumed, the
        agent blocks here indefinitely until planner adds new items.
        Only way to break out is CancelledError (flow teardown).
        """
        while True:
            async with self._lock:
                if self._current_index < len(self._items):
                    return self._items[self._current_index]
            self._item_available.clear()
            await self._item_available.wait()

    def mark_current_done(self, result: ItemResult) -> None:
        """Mark current item as done. Synchronous, lock-free.

        Safe without a lock under V2's single-threaded asyncio model:
        - This function is sync and runs atomically (no await points).
        - The only other writer that touches `_items` is `replace_post_current`
          which holds `self._lock` and never modifies the in-progress slot.

        If V2 ever moves to multi-threaded execution or this function gains
        an await, reintroduce a CAS or wrap in a lock.
        """
        result.completed_at = datetime.now()
        self._results.append(result)
        self._current_index += 1

        # Notify callbacks
        for cb in self._on_item_done_callbacks:
            try:
                cb(result)
            except Exception:
                pass

        # Signal item_available in case agent is re-entering wait
        self._item_available.set()

        # UI snapshot — item just transitioned done → next item becomes current.
        self._notify_checklist_changed()

    def check_interrupt(self) -> bool:
        """Non-blocking check if interrupt is pending."""
        return self._interrupt_event.is_set()

    def acknowledge_interrupt(self) -> str:
        """Agent acknowledges interrupt — allows planner to send next one.

        Returns the interrupt reason (may be empty string).
        """
        reason = self._interrupt_reason
        self._interrupt_reason = ""
        self._interrupt_event.clear()
        self._interrupt_acked.set()
        return reason

    def get_current_item(self) -> Optional[CheckListItem]:
        """Non-blocking read of current item (None if exhausted or not yet populated)."""
        if self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    # ── Planner interface ────────────────────────────────────────────────────

    async def replace_post_current(
        self, items: List[CheckListItem]
    ) -> None:
        """Replace _items[_current_index+1:] with the given list.

        This is the SINGLE checklist mutation op available to the planner.
        Behaviour:
          - Empty checklist: items become the initial list, _current_index=0
            (the first item becomes "current" once agent picks it up).
          - Non-empty: replaces the pending tail, leaving completed items
            and the current in-progress item untouched.
          - items=[] is a valid input — clears all pending items (e.g. when
            the planner judges the task is winding down).

        The current in-progress item is NEVER modified by this method. To
        abort the current item, the caller must issue a separate
        interrupt_agent() call AFTER applying replace_post_current; this
        order ensures the new pending tail is already in place by the time
        the agent processes the interrupt and advances _current_index, so
        the agent's next pickup is the new head rather than any leftover
        old pending item.
        """
        async with self._lock:
            new_pending = list(items)
            if self.total_items == 0:
                # First plan — items become the initial list.
                self._items = new_pending
                self._current_index = 0
            else:
                head = self._items[: self._current_index + 1]
                old_tail = self._items[self._current_index + 1:]
                new_ids = {it.item_id for it in new_pending}
                anchor_id = head[-1].item_id
                for dropped in old_tail:
                    if dropped.item_id not in new_ids:
                        self._skipped.append((anchor_id, dropped))
                self._items = head + new_pending
            if self._items and self._current_index < len(self._items):
                self._item_available.set()

        # UI snapshot — fire outside the lock so callback work (snapshot render
        # + UI forwarding) never extends the critical section or risks re-entry.
        self._notify_checklist_changed()

    async def interrupt_agent(self, reason: str = "") -> None:
        """Send interrupt signal to agent. Waits for ack before returning."""
        # Rate limit: wait for previous interrupt to be acked
        await self._interrupt_acked.wait()
        self._interrupt_acked.clear()
        self._interrupt_reason = reason
        self._interrupt_event.set()

    # ── Shared read interface ────────────────────────────────────────────────

    def get_completed_results(self) -> List[ItemResult]:
        """Get all completed item results (for planner evaluation)."""
        return list(self._results)

    # ── Progress-sense bus ────────────────────────────────────────────────────

    def append_turn_digest(self, digest: TurnDigest) -> None:
        """Agent writes one mechanical digest per turn (cheap, synchronous)."""
        self._turn_digests.append(digest)

    def get_turn_digests(self) -> List[TurnDigest]:
        """Snapshot of the in-flight digest ring (safe to iterate after).

        Returns a plain list copy so a caller that awaits between reads is not
        exposed to concurrent appends mutating a live deque mid-iteration.
        """
        return list(self._turn_digests)

    def set_progress_concern(self, concern: ProgressConcern) -> None:
        """Watcher writes a verdict (last-write-wins) + fires callbacks.

        Mirrors mark_current_done's dispatch: lock-free single-slot write, then
        best-effort callback fan-out with per-callback exception isolation so a
        throwing subscriber can't break the watcher coroutine.
        """
        self._progress_concern = concern
        for cb in self._on_progress_concern_callbacks:
            try:
                cb(concern)
            except Exception:
                pass

    def get_progress_concern(self) -> Optional[ProgressConcern]:
        return self._progress_concern

    def clear_progress_concern(self) -> None:
        """Drop the current verdict (called at each item boundary)."""
        self._progress_concern = None

    def get_pending_items(self) -> List[CheckListItem]:
        """Get all items not yet started (for planner to modify/view)."""
        return self._items[self._current_index + 1:]

    def get_checklist_context_for_planner(self) -> str:
        """Format full checklist state for planner prompt injection."""
        lines = []
        lines.append(f"## CheckList Status ({self.completed_count}/{self.total_items} done)")
        lines.append("")

        # Completed items — render only the most recent tail so the planner
        # context stays bounded on long sessions (_results itself is full).
        rendered = self._results[-PLANNER_RESULT_RENDER_LIMIT:]
        omitted = len(self._results) - len(rendered)
        if omitted > 0:
            lines.append(f"... ({omitted} earlier completed items omitted)")
        for result in rendered:
            status_tag = "Done" if result.success else "Failed"
            lines.append(f"[{status_tag}] item={result.item_id}")
            if result.factual_outcome:
                lines.append(f"  Outcome: {'; '.join(result.factual_outcome)}")
            if result.artifacts:
                lines.append(f"  Artifacts: {', '.join(result.artifacts)}")
            if result.key_findings:
                lines.append(f"  Findings: {'; '.join(result.key_findings)}")
            if not result.success and result.issues:
                lines.append(f"  Issues: {'; '.join(result.issues)}")
            if result.plan_feedback:
                lines.append(
                    f"  → AGENT FLAGS FOR PLANNER: {result.plan_feedback}"
                )

        # Current item
        current = self.get_current_item()
        if current:
            lines.append(f"\n[In Progress] item={current.item_id}")
            lines.append(f"  Instruction: {current.instruction}")
            # In-flight mechanical view — the planner's only window into a
            # running item (it is otherwise woken only at item boundaries).
            recent = [d for d in self._turn_digests if d.item_id == current.item_id][-5:]
            for d in recent:
                tools = ",".join(d.tool_names) if d.tool_names else "none"
                lines.append(
                    f"  · iter {d.iteration}: tools={tools} "
                    f"ok={d.success_count} fail={d.fail_count} "
                    f"new_artifact={d.produced_new_artifact} "
                    f"info_gain={d.info_gain} "
                    f"no_progress_streak={d.no_progress_streak}"
                )
            concern = self._progress_concern
            if concern is not None and concern.item_id == current.item_id:
                lines.append(f"  ⚠ PROGRESS CONCERN [{concern.verdict}]: {concern.rationale}")
                if concern.suggest_replan:
                    lines.append("    → watcher suggests re-planning the remaining steps")
                if concern.suggest_interrupt:
                    lines.append("    → watcher suggests interrupting the current item")

        # Future items
        future = self.get_pending_items()
        if future:
            lines.append(f"\n[Pending] ({len(future)} items)")
            for item in future:
                lines.append(f"  - {item.item_id}: {item.instruction[:80]}")

        return "\n".join(lines)

    def get_recent_results_for_agent(self, limit: int = 10) -> str:
        """Render last N ItemResults as a compact boundary block for agent context.

        Differs from get_checklist_context_for_planner: no pending tail, no
        in-progress line, single-line per item with status glyph. Designed to
        slot into the agent's per-turn instruction message — gives the agent a
        cross-item view of what already happened (success/fail/interrupt) so it
        does not retry dead approaches or lose track of session boundaries.
        """
        if not self._results:
            return ""
        recent = self._results[-limit:]
        lines = ["[Item Boundary History]"]
        for r in recent:
            if r.success:
                tag = "✓"
            elif r.issues and r.issues[0].startswith(INTERRUPTED_BY_PLANNER):
                tag = "⊗"
            else:
                tag = "✗"
            outcome = "; ".join(r.factual_outcome[:2]) if r.factual_outcome else ""
            issue = r.issues[0] if r.issues else ""
            detail = (outcome or issue)[:120]
            lines.append(f"{tag} [{r.item_id}] {detail}")
        return "\n".join(lines)

    # ── UI snapshot ──────────────────────────────────────────────────────────

    def get_ui_snapshot(self) -> List[Dict[str, Any]]:
        """Render the checklist as a flat list for a live UI task panel.

        Status is derived from the index-aligned (_items, _current_index,
        _results) triple — len(_results) == _current_index, and
        replace_post_current never touches completed or in-progress slots:

          - i < len(_results)        → completed: "interrupted" when the result
                                       was an interrupt exit, else "done"/"failed"
          - i == _current_index      → "running"
          - otherwise                → "pending"

        Additionally, items dropped from the pending tail by
        replace_post_current (planner re-emitted a shorter/different tail)
        are rendered with status "skipped", spliced in immediately after the
        item that was "current" at the moment they were dropped (see
        _skipped) — same relative position they occupied before being cut,
        so a shrinking tail stays legible instead of silently vanishing.

        Each entry: {"item_id", "instruction", "status"}.
        """
        skipped_by_anchor: Dict[str, List[CheckListItem]] = {}
        for anchor_id, skipped_item in self._skipped:
            skipped_by_anchor.setdefault(anchor_id, []).append(skipped_item)

        snapshot: List[Dict[str, Any]] = []
        done_count = len(self._results)
        for i, item in enumerate(self._items):
            if i < done_count:
                result = self._results[i]
                if result.issues and result.issues[0].startswith(INTERRUPTED_BY_PLANNER):
                    status = "interrupted"
                elif result.success:
                    status = "done"
                else:
                    status = "failed"
            elif i == self._current_index:
                status = "running"
            else:
                status = "pending"
            snapshot.append({
                "item_id": item.item_id,
                "instruction": item.instruction,
                "status": status,
            })
            for skipped_item in skipped_by_anchor.get(item.item_id, []):
                snapshot.append({
                    "item_id": skipped_item.item_id,
                    "instruction": skipped_item.instruction,
                    "status": "skipped",
                })
        return snapshot

    def _notify_checklist_changed(self) -> None:
        """Build the UI snapshot once and fan it out to subscribers.

        Best-effort with per-callback exception isolation (mirrors the
        on_item_done fan-out). Called from mark_current_done and from
        replace_post_current (outside the lock).
        """
        if not self._on_checklist_changed_callbacks:
            return
        snapshot = self.get_ui_snapshot()
        for cb in self._on_checklist_changed_callbacks:
            try:
                cb(snapshot)
            except Exception:
                pass

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on_item_done(self, callback: Callable[[ItemResult], Any]) -> None:
        self._on_item_done_callbacks.append(callback)

    def on_checklist_changed(
        self, callback: Callable[[List[Dict[str, Any]]], Any]
    ) -> None:
        """Register a callback fired on every checklist mutation.

        Callback receives the rendered UI snapshot (see get_ui_snapshot) so a
        UI delegate can paint a live task panel. Fired from mark_current_done
        and replace_post_current. Best-effort, exception-isolated — a throwing
        subscriber cannot break the agent/planner coroutines.
        """
        self._on_checklist_changed_callbacks.append(callback)

    def on_progress_concern(self, callback: Callable[[ProgressConcern], Any]) -> None:
        """Register a callback fired when the watcher sets a concern.

        Symmetric to on_item_done: the Orchestrator subscribes to set its
        planner trigger, so a divergence verdict wakes the planner loop the same
        way an item completion does — without giving the watcher (or the agent)
        any direct handle on the orchestrator.
        """
        self._on_progress_concern_callbacks.append(callback)

    def on_tools_changed(self, callback: Callable[[List[str]], Any]) -> None:
        """Register callback fired when new tools are activated.

        Callback receives the delta (newly added tool names). Used by
        PersistentAgent to load new tool implementations into its registry
        and trigger session-level provider prep (e.g. SSH cred dir setup,
        browser session warm-up) — runs ONCE per tool, not per item.
        """
        self._on_tools_changed_callbacks.append(callback)
