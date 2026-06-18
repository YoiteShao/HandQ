"""Per-session resource container.

One ``SessionContext`` is created per :class:`FlowControllerV2` session and owns:

* the UI bus (:class:`InteractionManager`)
* identification / config (working dir, storage dir, config manager)
* a forwarding handle on the checklist's interrupt event
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
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..infrastructure.config_manager import ConfigManager
    from ..infrastructure.execution_recorder import ExecutionRecorder
    from ..tools.browser_tool import BrowserSessionHolder
    from ..tools.desktop_tool import DesktopState
    from ..tools.file_state import FileState
    from ..tools.session_tool import SessionRegistry
    from ..tools.ssh_tool import SshConnectionPool
    from .interaction_manager import InteractionManager
    from .shared_checklist import SharedCheckList


_logger = logging.getLogger("handq.controller_v2.session_context")


@dataclass
class SessionContext:
    """Per-session resource bundle. Constructed in ``FlowControllerV2.start()``
    after the SharedCheckList is built; passed to ``PersistentAgent`` and from
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

    # ── Late-bound checklist for interrupt event forwarding ──────────────
    # SharedCheckList owns ``_interrupt_event`` because the planner-driven
    # ``interrupt_agent(reason)`` writes to it. Tools (shell / session) need to
    # read it. Rather than duplicating the event, the ctx exposes a forwarding
    # property — single source of truth on the checklist, but tools find it
    # via ctx so they don't need to know about the checklist directly.
    _checklist: Optional["SharedCheckList"] = field(default=None, repr=False)

    # Idempotence flag for close(). _do_new_session and _do_shutdown can both
    # land on the same flow if the user clicks New right before Quit.
    _closed: bool = field(default=False, repr=False)

    # ── Public properties ────────────────────────────────────────────────

    @property
    def interrupt_event(self) -> asyncio.Event:
        """The asyncio.Event tools listen on for "session is being torn down,
        cancel any in-flight subprocess wait". Set by:
          * ``SharedCheckList.interrupt_agent(reason)`` — planner-driven mid-task
            interrupt (normal operation)
          * ``FlowControllerV2._signal_interrupt()`` — session-end (this is what
            propagates "kill subprocesses" through to shell_tool / session_tool's
            ``asyncio.wait([communicate, interrupt_wait])`` race)
        """
        if self._checklist is None:
            # Standalone instance (test fixture) without a checklist — return an
            # event that can be set/cleared but doesn't share state with anyone.
            if not hasattr(self, "_orphan_interrupt"):
                self._orphan_interrupt = asyncio.Event()  # type: ignore[attr-defined]
            return self._orphan_interrupt  # type: ignore[attr-defined]
        return self._checklist._interrupt_event

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
