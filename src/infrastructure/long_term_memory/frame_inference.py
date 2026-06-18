"""Frame inference: process + window title → execution-frame dict.

Pure-function module. No async, no IO. Maps the cheap-to-capture signals
(``GetWindowThreadProcessId`` + ``GetWindowText``) to a structured
``{os, host, confidence, evidence}`` frame that the rest of the LTM
pipeline uses to anchor observations and recall.

Why heuristic and not LLM:
- Frame inference runs **per snapshot** (potentially many per minute).
  An LLM call here is cost-prohibitive.
- The process name is a hard signal: ``powershell.exe`` cannot be on
  Linux. The window title is a soft signal: ``user@host:`` in a terminal
  emulator's title strongly implies SSH context.
- The LLM-side semantic extractor (run once per session) gets a chance
  to RECONFIRM or downgrade the per-snapshot frame at higher granularity,
  but never to UPGRADE — process signal is the floor.

Confidence ranges (matches the recall-time `_frame_compatible` thresholds):
    0.90+ : high — process unambiguous (PowerShell → Windows)
    0.60-0.90 : medium — process + title agree (mintty + user@host)
    0.30-0.60 : low — only weak signal (browser, generic editor)
    <0.30 : ambient — observation is about something on screen, not the
                       agent's execution environment
"""
from __future__ import annotations

import re
from typing import Optional

# Process name → frame os classification. All lowercased.
WINDOWS_PROCESSES = frozenset({
    "powershell.exe", "pwsh.exe", "cmd.exe", "explorer.exe",
    "windowsterminal.exe", "wt.exe", "notepad.exe", "notepad++.exe",
    "outlook.exe", "winscp.exe", "filezilla.exe",
})

# Terminal emulators that commonly host SSH/WSL sessions to Linux hosts.
# Title parsing for `user@host:` distinguishes which Linux machine.
LINUX_SSH_PROCESSES = frozenset({
    "mintty.exe", "wsl.exe", "wezterm.exe", "wezterm-gui.exe",
    "mobaxterm.exe", "putty.exe", "kitty.exe", "alacritty.exe",
})

# Remote-desktop clients. Title typically embeds the target host name.
REMOTE_DESKTOP_PROCESSES = frozenset({
    "mstsc.exe", "vncviewer.exe", "anydesk.exe", "teamviewer.exe",
})

# Apps whose content is "about something else" (browser tab, editor file).
# Frame stays Windows-local but with very low confidence so dim recall
# filtering treats them as non-authoritative.
AMBIENT_PROCESSES = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "code.exe",         # VS Code often shows Linux paths inside Windows process
    "cursor.exe", "pycharm64.exe", "idea64.exe", "studio64.exe",
    "discord.exe", "slack.exe", "teams.exe", "wechat.exe",
})

# user@host pattern in window title. Anchored on @ to avoid matching
# email addresses inside arbitrary text. Hostnames may contain dashes
# (e.g. longjian6-gv) and dots (e.g. apt-lv-sh186.qualcomm.com).
_USER_AT_HOST_RE = re.compile(
    r"\b([a-z_][\w-]*)@([a-z0-9][a-z0-9.\-]+)",
    re.IGNORECASE,
)

# Remote Desktop window title patterns — English + zh-CN seen in the wild.
_RDP_TITLE_RES = (
    re.compile(r"Remote\s+Desktop\s+Connection[\s\-—–]+(.+)$", re.IGNORECASE),
    re.compile(r"远程桌面连接[\s\-—–]+(.+)$"),
)


def infer_frame(
    process_name: Optional[str],
    window_title: Optional[str],
    ax_text: Optional[str] = None,
) -> dict:
    """Return ``{os, host, confidence, evidence}`` for the foreground.

    ``ax_text`` (UIA accessibility text) is consulted for additional
    confidence boosts when available — e.g. seeing a ``PS C:\\`` prompt
    in ax_text raises Windows confidence past 0.95.
    """
    proc = (process_name or "").strip().lower()
    title = (window_title or "").strip()
    ax = (ax_text or "")

    if not proc:
        return _frame("unknown", "unknown", 0.0, "no_process_signal")

    if proc in WINDOWS_PROCESSES:
        conf = 0.95
        evidence = f"process={proc}"
        if ax and re.search(r"PS\s+[A-Z]:\\", ax):
            conf = 0.98
            evidence += ", prompt_match=PS_drive"
        return _frame("windows", "local", conf, evidence)

    if proc in LINUX_SSH_PROCESSES:
        m = _USER_AT_HOST_RE.search(title) or _USER_AT_HOST_RE.search(ax)
        if m:
            return _frame("linux", m.group(2), 0.92,
                          f"process={proc}, title_match={m.group(0)}")
        # Terminal app but no user@host signal — could be local PowerShell-in-terminal
        # or pre-login shell. Drop confidence.
        return _frame("linux", "unknown", 0.50,
                      f"process={proc}, no_user_at_host")

    if proc in REMOTE_DESKTOP_PROCESSES:
        host = "unknown"
        for rx in _RDP_TITLE_RES:
            m = rx.search(title)
            if m:
                host = m.group(1).strip()
                break
        # Remote process spec: os reported as 'remote' to flag "not local
        # execution"; recall filter treats this distinctly from windows/linux.
        return _frame("remote", host, 0.75 if host != "unknown" else 0.45,
                      f"process={proc}, title={title[:60]!r}")

    if proc in AMBIENT_PROCESSES:
        return _frame("windows", "local", 0.30,
                      f"ambient_app process={proc}")

    return _frame("unknown", "unknown", 0.0,
                  f"process={proc} not_classified")


def _frame(os_: str, host: str, confidence: float, evidence: str) -> dict:
    return {
        "os": os_,
        "host": host,
        "confidence": round(confidence, 2),
        "evidence": evidence,
    }
