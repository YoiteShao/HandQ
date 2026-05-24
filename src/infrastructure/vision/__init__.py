"""HandQ vision package — multimodal client + screenshot tier storage.

Public API:

  * :class:`VisionClient`, :class:`VisionResult` —  one-shot LLM vision
    calls against the QGenie OpenAI-compatible gateway.
  * :func:`get_vision_client`, :func:`flush_vision_client` — process-wide
    singleton lifecycle, mirroring the browser pool idiom.
  * :class:`ScreenshotStore` — tiered scratch storage (ephemeral / task /
    activity) for screenshots and vision input frames. Each producer
    (browser_tool, desktop_tool, activity_monitor) holds its own
    instance with a different root directory.

Future modules (Phase 2/3) will land in this package alongside the
existing pieces:

  * ``ocr.py``       — RapidOCR / WinRT adapters for local OCR
  * ``capture.py``   — common screenshot-capture interface across
                      Playwright pages, mss desktop, etc.

See ARCHITECTURE.md §1.6 for the screenshot categorisation contract.
"""
from .client import (
    VisionClient,
    VisionResult,
    get_vision_client,
    flush_vision_client,
)
from .storage import ScreenshotStore

__all__ = [
    "VisionClient",
    "VisionResult",
    "get_vision_client",
    "flush_vision_client",
    "ScreenshotStore",
]
