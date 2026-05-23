# -*- coding: utf-8 -*-
"""BrowserContextProvider — activate the browser tool by step keywords.

Mirrors :class:`SSHContextProvider` but the model is simpler:

* No per-target identity (browser is process-wide; one persistent profile
  shared across all FlowController sessions in this process). Memory is
  keyed by a constant ``"default"`` slot — Phase 5 may add attach-mode
  probe slots beside it.
* No credential prompting. Authentication is deferred to
  ``request_user_login`` (Phase 3) when an actual login wall is hit.
* No I/O in :meth:`matches`. Just a keyword/URL scan over the step text.

Lifecycle wired into FlowController:

1. Before every step, ``matches(step)`` runs.
2. When True, ``prepare(step, im, memory)`` returns a hint string and
   ``extra_tool_names()`` returns ``["browser"]`` so the runtime agent
   gets the on-demand tool added to its LLM call.
3. The hint is appended to ``effective_goal`` so the LLM sees
   "Tool 'browser' is available, here is how to use it.".
4. Memory caches a ``"prepared": True`` flag so subsequent steps in the
   same task get a brief reminder instead of the full workflow guide
   (mirrors SSH progressive disclosure).

Windows-only: :func:`is_windows` from :mod:`browser_paths` gates the
provider — on non-Windows hosts it never matches, so the browser tool
remains hidden from the LLM (the tool itself is also not registered
on non-Windows; this is belt-and-suspenders).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

from .browser_paths import is_windows, user_browser_profile_dir
from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


# Keyword set deliberately conservative to avoid false positives like
# "open the file" / "click <button name in CLI tool>". A bare "click"
# without context is too vague — we require pairs ("click link",
# "click button") or domain-specific phrases ("登录" / "网站").
_BROWSER_KEYWORDS = frozenset({
    # English — multi-word phrases reduce false positives
    "browser", "website", "webpage", "navigate to", "go to https",
    "log in to", "login to", "click link", "click button",
    # Single-word terms that strongly imply web context
    "url",
    # Chinese
    "网页", "网站", "浏览器", "登录", "网址", "链接",
    "打开网页", "打开网站",
})

# Strong signal: any URL in the step text. Matches https://example.com
# but NOT a bare hostname so file paths like /tmp/example.com don't trip.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


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
    """Activate the browser tool when a step looks like it needs web automation.

    Attaches as a default StepContextProvider in :class:`FlowController` so
    every step is screened. Cheap (keyword + regex scan); no I/O until
    :meth:`prepare` is called.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    def matches(self, step: "Step") -> bool:
        # Belt-and-suspenders: the tool itself is registered Windows-only,
        # and so is this provider's activation. Non-Windows hosts never
        # see browser actions — even if an external StepContextProvider
        # registry tried to enable us.
        if not is_windows():
            return False

        text = f"{step.goal} {step.description}".lower()
        for kw in _BROWSER_KEYWORDS:
            if kw in text:
                self.logger.debug(
                    f"BrowserContextProvider matched step {step.step_id!r} "
                    f"via keyword: {kw!r}",
                    component="BrowserContextProvider",
                )
                return True
        if _URL_RE.search(text):
            self.logger.debug(
                f"BrowserContextProvider matched step {step.step_id!r} via URL pattern",
                component="BrowserContextProvider",
            )
            return True
        return False

    def extra_tool_names(self) -> List[str]:
        return ["browser"]

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

        # Progressive disclosure mirrors SSH: full guide on first activation
        # in this task, brief reminder thereafter. The brief reminder still
        # tells the agent the tool exists and that prior cookies are alive,
        # but skips the multi-line workflow it has already seen.
        cached = memory.get_browser_context("default")
        if cached and cached.get("prepared"):
            return _build_brief_hint()

        memory.set_browser_context("default", {
            "prepared": True,
            "profile_dir": profile_dir,
            "attach_enabled": attach_enabled,
        })
        return _build_full_hint(profile_dir, attach_enabled)
