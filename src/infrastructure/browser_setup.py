# -*- coding: utf-8 -*-
"""BrowserContextProvider — prepare browser profile when Planner declares it.

Activation is purely Planner-driven: when "browser" appears in
step.tools_required, FlowController invokes prepare() to verify playwright
availability, ensure the persistent profile directory exists, and return
the workflow hint. There is no keyword scan.

Windows-only: the browser tool is registered Windows-only in ToolRegistry,
and FlowController only registers this provider on Windows.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .browser_paths import user_browser_profile_dir
from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


def _build_full_hint(profile_dir: str, attach_enabled: bool) -> str:
    """First-time hint — full workflow guide for this task."""
    attach_block = (
        "\n"
        "ATTACH MODE (advanced, available when the user's main Chrome state matters):\n"
        "  Default to launch_browser. Use action='attach_browser' ONLY when the\n"
        "  task explicitly references the user's RUNNING Chrome — phrases like\n"
        "  '刚才/正在/我现在打开的/接着我那个'. attach_browser requires user\n"
        "  approval (high-risk) and the user must have started Chrome with\n"
        "  --remote-debugging-port=9222 (see scripts/start_chrome_with_debug.bat).\n"
        "  In attach mode, action='new_tab' opens tabs in the background so the\n"
        "  user's focused tab is not switched.\n"
        if attach_enabled else ""
    )
    return (
        "[Browser Context — first activation in this task]\n"
        "The 'browser' tool is now available. It drives a Chromium browser\n"
        "(Edge by default) with a persistent profile at:\n"
        f"  {profile_dir}\n"
        "Cookies and login state survive across HandQ sessions in that dir,\n"
        "so the user only logs in once per site. The window launches\n"
        "off-screen to keep the user's desktop undisturbed.\n"
        "\n"
        "Workflow:\n"
        "  1. action='launch_browser' (idempotent — safe to call repeatedly).\n"
        "  2. action='navigate', url='https://...' to load a page. The result\n"
        "     includes 'page_state' (open dialog + toast text); inspect it\n"
        "     before deciding the next call.\n"
        "  3. action='snapshot' — preferred entry point for figuring out a\n"
        "     page. Returns every interactable element with a suggested\n"
        "     selector + any open modal. Use this BEFORE trying to guess\n"
        "     selectors via repeated extract probes.\n"
        "  4. action='extract' to read content (mode='text' default;\n"
        "     mode='list' with a selector + limit to enumerate matches).\n"
        "  5. action='click' / 'type' to interact. Both echo 'page_state'\n"
        "     after the action — read it instead of running a follow-up\n"
        "     extract just to see what changed.\n"
        "  6. action='wait_for' for selectors / URL patterns when needed.\n"
        "  7. If you hit a login wall: action='request_user_login' so the\n"
        "     user can log in manually. The agent NEVER reads or types\n"
        "     passwords — input[type=password] is REFUSED server-side.\n"
        "  8. action='close_tab' for tabs you no longer need.\n"
        + attach_block
    )


def _build_brief_hint() -> str:
    """Subsequent-step hint — short reminder; full workflow already in this task."""
    return (
        "[Browser Context] tool 'browser' available; "
        "the persistent session and login cookies from earlier steps are reused."
    )


class BrowserContextProvider(StepContextProvider):
    """Prepare the browser tool's persistent profile when the Planner declares it.

    No keyword scanning — activation is purely declaration-driven via
    step.tools_required. Cheap setup (mkdir + memory cache lookup); first
    invocation per task emits the full workflow hint, subsequent invocations
    emit the brief reminder.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "browser"

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        # Verify playwright importable. If missing, surface a clear hint so
        # the agent can report the issue rather than blindly calling the
        # tool and getting an opaque ImportError later.
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.logger.warning(
                "BrowserContextProvider: playwright not installed — tool calls will fail",
                component="BrowserContextProvider",
            )
            return (
                "[Browser Context — UNAVAILABLE]\n"
                "playwright is not installed. The browser tool will fail. Run:\n"
                "  pip install playwright\n"
                "  playwright install msedge\n"
                "and retry. If that is not possible, report to the user that "
                "this task requires browser automation which is unavailable."
            )

        # Ensure the persistent profile dir exists. user_browser_profile_dir
        # is idempotent (mkdir parents=True, exist_ok=True).
        try:
            profile_dir = user_browser_profile_dir()
        except Exception as exc:
            self.logger.error(
                f"BrowserContextProvider: cannot create profile dir: {exc}",
                component="BrowserContextProvider",
            )
            return (
                "[Browser Context — UNAVAILABLE]\n"
                f"Could not create the browser profile directory: {exc}\n"
                "Report this to the user and skip the browser path."
            )

        # Read attach_enabled from config so the hint mentions attach mode
        # only when it's actually usable. Best-effort; failure → attach
        # block omitted from the hint (the LLM won't try a disabled action).
        attach_enabled = False
        try:
            from .config_manager import ConfigManager
            cm = ConfigManager()
            browser_cfg = cm.get_section("browser") or {}
            attach_enabled = bool(browser_cfg.get("attach_enabled", False))
        except Exception as exc:
            self.logger.debug(
                f"BrowserContextProvider: cannot read attach_enabled: {exc}",
                component="BrowserContextProvider",
            )

        # Progressive disclosure: full guide on first activation in this task,
        # brief reminder thereafter.
        cached = memory.get_browser_context("default")
        if cached and cached.get("prepared"):
            return _build_brief_hint()

        memory.set_browser_context("default", {
            "prepared": True,
            "profile_dir": profile_dir,
            "attach_enabled": attach_enabled,
        })
        return _build_full_hint(profile_dir, attach_enabled)
