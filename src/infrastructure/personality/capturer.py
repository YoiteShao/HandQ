"""Per-monitor screenshot capture wrapping :mod:`mss`.

The capture path writes to a single rotating filename per monitor —
``activity_m<index>.png`` under the activity tier of
:class:`vision.storage.ScreenshotStore`. Each capture overwrites the
previous file, and the activity service unlinks it the moment OCR
returns. There is therefore at most ONE PNG per monitor on disk at any
moment, and that file's lifetime is sub-second on modern hardware.

Why a fixed filename rather than timestamped: it makes "is the disk
accumulating screenshots?" a trivial check (the answer must be no), and
removes the need for a periodic GC pass. The :class:`ScreenshotStore`
retention sweep stays in place as a backstop.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

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

    def capture(self, info: MonitorInfo, out_path: str) -> Optional[str]:
        """Grab the monitor described by *info* and write a PNG to
        *out_path*. Returns the path on success, None on failure.

        Failure modes (logged at debug):
          - mss raised (display switched, RDP disconnect, GPU hung)
          - PIL save raised (unwritable path, disk full)

        We never raise to the caller — a missed frame is recoverable
        on the next tick; raising would crash the loop.
        """
        try:
            c = self._client()
        except Exception as exc:
            _logger.warning("activity capture: mss client init failed: %s", exc)
            return None
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            l, t, r, b = info.bbox
            region = {"left": l, "top": t, "width": r - l, "height": b - t}
            shot = c.grab(region)
            from PIL import Image
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(out_path, format="PNG", optimize=False, compress_level=1)
            return out_path
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
