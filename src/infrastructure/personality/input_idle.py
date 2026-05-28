"""Windows input-idle and multi-monitor enumeration helpers.

Two responsibilities:

1. Tell the activity service "how long since the user last touched any
   input device". Drives the GLOBAL idle gate that pauses every monitor
   if nobody's at the machine.

2. Enumerate physical displays with their virtual-screen rectangles.
   Drives the *per-monitor* state — each display gets its own state
   machine, captured independently, with its own buffer.

Both wrappers degrade to "best effort" on non-Windows or when the
optional pywin32 / ctypes path fails. The activity service treats
``None`` returns as "no signal" and falls back to wall-clock heuristics
rather than crashing.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import List, Optional, Tuple

from ..long_term_memory.models import MonitorInfo

_logger = logging.getLogger("handq.activity.input")


# ── Input idle (GetLastInputInfo) ───────────────────────────────────────────


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def system_idle_seconds() -> Optional[float]:
    """Return seconds since the last keyboard / mouse input system-wide.

    Windows-only. Returns ``None`` on every non-Windows platform AND on
    Windows when GetLastInputInfo fails (e.g. running as a service in
    session 0 — there is no input session to query).
    """
    if sys.platform != "win32":
        return None
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):  # type: ignore[attr-defined]
            return None
        # GetTickCount wraps every ~49.7 days — not a problem for our use
        # because we read both within milliseconds of each other.
        tick = ctypes.windll.kernel32.GetTickCount()  # type: ignore[attr-defined]
        delta_ms = (tick - lii.dwTime) & 0xFFFFFFFF
        return float(delta_ms) / 1000.0
    except Exception:  # pragma: no cover — defensive
        _logger.exception("system_idle_seconds failed")
        return None


# ── Cursor location (which monitor is the user pointing at?) ────────────────


def cursor_pos() -> Optional[Tuple[int, int]]:
    """Return (x, y) of the cursor on the virtual screen, or None."""
    if sys.platform != "win32":
        return None
    try:
        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):  # type: ignore[attr-defined]
            return None
        return (int(pt.x), int(pt.y))
    except Exception:
        return None


# ── Monitor enumeration ─────────────────────────────────────────────────────


def enumerate_monitors() -> List[MonitorInfo]:
    """Best-effort enumeration of physical displays.

    Order of preference:
      1. ``mss.mss().monitors`` — already a dependency for screen capture,
         and gives consistent coordinates with what we'll capture from.
      2. EnumDisplayMonitors via ctypes (pure Windows API fallback).
      3. Single virtual-screen entry (covers everything; used when both
         options above fail). The activity loop still works in this
         degraded mode — just with no per-display granularity.

    Returns an empty list on non-Windows when both paths fail; the
    activity service treats that as "no monitors to capture" and stays
    idle.
    """
    monitors = _enumerate_via_mss()
    if monitors:
        return monitors
    monitors = _enumerate_via_winapi()
    if monitors:
        return monitors
    monitors = _enumerate_virtual_only()
    return monitors


def _enumerate_via_mss() -> List[MonitorInfo]:
    try:
        import mss
    except Exception:
        return []
    try:
        with mss.mss() as sct:
            raw = list(sct.monitors)
    except Exception:
        _logger.exception("mss enumerate failed")
        return []
    # mss.monitors[0] is the union of all displays; entries 1..N are
    # individual monitors. We expose the per-display ones; index 0
    # (the virtual screen) only matters for capture, not for sampling.
    out: List[MonitorInfo] = []
    for i, m in enumerate(raw[1:], start=1):
        try:
            left = int(m.get("left", 0))
            top = int(m.get("top", 0))
            width = int(m.get("width", 0))
            height = int(m.get("height", 0))
        except Exception:
            continue
        if width <= 0 or height <= 0:
            continue
        bbox = (left, top, left + width, top + height)
        out.append(MonitorInfo(
            index=i,
            bbox=bbox,
            primary=(i == 1),
            label=f"Display {i} ({width}x{height}{', primary' if i == 1 else ''})",
        ))
    return out


def _enumerate_via_winapi() -> List[MonitorInfo]:
    if sys.platform != "win32":
        return []
    try:
        # EnumDisplayMonitors callback signature.
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,    # HMONITOR
            ctypes.c_void_p,    # HDC
            ctypes.POINTER(wintypes.RECT),
            ctypes.c_double,    # LPARAM
        )

        out: List[MonitorInfo] = []

        def _cb(hmon, hdc, lprect, lparam):  # type: ignore[no-untyped-def]
            r = lprect.contents
            out.append(MonitorInfo(
                index=len(out) + 1,
                bbox=(int(r.left), int(r.top), int(r.right), int(r.bottom)),
                primary=(int(r.left) == 0 and int(r.top) == 0),
                label=f"Display {len(out)+1} ({int(r.right-r.left)}x{int(r.bottom-r.top)})",
            ))
            return 1

        if not ctypes.windll.user32.EnumDisplayMonitors(  # type: ignore[attr-defined]
            None, None, MONITORENUMPROC(_cb), 0,
        ):
            return []
        return out
    except Exception:
        _logger.exception("winapi enumerate failed")
        return []


def _enumerate_virtual_only() -> List[MonitorInfo]:
    if sys.platform != "win32":
        # Last-resort: a single virtual display covering some default size.
        return [MonitorInfo(
            index=1, bbox=(0, 0, 1920, 1080),
            primary=True, label="Display 1 (virtual fallback)",
        )]
    try:
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        gsm = ctypes.windll.user32.GetSystemMetrics  # type: ignore[attr-defined]
        x = int(gsm(SM_XVIRTUALSCREEN))
        y = int(gsm(SM_YVIRTUALSCREEN))
        w = int(gsm(SM_CXVIRTUALSCREEN))
        h = int(gsm(SM_CYVIRTUALSCREEN))
        return [MonitorInfo(
            index=1, bbox=(x, y, x + w, y + h),
            primary=True, label=f"Virtual {w}x{h}",
        )]
    except Exception:
        return []


def cursor_in_monitor(pt: Tuple[int, int], info: MonitorInfo) -> bool:
    x, y = pt
    l, t, r, b = info.bbox
    return l <= x < r and t <= y < b


# ── Foreground window (sensitive-app gate) ─────────────────────────────────


def foreground_window_title() -> str:
    """Return the foreground window title, or "" on any failure.

    Used by the sensitive-window gate: matching the title against
    ACTIVITY_SENSITIVE_WINDOW_PATTERNS lets the monitor abandon a capture
    that would otherwise screenshot a password manager etc.
    """
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


def foreground_app_name() -> str:
    """Return the foreground process name (e.g. ``Code.exe``), or ""."""
    if sys.platform != "win32":
        return ""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        try:
            import psutil  # type: ignore
            p = psutil.Process(pid.value)
            return p.name() or ""
        except Exception:
            return ""
    except Exception:
        return ""
