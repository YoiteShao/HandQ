"""Browser-related per-user path resolution.

The browser tool stores its persistent Chromium user-data-dir at
``%USERPROFILE%\\HandQ\\browser_profile\\`` so cookies / login state /
extensions survive across HandQ sessions and across upgrades, in line
with the user-data classification in ARCHITECTURE.md §1.5
("Session 历史 / 用户配置 跨升级").

In the multi-session v2 model each flow gets its own user-data-dir under
``%USERPROFILE%\\HandQ\\browser_profile\\sessions\\<sid>\\`` so Chromium's
per-process user-data-dir lock never blocks one session waiting on
another. To keep SSO / login state across sessions despite this
isolation, ``browser_tool`` copies a small subset of files (cookies +
DOM storage) between the shared cookie dir (``shared/``) and the
session profile on launch / close. The legacy single-dir path is still
returned when no sid is passed — it backs the module-level fallback
holder (ctx-less callers) and test fixtures that pre-date the
multi-session split.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional, Tuple


def user_handq_root() -> str:
    """Return ``%USERPROFILE%\\HandQ`` (or ``~/HandQ`` when USERPROFILE is missing).

    Mirrors ``bridge_main._user_handq_root()`` so tools can resolve the same
    per-user root without importing the bridge entrypoint module.
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "HandQ")


_SID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_sid(sid: str) -> str:
    """Sanitise a session_id for use as a filesystem path component.

    Renderer-generated sids are RFC4122 UUIDs (already safe); scheduler
    sids are ``sched-<12hex>`` (already safe). This is purely belt-and-
    braces against future sid formats that could include slashes or
    other path-meaningful characters.
    """
    cleaned = _SID_SAFE_RE.sub("_", sid).strip("._")
    return cleaned or "default"


def user_browser_profile_dir(sid: Optional[str] = None) -> str:
    """Return the Chromium user-data-dir for *sid* (or the legacy single
    profile when *sid* is None).

    ``sid=None`` returns ``<root>/browser_profile`` — backs the module-
    level fallback holder used by ctx-less callers (legacy
    ``flush_browser_pool`` shim, test fixtures). The directory is created
    on first access.

    ``sid="abc"`` returns ``<root>/browser_profile/sessions/abc/``. Each
    flow gets a fresh, independent profile so concurrent sessions can
    each hold their own Chromium without contending on the user-data-dir
    lock. Login state is carried across sessions by ``browser_tool``
    copying :data:`SHARED_COOKIE_SUBPATHS` between the session dir and
    :func:`user_browser_shared_cookies_dir` on launch / close.
    """
    base = os.path.join(user_handq_root(), "browser_profile")
    if sid is None:
        os.makedirs(base, exist_ok=True)
        return base
    path = os.path.join(base, "sessions", _safe_sid(sid))
    os.makedirs(path, exist_ok=True)
    return path


def user_browser_shared_cookies_dir() -> str:
    """Return the cross-session cookie/login-state carry-over directory.

    Path: ``<root>/browser_profile/shared/``.

    The browser tool hydrates a fresh per-session Chromium user-data-dir
    from this directory on launch (so a fresh session inherits the last
    logged-in state) and writes back into it on close (so the next
    session picks up whatever was refreshed during this run). Only
    :data:`SHARED_COOKIE_SUBPATHS` are copied — window geometry /
    active tab / saved passwords are deliberately excluded (see the
    ``SHARED_COOKIE_SUBPATHS`` docstring).

    Concurrency: two sessions closing simultaneously produce a
    last-writer-wins race on the copy-back step. The common single-
    session case is safe; the concurrent case degrades gracefully by
    keeping whichever session finished its close() last.
    """
    path = os.path.join(user_handq_root(), "browser_profile", "shared")
    os.makedirs(path, exist_ok=True)
    return path


# Files / directories under a Chromium user-data-dir that carry DOM
# storage (localStorage / sessionStorage / IndexedDB) — best-effort
# supplemental state for web apps that stash auth artefacts outside HTTP
# cookies (e.g. Azure AD MSAL keeps tokens in IndexedDB).
#
# HTTP cookies themselves are NOT synced via file copy here. Chromium
# encrypts the Cookies SQLite with a per-profile ``os_crypt`` master key
# in ``Local State`` (Windows M116+ App-Bound Encryption), and empirically
# Chromium **rewrites Local State on launch**, discarding any master key
# we copied in — so file-based cookie sync silently loses every cookie
# even when both files are present. HTTP cookies are instead persisted
# via Playwright's ``context.storage_state()`` API — see
# :func:`user_browser_shared_storage_state_path` and the launch/close
# hooks in ``browser_tool.py`` that read/write it.
#
# The ``is_dir`` flag drives which copy helper to use: file vs. directory
# tree. Missing entries are silently skipped on both hydrate and persist.
#
# Deliberately excluded:
#   * ``Local State`` / ``Default/(Network/)Cookies`` — see rationale above.
#   * ``Default/Preferences`` — window geometry, active tab, extension
#     state; hydrating breaks the fresh session's browser layout.
#   * ``Default/Login Data`` — Chrome's saved-password store. Copying
#     across sessions is a credential-broadening surface we don't want.
#   * ``Default/Safe Browsing Network/Safe Browsing Cookies`` — Google's
#     internal safe-browsing telemetry, not user session state.
#   * ``Default/History`` / ``Default/Top Sites`` — browsing history is
#     not part of the "SSO carries over" contract.
SHARED_COOKIE_SUBPATHS: Tuple[Tuple[str, bool], ...] = (
    ("Default/Local Storage",   True),   # localStorage — Azure AD / Atlassian SSO
    ("Default/Session Storage", True),   # session-scoped storage
    ("Default/IndexedDB",       True),   # web-app auth caches (MSAL / OIDC)
)


def user_browser_shared_storage_state_path() -> str:
    """Return the path to the cross-session Playwright ``storage_state`` JSON.

    ``<shared>/storage_state.json``. Contains cookies (and, optionally,
    origins/localStorage) serialised via Playwright's ``context.storage_state()``.
    Written by ``BrowserSessionHolder.close`` before the persistent context is
    torn down; loaded by ``_action_launch_browser`` after Chromium comes up and
    injected via ``context.add_cookies()``. This path deliberately sidesteps
    Chromium's Cookies SQLite encryption entirely — Playwright works in
    plaintext at the CDP layer and re-encrypts on the target profile.

    First-launch case (file missing): callers no-op and the user will complete
    the initial login normally; the file is created on the first successful
    close.
    """
    return os.path.join(
        user_handq_root(), "browser_profile", "shared", "storage_state.json"
    )


def is_windows() -> bool:
    """The browser tool is Windows-only for now (Edge channel + tested
    window-positioning). Used by ToolRegistry to gate registration."""
    return sys.platform == "win32"
