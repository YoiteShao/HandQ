# -*- coding: utf-8 -*-
"""DesktopContextProvider — emit desktop usage hint when Planner declares it.

Activation is purely Planner-driven: when "desktop" appears in
step.tools_required, FlowController invokes prepare() to inject the
workflow + tool-choice-hierarchy hint. There is no keyword scan — the
desktop tool is the most disruptive in the kit (steals real mouse +
keyboard) so we now require the Planner to make an explicit decision.

Windows-only: the desktop tool is registered Windows-only in ToolRegistry,
and FlowController only registers this provider on Windows.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


def _build_full_hint() -> str:
    """First-time hint — full workflow + the TOOL CHOICE HIERARCHY rule."""
    return (
        "[Desktop Context — first activation in this task]\n"
        "The 'desktop' tool is now available. It drives Windows mouse +\n"
        "keyboard at the OS level — every input action is INDISTINGUISHABLE\n"
        "from real human input. Read the rules below before calling.\n"
        "\n"
        "USE THIS TOOL — DO NOT ROLL YOUR OWN:\n"
        "  Do NOT write ad-hoc Python scripts with pywinauto / uiautomation /\n"
        "  pywin32 / comtypes / win32com.client to automate the desktop and\n"
        "  shell-out to run them. The 'desktop' tool here already wraps\n"
        "  screenshots, OCR, vision matching, and pyautogui — using shell +\n"
        "  custom scripts is 5-10x slower (write file, run python, parse\n"
        "  output, repeat) and the user does NOT see the takeover indicator\n"
        "  for those calls. If a workflow truly cannot be expressed with the\n"
        "  desktop actions below, ASK the user before falling back.\n"
        "\n"
        "TOOL CHOICE HIERARCHY (re-check before each desktop action):\n"
        "  ✅ Use desktop ONLY when the target is a NATIVE Windows app:\n"
        "     Notepad, Excel, File Explorer, Settings, Task Manager, etc.\n"
        "  ❌ For these, use the named tool instead, NOT desktop:\n"
        "     - Web pages / URLs       → browser   (DOM is deterministic, ~10x faster)\n"
        "     - File read/write/edit   → read / write / edit\n"
        "     - Run command / script   → shell\n"
        "     - Search files           → glob / grep\n"
        "     - Remote machine         → ssh\n"
        "  Even if you can SEE a target on screen, prefer the specialised\n"
        "  tool — desktop is the LAST resort, not a shortcut.\n"
        "\n"
        "ACTION HIERARCHY (within desktop — check top-down before each action):\n"
        "  1. hotkey / key_press        — fastest, no vision dependency. If you\n"
        "                                 already KNOW the shortcut for the\n"
        "                                 target action (Ctrl+S to save, F5 to\n"
        "                                 refresh, Alt+F4 to close, Ctrl+L to\n"
        "                                 focus an address bar, etc.), USE IT.\n"
        "                                 Don't go hunt for a 'Save' menu when\n"
        "                                 Ctrl+S exists.\n"
        "  2. find_and_click on a UIA-named control — robust to layout shifts;\n"
        "                                 use when no hotkey exists for the\n"
        "                                 action or the app is unfamiliar.\n"
        "  3. snapshot → click_at(x, y) — when you need a structured listing\n"
        "                                 of every interactable control first.\n"
        "                                 The snapshot result is CACHED per\n"
        "                                 hwnd until the UI state changes, so\n"
        "                                 calling it twice in a row is cheap.\n"
        "  4. screenshot+OCR / vision_fallback — last resort. Slow (~700 ms-5 s)\n"
        "                                 and brittle to label rendering. Only\n"
        "                                 when the target is iconographic or\n"
        "                                 not exposed via UIA.\n"
        "\n"
        "WORKFLOW (typical):\n"
        "  1. shell(...) — launch the target app if not already open.\n"
        "  2. desktop.list_windows — confirm the app is foregrounded.\n"
        "  3. If you already know the right hotkey for what you want, fire\n"
        "     desktop.hotkey(...) directly — no inspection needed first.\n"
        "  4. desktop.snapshot — for unfamiliar UIs / when you need a\n"
        "     structured control list. Result is cached until state changes;\n"
        "     re-calling after a click that opened a menu rebuilds the cache,\n"
        "     re-calling on the same UI is ~free.\n"
        "  5. desktop.find_and_click, description='<label>' — when the target\n"
        "     is visual-only or you described it textually. Combo: screenshot\n"
        "     + OCR + vision-fallback + click in one tool call.\n"
        "  6. desktop.screenshot, region='foreground', with_ocr=true — only\n"
        "     when #3-5 are not enough (e.g. you want every visible label's\n"
        "     coords in one shot). Re-capture only when the UI state has\n"
        "     actually changed; capture-spam is the #1 slowness.\n"
        "  7. desktop.type_text / drag / scroll — drive specifics. type_text\n"
        "     is capped at 4000 chars (use clipboard via shell for long pastes).\n"
        "\n"
        "TAKEOVER + REVOCATION:\n"
        "  As soon as the user APPROVES desktop control (or auto_approve\n"
        "  is on), a HIGHLY VISIBLE on-screen indicator (full-screen\n"
        "  rainbow border + watermark) appears so the user knows the agent\n"
        "  is driving. The user can hit Ctrl+Shift+C any time to REVOKE\n"
        "  control — your subsequent click_at / type_text / drag / scroll /\n"
        "  hotkey / key_press will need RE-APPROVAL. Read-only desktop\n"
        "  actions (screenshot / list_windows / find_element) keep working.\n"
        "  If you see 'REFUSED: user revoked desktop control', stop using\n"
        "  desktop input — finish or report.\n"
        "\n"
        "SENSITIVE WINDOW REFUSAL (HARD):\n"
        "  Banking / password manager / wallet app foreground is refused\n"
        "  outright by the tool itself. You cannot bypass this. If the\n"
        "  user's foreground happens to land on such a window, ask them\n"
        "  to switch focus before retrying.\n"
        "\n"
        "PASSWORD GUARD:\n"
        "  Like browser_tool, you must NEVER type a password. The user\n"
        "  enters credentials directly. (No technical guard for arbitrary\n"
        "  text fields — use judgement: if the field looks like a password\n"
        "  prompt, do not type into it; ask the user.)\n"
    )


def _build_brief_hint() -> str:
    """Subsequent-step hint — brief reminder. Full guide already shown."""
    return (
        "[Desktop Context] tool 'desktop' available; same TOOL CHOICE "
        "HIERARCHY as before — prefer browser / shell / read / write "
        "when they fit. ACTION HIERARCHY: hotkey > find_and_click > "
        "snapshot+click_at > screenshot+OCR. Snapshot results are cached "
        "until UI state changes. Takeover state from earlier actions "
        "persists until task end."
    )


class DesktopContextProvider(StepContextProvider):
    """Inject the desktop usage hint when the Planner declares it.

    No keyword scanning — activation is purely declaration-driven. Required
    transitive deps (pyautogui / mss / pywin32 / rapidocr) are bundled in
    both dev and packaged builds; missing deps surface their own pip-install
    hint at the call site, mirroring how every other tool handles them.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    @property
    def tool_name(self) -> str:
        return "desktop"

    def planner_description(self) -> str:
        return (
            "`desktop` | "
            "Native Windows app automation: Notepad, Excel, File Explorer, Settings, Task Manager, "
            "Office apps, third-party desktop software. Drives real mouse / keyboard. "
            "❌ Do NOT use for web pages — use `browser` instead. "
            "Routing: `[\"desktop\"]`. | "
            "Step references a native app or screen-level action outside the browser"
        )

    def planner_routing_rule(self) -> str:
        return "Native Windows app interaction → `tools_required: [\"desktop\"]`"

    def planner_antipatterns(self) -> list:
        return [
            '`["desktop"]` for clicking on a web page — that\'s `["browser"]`',
        ]

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        # Progressive disclosure: full guide on first activation in this task,
        # brief reminder thereafter.
        cached = memory.get_desktop_context("default")
        if cached and cached.get("prepared"):
            return _build_brief_hint()

        memory.set_desktop_context("default", {"prepared": True})
        return _build_full_hint()
