# -*- coding: utf-8 -*-
"""Browser Tool — Playwright-based browser automation for Windows.

Architecture
============
Single process-wide browser session (Playwright launches one Chromium-based
browser holding a persistent user-data-dir at
``%USERPROFILE%\\HandQ\\browser_profile\\``).  The same session is reused
across steps and across agents — Chromium's user-data-dir can only be
locked by one process at a time, so a singleton is the correct model.

Stealth model
-------------
Stealth is **off-screen positioning**, not headless.  The browser launches
with ``--window-position=-32000,-32000`` and ``headless=False`` so the
fingerprint matches a real user (some sites detect headless via subtle JS
features) but the window never appears on the user's screen.

``request_user_login`` (Phase 3) will move the window on-screen via CDP
``Browser.setWindowBounds``, let the user log in manually, then move it
back.  Phase 1 only ships the off-screen path.

Browser source priority
-----------------------
1. Microsoft Edge (``channel="msedge"``) — preinstalled on Windows 11
2. Google Chrome (``channel="chrome"``) — if installed
3. Bundled Chromium (no channel) — requires ``playwright install chromium``

First success wins; each fallback is logged.

Concurrency
-----------
``is_concurrency_safe = False``.  An asyncio lock serialises every action
on the session — DOM mutations and navigation events are too entangled
to safely parallelise without per-tab fences.

Persistent profile
------------------
The user-data-dir lives at ``user_browser_profile_dir()`` (see
``browser_paths.py``) so cookies / login state survive across HandQ
sessions per ARCHITECTURE.md §1.5.

Phase 1 actions
---------------
``launch_browser`` (idempotent), ``navigate``, ``extract``, ``list_tabs``,
``close_tab``.  The remaining actions (click / type / wait_for /
screenshot / request_user_login / attach_browser) are added in later
phases.

Cleanup
-------
``flush_browser_pool()`` is called from the bridge's ``new_session``
sequence to close the browser cleanly so the next session can re-acquire
the user-data-dir without lock contention.  The function is idempotent
and best-effort — exceptions are swallowed and logged.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .base_tool import BaseTool, ToolResult
from ..infrastructure.browser_paths import user_browser_profile_dir
from ..infrastructure.logger import get_logger

# ── Optional Playwright dependency ────────────────────────────────────────────
# The agent must run even when playwright is not installed; the tool reports
# a clear actionable error in that case.

try:
    from playwright.async_api import (  # type: ignore[import-not-found]
        async_playwright,
        Browser,
        BrowserContext,
        Page,
        TimeoutError as _PlaywrightTimeoutError,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    async_playwright = None      # type: ignore[assignment]
    Browser = Any                # type: ignore[misc, assignment]
    BrowserContext = Any         # type: ignore[misc, assignment]
    Page = Any                   # type: ignore[misc, assignment]
    _PlaywrightTimeoutError = Exception  # type: ignore[assignment]


# Maximum characters returned by ``extract`` — mirrors read_tool's 100KB
# cap so the LLM context budget stays predictable.
_EXTRACT_MAX_CHARS = 100_000

# Per-item char cap for extract mode='list'. Whole-payload cap is still
# _EXTRACT_MAX_CHARS; this just keeps a single oversized element from
# eating the entire budget.
_EXTRACT_LIST_ITEM_HTML_CAP = 1_200
_EXTRACT_LIST_ITEM_TEXT_CAP = 400
_EXTRACT_LIST_DEFAULT_LIMIT = 20
_EXTRACT_LIST_MAX_LIMIT = 100

# Default window size used by launch_browser. Important: 1280x800 keeps
# us well above the common mobile breakpoint so sites render the desktop
# layout, which is what the agent expects.
_DEFAULT_WINDOW_SIZE = "1280,800"

# Off-screen position. -32000 is the Win32-documented "hidden" coordinate
# (used by Win32 SetWindowPos for hidden windows). Chromium honours it.
_OFFSCREEN_POSITION = "-32000,-32000"

# Default navigation timeout (ms) for a fresh page.load. Most pages fully
# settle within 10–15s; we cap at 30 to surface broken sites quickly.
_DEFAULT_NAV_TIMEOUT_MS = 30_000

# Default per-action timeout (ms) for click / type / wait_for. Playwright
# auto-waits on actionability; this caps the wait so the LLM gets a clear
# failure instead of stalling silently.
_DEFAULT_ACTION_TIMEOUT_MS = 15_000

# Probe timeout used inside extract when looking up an element via
# selector. 2s is enough for hydrated SPAs but cheap enough that the
# LLM can iterate selector hypotheses without burning a full 5–15s
# wait per miss (the original 5s was the dominant cost in selector-
# guessing loops on jconfirm-style modals).
_DEFAULT_EXTRACT_SELECTOR_TIMEOUT_MS = 2_000

# When the type tool encounters an input[type=password], it MUST refuse.
# Passwords go through request_user_login (Phase 3) — the agent never
# fills password fields. Enforced server-side here, not just in the
# system prompt.
_PASSWORD_REFUSAL = (
    "REFUSED: agent is forbidden from filling password fields. "
    "Use action='request_user_login' (when available) to let the user log in "
    "manually. Cookies will be persisted to the user-data-dir for future "
    "sessions."
)

# Browser screenshot scratch store. The actual class + retention logic
# lives in ``infrastructure.vision.ScreenshotStore`` so the same tier
# semantics can be reused by desktop_tool (Phase 2) and activity_monitor
# (Phase 3) — only the root directory differs per producer. We hold a
# lazy module-level instance because ConfigManager is not always ready
# at import time.
#
# Long-term keepers do NOT live here. If the agent wants to preserve a
# capture beyond the task, it writes to its session directory (working
# dir) instead — see ARCHITECTURE.md §1.6.
_browser_store_instance: Optional[Any] = None


def _browser_store():
    global _browser_store_instance
    if _browser_store_instance is None:
        from ..infrastructure.config_manager import ConfigManager
        from ..infrastructure.vision import ScreenshotStore
        try:
            cfg = ConfigManager().get_section("screenshots") or {}
        except Exception:
            cfg = {}
        _browser_store_instance = ScreenshotStore(
            root=os.path.join(user_browser_profile_dir(), "screenshots"),
            config_section=cfg,
        )
    return _browser_store_instance


def _default_screenshot_dir() -> str:
    """Backward-compat shim — returns the ``task`` subdir of the browser
    store. Most call sites should call ``_browser_store().subdir(cat)``
    directly with an explicit category."""
    return _browser_store().subdir("task")


# Recognised Playwright selector engine prefixes. We only validate CSS
# selectors; the ``text=`` / ``xpath=`` / ``role=`` engines have their
# own grammars that we do not police here.
_NON_CSS_SELECTOR_PREFIXES = (
    "text=", "text/", "xpath=", "//", "role=", "css=", "id=", "data-testid=",
)


def _validate_css_selector(selector: str) -> Optional[str]:
    """Cheap pre-flight check that catches the most common selector typos
    before the call reaches Playwright (which otherwise eats a full
    timeout / parse-error round trip).

    Today this only catches ``.<digit>`` / ``#<digit>`` — the footgun
    that bit the iter-34 attempt to use ``SA8797P.HGY.5.1.7.0`` as a
    selector. CSS identifiers cannot start with a digit, so any ``.5``
    or ``#5`` token outside an attribute-selector value is wrong.

    Attribute selector values (``[id="foo.5"]``) are stripped before the
    check so legitimate quoted values containing dots-and-digits are
    not flagged. Non-CSS engines (text=/xpath=/role=) skip the check
    entirely; they have their own grammars and Playwright fails fast
    on those anyway.
    """
    s = (selector or "").strip()
    if not s:
        return "selector is empty"
    for prefix in _NON_CSS_SELECTOR_PREFIXES:
        if s.startswith(prefix):
            return None
    # Strip the contents of every [..] so quoted attribute values cannot
    # produce false positives. We don't need to be a real CSS parser —
    # this is just heuristic validation.
    cleaned = re.sub(r"\[[^\]]*\]", "", s)
    if re.search(r"[.#]\d", cleaned):
        return (
            f"selector {selector!r} starts a class/id token with a digit "
            "(CSS treats '.5' as a numeric literal, not a class). If you "
            "meant to match a literal value containing dots or digits, "
            "use an attribute selector — e.g. [id='SA8797P.HGY.5.1.7.0'] "
            "or [class~='5']. To target by visible text, use Playwright's "
            "text= engine: text='SA8797P.HGY.5.1.7.0'."
        )
    return None


# ── Module-level singleton state ─────────────────────────────────────────────
# A single BrowserSession is shared across all FlowController sessions in
# the current process. user-data-dir locking forces this — see header.

@dataclass
class BrowserSession:
    """In-process browser handle. Created on first launch_browser call."""
    playwright: Any                       # the async_playwright() context
    context: Any                          # BrowserContext (persistent or attached)
    mode: str                             # "launch" | "attach"
    channel: str                          # "msedge" | "chrome" | "chromium" | "attached"
    profile_dir: str                      # absolute path (empty for attach)
    browser: Any = None                   # attach mode: connected Browser; launch: None
    cdp_url: str = ""                     # attach mode only
    tabs: Dict[str, Any] = field(default_factory=dict)  # tab_id → Page
    last_used: float = 0.0
    _next_tab_seq: int = 1

    def mint_tab_id(self) -> str:
        tid = f"t{self._next_tab_seq}"
        self._next_tab_seq += 1
        return tid

    def first_tab_id(self) -> Optional[str]:
        """Return the first tab_id (insertion order) or None when empty."""
        for tid in self.tabs:
            return tid
        return None


_session: Optional[BrowserSession] = None
_session_lock = asyncio.Lock()


# ── Public helpers for cross-tool reuse ──────────────────────────────────────
# web_search_tool reuses the live Playwright session to do same-origin
# REST calls (cookies + SSO already in place via the user's profile). These
# helpers are the public contract; downstream tools must NOT import _session
# / _session_lock directly so the singleton can be refactored later without
# breaking callers.

def is_browser_available() -> bool:
    """True iff Playwright is importable. Used by setup providers to fail
    fast with an actionable install hint instead of a deep stacktrace."""
    return _PLAYWRIGHT_AVAILABLE


@asynccontextmanager
async def acquire_browser_lock() -> AsyncIterator["BrowserSession"]:
    """Yield the live ``BrowserSession`` while holding the global session lock.

    Raises ``RuntimeError`` if no session is launched — caller must invoke
    ``browser.launch_browser`` first. Lock is global to the process; all
    actions across agents and steps queue on it.
    """
    async with _session_lock:
        if _session is None:
            raise RuntimeError(
                "browser session is not launched. Call "
                "browser.launch_browser before reusing the browser session."
            )
        yield _session


# Cap on the body returned by evaluate_fetch — mirrors _EXTRACT_MAX_CHARS so
# the LLM context budget stays predictable across browser primitives.
_FETCH_BODY_MAX_CHARS = 100_000


async def evaluate_fetch(
    session: "BrowserSession",
    *,
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    same_origin: bool = True,
    timeout_ms: int = 30_000,
    max_body_chars: int = _FETCH_BODY_MAX_CHARS,
) -> Dict[str, Any]:
    """Run ``fetch()`` from a page in the live browser, returning the response.

    Same-origin mode (default): a tab is navigated to the target's origin
    first if no open tab is already there, so the browser sends cookies
    (SameSite=Strict / Lax SSO cookies require the page origin to match
    the request origin). Set ``same_origin=False`` to fire from whatever
    tab happens to be in front; cross-origin fetches require the server
    to expose CORS headers and are rare for internal APIs.

    Caller MUST already hold ``_session_lock`` (use ``acquire_browser_lock``).

    Returns ``{status, headers, body, url, truncated}``. On JS-side error
    (network failure, abort, malformed URL): ``{status: 0, error: ..., ...}``.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"evaluate_fetch: url must include scheme and host, got {url!r}"
        )
    target_origin = f"{parsed.scheme}://{parsed.netloc}"

    tab_id = session.first_tab_id()
    if tab_id is None:
        page = await session.context.new_page()
        tab_id = session.mint_tab_id()
        session.tabs[tab_id] = page
    else:
        page = session.tabs[tab_id]

    if same_origin:
        current_url = page.url or ""
        cur_parsed = urlparse(current_url)
        cur_origin = (
            f"{cur_parsed.scheme}://{cur_parsed.netloc}"
            if cur_parsed.scheme and cur_parsed.netloc
            else ""
        )
        if cur_origin != target_origin:
            try:
                await page.goto(
                    target_origin,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            except Exception:
                # Origin landing page may 403 or redirect to SSO — the
                # follow-up fetch may still succeed if the target API is
                # public on that host. We let the caller surface the
                # eventual fetch status.
                pass

    js = """
        async ({url, method, headers, body, timeoutMs}) => {
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), timeoutMs);
            try {
                const init = {method, credentials: 'include', signal: ctrl.signal};
                if (headers && Object.keys(headers).length) init.headers = headers;
                if (body !== null && body !== undefined) init.body = body;
                const r = await fetch(url, init);
                const text = await r.text();
                const hdrs = {};
                r.headers.forEach((v, k) => { hdrs[k] = v; });
                return {status: r.status, statusText: r.statusText,
                        url: r.url, headers: hdrs, body: text};
            } catch (err) {
                return {error: (err && (err.message || String(err))) || 'fetch failed',
                        name: (err && err.name) || 'Error'};
            } finally {
                clearTimeout(timer);
            }
        }
    """
    raw = await page.evaluate(
        js,
        {
            "url": url,
            "method": method.upper(),
            "headers": dict(headers) if headers else {},
            "body": body,
            "timeoutMs": int(timeout_ms),
        },
    )
    if "error" in raw:
        return {
            "status": 0,
            "headers": {},
            "body": "",
            "url": url,
            "truncated": False,
            "error": raw["error"],
            "error_name": raw.get("name", "Error"),
        }

    text = raw.get("body") or ""
    truncated = False
    if len(text) > max_body_chars:
        text = text[:max_body_chars]
        truncated = True
    return {
        "status": int(raw.get("status") or 0),
        "headers": raw.get("headers") or {},
        "body": text,
        "url": raw.get("url") or url,
        "truncated": truncated,
    }


# ── Phase 5b: attach auto-setup helpers ──────────────────────────────────────
# These keep the bat-script path opaque to end users. attach_browser walks:
#   1. probe CDP port → reachable: connect transparently
#   2. unreachable + Chrome not running → spawn bat, wait, retry probe
#   3. unreachable + Chrome IS running → ask user via risk_confirmation
#                                         (Approve = kill+restart Chrome)
# The user never has to know the bat exists unless step 3 triggers, and even
# then they only see a single Approve/Reject choice.

def _parse_cdp_url(url: str) -> Tuple[str, int]:
    """Extract (host, port) from a CDP URL like ``http://localhost:9222``.

    Falls back to ("localhost", 9222) on parse errors so the probe never
    crashes the auto-setup chain.
    """
    try:
        m = re.match(r"^https?://([^:/]+)(?::(\d+))?", url, re.IGNORECASE)
        if m:
            host = m.group(1) or "localhost"
            port = int(m.group(2)) if m.group(2) else 9222
            return host, port
    except Exception:
        pass
    return "localhost", 9222


async def _probe_cdp_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP connection to *host:port* succeeds within *timeout*.

    Uses asyncio.open_connection so we don't block the event loop. Closes
    the socket immediately — we only need to know the port is listening.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    except Exception:
        return False


def _is_chrome_running() -> bool:
    """Return True if chrome.exe or msedge.exe is in the Windows process list.

    Uses ``tasklist`` which is bundled with Windows. /FI filters by image
    name; /NH suppresses the header so any non-empty output means a match.
    Returns False on any error — this gates the "ask user to restart"
    branch, and erring on the side of "not running" lets the auto-spawn
    path proceed (which is harmless if Chrome wasn't actually running).
    """
    if sys.platform != "win32":
        return False
    for image in ("chrome.exe", "msedge.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if image.lower() in (out.stdout or "").lower():
                return True
        except Exception:
            continue
    return False


def _kill_chrome_processes() -> None:
    """Best-effort terminate chrome.exe / msedge.exe — used after user approves
    the "restart Chrome with debug port" path. /F forces unconditional kill.
    Errors are swallowed: if the user has already closed Chrome between the
    check and now, taskkill will fail and we'll just proceed to spawn the bat.
    """
    if sys.platform != "win32":
        return
    for image in ("chrome.exe", "msedge.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", image],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass


def _find_bat_script() -> Optional[str]:
    """Locate ``start_chrome_with_debug.bat`` for both dev and packaged layouts.

    Search order:
      1. ``HANDQ_INSTALL_DIR\\scripts\\``      (env override)
      2. ``<repo>/scripts/``                   (dev: walks up from this file)
      3. ``<sys.executable parent>/scripts/``  (Nuitka packaged layout)

    Returns the absolute path on first hit, or None if not found anywhere.
    The caller surfaces the missing-script case as an actionable error.
    """
    name = "start_chrome_with_debug.bat"
    candidates: List[Path] = []

    env_root = os.environ.get("HANDQ_INSTALL_DIR")
    if env_root:
        candidates.append(Path(env_root) / "scripts" / name)

    # Dev: this file is at <repo>/src/tools/browser_tool.py → ../../scripts/
    candidates.append(Path(__file__).resolve().parent.parent.parent / "scripts" / name)

    # Packaged: next to handq-bridge.exe
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        candidates.append(Path(sys.executable).resolve().parent / "scripts" / name)

    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except Exception:
            continue
    return None


def _spawn_bat_detached(bat_path: str) -> None:
    """Spawn the bat script as a detached process so it survives our return.

    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP keeps the new Chrome from
    being killed when bridge_main exits. Output is discarded — we only
    care that Chrome ends up listening on the debug port, which the
    caller verifies with a follow-up _probe_cdp_port loop.
    """
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


async def flush_browser_pool() -> int:
    """Close the active browser session if any. Returns the number closed (0 or 1).

    Mode-aware:
      * launch mode → ``context.close()`` shuts down our persistent
        Chromium subprocess and releases the user-data-dir lock so the
        next session can re-acquire it.
      * attach mode → ``browser.close()`` only DISCONNECTS the CDP
        session; the user's Chrome process keeps running and their tabs
        are unaffected. This is critical: attach mode must not destroy
        the user's session.

    Called from the bridge's ``new_session`` sequence. Best-effort: every
    step is wrapped in try/except so a partially-broken session never
    blocks shutdown. Async because Playwright's close / stop are
    coroutines — the caller must ``await``.
    """
    global _session
    sess = _session
    _session = None
    if sess is None:
        return 0

    logger = get_logger()
    if sess.mode == "attach":
        # Disconnect CDP — does NOT terminate the user's Chrome.
        try:
            await sess.browser.close()
        except Exception as exc:
            logger.warning(f"browser flush (attach): browser.close failed: {exc}",
                           component="BrowserTool")
    else:
        # Launch mode: close our owned context (kills our Chromium subprocess).
        try:
            await sess.context.close()
        except Exception as exc:
            logger.warning(f"browser flush (launch): context.close failed: {exc}",
                           component="BrowserTool")
    try:
        await sess.playwright.stop()
    except Exception as exc:
        logger.warning(f"browser flush: playwright.stop failed: {exc}",
                       component="BrowserTool")
    logger.info(f"browser flush: {sess.mode} session closed",
                component="BrowserTool")
    # Screenshot housekeeping at the session boundary.
    #   ephemeral/  : nuked unconditionally — vision_query work files
    #                 should never cross sessions.
    #   task/       : aged sweep using screenshots.task.retain_after_task_days
    #                 (defaults to 1). Keeps a short replay window.
    try:
        swept = _browser_store().session_close_sweep()
        if swept.get("ephemeral") or swept.get("task"):
            logger.info(
                f"screenshots flush: purged "
                f"{swept.get('ephemeral', 0)} ephemeral, "
                f"{swept.get('task', 0)} aged-task file(s)",
                component="BrowserTool",
            )
    except Exception as exc:
        logger.warning(f"screenshots flush failed: {exc}",
                       component="BrowserTool")
    return 1


# ── BrowserTool ──────────────────────────────────────────────────────────────


class BrowserTool(BaseTool):
    """Single tool exposing a small set of Playwright-driven browser actions.

    Actions are dispatched by the ``action`` parameter. Phase 1 supports:
      * ``launch_browser`` — start (or reuse) the persistent profile
      * ``list_tabs``     — enumerate open tabs
      * ``navigate``      — go to a URL
      * ``extract``       — read text/html/attributes from the page
      * ``close_tab``     — close one tab

    All actions other than launch_browser require a session to exist
    (the LLM is expected to call launch_browser first). When no session
    exists, the action returns an actionable error.

    Concurrency: actions on the same session are serialised by an
    asyncio.Lock so the LLM never has two parallel DOM operations
    fighting each other. This is enforced even when the model marks
    multiple browser calls as concurrent_safe.
    """

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "launch_browser",
                    "attach_browser",
                    "list_tabs",
                    "navigate",
                    "extract",
                    "snapshot",
                    "click",
                    "type",
                    "wait_for",
                    "screenshot",
                    "vision_query",
                    "video_context",
                    "fetch_json",
                    "request_user_login",
                    "new_tab",
                    "close_tab",
                ],
                "description": (
                    "Browser action to perform. attach_browser is gated by "
                    "browser.attach_enabled in handq_config.yaml — the action "
                    "fails with an actionable error when the switch is off. "
                    "Prefer 'snapshot' over repeated 'extract' probes when you "
                    "need to discover what is interactable on a page. Use "
                    "'fetch_json' to call a REST API as the logged-in user "
                    "(cookies + SSO are reused) without leaving the DOM in "
                    "an unexpected state."
                ),
            },
            "tab_id": {
                "type": "string",
                "description": (
                    "[navigate / extract / click / type / wait_for / screenshot / "
                    "close_tab] Tab identifier returned by list_tabs or implied "
                    "(first tab when omitted)."
                ),
            },
            "url": {
                "type": "string",
                "description": "[navigate] Absolute URL to load (must include scheme).",
            },
            "selector": {
                "type": "string",
                "description": (
                    "[extract / click / type / wait_for / screenshot] Element selector. "
                    "Supports CSS selectors, Playwright text='Login', "
                    "role='button[name=Submit]', xpath=// syntax. For extract, "
                    "omit to read the whole page. NOTE: CSS rejects '.<digit>' "
                    "tokens (e.g. SA8797P.HGY.5.1.7.0 fails because '.5' is a "
                    "numeric literal) — use [id='...'] or text='...' for "
                    "values containing dots or digits."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["text", "html", "attr", "list"],
                "description": (
                    "[extract] Output format. text (default): visible text, "
                    "html: outerHTML of FIRST match, attr: attributes of first "
                    "match. list: outerHTML + text of EVERY match (capped by "
                    "'limit') — use this when enumerating candidates instead of "
                    "guessing successively narrower selectors."
                ),
            },
            "attribute": {
                "type": "string",
                "description": (
                    "[extract mode=attr] Single attribute name; if omitted "
                    "all attributes are returned as a JSON object."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "[extract mode=list] Max elements to return. Default "
                    f"{_EXTRACT_LIST_DEFAULT_LIMIT}, hard cap "
                    f"{_EXTRACT_LIST_MAX_LIMIT}. Each item is truncated "
                    f"(html≤{_EXTRACT_LIST_ITEM_HTML_CAP}, "
                    f"text≤{_EXTRACT_LIST_ITEM_TEXT_CAP}) so the total "
                    "payload stays within the LLM context budget."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "[type] Text to fill into the matched input. The previous "
                    "content is cleared. Refused on input[type=password] — use "
                    "request_user_login instead for credential entry."
                ),
            },
            "press_enter": {
                "type": "boolean",
                "description": (
                    "[type] If true, press Enter after typing (submits forms / "
                    "triggers search). Default: false."
                ),
            },
            "nth": {
                "type": "integer",
                "description": (
                    "[click / type] 0-based index when the selector matches "
                    "multiple elements. Default: 0 (first match)."
                ),
            },
            "url_pattern": {
                "type": "string",
                "description": (
                    "[wait_for] Regex pattern (Python re syntax) the page URL "
                    "must match. Mutually exclusive with selector."
                ),
            },
            "state": {
                "type": "string",
                "enum": ["visible", "hidden", "attached", "detached"],
                "description": (
                    "[wait_for] Element state to wait for. Default: visible. "
                    "Only meaningful with selector."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "[screenshot] Output file path. Absolute paths used as-is "
                    "(use this to write a long-term keeper into the session "
                    "working directory). Relative paths are resolved under "
                    "the auto-cleaned task tier — files there are removed "
                    "shortly after the task completes."
                ),
            },
            "full_page": {
                "type": "boolean",
                "description": (
                    "[screenshot] If true, capture the full scrollable page; "
                    "otherwise only the viewport. Default: false."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "[request_user_login] Short human-readable explanation of "
                    "why login is required. Shown in the approval modal so "
                    "the user understands what's happening."
                ),
            },
            "success_url_pattern": {
                "type": "string",
                "description": (
                    "[request_user_login] Optional regex (Python re syntax) the "
                    "page URL is expected to match after a successful login. "
                    "When the user clicks Approve, the tool reports whether "
                    "the URL matched — useful for the agent to confirm login "
                    "actually completed before proceeding."
                ),
            },
            "browser_credentials_file": {
                "type": "string",
                "description": (
                    "[attach_browser] Optional local YAML/JSON file with CDP "
                    "connection details: {cdp_port: 9222, cdp_url: ''}. "
                    "When omitted, defaults from handq_config.yaml's browser "
                    "section are used (typically localhost:9222). The file path "
                    "is the only thing passed through the LLM — the actual "
                    "port / URL is read from disk so prompts stay clean."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "[new_tab] Open the new tab in the background so the user's "
                    "current focus is preserved. Default: true (recommended in "
                    "attach mode). Setting false steals focus and is rarely "
                    "what you want."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Timeout in milliseconds. navigate default: "
                    f"{_DEFAULT_NAV_TIMEOUT_MS}. click / type / wait_for / "
                    f"screenshot default: {_DEFAULT_ACTION_TIMEOUT_MS}."
                ),
            },
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "description": (
                    "[navigate] Lifecycle event to wait for. "
                    "Default: load (full page)."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "[vision_query] Natural-language instruction telling the "
                    "vision model what to look at or extract. Be concrete: "
                    "'What does the chart show?' / 'Where is the Sign In "
                    "button? Reply with pixel coordinates.' / 'Is there a "
                    "captcha on this page?' Pair with output_schema when you "
                    "need a JSON answer instead of free text."
                ),
            },
            "output_schema": {
                "type": "object",
                "description": (
                    "[vision_query] Optional JSON Schema the vision model's "
                    "reply must conform to. When set, the gateway is asked "
                    "for a JSON object; the parsed JSON is returned in "
                    "parsed_json alongside the raw answer text. Useful for "
                    "coordinate or yes/no queries — leave omitted for prose."
                ),
            },
            "max_image_dim": {
                "type": "integer",
                "description": (
                    "[vision_query] Long-edge resize cap before sending to "
                    "the model. Default from config (vision.max_image_dim, "
                    "typically 1024). Lower = faster but loses small text; "
                    "higher = more detail but more tokens."
                ),
            },
            "max_cues": {
                "type": "integer",
                "description": (
                    "[video_context] Maximum subtitle / caption cues to "
                    "return. Default 500 — fits a ~30-minute video. Set "
                    "higher for long lectures, lower if you only need the "
                    "intro. Cues beyond the limit are dropped and "
                    "captions_truncated=true is reported."
                ),
            },
            "seek_to_s": {
                "type": "number",
                "description": (
                    "[video_context] Optional: jump the video to this "
                    "second BEFORE reading state. Use for 'what's at "
                    "minute 2:30?' — pair with pause=true and the "
                    "subsequent screenshot will land on that frame."
                ),
            },
            "pause": {
                "type": "boolean",
                "description": (
                    "[video_context] If true, pause playback before "
                    "reading state. Default false. Useful when you want "
                    "to follow up with screenshot+vision_query on a "
                    "specific frame without it advancing."
                ),
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                "description": (
                    "[fetch_json] HTTP method. Default GET. Most internal "
                    "search APIs are GET; POST is occasionally needed for "
                    "Atlassian's CQL endpoint when the query is too long "
                    "for a URL parameter."
                ),
            },
            "headers": {
                "type": "object",
                "description": (
                    "[fetch_json] Optional request headers as a flat "
                    "{name: value} object. Common useful headers: "
                    "{'Accept': 'application/json'} for REST endpoints, "
                    "{'X-Atlassian-Token': 'no-check'} for Jira DC POSTs. "
                    "Cookies are NOT a header here — they are added "
                    "automatically because the request runs in the page "
                    "context with credentials: 'include'."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "[fetch_json] Optional request body string. Caller is "
                    "responsible for serialisation (e.g. json.dumps) and "
                    "for setting a matching Content-Type header. Ignored "
                    "for GET / HEAD."
                ),
            },
            "same_origin": {
                "type": "boolean",
                "description": (
                    "[fetch_json] When true (default), the tool ensures "
                    "an open tab is on the URL's origin before firing the "
                    "fetch — this is required for SSO cookies to be sent "
                    "(SameSite=Strict / Lax). Set false to fire from "
                    "whatever tab is in front; only useful when the API "
                    "exposes CORS for cross-origin reads, which is rare "
                    "on internal Qualcomm endpoints."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__("browser")
        self.logger = get_logger()

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        start = time.time()
        params: Dict[str, Any] = {"action": action, **kwargs}

        if not _PLAYWRIGHT_AVAILABLE:
            return self._error(
                params, start,
                "playwright is not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install msedge   # or: chromium",
            )

        if not action:
            return self._error(params, start, "browser tool requires 'action'.")

        dispatch: Dict[str, Any] = {
            "launch_browser":     self._action_launch_browser,
            "attach_browser":     self._action_attach_browser,
            "list_tabs":          self._action_list_tabs,
            "navigate":           self._action_navigate,
            "extract":            self._action_extract,
            "snapshot":           self._action_snapshot,
            "click":              self._action_click,
            "type":               self._action_type,
            "wait_for":           self._action_wait_for,
            "screenshot":         self._action_screenshot,
            "vision_query":       self._action_vision_query,
            "video_context":      self._action_video_context,
            "fetch_json":         self._action_fetch_json,
            "request_user_login": self._action_request_user_login,
            "new_tab":            self._action_new_tab,
            "close_tab":          self._action_close_tab,
        }
        handler = dispatch.get(action)
        if handler is None:
            return self._error(
                params, start,
                f"Unknown browser action: {action!r}. "
                f"Valid: {', '.join(dispatch)}",
            )

        # Single-action serialisation. The lock is global to the process —
        # actions across agents and steps all queue here. Operations are
        # short (sub-second to a few seconds), so contention is acceptable
        # in exchange for safety.
        async with _session_lock:
            try:
                return await handler(params, start, **kwargs)
            except Exception as exc:
                self.logger.error(
                    f"browser action {action!r} raised: {exc}",
                    component="BrowserTool", exc_info=True,
                )
                return self._error(
                    params, start,
                    f"browser action {action!r} failed: {exc}",
                )

    # ── launch_browser ────────────────────────────────────────────────────────

    async def _action_launch_browser(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Idempotent: returns the existing session if one is alive."""
        global _session

        if _session is not None:
            # Verify the session is still healthy. context.pages will raise
            # if the underlying browser process has exited.
            try:
                _ = _session.context.pages
                _session.last_used = time.time()
                return ToolResult(
                    success=True,
                    output={
                        "reused":      True,
                        "channel":     _session.channel,
                        "mode":        _session.mode,
                        "profile_dir": _session.profile_dir,
                        "tabs":        list(_session.tabs.keys()),
                    },
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=time.time() - start,
                )
            except Exception:
                # Stale session — drop and relaunch.
                self.logger.warning(
                    "browser session stale on launch_browser; relaunching",
                    component="BrowserTool",
                )
                _session = None

        profile_dir = user_browser_profile_dir()
        pw = await async_playwright().start()

        # Channel priority: msedge → chrome → chromium. Each failure logs
        # and moves on; full failure surfaces an actionable error.
        last_error: Optional[Exception] = None
        for channel in ("msedge", "chrome", None):
            try:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    channel=channel,
                    headless=False,
                    args=[
                        f"--window-position={_OFFSCREEN_POSITION}",
                        f"--window-size={_DEFAULT_WINDOW_SIZE}",
                        # Reduces automation detection without breaking sites.
                        "--disable-blink-features=AutomationControlled",
                        # Suppress the first-run "what's new" tab.
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    viewport={"width": 1280, "height": 800},
                    accept_downloads=False,
                )
                channel_label = channel or "chromium"
                self.logger.info(
                    f"browser launched: channel={channel_label} profile={profile_dir}",
                    component="BrowserTool",
                )
                break
            except Exception as exc:
                last_error = exc
                self.logger.warning(
                    f"browser launch via channel={channel!r} failed: {exc}",
                    component="BrowserTool",
                )
                continue
        else:
            # All channels exhausted.
            try:
                await pw.stop()
            except Exception:
                pass
            return self._error(
                params, start,
                "Could not launch any browser. Tried msedge, chrome, chromium.\n"
                f"Last error: {last_error}\n"
                "Install Microsoft Edge (default on Windows 11) or run:\n"
                "  playwright install chromium",
            )

        # Persistent contexts may already have an "about:blank" page from
        # the profile's session restore, or none. Ensure at least one tab
        # exists so the agent has something to navigate.
        if not context.pages:
            page = await context.new_page()
        else:
            page = context.pages[0]

        sess = BrowserSession(
            playwright=pw,
            context=context,
            mode="launch",
            channel=channel_label,
            profile_dir=profile_dir,
            last_used=time.time(),
        )
        tab_id = sess.mint_tab_id()
        sess.tabs[tab_id] = page

        _session = sess

        return ToolResult(
            success=True,
            output={
                "reused":      False,
                "channel":     channel_label,
                "mode":        "launch",
                "profile_dir": profile_dir,
                "tabs":        [tab_id],
                "first_tab":   tab_id,
                "note": (
                    "Browser launched off-screen. Use action='navigate' to load "
                    "a URL on the first tab; the tab_id parameter is optional "
                    "(defaults to the first open tab)."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── attach_browser ────────────────────────────────────────────────────────

    async def _action_attach_browser(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Connect to the user's running Chrome / Edge over CDP.

        Pre-conditions:
          * ``browser.attach_enabled: true`` in handq_config.yaml.
          * The user has started Chrome / Edge with
            ``--remote-debugging-port=9222`` (see scripts/start_chrome_with_debug.bat).
          * No active session — call ``flush_browser_pool`` from the bridge
            or wait for new_session to free the slot.

        Effects:
          * Opens a Playwright Browser via ``connect_over_cdp``.
          * Registers all currently-open user tabs with mint_tab_id() so
            list_tabs / navigate / extract can target them.
          * Sets ``mode='attach'`` — flush_browser_pool then DISCONNECTS
            instead of closing Chrome.

        New tabs created via ``action='new_tab'`` are opened in the
        background by default so the user's focus is not stolen.
        """
        global _session

        # Read attach_enabled from config. Best-effort — when config can't
        # be read we conservatively REFUSE rather than silently allow.
        try:
            from ..infrastructure.config_manager import ConfigManager
            cm = ConfigManager()
            browser_cfg = cm.get_section("browser") or {}
        except Exception as exc:
            return self._error(
                params, start,
                f"attach_browser: cannot read config to verify attach_enabled: {exc}",
            )
        if not bool(browser_cfg.get("attach_enabled", False)):
            return self._error(
                params, start,
                "attach_browser is disabled. Set browser.attach_enabled: true in "
                "handq_config.yaml AND start Chrome with "
                "--remote-debugging-port=9222 before retrying.",
            )

        if _session is not None:
            return self._error(
                params, start,
                f"attach_browser: a {_session.mode!r} session is already active. "
                "Start a new HandQ session (which flushes the pool) and retry.",
            )

        # Resolve CDP URL. Priority: credentials_file > config.cdp_url > config.cdp_port.
        creds_file: Optional[str] = kwargs.get("browser_credentials_file")
        cdp_url, source = self._resolve_cdp_url(creds_file, browser_cfg)
        if not cdp_url:
            return self._error(
                params, start,
                f"attach_browser: could not resolve CDP URL from {source}. "
                "Provide browser_credentials_file with cdp_port / cdp_url, or "
                "set browser.cdp_port in handq_config.yaml.",
            )

        # ── Phase 5b: auto-setup ──────────────────────────────────────────────
        # Make the debug port reachable transparently. The user does NOT need
        # to know about start_chrome_with_debug.bat — we run it for them.
        # See _ensure_cdp_reachable for the full decision tree.
        setup_err = await self._ensure_cdp_reachable(cdp_url)
        if setup_err is not None:
            return self._error(params, start, setup_err)

        try:
            pw = await async_playwright().start()
        except Exception as exc:
            return self._error(params, start, f"attach_browser: playwright start failed: {exc}")

        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            try:
                await pw.stop()
            except Exception:
                pass
            return self._error(
                params, start,
                f"attach_browser: could not connect to {cdp_url}. "
                f"Verify Chrome / Edge is running with --remote-debugging-port. "
                f"Underlying: {exc}",
            )

        # Pick the first context (default profile). Chrome typically has one.
        contexts = browser.contexts
        if not contexts:
            try:
                await browser.close()
                await pw.stop()
            except Exception:
                pass
            return self._error(
                params, start,
                f"attach_browser: connected to {cdp_url} but no browser context "
                "is open. Make sure Chrome has at least one window.",
            )
        context = contexts[0]

        # Register existing tabs.
        sess = BrowserSession(
            playwright=pw,
            context=context,
            browser=browser,
            mode="attach",
            channel="attached",
            profile_dir="",
            cdp_url=cdp_url,
            last_used=time.time(),
        )
        registered: List[Dict[str, Any]] = []
        for page in context.pages:
            tid = sess.mint_tab_id()
            sess.tabs[tid] = page
            try:
                title = await page.title()
            except Exception:
                title = "(unavailable)"
            registered.append({"tab_id": tid, "url": page.url, "title": title})

        _session = sess
        self.logger.info(
            f"browser attached: cdp_url={cdp_url} tabs={len(registered)}",
            component="BrowserTool",
        )

        return ToolResult(
            success=True,
            output={
                "mode":     "attach",
                "cdp_url":  cdp_url,
                "tabs":     registered,
                "tab_count": len(registered),
                "note": (
                    "Connected to user's running Chrome. New tabs opened via "
                    "action='new_tab' default to background=true so the user's "
                    "focus is preserved. flush_browser_pool only DISCONNECTS — "
                    "your Chrome stays open."
                ),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    def _resolve_cdp_url(
        self, creds_file: Optional[str], browser_cfg: Dict[str, Any],
    ) -> tuple:
        """Resolve the CDP URL from credentials_file / config; return (url, source).

        Order of precedence:
          1. credentials_file (YAML or JSON) with cdp_url or cdp_port keys.
          2. browser.cdp_url in handq_config.yaml.
          3. browser.cdp_port in handq_config.yaml → http://localhost:<port>.

        Returns ``(url, source)`` where ``source`` is a human-readable origin
        for error messages. ``url`` is empty when nothing resolves.
        """
        # 1. credentials_file
        if creds_file:
            try:
                path = os.path.expanduser(creds_file)
                if not os.path.isfile(path):
                    return "", f"credentials_file (not found: {path})"
                import json as _json
                with open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
                # Try YAML first, fall back to JSON.
                try:
                    import yaml as _yaml  # type: ignore[import-not-found]
                    data = _yaml.safe_load(raw)
                except ImportError:
                    data = _json.loads(raw)
                if isinstance(data, dict):
                    if data.get("cdp_url"):
                        return str(data["cdp_url"]), f"credentials_file ({path})"
                    if data.get("cdp_port"):
                        return f"http://localhost:{int(data['cdp_port'])}", f"credentials_file ({path})"
                return "", f"credentials_file ({path}, missing cdp_url/cdp_port)"
            except Exception as exc:
                return "", f"credentials_file (parse failed: {exc})"

        # 2. config cdp_url
        cdp_url = (browser_cfg.get("cdp_url") or "").strip()
        if cdp_url:
            return cdp_url, "config browser.cdp_url"

        # 3. config cdp_port
        port = browser_cfg.get("cdp_port")
        if port:
            try:
                return f"http://localhost:{int(port)}", "config browser.cdp_port"
            except (TypeError, ValueError):
                pass

        return "", "config (no cdp_url/cdp_port)"

    async def _ensure_cdp_reachable(self, cdp_url: str) -> Optional[str]:
        """Make the CDP debug port reachable, hiding the bat from the user.

        Decision tree:

        1. Probe the port. If reachable → return None (caller proceeds).
        2. Port unreachable AND Chrome is NOT running:
           Spawn the bat detached, wait up to ~10s for the port to come up.
           This is the happy path — user never sees anything.
        3. Port unreachable AND Chrome IS running:
           We cannot get the port without restarting Chrome, which would
           destroy currently-open tabs. Surface this to the user via a
           risk_confirmation modal:
             Approve → kill chrome.exe / msedge.exe + spawn bat + wait
             Reject  → return error so the agent can fall back to
                       launch_browser instead.

        Returns None on success (port now reachable) or an error string
        describing what failed.

        All paths log progress at INFO so the bridge log shows what
        happened during setup. The bat path itself is never surfaced
        to the LLM or the user — it's an internal implementation detail.
        """
        host, port = _parse_cdp_url(cdp_url)
        self.logger.info(
            f"attach setup: probing CDP at {host}:{port}",
            component="BrowserTool",
        )
        if await _probe_cdp_port(host, port):
            self.logger.info(
                "attach setup: port already reachable, skipping spawn",
                component="BrowserTool",
            )
            return None

        bat_path = _find_bat_script()
        if not bat_path:
            return (
                "attach_browser: debug port unreachable AND the setup script "
                "(start_chrome_with_debug.bat) was not found. Reinstall HandQ "
                "or start Chrome manually with --remote-debugging-port="
                f"{port}."
            )

        chrome_running = _is_chrome_running()
        if chrome_running:
            # Need explicit user permission — restarting Chrome destroys
            # their currently-open tabs and any unsaved form state.
            self.logger.info(
                "attach setup: Chrome/Edge already running, asking user to restart",
                component="BrowserTool",
            )
            from ..controller.interaction_manager import InteractionManager
            from ..models.decision import Decision, ToolCall

            description = (
                "Agent wants to ATTACH to your Chrome / Edge so it can use your\n"
                "real cookies and login state.\n"
                "\n"
                "Chrome IS running, but it was not started with the debug port\n"
                "needed for this. To enable attach mode, HandQ needs to:\n"
                "  • Close all Chrome / Edge windows\n"
                "  • Restart Chrome with --remote-debugging-port=" + str(port) + "\n"
                "\n"
                "⚠ Currently open tabs and unsaved form data WILL BE LOST.\n"
                "  (Your bookmarks, history, and saved logins are unaffected.)\n"
                "\n"
                "Approve = HandQ closes Chrome and restarts it.\n"
                "Reject  = Cancel attach. Agent will fall back to launch_browser\n"
                "             (independent profile; no access to your current Chrome state)."
            )
            decision_proxy = Decision(
                reasoning="attach_browser auto-setup needs Chrome restart",
                tool_calls=[ToolCall(
                    call_id="attach_setup_restart",
                    tool_name="browser",
                    parameters={"action": "attach_browser", "_setup": "restart_chrome"},
                )],
            )
            try:
                im = InteractionManager.get_instance()
            except Exception as exc:
                return (
                    f"attach_browser: cannot ask user about Chrome restart "
                    f"(InteractionManager unavailable: {exc}). Close Chrome "
                    f"manually and retry."
                )
            loop = asyncio.get_event_loop()
            confirmation = await loop.run_in_executor(
                None,
                lambda: im.request_risk_confirmation(decision_proxy, description),
            )
            if confirmation.is_rejected() or confirmation.has_new_message():
                msg = (
                    confirmation.message
                    if confirmation.has_new_message() else
                    "User declined to restart Chrome."
                )
                return (
                    f"attach_browser: cancelled — {msg} "
                    "Use action='launch_browser' instead for an independent profile."
                )
            # Approved: kill Chrome.
            self.logger.info(
                "attach setup: user approved restart, killing Chrome / Edge",
                component="BrowserTool",
            )
            _kill_chrome_processes()
            await asyncio.sleep(1.0)  # let OS finalise process exits

        # Either Chrome wasn't running, or we just killed it. Spawn bat.
        try:
            self.logger.info(
                f"attach setup: spawning {bat_path}",
                component="BrowserTool",
            )
            _spawn_bat_detached(bat_path)
        except Exception as exc:
            return f"attach_browser: failed to spawn setup script: {exc}"

        # Wait up to 10s for the port to come up. Probe every 0.5s.
        for _ in range(20):
            await asyncio.sleep(0.5)
            if await _probe_cdp_port(host, port):
                self.logger.info(
                    "attach setup: port reachable after spawn",
                    component="BrowserTool",
                )
                return None

        return (
            f"attach_browser: Chrome did not start listening on {host}:{port} "
            "within 10 seconds after running the setup script. Try starting "
            "Chrome manually with --remote-debugging-port=" + str(port) + " "
            "or fall back to action='launch_browser'."
        )

    # ── new_tab ───────────────────────────────────────────────────────────────

    async def _action_new_tab(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Open a new tab in the current session.

        attach mode: uses CDP ``Target.createTarget`` with ``background=True``
        (default) so the user's focused tab is not switched. Falls back to
        ``context.new_page()`` if CDP path fails (which DOES steal focus).

        launch mode: ``background`` is irrelevant (the window is off-screen);
        ``context.new_page()`` is used directly.
        """
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' or 'attach_browser' first.")

        url: str = (kwargs.get("url") or "about:blank").strip()
        background: bool = bool(kwargs.get("background", True))
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_NAV_TIMEOUT_MS)

        if sess.mode == "attach" and background:
            # Open in background via CDP so the user's current tab keeps focus.
            page = None
            anchor = sess.context.pages[0] if sess.context.pages else None
            if anchor is None:
                # No anchor — fall back to non-background path.
                page = await sess.context.new_page()
            else:
                cdp = await sess.context.new_cdp_session(anchor)
                try:
                    # Use expect_page so Playwright wraps the new target.
                    async with sess.context.expect_page(timeout=timeout_ms) as info:
                        await cdp.send("Target.createTarget", {
                            "url": url,
                            "background": True,
                        })
                    page = await info.value
                finally:
                    try:
                        await cdp.detach()
                    except Exception:
                        pass
        else:
            # launch mode (window off-screen) or foreground attach.
            page = await sess.context.new_page()
            if url and url != "about:blank":
                try:
                    await page.goto(url, timeout=timeout_ms, wait_until="load")
                except _PlaywrightTimeoutError:
                    pass  # surface partial result; agent can retry navigate

        tid = sess.mint_tab_id()
        sess.tabs[tid] = page

        return ToolResult(
            success=True,
            output={
                "tab_id":     tid,
                "url":        page.url,
                "background": background and sess.mode == "attach",
                "mode":       sess.mode,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── list_tabs ─────────────────────────────────────────────────────────────

    async def _action_list_tabs(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        tabs: List[Dict[str, Any]] = []
        for tid, page in sess.tabs.items():
            try:
                url = page.url
                title = await page.title()
            except Exception as exc:
                # Page may have been closed externally (e.g. user closed tab
                # in attach mode). Mark as broken and skip.
                self.logger.warning(
                    f"list_tabs: tab {tid} unhealthy: {exc}",
                    component="BrowserTool",
                )
                url = ""
                title = "(unavailable)"
            tabs.append({"tab_id": tid, "url": url, "title": title})

        return ToolResult(
            success=True,
            output={
                "tabs":  tabs,
                "count": len(tabs),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── navigate ──────────────────────────────────────────────────────────────

    async def _action_navigate(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        url: str = (kwargs.get("url") or "").strip()
        if not url:
            return self._error(params, start, "navigate requires 'url'.")
        # RFC 3986 scheme check: <scheme>:<rest>. Accepts http://, https://,
        # data:, file:, about:, chrome: etc. Rejects "example.com" without
        # any scheme — Playwright would otherwise treat it as a relative
        # path and produce confusing errors.
        if not re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.IGNORECASE):
            return self._error(
                params, start,
                f"navigate: url must include a scheme (got {url!r}). "
                "Examples: https://example.com, data:text/html,<p>hi</p>",
            )

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_NAV_TIMEOUT_MS)
        wait_until = kwargs.get("wait_until") or "load"

        try:
            response = await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
        except _PlaywrightTimeoutError:
            return self._error(
                params, start,
                f"navigate timed out after {timeout_ms} ms loading {url}. "
                f"Current URL: {page.url}",
            )

        status = response.status if response is not None else None
        page_state = await self._capture_page_state(page)
        out: Dict[str, Any] = {
            "url":         page.url,
            "status":      status,
            "tab_id":      self._tab_id_of(sess, page),
            "wait_until":  wait_until,
        }
        if page_state:
            out["page_state"] = page_state
        return ToolResult(
            success=True,
            output=out,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── extract ───────────────────────────────────────────────────────────────

    async def _action_extract(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: Optional[str] = kwargs.get("selector")
        mode: str = (kwargs.get("mode") or "text").lower()

        # Cheap pre-flight: catch the '.<digit>' / '#<digit>' footgun before
        # Playwright spends a parse round-trip on it. Skipped for non-CSS
        # engine selectors (text=/xpath=/role=).
        if selector:
            invalid = _validate_css_selector(selector)
            if invalid is not None:
                return self._error(params, start, f"extract: {invalid}")

        sel_timeout_ms = int(
            kwargs.get("timeout_ms") or _DEFAULT_EXTRACT_SELECTOR_TIMEOUT_MS
        )

        try:
            if mode == "text":
                if selector:
                    text = await page.locator(selector).first.inner_text(timeout=sel_timeout_ms)
                else:
                    # Strip the noisiest static UI chrome before reading
                    # innerText. Filter dropdowns alone can be 1–2 KB on
                    # modern dashboards (the APT Auto page emitted ~1 KB
                    # of <select> options on every text extract); collapse
                    # each <select> to its current value, drop scripts /
                    # styles / datalists / SVGs / iframes entirely.
                    text = await page.evaluate(
                        """() => {
  if (!document.body) return '';
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,template,datalist,svg,iframe').forEach(e => e.remove());
  clone.querySelectorAll('select').forEach(sel => {
    const opts = sel.querySelectorAll('option');
    let chosen = sel.querySelector('option[selected]');
    if (!chosen && opts.length) chosen = opts[0];
    sel.innerHTML = '';
    if (chosen) sel.appendChild(chosen.cloneNode(true));
  });
  return clone.innerText || '';
}"""
                    )
                content = self._truncate(text or "")
                output: Dict[str, Any] = {
                    "mode":     "text",
                    "selector": selector,
                    "url":      page.url,
                    "content":  content,
                    "truncated": len(text or "") > _EXTRACT_MAX_CHARS,
                }

            elif mode == "html":
                if selector:
                    html = await page.locator(selector).first.evaluate(
                        "el => el.outerHTML", timeout=sel_timeout_ms,
                    )
                else:
                    html = await page.content()
                content = self._truncate(html or "")
                output = {
                    "mode":     "html",
                    "selector": selector,
                    "url":      page.url,
                    "content":  content,
                    "truncated": len(html or "") > _EXTRACT_MAX_CHARS,
                }

            elif mode == "list":
                if not selector:
                    return self._error(
                        params, start,
                        "extract mode=list requires a 'selector'. Use "
                        "action='snapshot' for a selectorless overview.",
                    )
                try:
                    raw_limit = int(kwargs.get("limit") or _EXTRACT_LIST_DEFAULT_LIMIT)
                except (TypeError, ValueError):
                    raw_limit = _EXTRACT_LIST_DEFAULT_LIMIT
                limit = max(1, min(raw_limit, _EXTRACT_LIST_MAX_LIMIT))

                locator = page.locator(selector)
                try:
                    count = await locator.count()
                except Exception as exc:
                    return self._error(
                        params, start,
                        f"extract list: locator.count failed for {selector!r}: {exc}",
                    )
                returned = min(count, limit)
                items: List[Dict[str, Any]] = []
                # Per-element JS that returns a compact, capped record.
                # Caps are enforced server-side in Python below as a defence
                # against weird DOM wrappers, but doing the slice in JS first
                # cuts the bridge payload.
                _per_item_js = (
                    "el => ({"
                    "tag: el.tagName ? el.tagName.toLowerCase() : '',"
                    "text: ((el.innerText || el.value || '') + '').slice(0, "
                    f"{_EXTRACT_LIST_ITEM_TEXT_CAP}),"
                    "outer_html: (el.outerHTML || '').slice(0, "
                    f"{_EXTRACT_LIST_ITEM_HTML_CAP}),"
                    "id: el.id || '',"
                    "name: el.getAttribute ? (el.getAttribute('name') || '') : '',"
                    "type: el.getAttribute ? (el.getAttribute('type') || '') : '',"
                    "})"
                )
                for i in range(returned):
                    try:
                        item = await locator.nth(i).evaluate(
                            _per_item_js, timeout=sel_timeout_ms,
                        )
                    except _PlaywrightTimeoutError:
                        # Element disappeared between count() and this
                        # iteration — stop early rather than fail the whole
                        # call; partial results are still useful.
                        break
                    except Exception as exc:
                        self.logger.debug(
                            f"extract list: item {i} eval failed: {exc}",
                            component="BrowserTool",
                        )
                        continue
                    items.append(item)
                output = {
                    "mode":     "list",
                    "selector": selector,
                    "url":      page.url,
                    "matched":  count,
                    "returned": len(items),
                    "limit":    limit,
                    "truncated": count > len(items),
                    "items":    items,
                }

            elif mode == "attr":
                if not selector:
                    return self._error(
                        params, start,
                        "extract mode=attr requires a 'selector'.",
                    )
                attr_name: Optional[str] = kwargs.get("attribute")
                if attr_name:
                    value = await page.locator(selector).first.get_attribute(
                        attr_name, timeout=sel_timeout_ms,
                    )
                    output = {
                        "mode":      "attr",
                        "selector":  selector,
                        "attribute": attr_name,
                        "value":     value,
                        "url":       page.url,
                    }
                else:
                    # All attributes as a dict
                    attrs = await page.locator(selector).first.evaluate(
                        "el => Object.fromEntries(Array.from(el.attributes).map(a => [a.name, a.value]))",
                        timeout=sel_timeout_ms,
                    )
                    output = {
                        "mode":     "attr",
                        "selector": selector,
                        "attributes": attrs,
                        "url":      page.url,
                    }
            else:
                return self._error(
                    params, start,
                    f"Unknown extract mode: {mode!r}. Use text|html|attr|list.",
                )

        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"extract: selector {selector!r} not found within "
                f"{sel_timeout_ms} ms: {exc}. If you are searching for what "
                "is on the page, prefer action='snapshot' over guessing "
                "selectors — it returns every interactable element in one call.",
            )

        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── snapshot ──────────────────────────────────────────────────────────────

    # Cap the number of interactable elements returned by snapshot. 200 is
    # large enough for almost any real dashboard (the APT Auto page has
    # ~480 buttons across 160 rows; 200 covers the visible viewport plus
    # any open dialog) while keeping the payload bounded.
    _SNAPSHOT_MAX_ELEMENTS: int = 200
    _SNAPSHOT_DIALOG_TEXT_CAP: int = 600
    _SNAPSHOT_NOTIFICATION_CAP: int = 200

    _SNAPSHOT_JS: str = """() => {
  const isVisible = el => {
    if (!el || !el.getClientRects) return false;
    const rects = el.getClientRects();
    if (rects.length === 0) return false;
    if (el.offsetParent === null) {
      // <dialog open> and fixed-position elements can have no offsetParent
      // but still be on-screen — fall back to bounding-rect check.
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }
    return true;
  };
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=tab], [role=menuitem], [onclick], [tabindex]:not([tabindex="-1"])';
  const seen = new Set();
  const items = [];
  for (const el of document.querySelectorAll(sel)) {
    if (seen.has(el) || !isVisible(el)) continue;
    seen.add(el);
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const text = ((el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '') + '').trim().slice(0, 100);
    items.push({
      tag,
      role: el.getAttribute('role') || '',
      text,
      id: el.id || '',
      name: el.getAttribute('name') || '',
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      disabled: !!el.disabled || el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
      onclick: ((el.getAttribute('onclick') || '') + '').slice(0, 140),
      href: ((el.getAttribute('href') || '') + '').slice(0, 200),
      value: ((el.value || '') + '').slice(0, 100),
      classes: ((el.className || '') + '').split(/\\s+/).filter(Boolean).slice(0, 5).join(' '),
      ariaLabel: el.getAttribute('aria-label') || '',
    });
    if (items.length >= __MAX__) break;
  }
  // Open dialogs: jconfirm, role=dialog, aria-modal, .modal.show, <dialog>.
  const dialogSel = '[role=dialog], [aria-modal="true"], .modal.show, .jconfirm.jconfirm-open, dialog[open]';
  const dialogs = [...document.querySelectorAll(dialogSel)]
    .filter(d => isVisible(d))
    .slice(0, 3)
    .map(d => {
      const titleEl = d.querySelector('.jconfirm-title, .modal-title, [class*="dialog-title"], h1, h2, h3, [aria-labelledby]');
      return {
        title: titleEl ? (titleEl.innerText || '').trim().slice(0, 140) : '',
        text: ((d.innerText || '') + '').trim().slice(0, __DIALOG_CAP__),
      };
    });
  // Toast / alert notifications.
  const notifSel = '.toast.show, .alert:not(.d-none):not([style*="display: none"]), [class*="notification"]:not([style*="display: none"]), [role=alert]';
  const notifications = [...document.querySelectorAll(notifSel)]
    .filter(n => isVisible(n))
    .slice(0, 5)
    .map(n => ((n.innerText || '') + '').trim().slice(0, __NOTIF_CAP__))
    .filter(Boolean);
  return {
    title: document.title || '',
    url: location.href,
    elements: items,
    dialogs,
    notifications,
  };
}"""

    async def _action_snapshot(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Compact accessibility-tree-style snapshot of interactable elements.

        Returns a structured listing of every visible button / link /
        form control, plus any open dialogs and toast notifications. For
        each element the tool emits a *suggested selector* that the LLM
        can hand back to ``click`` / ``type`` / ``extract`` directly,
        eliminating the selector-guessing loops that dominated the
        2026-05-23 APT Auto run (8 iterations to find the jconfirm
        modal structure, 4 more to find the OK button).

        Output shape:
          {
            url, title,
            elements: [{tag, role, text, selector, id, name, type,
                        placeholder, disabled, onclick, href, value,
                        classes}, ...],
            dialogs: [{title, text}, ...],
            notifications: [str, ...],
            summary: <multi-line markdown listing for the LLM>
          }
        """
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        js = (
            self._SNAPSHOT_JS
            .replace("__MAX__", str(self._SNAPSHOT_MAX_ELEMENTS))
            .replace("__DIALOG_CAP__", str(self._SNAPSHOT_DIALOG_TEXT_CAP))
            .replace("__NOTIF_CAP__", str(self._SNAPSHOT_NOTIFICATION_CAP))
        )
        try:
            raw = await page.evaluate(js)
        except Exception as exc:
            return self._error(params, start, f"snapshot: page.evaluate failed: {exc}")

        elements = list(raw.get("elements") or [])
        for el in elements:
            el["selector"] = self._suggest_selector(el)

        summary = self._format_snapshot_summary(raw, elements)

        output = {
            "url":           raw.get("url") or page.url,
            "title":         raw.get("title") or "",
            "elements":      elements,
            "element_count": len(elements),
            "truncated":     len(elements) >= self._SNAPSHOT_MAX_ELEMENTS,
            "dialogs":       raw.get("dialogs") or [],
            "notifications": raw.get("notifications") or [],
            "summary":       summary,
        }
        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    @staticmethod
    def _suggest_selector(el: Dict[str, Any]) -> str:
        """Pick a stable-looking selector for an element record from snapshot.

        Priority (most stable first):
          1. ``#<id>`` if id is present and a valid CSS ident.
          2. ``<tag>[name="..."]`` for form controls with a name.
          3. ``<tag>[onclick*="..."]`` when onclick has a recognisable
             handler call (e.g. ``handleDeviceAction('book', 756)``).
          4. ``<tag>.<class1>.<class2>[<href-fragment>]`` falling back to
             classes and an href fragment for anchors.
          5. Plain ``<tag>`` as a last resort — caller will need ``nth``.

        The returned string is meant for direct reuse with click / type /
        extract; avoiding text= so the same selector survives small UI
        copy changes.
        """
        tag = (el.get("tag") or "*").strip() or "*"
        el_id = (el.get("id") or "").strip()
        if el_id and re.match(r"^[A-Za-z][\w\-]*$", el_id):
            return f"#{el_id}"
        name = (el.get("name") or "").strip()
        if name:
            return f"{tag}[name=\"{name}\"]"
        onclick = (el.get("onclick") or "").strip()
        if onclick:
            # Prefer the function name + first arg literal — that pair is
            # usually unique on the page (handleDeviceAction('book', 756)
            # is the per-row dispatch on APT Auto).
            m = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", onclick)
            if m:
                fn, args = m.group(1), m.group(2).strip()
                fragment = f"{fn}({args})" if args else fn
                # Use a substring selector on onclick to dodge whitespace
                # / quoting differences between the attribute as parsed
                # and its raw form. CSS attribute substring is *= .
                escaped = fragment.replace('"', '\\"')
                return f'{tag}[onclick*="{escaped}"]'
        href = (el.get("href") or "").strip()
        classes = (el.get("classes") or "").split()
        if classes:
            cls = "".join("." + c for c in classes if re.match(r"^[A-Za-z_-][\w\-]*$", c))
            if cls:
                if href and href != "#":
                    return f'{tag}{cls}[href="{href}"]'
                return f"{tag}{cls}"
        if href and href != "#":
            return f'{tag}[href="{href}"]'
        return tag

    @staticmethod
    def _format_snapshot_summary(
        raw: Dict[str, Any], elements: List[Dict[str, Any]],
    ) -> str:
        """Render a markdown-style overview the LLM can scan in one read.

        Buckets elements by role (dialogs first, then buttons, links,
        form controls, other). Each line carries text, the suggested
        selector and the most useful disambiguator (onclick / disabled
        / value) so the LLM can pick a target without a second probe.
        """
        lines: List[str] = []
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        if title or url:
            lines.append(f"[snapshot] title={title!r} url={url}")
        else:
            lines.append("[snapshot]")

        dialogs = raw.get("dialogs") or []
        if dialogs:
            lines.append(f"\nDIALOGS ({len(dialogs)}):")
            for d in dialogs:
                t = (d.get("title") or "").strip() or "(untitled)"
                txt = (d.get("text") or "").strip().replace("\n", " ⏎ ")
                lines.append(f"  ## {t!r}")
                if txt:
                    lines.append(f"     text: {txt[:300]!r}")

        notifications = raw.get("notifications") or []
        if notifications:
            lines.append(f"\nNOTIFICATIONS ({len(notifications)}):")
            for n in notifications:
                txt = (n or "").strip().replace("\n", " ⏎ ")
                if txt:
                    lines.append(f"  - {txt!r}")

        buttons, links, fields, other = [], [], [], []
        for el in elements:
            tag = el.get("tag") or ""
            role = el.get("role") or ""
            if tag == "button" or role == "button":
                buttons.append(el)
            elif tag == "a" or role == "link":
                links.append(el)
            elif tag in ("input", "textarea", "select"):
                fields.append(el)
            else:
                other.append(el)

        def _fmt(el: Dict[str, Any]) -> str:
            text = (el.get("text") or "").strip().replace("\n", " ⏎ ")
            sel = el.get("selector") or ""
            extras: List[str] = []
            if el.get("disabled"):
                extras.append("disabled")
            if el.get("type"):
                extras.append(f"type={el['type']}")
            if el.get("placeholder"):
                extras.append(f"placeholder={el['placeholder']!r}")
            if el.get("value"):
                extras.append(f"value={el['value']!r}")
            if el.get("href"):
                extras.append(f"href={el['href']!r}")
            extra_str = (" " + " ".join(extras)) if extras else ""
            label = f"{text!r}" if text else "(no text)"
            return f"  - {label} [selector: {sel}]{extra_str}"

        if buttons:
            lines.append(f"\nBUTTONS ({len(buttons)}):")
            lines.extend(_fmt(b) for b in buttons)
        if fields:
            lines.append(f"\nFORM FIELDS ({len(fields)}):")
            lines.extend(_fmt(f) for f in fields)
        if links:
            lines.append(f"\nLINKS ({len(links)}):")
            lines.extend(_fmt(l) for l in links)
        if other:
            lines.append(f"\nOTHER ({len(other)}):")
            lines.extend(_fmt(o) for o in other)
        return "\n".join(lines)

    # ── click ─────────────────────────────────────────────────────────────────

    async def _action_click(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: str = (kwargs.get("selector") or "").strip()
        if not selector:
            return self._error(params, start, "click requires 'selector'.")
        invalid = _validate_css_selector(selector)
        if invalid is not None:
            return self._error(params, start, f"click: {invalid}")
        nth = int(kwargs.get("nth") or 0)
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_ACTION_TIMEOUT_MS)

        # Snapshot the URL before clicking so we can report navigation.
        url_before = page.url
        locator = page.locator(selector)
        if nth:
            locator = locator.nth(nth)
        else:
            locator = locator.first

        try:
            await locator.click(timeout=timeout_ms)
        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"click: selector {selector!r} not actionable within {timeout_ms} ms "
                f"(element missing, hidden, or covered). Underlying: {exc}",
            )
        except Exception as exc:
            return self._error(params, start, f"click failed: {exc}")

        # Give the click time to trigger any navigation; do NOT block on
        # full networkidle (some sites keep long-poll connections alive).
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        except _PlaywrightTimeoutError:
            # Click may not have triggered navigation — that's fine.
            pass

        page_state = await self._capture_page_state(page)
        out: Dict[str, Any] = {
            "selector":   selector,
            "nth":        nth,
            "url_before": url_before,
            "url_after":  page.url,
            "navigated":  url_before != page.url,
        }
        if page_state:
            out["page_state"] = page_state
        return ToolResult(
            success=True,
            output=out,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── type ──────────────────────────────────────────────────────────────────

    async def _action_type(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: str = (kwargs.get("selector") or "").strip()
        if not selector:
            return self._error(params, start, "type requires 'selector'.")
        invalid = _validate_css_selector(selector)
        if invalid is not None:
            return self._error(params, start, f"type: {invalid}")
        if "text" not in kwargs:
            # Empty string is a valid value (clearing a field), so check the
            # key existence rather than truthiness.
            return self._error(params, start, "type requires 'text'.")
        text: str = str(kwargs["text"])
        nth = int(kwargs.get("nth") or 0)
        press_enter = bool(kwargs.get("press_enter", False))
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_ACTION_TIMEOUT_MS)

        locator = page.locator(selector)
        if nth:
            locator = locator.nth(nth)
        else:
            locator = locator.first

        # ── PASSWORD GUARD ─────────────────────────────────────────────────
        # Server-side enforcement: refuse type on input[type=password].
        # Defence-in-depth: even if the system prompt is overridden / drifts,
        # the agent cannot silently fill a password field. Phase 3 wires up
        # request_user_login as the alternative.
        try:
            elem_type = await locator.get_attribute("type", timeout=timeout_ms)
        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"type: selector {selector!r} not found within {timeout_ms} ms: {exc}",
            )
        except Exception as exc:
            return self._error(params, start, f"type: probe failed: {exc}")
        if (elem_type or "").strip().lower() == "password":
            return self._error(params, start, _PASSWORD_REFUSAL)

        try:
            # locator.fill clears existing content then types the new value.
            # Safer than locator.type which appends to whatever is there.
            await locator.fill(text, timeout=timeout_ms)
        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"type: fill timed out after {timeout_ms} ms: {exc}",
            )
        except Exception as exc:
            return self._error(params, start, f"type failed: {exc}")

        url_before = page.url
        if press_enter:
            try:
                await locator.press("Enter", timeout=timeout_ms)
                # Brief settle for any submit-triggered navigation.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=3_000)
                except _PlaywrightTimeoutError:
                    pass
            except Exception as exc:
                return self._error(params, start, f"type: press Enter failed: {exc}")

        page_state = await self._capture_page_state(page)
        out: Dict[str, Any] = {
            "selector":     selector,
            "nth":          nth,
            "text_length":  len(text),
            "press_enter":  press_enter,
            "url_before":   url_before,
            "url_after":    page.url,
            "navigated":    url_before != page.url,
        }
        if page_state:
            out["page_state"] = page_state
        return ToolResult(
            success=True,
            output=out,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── wait_for ──────────────────────────────────────────────────────────────

    async def _action_wait_for(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: Optional[str] = kwargs.get("selector")
        url_pattern: Optional[str] = kwargs.get("url_pattern")

        if not selector and not url_pattern:
            return self._error(
                params, start,
                "wait_for requires either 'selector' or 'url_pattern'.",
            )
        if selector and url_pattern:
            return self._error(
                params, start,
                "wait_for: 'selector' and 'url_pattern' are mutually exclusive.",
            )
        if selector:
            invalid = _validate_css_selector(selector)
            if invalid is not None:
                return self._error(params, start, f"wait_for: {invalid}")

        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_ACTION_TIMEOUT_MS)

        if selector:
            state: str = (kwargs.get("state") or "visible").lower()
            valid_states = {"visible", "hidden", "attached", "detached"}
            if state not in valid_states:
                return self._error(
                    params, start,
                    f"wait_for: invalid state {state!r}. Valid: {sorted(valid_states)}",
                )
            try:
                await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            except _PlaywrightTimeoutError as exc:
                return self._error(
                    params, start,
                    f"wait_for: selector {selector!r} did not reach state "
                    f"{state!r} within {timeout_ms} ms. Current URL: {page.url}. "
                    f"Underlying: {exc}",
                )
            return ToolResult(
                success=True,
                output={
                    "selector": selector,
                    "state":    state,
                    "url":      page.url,
                },
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )

        # url_pattern path. We accept Python regex syntax and pass a
        # compiled regex to Playwright's wait_for_url so the LLM has a
        # familiar syntax (re.compile-style) instead of glob.
        try:
            compiled = re.compile(url_pattern or "")
        except re.error as exc:
            return self._error(
                params, start,
                f"wait_for: invalid url_pattern regex {url_pattern!r}: {exc}",
            )
        try:
            await page.wait_for_url(compiled, timeout=timeout_ms)
        except _PlaywrightTimeoutError:
            return self._error(
                params, start,
                f"wait_for: URL did not match {url_pattern!r} within "
                f"{timeout_ms} ms. Current URL: {page.url}",
            )
        return ToolResult(
            success=True,
            output={
                "url_pattern": url_pattern,
                "url":         page.url,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── screenshot ────────────────────────────────────────────────────────────

    async def _action_screenshot(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        path_arg: Optional[str] = kwargs.get("path")
        full_page = bool(kwargs.get("full_page", False))
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_ACTION_TIMEOUT_MS)
        selector: Optional[str] = kwargs.get("selector")

        # Resolve output path. Absolute paths used as-is; relative or empty
        # paths fall back to the 'task' tier (auto-cleaned at session
        # close per screenshots.task.retain_after_task_days). For
        # long-term keepers the agent should write into its session
        # working_directory directly via an absolute path — see
        # ARCHITECTURE.md §1.6.
        store = _browser_store()
        if path_arg and os.path.isabs(path_arg):
            out_path = path_arg
            wrote_to_store = False
        else:
            base_dir = store.subdir("task")
            if path_arg:
                out_path = os.path.join(base_dir, path_arg)
            else:
                ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
                out_path = os.path.join(base_dir, f"screenshot-{ts}.png")
            wrote_to_store = True
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        try:
            if selector:
                locator = page.locator(selector).first
                await locator.screenshot(path=out_path, timeout=timeout_ms)
            else:
                await page.screenshot(
                    path=out_path,
                    full_page=full_page,
                    timeout=timeout_ms,
                )
        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"screenshot: not ready within {timeout_ms} ms: {exc}",
            )
        except Exception as exc:
            return self._error(params, start, f"screenshot failed: {exc}")

        try:
            size = os.path.getsize(out_path)
        except OSError:
            size = 0

        # Apply retention only when the file landed in the store.
        # Absolute-path writes are caller-managed.
        if wrote_to_store:
            store.enforce_retention("task")

        return ToolResult(
            success=True,
            output={
                "path":      out_path,
                "selector":  selector,
                "full_page": full_page if not selector else None,
                "url":       page.url,
                "bytes":     size,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── vision_query (Phase 1, shipped) ───────────────────────────────────────
    #
    # Asks a vision LLM about the visible page. Routes through
    # ``infrastructure.vision`` (the ``client.py`` submodule) which talks
    # to the QGenie gateway
    # (azure::gpt-5.4-mini); endpoint / api_key / model live in
    # ``handq_config.yaml`` under the ``vision:`` section.
    #
    # Why a separate action instead of "screenshot with vision=true":
    #   • Decouples cost: vision_query owns the per-call LLM spend; the
    #     plain screenshot stays free (just disk I/O).
    #   • Decouples context: vision_query returns a TEXT answer, so the
    #     image bytes never enter the main agent's context window. The
    #     small vision LLM eats the image, distills it to a string, and
    #     the main agent sees only the answer. This protects the main
    #     agent's KV-cache from per-screenshot churn.
    #   • Decouples model: vision_query pins to a dedicated multimodal
    #     model without forcing the main agent's planner / executor to
    #     be vision-capable.
    #
    # Misuse guardrail lives at the prompt layer (tool_registry.py
    # browser usage_guide → "VISION_QUERY DECISION RULE"), not as a
    # numeric quota — the agent already has anti-loop machinery
    # (_failed_approach_signature, observation compaction) that
    # subsumes a per-step counter.

    async def _action_vision_query(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Ask a vision LLM about the visible browser page.

        The agent sends a natural-language question; the tool screenshots the
        viewport (or a selector subtree), downscales the image, ships it to a
        small multimodal model, and returns a TEXT answer. The image itself
        never enters the main agent's context — only the distilled answer.

        Primary use cases:
          * Reading content rendered inside ``<canvas>`` (charts, games,
            whiteboards, PDF.js pages) where DOM extract returns nothing.
          * Image-heavy pages (Pinterest, social feeds) where text content
            is sparse and the meaning lives in the visuals.
          * Layout queries the agent cannot answer from extract alone:
            "where is the Sign In button?", "is this row highlighted?".
          * Captcha / verification page detection (cannot SOLVE — only
            recognise that the page has one and bail out to
            request_user_login).

        Parameters (proposed schema)
        ----------------------------
        question : str  (required)
            Natural-language query. Tell the vision model what to look for.
            Examples:
              - "What does this chart show? Give me the trend in one sentence."
              - "Where is the 'Sign in with Google' button? Reply with a
                 CSS selector if possible, otherwise viewport coordinates."
              - "Is there a captcha on this page? Yes/no plus reason."

        tab_id : str  (optional, default = first tab)
            Which tab to capture. Same semantics as the other actions.

        selector : str  (optional)
            Restrict the screenshot to a sub-element. When omitted, the
            full viewport is used (or the full scrollable page if
            full_page=true). Selector takes precedence over full_page.

        full_page : bool  (optional, default false)
            Capture the entire scrollable page rather than just the
            viewport. Costs more tokens. Ignored when selector is set.

        max_image_dim : int  (optional, default 1024)
            Resize the long edge to this many pixels before sending.
            Below 768 most details are lost; above 1568 hits Claude's
            scale-down behaviour without paying off in detail. 1024 is
            the sweet spot for most "what is on this page" queries.

        timeout_ms : int  (optional, default 30000)
            Deadline for the whole pipeline (screenshot + LLM call).
            The vision model is the slow part — 10–20 s typical.

        output_schema : dict  (optional)
            JSON Schema the vision model must conform to. When set, the
            model's reply is parsed as JSON; on parse failure the call
            returns success=False with a clear error. Without this, the
            answer is free text. Recommended pattern for action queries:
              {"type": "object", "properties":
                 {"found": {"type": "boolean"},
                  "selector": {"type": "string"},
                  "coords": {"type": "array", "items": {"type": "number"},
                             "minItems": 2, "maxItems": 2},
                  "description": {"type": "string"}},
               "required": ["found", "description"]}

        Returns (proposed)
        ------------------
        On success::

            {
              "answer":         <str | dict>   # free text or parsed JSON
              "screenshot":     <abs path>     # archived for traceability
              "image_dims":     [w, h]         # post-resize, pre-send
              "tokens_input":   int            # billing visibility
              "tokens_output":  int
              "model":          str            # which vision model answered
              "url":            str            # page url at capture time
            }

        On failure::

            {success: False, error: <one-line reason>}

        Failure modes to surface:
          - playwright unavailable (tool itself disabled)
          - no vision-capable LLMService configured in handq_config.yaml
          - per-step quota exceeded (RuntimeAgent enforces; tool reports)
          - screenshot capture timeout
          - vision LLM call timeout / rate-limit / 4xx
          - output_schema given but reply did not parse / validate

        Implementation outline
        ----------------------
        1. Resolve tab + take screenshot to a temp PNG (re-use
           ``_action_screenshot`` infra; do not duplicate path logic).
        2. Open with PIL, resize to max_image_dim long-edge (preserve
           aspect ratio), re-encode as PNG (or JPEG quality 80 for size).
        3. Base64-encode and build a single user-turn message:
              [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": <b64>}},
                {"type": "text", "text": <prompt + output_schema hint>}
              ]
        4. ``call_with_fallback(self._vision_services, dict(messages=...))``
           — services list plumbed through from FlowController.
        5. If output_schema given, ``json_repair.parse(content)`` + jsonschema
           validate; on failure return success=False with the raw content.
        6. Return the structured result above.

        Cost reference (Anthropic, 2026-05 prices)
        ------------------------------------------
        Claude 4.5 Haiku multimodal: ~1500 input tokens for a 1024×768
        image + prompt overhead, ~100 output tokens for a one-line answer.
        Per call ≈ $0.001 with Haiku, ≈ $0.005 with Sonnet. A typical
        agent run consumes 1-3 vision_query calls. Misuse prevention
        is handled at the prompt layer (see VISION_QUERY DECISION RULE
        in tool_registry.py), not via a numeric per-step quota.
        """
        # ── Resolve config + question / tab / capture region ─────────────────
        from ..infrastructure.config_manager import ConfigManager
        from ..infrastructure.vision import get_vision_client
        try:
            cm = ConfigManager()
        except Exception as exc:
            return self._error(params, start, f"vision_query: config load failed: {exc}")

        question: str = (kwargs.get("question") or "").strip()
        if not question:
            return self._error(
                params, start,
                "vision_query requires 'question' — a natural-language "
                "instruction telling the model what to look for.",
            )
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")
        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: Optional[str] = kwargs.get("selector")
        full_page = bool(kwargs.get("full_page", False))
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_ACTION_TIMEOUT_MS)
        max_image_dim = kwargs.get("max_image_dim")  # may be None → use config default

        # ── Capture screenshot to a stable path ──────────────────────────────
        # vision_query work files always land in the ephemeral tier so a
        # high-frequency task (e.g. iterating on what's on a chart) cannot
        # blow up disk usage — the store's enforce_retention runs LRU + age
        # cleanup right after the write.
        store = _browser_store()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        out_path = os.path.join(store.subdir("ephemeral"), f"vision-{ts}.png")
        try:
            if selector:
                await page.locator(selector).first.screenshot(path=out_path, timeout=timeout_ms)
            else:
                await page.screenshot(path=out_path, full_page=full_page, timeout=timeout_ms)
        except _PlaywrightTimeoutError as exc:
            return self._error(
                params, start,
                f"vision_query: screenshot not ready within {timeout_ms} ms: {exc}",
            )
        except Exception as exc:
            return self._error(params, start, f"vision_query: screenshot failed: {exc}")
        store.enforce_retention("ephemeral")

        # ── Send to vision model ─────────────────────────────────────────────
        try:
            client = get_vision_client(cm)
        except Exception as exc:
            return self._error(params, start, f"vision_query: vision client unavailable: {exc}")
        # Per-call max_image_dim override is supported by VisionClient via
        # constructor; we apply it by temporarily overriding the instance
        # attribute since we share one singleton across calls. This keeps
        # the API simple — most calls just use the config default.
        prev_max_dim: Optional[int] = None
        if isinstance(max_image_dim, int) and max_image_dim > 0:
            prev_max_dim = client.max_image_dim
            client.max_image_dim = max_image_dim

        output_schema = kwargs.get("output_schema") if isinstance(kwargs.get("output_schema"), dict) else None
        try:
            result = await client.query(
                out_path, question,
                output_schema=output_schema,
                max_tokens=int(kwargs.get("max_tokens") or 600),
            )
        finally:
            if prev_max_dim is not None:
                client.max_image_dim = prev_max_dim

        if not result.ok:
            return self._error(params, start, f"vision_query: {result.error}")

        return ToolResult(
            success=True,
            output={
                "answer":        result.answer,
                "parsed_json":   result.parsed_json,
                "screenshot":    out_path,
                "image_dims":    list(result.image_dims),
                "elapsed_ms":    result.elapsed_ms,
                "tokens_input":  result.tokens_input,
                "tokens_output": result.tokens_output,
                "model":         result.model,
                "url":           page.url,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── video_context (Phase 1.5) ────────────────────────────────────────────
    #
    # Pulls TEXT context for the active <video> on the page: title,
    # description, duration, and (most importantly) caption / subtitle
    # text via the HTML5 textTracks API. This lets the agent answer
    # "what is this video about" / "is there a part about X" / "watch
    # this section before clicking next" WITHOUT calling vision_query
    # at any frame rate. Single-keyframe vision_query is still fine for
    # "what does the slide at 2:30 LOOK like" — pair it with seek_to_s
    # + pause=true here, then call screenshot + vision_query.
    #
    # Why a focused action instead of a generic page.evaluate:
    #   • Whitelisted JS — we read DOM/textTracks fields, never execute
    #     arbitrary author code from the LLM.
    #   • Bounded payload — caption arrays are capped at max_cues so a
    #     2-hour podcast does not flood the agent's context.
    #   • Site-aware fallbacks — YouTube/Bilibili keep their custom UI
    #     captions outside textTracks; the helper checks the visible
    #     caption container as a last-resort signal.

    _VIDEO_SEEK_PAUSE_JS: str = """({selector, seek, pause}) => {
  const v = selector ? document.querySelector(selector)
                     : [...document.querySelectorAll('video')]
                       .find(el => el.getClientRects().length > 0);
  if (!v) return {ok: false, reason: 'no video element found'};
  try {
    if (typeof seek === 'number' && isFinite(seek)) v.currentTime = seek;
    if (pause) v.pause();
    return {ok: true, currentTime: v.currentTime, paused: v.paused};
  } catch (exc) {
    return {ok: false, reason: String(exc)};
  }
}"""

    _VIDEO_CONTEXT_JS: str = """async ({selector, max_cues}) => {
  const fallbacks = [];
  const v = selector ? document.querySelector(selector)
                     : [...document.querySelectorAll('video')]
                       .find(el => el.getClientRects().length > 0);
  if (!v) return {error: 'no visible <video> element found on this page'};

  // textTracks populate asynchronously after the player loads — cues
  // can be empty for the first ~500-1500ms even when subtitles will
  // eventually arrive. Poll up to 1.5s before giving up so we don't
  // falsely report "no_text_tracks" on a still-initialising player.
  const pickActive = () => {
    let a = [...v.textTracks].find(t => t.mode === 'showing');
    if (!a) a = [...v.textTracks].find(t => t.cues && t.cues.length > 0);
    return a;
  };
  let waitedMs = 0;
  let active = pickActive();
  const deadline = Date.now() + 1500;
  while ((!active || !active.cues || active.cues.length === 0) && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 100));
    active = pickActive();
    waitedMs = Math.min(1500, Date.now() - (deadline - 1500));
  }

  const tracks = [...v.textTracks].map(t => ({
    kind: t.kind || '',
    language: t.language || '',
    label: t.label || '',
    mode: t.mode || '',
    cues_count: t.cues ? t.cues.length : 0,
  }));

  if (!active) fallbacks.push('no_text_tracks');

  const captions = [];
  let truncated = false;
  if (active && active.cues) {
    const total = active.cues.length;
    const limit = Math.max(1, Math.min(total, max_cues || 500));
    for (let i = 0; i < limit; i++) {
      const c = active.cues[i];
      const text = ((c.text || '') + '').replace(/\\s+/g, ' ').trim();
      if (text) captions.push({t: c.startTime, text});
    }
    truncated = total > limit;
  }

  // Site-specific custom-UI caption containers (YouTube / Bilibili / generic).
  let visibleCaption = '';
  const visualSelectors = [
    '.ytp-caption-segment',
    '.bilibili-player-subtitle-panel-text',
    '.bpx-player-subtitle-panel-text',
    '[class*="subtitle"][class*="text"]',
    '[class*="caption"][class*="text"]',
  ];
  for (const sel of visualSelectors) {
    const els = [...document.querySelectorAll(sel)];
    const text = els.map(e => (e.innerText || '').trim()).filter(Boolean).join(' ').trim();
    if (text) { visibleCaption = text; break; }
  }
  if (!visibleCaption && captions.length === 0) fallbacks.push('no_visible_caption');

  // Page metadata.
  const meta = (n) => {
    const el = document.querySelector(`meta[property="og:${n}"]`)
            || document.querySelector(`meta[name="${n}"]`);
    return el ? (el.getAttribute('content') || '') : '';
  };
  const title = (document.title || '').trim() || meta('title');
  const description = meta('description');

  return {
    url: location.href,
    page: {title, description},
    video: {
      selector: selector || 'video',
      src: v.currentSrc || v.src || '',
      duration_s: isFinite(v.duration) ? v.duration : 0,
      current_time_s: v.currentTime || 0,
      paused: !!v.paused,
      ended: !!v.ended,
      tracks,
      captions,
      captions_truncated: truncated,
    },
    visible_caption: visibleCaption,
    fallbacks_used: fallbacks,
    waited_for_cues_ms: waitedMs,
  };
}"""

    async def _action_video_context(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Read text context for the active <video> on the page.

        Returns title, description, duration, current playback time,
        track listing, and a capped list of caption cues.  Use this
        BEFORE reaching for vision_query when the question is "what is
        this video about" / "is there a section about X" / "have I
        finished section N" — it's faster, more accurate, and consumes
        no vision tokens.

        See ``_VIDEO_CONTEXT_JS`` for the exact fields returned.
        """
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")
        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        selector: Optional[str] = kwargs.get("selector")
        try:
            max_cues = int(kwargs.get("max_cues") or 500)
        except (TypeError, ValueError):
            max_cues = 500
        max_cues = max(1, min(max_cues, 5000))   # hard ceiling

        seek_to_s = kwargs.get("seek_to_s")
        pause = bool(kwargs.get("pause", False))

        # Optional seek + pause before reading state — used when the
        # caller wants a specific frame for a follow-up screenshot.
        seek_result: Optional[Dict[str, Any]] = None
        if seek_to_s is not None or pause:
            try:
                seek_arg: Dict[str, Any] = {
                    "selector": selector,
                    "seek": float(seek_to_s) if seek_to_s is not None else None,
                    "pause": pause,
                }
                seek_result = await page.evaluate(self._VIDEO_SEEK_PAUSE_JS, seek_arg)
                # Tiny settle window so currentTime stabilises before
                # the main read; 150 ms covers HTML5 native players.
                await asyncio.sleep(0.15)
            except Exception as exc:
                self.logger.warning(
                    f"video_context: seek/pause failed (continuing): {exc}",
                    component="BrowserTool",
                )

        try:
            data = await page.evaluate(
                self._VIDEO_CONTEXT_JS,
                {"selector": selector, "max_cues": max_cues},
            )
        except Exception as exc:
            return self._error(params, start, f"video_context: page.evaluate failed: {exc}")

        if not isinstance(data, dict) or data.get("error"):
            return self._error(
                params, start,
                f"video_context: {data.get('error') if isinstance(data, dict) else 'no data returned'}",
            )

        if seek_result is not None:
            data["seek_result"] = seek_result
        return ToolResult(
            success=True,
            output=data,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── fetch_json ────────────────────────────────────────────────────────────

    async def _action_fetch_json(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Run an authenticated ``fetch()`` from inside the browser.

        Use this to call internal REST APIs without leaving the DOM in an
        unexpected state — cookies and SSO tokens from the persistent
        profile are reused automatically. ``same_origin=True`` (default)
        navigates a tab to the URL's origin first so SameSite-restricted
        auth cookies are sent.

        The tool runs inside the global ``_session_lock`` (acquired by
        ``execute``), so direct callers reusing ``acquire_browser_lock``
        must not call this method.
        """
        global _session

        url = (kwargs.get("url") or "").strip()
        if not url:
            return self._error(
                params, start,
                "fetch_json requires 'url' (absolute, with scheme).",
            )
        if _session is None:
            return self._error(
                params, start,
                "fetch_json: no browser session. "
                "Call action='launch_browser' first.",
            )

        method = (kwargs.get("method") or "GET").upper()
        headers = kwargs.get("headers") or {}
        body = kwargs.get("body")
        same_origin = bool(kwargs.get("same_origin", True))
        timeout_ms = int(kwargs.get("timeout_ms") or _DEFAULT_NAV_TIMEOUT_MS)

        if not isinstance(headers, dict):
            return self._error(
                params, start,
                f"fetch_json: 'headers' must be a flat object, got {type(headers).__name__}.",
            )

        try:
            result = await evaluate_fetch(
                _session,
                url=url,
                method=method,
                headers=headers,
                body=body,
                same_origin=same_origin,
                timeout_ms=timeout_ms,
            )
        except ValueError as exc:
            return self._error(params, start, f"fetch_json: {exc}")
        except Exception as exc:
            self.logger.error(
                f"fetch_json url={url!r} raised: {exc}",
                component="BrowserTool", exc_info=True,
            )
            return self._error(params, start, f"fetch_json failed: {exc}")

        _session.last_used = time.time()

        # JS-side error (network failure, timeout, malformed URL inside fetch).
        if result.get("error"):
            return ToolResult(
                success=False,
                output=None,
                tool_name=self.name,
                tool_parameters=params,
                error=(
                    f"fetch_json transport error ({result.get('error_name', 'Error')}): "
                    f"{result['error']}"
                ),
                execution_time=time.time() - start,
            )

        status = int(result.get("status") or 0)
        ok = 200 <= status < 400
        return ToolResult(
            success=ok,
            output={
                "status": status,
                "url": result.get("url") or url,
                "headers": result.get("headers") or {},
                "body": result.get("body") or "",
                "truncated": bool(result.get("truncated")),
            },
            tool_name=self.name,
            tool_parameters=params,
            error=None if ok else f"HTTP {status}",
            execution_time=time.time() - start,
        )

    # ── request_user_login ────────────────────────────────────────────────────

    async def _action_request_user_login(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        """Surface the browser on-screen and wait for the user to log in.

        Flow:
          1. Move the page's window from off-screen to (100, 100) and
             bring_to_front so the user sees the login page.
          2. Open a risk-confirmation modal in the HandQ UI (Phase 0 plumbing)
             with an explanation of why login is needed and instructions
             ("log in here, click Approve when done"). Block on the answer
             via run_in_executor so the asyncio loop stays free for the
             stdin reader / dispatcher.
          3. Move the window back off-screen regardless of outcome.
          4. If a success_url_pattern was given, verify the post-login URL
             matches it (the agent then knows whether login really completed).

        Password safety: this method NEVER reads, types, or screenshots
        the password field. The user enters credentials directly into
        Chrome's native UI — agent only observes the cookie that results
        (stored in the persistent profile and reused next session).
        """
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session. Call action='launch_browser' first.")

        page = self._resolve_tab(sess, kwargs.get("tab_id"))
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {kwargs.get('tab_id')!r}")

        reason: str = (kwargs.get("reason") or "").strip()
        if not reason:
            return self._error(
                params, start,
                "request_user_login requires 'reason' explaining why login is needed.",
            )
        success_url_pattern: Optional[str] = kwargs.get("success_url_pattern")
        # Pre-validate the regex so we fail fast (and keep the window off-screen)
        # if the pattern is malformed.
        compiled_pattern: Optional["re.Pattern[str]"] = None
        if success_url_pattern:
            try:
                compiled_pattern = re.compile(success_url_pattern)
            except re.error as exc:
                return self._error(
                    params, start,
                    f"request_user_login: invalid success_url_pattern "
                    f"{success_url_pattern!r}: {exc}",
                )

        url_before = page.url

        # ── 1. Surface the window on-screen ─────────────────────────────
        try:
            await self._set_window_bounds(page, 100, 100)
            await page.bring_to_front()
        except Exception as exc:
            self.logger.warning(
                f"request_user_login: window surfacing failed (proceeding anyway): {exc}",
                component="BrowserTool",
            )

        # ── 2. Build modal payload + block on user answer ───────────────
        # Lazy imports to avoid any chance of import cycles via the tool registry.
        from ..controller.interaction_manager import InteractionManager
        from ..models.decision import Decision, ToolCall

        description = (
            "Agent needs you to log in before continuing.\n\n"
            f"Why: {reason}\n"
            f"Page: {url_before}\n\n"
            "A browser window has been opened on your screen at the login page.\n"
            "  1. Complete the login in that window (HandQ never sees your password).\n"
            "  2. Return here and click Approve when finished.\n"
            "  3. (Or click Reject to cancel this task.)\n\n"
            "Cookies from your login will be stored locally so you do not need "
            "to log in again for this site in future sessions."
        )
        decision_proxy = Decision(
            reasoning="agent requested user login",
            tool_calls=[ToolCall(
                call_id="request_user_login",
                tool_name="browser",
                parameters={
                    "action": "request_user_login",
                    "reason": reason,
                    "tab_id": kwargs.get("tab_id"),
                },
            )],
        )

        try:
            im = InteractionManager.get_instance()
        except Exception as exc:
            # No IM singleton — running in a context without UI (test). Fall
            # back to auto-approving so callers can exercise the rest of the
            # flow; tests can stub this out.
            self.logger.warning(
                f"request_user_login: no InteractionManager available ({exc}); "
                "auto-approving for compatibility",
                component="BrowserTool",
            )
            confirmation = None
        else:
            loop = asyncio.get_event_loop()
            # Run the blocking IM call in an executor thread so the event
            # loop is free to dispatch other work (e.g. background tasks)
            # while we wait for the user.
            confirmation = await loop.run_in_executor(
                None,
                lambda: im.request_risk_confirmation(decision_proxy, description),
            )

        # ── 3. Move window off-screen regardless of outcome ─────────────
        try:
            await self._set_window_bounds(page, -32000, -32000)
        except Exception as exc:
            self.logger.warning(
                f"request_user_login: window hide failed (non-fatal): {exc}",
                component="BrowserTool",
            )

        # ── 4. Interpret the user's answer ──────────────────────────────
        # confirmation may be None when no IM was available (test path).
        if confirmation is not None:
            if confirmation.is_rejected():
                return self._error(
                    params, start,
                    "User rejected the login request. Task cancelled — try a "
                    "different approach or ask the user for clarification.",
                )
            if confirmation.has_new_message():
                # User typed guidance instead of yes/no. Surface it as a
                # successful action with a guidance field so the agent
                # incorporates it into its next decision.
                return ToolResult(
                    success=False,
                    output=None,
                    error=(
                        f"User provided guidance instead of approving: "
                        f"{confirmation.message}"
                    ),
                    tool_name=self.name,
                    tool_parameters=params,
                    execution_time=time.time() - start,
                )

        # Approved (or no IM in test mode) — verify URL and report.
        url_after = page.url
        matched = None
        if compiled_pattern is not None:
            matched = bool(compiled_pattern.search(url_after))

        output: Dict[str, Any] = {
            "url_before":      url_before,
            "url_after":       url_after,
            "url_changed":     url_before != url_after,
            "cookie_persisted": True,
            "note": (
                "Cookies from this session are stored under "
                "%USERPROFILE%\\HandQ\\browser_profile\\ and will be reused "
                "next time HandQ starts."
            ),
        }
        if compiled_pattern is not None:
            output["success_url_pattern"] = success_url_pattern
            output["matched_success_pattern"] = matched
            if not matched:
                output["warning"] = (
                    f"URL after login ({url_after!r}) does not match "
                    f"{success_url_pattern!r}; the user may not have completed "
                    "login as expected. Verify the page state before proceeding."
                )

        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── close_tab ─────────────────────────────────────────────────────────────

    async def _action_close_tab(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        sess = _session
        if sess is None:
            return self._error(params, start, "No browser session.")

        tab_id: Optional[str] = kwargs.get("tab_id")
        if not tab_id:
            return self._error(params, start, "close_tab requires 'tab_id'.")
        page = sess.tabs.get(tab_id)
        if page is None:
            return self._error(params, start, f"Unknown tab_id: {tab_id!r}")

        # Refuse to close the last remaining tab — Playwright auto-closes
        # the context when the last page closes, which would invalidate
        # the session unexpectedly. The agent should call close_browser
        # (Phase 5) for full shutdown.
        if len(sess.tabs) <= 1:
            return self._error(
                params, start,
                "Refused to close the only remaining tab. Use launch_browser "
                "to start a new session, or open another tab first.",
            )

        try:
            await page.close()
        except Exception as exc:
            return self._error(params, start, f"close failed: {exc}")
        sess.tabs.pop(tab_id, None)

        return ToolResult(
            success=True,
            output={
                "closed_tab":      tab_id,
                "remaining_tabs":  list(sess.tabs.keys()),
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _set_window_bounds(
        self, page: Any, left: int, top: int,
        width: int = 1280, height: int = 800,
    ) -> None:
        """Move the page's OS-level window to *(left, top)* via CDP.

        Used for stealth on/off-screen toggling: launch positions the window
        at -32000,-32000 via ``--window-position``; ``request_user_login``
        moves it on-screen with this helper, then back off-screen when
        the user is done.

        ``windowState: "normal"`` is required because Chromium ignores
        bounds while the window is minimised / maximised. This must run
        AFTER any prior minimise call so the bounds take effect.

        Best-effort: callers wrap this in try/except. Some Chrome builds
        clamp negative coordinates to monitor bounds — when the off-screen
        move is rejected, we fall back to leaving the window where it is
        (still less disruptive than headless detection).
        """
        cdp = await page.context.new_cdp_session(page)
        try:
            info = await cdp.send("Browser.getWindowForTarget", {})
            window_id = int(info["windowId"])
            await cdp.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {
                    "left":        left,
                    "top":         top,
                    "width":       width,
                    "height":      height,
                    "windowState": "normal",
                },
            })
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass

    # JS executed after every navigate / click / type to give the LLM a
    # tiny "what changed" packet without forcing a follow-up extract.
    # Mirrors the snapshot dialog/notification logic but skips the
    # element listing — keep this cheap (<5 ms typical) so it can run
    # on every interaction.
    _PAGE_STATE_JS: str = """() => {
  const isVisible = el => {
    if (!el || !el.getClientRects) return false;
    const rects = el.getClientRects();
    if (rects.length === 0) return false;
    if (el.offsetParent === null) {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }
    return true;
  };
  const dialogSel = '[role=dialog], [aria-modal="true"], .modal.show, .jconfirm.jconfirm-open, dialog[open]';
  const dialogs = [...document.querySelectorAll(dialogSel)].filter(isVisible);
  let dialog = null;
  if (dialogs.length) {
    const d = dialogs[dialogs.length - 1];
    const titleEl = d.querySelector('.jconfirm-title, .modal-title, [class*="dialog-title"], h1, h2, h3');
    dialog = {
      title: titleEl ? (titleEl.innerText || '').trim().slice(0, 140) : '',
      text: ((d.innerText || '') + '').trim().slice(0, 400),
    };
  }
  const notifSel = '.toast.show, .alert:not(.d-none):not([style*="display: none"]), [class*="notification"]:not([style*="display: none"]), [role=alert]';
  const notifications = [...document.querySelectorAll(notifSel)]
    .filter(isVisible)
    .slice(0, 3)
    .map(n => ((n.innerText || '') + '').trim().slice(0, 200))
    .filter(Boolean);
  return { title: document.title || '', dialog, notifications };
}"""

    async def _capture_page_state(self, page: Any) -> Dict[str, Any]:
        """Cheap "what just happened" probe used after navigate/click/type.

        Reports the topmost open dialog (title + first 400 chars of body)
        plus any visible toast / alert notifications. Designed to surface
        modal flows like the jconfirm Book / OK dialogs that confused the
        2026-05-23 APT Auto run — without that signal the LLM has to fire
        a screenshot + extract pair after every interaction just to learn
        whether the click opened a modal.

        Best-effort: any failure returns ``{}`` and the caller silently
        omits the page_state field. We never want this probe to mask the
        actual tool error.
        """
        try:
            result = await page.evaluate(self._PAGE_STATE_JS)
        except Exception as exc:
            self.logger.debug(
                f"page_state probe failed: {exc}",
                component="BrowserTool",
            )
            return {}
        if not isinstance(result, dict):
            return {}
        # Drop empty fields so the LLM only sees signal.
        out: Dict[str, Any] = {}
        title = (result.get("title") or "").strip()
        if title:
            out["title"] = title
        dialog = result.get("dialog")
        if isinstance(dialog, dict) and (dialog.get("title") or dialog.get("text")):
            out["dialog"] = {
                "title": (dialog.get("title") or "").strip(),
                "text":  (dialog.get("text") or "").strip(),
            }
        notifications = [n for n in (result.get("notifications") or []) if n]
        if notifications:
            out["notifications"] = notifications
        return out

    def _resolve_tab(self, sess: BrowserSession, tab_id: Optional[str]) -> Optional[Any]:
        """Return the Page for *tab_id*, or the first tab when None.

        Returns None when no tabs are open or the id is unknown so callers
        can produce an actionable error.
        """
        if tab_id:
            return sess.tabs.get(tab_id)
        first = sess.first_tab_id()
        if first is None:
            return None
        return sess.tabs[first]

    def _tab_id_of(self, sess: BrowserSession, page: Any) -> Optional[str]:
        for tid, p in sess.tabs.items():
            if p is page:
                return tid
        return None

    def _truncate(self, s: str) -> str:
        if len(s) <= _EXTRACT_MAX_CHARS:
            return s
        return s[:_EXTRACT_MAX_CHARS] + f"\n\n[... truncated at {_EXTRACT_MAX_CHARS} chars]"

    def _error(
        self, params: Dict[str, Any], start: float, msg: str,
    ) -> ToolResult:
        return ToolResult(
            success=False, output=None, error=msg,
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - start,
        )
