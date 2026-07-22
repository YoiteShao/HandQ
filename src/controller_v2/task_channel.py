"""
TaskChannel — the Coordinator↔Agent IPC channel (NOT a todo list).

This is the shared rendezvous point between two independent asyncio loops:
  - the Coordinator (front desk: triage + relay), and
  - the PersistentAgent (the worker that owns and executes the task).

It is deliberately NOT a task-decomposition tree or a user-facing todo. The
agent owns decomposition internally and tracks its own plan via the `todo_write`
tool. What lives here is only what the two loops must SHARE:
  - the current task + a pending queue (the Coordinator hands work in),
  - the asyncio.Events that let the loops rendezvous and interrupt each other,
  - completed TaskResults (the agent reports back; the Coordinator reads them),
  - append-only tool activation, the latest user message, and the progress bus.

(Was formerly `SharedCheckList` — renamed to reflect what it actually is.)
"""
import asyncio
import collections
from dataclasses import dataclass, field
from datetime import datetime
from typing import AbstractSet, Any, Callable, Deque, Dict, Iterable, List, Optional, Set

from ..models.token_usage import TokenUsage
from .agent_utils import ProgressConcern, TurnDigest


# Canonical interrupt tag — used by PersistentAgent when writing TaskResult.issues
# and by TaskChannel's own rendering to distinguish an interrupted item from an
# ordinary failure.
INTERRUPTED_BY_COORDINATOR = "Interrupted by coordinator"

# Cap on how many completed TaskResults are rendered into the coordinator
# context. _results itself stays append-only (other readers depend on the full
# list); only the rendered tail is bounded so context does not grow O(n) with
# task count on long sessions.
COORDINATOR_RESULT_RENDER_LIMIT = 20


@dataclass
class TaskSpec:
    """A queued unit of work handed from the Coordinator to the agent.

    Just a verbatim instruction + id — the agent owns decomposition,
    tool selection, and target-host discovery internally.
    """

    item_id: str
    instruction: str
    # Precomputed PRECISE-tier (rerank=True) LTM recall block for this task's
    # instruction, computed by the Coordinator CONCURRENTLY with the INTENT
    # call that queued it (see Orchestrator._build_precise_long_term_block)
    # and handed down here so the Agent starts execution with a rerank-
    # quality block instead of running its own recall. ``None`` when LTM
    # is unavailable/empty/timed out — the Agent proceeds without LTM
    # context in that case, same as any other recall failure.
    ltm_block: Optional[str] = None
    # Non-None when this item was mechanically re-queued by the Coordinator's
    # standing-goal check-in loop (see Orchestrator._requeue_goal) rather than
    # a fresh user message — the Nth re-queue of the same goal condition.
    # Purely observational: it lets the Agent's reasoning and the task panel
    # show "this is attempt #N at the same standing goal" without changing
    # any Agent decision logic.
    goal_iteration: Optional[int] = None
    # Non-None when this item was re-queued by a schedule_wakeup timer firing
    # (a self-paced /loop tick) rather than a fresh user message — the Nth
    # wakeup of the same self-paced loop. Purely observational, like
    # goal_iteration: it surfaces "loop tick #N" in the task panel and agent
    # reasoning without changing any decision logic.
    wakeup_iteration: Optional[int] = None

    def to_agent_message(self) -> str:
        """Format this task as a user message for the agent."""
        if self.goal_iteration is not None:
            prefix = f"[New Task] (goal check-in #{self.goal_iteration})"
        elif self.wakeup_iteration is not None:
            prefix = f"[New Task] (loop tick #{self.wakeup_iteration})"
        else:
            prefix = "[New Task]"
        return f"{prefix}\n{self.instruction}"



@dataclass
class TaskResult:
    """Structured result produced by the agent when a task completes."""

    item_id: str
    success: bool
    # Tool-grounded audit bullets — what tools verified this task.
    verification: List[str] = field(default_factory=list)
    key_findings: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    # User-facing answer body — markdown shown in the chat bubble. Populated
    # from the completion turn's ``final_answer`` field. Empty on non-success
    # paths (interrupt / error / iteration cap).
    final_answer: str = ""
    # Optional agent→coordinator advisory (see TurnOutcome.plan_feedback).
    # Non-empty when the agent — having read a skill body or discovered a fact —
    # judges the REMAINING plan should change. Surfaced via
    # render_state_for_coordinator on both success and failure.
    plan_feedback: str = ""
    iterations: int = 0
    token_usage: Optional[TokenUsage] = None
    completed_at: Optional[datetime] = None


