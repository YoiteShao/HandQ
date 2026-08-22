"""
FlowControllerV2 — session-scoped thin orchestrator.

Architecture (one-liner):
  TaskChannel is a BUS. PersistentAgent loops on it forever; Orchestrator
  (the Coordinator) mutates it from user messages: INTENT classification +
  mechanical queueing. FlowControllerV2 wires them together and owns
  session-level concerns.

Responsibilities:
  - Lifecycle: start() creates components + starts loops, destroy() cancels them
  - Message routing: on_user_message() forwards to orchestrator (唯一入口)

Completion detection lives inside Orchestrator._handle_task_complete_candidate,
not here.

Round 5 (Phase 5E/5G): the ContextProvider system (SSH/RemoteHandQ credential
pre-establishment, per-item hint injection) was removed. SSH credential setup
now happens lazily inside ssh_tool / remote_handq_tool themselves (see
infrastructure/ssh_setup.py ensure_ssh_credentials_lazy) when the agent calls
them with ``ssh_target``. Tool selection is the agent's own job via claim_tool.
"""
import asyncio
import os
from typing import Any, Callable, Dict, List, Optional

from ..infrastructure.llm_service import LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.execution_recorder import ExecutionRecorder
from ..tools.browser_tool import BrowserSessionHolder
from ..tools.desktop_tool import DesktopState
from ..tools.file_state import FileState
from ..tools.session_tool import SessionRegistry
from ..tools.ssh_tool import SshConnectionPool
from .interaction_manager import InteractionManager
from .session_context import GoalState, SessionContext
from .session_digest import SessionDigest
from .task_channel import TaskChannel, TaskResult, TaskSpec
from .orchestrator import Orchestrator
from .persistent_agent import PersistentAgent
from .rewind_store import RewindStore


