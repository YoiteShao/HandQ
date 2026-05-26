"""Local OCR engines — RapidOCR primary, with shared dataclasses.

Phase 0 benchmark (`scripts/local_ocr_bench/`) selected RapidOCR
(`rapidocr-onnxruntime`, PP-OCR-v4 mobile) as the primary local engine
for HandQ:

  * Mean CER 0.025 on the synthetic dataset (English + Chinese mix).
  * Element-localisation centre distance 7 px — better than LLM vision
    (19 px) on the same dataset.
  * Warm P95 ~1.1 s for 1024-pixel screenshots on CPU.
  * Pure CPU, ~10 MB model bundle, no GPU dependency.

This module exposes a singleton wrapper plus the small dataclasses that
callers (desktop_tool find_element, activity_monitor periodic capture)
use to consume results.  The bench scripts live in ``scripts/`` and are
intentionally separate — they own their own engine adapters because
they need to A/B against PaddleOCR / WinRT / LLM vision.

Configuration in ``handq_config.yaml`` (optional — defaults work)::

    vision:
      ocr:
        engine: rapidocr   # only one option today
        lang: ch           # ch covers Chinese + English
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from ..logger import get_logger


@dataclass
class OCRBox:
    """A single recognised text region in the input image.

    bbox is (x1, y1, x2, y2) in pixel coordinates of the input.
    """
    text: str
    bbox: Tuple[int, int, int, int]
    confidence: float = 0.0

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) // 2, (y1 + y2) // 2)


@dataclass
class OCRResult:
    """Output of a single :meth:`LocalOCR.recognize`."""
    full_text: str
    boxes: List[OCRBox] = field(default_factory=list)
    elapsed_ms: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class LocalOCR:
    """Thin wrapper around RapidOCR with HandQ-flavoured semantics.

    Lazy-loads the underlying engine on first ``recognize`` call (cold
    start ~600 ms; the cold cost is documented in
    `docs/long_term_memory/02_handq_design.md` and Phase 0's
    ``results.md`` if you regenerate them).

    Thread-safety: RapidOCR's onnxruntime sessions are not strictly
    thread-safe, but our usage pattern is event-loop serialised
    (desktop_tool's lock + activity_monitor's single worker), so we do
    not add an explicit lock here.
    """

    def __init__(self, *, lang: str = "ch") -> None:
        # 'ch' covers both Chinese and English; 'en' is English-only and
        # marginally faster but loses our bilingual coverage.
        self._lang = lang
        self._engine: Any = None
        self._logger = get_logger()

    def _ensure_engine(self) -> Optional[str]:
        """Load the RapidOCR engine on first use. Returns an error string
        when the dependency is missing, None on success."""
        if self._engine is not None:
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            return (
                "rapidocr_onnxruntime is not installed. Run:\n"
                "  pip install rapidocr-onnxruntime\n"
                f"Underlying: {exc}"
            )
        self._engine = RapidOCR()
        return None

    def recognize(self, image_path: str) -> OCRResult:
        """Run OCR on a PNG/JPEG file path. Returns OCRResult with boxes
        and a newline-joined full_text. Catches its own exceptions —
        callers don't need to wrap.
        """
        err = self._ensure_engine()
        if err is not None:
            return OCRResult("", error=err)
        t0 = time.time()
        try:
            # RapidOCR returns (results, elapse_seconds) where each
            # result is [polygon_points, text, score]. Older versions
            # used a different ordering; we defensively unpack.
            result, _elapse = self._engine(image_path)
        except Exception as exc:
            return OCRResult("", error=f"recognize failed: {exc}",
                             elapsed_ms=int((time.time() - t0) * 1000))
        elapsed_ms = int((time.time() - t0) * 1000)

        boxes: List[OCRBox] = []
        full_lines: List[str] = []
        if result:
            for item in result:
                try:
                    polygon, text, score = item[0], item[1], item[2]
                except (TypeError, IndexError):
                    continue
                if not text:
                    continue
                xs = [int(p[0]) for p in polygon]
                ys = [int(p[1]) for p in polygon]
                boxes.append(OCRBox(
                    text=str(text),
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=float(score),
                ))
                full_lines.append(str(text))
        return OCRResult(
            full_text="\n".join(full_lines),
            boxes=boxes,
            elapsed_ms=elapsed_ms,
        )

    def close(self) -> None:
        # rapidocr_onnxruntime owns its onnx sessions and releases them
        # on GC; we just drop the reference.
        self._engine = None


# ── Process-wide singleton ───────────────────────────────────────────────────

_local_ocr: Optional[LocalOCR] = None


def get_local_ocr(config_manager: Any = None) -> LocalOCR:
    """Return the process-wide :class:`LocalOCR` singleton.

    Builds it on first call from the optional ``vision.ocr:`` subsection of
    ``handq_config.yaml``.  Defaults are sensible (lang='ch') so most
    deployments need no config block at all.
    """
    global _local_ocr
    if _local_ocr is not None:
        return _local_ocr
    lang = "ch"
    if config_manager is not None:
        try:
            vision_section = config_manager.get_section("vision") or {}
            section = vision_section.get("ocr") or {}
            lang = str(section.get("lang", "ch")).strip() or "ch"
        except Exception:
            pass
    _local_ocr = LocalOCR(lang=lang)
    return _local_ocr


def flush_local_ocr() -> int:
    """Drop the singleton (if any). Returns 1 on close, 0 otherwise."""
    global _local_ocr
    inst = _local_ocr
    _local_ocr = None
    if inst is None:
        return 0
    try:
        inst.close()
    except Exception:
        pass
    return 1
