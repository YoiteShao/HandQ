"""Per-monitor screenshot capture wrapping :mod:`mss`.

Returns the captured frame as a numpy ndarray (RGB, H×W×3, contiguous).
The caller is responsible for any subsequent encoding or persistence —
the activity service keeps captured frames in memory (per-monitor ring
buffer of JPEG bytes) and never writes raw PNGs to disk, so we don't
flush to %USERPROFILE% and don't churn AV scanners.

The legacy "write PNG to ephemeral/ then unlink after OCR" path was
removed in favour of in-memory ndarray transport — see plan
robust-gathering-shannon.md §5 for context.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from ..long_term_memory.models import MonitorInfo

_logger = logging.getLogger("handq.activity.capture")


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

    def close(self) -> None:
        c = getattr(self._tls, "mss", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._tls.mss = None
