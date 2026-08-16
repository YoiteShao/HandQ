"""Per-session resource container.

One ``SessionContext`` is created per :class:`FlowControllerV2` session and owns:

* the UI bus (:class:`InteractionManager`)
* identification / config (working dir, storage dir, config manager)
* a forwarding handle on the TaskChannel's interrupt event
* per-session tool resources: file state, SSH pool, browser session, interactive
  shell session registry, desktop state machine
* the per-session execution recorder

Tools receive the ctx via constructor injection (``Tool(ctx=ctx)``) and read
their dependencies from it instead of going through module-level globals.
``flow.destroy()`` awaits :meth:`SessionContext.close`, which tears down all
owned resources. This guarantees:

* **session 之间硬隔离**:同进程跑两个 session 互不干扰;新 session 启动时所有
  工具状态都是新鲜的(SSH 池、浏览器、shell session、文件读痕、desktop 授权)。
* **bridge 与工具解耦**:bridge 的 ``_do_new_session`` / ``_do_shutdown`` 只调
  ``flow.destroy()``,新增工具不再需要回头改 bridge。

State that intentionally remains process-level:

* ``email_tool`` Outlook COM handle / ThreadPoolExecutor (init ~2-3s, cross-session
  reuse is a real cost win)
* ``cancellation.py`` thread-local interrupt token (per-invocation, not per-session)
* ``ToolRegistry._tools`` registry (immutable after init)
* ``desktop_tool._DPI_INITIALISED`` (Windows DPI is OS-process-level)
* ``desktop_tool._desktop_lock`` (mouse/keyboard exclusivity is per-display, hence
  per-process — not per-session)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional, Tuple

if TYPE_CHECKING:
    from ..infrastructure.config_manager import ConfigManager
    from ..infrastructure.execution_recorder import ExecutionRecorder
    from ..tools.browser_tool import BrowserSessionHolder
    from ..tools.desktop_tool import DesktopState
    from ..tools.file_state import FileState
    from ..tools.session_tool import SessionRegistry
    from ..tools.ssh_tool import SshConnectionPool
    from .interaction_manager import InteractionManager
    from .task_channel import TaskChannel
    from .rewind_store import RewindStore


_logger = logging.getLogger("handq.controller_v2.session_context")

# schedule_wakeup delay bounds (seconds), mirroring Claude Code's ScheduleWakeup
# clamp. Sub-minute wakeups are pointless (use wait_interval); >1h belongs in a
# real scheduled task (schedule_create).
WAKEUP_MIN_DELAY_SEC = 60
WAKEUP_MAX_DELAY_SEC = 3600


@dataclass
class GoalState:
    """A standing condition declared by the user, tracked across item
    boundaries so the Coordinator can keep re-queuing work until it holds.

    Distinct from a normal ``TaskSpec``: a task's completion is a one-shot
    fact settled within a single Agent item loop, while a goal describes a
    world-state that may still not hold even after an item "succeeds" (e.g.
    a check task always succeeds by running the check; the condition only
    holds once the check actually observes the threshold). This state is
    what lets that distinction survive across item boundaries, where the
    Agent's own per-item loop has no memory of it.
    """

    condition: str
    iterations: int = 0
    baseline_result_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionContext:
    """Per-session resource bundle. Constructed in ``FlowControllerV2.start()``
    after the TaskChannel is built; passed to ``PersistentAgent`` and from
    there to every tool's ``__init__``."""

    # ── Identity / config ────────────────────────────────────────────────
    working_directory: Optional[str]
    storage_directory: str
    config_manager: "ConfigManager"

    # ── UI bus ────────────────────────────────────────────────────────────
    interaction_manager: "InteractionManager"

    # ── Per-session tool resources ───────────────────────────────────────
    file_state: "FileState"
    ssh_pool: "SshConnectionPool"
    browser_session: "BrowserSessionHolder"
    session_registry: "SessionRegistry"
    desktop_state: "DesktopState"

    # ── Execution recorder (per-session, owned by ctx) ───────────────────
    execution_recorder: Optional["ExecutionRecorder"] = None

    # ── File checkpoints for undo + change auditing (RewindStore) ─────────
    # Per-session before/after file snapshots keyed by item_id. write/edit
    # tools call ``capture_before`` pre-operation; PersistentAgent brackets
    # each item with begin_item/end_item; the bridge exposes rewind_item for
    # user-driven undo; the completion verifier calls capture_diff for
    # ground-truth evidence. None in test fixtures / ctx-less callers — every
    # call site treats None as "no checkpointing this session".
    rewind_store: Optional["RewindStore"] = field(default=None, repr=False)

    # ── Agent-owned todo (Claude-Code-style) ─────────────────────────────
    # The agent's OWN progress scratchpad for decomposing its current item —
    # written and read by the agent via the `todo_write` tool, surfaced to the
    # UI. This is DISTINCT from TaskChannel: the channel is the
    # Coordinator↔Agent IPC layer; this is the worker's private plan. A flat
    # list of {content, status} dicts; last-write-wins (the tool replaces the
    # whole list each call, mirroring TodoWrite).
    agent_todo: list = field(default_factory=list, repr=False)

    # ── Self-extension intent (claim_tool / release_tool tools) ───────────
    # Populated by ClaimToolTool/ReleaseToolTool.execute() (self_extension_
    # tool.py) when the agent calls the real structured tool — a genuine
    # tool_use, unlike the old free-text-JSON-scan convention. PersistentAgent
    # drains both lists right after the tool result comes back and routes
    # them into the existing, unchanged _apply_self_extension(claim, release);
    # draining (not just reading) means a name only ever activates once per
    # call, even if a later turn re-reads this same ctx before the next call.
    pending_claim_tool: list = field(default_factory=list, repr=False)
    pending_release_tool: list = field(default_factory=list, repr=False)

    # ── Pending file-undo notices (RewindStore → agent, cross-task safe) ──
    # A user-driven undo runs on the bridge task, NOT the agent task. It must
    # never mutate the agent's `_turns` directly (that would race the running
    # turn and can desync tool_calls/tool_results → a 400). Instead the undo
    # handler appends a short faithful-notice string here; PersistentAgent
    # drains this list at a safe point in its loop (like
    # _poll_completed_background_tasks) and persists each as an observation so
    # the model learns "file X was reverted by the user; your recorded state
    # for it is void — re-read before relying on it." Drained (not just read)
    # so a notice surfaces exactly once. See [[project_cc_aligned_uniform_rendering]]:
    # the model only self-heals when history stays faithful to the world.
    pending_file_notices: list = field(default_factory=list, repr=False)

    # ── Standing goal (Coordinator-owned, survives across item boundaries) ──
    # Set by the Orchestrator when INTENT recognizes a persistent condition
    # (as opposed to a one-shot task); cleared on explicit cancellation or
    # once the Coordinator's goal-judge call confirms it holds. None = no
    # active goal, the ordinary one-item-and-done completion path applies.
    active_goal: Optional["GoalState"] = field(default=None, repr=False)

    # ── Session-resume search gate (bridge-owned, see stdio_bridge.py) ──────
    # Session-resume search runs on EVERY user message (not just the first)
    # until the session's identity is settled one of three ways:
    #   (a) the user accepted a resume offer (_do_resume_confirm succeeded —
    #       identity as "continuing session X" is now fixed);
    #   (b) the user clicked "Not resuming" on a candidate card
    #       (resume_disable_for_session IPC — explicit "this is a fresh
    #       conversation, stop asking"); or
    #   (c) INTENT classified a turn's FINAL lane as "queue" — a real task
    #       (_on_coordinator_intent — the user's intent is now unambiguous
    #       without needing an explicit click; "chat" and "interrupt" are
    #       both inert here, see that method's docstring for why).
    # False (default) means still-undecided: every subsequent message keeps
    # searching and may surface a new offer.
    resume_search_disabled: bool = field(default=False, repr=False)

    # ── Scheduler singleton (process-level, injected by FlowControllerV2) ──
    # The live bridge-global ``Scheduler`` (stdio_bridge.scheduler), passed in
    # so the agent-facing schedule_create/list/delete tools can reach it via
    # ctx. None when running without a bridge (offline tests, CLI paths) — the
    # tools then return a clean "scheduler unavailable" error instead of
    # crashing. Typed Any to avoid importing the infrastructure layer here.
    scheduler: Optional[object] = field(default=None, repr=False)

    # ── In-session wakeup timers (schedule_wakeup / dynamic /loop parity) ──
    # Pending asyncio tasks each sleeping until a scheduled wakeup fires, then
    # re-queuing a prompt onto _task_channel to continue a self-paced loop.
    # Tracked so close() can cancel them (a torn-down session must not leave a
    # timer that later re-queues work onto a dead channel). See schedule_wakeup.
    _wakeup_tasks: set = field(default_factory=set, repr=False)

    # ── Cross-layer write-path dedup ─────────────────────────────────────
    # asyncio.Lock per absolute file path, shared by the parent agent's own
    # concurrent tool dispatch (PersistentAgent._think_streaming) AND every
    # fan_out_agents / spawn_agent sub-task running under this session. Two
    # writers targeting the SAME path — whether both are parent tool calls,
    # both are fan_out sub-tasks, or one of each — serialize on this lock
    # instead of racing; writers to DIFFERENT paths never contend. Entries
    # are never removed (a session's total distinct write paths is small),
    # so this is a simple dict behind a session-lifetime set of locks, not a
    # pool that needs eviction.
    write_path_locks: Dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)

    def write_lock_for(self, path: str) -> asyncio.Lock:
        """Return the shared lock for *path*, creating it on first use."""
        lock = self.write_path_locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self.write_path_locks[path] = lock
        return lock

    # ── Cross-layer SSH-target dedup ─────────────────────────────────────
    # Same pattern as write_path_locks, keyed by (hostname, username) instead
    # of path. Sub-agents run with the SAME tool instances as the parent
    # (ctx is shared — see spawn_agent_tool.py), so two agents targeting the
    # SAME remote host serialize their exec/write/run_script calls instead of
    # racing on shared remote state (job files, working dir); two agents
    # targeting DIFFERENT hosts never contend. In normal use sub-tasks are
    # dispatched over disjoint hosts, so this lock rarely activates — it is a
    # safety net for the case where they overlap, not the common path.
    ssh_host_locks: Dict[Tuple[str, str], asyncio.Lock] = field(default_factory=dict, repr=False)

    def ssh_lock_for(self, hostname: str, username: str) -> asyncio.Lock:
        """Return the shared lock for *(hostname, username)*, creating it on first use."""
        key = (hostname, username)
        lock = self.ssh_host_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.ssh_host_locks[key] = lock
        return lock

    # ── Late-bound TaskChannel for interrupt event forwarding ────────────
    # TaskChannel owns ``_interrupt_event`` because the coordinator-driven
    # ``interrupt_agent(reason)`` writes to it. Tools (shell / session) need to
    # read it. Rather than duplicating the event, the ctx exposes a forwarding
    # property — single source of truth on the channel, but tools find it
    # via ctx so they don't need to know about the channel directly.
    _task_channel: Optional["TaskChannel"] = field(default=None, repr=False)

    # Idempotence flag for close(). _do_new_session and _do_shutdown can both
    # land on the same flow if the user clicks New right before Quit.
    _closed: bool = field(default=False, repr=False)

    # ── Public properties ────────────────────────────────────────────────

    @property
    def interrupt_event(self) -> asyncio.Event:
        """The asyncio.Event tools listen on for "session is being torn down,
        cancel any in-flight subprocess wait". Set by:
          * ``TaskChannel.interrupt_agent(reason)`` — coordinator-driven mid-task
            interrupt (normal operation)
          * ``FlowControllerV2._signal_interrupt()`` — session-end (this is what
            propagates "kill subprocesses" through to shell_tool / session_tool's
            ``asyncio.wait([communicate, interrupt_wait])`` race)
        """
        if self._task_channel is None:
            # Standalone instance (test fixture) without a channel — return an
            # event that can be set/cleared but doesn't share state with anyone.
            if not hasattr(self, "_orphan_interrupt"):
                self._orphan_interrupt = asyncio.Event()  # type: ignore[attr-defined]
            return self._orphan_interrupt  # type: ignore[attr-defined]
        return self._task_channel._interrupt_event

    # ── Self-paced loop wakeup (schedule_wakeup tool) ────────────────────────

    def schedule_wakeup(
        self, delay_seconds: int, prompt: str, reason: str = "",
    ) -> int:
        """Schedule an in-session wakeup: after *delay_seconds*, re-queue
        *prompt* onto this session's TaskChannel as a new item so the (idle)
        agent picks it up and continues — a self-paced loop tick.

        Returns the clamped delay actually used. This is the HandQ counterpart
        to Claude Code's ScheduleWakeup: it does NOT persist and does NOT spawn
        a fresh session — the agent's conversation history stays intact, so the
        loop resumes with full context. Ending the loop = simply not calling
        this again.

        No-op (returns the clamp) when there is no task channel (test fixtures
        without a live loop) or the session is already closed.
        """
        clamped = max(WAKEUP_MIN_DELAY_SEC, min(int(delay_seconds), WAKEUP_MAX_DELAY_SEC))
        if self._task_channel is None or self._closed:
            return clamped

        channel = self._task_channel
        # Count prior wakeups so the re-queued item can show "loop tick #N".
        self._wakeup_count = getattr(self, "_wakeup_count", 0) + 1
        iteration = self._wakeup_count

        async def _fire() -> None:
            import uuid as _uuid
            from .task_channel import TaskSpec
            try:
                await asyncio.sleep(clamped)
            except asyncio.CancelledError:
                return
            if self._closed:
                return
            try:
                await channel.replace_post_current([
                    TaskSpec(
                        item_id=str(_uuid.uuid4()),
                        instruction=prompt,
                        wakeup_iteration=iteration,
                    )
                ])
            except Exception:
                _logger.warning(
                    "[SessionContext] schedule_wakeup re-queue failed",
                    exc_info=True,
                )

        task = asyncio.ensure_future(_fire())
        self._wakeup_tasks.add(task)
        task.add_done_callback(self._wakeup_tasks.discard)
        _logger.info(
            "[SessionContext] schedule_wakeup in %ds (tick #%d): %s",
            clamped, iteration, (reason or prompt)[:80],
        )
        return clamped

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Tear down all per-session resources. Idempotent — safe to call twice.

        Every per-session resource (``browser_session`` / ``session_registry`` /
        ``ssh_pool`` / ``desktop_state``) owns its state via SessionContext DI:
        the live flow writes through the ctx instances, so closing the holder is
        the real teardown. ``file_state`` holds no OS resources — dropping the
        reference is enough, so it has no ``close``.

        ``return_exceptions=True`` so any one resource misbehaving cannot
        derail the others. The bridge wraps the call in ``asyncio.wait_for(2.0s)``
        for a hard upper bound.
        """
        if self._closed:
            return
        self._closed = True

        # Cancel any pending schedule_wakeup timers first — a torn-down session
        # must not have a timer fire later and re-queue work onto a dead channel.
        for task in list(self._wakeup_tasks):
            task.cancel()
        self._wakeup_tasks.clear()

        coros = [
            self._safe_close("browser_session_holder", self.browser_session.close()),
            self._safe_close("session_registry", self.session_registry.close_all()),
            self._safe_close("ssh_pool", asyncio.to_thread(self.ssh_pool.close)),
            self._safe_close("desktop_state", asyncio.to_thread(self.desktop_state.close)),
        ]

        await asyncio.gather(*coros, return_exceptions=True)

    @staticmethod
    async def _safe_close(name: str, awaitable) -> None:
        """Run an awaitable close() with per-resource error isolation.

        Logs and swallows. The dataclass close() pattern means a single resource
        misbehaving (e.g. paramiko hang on TCP keepalive) cannot derail the rest
        of the cleanup, and the bridge's outer wait_for caps total time.
        """
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.warning("[SessionContext] close failed: %s", name, exc_info=True)
