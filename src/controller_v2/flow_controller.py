"""
FlowControllerV2 — session-scoped thin orchestrator.

Architecture (one-liner):
  SharedCheckList is a BUS. PersistentAgent loops on it forever; Orchestrator
  mutates it from two channels (user messages + per-item eval). FlowControllerV2
  wires them together and owns session-level concerns: provider registration.

Responsibilities:
  - Lifecycle: start() creates components + starts loops, destroy() cancels them
  - Message routing: on_user_message() forwards to orchestrator (唯一入口)
  - Provider system: registers ContextProviders, pushes tool table to Orchestrator,
    supplies a per-item hint callback to PersistentAgent

Verification gate (B1) lives inside Orchestrator._handle_task_complete_candidate,
not here.
"""
import asyncio
import sys
from typing import Any, Callable, Dict, List, Optional

from ..infrastructure.llm_service import LLMService
from ..infrastructure.logger import get_logger
from ..infrastructure.config_manager import ConfigManager
from ..infrastructure.execution_recorder import ExecutionRecorder
from ..infrastructure.ssh_setup import SSHContextProvider
from ..infrastructure.coding_setup import CodingContextProvider
from ..tools.browser_tool import BrowserSessionHolder
from ..tools.desktop_tool import DesktopState
from ..tools.file_state import FileState
from ..tools.session_tool import SessionRegistry
from ..tools.ssh_tool import SshConnectionPool
from .context import ContextProvider, ItemContext, ProviderCache
from .interaction_manager import InteractionManager
from .session_context import SessionContext
from .shared_checklist import SharedCheckList, CheckListItem
from .orchestrator import Orchestrator
from .persistent_agent import PersistentAgent