class FlowControllerV2:
    """Session-scoped orchestrator. Thin shell over Orchestrator + Agent + TaskChannel.

    Public API:
      start()                          — bootstrap all components and loops
      on_user_message()                — forward user message to orchestrator
      destroy()                        — cancel all loops (clean session end)
      cancel_all_tasks()               — cancel loops without teardown (session swap)
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        config_path: Optional[str] = None,
        on_reply_to_user: Optional[Callable[[str], None]] = None,
        on_intent_classified: Optional[Callable[[str], None]] = None,
        expose_session_storage_in_prompt: bool = True,
        helper_llm_services: Optional[List[LLMService]] = None,
        session_id: Optional[str] = None,
    ):
        if not llm_services:
            raise ValueError("llm_services must contain at least one LLMService")

        # session_id is the per-flow identifier the bridge dispatch layer
        # uses to route IPC. We thread it down to per-session resources
        # whose isolation key depends on it — currently the
        # BrowserSessionHolder, which uses it to pick a per-session
        # Chromium user-data-dir under
        # ``%USERPROFILE%\HandQ\browser_profile\sessions\<sid>\``. ``None``
        # falls back to the legacy single profile (test / ctx-less callers).
        self._session_id: Optional[str] = session_id

        self._llm_services: List[LLMService] = llm_services
        # helper_llm_services (llm.helper_models) is accepted for
        # constructor-signature compatibility with real callers
        # (stdio_bridge.py, handq_linux.py) that still pass it, but nothing
        # downstream consumes it — the Tier-1 progress watcher that used to
        # was removed, and Orchestrator/PersistentAgent make no auxiliary
        # LLM calls of their own. Intentionally not stored.
        self.working_directory: Optional[str] = working_directory
        self.storage_directory: str = storage_directory or working_directory or "."
        self.config_manager = ConfigManager(config_path)
        self.logger = get_logger()

        self._on_reply_to_user = on_reply_to_user
        # Passed straight through to Orchestrator's own on_intent_classified
        # (see its docstring) — unlike on_reply_to_user, this one has no IM/UI
        # fallback: it's a bridge-internal signal (session-resume's gate),
        # never something the renderer itself needs to render directly.
        self._on_intent_classified = on_intent_classified
        # When False, PersistentAgent's prompt env block lists only
        # working_directory and omits the session-storage root. Used by the
        # Windows GUI bridge so the agent's mental model is "workspace =
        # everything"; Linux CLI keeps the default True.
        self._expose_session_storage_in_prompt: bool = expose_session_storage_in_prompt

        try:
            self.interaction_manager: Optional[InteractionManager] = InteractionManager()
        except RuntimeError:
            self.interaction_manager = None

        self._task_channel: Optional[TaskChannel] = None
        self._orchestrator: Optional[Orchestrator] = None
        self._agent: Optional[PersistentAgent] = None
        self._agent_task: Optional[asyncio.Task] = None

        # Per-session resource bundle. Constructed in ``start()`` once the
        # TaskChannel exists (the ctx forwards ``interrupt_event`` from it).
        # Owns FileState / SshConnectionPool / BrowserSessionHolder /
        # SessionRegistry / DesktopState — ``destroy()`` awaits ``ctx.close()``
        # to tear them all down so no per-session state leaks across sessions.
        self._ctx: Optional["SessionContext"] = None

        self._started = False

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self, resume_digest: Optional[SessionDigest] = None) -> None:
        """Bootstrap all components and start loops.

        After this returns:
          - TaskChannel created (empty, or restored from resume_digest)
          - Orchestrator created (INTENT classification + mechanical queueing)
          - PersistentAgent loop is running (blocked on empty task channel)
          - System idle, awaiting user messages via on_user_message()

        ``resume_digest``, when given, is a previously-saved SessionDigest
        (session_digest.py) whose trajectory — verbatim conversation,
        completed/pending task queue, standing goal, agent summary — is
        grafted onto the freshly-built components (§6.5). Deliberately NOT
        restored: ``_turns`` (the agent's per-item working memory) and any
        live OS handle — the agent starts with a clean working memory and
        re-observes the world via tool calls, per design §4.
        """
        if self._started:
            return

        self.logger.info(
            "[FlowControllerV2] Starting session"
            + (" (resuming)" if resume_digest is not None else ""),
            component="FlowControllerV2",
        )

        execution_recorder = ExecutionRecorder(
            plan_id="persistent_session",
            goal="(session)",
            log_dir=self.storage_directory,
        )

        self._task_channel = TaskChannel()

        # Build the per-session resource bundle now that the task channel exists.
        # Tools constructed by ToolRegistry below will pull their dependencies
        # (IM, file_state, ssh pool, browser session, session registry, desktop
        # state, interrupt event) from this object instead of module globals.
        # ``destroy()`` awaits ``ctx.close()`` to tear them all down.
        if self.interaction_manager is None:
            # Defensive: IM construction failed (RuntimeError caught in
            # __init__). Build a fresh one — tools must have a real IM ref to
            # call ``request_*_confirmation`` against.
            self.interaction_manager = InteractionManager()
        self._ctx = SessionContext(
            working_directory=self.working_directory,
            storage_directory=self.storage_directory,
            config_manager=self.config_manager,
            interaction_manager=self.interaction_manager,
            file_state=FileState(),
            ssh_pool=SshConnectionPool(),
            browser_session=BrowserSessionHolder(session_id=self._session_id),
            session_registry=SessionRegistry(),
            desktop_state=DesktopState(im=self.interaction_manager),
            execution_recorder=execution_recorder,
            rewind_store=RewindStore(),
            scheduler=self._resolve_scheduler(),
            _task_channel=self._task_channel,
        )
        self._ctx.on_model_stats_changed = self._forward_model_stats_to_ui

        self._orchestrator = Orchestrator(
            llm_services=self._llm_services,
            task_channel=self._task_channel,
            on_reply_to_user=self._forward_reply_to_ui,
            on_response_chunk=self._forward_reply_chunk_to_ui,
            on_response_done=self._seal_reply_to_ui,
            on_state_changed=self._forward_state_to_ui,
            on_recall_started=self._forward_recall_started_to_ui,
            on_task_complete=self._on_task_complete_cleanup,
            on_task_completed_notify=self._forward_task_completed_to_ui,
            on_intent_classified=self._on_intent_classified,
            session_dir=self.storage_directory,
            session_ctx=self._ctx,
        )

        self._agent = PersistentAgent(
            llm_services=self._llm_services,
            task_channel=self._task_channel,
            working_directory=self.working_directory,
            storage_directory=self.storage_directory,
            config_manager=self.config_manager,
            execution_recorder=execution_recorder,
            interaction_manager=self.interaction_manager,
            ctx=self._ctx,
            expose_session_storage_in_prompt=self._expose_session_storage_in_prompt,
        )

        # Bridge task-channel mutations → UI. FlowController owns both the
        # task channel and the InteractionManager, so it is the natural place to
        # forward the live task-panel snapshot (same role it plays for state
        # transitions via _forward_state_to_ui).
        self._task_channel.on_tasks_changed(self._forward_task_plan_to_ui)

        # Crash safety net for SessionDigest (see docs/session_resume_design.md
        # §6.2): destroy() writes the authoritative "destroyed" digest, but if
        # the process dies before destroy() ever runs, this callback still
        # leaves a "crashed" digest checkpointed at the last completed item
        # boundary — better than nothing on disk at all.
        self._task_channel.on_item_done(self._on_item_done_checkpoint)

        if resume_digest is not None:
            await self._restore_from_digest(resume_digest)

        self._agent_task = asyncio.create_task(
            self._agent.run_loop(),
            name="persistent_agent_loop",
        )

        self._started = True

    async def _restore_from_digest(self, digest: SessionDigest) -> None:
        """Graft a SessionDigest's trajectory onto the just-built components
        (§6.5) — MUST run before the agent loop starts, so the restored
        queue is what the loop picks up on its very first
        ``wait_for_current_item()``.

        Only §3's "③ 轨迹" is restored here: conversation, task queue,
        standing goal, agent summary. ``_turns`` (②) is deliberately left
        untouched — PersistentAgent starts with an empty working memory and
        re-observes the world via tool calls (design §4).
        """
        assert self._orchestrator is not None and self._task_channel is not None
        assert self._agent is not None and self._ctx is not None

        self._orchestrator.restore_conversation(digest.conversation)
        await self._task_channel.restore_queue({
            "completed": digest.completed,
            "current": digest.current,
            "pending": digest.pending,
        })
        if digest.active_tools:
            self._task_channel.activate_tools(digest.active_tools)
        if digest.active_goal:
            try:
                self._ctx.active_goal = GoalState(
                    condition=digest.active_goal.get("condition", ""),
                    iterations=int(digest.active_goal.get("iterations", 0)),
                    baseline_result_count=int(
                        digest.active_goal.get("baseline_result_count", 0)
                    ),
                )
            except Exception:
                self.logger.warning(
                    "[FlowControllerV2] resume: failed to restore active_goal; "
                    "continuing without a standing goal",
                    component="FlowControllerV2",
                )
        if digest.agent_summary:
            self._agent.restore_summary(digest.agent_summary)

        # One-shot banner (§6.6) — consumed by the agent's very first
        # post-resume _build_messages call, then cleared automatically.
        self._agent.set_resume_banner(
            "[会话续接] 本 session 曾经活跃，现已重开。你的逐轮工作记忆没有被搬过来，"
            "但轨迹是完整的：下方是至此的对话、已完成任务及其已验证结果、以及尚未完成"
            "的部分。磁盘上的 workspace 与上次关闭时完全一致。在对任何你\"记得\"的文件或"
            "远端状态动手前，请先重读 / 重新观察——把恢复的摘要当作地图，而不是当作"
            "当前的 ground truth。"
        )

    @staticmethod
    def _resolve_scheduler() -> Optional[object]:
        """Return the live bridge-global Scheduler, or None when there is no
        bridge (offline tests, CLI paths without stdio_bridge running).

        Lazy module-attribute read — the scheduler is assigned onto
        ``stdio_bridge.scheduler`` by bridge_main at boot, AFTER this module is
        imported, so we cannot import the value at module load. Reading the
        attribute at session-start time picks up whatever the bridge wired up.
        """
        try:
            from ..bridge import stdio_bridge
            return getattr(stdio_bridge, "scheduler", None)
        except Exception:
            return None

    async def on_user_message(self, message: str) -> str:
        """Single entry point for all user input.

        Forwards to the orchestrator, which handles intent + tool activation
        + task-channel mutations. Replies are delivered to UI via _forward_reply_to_ui.

        Brackets the call with the coordinator "thinking…" indicator: on at
        entry, off in ``finally``. The renderer also clears it on the first
        ``reply_delta``, so for the streaming path the ``finally`` is a no-op;
        it only matters when ``response_to_user`` stays silent so the bubble
        doesn't linger.
        """
        if not self._started or self._orchestrator is None:
            return "Session not started."

        # Record the verbatim user prompt as its own log record — one per send,
        # before mention-preprocessing, so the raw prompt is preserved.
        if self._ctx is not None and self._ctx.execution_recorder is not None:
            try:
                self._ctx.execution_recorder.write_user_request(message)
            except Exception:
                pass

        if self.interaction_manager is not None:
            try:
                self.interaction_manager.notify_coordinator_thinking()
            except Exception:
                pass
        try:
            return await self._orchestrator.on_user_message(message)
        finally:
            if self.interaction_manager is not None:
                try:
                    self.interaction_manager.clear_coordinator_thinking()
                except Exception:
                    pass

    async def destroy(self) -> None:
        """Cancel all loops + tear down per-session resources. Call at
        session end. Awaitable: closes the SessionContext (browser /
        sessions / ssh / desktop / file state) and forwards the interrupt
        event so any in-flight subprocess wait wakes up before its asyncio
        task is cancelled.
        """
        self._signal_interrupt()
        self._cancel_loops()
        if self._ctx is not None:
            if self._ctx.execution_recorder is not None:
                try:
                    self._ctx.execution_recorder.write_session_end(success=True)
                except Exception:
                    pass
            # Flush the resume digest BEFORE tearing anything down — this is
            # the authoritative "destroyed" checkpoint (see §6.2). Everything
            # it reads (_orchestrator, _task_channel, _agent, _ctx) is still
            # alive at this point.
            self._checkpoint_digest(status="destroyed")
            try:
                await self._ctx.close()
            except Exception:
                self.logger.warning(
                    "[FlowControllerV2] ctx.close raised; continuing teardown",
                    component="FlowControllerV2",
                )
            self._ctx = None
        self._started = False
        self.logger.info(
            "[FlowControllerV2] Session destroyed",
            component="FlowControllerV2",
        )

    def cancel_all_tasks(self) -> None:
        """Cancel internal loops WITHOUT marking destroyed.

        Used when swapping the active session (e.g. handq --save spawns a fresh
        FlowController). Sub-tasks won't compete with the new flow for the
        shared InteractionManager. The instance becomes inert but is not torn
        down — typical caller pattern is to drop the reference shortly after.

        SessionContext is NOT closed here — sync caller can't await. Drop the
        flow reference and let GC + ``destroy()``-on-the-next-call reach the
        ctx instead. For the synchronous "cancel + walk away" pattern
        (``handq --save``), schedule ctx.close as a fire-and-forget task so
        OS resources still get released — it just runs out of band.
        """
        self._signal_interrupt()
        self._cancel_loops()
        if self._ctx is not None:
            try:
                asyncio.create_task(
                    self._ctx.close(), name="ctx-close-fire-and-forget",
                )
            except RuntimeError:
                # No running loop — best effort; resources will GC eventually.
                pass
            self._ctx = None
        self.logger.info(
            "[FlowControllerV2] Tasks cancelled (externally)",
            component="FlowControllerV2",
        )

    @property
    def started(self) -> bool:
        return self._started

    def _signal_interrupt(self) -> None:
        """Trip the task-channel interrupt event so tools parked on
        ``asyncio.wait([communicate, interrupt])`` (shell_tool, session_tool)
        wake up and terminate their subprocess trees before the event-loop
        cancellation lands.

        asyncio task cancellation alone does NOT reliably propagate to a
        running subprocess on Windows: a tool wedged in
        ``await process.communicate()`` will see ``CancelledError`` on the
        await but the OS-level child keeps running until it exits naturally.
        The interrupt event lives on the task channel (set normally by the
        Coordinator's ``interrupt_agent`` on the interrupt lane) and we re-use it here for
        session-end so each new session starts on a clean slate without any
        leftover children from the prior session's tool calls.

        The agent loop will not get a chance to acknowledge the interrupt
        (the loop is being cancelled), and that is fine — the event lives on
        the task channel, which is dropped along with the flow.
        """
        if self._task_channel is None:
            return
        try:
            self._task_channel._interrupt_event.set()
        except Exception:
            self.logger.warning(
                "[FlowControllerV2] _signal_interrupt failed",
                component="FlowControllerV2",
            )

    # ── Internal: session-resume digest (session_digest.py) ─────────────────

    def _on_item_done_checkpoint(self, result: TaskResult) -> None:
        """on_item_done callback — crash-safety checkpoint at every item
        boundary (see docs/session_resume_design.md §6.2). Synchronous, same
        calling convention as Orchestrator._on_item_done_sync — TaskChannel
        invokes callbacks synchronously from mark_current_done."""
        self._checkpoint_digest(status="crashed")

    def _checkpoint_digest(self, status: str) -> None:
        """Assemble and save a SessionDigest from the currently-live
        components. Best-effort: any failure is logged and swallowed so a
        broken checkpoint never blocks item completion or session teardown.

        No-op before start() has wired everything up, or after destroy()
        has already torn them down (all four components are required).
        """
        if (
            self._ctx is None
            or self._task_channel is None
            or self._orchestrator is None
            or self._agent is None
        ):
            return
        try:
            import time as _time

            workspace_dir = self._ctx.working_directory or ""
            workspace_files: List[str] = []
            if workspace_dir and os.path.isdir(workspace_dir):
                try:
                    workspace_files = [
                        name for name in os.listdir(workspace_dir)
                        if os.path.isfile(os.path.join(workspace_dir, name))
                    ]
                except OSError:
                    pass

            conversation = [
                {
                    "role": turn.get("role", ""),
                    "content": SessionDigest.cap(turn.get("content", "")),
                }
                for turn in self._orchestrator.conversation_history
            ]
            queue_snapshot = self._task_channel.snapshot_queue()
            for result in queue_snapshot.get("completed", []) or []:
                if result.get("final_answer"):
                    result["final_answer"] = SessionDigest.cap(result["final_answer"])

            goal = self._ctx.active_goal
            title = conversation[0]["content"] if conversation else ""
            now = _time.strftime("%Y-%m-%d %H:%M:%S")
            # Preserve created_at across repeated checkpoints (item-boundary
            # crash-safety net fires many times per session) — only the very
            # first checkpoint should stamp it; every later one just advances
            # updated_at. Falls back to `now` when this is the first checkpoint.
            prior = SessionDigest.load(self.storage_directory)
            created_at = prior.created_at if prior is not None else now

            digest = SessionDigest(
                session_id=self._session_id or "",
                title=title,
                created_at=created_at,
                updated_at=now,
                workspace_dir=workspace_dir,
                workspace_files=workspace_files,
                status=status,
                conversation=conversation,
                completed=queue_snapshot.get("completed", []),
                current=queue_snapshot.get("current"),
                pending=queue_snapshot.get("pending", []),
                active_tools=sorted(self._task_channel.active_tools),
                active_goal=(
                    {
                        "condition": goal.condition,
                        "iterations": goal.iterations,
                        "baseline_result_count": goal.baseline_result_count,
                        "created_at": str(goal.created_at),
                    }
                    if goal is not None else None
                ),
                agent_summary=self._agent.export_summary(),
            )
            digest.save(self.storage_directory)
        except Exception:
            self.logger.warning(
                "[FlowControllerV2] _checkpoint_digest(status=%s) failed; "
                "continuing without a persisted digest for this checkpoint" % status,
                component="FlowControllerV2",
            )

    # ── Internal: loop management ────────────────────────────────────────────

    def _cancel_loops(self) -> None:
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()

    # ── Internal: status / reply ─────────────────────────────────────────────

    def _forward_reply_to_ui(self, reply: str) -> None:
        """Forward orchestrator reply to external callback / UI."""
        if self._on_reply_to_user:
            self._on_reply_to_user(reply)
        elif self.interaction_manager and reply:
            try:
                self.interaction_manager.notify_inline_event("reply", reply[:500])
            except Exception:
                pass

    def _forward_reply_chunk_to_ui(self, fragment: str) -> None:
        """Stream one reply fragment (the coordinator's INTENT ``response_to_user``)
        to the UI as it arrives from the LLM. Renderer appends it to the live
        assistant bubble (``reply_delta``)."""
        if self.interaction_manager and fragment:
            try:
                self.interaction_manager.stream_coordinator_reply_chunk(fragment)
            except Exception:
                pass

    def _seal_reply_to_ui(self) -> None:
        """Finalize the current streamed reply bubble (``reply_done``)."""
        if self.interaction_manager:
            try:
                self.interaction_manager.seal_coordinator_reply()
            except Exception:
                pass

    def _forward_state_to_ui(self, state: str) -> None:
        """Forward an orchestrator state transition (``idle``) to the UI.
        The agent emits ``thinking`` / ``executing`` directly via the
        InteractionManager; this covers the task-settled transition, which
        only the orchestrator sees.

        Multi-session task-level queueing for desktop: when the orchestrator
        reaches ``idle`` (task settled, final reply sent), proactively
        release this session's cross-session desktop ownership lock so any
        other session blocked waiting on desktop can immediately acquire it.
        The session itself stays alive and can re-acquire on its next
        desktop call (``acquire_global_takeover`` is idempotent). Without
        this, a single long-lived session could indefinitely starve all
        other sessions of desktop access purely because the user hasn't
        clicked its X button. Browser is NOT released here — per-session
        browser instances mean there is no cross-session lock to release;
        the browser's user-data-dir is held until ``ctx.close()``.
        """
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_state_changed(state)
            except Exception:
                pass
        if state == "idle" and self._ctx is not None:
            try:
                self._ctx.desktop_state.reset_takeover_state()
            except Exception as exc:
                self.logger.warning(
                    "desktop reset on idle failed (best-effort): %s" % exc,
                    component="FlowControllerV2",
                )

    def _forward_recall_started_to_ui(self) -> None:
        """Forward an LTM-recall-in-flight signal from the orchestrator's
        INTENT/PLAN gather to the UI, which shows a transient ``recalling…``
        label on the activity strip."""
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_recall_started()
            except Exception:
                pass

    def _forward_task_completed_to_ui(self, summary: str) -> None:
        """Forward a real task-completion (not just a chat reply) to the UI
        so it can raise a system notification / taskbar flash. Fired from
        ``Orchestrator._emit_completion_reply`` — never for ordinary chat
        turns, which only go through ``_forward_reply_to_ui``."""
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_task_completed(summary)
            except Exception:
                pass

    async def _on_task_complete_cleanup(self) -> None:
        """Close on-demand tool resources at the task-complete boundary.

        Wired into ``Orchestrator.on_task_complete``. Fires when the acceptance
        gate reaches a terminal verdict (PASS / TRIVIAL / ACCEPT / unknown) —
        NOT on EXTEND / VALIDATE where more work is queued. Currently closes
        the browser session so Chromium doesn't linger between tasks; add
        further on-demand-tool teardown here (SSH sessions, remote HandQ, etc.)
        rather than growing the callback surface on Orchestrator.

        Best-effort: any per-resource failure is logged and swallowed so a
        broken cleanup never blocks completion delivery. Skipped if the ctx
        was never constructed (start() failed or destroy() already ran)."""
        if self._ctx is None:
            return
        try:
            closed = await self._ctx.browser_session.close()
            if closed:
                self.logger.info(
                    "[FlowControllerV2] task-complete cleanup: browser holder closed",
                    component="FlowControllerV2",
                )
        except Exception as e:
            self.logger.warning(
                f"[FlowControllerV2] task-complete cleanup: browser close failed "
                f"({type(e).__name__}: {e}); ignoring.",
                component="FlowControllerV2",
            )

    def _forward_task_plan_to_ui(self, items: List[Dict[str, Any]]) -> None:
        """Forward a task-plan snapshot (from ``on_tasks_changed``) to the
        UI, which paints a live task panel. ``items`` is the list of
        ``{item_id, instruction, status}`` dicts. Fire-and-forget."""
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_task_plan_changed(items)
            except Exception:
                pass

    def _forward_model_stats_to_ui(self, stats: Dict[str, Any]) -> None:
        """Forward a per-model token tally (from ``ctx.record_model_usage``)
        to the UI stats panel. ``stats`` is ``{model_name: TokenUsage}``.
        Fire-and-forget."""
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_model_stats_changed(stats)
            except Exception:
                pass

    # ── User-driven file undo (RewindStore, Tier-1.3, option-3 semantics) ────

    async def undo_files(self, item_id: Optional[str] = None) -> Dict[str, Any]:
        """Undo the file changes of a task item on explicit user request.

        Undo is a USER action, not a silent disk edit — it changes the world
        the agent believes in, and the agent only trusts its own history. So we
        handle it in two modes (see the design notes below), never just
        clobbering the disk:

        **Case A — the undone item is the one currently in flight.** The user is
        correcting the agent mid-task. We restore under the per-path write lock
        (so an in-flight write/edit to the same path can't race or re-clobber),
        then trip the existing coordinator interrupt so the running turn stops
        cleanly and control returns to the user. The user's next message goes
        through INTENT normally — whether that's "stop" or "try it another way"
        is the Coordinator's call, not ours (principle #1: the agent never
        guesses intent). This is why undo routes through interrupt rather than
        silently reverting: it prevents the ping-pong where the agent's very
        next turn just re-applies the edit we undid.

        **Case B — the undone item already completed** and the agent is idle or
        on a later, unrelated item. No interrupt. We restore under the write
        lock, correct that completed TaskResult's artifacts (a reverted file is
        no longer a produced artifact — the grounded completion contract must
        stay honest), and queue a faithful notice so the agent's NEXT turn is
        told the file was reverted (its in-context memory of that file is now
        void). ``read`` does not staleness-check, so this notice is the only
        thing that stops the model reasoning from a stale in-context copy.

        In BOTH modes an external-modification conflict (disk changed since the
        item ended) is NOT overwritten — it's returned for the caller to surface
        for user confirmation ([[feedback_bulk_delete_verify_each_file]]).

        Returns a result dict for the bridge: restored paths, conflicts, mode.
        """
        if self._ctx is None or self._ctx.rewind_store is None:
            return {"ok": False, "error": "no active session / rewind store"}
        store = self._ctx.rewind_store
        channel = self._task_channel

        target = item_id or store.last_item_id()
        if target is None or not store.can_rewind(target):
            return {"ok": False, "error": "nothing to undo for that task"}

        current = channel.get_current_item() if channel else None
        is_current = current is not None and current.item_id == target

        # Acquire every touched path's write lock so the restore serializes
        # against any in-flight write/edit to the same path (they share
        # ctx.write_lock_for). Sorted for deterministic acquisition order.
        paths = sorted(store.paths_for_item(target))
        locks = [self._ctx.write_lock_for(p) for p in paths]
        acquired: List[Any] = []
        try:
            for lk in locks:
                await lk.acquire()
                acquired.append(lk)
            report = await asyncio.to_thread(store.rewind_item, target)
        finally:
            for lk in acquired:
                lk.release()

        mode = "interrupt" if is_current else "notice"

        if is_current:
            # Case A: stop the running turn; control returns to the user.
            try:
                await channel.interrupt_agent(
                    reason=(
                        f"User undid file changes for the current task "
                        f"({len(report.restored_paths)} file(s) reverted). "
                        f"Do not re-apply them; await the user's next instruction."
                    )
                )
            except Exception:
                self.logger.warning(
                    "[FlowControllerV2] undo_files interrupt failed",
                    component="FlowControllerV2",
                )
        else:
            # Case B: correct the completed item's artifacts + queue a faithful
            # notice for the agent's next turn.
            self._correct_artifacts_after_undo(target, report.restored_paths)
            if report.restored_paths:
                try:
                    self._ctx.pending_file_notices.append(
                        "The user reverted your file changes to "
                        + ", ".join(report.restored_paths)
                        + f" (from task {target}). Your recorded content for "
                        "these files is now void — re-read any of them before "
                        "relying on their contents again."
                    )
                except Exception:
                    pass

        return {
            "ok": True,
            "mode": mode,
            "item_id": target,
            # List of dicts (not plain paths) so the UI can distinguish a
            # content restore (leaf stays, ↺ suppressed) from an ABSENT
            # restore (agent-created file was deleted; leaf must leave the
            # tree). ``was_absent`` mirrors FileRewindResult.was_absent.
            "restored": [
                {"path": r.path, "was_absent": r.was_absent}
                for r in report.results if r.restored
            ],
            "conflicts": [
                {"path": c.path, "conflict": c.conflict.value, "detail": c.detail}
                for c in report.conflicts
            ],
        }

    def _correct_artifacts_after_undo(
        self, item_id: str, reverted_paths: List[str]
    ) -> None:
        """Drop reverted paths from a completed item's TaskResult.artifacts so
        the grounded completion contract stays honest (a reverted file is not a
        produced artifact). Best-effort; no-op if the result isn't found."""
        if not reverted_paths or self._task_channel is None:
            return
        reverted = set(reverted_paths)
        for result in self._task_channel.get_completed_results():
            if result.item_id == item_id and result.artifacts:
                result.artifacts = [
                    a for a in result.artifacts if a not in reverted
                ]