class TaskChannel:
    """Coordinator↔Agent IPC channel + synchronization primitive.

    Concurrency model:
      - replace_post_current is async and holds asyncio.Lock during its
        critical section (no awaits inside)
      - Agent blocks on _item_available (asyncio.Event) when no current task
      - Coordinator signals _item_available after appending/inserting
      - Interrupt uses a separate asyncio.Event (no lock needed for set/clear)
      - mark_current_done is sync + lock-free (single-threaded asyncio model
        means it cannot be interleaved by another coroutine)
    """

    def __init__(self) -> None:
        self._items: List[TaskSpec] = []
        self._results: List[TaskResult] = []
        self._current_index: int = 0

        self._lock = asyncio.Lock()
        self._item_available = asyncio.Event()
        self._interrupt_event = asyncio.Event()
        self._interrupt_acked = asyncio.Event()
        self._interrupt_acked.set()  # initially no pending interrupt
        self._interrupt_reason: str = ""  # set alongside _interrupt_event
        self._on_item_done_callbacks: List[Callable[[TaskResult], Any]] = []

        # UI-facing task-panel snapshot bus. Fired on every mutation (task
        # completed, pending tail replaced) so a UI delegate can render a live
        # panel. Callbacks receive the rendered snapshot (list of
        # {item_id, instruction, status}) — see get_ui_snapshot. Append-only
        # fan-out with per-callback exception isolation, same as on_item_done.
        self._on_tasks_changed_callbacks: List[Callable[[List[Dict[str, Any]]], Any]] = []

        # Tools are append-only session state. Coordinator declares activations;
        # agent reacts via on_tools_changed (loads tool registry + runs
        # session-level provider prep). Per-host prep (e.g. SSH for a specific
        # ssh_target) is task-driven separately.
        #
        # Skills are NOT tracked here: the progressive-disclosure model injects
        # the enabled menu + standing bodies live from the SkillRegistry every
        # turn, and the agent pulls non-standing bodies on demand via read_skill.
        self._active_tools: Set[str] = set()
        self._on_tools_changed_callbacks: List[Callable[[List[str]], Any]] = []

        # Latest user message — verbatim. Written by Orchestrator on every user
        # turn; read by PersistentAgent to inject a `[User Original Request]`
        # grounding block. Single-slot, last-write-wins.
        self._latest_user_message: str = ""

        # ── Progress-sense bus (Tier-0 digests + Tier-1 concern) ─────────────
        # Bounded ring of per-turn mechanical digests written by the agent each
        # turn; the coordinator reads the in-flight tail and the watcher
        # snapshots it. maxlen caps memory regardless of how long a task runs.
        self._turn_digests: Deque[TurnDigest] = collections.deque(maxlen=24)
        # Single-slot watcher verdict, last-write-wins. Cleared at each task
        # boundary so a stale verdict never leaks into the next task.
        self._progress_concern: Optional[ProgressConcern] = None
        self._on_progress_concern_callbacks: List[Callable[[ProgressConcern], Any]] = []

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def total_items(self) -> int:
        return len(self._items)

    @property
    def completed_count(self) -> int:
        return len(self._results)

    @property
    def has_pending(self) -> bool:
        """True iff there are pending tasks beyond the current one."""
        return self._current_index + 1 < len(self._items)

    @property
    def items(self) -> List[TaskSpec]:
        return list(self._items)

    @property
    def active_tools(self) -> AbstractSet[str]:
        """Read-only snapshot of currently activated tool names."""
        return frozenset(self._active_tools)

    # ── Tools (coordinator writes, agent reads diff) ─────────────────────────

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
        """Record the latest verbatim user message. Sync, lock-free."""
        self._latest_user_message = text or ""

    def get_latest_user_message(self) -> str:
        """Return the most recently recorded user message verbatim."""
        return self._latest_user_message

    # ── Agent interface ──────────────────────────────────────────────────────

    async def wait_for_current_item(self) -> TaskSpec:
        """Block until a current task is available. Never returns None.

        The agent calls this in a loop. When all tasks are consumed, the agent
        blocks here indefinitely until the coordinator adds new work. Only way
        to break out is CancelledError (flow teardown).
        """
        while True:
            async with self._lock:
                if self._current_index < len(self._items):
                    return self._items[self._current_index]
            self._item_available.clear()
            await self._item_available.wait()

    def mark_current_done(self, result: TaskResult) -> None:
        """Mark the current task as done. Synchronous, lock-free.

        Safe without a lock under V2's single-threaded asyncio model:
        - This function is sync and runs atomically (no await points).
        - The only other writer that touches `_items` is `replace_post_current`
          which holds `self._lock` and never modifies the in-progress slot.
        """
        result.completed_at = datetime.now()
        self._results.append(result)
        self._current_index += 1

        for cb in self._on_item_done_callbacks:
            try:
                cb(result)
            except Exception:
                pass

        # Signal item_available in case agent is re-entering wait
        self._item_available.set()

        # UI snapshot — task just transitioned done → next task becomes current.
        self._notify_tasks_changed()

    def check_interrupt(self) -> bool:
        """Non-blocking check if interrupt is pending."""
        return self._interrupt_event.is_set()

    def acknowledge_interrupt(self) -> str:
        """Agent acknowledges interrupt — allows coordinator to send next one.

        Returns the interrupt reason (may be empty string).
        """
        reason = self._interrupt_reason
        self._interrupt_reason = ""
        self._interrupt_event.clear()
        self._interrupt_acked.set()
        return reason

    def get_current_item(self) -> Optional[TaskSpec]:
        """Non-blocking read of the current task (None if exhausted/unpopulated)."""
        if self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    # ── Coordinator interface ─────────────────────────────────────────────────

    async def replace_post_current(
        self, items: List[TaskSpec]
    ) -> None:
        """Replace _items[_current_index+1:] with the given list.

        The SINGLE mutation op available to the coordinator.
          - Empty channel: items become the initial list, _current_index=0.
          - Non-empty: replaces the pending tail, leaving completed tasks and
            the current in-progress task untouched.
          - items=[] clears all pending tasks.

        The current in-progress task is NEVER modified. To abort it, the caller
        issues a separate interrupt_agent() AFTER replace_post_current; this
        order ensures the new pending tail is in place by the time the agent
        processes the interrupt and advances _current_index, so its next pickup
        is the new head rather than a leftover old pending task.
        """
        async with self._lock:
            new_pending = list(items)
            if self.total_items == 0:
                self._items = new_pending
                self._current_index = 0
            else:
                head = self._items[: self._current_index + 1]
                self._items = head + new_pending
            if self._items and self._current_index < len(self._items):
                self._item_available.set()

        # UI snapshot — fire outside the lock so callback work never extends
        # the critical section or risks re-entry.
        self._notify_tasks_changed()

    async def interrupt_agent(self, reason: str = "") -> None:
        """Send interrupt signal to agent. Waits for ack before returning."""
        await self._interrupt_acked.wait()
        self._interrupt_acked.clear()
        self._interrupt_reason = reason
        self._interrupt_event.set()

    # ── Shared read interface ────────────────────────────────────────────────

    def get_completed_results(self) -> List[TaskResult]:
        """Get all completed task results (for coordinator evaluation)."""
        return list(self._results)

    # ── Progress-sense bus ────────────────────────────────────────────────────

    def append_turn_digest(self, digest: TurnDigest) -> None:
        """Agent writes one mechanical digest per turn (cheap, synchronous)."""
        self._turn_digests.append(digest)

    def get_turn_digests(self) -> List[TurnDigest]:
        """Snapshot of the in-flight digest ring (safe to iterate after)."""
        return list(self._turn_digests)

    def set_progress_concern(self, concern: ProgressConcern) -> None:
        """Watcher writes a verdict (last-write-wins) + fires callbacks."""
        self._progress_concern = concern
        for cb in self._on_progress_concern_callbacks:
            try:
                cb(concern)
            except Exception:
                pass

    def get_progress_concern(self) -> Optional[ProgressConcern]:
        return self._progress_concern

    def clear_progress_concern(self) -> None:
        """Drop the current verdict (called at each task boundary)."""
        self._progress_concern = None

    def get_pending_items(self) -> List[TaskSpec]:
        """Get all tasks not yet started (for the coordinator to modify/view)."""
        return self._items[self._current_index + 1:]

    def render_state_for_coordinator(self) -> str:
        """Format full channel state for coordinator prompt injection.

        Includes completed results, the in-flight task with its recent
        mechanical per-turn digests (the coordinator's window into a running
        task — used to answer "how's progress?"), and the pending tail.
        """
        lines = []
        lines.append(f"## Task Status ({self.completed_count}/{self.total_items} done)")
        lines.append("")

        # Completed — render only the most recent tail so the context stays
        # bounded on long sessions (_results itself is full).
        rendered = self._results[-COORDINATOR_RESULT_RENDER_LIMIT:]
        omitted = len(self._results) - len(rendered)
        if omitted > 0:
            lines.append(f"... ({omitted} earlier completed tasks omitted)")
        for result in rendered:
            status_tag = "Done" if result.success else "Failed"
            lines.append(f"[{status_tag}] item={result.item_id}")
            if result.verification:
                lines.append(f"  Outcome: {'; '.join(result.verification)}")
            if result.artifacts:
                lines.append(f"  Artifacts: {', '.join(result.artifacts)}")
            if result.key_findings:
                lines.append(f"  Findings: {'; '.join(result.key_findings)}")
            if not result.success and result.issues:
                lines.append(f"  Issues: {'; '.join(result.issues)}")
            if result.plan_feedback:
                lines.append(
                    f"  → AGENT FLAGS FOR COORDINATOR: {result.plan_feedback}"
                )

        # In-progress task
        current = self.get_current_item()
        if current:
            lines.append(f"\n[In Progress] item={current.item_id}")
            lines.append(f"  Instruction: {current.instruction}")
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
                    lines.append("    → watcher suggests interrupting the current task")

        # Pending tail
        future = self.get_pending_items()
        if future:
            lines.append(f"\n[Pending] ({len(future)} tasks)")
            for item in future:
                lines.append(f"  - {item.item_id}: {item.instruction[:80]}")

        return "\n".join(lines)

    def get_recent_results_for_agent(self, limit: int = 10) -> str:
        """Render last N TaskResults as a compact boundary block for the agent.

        Differs from render_state_for_coordinator: no pending tail, no
        in-progress line, single-line per task with a status glyph. Slots into
        the agent's per-turn instruction message — a cross-task view of what
        already happened so it does not retry dead approaches.
        """
        if not self._results:
            return ""
        recent = self._results[-limit:]
        lines = ["[Task Boundary History]"]
        for r in recent:
            if r.success:
                tag = "✓"
            elif r.issues and r.issues[0].startswith(INTERRUPTED_BY_COORDINATOR):
                tag = "⊗"
            else:
                tag = "✗"
            outcome = "; ".join(r.verification[:2]) if r.verification else ""
            issue = r.issues[0] if r.issues else ""
            detail = (outcome or issue)[:120]
            lines.append(f"{tag} [{r.item_id}] {detail}")
        return "\n".join(lines)

    # ── UI snapshot ──────────────────────────────────────────────────────────

    def get_ui_snapshot(self) -> List[Dict[str, Any]]:
        """Render the channel as a flat list for a live UI task panel.

        Status is derived from the index-aligned (_items, _current_index,
        _results) triple — len(_results) == _current_index, and
        replace_post_current never touches completed or in-progress slots:

          - i < len(_results)   → completed: "interrupted" on an interrupt exit,
                                  else "done" / "failed"
          - i == _current_index → "running"
          - otherwise           → "pending"

        Each entry: {"item_id", "instruction", "status"}.
        """
        snapshot: List[Dict[str, Any]] = []
        done_count = len(self._results)
        for i, item in enumerate(self._items):
            if i < done_count:
                result = self._results[i]
                if result.issues and result.issues[0].startswith(INTERRUPTED_BY_COORDINATOR):
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
        return snapshot

    def _notify_tasks_changed(self) -> None:
        """Build the UI snapshot once and fan it out to subscribers.

        Best-effort with per-callback exception isolation. Called from
        mark_current_done and replace_post_current (outside the lock).
        """
        if not self._on_tasks_changed_callbacks:
            return
        snapshot = self.get_ui_snapshot()
        for cb in self._on_tasks_changed_callbacks:
            try:
                cb(snapshot)
            except Exception:
                pass

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on_item_done(self, callback: Callable[[TaskResult], Any]) -> None:
        self._on_item_done_callbacks.append(callback)

    def on_tasks_changed(
        self, callback: Callable[[List[Dict[str, Any]]], Any]
    ) -> None:
        """Register a callback fired on every channel mutation.

        Callback receives the rendered UI snapshot (see get_ui_snapshot). Fired
        from mark_current_done and replace_post_current. Best-effort,
        exception-isolated — a throwing subscriber cannot break the loops.
        """
        self._on_tasks_changed_callbacks.append(callback)

    def on_progress_concern(self, callback: Callable[[ProgressConcern], Any]) -> None:
        """Register a callback fired when the watcher sets a concern.

        Symmetric to on_item_done. The Orchestrator subscribes but the
        concern is surfaced passively — it is already stored on the channel
        and rendered into render_state_for_coordinator, so INTENT picks it up
        on the next user turn without any proactive push from this callback.
        """
        self._on_progress_concern_callbacks.append(callback)

    def on_tools_changed(self, callback: Callable[[List[str]], Any]) -> None:
        """Register callback fired when new tools are activated.

        Callback receives the delta (newly added tool names). Used by
        PersistentAgent to load new tool implementations and trigger
        session-level provider prep — runs ONCE per tool, not per task.
        """
        self._on_tools_changed_callbacks.append(callback)