class FlowControllerV2:
    """Session-scoped orchestrator. Thin shell over Orchestrator + Agent + CheckList.

    Public API:
      start()                          — bootstrap all components and loops
      on_user_message()                — forward user message to orchestrator
      destroy()                        — cancel all loops (clean session end)
      cancel_all_tasks()               — cancel loops without teardown (session swap)
      register_item_context_provider() — add a custom ContextProvider
    """

    def __init__(
        self,
        llm_services: List[LLMService],
        working_directory: Optional[str] = None,
        storage_directory: Optional[str] = None,
        config_path: Optional[str] = None,
        on_reply_to_user: Optional[Callable[[str], None]] = None,
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
        # Cheap auxiliary pool (llm.helper_models). Used by the Tier-1 progress
        # watcher in PersistentAgent — a fire-and-forget divergence check that
        # must NOT share service objects with the main streaming stack (no
        # shared stream state) and should run on cheap models. None/empty
        # disables the watcher entirely (Tier-0 sense still works).
        self._helper_llm_services: Optional[List[LLMService]] = helper_llm_services
        self.working_directory: Optional[str] = working_directory
        self.storage_directory: str = storage_directory or working_directory or "."
        self.config_manager = ConfigManager(config_path)
        self.logger = get_logger()

        self._on_reply_to_user = on_reply_to_user
        # When False, PersistentAgent's prompt env block lists only
        # working_directory and omits the session-storage root. Used by the
        # Windows GUI bridge so the agent's mental model is "workspace =
        # everything"; Linux CLI keeps the default True.
        self._expose_session_storage_in_prompt: bool = expose_session_storage_in_prompt

        try:
            self.interaction_manager: Optional[InteractionManager] = InteractionManager()
        except RuntimeError:
            self.interaction_manager = None

        # ProviderCache is a per-session namespaced dict store used ONLY by
        # ContextProviders for their cache.get / cache.set calls (cred caches,
        # "already prepared" flags). V2 has no notion of step history — that
        # role belongs to SharedCheckList. We instantiate the cache once so
        # providers have somewhere to write their per-host entries.
        self.cache: ProviderCache = ProviderCache()

        self._checklist: Optional[SharedCheckList] = None
        self._orchestrator: Optional[Orchestrator] = None
        self._agent: Optional[PersistentAgent] = None
        self._agent_task: Optional[asyncio.Task] = None
        self._planner_task: Optional[asyncio.Task] = None

        # Per-session resource bundle. Constructed in ``start()`` once the
        # checklist exists (the ctx forwards ``interrupt_event`` from it).
        # Owns FileState / SshConnectionPool / BrowserSessionHolder /
        # SessionRegistry / DesktopState — ``destroy()`` awaits ``ctx.close()``
        # to tear them all down so no per-session state leaks across sessions.
        self._ctx: Optional["SessionContext"] = None

        self._started = False

        # Provider system — populated by _collect_default_providers.
        # Provides:
        #   - on-demand tool table for the planner prompt (planner_description /
        #     planner_routing_rule / planner_antipatterns)
        #   - per-item hint injection (before_item) before an item starts so
        #     the agent has SSH credentials / browser session info / etc.
        #   - session-once setup (on_tool_activated) when the planner first
        #     activates a tool — wired via the on_tools_changed callback chain.
        self._item_context_providers: List[ContextProvider] = []
        self._collect_default_providers()

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bootstrap all components and start loops.

        After this returns:
          - SharedCheckList created (empty)
          - Orchestrator created with provider tool table populated
          - PersistentAgent loop is running (blocked on empty checklist)
          - Orchestrator planner loop is running (blocked on _planner_trigger)
          - System idle, awaiting user messages via on_user_message()
        """
        if self._started:
            return

        self.logger.info(
            "[FlowControllerV2] Starting session",
            component="FlowControllerV2",
        )

        execution_recorder = ExecutionRecorder(
            plan_id="persistent_session",
            goal="(session)",
            log_dir=self.storage_directory,
        )

        self._checklist = SharedCheckList()

        # Build the per-session resource bundle now that the checklist exists.
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
            _checklist=self._checklist,
        )

        self._orchestrator = Orchestrator(
            llm_services=self._llm_services,
            checklist=self._checklist,
            on_reply_to_user=self._forward_reply_to_ui,
            on_response_chunk=self._forward_reply_chunk_to_ui,
            on_response_done=self._seal_reply_to_ui,
            on_state_changed=self._forward_state_to_ui,
            on_recall_started=self._forward_recall_started_to_ui,
            on_task_complete=self._on_task_complete_cleanup,
            session_dir=self.storage_directory,
            helper_services=self._helper_llm_services,
        )

        # Push the platform-aware tool table from registered providers into
        # the orchestrator BEFORE the planning system prompt is first rendered.
        self._push_provider_table_to_orchestrator()

        self._agent = PersistentAgent(
            llm_services=self._llm_services,
            checklist=self._checklist,
            working_directory=self.working_directory,
            storage_directory=self.storage_directory,
            config_manager=self.config_manager,
            execution_recorder=execution_recorder,
            interaction_manager=self.interaction_manager,
            pre_item_hint_provider=self._gather_pre_item_hints,
            ctx=self._ctx,
            expose_session_storage_in_prompt=self._expose_session_storage_in_prompt,
            helper_services=self._helper_llm_services,
        )

        # Register the provider-side tool-activation hook AFTER PersistentAgent
        # has registered its tool-loading callback (via __init__). The order
        # makes the agent load the tool implementation first, then the provider
        # gets a chance to do session-once setup against the new tool.
        self._checklist.on_tools_changed(self._on_tools_activated)

        # Bridge checklist mutations → UI. FlowController owns both the
        # checklist and the InteractionManager, so it is the natural place to
        # forward the live task-panel snapshot (same role it plays for state
        # transitions via _forward_state_to_ui).
        self._checklist.on_checklist_changed(self._forward_checklist_to_ui)

        self._agent_task = asyncio.create_task(
            self._agent.run_loop(),
            name="persistent_agent_loop",
        )
        self._planner_task = asyncio.create_task(
            self._orchestrator.run_planner_loop(),
            name="planner_loop",
        )

        self._started = True

    async def on_user_message(self, message: str) -> str:
        """Single entry point for all user input.

        Forwards to the orchestrator, which handles intent + skill activation
        + checklist mutations. Replies are delivered to UI via _forward_reply_to_ui.

        Brackets the call with the receptionist "thinking…" indicator: on at
        entry, off in ``finally``. The renderer also clears it on the first
        ``reply_delta``, so for the streaming path the ``finally`` is a no-op;
        it only matters when ``response_to_user`` stays silent (e.g. a pure
        background re-plan) so the bubble doesn't linger.
        """
        if not self._started or self._orchestrator is None:
            return "Session not started."

        if self.interaction_manager is not None:
            try:
                self.interaction_manager.notify_receptionist_thinking()
            except Exception:
                pass
        try:
            return await self._orchestrator.on_user_message(message)
        finally:
            if self.interaction_manager is not None:
                try:
                    self.interaction_manager.clear_receptionist_thinking()
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
        """Trip the checklist interrupt event so tools parked on
        ``asyncio.wait([communicate, interrupt])`` (shell_tool, session_tool)
        wake up and terminate their subprocess trees before the event-loop
        cancellation lands.

        asyncio task cancellation alone does NOT reliably propagate to a
        running subprocess on Windows: a tool wedged in
        ``await process.communicate()`` will see ``CancelledError`` on the
        await but the OS-level child keeps running until it exits naturally.
        The interrupt event lives on the checklist (set normally by
        planner-driven ``interrupt_agent``) and we re-use it here for
        session-end so each new session starts on a clean slate without any
        leftover children from the prior session's tool calls.

        The agent loop will not get a chance to acknowledge the interrupt
        (the loop is being cancelled), and that is fine — the event lives on
        the checklist, which is dropped along with the flow.
        """
        if self._checklist is None:
            return
        try:
            self._checklist._interrupt_event.set()
        except Exception:
            self.logger.warning(
                "[FlowControllerV2] _signal_interrupt failed",
                component="FlowControllerV2",
            )

    def register_item_context_provider(self, provider: ContextProvider) -> None:
        """Register a custom ContextProvider.

        Provider's planner_* hooks contribute to the on-demand tool table that
        the planning system prompt sees. Provider's prepare() runs before any
        CheckListItem whose tools_required contains its tool_name, returning a
        hint string injected as a synthetic observation in the agent context.

        Call BEFORE start() so the tool table reflects the new provider on
        the first plan. Calling after start() works for pre-item injection
        but the planner prompt won't see the new tool until the next session.
        """
        self._item_context_providers.append(provider)
        if self._started and self._orchestrator is not None:
            self._push_provider_table_to_orchestrator()

    # ── Internal: loop management ────────────────────────────────────────────

    def _cancel_loops(self) -> None:
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        if self._planner_task and not self._planner_task.done():
            self._planner_task.cancel()

    # ── Internal: status / reply ─────────────────────────────────────────────

    def _forward_reply_to_ui(self, reply: str) -> None:
        """Forward orchestrator reply to external callback / UI."""
        if self._on_reply_to_user:
            self._on_reply_to_user(reply)
        elif self.interaction_manager and reply:
            try:
                self.interaction_manager.notify_inline_event("planner", reply[:500])
            except Exception:
                pass

    def _forward_reply_chunk_to_ui(self, fragment: str) -> None:
        """Stream one reply fragment (the receptionist/INTENT ``response_to_user``)
        to the UI as it arrives from the LLM. Renderer appends it to the live
        assistant bubble (``reply_delta``)."""
        if self.interaction_manager and fragment:
            try:
                self.interaction_manager.stream_receptionist_reply_chunk(fragment)
            except Exception:
                pass

    def _seal_reply_to_ui(self) -> None:
        """Finalize the current streamed reply bubble (``reply_done``)."""
        if self.interaction_manager:
            try:
                self.interaction_manager.seal_receptionist_reply()
            except Exception:
                pass

    def _forward_state_to_ui(self, state: str) -> None:
        """Forward an orchestrator state transition (``planning`` / ``idle``)
        to the UI. The agent emits ``thinking`` / ``executing`` directly via
        the InteractionManager; this covers the planner phase and the
        task-settled transition, which only the orchestrator sees.

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

    def _forward_checklist_to_ui(self, items: List[Dict[str, Any]]) -> None:
        """Forward a checklist snapshot (from ``on_checklist_changed``) to the
        UI, which paints a live task panel. ``items`` is the list of
        ``{item_id, instruction, status}`` dicts. Fire-and-forget."""
        if self.interaction_manager:
            try:
                self.interaction_manager.notify_checklist_changed(items)
            except Exception:
                pass

    # ── Provider system ──────────────────────────────────────────────────────

    def _collect_default_providers(self) -> None:
        """Collect built-in ContextProviders into self._item_context_providers.

        SSH and Coding providers are cross-platform; the rest are Windows-only
        and skipped on Linux. The Windows-only setup modules are imported
        lazily inside the win32 branch (not at module top level) because they
        pull eager hard deps — playwright, pywin32, pyautogui — that are absent
        on the headless Linux daemon; a top-level import would crash Linux boot
        before the runtime gate runs. The imports are still plain ``from ...``
        statements, so PyInstaller's bytecode scan discovers them for Windows
        packaging, and any missing dep surfaces here at session construction.
        """
        self._item_context_providers.append(SSHContextProvider())
        self._item_context_providers.append(CodingContextProvider())

        if sys.platform != "win32":
            self.logger.info(
                "Linux platform: skipping Windows-only provider registration",
                component="FlowControllerV2",
            )
            return

        from ..infrastructure.browser_setup import BrowserContextProvider
        from ..infrastructure.remote_handq_setup import RemoteHandQContextProvider
        from ..infrastructure.web_search_setup import WebSearchContextProvider
        from ..infrastructure.email_setup import EmailContextProvider
        from ..infrastructure.teams_setup import TeamsContextProvider
        from ..infrastructure.desktop_setup import DesktopContextProvider
        from ..infrastructure.ask_human_setup import AskHumanContextProvider
        from ..infrastructure.session_setup import SessionContextProvider

        self._item_context_providers.append(BrowserContextProvider())
        self._item_context_providers.append(RemoteHandQContextProvider())
        self._item_context_providers.append(WebSearchContextProvider())
        self._item_context_providers.append(EmailContextProvider())
        self._item_context_providers.append(TeamsContextProvider())
        self._item_context_providers.append(DesktopContextProvider())
        self._item_context_providers.append(AskHumanContextProvider())
        self._item_context_providers.append(SessionContextProvider())

    def _push_provider_table_to_orchestrator(self) -> None:
        """Build dynamic planner sections from providers and push to Orchestrator.

        After this call, the planning system prompt (rendered on the next LLM
        request) will list every registered provider's tool name + activation
        condition + routing rule + anti-patterns.
        """
        if self._orchestrator is None:
            return

        table_rows: List[str] = []
        routing_rules: List[str] = []
        antipatterns: List[str] = []

        for provider in self._item_context_providers:
            row = provider.planner_description()
            if row:
                table_rows.append(f"| {row} |\n")
            rule = provider.planner_routing_rule()
            if rule:
                routing_rules.append(rule)
            for ap in provider.planner_antipatterns():
                antipatterns.append(ap)

        # Routing rules are merged with static ones via plain bullets; "first
        # match wins, top to bottom" still works without numbers, and we no
        # longer need the static-rule count to align the trailing coding rule.
        formatted_rules = "".join(f"- {rule}\n" for rule in routing_rules)
        formatted_antipatterns = "".join(f"  ❌ {ap}\n" for ap in antipatterns)

        self._orchestrator._on_demand_tools_table   = "".join(table_rows)
        self._orchestrator._on_demand_routing_rules = formatted_rules
        self._orchestrator._on_demand_antipatterns  = formatted_antipatterns

        tool_names = [
            p.tool_name for p in self._item_context_providers
            if p.planner_description()
        ]
        self.logger.info(
            f"Planner tool table updated: {len(table_rows)} dynamic tool(s) — {tool_names}",
            component="FlowControllerV2",
        )

    async def _gather_pre_item_hints(self, item: CheckListItem) -> str:
        """Per-item provider hints — dispatches to all eligible providers.

        Eligibility: the provider's ``tool_name`` is in ``checklist.active_tools``
        (planner approved it). Each eligible provider's ``before_item`` is
        invoked; the default implementation returns ``None`` so non-overriding
        providers are silently filtered out when collecting hints.

        Special case: when ``item.ssh_target`` is set, ``SSHContextProvider``
        is invoked even if the ``ssh`` tool itself was not activated. The
        planner often (correctly) declines to activate ``ssh`` for single
        remote commands — the agent just runs ``shell`` with ``ssh host 'cmd'``.
        Without the SSH context hint, the agent has to rediscover the
        credential layout (``~/.ssh/handq_<host>.yaml`` + keyring service)
        from scratch every session.

        Called by PersistentAgent before each item execution. Session-once
        provider setup runs separately via ``_on_tools_activated``.

        Returns "" when no provider produces output.
        """
        if self._checklist is None or self.interaction_manager is None:
            return ""
        active_tools = self._checklist.active_tools
        eligible: List[ContextProvider] = [
            p for p in self._item_context_providers
            if p.tool_name in active_tools
        ]
        if (item.ssh_target or "").strip():
            for p in self._item_context_providers:
                if p.tool_name == "ssh" and p not in eligible:
                    eligible.append(p)
                    break
        if not eligible:
            return ""

        ctx = ItemContext.from_item(item)
        im = self.interaction_manager

        hint_parts: List[str] = []
        for provider in eligible:
            try:
                hint = await provider.before_item(
                    ctx, im, self.cache
                )
                if hint:
                    hint_parts.append(hint)
            except Exception as exc:
                self.logger.warning(
                    f"{provider.tool_name} provider failed for item "
                    f"'{item.item_id}': {exc}",
                    component="FlowControllerV2",
                )
                hint_parts.append(
                    f"[Context Setup Warning]\n"
                    f"{provider.tool_name} provider failed: {exc}"
                )

        return "\n\n".join(p for p in hint_parts if p)

    def _on_tools_activated(self, names: List[str]) -> None:
        """Provider-side hook for newly-activated tools.

        Sync callback (per ``SharedCheckList.on_tools_changed`` contract).
        For each newly-activated tool name with a matching provider, schedule
        the provider's async ``on_tool_activated`` as a fire-and-forget task.

        ``on_tool_activated`` is a session-once setup hook. Providers that need
        per-item context override ``before_item`` instead (called from
        ``_gather_pre_item_hints``). Providers may override both.
        """
        if not names:
            return
        for name in names:
            provider = next(
                (p for p in self._item_context_providers if p.tool_name == name),
                None,
            )
            if provider is None:
                continue
            try:
                asyncio.create_task(self._run_on_tool_activated(provider))
            except RuntimeError:
                # No running loop — can happen if this fires before the main
                # loop is ready. Provider's per-item path will compensate.
                self.logger.debug(
                    f"_on_tools_activated: no running loop for {name}; "
                    f"deferring to per-item path",
                    component="FlowControllerV2",
                )

    async def _run_on_tool_activated(self, provider: ContextProvider) -> None:
        """Wrap one provider's session-once hook with logging + exception isolation."""
        if self.interaction_manager is None:
            return
        try:
            await provider.on_tool_activated(self.interaction_manager, self.cache)
        except Exception as exc:
            self.logger.warning(
                f"{provider.tool_name} on_tool_activated failed: {exc}",
                component="FlowControllerV2",
            )
