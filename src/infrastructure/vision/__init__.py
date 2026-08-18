"""HandQ vision package — multimodal LLM + local OCR + screenshot tier storage.

File layout
-----------
  llm.py       multimodal LLM client (YOUR-AI-ENDPOINT azure::gpt-5.4-mini)
  ocr.py       local OCR engines (RapidOCR / PP-OCR-v4 mobile)
  storage.py   tiered scratch storage (ephemeral / task / activity)

Each producer (browser_tool, desktop_tool, activity_monitor) holds its
own ScreenshotStore instance with a different root directory but
shares the ``handq_config.yaml`` ``screenshots:`` section for retention
limits. The LLM client and OCR engine are both process-wide singletons.

See ARCHITECTURE.md §1.6 for the screenshot categorisation contract
and `docs/desktop_tool.md` for the desktop tool's pipeline.
"""
from .llm import (
    VisionClient,
    VisionResult,
    get_vision_client,
    flush_vision_client,
)
from .storage import ScreenshotStore
from .ocr import (
    LocalOCR,
    OCRBox,
    OCRResult,
    get_local_ocr,
    get_local_ocr_background,
    get_local_ocr_last_used_ts,
    is_local_ocr_loaded,
    flush_local_ocr,
)

__all__ = [
    "VisionClient",
    "VisionResult",
    "get_vision_client",
    "flush_vision_client",
    "ScreenshotStore",
    "LocalOCR",
    "OCRBox",
    "OCRResult",
    "get_local_ocr",
    "get_local_ocr_background",
    "get_local_ocr_last_used_ts",
    "is_local_ocr_loaded",
    "flush_local_ocr",
]
