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
        "WORKFLOW (typical):\n"
        "  1. shell(...) — launch the target app if not already open.\n"
        "  2. desktop.list_windows — confirm the app is foregrounded.\n"
        "  3. desktop.screenshot, region='foreground', with_ocr=true —\n"
        "     ONE call returns the image PLUS every visible text region\n"
        "     with screen-space (x, y) ready for click_at. Drive several\n"
        "     follow-up clicks off this single result; do NOT re-capture\n"
        "     before every action. Re-capture only when the UI state has\n"
        "     actually changed (after a click that opens a menu, after a\n"
        "     hotkey, after typing). Capture-spam is the #1 slowness.\n"
        "  4. desktop.find_and_click, description='<label>' — when the\n"
        "     target is NOT in the with_ocr list (visual-only icons,\n"
        "     elements you described visually). Combo: screenshot + OCR\n"
        "     + vision-fallback + click in one call. Prefer this over\n"
        "     find_element + click_at split.\n"
        "  5. desktop.type_text / hotkey / key_press / drag / scroll —\n"
        "     drive it. type_text is capped at 4000 chars (use clipboard\n"
        "     via shell for long pastes).\n"
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
        "when they fit. Takeover state from earlier actions persists "
        "until task end."
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
