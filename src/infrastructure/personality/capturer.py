"""Per-monitor screenshot capture wrapping :mod:`mss`.

Returns the captured frame as a numpy ndarray (RGB, H×W×3, contiguous).
The caller is responsible for any subsequent encoding or persistence —
the activity service keeps captured frames in memory (per-monitor ring
buffer of JPEG bytes) and never writes raw PNGs to disk, so we don't
flush to %USERPROFILE% and don't churn AV scanners.

The legacy "write PNG to ephemeral/ then unlink after OCR" path was
removed in favour of in-memory ndarray transport — see plan
robust-gathering-shannon.md §5 for context.

LTM 2.0 ``capture_focus_rect`` adds an optional optimization that screen-
shots only the foreground window's bounding rectangle (intersected with
the monitor bbox) instead of the whole display. For typical IDE / browser
sessions this halves the OCR-input pixel count, roughly halving OCR
latency. Falls back to full-monitor capture when:
  * The foreground hwnd is invalid or off-screen
  * The foreground spans more than one monitor
  * The foreground covers >80% of the monitor (savings marginal)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
from typing import Any, Optional, Tuple

from ..long_term_memory.models import MonitorInfo

_logger = logging.getLogger("handq.activity.capture")

# Win32 GetWindowRect — only on Windows. Other platforms get a no-op shim.
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _GetWindowRect = _user32.GetWindowRect
    _GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _GetWindowRect.restype = wintypes.BOOL
    _IsWindowVisible = _user32.IsWindowVisible
    _IsWindowVisible.argtypes = [wintypes.HWND]
    _IsWindowVisible.restype = wintypes.BOOL
    _WIN32_AVAILABLE = True
except (OSError, AttributeError):
    _WIN32_AVAILABLE = False


def get_foreground_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return (left, top, right, bottom) for hwnd, or None on failure."""
    if not _WIN32_AVAILABLE or not hwnd:
        return None
    try:
        if not _IsWindowVisible(wintypes.HWND(hwnd)):
            return None
        rect = wintypes.RECT()
        if not _GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def compute_focus_rect(
    *,
    foreground_hwnd: Optional[int],
    monitor_bbox: Tuple[int, int, int, int],
) -> Optional[Tuple[int, int, int, int]]:
    """Compute (x, y, w, h) of the foreground window inside the monitor.

    Returns None if:
      * No hwnd provided / window not visible
      * Foreground doesn't intersect this monitor (it's on a different display)
      * Intersection covers >80% of the monitor area (savings marginal —
        full grab simpler)
      * Intersection covers <5% of the monitor area (likely a popup that
        won't contain useful OCR text)
    """
    if not foreground_hwnd:
        return None
    win_rect = get_foreground_window_rect(foreground_hwnd)
    if not win_rect:
        return None
    wl, wt, wr, wb = win_rect
    ml, mt, mr, mb = monitor_bbox
    # Intersect
    ix1 = max(wl, ml)
    iy1 = max(wt, mt)
    ix2 = min(wr, mr)
    iy2 = min(wb, mb)
    if ix2 <= ix1 or iy2 <= iy1:
        return None  # no overlap
    iw, ih = ix2 - ix1, iy2 - iy1
    mw, mh = mr - ml, mb - mt
    if mw <= 0 or mh <= 0:
        return None
    coverage = (iw * ih) / (mw * mh)
    if coverage > 0.80 or coverage < 0.05:
        return None
    return (ix1, iy1, iw, ih)


class MonitorCapturer:
    """Thread-local mss capturer.

    mss stores OS-level handles in a per-instance state and is **not**
    safe to share across threads on Windows. We allocate one per loop
    thread and keep it alive for the process lifetime so cold-start
    overhead is paid once.
    """

    def __init__(self) -> None:
        self._tls = threading.local()

    def _client(self):  # type: ignore[no-untyped-def]
        c = getattr(self._tls, "mss", None)
        if c is None:
            try:
                import mss
            except Exception as exc:
                raise RuntimeError(
                    "mss is not installed; activity_monitor cannot capture. "
                    "Run: pip install mss"
                ) from exc
            c = mss.mss()
            self._tls.mss = c
        return c

    def capture(self, info: MonitorInfo) -> Optional[Any]:
        """Grab the monitor described by *info* and return an RGB ndarray
        (H, W, 3, dtype=uint8, C-contiguous). Returns None on failure.

        Failure modes (logged at debug):
          - mss raised (display switched, RDP disconnect, GPU hung)
          - numpy is missing (degenerate dev environment)

        We never raise to the caller — a missed frame is recoverable on
        the next tick; raising would crash the loop.
        """
        try:
            c = self._client()
        except Exception as exc:
            _logger.warning("activity capture: mss client init failed: %s", exc)
            return None
        try:
            import numpy as np
            l, t, r, b = info.bbox
            region = {"left": l, "top": t, "width": r - l, "height": b - t}
            shot = c.grab(region)
            # mss returns BGRA bytes; reshape into a numpy view, then
            # swap channels to RGB and drop the alpha. ascontiguousarray
            # is required because RapidOCR / PIL JPEG encode want a
            # contiguous buffer (numpy fancy indexing returns a copy
            # that's already contiguous, but be explicit to document
            # the invariant).
            w, h = shot.size
            arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(h, w, 4)
            rgb = np.ascontiguousarray(arr[..., [2, 1, 0]])
            return rgb
        except Exception:
            _logger.debug("activity capture failed for monitor %d",
                          info.index, exc_info=True)
            return None

    def capture_focus_rect(
        self,
        info: MonitorInfo,
        *,
        foreground_hwnd: Optional[int],
    ) -> Tuple[Optional[Any], Optional[Tuple[int, int, int, int]], bool]:
        """Capture only the foreground window's bbox on this monitor.

        Returns ``(rgb_array, focus_rect_xywh, used_focus_rect)``.
        On any failure (no hwnd, no overlap, coverage out of band) falls
        back to full-monitor capture and ``used_focus_rect=False``.
        """
        rect = compute_focus_rect(
            foreground_hwnd=foreground_hwnd, monitor_bbox=info.bbox,
        )
        if rect is None:
            return self.capture(info), None, False
        try:
            c = self._client()
            import numpy as np
            x, y, w, h = rect
            region = {"left": x, "top": y, "width": w, "height": h}
            shot = c.grab(region)
            sw, sh = shot.size
            arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(sh, sw, 4)
            rgb = np.ascontiguousarray(arr[..., [2, 1, 0]])
            return rgb, rect, True
        except Exception:
            _logger.debug(
                "activity focus_rect capture failed; falling back to full monitor",
                exc_info=True,
            )
            return self.capture(info), None, False

    def close(self) -> None:
        c = getattr(self._tls, "mss", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._tls.mss = None
