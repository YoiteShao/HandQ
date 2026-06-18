"""
SharedCheckList — thread-safe shared state between Agent and Planner.

Agent:   reads current_item, marks done, reports result; reads active_skills
         and injects newly added skill bodies into its observation history.
Planner: writes items, modifies future items, reads results, signals completion;
         writes to active_skills (append-only; never deactivates).
"""
import asyncio
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AbstractSet, Any, Callable, Dict, Iterable, List, Optional, Set

from ..models.token_usage import TokenUsage


# Canonical interrupt tag — used by PersistentAgent when writing ItemResult.issues
# and by PlannerMixin structural guard when detecting interrupt-driven exits.
INTERRUPTED_BY_PLANNER = "Interrupted by planner"


@dataclass
class CheckListItem:
    """A single unit of work in the CheckList — analogous to the old Step."""

    item_id: str
    instruction: str
    expected_outcomes: List[str] = field(default_factory=list)
    supplement: str = ""
    planner_reasoning: str = ""
    # Planner-only metadata (agent does not see these)
    risk_assessment: str = ""
    ssh_target: str = ""

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
        )

    def to_agent_message(self) -> str:
        """Format this item as a user message for the agent."""
        content = f"[New Task]\n{self.instruction}"
        if self.supplement:
            content += f"\n\n[Input]\n{self.supplement}"
        if self.expected_outcomes:
            outcomes = "\n".join(f"  - {e}" for e in self.expected_outcomes)
            content += f"\n\n[Expected Outcomes]\n{outcomes}"
        return content


@dataclass
class ItemResult:
    """Structured result produced by Agent when an item completes."""

    item_id: str
    success: bool
    factual_outcome: List[str] = field(default_factory=list)
    key_findings: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
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
        self._lock = asyncio.Lock()
        self._item_available = asyncio.Event()
        self._interrupt_event = asyncio.Event()
        self._interrupt_acked = asyncio.Event()
        self._interrupt_acked.set()  # initially no pending interrupt
        self._interrupt_reason: str = ""  # set alongside _interrupt_event
        self._on_item_done_callbacks: List[Callable[[ItemResult], Any]] = []

        # Skills are append-only session state. Planner (Stage 2) adds names
        # via activate_skills(); agent reads active_skills to detect deltas
        # and injects bodies as observations. There is no deactivate path.
        self._active_skills: Set[str] = set()
        self._on_skills_changed_callbacks: List[Callable[[List[str]], Any]] = []

        # Tools follow the same append-only pattern as skills. Planner declares
        # activations; agent reacts via on_tools_changed (loads tool registry +
        # runs session-level provider prep). Per-host prep (e.g. SSH for a
        # specific ssh_target) is item-driven separately.
        self._active_tools: Set[str] = set()
        self._on_tools_changed_callbacks: List[Callable[[List[str]], Any]] = []

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
    def active_skills(self) -> AbstractSet[str]:
        """Read-only snapshot of currently activated skill names."""
        return frozenset(self._active_skills)

    @property
    def active_tools(self) -> AbstractSet[str]:
        """Read-only snapshot of currently activated tool names."""
        return frozenset(self._active_tools)

    # ── Skills (planner writes, agent reads diff) ────────────────────────────

    def activate_skills(self, names: Iterable[str]) -> List[str]:
        """Append-only skill activation. Returns names that were newly added.

        Skills are session-level state. Once activated they stay until session
        teardown — this matches the agent's append-only history semantics
        (skill bodies injected as observations cannot be unwritten).

        Caller is responsible for filtering against SkillRegistry; this method
        does not validate names.
        """
        new = [n for n in names if n and n not in self._active_skills]
        if not new:
            return []
        self._active_skills.update(new)
        for cb in self._on_skills_changed_callbacks:
            try:
                cb(new)
            except Exception:
                pass
        return new

    # ── Tools (planner writes, agent reads diff) ─────────────────────────────

    def activate_tools(self, names: Iterable[str]) -> List[str]:
        """Append-only tool activation. Returns names that were newly added.

        Mirror of activate_skills: tools accumulate over the session, never
        deactivate. Agent reacts to the diff by loading new tools into its
        registry and running session-level provider prep (one prep per tool).

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
                self._items = head + new_pending
            if self._items and self._current_index < len(self._items):
                self._item_available.set()

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

    def get_pending_items(self) -> List[CheckListItem]:
        """Get all items not yet started (for planner to modify/view)."""
        return self._items[self._current_index + 1:]

    def get_checklist_context_for_planner(self) -> str:
        """Format full checklist state for planner prompt injection."""
        lines = []
        lines.append(f"## CheckList Status ({self.completed_count}/{self.total_items} done)")
        lines.append("")

        # Completed items
        for result in self._results:
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

        # Current item
        current = self.get_current_item()
        if current:
            lines.append(f"\n[In Progress] item={current.item_id}")
            lines.append(f"  Instruction: {current.instruction}")

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

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on_item_done(self, callback: Callable[[ItemResult], Any]) -> None:
        self._on_item_done_callbacks.append(callback)

    def on_skills_changed(self, callback: Callable[[List[str]], Any]) -> None:
        """Register callback fired when new skills are activated.

        Callback receives the delta (newly added names). Used by PersistentAgent
        to inject skill bodies as observations when planner activates a new
        skill mid-session.
        """
        self._on_skills_changed_callbacks.append(callback)

    def on_tools_changed(self, callback: Callable[[List[str]], Any]) -> None:
        """Register callback fired when new tools are activated.

        Callback receives the delta (newly added tool names). Used by
        PersistentAgent to load new tool implementations into its registry
        and trigger session-level provider prep (e.g. SSH cred dir setup,
        browser session warm-up) — runs ONCE per tool, not per item.
        """
        self._on_tools_changed_callbacks.append(callback)
