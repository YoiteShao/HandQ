# -*- coding: utf-8 -*-
"""Desktop Tool — Windows-native screen + input automation.

Architecture
============
Single tool exposing a Computer-Use-Agent (CUA) action set:

  screenshot      capture foreground window or full screen → PNG path
  list_windows    enumerate visible top-level windows
  find_element    OCR + (optional) vision fallback → (x, y) center
  click_at        mouse click at coordinates
  type_text       keyboard input
  drag            mouse drag from → to
  scroll          mouse wheel scroll at point
  hotkey          key combo (ctrl+c, alt+tab)
  key_press       single key press

Engine choice
-------------
Phase 0 benchmark selected RapidOCR (`infrastructure.vision.LocalOCR`)
as the primary local engine.  ``find_element`` walks:

  1. screenshot the chosen region
  2. RapidOCR → list of (text, bbox)
  3. rapidfuzz match `description` against OCR texts (token_set_ratio)
  4. on hit: return bbox center, source='ocr', elapsed ~1 s
  5. on miss: optionally fall back to LLM vision (vision_client.query
     with a JSON output_schema), source='vision', elapsed ~5 s

Sensitive window guard
----------------------
Before any read or write, the tool inspects the active foreground
window and refuses to operate when its title matches the configured
sensitive patterns (password managers, banking apps).  This is the
desktop analogue of browser_tool's password-field guard.

Concurrency
-----------
Mouse and keyboard are global singletons — every action serialises on
``_desktop_lock``.  ``is_concurrency_safe = False``.

DPI awareness
-------------
At first use the module sets DPI awareness to per-monitor v2 so
``pyautogui`` coordinates match the physical screen even when Windows
display scaling is on (200% on a 4K laptop is common and breaks naive
coordinates).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .base_tool import BaseTool, ToolResult
from ..infrastructure.logger import get_logger

if TYPE_CHECKING:
    from ..controller_v2.interaction_manager import InteractionManager


# ── Hard Windows / input deps ────────────────────────────────────────────────
# On Windows these are eager hard deps — desktop_tool is registered Windows-only
# and the libs must be present at module load, so any missing dep fails the
# whole launch (fail-fast). On non-Windows (e.g. the Linux HandQ runtime) the
# imports are skipped and the availability flags stay False; the existing
# ``if not _X_AVAILABLE`` guards in every action then return a clean error
# instead of the module failing to import. desktop_tool is never registered
# off-Windows, so the native symbols below are only reached on Windows anyway.
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import mss  # type: ignore[import-not-found]
    import pyautogui  # type: ignore[import-not-found]
    pyautogui.FAILSAFE = True       # corner-of-screen panic abort
    pyautogui.PAUSE = 0.05          # global throttle so OS UI keeps up

    import win32gui                 # type: ignore[import-not-found]
    import win32process             # type: ignore[import-not-found]
    import win32con                 # type: ignore[import-not-found]
    from PIL import Image           # type: ignore[import-not-found]
    import pywinauto                # type: ignore[import-not-found]
    from pywinauto import Desktop   # type: ignore[import-not-found]
    from rapidfuzz import fuzz      # type: ignore[import-not-found]
    import psutil                   # type: ignore[import-not-found]

    _MSS_AVAILABLE = True
    _PYAUTOGUI_AVAILABLE = True
    _WIN32_AVAILABLE = True
else:
    # Bind the native names to Any-typed None so static analysis treats the
    # Windows-only action code (all of it guarded by ``if not _X_AVAILABLE``)
    # as reachable-but-typed rather than "possibly unbound". None of these are
    # touched at runtime off-Windows.
    mss: Any = None
    pyautogui: Any = None
    win32gui: Any = None
    win32process: Any = None
    win32con: Any = None
    Image: Any = None
    pywinauto: Any = None
    Desktop: Any = None
    fuzz: Any = None
    psutil: Any = None

    _MSS_AVAILABLE = False
    _PYAUTOGUI_AVAILABLE = False
    _WIN32_AVAILABLE = False


# ── DPI awareness one-shot ───────────────────────────────────────────────────
# pyautogui reads/writes coordinates in physical pixels but on Windows
# 10/11 with display scaling > 100%, default DPI mode reports virtual
# (scaled) pixels. Setting per-monitor v2 awareness fixes the mismatch.
# We do this lazily on first call so plain ``import desktop_tool`` does
# not have process-wide side effects.
_DPI_INITIALISED = False


def _ensure_dpi_aware() -> None:
    global _DPI_INITIALISED
    if _DPI_INITIALISED or sys.platform != "win32":
        return
    _DPI_INITIALISED = True
    try:
        import ctypes
        # Per-monitor v2 (Win10+); falls back to per-monitor v1 on older.
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        # Non-fatal — coords just may be off on high-DPI machines.
        pass


# ── Module state ─────────────────────────────────────────────────────────────

_desktop_lock = asyncio.Lock()

# Process-wide desktop ownership lock for multi-session concurrency.
# Distinct from ``_desktop_lock`` above: that one serialises individual input
# *actions* (release between each click / keystroke). This one is held for the
# entire duration of a session's takeover (acquired on the first input action,
# released by ``DesktopState.reset_takeover_state`` when the task settles), so
# session A's multi-step desktop sequence never gets interleaved with session
# B's actions. B's input-driving calls await here until A's task settles.
_GLOBAL_DESKTOP_OWNERSHIP_LOCK = asyncio.Lock()
_GLOBAL_DESKTOP_OWNER: Optional["DesktopState"] = None

# Actions that drive real mouse / keyboard input and therefore must claim the
# cross-session ownership lock. Read-only actions (screenshot / snapshot /
# list_windows / find_element) are absent on purpose — they never take
# ownership and stay freely concurrent across sessions. Kept in sync with the
# handlers that call ``_input_action_guard`` (which itself remains the single
# place that flips takeover state and re-checks the sensitive-window gate).
_INPUT_ACTIONS = frozenset({
    "hover_at", "find_and_click", "click_at", "type_text",
    "drag", "scroll", "hotkey", "key_press",
})


def is_any_session_holding_desktop() -> bool:
    """True iff any DesktopState currently owns the cross-session
    desktop ownership lock.

    Direct read of ``_GLOBAL_DESKTOP_OWNER`` — no refcount, no mirror
    state. Used by :class:`PersonalityMonitor` to gate OCR capture during
    agent input: when ANY session is driving input through the desktop
    tool, the monitor's screenshot samples would otherwise interleave
    with agent-driven mouse / keyboard events. Querying directly (rather
    than mirroring via notifications) makes drift impossible — even when
    ``_force_release_session_locks`` force-resets the owner after a
    destroy timeout, the very next caller sees the new truth.
    """
    return _GLOBAL_DESKTOP_OWNER is not None


_desktop_store_instance: Optional[Any] = None

# RapidOCR cold-start (~600ms) is paid on the first find_element. To
# hide it, the first call to DesktopTool.execute fires a background
# task that loads the engine while the agent is still doing whatever
# else (likely list_windows / screenshot before find_element). One-shot
# guard so we never queue more than one warm-up.
_ocr_prewarm_started: bool = False


# ── Snapshot result cache ────────────────────────────────────────────────────
#
# Every desktop.snapshot call without a cache pays for a UIA tree walk
# (~300-500 ms) plus, on UIA miss, an OCR pass (~700 ms). The agent
# typically calls snapshot, picks an element, clicks it, then calls
# snapshot AGAIN to find the next element — even though the UI hasn't
# materially changed. The cache short-circuits the second call.
#
# Key:    hwnd (int)
# Value:  {
#           "output": <full snapshot ToolResult.output dict>,
#           "sig":    (foreground_pid, foreground_title, n_visible_windows),
#           "ts":     time.time() at population
#         }
#
# A hit requires:
#   1. matching hwnd
#   2. matching sig (catches focus-loss / window-list change)
#   3. age < TTL (catches in-window state drift the sig misses)
#
# Invalidation:
#   • Input actions whose state_after shows foreground_changed,
#     title_changed, or new_windows clear the entire cache
#     (``DesktopState.invalidate_on_state_change``).
#   • TTL expiry (lazy — checked at hit time, no sweeper).
#   • reset_takeover_state() clears at task boundary so a stale cache
#     can never bleed across tasks.
#
# Storage: the live cache is ``DesktopState.snapshot_cache`` (per-session,
# bound to the FlowControllerV2 that owns this DesktopState). There is no
# module-level mirror — the per-session dict is the single source of truth.
# Snapshot read/write go through ``self.state.snapshot_cache`` (see the
# ``snapshot`` action) and invalidation runs through
# ``self.state.invalidate_on_state_change(state_after)``.
_SNAPSHOT_CACHE_TTL_S: float = 30.0


def _snapshot_sig(state: Dict[str, Any]) -> Tuple[int, str, int]:
    """Build a 3-tuple sig from a _capture_state_before/after dict.

    Used by both the snapshot action (to validate cache hits) and the
    per-session invalidator. Pure helper — no module state.
    """
    return (
        int(state.get("foreground_pid", 0) or 0),
        str(state.get("foreground_title", "") or ""),
        len(state.get("visible_hwnds") or ()),
    )


def _prewarm_local_ocr_async() -> None:
    """Kick off a background task that loads the RapidOCR engine.

    Safe no-op when no event loop is running yet (e.g. import-time use
    in tests). Errors are swallowed because find_element will surface
    its own clear message if OCR is genuinely unavailable.
    """
    global _ocr_prewarm_started
    if _ocr_prewarm_started:
        return
    _ocr_prewarm_started = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop — nothing to do, skip
    def _load() -> None:
        try:
            from ..infrastructure.vision import get_local_ocr
            get_local_ocr()._ensure_engine()
        except Exception:
            pass
    loop.run_in_executor(None, _load)


def _desktop_store():
    """Lazy ScreenshotStore for the desktop tool. Roots at
    ``%USERPROFILE%\\HandQ\\desktop_shots\\`` per ARCHITECTURE.md §1.6.
    """
    global _desktop_store_instance
    if _desktop_store_instance is None:
        from ..infrastructure.config_manager import ConfigManager
        from ..infrastructure.vision import ScreenshotStore
        try:
            cfg = ConfigManager().get_section("screenshots") or {}
        except Exception:
            cfg = {}
        root = os.path.join(os.path.expanduser("~"), "HandQ", "desktop_shots")
        os.makedirs(root, exist_ok=True)
        _desktop_store_instance = ScreenshotStore(root=root, config_section=cfg)
    return _desktop_store_instance


async def flush_desktop_store() -> Dict[str, int]:
    """Session-boundary cleanup contract for desktop screenshots.

    Mirrors :func:`browser_tool.flush_browser_pool` and
    :func:`infrastructure.vision.flush_vision_client` so the
    flow-controller dispatcher only sees one shape:
    ``async () -> something``.

    Runs the store's session_close_sweep when an instance has been
    built; returns ``{"ephemeral": n_eph, "task": n_task}`` for
    logging. Best-effort: any IO failure is swallowed.
    """
    if _desktop_store_instance is None:
        return {"ephemeral": 0, "task": 0}
    try:
        return _desktop_store_instance.session_close_sweep()
    except Exception:
        return {"ephemeral": 0, "task": 0}


# ── Takeover state machine ──────────────────────────────────────────────────
#
# Desktop input actions cannot be hidden from the user — pyautogui injects
# OS-level events that look identical to human input. The user therefore
# needs:
#
#   1. A clear visual indicator while the agent is driving.
#   2. A revoke channel — e.g. a global hotkey routed by Electron that
#      sends ``user_input.kind = desktop_takeover_revoked`` back to the
#      bridge. We clear ``_task_approved`` so the runtime gate re-prompts
#      the user before the next desktop input action runs.
#
# Approval scope is the **whole task**, not the single ToolCall. The
# runtime agent (src/agent/runtime_agent.py:_check_before_act) checks
# ``is_task_approved()`` before every desktop ToolCall; on the first
# user-approved confirmation it calls ``mark_task_approved()`` and every
# subsequent desktop call in the same task passes silently. Boundaries
# that clear approval:
#
#   * ``revoke_takeover()`` — user pressed Ctrl+Shift+C in Electron.
#   * ``reset_takeover_state()`` — task ended (FlowController) or
#     ``new_session`` from the bridge.
#
# The Python side here only owns the STATE; the Electron overlay (full-
# screen rainbow border + corner watermark + Ctrl+Shift+C hook) reacts
# to the ``notify_desktop_takeover_started/ended`` events emitted via
# :class:`InteractionManager`. See `docs/desktop_tool.md` §11 for the
# full IPC contract.

_takeover_active: bool = False
_task_approved: bool = False
# Once the user revokes (Ctrl+Shift+C), we set this for the rest of the
# task so the YAML ``tool_desktop.auto_approve=true`` policy stops
# silently re-approving. The runtime gate then forces a real confirmation
# every desktop ToolCall until ``reset_takeover_state`` clears the flag
# at task end. Rationale: the user's just-now signal "stop driving" is
# stronger than their earlier YAML choice "always allow".
_task_user_rescinded: bool = False

# Module-level InteractionManager handle for the takeover state-change
# helpers. Cleared on ``new_session`` / shutdown so a stale ref can't
# outlive its flow.
_im_ref: Optional["InteractionManager"] = None


def set_interaction_manager(im: Optional["InteractionManager"]) -> None:
    """Wire (or clear with ``None``) the IM that the takeover helpers
    forward events through. Called by the bridge per session lifecycle."""
    global _im_ref
    _im_ref = im


def _start_takeover(reason: str = "input_action") -> None:
    """Mark the desktop as 'agent-driven' and emit the start event.
    Idempotent — if already active, just no-ops.
    """
    global _takeover_active
    if _takeover_active:
        return
    _takeover_active = True
    if _im_ref is not None:
        _im_ref.notify_desktop_takeover_started(reason)


def _end_takeover(reason: str = "task_ended") -> None:
    """Drop the 'agent-driven' state and emit the end event. Idempotent."""
    global _takeover_active
    if not _takeover_active:
        return
    _takeover_active = False
    if _im_ref is not None:
        _im_ref.notify_desktop_takeover_ended(reason)


def mark_task_approved() -> None:
    """Memo that the user has granted desktop control for the rest of
    this task. Set by the runtime gate after a successful confirmation
    (or when the YAML ``tool_desktop.auto_approve`` switch is on, so a
    revoke during an auto-approved task still forces a re-prompt before
    the next call).

    Also triggers the takeover-started event so the Electron overlay
    appears immediately on approval — not later when the first input
    action runs. Rationale: from the user's POV approval is the
    contract ("I'm letting the agent drive my desktop"); the indicator
    should reflect that contract, not the implementation detail of
    "first pyautogui call". This is also more robust to the agent
    being stuck on read-only actions (screenshots, find_element) that
    never trigger the input guard.
    """
    global _task_approved
    _task_approved = True
    _start_takeover("approved")


def is_task_approved() -> bool:
    """True while the current task is allowed to use desktop input
    without per-call confirmation. Cleared by revoke or task end.
    """
    return _task_approved


def revoke_takeover() -> bool:
    """Called from the bridge when the user signals 'stop driving'.

    Clears the task-scoped approval and ends the takeover overlay. The
    next desktop ToolCall will hit the runtime gate as if no approval
    had ever been given — the user is asked again, and if they say yes
    a fresh ``mark_task_approved()`` re-arms approval (and the overlay
    re-appears on the next input action).

    Also flips the per-task rescinded flag. While set, the runtime gate
    ignores ``tool_desktop.auto_approve=true`` and forces a real
    confirmation prompt — so a user who revokes mid-task always gets to
    re-decide explicitly, not be silently bulldozed by their old YAML
    choice. The flag clears at task end via ``reset_takeover_state``.

    Read-only actions (screenshot / list_windows / find_element) keep
    working throughout — they do not steal the user's input.

    Returns True when state actually changed, False when there was
    nothing to revoke (no approval and no active takeover).
    """
    global _task_approved, _task_user_rescinded
    if not _task_approved and not _takeover_active:
        return False
    _task_approved = False
    _task_user_rescinded = True
    # _end_takeover is idempotent; emits 'user_revoked' only if active.
    _end_takeover("user_revoked")
    return True


def was_user_rescinded() -> bool:
    """True if the user has revoked desktop control during the current
    task. Cleared at task end. The runtime gate uses this to suppress
    the YAML auto-approve while the rescinded flag is set, so a revoke
    actually forces the next call to ask.
    """
    return _task_user_rescinded


def reset_takeover_state() -> None:
    """Wipe approval + takeover (module-level, ctx=None fallback path).

    Sessions with a ``SessionContext`` use the per-instance
    :meth:`DesktopState.reset_takeover_state` instead, which is driven by
    :meth:`flow_controller.FlowControllerV2._forward_state_to_ui` on the
    task-settled (``idle``) transition and by ``DesktopState.close()`` at
    session teardown (``ctx.close()`` ← ``flow.destroy()`` ←
    ``stdio_bridge._do_close_session``). This module-level twin only backs
    the ctx-less path (test fixtures / direct module callers).

    Snapshot cache is not cleared here — there is no module-level cache;
    the per-session ``DesktopState.snapshot_cache`` is cleared in its own
    ``reset_takeover_state`` instance method.
    """
    global _task_approved, _task_user_rescinded
    if _takeover_active:
        _end_takeover("task_ended")
    _task_approved = False
    _task_user_rescinded = False


# ── Per-session desktop state class (used by SessionContext) ─────────────────
#
# Wraps the takeover state machine + IM ref + per-session snapshot cache + the
# (lazy) ScreenshotStore handle into a per-instance object. ``SessionContext``
# constructs one per session; ``ctx.close()`` calls :meth:`close` via
# ``asyncio.to_thread`` (the close path touches the screenshot store on disk
# but is sync).
#
# Process-level state that intentionally STAYS module-level:
#   * ``_DPI_INITIALISED`` — Windows DPI awareness is per-process by OS API
#     contract; setting it twice is harmless but flipping it per-session is
#     meaningless.
#   * ``_desktop_lock`` — mouse / keyboard exclusivity is per-display, hence
#     per-process. Two sessions in the same process must never drive input
#     concurrently regardless of who 'owns' the takeover.
#
# The module-level ``_takeover_active`` / ``_task_approved`` /
# ``_task_user_rescinded`` / ``_im_ref`` / ``set_interaction_manager`` /
# ``_start_takeover`` / ``_end_takeover`` / ``mark_task_approved`` /
# ``is_task_approved`` / ``revoke_takeover`` / ``was_user_rescinded`` /
# ``reset_takeover_state`` and the corresponding ``_snapshot_cache`` /
# ``_desktop_store_instance`` / ``_ocr_prewarm_started`` globals stay
# alongside this class for callers still routing through the module path.


class DesktopState:
    """Per-session desktop control state.

    Owns:

    * **Takeover state machine** — three bool flags + IM forwarder for the
      ``notify_desktop_takeover_started/ended`` events the Electron overlay
      listens on.
    * **Snapshot cache** — ``hwnd → cached snapshot output`` so the agent's
      common pattern (snapshot → click → snapshot) skips a UIA tree walk.
    * **OCR prewarm guard** — one-shot flag for the background warm-up.
    * **ScreenshotStore handle** — lazy because ConfigManager isn't ready at
      module load time.

    Construct with ``DesktopState(im=ctx.interaction_manager)`` so the
    forwarders fire on the current session's UI delegate. ``close()`` is sync
    and must run in a worker thread (``asyncio.to_thread``) because it touches
    the screenshot store's disk-side sweep.
    """

    def __init__(
        self,
        im: Optional["InteractionManager"] = None,
    ) -> None:
        self._im: Optional["InteractionManager"] = im
        # takeover state
        self.takeover_active: bool = False
        self.task_approved: bool = False
        self.task_user_rescinded: bool = False
        # Multi-session ownership flag — True iff this DesktopState currently
        # holds the process-wide _GLOBAL_DESKTOP_OWNERSHIP_LOCK. Acquired
        # lazily on the first input-driving action (idempotent); released by
        # reset_takeover_state when the task settles.
        self._owns_global_lock: bool = False
        # The event loop on which the ownership lock was acquired. asyncio.Lock
        # is NOT thread-safe: ``close()`` runs in a worker thread (to_thread)
        # and releasing from there would not wake a waiter parked in
        # ``acquire_global_takeover`` on the loop thread. We capture the loop at
        # acquire and bounce the release back onto it via call_soon_threadsafe
        # when releasing off-thread (F3).
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        # snapshot cache (hwnd → entry)
        self.snapshot_cache: Dict[int, Dict[str, Any]] = {}
        # OCR prewarm one-shot
        self.ocr_prewarm_started: bool = False
        # lazy ScreenshotStore (constructed on first get_store)
        self._store: Optional[Any] = None

    # ── IM wiring ────────────────────────────────────────────────────────

    def set_interaction_manager(self, im: Optional["InteractionManager"]) -> None:
        self._im = im

    @property
    def interaction_manager(self) -> Optional["InteractionManager"]:
        return self._im

    # ── Takeover state machine ───────────────────────────────────────────

    def start_takeover(self, reason: str = "input_action") -> None:
        if self.takeover_active:
            return
        self.takeover_active = True
        if self._im is not None:
            self._im.notify_desktop_takeover_started(reason)

    def end_takeover(self, reason: str = "task_ended") -> None:
        if not self.takeover_active:
            return
        self.takeover_active = False
        if self._im is not None:
            self._im.notify_desktop_takeover_ended(reason)

    def mark_task_approved(self) -> None:
        self.task_approved = True
        self.start_takeover("approved")

    def is_task_approved(self) -> bool:
        return self.task_approved

    def revoke_takeover(self) -> bool:
        if not self.task_approved and not self.takeover_active:
            return False
        self.task_approved = False
        self.task_user_rescinded = True
        self.end_takeover("user_revoked")
        return True

    def was_user_rescinded(self) -> bool:
        return self.task_user_rescinded

    def reset_takeover_state(self) -> None:
        if self.takeover_active:
            self.end_takeover("task_ended")
        self.task_approved = False
        self.task_user_rescinded = False
        self.snapshot_cache.clear()
        self._release_global_takeover_if_owned()

    # ── Global ownership lock (multi-session concurrency) ────────────────

    async def acquire_global_takeover(self) -> None:
        """Acquire the process-wide desktop ownership lock for the current
        task. Idempotent — re-entrant calls on the same DesktopState return
        immediately. Released by ``reset_takeover_state`` when the planner
        signals task settled, or by ``close`` when the session is destroyed.

        While this DesktopState owns the lock, other sessions' input-driving
        desktop actions block here until release. Read-only actions
        (screenshot, list_windows, find_element, snapshot) do NOT call this —
        they remain freely concurrent.
        """
        global _GLOBAL_DESKTOP_OWNER
        if self._owns_global_lock:
            return
        await _GLOBAL_DESKTOP_OWNERSHIP_LOCK.acquire()
        self._owns_global_lock = True
        self._owner_loop = asyncio.get_running_loop()
        _GLOBAL_DESKTOP_OWNER = self

    def _release_global_takeover_if_owned(self) -> None:
        """Release the process-wide desktop ownership lock iff this
        DesktopState currently holds it. Safe to call repeatedly.

        Loop-safe (F3): ``asyncio.Lock`` must be released on the loop it was
        acquired on, otherwise a waiter parked in ``acquire_global_takeover``
        may never be woken. ``close()`` runs in a worker thread, so when this
        is called off the owner loop we bounce the actual ``.release()`` onto
        it via ``call_soon_threadsafe``. The ownership flags are cleared
        synchronously first, so the slot reads as free the instant we return.
        """
        global _GLOBAL_DESKTOP_OWNER
        if not self._owns_global_lock:
            return
        self._owns_global_lock = False
        if _GLOBAL_DESKTOP_OWNER is self:
            _GLOBAL_DESKTOP_OWNER = None
        owner_loop = self._owner_loop
        self._owner_loop = None

        def _do_release() -> None:
            try:
                _GLOBAL_DESKTOP_OWNERSHIP_LOCK.release()
            except RuntimeError:
                # Lock wasn't held — defensive; paired acquire/release match.
                pass

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if owner_loop is not None and owner_loop is not running:
            # Off the owner loop (e.g. close() in a worker thread): schedule the
            # release on the owner loop so its selector wakes the next waiter.
            try:
                owner_loop.call_soon_threadsafe(_do_release)
            except RuntimeError:
                # Owner loop already closed — nothing parked on it to wake.
                _do_release()
        else:
            _do_release()

    # ── ScreenshotStore (lazy) ───────────────────────────────────────────

    def get_store(self):
        """Return the (lazily-built) ScreenshotStore for desktop captures."""
        if self._store is None:
            from ..infrastructure.config_manager import ConfigManager
            from ..infrastructure.vision import ScreenshotStore
            try:
                cfg = ConfigManager().get_section("screenshots") or {}
            except Exception:
                cfg = {}
            self._store = ScreenshotStore(
                root=os.path.join(
                    os.environ.get("USERPROFILE") or os.path.expanduser("~"),
                    "HandQ", "desktop_screenshots",
                ),
                config_section=cfg,
            )
        return self._store

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """End any active takeover, sweep the screenshot store, drop the
        snapshot cache. Sync — runs in a thread under ``asyncio.to_thread``
        because the store sweep touches disk.
        """
        try:
            self.reset_takeover_state()
        except Exception:
            pass
        if self._store is not None:
            try:
                self._store.session_close_sweep()
            except Exception:
                pass

    # ── Snapshot cache invalidation (was a module-level helper) ──────────

    def invalidate_on_state_change(self, state_after: Dict[str, Any]) -> None:
        """Drop the snapshot cache when an input action moved the UI.

        Whole-cache nuke is fine — there is at most 1-2 entries (one per
        foreground hwnd the agent has snapshotted this task). Rebuilding
        is the same cost as the first call.
        """
        if not self.snapshot_cache:
            return
        if (
            state_after.get("foreground_changed")
            or state_after.get("title_changed")
            or state_after.get("new_windows")
        ):
            self.snapshot_cache.clear()

    # ── OCR prewarm one-shot (was a module-level helper) ─────────────────

    def prewarm_ocr_if_needed(self) -> None:
        """Kick off a background task that loads the RapidOCR engine.

        Safe no-op when no event loop is running yet (e.g. import-time use
        in tests). Errors are swallowed — find_element surfaces its own
        clear message if OCR is genuinely unavailable. One-shot per-state
        instance: subsequent calls within the same session are no-ops.
        """
        if self.ocr_prewarm_started:
            return
        self.ocr_prewarm_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        def _load() -> None:
            try:
                from ..infrastructure.vision import get_local_ocr
                get_local_ocr()._ensure_engine()
            except Exception:
                pass
        loop.run_in_executor(None, _load)


# ── Per-action tunables ──────────────────────────────────────────────────────

_DEFAULT_MOUSE_DURATION = 0.0      # 0 = teleport; non-zero = animated drag
_DEFAULT_TYPE_INTERVAL = 0.02      # seconds between keystrokes
_DEFAULT_FUZZY_THRESHOLD = 70      # rapidfuzz token_set_ratio threshold
_DEFAULT_SENSITIVE_PATTERNS: Tuple[str, ...] = (
    r"(?i)bitwarden|1password|keepass|lastpass|dashlane",
    r"(?i)bank|wallet|crypto|trading",
)

# Safety net on list_windows so we never flood the LLM context.
_LIST_WINDOWS_CAP = 50

# Hard cap on type_text payload — long pastes should go through clipboard,
# not synthetic keystrokes (slow + leaks visible state).
_TYPE_TEXT_MAX_CHARS = 4000


# ── Window inspection ────────────────────────────────────────────────────────

def _foreground_window_info() -> Dict[str, Any]:
    """Return {hwnd, title, pid, process_name, rect: [x1,y1,x2,y2]} for the
    foreground window, or {} when win32 is unavailable / no foreground.
    """
    if not _WIN32_AVAILABLE:
        return {}
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {}
        title = win32gui.GetWindowText(hwnd) or ""
        rect = win32gui.GetWindowRect(hwnd)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        return {
            "hwnd": int(hwnd),
            "title": title,
            "pid": int(pid),
            "process_name": _process_name_for_pid(pid),
            "rect": list(rect),
        }
    except Exception:
        return {}


def _process_name_for_pid(pid: int) -> str:
    """Best-effort — returns '' if the PID is gone or not enumerable."""
    if not pid:
        return ""
    try:
        return psutil.Process(pid).name() or ""
    except Exception:
        return ""


def _enumerate_visible_windows(limit: int = _LIST_WINDOWS_CAP) -> List[Dict[str, Any]]:
    if not _WIN32_AVAILABLE:
        return []
    items: List[Dict[str, Any]] = []
    foreground_hwnd = 0
    try:
        foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        pass

    def _cb(hwnd: int, _ctx: Any) -> bool:
        if len(items) >= limit:
            return False
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if not title.strip():
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            items.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid),
                "process_name": _process_name_for_pid(pid),
                "rect": list(rect),
                "foreground": int(hwnd) == foreground_hwnd,
            })
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return items


# ── State-delta probes (the desktop analogue of browser's page_state) ────────
#
# Why this exists: input actions (click_at / type_text / drag / scroll /
# hotkey / key_press) return only "what they did" — coordinates, key
# names, payload sizes. Without a "what changed" signal the LLM has no
# choice but to follow up with a screenshot to verify, which costs
# ~5 s per round-trip and is the dominant cause of the 27-iter Teams /
# 60-iter OneNote slowness traces.
#
# Browser_tool already solves this with ``page_state`` (in-page DOM
# probe for title / open dialog / toast text). Desktop's equivalent
# is harder because there is no DOM — but Win32 + UIA give us:
#
#   * foreground window title / hwnd / pid (cheap, ~5 ms)
#   * the visible-window set (lets us spot NEW dialogs that opened
#     during the action — the most common state change after a click)
#
# Pattern used by every input action:
#
#   state_before = _capture_state_before()
#   <do the action>
#   await asyncio.sleep(0.1)            # let Windows react
#   state_after = _capture_state_after(state_before)
#   output["state_after"] = state_after
#
# Cost per action: ~50-150 ms (one foreground probe + one EnumWindows
# pass, both already cached in win32 internals). Net win: replaces
# multi-second screenshot+OCR follow-ups.

def _capture_state_before() -> Dict[str, Any]:
    """Snapshot foreground state + visible-window set before an action.

    The returned dict is opaque to callers — pass it back into
    :func:`_capture_state_after` to compute the delta.
    """
    info = _foreground_window_info()
    visible_hwnds: set = set()
    if _WIN32_AVAILABLE:
        for w in _enumerate_visible_windows(limit=200):
            try:
                visible_hwnds.add(int(w.get("hwnd", 0) or 0))
            except (TypeError, ValueError):
                continue
    return {
        "foreground_hwnd":  int(info.get("hwnd", 0) or 0),
        "foreground_title": info.get("title", "") or "",
        "foreground_pid":   int(info.get("pid", 0) or 0),
        "visible_hwnds":    visible_hwnds,
    }


def _capture_state_after(state_before: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the delta the LLM cares about after an input action.

    Returns a flat dict suitable for inclusion in ``ToolResult.output``::

        {
          "foreground_title": "Save As",
          "foreground_pid":   12345,
          "foreground_changed": True,    # hwnd or pid switched
          "title_changed":      False,   # same hwnd, new title text
          "new_windows": [
            {"hwnd": ..., "title": "Save As", "process_name": "ONENOTE.EXE"},
            ...
          ],
        }

    A populated ``new_windows`` list is the strongest signal — a new
    visible top-level window typically means a dialog opened. The
    LLM reading this can SKIP the follow-up screenshot, since title +
    new_windows already convey "what's new on screen".
    """
    info = _foreground_window_info()
    fg_hwnd  = int(info.get("hwnd", 0) or 0)
    fg_title = info.get("title", "") or ""
    fg_pid   = int(info.get("pid", 0) or 0)

    foreground_changed = (
        fg_hwnd != state_before.get("foreground_hwnd", 0)
        or fg_pid != state_before.get("foreground_pid", 0)
    )
    title_changed = (
        not foreground_changed
        and fg_title != state_before.get("foreground_title", "")
    )

    new_windows: List[Dict[str, Any]] = []
    if _WIN32_AVAILABLE:
        before_hwnds: set = state_before.get("visible_hwnds") or set()
        for w in _enumerate_visible_windows(limit=200):
            try:
                hwnd = int(w.get("hwnd", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not hwnd or hwnd in before_hwnds:
                continue
            new_windows.append({
                "hwnd":         hwnd,
                "title":        w.get("title", ""),
                "process_name": w.get("process_name", ""),
            })

    return {
        "foreground_title":   fg_title,
        "foreground_pid":     fg_pid,
        "foreground_changed": foreground_changed,
        "title_changed":      title_changed,
        "new_windows":        new_windows,
    }


# ── Sensitive window filter ──────────────────────────────────────────────────

def _load_sensitive_patterns() -> List["re.Pattern[str]"]:
    """Read patterns from handq_config.yaml; fall back to defaults on any error.
    Compiled once per call — cheap, and the config can be tweaked without
    a restart.
    """
    raw: List[str] = []
    try:
        from ..infrastructure.config_manager import ConfigManager
        cfg = ConfigManager().get_section("desktop") or {}
        raw = list(cfg.get("sensitive_window_patterns") or [])
    except Exception:
        raw = []
    if not raw:
        raw = list(_DEFAULT_SENSITIVE_PATTERNS)
    out: List["re.Pattern[str]"] = []
    for p in raw:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out


def _is_sensitive_window(title: str, process_name: str = "") -> bool:
    text = f"{title}\n{process_name}"
    for pat in _load_sensitive_patterns():
        if pat.search(text):
            return True
    return False


# ── Screen capture ───────────────────────────────────────────────────────────

def _screenshot_hwnd_via_print_window(
    out_path: str, hwnd: int,
) -> Tuple[Tuple[int, int, int, int], int, bool]:
    """Capture a SPECIFIC window by hwnd via PrintWindow, regardless of foreground state.

    Returns ((x1, y1, x2, y2), bytes_written, looked_black).

    ``looked_black`` is True when the captured bitmap appears to be a fully
    black rectangle — common for hardware-accelerated targets (Chromium,
    DirectX surfaces) where PrintWindow cannot read the GPU-composed output.
    Caller should fall back to a foreground-based capture in that case.

    Uses PW_RENDERFULLCONTENT (0x2) so DWM-composed and partially obscured
    windows still capture properly. mstsc (Remote Desktop Connection) works
    reliably with this flag.

    Implementation note: deliberately uses ctypes directly rather than
    win32ui to keep the dependency surface aligned with the rest of the
    module (win32gui only). Resources are released even on exception via
    try/finally.
    """
    if not _WIN32_AVAILABLE:
        raise RuntimeError("hwnd capture requires pywin32 (win32gui).")
    _ensure_dpi_aware()

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = win32gui.GetWindowRect(hwnd)
    x1, y1, x2, y2 = rect
    width = x2 - x1
    height = y2 - y1
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"hwnd {hwnd} has invalid rect {rect}. Window may be minimised."
        )

    # ── BITMAPINFO struct for GetDIBits ──────────────────────────────────────
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    PW_RENDERFULLCONTENT = 0x00000002
    BI_RGB = 0
    DIB_RGB_COLORS = 0

    hdc_window = user32.GetWindowDC(hwnd)
    if not hdc_window:
        raise RuntimeError(f"GetWindowDC failed for hwnd {hwnd}")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    old_obj = gdi32.SelectObject(hdc_mem, hbitmap)

    try:
        # PrintWindow returns BOOL (non-zero on success).
        ok = user32.PrintWindow(
            wintypes.HWND(hwnd), wintypes.HDC(hdc_mem), ctypes.c_uint(PW_RENDERFULLCONTENT),
        )
        if not ok:
            raise RuntimeError(
                f"PrintWindow returned 0 for hwnd {hwnd}. Window may be "
                "elevated/protected or hwnd is invalid."
            )

        # Read pixels back via GetDIBits — top-down 32-bit BGRA.
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height          # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = 0

        buf_size = width * height * 4
        buf = (ctypes.c_ubyte * buf_size)()

        scanlines = gdi32.GetDIBits(
            wintypes.HDC(hdc_mem),
            wintypes.HBITMAP(hbitmap),
            ctypes.c_uint(0),
            ctypes.c_uint(height),
            buf,
            ctypes.byref(bmi),
            ctypes.c_uint(DIB_RGB_COLORS),
        )
        if scanlines == 0:
            raise RuntimeError(f"GetDIBits returned 0 for hwnd {hwnd}")

        # BGRA bytes → RGB image; ignore alpha (mstsc / most win32 windows
        # return alpha=0 which would look fully transparent if preserved).
        img = Image.frombytes("RGB", (width, height), bytes(buf), "raw", "BGRX")
        img.save(out_path, "PNG", optimize=True)

        # Black-frame heuristic — sample at most ~512 evenly-spaced pixels
        # from the buffer; if every sampled (B, G, R) byte is 0, treat as
        # PrintWindow GPU miss and signal caller to fall back.
        sample_step = max(1, buf_size // (512 * 4)) * 4
        looked_black = True
        for i in range(0, buf_size, sample_step):
            if buf[i] != 0 or buf[i + 1] != 0 or buf[i + 2] != 0:
                looked_black = False
                break
    finally:
        gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)

    try:
        size = os.path.getsize(out_path)
    except OSError:
        size = 0
    return (x1, y1, x2, y2), size, looked_black


def _screenshot_region(out_path: str, region: str = "foreground",
                       monitor: int = 0,
                       hwnd: Optional[int] = None,
                       ) -> Tuple[Tuple[int, int, int, int], int]:
    """Capture *region* to *out_path* as PNG.

    Returns ((x1, y1, x2, y2), bytes_written).

      hwnd=<int>           target a specific window via PrintWindow,
                           bypassing foreground state. Falls back to mss
                           capture of the window's rect if PrintWindow
                           returns a black frame (GPU-accelerated targets).
      region="foreground"  current foreground window's bounding rect
      region="fullscreen"  whole virtual screen (or specific monitor index)

    Raises RuntimeError when neither mss nor PIL.ImageGrab is available.
    """
    if not _MSS_AVAILABLE:
        raise RuntimeError(
            "mss is not installed. Run:\n"
            "  pip install mss\n"
            "Required for desktop screenshot capture.\n"
            "IMPORTANT: after installing, RESTART HandQ — the bridge "
            "caches the import result at startup, so a mid-session "
            "pip install will not take effect until restart."
        )
    _ensure_dpi_aware()

    # ── hwnd path (PrintWindow with mss fallback) ────────────────────────────
    if hwnd:
        try:
            rect, size, looked_black = _screenshot_hwnd_via_print_window(out_path, int(hwnd))
        except Exception as exc:
            # PrintWindow refused outright (elevated process, invalid hwnd) —
            # fall through to the mss path which will at least capture
            # whatever pixels are visible at the rect.
            looked_black = True
            rect = None
            _logger = get_logger()
            _logger.warning(
                f"PrintWindow capture failed for hwnd={hwnd}: {exc}; "
                f"falling back to rect-based mss capture",
                component="DesktopTool",
            )

        if not looked_black and rect is not None:
            return rect, size

        # Fallback: capture the hwnd's rect via mss (no foreground change).
        # The rect may show whatever is on top at that screen location, but
        # at least it's deterministic about coordinates — better than the
        # current foreground guesswork.
        if _WIN32_AVAILABLE:
            r = win32gui.GetWindowRect(int(hwnd))
            if r[2] - r[0] > 0 and r[3] - r[1] > 0:
                rect = (r[0], r[1], r[2], r[3])
        if rect is None:
            raise RuntimeError(
                f"hwnd {hwnd} capture failed and rect could not be resolved."
            )
        # fall through to mss capture path below using the resolved rect.
    else:
        rect = None  # populated by region branch below

    rect_resolved: Tuple[int, int, int, int]
    if rect is not None:
        rect_resolved = rect
    elif region == "foreground":
        info = _foreground_window_info()
        if not info or not info.get("rect"):
            raise RuntimeError(
                "Cannot determine foreground window. Make sure a window is "
                "active before calling screenshot region='foreground'."
            )
        x1, y1, x2, y2 = info["rect"]
        # Clamp non-positive dimensions (minimised windows can return -32000).
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            raise RuntimeError(
                f"Foreground window has invalid rect {info['rect']}. "
                "It may be minimised — restore it before screenshotting."
            )
        rect_resolved = (x1, y1, x2, y2)
    elif region == "fullscreen":
        # mss monitor[0] = "all monitors" virtual rect; 1+ = each monitor.
        with mss.mss() as sct:
            mon = sct.monitors[monitor if monitor < len(sct.monitors) else 0]
        rect_resolved = (mon["left"], mon["top"],
                         mon["left"] + mon["width"], mon["top"] + mon["height"])
    else:
        raise RuntimeError(
            f"unknown region {region!r}. Use 'foreground' or 'fullscreen'."
        )

    with mss.mss() as sct:
        bbox = {"left": rect_resolved[0], "top": rect_resolved[1],
                "width": rect_resolved[2] - rect_resolved[0],
                "height": rect_resolved[3] - rect_resolved[1]}
        raw = sct.grab(bbox)
        # mss returns a `ScreenShot` object; convert to PNG via PIL so we
        # share the encoder with the rest of the pipeline (vision_client
        # already requires Pillow).
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        img.save(out_path, "PNG", optimize=True)
    try:
        size = os.path.getsize(out_path)
    except OSError:
        size = 0
    return rect_resolved, size


# ── UIA accessibility-tree enumeration ───────────────────────────────────────
#
# pywinauto's UIA backend gives us the same data Windows screen readers see:
# every interactable control has an accessibility "name" + "automation_id"
# even when no visible text label is rendered. This is the primary win path
# for snapshot — it handles iconless toolbar buttons (gear / refresh / close
# / etc.) that OCR cannot read because there is nothing to OCR.
#
# Why pywinauto and not raw uiautomation: pywinauto wraps the COM API with a
# Python-friendly facade that handles edge cases (wrong process integrity
# levels, hidden top-level frames, etc.) for free. Cost is one dependency
# already in requirements.txt.
#
# Failure modes:
#   * Win32-only legacy apps (some old MFC dialogs) advertise IAccessible
#     not UIA; pywinauto's UIA backend can still see them but with reduced
#     attribute coverage. snapshot_uia returns whatever it gets.
#   * Some apps run at a higher integrity level than HandQ (UAC-elevated
#     installer dialogs, Task Manager). UIA cross-IL enumeration requires
#     equal-or-higher integrity — snapshot_uia returns [] and the caller
#     falls back to OCR.
#   * Electron / custom-rendered apps may not implement UIA at all.
#     Same fallback path.

# Control types we treat as "interactable" — the snapshot's purpose is to
# tell the LLM what it can click / type into, not dump every leaf node.
_UIA_INTERACTABLE_TYPES: Tuple[str, ...] = (
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink",
    "ListItem", "MenuItem", "RadioButton", "TabItem", "TreeItem",
    "Slider", "SplitButton", "Spinner",
)

# Hard cap on snapshot element count. 100 is large enough for any real
# foreground window (the OneNote File→New panel hits ~40, deep IDEs hit
# ~80) while keeping the JSON payload bounded.
_UIA_MAX_ELEMENTS: int = 100


def _suggest_uia_selector(name: str, automation_id: str, control_type: str) -> str:
    """Pick the most stable selector hint for a follow-up pywinauto call.

    Priority: automation_id > name > control_type. The agent uses this in
    a shell call like ``win.child_window(auto_id='SaveButton').click()``.
    """
    if automation_id:
        return f"auto_id={automation_id!r}"
    if name:
        return f"title={name!r}"
    return f"control_type={control_type!r}"


def _uia_enumerate(hwnd: int) -> List[Dict[str, Any]]:
    """Walk the UIA tree of the foreground window and return interactable
    elements as plain dicts (so the result is asyncio-safe to ship).

    Runs synchronously — caller must invoke through ``run_in_executor``.
    Returns ``[]`` on any failure (UIA refused / cross-IL / app exited);
    caller treats empty list as "fall back to screenshot+OCR".
    """
    if not hwnd:
        return []
    try:
        app = pywinauto.Application(backend="uia").connect(handle=hwnd, timeout=2)
        win = app.window(handle=hwnd)
    except Exception:
        return []

    elements: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        descendants = win.descendants()
    except Exception:
        return []

    for ctrl in descendants:
        if len(elements) >= _UIA_MAX_ELEMENTS:
            break
        try:
            ei = ctrl.element_info
            ctrl_type = (ei.control_type or "").strip()
            if ctrl_type not in _UIA_INTERACTABLE_TYPES:
                continue
            # Skip invisible / disabled. Some controls fail .is_visible()
            # on cross-thread access; treat that as "skip" not "include".
            try:
                if not ctrl.is_visible():
                    continue
                # is_enabled is allowed to be False (e.g. greyed-out menu
                # items) — we still report them so the LLM knows they
                # exist but understands they cannot be clicked. Use the
                # 'enabled' field on the dict.
                enabled = bool(ctrl.is_enabled())
            except Exception:
                continue
            name = (ei.name or "").strip()
            auto_id = (ei.automation_id or "").strip()
            # Need at least one identifier — purely anonymous nodes
            # are decoration / layout containers.
            if not name and not auto_id:
                continue
            r = ei.rectangle  # has .left .top .right .bottom
            if r is None:
                continue
            x1, y1, x2, y2 = int(r.left), int(r.top), int(r.right), int(r.bottom)
            if x2 - x1 <= 0 or y2 - y1 <= 0:
                continue
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            # Dedup on (name, type, centre): same control discovered via
            # multiple parent paths is common in UIA.
            key = (name, ctrl_type, cx, cy)
            if key in seen:
                continue
            seen.add(key)
            elements.append({
                "role": ctrl_type,
                "text": name,
                "automation_id": auto_id,
                "x": cx, "y": cy,
                "rect": [x1, y1, x2, y2],
                "enabled": enabled,
                "selector": _suggest_uia_selector(name, auto_id, ctrl_type),
            })
        except Exception:
            continue
    return elements


def _format_snapshot_summary(
    elements: List[Dict[str, Any]],
    window_title: str,
    source: str,
) -> str:
    """Render a compact, role-grouped Markdown summary the LLM can scan
    in one read.

    Token economy: this is the ONLY representation of the snapshot the
    LLM sees — the structured `elements` list is dropped from the
    ToolResult output to save context tokens. Each element line is
    ~12-15 tokens vs ~150 tokens for the equivalent JSON dict, so a
    100-element snapshot ships ~1.5K tokens instead of ~15K.

    Output format (one element per line, role-grouped):

        window: <title> (source=uia, n=N)

        BUTTONS (k):
          - 'OK' (820,412) → title='OK'
          - 'Cancel' (720,412) → title='Cancel' [disabled]
        EDITS (k):
          - 'Notebook Name' (722,233) → auto_id='NotebookNameEdit'

    The (x,y) and selector on each line are everything the agent needs
    to drive a follow-up click_at / pywinauto call — no roundtrip back
    to a structured form is required.
    """
    n = len(elements)
    header = f"window: {window_title!r} (source={source}, n={n})"
    if not elements:
        return header + "\n  (no interactable elements found)"

    lines: List[str] = [header, ""]
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    for e in elements:
        by_role.setdefault(e.get("role") or "Other", []).append(e)
    # Plural pluralisation: append S unless role already ends in S.
    def _heading(role: str, count: int) -> str:
        upper = role.upper()
        plural = upper if upper.endswith("S") else upper + "S"
        return f"{plural} ({count}):"
    for role in sorted(by_role.keys()):
        items = by_role[role]
        lines.append(_heading(role, len(items)))
        for e in items:
            text = (e.get("text") or "(no name)").replace("\n", " ").strip()
            if len(text) > 60:
                text = text[:60] + "..."
            disabled = " [disabled]" if e.get("enabled") is False else ""
            sel = e.get("selector", "?")
            lines.append(f"  - {text!r} ({e['x']},{e['y']}) → {sel}{disabled}")
    return "\n".join(lines)


# ── UIA pattern helpers ──────────────────────────────────────────────────────
#
# Called synchronously via run_in_executor. Return None on success, an error
# string on any failure so callers fall back to pyautogui without raising.

def _uia_invoke_at_point(x: int, y: int) -> Optional[str]:
    """Try UIA InvokePattern / TogglePattern / SelectionItemPattern at (x, y).

    Walks all three patterns so buttons (Invoke), checkboxes (Toggle), and
    list/tab/tree items (SelectionItem) are covered without the caller needing
    to know the control type in advance.
    """
    try:
        elem = Desktop(backend="uia").from_point(x, y)
    except Exception as exc:
        return f"UIA from_point: {exc}"
    if elem is None:
        return "no UIA element at coordinate"
    errors: List[str] = []
    for method in ("invoke", "toggle", "select"):
        try:
            getattr(elem, method)()
            return None  # success
        except Exception as exc:
            errors.append(f"{method}: {exc}")
    ctrl_type = ""
    try:
        ctrl_type = elem.element_info.control_type or ""
    except Exception:
        pass
    return f"UIA patterns exhausted for {ctrl_type!r}: {'; '.join(errors)}"


def _uia_set_focused_value(text: str) -> Optional[str]:
    """Set the focused edit control's value via ValuePattern.SetValue.

    Replaces the entire field content — semantically different from
    pyautogui.typewrite (which appends). Callers surface this distinction
    via the 'input_source' key in ToolResult.output.
    """
    try:
        elem = Desktop(backend="uia").get_focus()
        if elem is None:
            return "no focused element"
        elem.set_edit_text(text)
        return None
    except Exception as exc:
        return f"UIA set_edit_text: {exc}"


# ── DesktopTool ──────────────────────────────────────────────────────────────


class DesktopTool(BaseTool):
    """Single tool exposing the desktop CUA action set. Windows only."""

    is_read_only = False
    is_concurrency_safe = False

    parameter_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "screenshot",
                    "snapshot",
                    "list_windows",
                    "find_element",
                    "find_and_click",
                    "hover_at",
                    "click_at",
                    "type_text",
                    "drag",
                    "scroll",
                    "hotkey",
                    "key_press",
                ],
                "description": (
                    "Desktop action to perform. Mouse / keyboard actions "
                    "are guarded by the foreground-window sensitive filter "
                    "(banking, password managers) and refuse outright when "
                    "active window matches."
                ),
            },
            "region": {
                "type": "string",
                "enum": ["foreground", "fullscreen"],
                "description": (
                    "[screenshot / find_element] Capture region. "
                    "'foreground' (default) captures only the active window — "
                    "smaller payload, more focused for vision. 'fullscreen' "
                    "captures all monitors."
                ),
            },
            "monitor": {
                "type": "integer",
                "description": (
                    "[screenshot region='fullscreen'] 0 = all monitors "
                    "stitched (default); 1..N = a specific monitor."
                ),
            },
            "hwnd": {
                "type": "integer",
                "description": (
                    "[screenshot] Target a specific window by hwnd, "
                    "regardless of foreground state. Captures via "
                    "PrintWindow under the hood — eliminates the focus-"
                    "thrashing class of failures (Teams / VSCode stealing "
                    "focus mid-screenshot). Falls back to a rect-based mss "
                    "capture if PrintWindow returns black (some hardware-"
                    "accelerated targets). Get hwnd from list_windows."
                ),
            },
            "with_ocr": {
                "type": "boolean",
                "description": (
                    "[screenshot] When true, run RapidOCR on the capture "
                    "and include a 'text_regions' list in the output: "
                    "every visible text region with its SCREEN-space "
                    "centre (x, y) ready to feed straight to click_at. "
                    "Use this on the FIRST inspection of a text-heavy UI "
                    "to skip several follow-up find_element round-trips: "
                    "one screenshot + one LLM read = full picture of the "
                    "screen's labelled controls. Adds ~700 ms to the "
                    "screenshot but saves ~1 s per find_element it "
                    "replaces. Default false."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "[screenshot] Output file path. Absolute paths used as-is "
                    "(use this to drop a long-term keeper into the session "
                    "working_directory). Relative or empty paths are stored "
                    "in the auto-cleaned 'task' tier under "
                    "%USERPROFILE%\\HandQ\\desktop_shots\\task\\."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "[find_element] Natural-language description of what to "
                    "locate. The tool runs OCR + fuzzy text match first; on "
                    "miss it can fall back to LLM vision when "
                    "vision_fallback=true. Examples: 'OK button', 'the "
                    "Settings icon in the toolbar', '保存 按钮'."
                ),
            },
            "vision_fallback": {
                "type": "boolean",
                "description": (
                    "[find_element] If OCR cannot match the description "
                    "with sufficient confidence, fall back to a single "
                    "vision LLM call. Default: true. Set false when you "
                    "know the target text is plain ASCII / CJK already on "
                    "screen, to avoid the 5-7 s vision overhead."
                ),
            },
            "fuzzy_threshold": {
                "type": "integer",
                "description": (
                    "[find_element] rapidfuzz token_set_ratio threshold "
                    "(0-100) for OCR text matching. Default 70. Lower = "
                    "more permissive but riskier; higher = stricter."
                ),
            },
            "x": {
                "type": "integer",
                "description": (
                    "[click_at / scroll] X coordinate in physical screen "
                    "pixels (0 = leftmost). For find_element + click "
                    "workflow, use the x value from find_element's output."
                ),
            },
            "y": {
                "type": "integer",
                "description": "[click_at / scroll] Y coordinate (0 = top).",
            },
            "from_x": {"type": "integer", "description": "[drag] Start X."},
            "from_y": {"type": "integer", "description": "[drag] Start Y."},
            "to_x":   {"type": "integer", "description": "[drag] End X."},
            "to_y":   {"type": "integer", "description": "[drag] End Y."},
            "duration": {
                "type": "number",
                "description": (
                    "[drag] Animation duration in seconds. Default 0.5. "
                    "0 = teleport (some targets don't accept that as a "
                    "real drag — use ≥ 0.2)."
                ),
            },
            "hover_seconds": {
                "type": "number",
                "description": (
                    "[hover_at] How long to leave the cursor at (x, y) "
                    "before reading state. Windows tooltips appear after "
                    "~500 ms; default 0.8 s leaves headroom. Cap at 5 s; "
                    "longer values are usually a sign you wanted "
                    "find_element / snapshot instead."
                ),
            },
            "capture_after_hover": {
                "type": "boolean",
                "description": (
                    "[hover_at] If true (default), screenshot + OCR a 200×100 "
                    "px window around the cursor after the hover delay. "
                    "Returns the OCR text in 'nearby_text' so you can read "
                    "the tooltip that just appeared. Set false when you "
                    "only want to position the cursor (rare)."
                ),
            },
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "[click_at] Mouse button. Default 'left'.",
            },
            "double": {
                "type": "boolean",
                "description": "[click_at] Double-click. Default false.",
            },
            "text": {
                "type": "string",
                "description": (
                    "[type_text] Text to type. Capped at 4000 characters; "
                    "for longer payloads paste via clipboard separately. "
                    "Refuses outright when the foreground window matches "
                    "the sensitive patterns (password managers / banking)."
                ),
            },
            "interval": {
                "type": "number",
                "description": (
                    "[type_text] Seconds between keystrokes. Default 0.02. "
                    "Increase to ~0.05 for sites that throttle / drop fast "
                    "input."
                ),
            },
            "dy": {
                "type": "integer",
                "description": (
                    "[scroll] Wheel clicks to scroll. Positive scrolls up, "
                    "negative scrolls down (matches pyautogui convention)."
                ),
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "[hotkey] List of keys held simultaneously, e.g. "
                    "['ctrl','c'] for copy or ['alt','tab']."
                ),
            },
            "key": {
                "type": "string",
                "description": (
                    "[key_press] Single key to press, e.g. 'enter', 'esc', "
                    "'f5', 'pagedown'."
                ),
            },
            "use_uia_pattern": {
                "type": "boolean",
                "description": (
                    "[click_at / type_text / find_and_click] Try UIA "
                    "InvokePattern / ValuePattern before falling back to "
                    "pyautogui mouse/keyboard simulation. Default true. "
                    "Ignored for right/middle clicks, double-clicks, drag, "
                    "scroll, hotkey, key_press — those always use pyautogui. "
                    "Set false for apps that don't expose UIA (custom-rendered "
                    "apps, games) or when you need exact coordinate-level input."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, ctx=None) -> None:
        super().__init__("desktop", ctx=ctx)
        # Per-session desktop state — takeover machine + IM + snapshot cache +
        # OCR prewarm guard + lazy ScreenshotStore. When ctx is supplied the
        # state lives on the SessionContext (dies with the session); when
        # ctx is None (test fixture) we instantiate a default. ``self.state``
        # backs every internal access — module-level helpers are only exposed
        # for outside callers reaching the takeover state machine directly.
        self.state: DesktopState = (
            ctx.desktop_state if ctx is not None else DesktopState()
        )
        self.logger = get_logger()

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        start = time.time()
        params: Dict[str, Any] = {"action": action, **kwargs}

        if sys.platform != "win32":
            return self._error(
                params, start,
                "desktop_tool is currently Windows-only. Linux/macOS "
                "support requires equivalent input/screenshot adapters.",
            )

        # Hide the ~600ms RapidOCR cold-start: first execute() call kicks
        # off a background load. By the time the agent gets around to
        # find_element (typically after a screenshot or list_windows),
        # the engine is warm. One-shot — see DesktopState.prewarm_ocr_if_needed.
        self.state.prewarm_ocr_if_needed()

        if not action:
            return self._error(params, start, "desktop tool requires 'action'.")

        dispatch: Dict[str, Any] = {
            "screenshot":   self._action_screenshot,
            "snapshot":     self._action_snapshot,
            "list_windows": self._action_list_windows,
            "find_element": self._action_find_element,
            "find_and_click": self._action_find_and_click,
            "hover_at":     self._action_hover_at,
            "click_at":     self._action_click_at,
            "type_text":    self._action_type_text,
            "drag":         self._action_drag,
            "scroll":       self._action_scroll,
            "hotkey":       self._action_hotkey,
            "key_press":    self._action_key_press,
        }
        handler = dispatch.get(action)
        if handler is None:
            return self._error(
                params, start,
                f"Unknown desktop action: {action!r}. "
                f"Valid: {', '.join(dispatch)}",
            )

        # Cross-session lock ordering (deadlock fix):
        # Claim the process-wide desktop OWNERSHIP lock BEFORE taking the
        # per-action ``_desktop_lock``. Ownership is held across a whole
        # session's takeover, while ``_desktop_lock`` is released between
        # individual actions. If we acquired them in the other order, two
        # sessions could deadlock: session A owns the ownership lock (mid
        # task) and waits for ``_desktop_lock``; session B holds
        # ``_desktop_lock`` and waits for ownership. Acquiring ownership
        # first means a session waiting for ownership never holds
        # ``_desktop_lock``, so no cycle can form. Read-only actions are not
        # in ``_INPUT_ACTIONS`` and never touch the ownership lock.
        if action in _INPUT_ACTIONS:
            # A sensitive-window refusal must not grab ownership, so run the
            # side-effect-free sensitive check before acquiring. The per-handler
            # ``_input_action_guard`` re-checks (and flips takeover state); its
            # own ``acquire_global_takeover`` is then an idempotent no-op.
            refusal = self._sensitive_guard()
            if refusal:
                return self._error(params, start, refusal)
            await self.state.acquire_global_takeover()

        # Mouse and keyboard are global — actions across agents and steps
        # all queue here.
        async with _desktop_lock:
            try:
                return await handler(params, start, **kwargs)
            except Exception as exc:
                self.logger.error(
                    f"desktop action {action!r} raised: {exc}",
                    component="DesktopTool", exc_info=True,
                )
                return self._error(
                    params, start,
                    f"desktop action {action!r} failed: {exc}",
                )

    # ── screenshot ────────────────────────────────────────────────────────────

    async def _action_screenshot(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        region = (kwargs.get("region") or "foreground").lower()
        monitor = int(kwargs.get("monitor") or 0)
        path_arg: Optional[str] = kwargs.get("path")
        with_ocr = bool(kwargs.get("with_ocr", False))
        try:
            hwnd_arg = int(kwargs["hwnd"]) if kwargs.get("hwnd") else None
        except (TypeError, ValueError):
            hwnd_arg = None

        # Sensitive window guard.
        # - hwnd path: check the title of the SPECIFIC target hwnd, not the
        #   foreground; capturing a non-foreground sensitive window is just
        #   as much of a leak as foregrounding it.
        # - foreground path: legacy behaviour — check current foreground.
        if hwnd_arg:
            if _WIN32_AVAILABLE:
                try:
                    target_title = win32gui.GetWindowText(hwnd_arg) or ""
                    try:
                        _, target_pid = win32process.GetWindowThreadProcessId(hwnd_arg)
                    except Exception:
                        target_pid = 0
                    target_proc = _process_name_for_pid(target_pid)
                    if _is_sensitive_window(target_title, target_proc):
                        return self._error(
                            params, start,
                            f"REFUSED: target hwnd {hwnd_arg} ({target_title!r}) "
                            "matches the sensitive_window_patterns list. Capture "
                            "of password managers / banking apps is blocked even "
                            "when not foregrounded.",
                        )
                except Exception:
                    pass
        elif region == "foreground":
            info = _foreground_window_info()
            if info and _is_sensitive_window(info.get("title", ""), info.get("process_name", "")):
                return self._error(
                    params, start,
                    f"REFUSED: foreground window {info.get('title')!r} matches "
                    "the sensitive_window_patterns list (password manager / "
                    "banking app). Switch focus to a non-sensitive window "
                    "and retry, or capture region='fullscreen' if you are "
                    "sure you need it.",
                )

        store = self.state.get_store()
        if path_arg and os.path.isabs(path_arg):
            out_path = path_arg
            wrote_to_store = False
        else:
            base_dir = store.subdir("task")
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            fname = path_arg or f"desktop-{ts}.png"
            out_path = os.path.join(base_dir, fname)
            wrote_to_store = True
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        try:
            rect, size = await asyncio.get_event_loop().run_in_executor(
                None, _screenshot_region, out_path, region, monitor, hwnd_arg,
            )
        except Exception as exc:
            return self._error(params, start, f"screenshot: {exc}")

        if wrote_to_store:
            store.enforce_retention("task")

        output: Dict[str, Any] = {
            "path": out_path,
            "region": region if not hwnd_arg else "hwnd",
            "rect": list(rect),
            "bytes": size,
        }
        if hwnd_arg:
            output["hwnd"] = hwnd_arg

        # Optional OCR pass. The big win is letting one screenshot replace
        # several find_element round-trips on text-heavy UIs — the agent
        # gets every visible label + its screen-space click coordinates
        # in a single response. Capped to keep the LLM context bounded
        # (~80 regions covers a packed Office ribbon; rare overflow is
        # flagged so the agent knows to use find_element for off-list
        # targets).
        if with_ocr:
            from ..infrastructure.vision import get_local_ocr
            origin_x, origin_y = int(rect[0]), int(rect[1])
            try:
                ocr = get_local_ocr()
                ocr_result = await asyncio.get_event_loop().run_in_executor(
                    None, ocr.recognize, out_path,
                )
            except Exception as exc:
                self.logger.warning(
                    f"screenshot with_ocr=true: OCR failed: {exc}",
                    component="DesktopTool",
                )
                output["ocr_error"] = str(exc)
            else:
                if ocr_result.ok and ocr_result.boxes:
                    cap = 80
                    regions: List[Dict[str, Any]] = []
                    for box in ocr_result.boxes[:cap]:
                        cx_img, cy_img = box.center
                        regions.append({
                            "text": box.text,
                            "x": origin_x + cx_img,
                            "y": origin_y + cy_img,
                            "confidence": round(float(box.confidence), 2),
                        })
                    output["text_regions"] = regions
                    if len(ocr_result.boxes) > cap:
                        output["text_regions_truncated"] = (
                            f"showing {cap} of {len(ocr_result.boxes)} "
                            "regions; use find_element for off-list targets"
                        )
                    output["ocr_elapsed_ms"] = ocr_result.elapsed_ms
                else:
                    output["text_regions"] = []
                    if ocr_result.error:
                        output["ocr_error"] = ocr_result.error

        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── list_windows ──────────────────────────────────────────────────────────

    async def _action_list_windows(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _WIN32_AVAILABLE:
            return self._error(
                params, start,
                "list_windows requires pywin32. Run: pip install pywin32",
            )
        windows = _enumerate_visible_windows()
        return ToolResult(
            success=True,
            output={"windows": windows, "count": len(windows)},
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── snapshot ──────────────────────────────────────────────────────────────
    #
    # Structured listing of every interactable control in the foreground
    # window. The decision-loop equivalent of browser_tool's snapshot.
    # Two paths:
    #
    #   1. UIA accessibility tree (preferred). Catches iconless controls
    #      (gear / refresh / close X) because Windows screen readers see
    #      the same name attribute the agent does. Output payload is
    #      bounded (cap=100) and structured, NOT a raw OCR text dump —
    #      so dropping it into the LLM context costs O(elements) tokens
    #      not O(visible characters).
    #
    #   2. screenshot + OCR fallback. Used when UIA returns nothing —
    #      e.g. cross-IL elevated targets, custom-rendered Electron,
    #      games. Same output shape; the LLM does not see the difference.
    #
    # Why this exists: the OneNote diagnostic run (2026-05-24) burned
    # 60 iterations on what should have been ~10. Root cause: every
    # screenshot's OCR text JSON went back into context; the LLM had
    # to re-derive "where is the New Notebook button" from a flat list
    # of 25 OCR boxes per screenshot. snapshot replaces that loop with
    # one structured call.

    async def _action_snapshot(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        # Sensitive guard runs against the foreground window — same as
        # screenshot. Snapshot is read-only so we don't fire takeover.
        info = _foreground_window_info()
        if not info:
            return self._error(
                params, start,
                "snapshot: cannot determine foreground window. Make sure a "
                "window is active and not minimised.",
            )
        if _is_sensitive_window(info.get("title", ""), info.get("process_name", "")):
            return self._error(
                params, start,
                f"REFUSED: foreground window {info.get('title')!r} matches "
                "the sensitive_window_patterns list.",
            )

        # ── Cache check ────────────────────────────────────────────────────
        # _capture_state_before is also our cache-validity probe. Cheap (~5 ms).
        hwnd = int(info.get("hwnd", 0) or 0)
        current_state = _capture_state_before()
        current_sig = _snapshot_sig(current_state)
        now = time.time()
        cached = self.state.snapshot_cache.get(hwnd)
        if (
            cached is not None
            and cached.get("sig") == current_sig
            and now - float(cached.get("ts", 0.0)) < _SNAPSHOT_CACHE_TTL_S
        ):
            cached_output = dict(cached.get("output") or {})
            cached_output["cached"] = True
            cached_output["cache_age_ms"] = int((now - float(cached["ts"])) * 1000)
            return ToolResult(
                success=True,
                output=cached_output,
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )
        # Sig mismatch on this hwnd → drop the entry now to keep the dict
        # small. (The whole-cache nuke runs from input actions; this path
        # only fires when the cache went stale without an input action,
        # e.g. user moved the mouse manually.)
        if cached is not None and cached.get("sig") != current_sig:
            self.state.snapshot_cache.pop(hwnd, None)

        loop = asyncio.get_event_loop()
        # 1. Try UIA tree.
        try:
            elements = await loop.run_in_executor(
                None, _uia_enumerate, hwnd
            )
        except Exception as exc:
            self.logger.warning(
                f"snapshot: UIA enumerate raised: {exc}",
                component="DesktopTool",
            )
            elements = []

        source = "uia"
        screenshot_path: Optional[str] = None

        # 2. Fallback — screenshot + OCR. Only when UIA found nothing.
        if not elements:
            store = self.state.get_store()
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            screenshot_path = os.path.join(
                store.subdir("ephemeral"), f"snap-{ts}.png"
            )
            try:
                rect, _bytes = await loop.run_in_executor(
                    None, _screenshot_region, screenshot_path, "foreground", 0,
                )
            except Exception as exc:
                return self._error(params, start, f"snapshot capture: {exc}")
            store.enforce_retention("ephemeral")
            origin_x, origin_y = int(rect[0]), int(rect[1])

            from ..infrastructure.vision import get_local_ocr
            try:
                ocr = get_local_ocr()
                ocr_result = await loop.run_in_executor(
                    None, ocr.recognize, screenshot_path,
                )
            except Exception as exc:
                return self._error(params, start, f"snapshot OCR: {exc}")

            if ocr_result.ok:
                for box in ocr_result.boxes:
                    text = (box.text or "").strip()
                    if not text:
                        continue
                    cx, cy = box.center
                    elements.append({
                        "role": "TextRegion",
                        "text": text,
                        "automation_id": "",
                        "x": origin_x + cx,
                        "y": origin_y + cy,
                        "rect": [
                            origin_x + box.bbox[0], origin_y + box.bbox[1],
                            origin_x + box.bbox[2], origin_y + box.bbox[3],
                        ],
                        "enabled": True,
                        "selector": f"text={text!r}",
                        "confidence": box.confidence,
                    })
            source = "ocr"

        # Cap defensively (UIA cap is 100; OCR can blow past on busy pages).
        if len(elements) > _UIA_MAX_ELEMENTS:
            elements = elements[:_UIA_MAX_ELEMENTS]
            truncated = True
        else:
            truncated = False

        summary = _format_snapshot_summary(
            elements, info.get("title", ""), source,
        )

        # MD `summary` is the ONLY representation we ship; we deliberately
        # drop the structured `elements` list and the window `rect` to
        # save context tokens (snapshot output goes back into every
        # subsequent agent decision via observation history). 100
        # elements: ~1.5K tokens of summary vs ~15K tokens of JSON.
        out: Dict[str, Any] = {
            "window_title":    info.get("title", ""),
            "hwnd":            info.get("hwnd"),
            "source":          source,
            "element_count":   len(elements),
            "truncated":       truncated,
            "summary":         summary,
        }
        if screenshot_path:
            out["screenshot"] = screenshot_path

        # Populate cache. Store a copy of `out` BEFORE the per-call
        # "cached"/"cache_age_ms" markers are added so future hits can
        # inject those markers freshly without compounding.
        self.state.snapshot_cache[hwnd] = {
            "output": dict(out),
            "sig":    current_sig,
            "ts":     time.time(),
        }

        return ToolResult(
            success=True,
            output=out,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── hover_at ──────────────────────────────────────────────────────────────
    #
    # Mimics the human "hover for a moment to see the tooltip" gesture. Use
    # for controls that snapshot can't name (UIA missing + no OCR text).
    # Move cursor → wait ~800 ms (default Windows tooltip latency) →
    # screenshot a region around the cursor → OCR → return the text.

    async def _action_hover_at(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        # hover is borderline input — it moves the cursor but does not click.
        # Run it through the takeover gate so the indicator + revoke apply,
        # matching how a tooltip-spam loop would feel to the user.
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        try:
            x = int(kwargs["x"]); y = int(kwargs["y"])
        except (KeyError, TypeError, ValueError):
            return self._error(params, start, "hover_at requires integer 'x' and 'y'.")
        try:
            hover_seconds = float(kwargs.get("hover_seconds") or 0.8)
        except (TypeError, ValueError):
            hover_seconds = 0.8
        hover_seconds = max(0.1, min(hover_seconds, 5.0))
        capture_after = bool(kwargs.get("capture_after_hover", True))

        _ensure_dpi_aware()
        loop = asyncio.get_event_loop()

        try:
            await loop.run_in_executor(None, lambda: pyautogui.moveTo(x, y))
        except pyautogui.FailSafeException:
            return self._error(
                params, start,
                "hover_at: PyAutoGUI failsafe triggered (mouse hit corner).",
            )
        except Exception as exc:
            return self._error(params, start, f"hover_at moveTo: {exc}")

        await asyncio.sleep(hover_seconds)

        out: Dict[str, Any] = {
            "x": x, "y": y, "hover_seconds": hover_seconds,
        }

        if capture_after:
            store = self.state.get_store()
            ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            shot_path = os.path.join(store.subdir("ephemeral"), f"hover-{ts}.png")
            # Fullscreen capture so a tooltip that overflows the foreground
            # window edge (common for taskbar / system tray icons) still
            # gets caught.
            try:
                _rect, _bytes = await loop.run_in_executor(
                    None, _screenshot_region, shot_path, "fullscreen", 0,
                )
            except Exception as exc:
                return self._error(params, start, f"hover_at capture: {exc}")
            store.enforce_retention("ephemeral")

            from ..infrastructure.vision import get_local_ocr
            try:
                ocr = get_local_ocr()
                result = await loop.run_in_executor(
                    None, ocr.recognize, shot_path,
                )
            except Exception as exc:
                self.logger.warning(
                    f"hover_at OCR: {exc}", component="DesktopTool",
                )
                result = None

            nearby: List[Dict[str, Any]] = []
            if result is not None and result.ok:
                # Tooltips appear within ~200 px right / ~100 px below the
                # cursor on Windows. Generous box catches both that and the
                # typical "tooltip above" placement near screen-bottom.
                R_X = 250
                R_Y = 120
                for box in result.boxes:
                    cx, cy = box.center
                    if abs(cx - x) <= R_X and abs(cy - y) <= R_Y:
                        nearby.append({
                            "text":       box.text,
                            "bbox":       list(box.bbox),
                            "confidence": box.confidence,
                            "dx":         cx - x,
                            "dy":         cy - y,
                        })
                # Sort closest-first so the most likely tooltip lands on top.
                nearby.sort(key=lambda b: abs(b["dx"]) + abs(b["dy"]))
            out["nearby_text"] = nearby
            out["screenshot"] = shot_path

        return ToolResult(
            success=True,
            output=out,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── find_element ──────────────────────────────────────────────────────────

    async def _action_find_element(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        description: str = (kwargs.get("description") or "").strip()
        if not description:
            return self._error(
                params, start,
                "find_element requires 'description' — what should I look for?",
            )
        region = (kwargs.get("region") or "foreground").lower()
        vision_fallback = bool(kwargs.get("vision_fallback", True))
        try:
            threshold = int(kwargs.get("fuzzy_threshold") or _DEFAULT_FUZZY_THRESHOLD)
        except (TypeError, ValueError):
            threshold = _DEFAULT_FUZZY_THRESHOLD
        threshold = max(0, min(threshold, 100))

        # Sensitive window guard (foreground capture only).
        info = _foreground_window_info() if region == "foreground" else {}
        if info and _is_sensitive_window(info.get("title", ""), info.get("process_name", "")):
            return self._error(
                params, start,
                f"REFUSED: foreground window {info.get('title')!r} is on the "
                "sensitive list. Switch focus before retrying.",
            )

        # 1) Capture to ephemeral.
        store = self.state.get_store()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        out_path = os.path.join(store.subdir("ephemeral"), f"find-{ts}.png")
        try:
            rect, _size = await asyncio.get_event_loop().run_in_executor(
                None, _screenshot_region, out_path, region, 0,
            )
        except Exception as exc:
            return self._error(params, start, f"find_element capture: {exc}")
        store.enforce_retention("ephemeral")

        # rect[0:2] is the screen-space origin of the captured area.
        # OCR / vision return bbox in IMAGE-space pixels; we add the
        # origin to get screen-space coords for click_at.
        origin_x, origin_y = int(rect[0]), int(rect[1])

        # 2) RapidOCR + rapidfuzz match.
        ocr_hit = await self._match_via_ocr(out_path, description, threshold)
        if ocr_hit is not None:
            cx, cy, conf, matched_text = ocr_hit
            return ToolResult(
                success=True,
                output={
                    "x": origin_x + cx,
                    "y": origin_y + cy,
                    "confidence": conf,
                    "source": "ocr",
                    "matched_text": matched_text,
                    "screenshot": out_path,
                    "region_origin": [origin_x, origin_y],
                    "image_xy": [cx, cy],
                },
                tool_name=self.name,
                tool_parameters=params,
                execution_time=time.time() - start,
            )

        # 3) Optional LLM vision fallback.
        if not vision_fallback:
            return self._error(
                params, start,
                f"find_element: OCR did not match {description!r} above "
                f"threshold {threshold}, and vision_fallback=false. "
                "Try lowering fuzzy_threshold or enabling vision_fallback.",
            )

        vision_hit = await self._match_via_vision(out_path, description)
        if vision_hit is None:
            return self._error(
                params, start,
                f"find_element: neither OCR nor vision could locate "
                f"{description!r}. Inspect the screenshot at {out_path} "
                "to verify the target is actually visible.",
            )
        vx, vy, vconf, vreason = vision_hit
        return ToolResult(
            success=True,
            output={
                "x": origin_x + vx,
                "y": origin_y + vy,
                "confidence": vconf,
                "source": "vision",
                "reason": vreason,
                "screenshot": out_path,
                "region_origin": [origin_x, origin_y],
                "image_xy": [vx, vy],
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    async def _match_via_ocr(
        self, image_path: str, description: str, threshold: int,
    ) -> Optional[Tuple[int, int, float, str]]:
        """Run local OCR + rapidfuzz match. Returns (cx, cy, confidence,
        matched_text) on hit, None on miss."""
        from ..infrastructure.vision import get_local_ocr
        ocr = get_local_ocr()
        result = await asyncio.get_event_loop().run_in_executor(
            None, ocr.recognize, image_path,
        )
        if not result.ok or not result.boxes:
            return None

        desc_lower = description.lower()
        best_score = 0.0
        best_box = None
        for box in result.boxes:
            text = (box.text or "").strip()
            if not text:
                continue
            score = float(fuzz.token_set_ratio(description, text))
            # Also check partial_ratio — a button label like "OK" inside
            # a longer OCR line should still match.
            score = max(score, float(fuzz.partial_ratio(description, text)))
            if score > best_score:
                best_score = score
                best_box = box

        if best_box is None or best_score < threshold:
            return None
        cx, cy = best_box.center
        return (cx, cy, best_score / 100.0, best_box.text)

    async def _match_via_vision(
        self, image_path: str, description: str,
    ) -> Optional[Tuple[int, int, float, str]]:
        """Fall back to LLM vision: ask for {x, y, confidence, reason}."""
        from ..infrastructure.config_manager import ConfigManager
        from ..infrastructure.vision import get_vision_client

        try:
            client = get_vision_client(ConfigManager())
        except Exception as exc:
            self.logger.warning(
                f"find_element vision fallback unavailable: {exc}",
                component="DesktopTool",
            )
            return None

        schema = {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
                "found": {"type": "boolean"},
            },
            "required": ["x", "y", "confidence"],
        }
        instruction = (
            f"Find the UI element described as: {description!r}. "
            "Return JSON {x, y, confidence, reason, found} where x and y "
            "are PIXEL coordinates of the target's visual centre in the "
            "input image. confidence is 0.0–1.0. If the element is not "
            "visible, set found=false and confidence near 0."
        )
        result = await client.query(image_path, instruction, output_schema=schema)
        if not result.ok or not result.parsed_json:
            return None
        d = result.parsed_json
        if d.get("found") is False:
            return None
        try:
            x = int(round(float(d.get("x", 0))))
            y = int(round(float(d.get("y", 0))))
            conf = float(d.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        reason = str(d.get("reason", ""))
        return (x, y, conf, reason)

    # ── find_and_click ───────────────────────────────────────────────────────
    #
    # One-shot combo: screenshot + find_element + click_at. Saves two LLM
    # round-trips per UI interaction (find returns coords; click consumes
    # them — the LLM never needs to see the coords). Most desktop tasks
    # are 80% click sequences, so wiring three sub-actions into one tool
    # call cuts the total LLM call count for a 10-click task from ~30 to
    # ~10.
    #
    # Failure modes are reported the same way as the underlying actions:
    # OCR / vision miss → error, sensitive window → error, pyautogui
    # failsafe → error. Read-only fall-through (no click) is NOT
    # provided: if the agent wants to inspect first, it should still
    # call find_element directly.

    async def _action_find_and_click(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())

        # Click parameters with sane defaults; same shape as click_at.
        button = (kwargs.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            return self._error(
                params, start, f"find_and_click: invalid button {button!r}"
            )
        double = bool(kwargs.get("double", False))
        use_uia = bool(kwargs.get("use_uia_pattern", True))

        # Delegate the find half to the existing handler so OCR pre-warm,
        # vision fallback, threshold handling, and the screenshot lifecycle
        # (ephemeral tier + retention sweep) all stay in one place.
        find_result = await self._action_find_element(params, start, **kwargs)
        if not find_result.success:
            return find_result  # propagate the find error verbatim

        out = find_result.output or {}
        try:
            x = int(out["x"])
            y = int(out["y"])
        except (KeyError, TypeError, ValueError):
            return self._error(
                params, start,
                "find_and_click: find_element succeeded but returned no "
                "(x, y) — internal error.",
            )

        # Now drive the click. The input guard (sensitive-window check +
        # _start_takeover) runs here as well — find_element never goes
        # through it because read-only.
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        _ensure_dpi_aware()
        state_before = _capture_state_before()

        # UIA path: only for left single-clicks (mirrors click_at logic).
        input_source: str
        if use_uia and button == "left" and not double:
            uia_err = await asyncio.get_event_loop().run_in_executor(
                None, _uia_invoke_at_point, x, y,
            )
            if uia_err is None:
                input_source = "uia_pattern"
            else:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: pyautogui.click(
                            x=x, y=y, button="left", clicks=1,
                        ),
                    )
                except pyautogui.FailSafeException:
                    return self._error(
                        params, start,
                        "find_and_click: PyAutoGUI failsafe triggered (mouse hit "
                        "corner). Move the mouse away from screen corners and retry.",
                    )
                input_source = "mouse"
        else:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pyautogui.click(
                        x=x, y=y, button=button, clicks=2 if double else 1,
                    ),
                )
            except pyautogui.FailSafeException:
                return self._error(
                    params, start,
                    "find_and_click: PyAutoGUI failsafe triggered (mouse hit "
                    "corner). Move the mouse away from screen corners and retry.",
                )
            input_source = "mouse"

        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)

        # Combined result: the find metadata + the click outcome. Lets the
        # LLM verify the OCR/vision match without an extra screenshot.
        merged = {
            "x": x, "y": y, "button": button, "double": double,
            "source": out.get("source"),
            "confidence": out.get("confidence"),
            "matched_text": out.get("matched_text"),
            "screenshot": out.get("screenshot"),
            "input_source": input_source,
            "state_after": state_after,
        }
        return ToolResult(
            success=True,
            output=merged,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── click_at ──────────────────────────────────────────────────────────────

    async def _action_click_at(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        try:
            x = int(kwargs["x"]); y = int(kwargs["y"])
        except (KeyError, TypeError, ValueError):
            return self._error(params, start, "click_at requires integer 'x' and 'y'.")
        button = (kwargs.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            return self._error(params, start, f"click_at: invalid button {button!r}")
        double = bool(kwargs.get("double", False))
        use_uia = bool(kwargs.get("use_uia_pattern", True))

        _ensure_dpi_aware()
        state_before = _capture_state_before()
        output: Dict[str, Any] = {"x": x, "y": y, "button": button, "double": double}

        # UIA path: only for left single-clicks (right/middle/double have no
        # UIA equivalent and must always use pyautogui).
        if use_uia and button == "left" and not double:
            uia_err = await asyncio.get_event_loop().run_in_executor(
                None, _uia_invoke_at_point, x, y,
            )
            if uia_err is None:
                output["input_source"] = "uia_pattern"
            else:
                output["uia_fallback_reason"] = uia_err
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: pyautogui.click(x=x, y=y, button="left", clicks=1),
                    )
                except pyautogui.FailSafeException:
                    return self._error(
                        params, start,
                        "click_at: PyAutoGUI failsafe triggered (mouse hit corner). "
                        "Move the mouse away from screen corners and retry.",
                    )
                output["input_source"] = "mouse"
        else:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pyautogui.click(x=x, y=y, button=button, clicks=2 if double else 1),
                )
            except pyautogui.FailSafeException:
                return self._error(
                    params, start,
                    "click_at: PyAutoGUI failsafe triggered (mouse hit corner). "
                    "Move the mouse away from screen corners and retry.",
                )
            output["input_source"] = "mouse"

        # Brief settle so Windows has time to react before we probe state.
        # 100 ms is enough for the typical click → dialog / focus shift,
        # while staying well below the user-visible threshold.
        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        output["state_after"] = state_after
        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── type_text ─────────────────────────────────────────────────────────────

    async def _action_type_text(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        text = kwargs.get("text")
        if text is None:
            return self._error(params, start, "type_text requires 'text'.")
        text = str(text)
        if len(text) > _TYPE_TEXT_MAX_CHARS:
            return self._error(
                params, start,
                f"type_text: payload {len(text)} chars exceeds cap "
                f"({_TYPE_TEXT_MAX_CHARS}). For long pastes use clipboard "
                "via shell tool instead.",
            )
        try:
            interval = float(kwargs.get("interval") or _DEFAULT_TYPE_INTERVAL)
        except (TypeError, ValueError):
            interval = _DEFAULT_TYPE_INTERVAL
        use_uia = bool(kwargs.get("use_uia_pattern", True))

        _ensure_dpi_aware()
        state_before = _capture_state_before()
        output: Dict[str, Any] = {"chars": len(text), "interval": interval}

        if use_uia:
            uia_err = await asyncio.get_event_loop().run_in_executor(
                None, _uia_set_focused_value, text,
            )
            if uia_err is None:
                # ValuePattern.SetValue replaces the entire field content.
                output["input_source"] = "uia_value_pattern"
            else:
                output["uia_fallback_reason"] = uia_err
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: pyautogui.typewrite(text, interval=interval),
                    )
                except Exception as exc:
                    return self._error(params, start, f"type_text: {exc}")
                output["input_source"] = "keyboard"
        else:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pyautogui.typewrite(text, interval=interval),
                )
            except Exception as exc:
                return self._error(params, start, f"type_text: {exc}")
            output["input_source"] = "keyboard"

        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        output["state_after"] = state_after
        return ToolResult(
            success=True,
            output=output,
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── drag ──────────────────────────────────────────────────────────────────

    async def _action_drag(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        try:
            fx = int(kwargs["from_x"]); fy = int(kwargs["from_y"])
            tx = int(kwargs["to_x"]);   ty = int(kwargs["to_y"])
        except (KeyError, TypeError, ValueError):
            return self._error(
                params, start,
                "drag requires integer from_x / from_y / to_x / to_y.",
            )
        try:
            duration = float(kwargs.get("duration") or 0.5)
        except (TypeError, ValueError):
            duration = 0.5
        # Some apps reject zero-duration drags as "click then release".
        # Clamp to a small minimum so drag actually registers.
        duration = max(duration, 0.1)

        _ensure_dpi_aware()
        state_before = _capture_state_before()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: (pyautogui.moveTo(fx, fy),
                         pyautogui.dragTo(tx, ty, duration=duration, button="left")),
            )
        except pyautogui.FailSafeException:
            return self._error(
                params, start,
                "drag: PyAutoGUI failsafe triggered. Mouse hit a corner.",
            )
        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        return ToolResult(
            success=True,
            output={
                "from": [fx, fy], "to": [tx, ty], "duration": duration,
                "state_after": state_after,
            },
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── scroll ────────────────────────────────────────────────────────────────

    async def _action_scroll(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        try:
            x = int(kwargs["x"]); y = int(kwargs["y"]); dy = int(kwargs["dy"])
        except (KeyError, TypeError, ValueError):
            return self._error(
                params, start,
                "scroll requires integer x / y / dy.",
            )
        _ensure_dpi_aware()
        state_before = _capture_state_before()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: pyautogui.scroll(dy, x=x, y=y),
            )
        except Exception as exc:
            return self._error(params, start, f"scroll: {exc}")
        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        return ToolResult(
            success=True,
            output={"x": x, "y": y, "dy": dy, "state_after": state_after},
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── hotkey ────────────────────────────────────────────────────────────────

    async def _action_hotkey(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        keys = kwargs.get("keys") or []
        if not isinstance(keys, list) or not keys:
            return self._error(
                params, start,
                "hotkey requires non-empty 'keys' list (e.g. ['ctrl','c']).",
            )
        keys = [str(k).strip().lower() for k in keys if str(k).strip()]
        _ensure_dpi_aware()
        state_before = _capture_state_before()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: pyautogui.hotkey(*keys),
            )
        except Exception as exc:
            return self._error(params, start, f"hotkey: {exc}")
        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        return ToolResult(
            success=True,
            output={"keys": keys, "state_after": state_after},
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── key_press ─────────────────────────────────────────────────────────────

    async def _action_key_press(
        self, params: Dict[str, Any], start: float, **kwargs: Any,
    ) -> ToolResult:
        if not _PYAUTOGUI_AVAILABLE:
            return self._error(params, start, _pyautogui_install_msg())
        guard = await self._input_action_guard()
        if guard:
            return self._error(params, start, guard)

        key = (kwargs.get("key") or "").strip().lower()
        if not key:
            return self._error(params, start, "key_press requires 'key'.")
        _ensure_dpi_aware()
        state_before = _capture_state_before()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: pyautogui.press(key),
            )
        except Exception as exc:
            return self._error(params, start, f"key_press: {exc}")
        await asyncio.sleep(0.1)
        state_after = _capture_state_after(state_before)
        self.state.invalidate_on_state_change(state_after)
        return ToolResult(
            success=True,
            output={"key": key, "state_after": state_after},
            tool_name=self.name,
            tool_parameters=params,
            execution_time=time.time() - start,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _input_action_guard(self) -> Optional[str]:
        """Pre-flight gate every input action runs.

        Order matters:
          1. sensitive-window check — banking / password manager are
             refused regardless of any approval.
          2. acquire the process-wide desktop ownership lock (per-task
             scope) so concurrent sessions queue here, not in the middle
             of mouse / keyboard sequences.
          3. mark takeover as active so the Electron overlay shows.

        The runtime agent (src/agent/runtime_agent.py:_check_before_act)
        is the source of truth for "may the agent operate the desktop".
        It consults ``is_task_approved()`` and only reaches this guard
        after a yes — so we don't need a mirror check here. Revoke
        flows back through the runtime gate by clearing the approval
        flag, not by failing this guard.

        Returns an error string when the action must be refused, or
        None when it may proceed. Async because acquiring the
        cross-session desktop ownership lock requires await.
        """
        info = _foreground_window_info()
        if info and _is_sensitive_window(info.get("title", ""), info.get("process_name", "")):
            return (
                f"REFUSED: foreground window {info.get('title')!r} matches "
                "the sensitive_window_patterns list. Input actions on "
                "password managers / banking apps are blocked. Switch "
                "focus before retrying."
            )
        # Cleared the sensitive-window gate — claim cross-session desktop
        # ownership (idempotent — re-entrant calls in the same task return
        # immediately) before flipping the takeover flag.
        await self.state.acquire_global_takeover()
        self.state.start_takeover("input_action")
        return None

    def _sensitive_guard(self) -> Optional[str]:
        """Backwards-compatible alias used by the few callers that only
        need the sensitive-window check (no takeover-state side-effect).
        Kept so older internal call sites keep working.
        """
        info = _foreground_window_info()
        if info and _is_sensitive_window(info.get("title", ""), info.get("process_name", "")):
            return (
                f"REFUSED: foreground window {info.get('title')!r} matches "
                "the sensitive_window_patterns list. Input actions on "
                "password managers / banking apps are blocked. Switch "
                "focus before retrying."
            )
        return None

    def _error(
        self, params: Dict[str, Any], start: float, msg: str,
    ) -> ToolResult:
        return ToolResult(
            success=False, output=None, error=msg,
            tool_name=self.name, tool_parameters=params,
            execution_time=time.time() - start,
        )


def _pyautogui_install_msg() -> str:
    return (
        "pyautogui is not installed. Run:\n"
        "  pip install pyautogui pywin32\n"
        "Required for desktop input actions (click_at, type_text, drag, "
        "scroll, hotkey, key_press).\n"
        "IMPORTANT: after installing, RESTART HandQ — the bridge caches "
        "the import result at startup, so a mid-session pip install will "
        "not take effect until restart."
    )
