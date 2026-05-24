# -*- coding: utf-8 -*-
"""DesktopContextProvider — activate the desktop tool by step keywords.

Mirrors :class:`BrowserContextProvider` (see :mod:`browser_setup`):

* Cheap matching: keyword + regex scan over ``step.goal + step.description``.
* No I/O in :meth:`matches`. Provider lookup happens before every step.
* Progressive disclosure via :class:`Memory`: first activation in a task
  emits the full workflow guide; subsequent steps get a brief reminder.
* Windows-only — the desktop tool itself is registered Windows-only,
  this provider's :meth:`matches` returns False on other platforms as a
  belt-and-suspenders measure.

The provider exists because :data:`DesktopTool.parameter_schema` is
``on_demand=True`` — without an active provider the LLM never sees the
tool. By gating activation on **explicit native-app cues** rather than
making it always-on, we avoid the failure mode where the LLM reaches
for desktop CUA on tasks that browser_tool / shell / read / write
should handle (those are 5-10× faster and deterministic). See
``docs/desktop_tool.md`` §11 for the takeover-indicator IPC contract
that fires once the tool is actually used.

Activation keywords are intentionally CONSERVATIVE — high precision
matters more than recall here, because the desktop tool steals the
user's actual mouse + keyboard. Adding more keywords later is cheap;
firing on a false positive and confusing the agent is expensive.
"""
from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING, List, Optional

from .logger import get_logger
from .step_context_provider import StepContextProvider

if TYPE_CHECKING:
    from ..controller.interaction_manager import InteractionManager
    from .memory import Memory
    from ..models.plan import Step


# Strong-signal keywords. Pure native-app names + phrases that have no
# meaningful browser interpretation. "settings" alone is too ambiguous
# (could be website settings page) so we require "windows settings" /
# "系统设置".
_DESKTOP_KEYWORDS = frozenset({
    # Native-only Windows apps (English)
    "notepad", "calculator", "task manager", "control panel",
    "file explorer", "device manager", "registry editor",
    "command prompt", "powershell window",
    "windows settings", "windows explorer",

    # Microsoft Office / 365 — desktop apps. The browser does have
    # office.com but when the user says "OneNote / Excel / Word /
    # PowerPoint / Outlook" without "online" or a URL, they mean the
    # native client. Same for Teams desktop and the M365 Copilot app.
    "onenote", "outlook", "powerpoint", "ms word", "ms excel",
    "microsoft word", "microsoft excel", "microsoft powerpoint",
    "microsoft outlook", "microsoft onenote", "microsoft teams",
    "ms teams", "teams desktop", "m365 copilot", "copilot app",

    # Other common third-party desktop apps
    "notepad++", "vscode", "vs code", "visual studio",
    "windows store", "microsoft store",

    # Phrases that strongly imply desktop, not browser / not a generic word
    "on the desktop", "click on screen", "click on desktop",
    "type on screen", "drag the file to", "on the taskbar",
    "system tray", "minimise the window", "minimize the window",
    "maximise the window", "maximize the window",
    "screenshot the screen", "screenshot the desktop",
    "native app", "desktop app", "desktop application",
    "windows app", "uwp app", "win32 app",

    # Native-only Windows apps (Chinese)
    "记事本", "计算器", "画图", "任务管理器", "控制面板",
    "文件管理器", "文件资源管理器", "设备管理器",
    "注册表编辑器", "Windows 设置", "Windows 应用",

    # Office / 365 (Chinese)
    "邮件应用", "邮件客户端", "便签",

    # Chinese phrases — desktop-specific
    "桌面上", "在桌面", "任务栏", "系统托盘", "最小化窗口",
    "最大化窗口", "桌面截图", "桌面应用", "本地应用",
    "本地的", "桌面应用程序", "桌面客户端", "本地客户端",
    "原生应用", "Windows 应用程序", "GUI 自动化", "GUI自动化",

    # Internal tool
    "QUTS", "PCAT", "ALPACA", "QXDM"
})


# "open / launch / run / 打开 / 启动 / 运行 + native app" pattern. The
# native-app whitelist is the same set as _DESKTOP_KEYWORDS' first block
# plus a few common shorthands (notepad without a verb is already in
# the keyword set).
_OPEN_NATIVE_RE = re.compile(
    r"(?:open|launch|run|start|打开|启动|运行|使用)\s*"
    r"(?:本地的?|本机的?|local\s+|native\s+)?"
    r"(?:notepad|calculator|excel|word|powerpoint|outlook|onenote|"
    r"explorer|paint|cmd|powershell|teams|copilot|"
    r"file\s*manager|control\s*panel|task\s*manager|"
    r"记事本|计算器|画图|任务管理器|控制面板|"
    r"文件管理器|文件资源管理器|设备管理器)",
    re.IGNORECASE,
)


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
    """Activate the desktop tool when a step explicitly references native
    Windows apps or screen-level actions.

    Cheap (one substring scan + one regex scan over the step text) and
    matches conservatively: the desktop tool is the most powerful and
    most disruptive tool in the kit, so we'd rather miss an activation
    and fall back to specialised tools than reach for it on every
    "click" / "open" mention.
    """

    def __init__(self) -> None:
        self.logger = get_logger()

    def matches(self, step: "Step") -> bool:
        # Belt-and-suspenders: the tool itself is registered Windows-only.
        if sys.platform != "win32":
            return False

        text = f"{step.goal} {step.description}".lower()
        for kw in _DESKTOP_KEYWORDS:
            if kw.lower() in text:
                self.logger.debug(
                    f"DesktopContextProvider matched step {step.step_id!r} "
                    f"via keyword: {kw!r}",
                    component="DesktopContextProvider",
                )
                return True
        if _OPEN_NATIVE_RE.search(text):
            self.logger.debug(
                f"DesktopContextProvider matched step {step.step_id!r} "
                "via 'open/launch/打开 + native-app' pattern",
                component="DesktopContextProvider",
            )
            return True
        return False

    def extra_tool_names(self) -> List[str]:
        return ["desktop"]

    async def prepare(
        self,
        step: "Step",
        interaction_manager: "InteractionManager",
        memory: "Memory",
    ) -> Optional[str]:
        # Dependencies (pyautogui / mss / pywin32 / rapidocr) are
        # bundled in both dev (requirements.txt) and packaged builds
        # (Nuitka --include-package-data), so we don't probe at
        # prepare-time. If something IS missing, the relevant action
        # surfaces its own "pip install X" hint at the call site —
        # matches how every other tool handles missing transitive deps.

        # Progressive disclosure mirrors browser provider exactly.
        cached = memory.get_desktop_context("default")
        if cached and cached.get("prepared"):
            return _build_brief_hint()

        memory.set_desktop_context("default", {"prepared": True})
        return _build_full_hint()
