"""Browser-related per-user path resolution.

The browser tool stores its persistent Chromium user-data-dir at
``%USERPROFILE%\\HandQ\\browser_profile\\`` so cookies / login state /
extensions survive across HandQ sessions and across upgrades, in line
with the user-data classification in ARCHITECTURE.md §1.5
("Session 历史 / 用户配置 跨升级").

This is intentionally NOT under the per-session ``storage_directory``
(which is allocated per request and rotates with each new session).
The browser profile is process-wide and durable.
"""
from __future__ import annotations

import os
import sys


def user_handq_root() -> str:
    """Return ``%USERPROFILE%\\HandQ`` (or ``~/HandQ`` when USERPROFILE is missing).

    Mirrors ``bridge_main._user_handq_root()`` so tools can resolve the same
    per-user root without importing the bridge entrypoint module.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "HandQ")


def user_browser_profile_dir() -> str:
    """Return the persistent Chromium user-data-dir.

    The directory is created on first access (``launch_persistent_context``
    requires it to exist). Per-session dirs (the agent's
    ``storage_directory``) are intentionally NOT used — that would force
    the user to re-log-in every session.
    """
    path = os.path.join(user_handq_root(), "browser_profile")
    os.makedirs(path, exist_ok=True)
    return path


def is_windows() -> bool:
    """The browser tool is Windows-only for now (Edge channel + tested
    window-positioning). Used by ToolRegistry to gate registration."""
    return sys.platform == "win32"
