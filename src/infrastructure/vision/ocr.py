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

import gc
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import numpy as np
from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

from ..long_term_memory import _constants as _C

# ── Dark-background preprocessing ────────────────────────────────────────────
# PP-OCR's text detection model was trained primarily on natural scenes and
# documents with dark-on-light text. White/green-on-black (terminals, CMD,
# PowerShell) hits a known blind spot. We detect such images and invert them
# before recognition so the detector sees the expected polarity.

_DARK_BG_MEAN_THRESHOLD = 90  # per-channel mean below this → dark background
_DARK_BG_SAMPLE_BORDER = 20   # pixels from each edge to sample for bg check


def _is_dark_background(img: np.ndarray) -> bool:
    """Heuristic: sample border strips and check if the image is predominantly dark."""
    h, w = img.shape[:2]
    if h < _DARK_BG_SAMPLE_BORDER * 3 or w < _DARK_BG_SAMPLE_BORDER * 3:
        return float(img.mean()) < _DARK_BG_MEAN_THRESHOLD
    b = _DARK_BG_SAMPLE_BORDER
    top = img[:b, :, ...]
    bottom = img[-b:, :, ...]
    left = img[:, :b, ...]
    right = img[:, -b:, ...]
    border_mean = np.mean([top.mean(), bottom.mean(), left.mean(), right.mean()])
    return float(border_mean) < _DARK_BG_MEAN_THRESHOLD


def _preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Invert dark-background images to improve PP-OCR detection."""
    if _is_dark_background(img):
        return 255 - img
    return img


def _cap_long_edge(img: np.ndarray, max_long_edge: int) -> Tuple[np.ndarray, float]:
    """Downscale *img* so its long edge <= max_long_edge.

    Returns ``(img, scale)`` where ``scale`` is the factor APPLIED (<=1.0).
    No-op (scale=1.0, same array) when already within bound. Callers must
    multiply any pixel coordinate derived from the returned image by
    ``1/scale`` to map it back into the ORIGINAL image's coordinate space —
    see ``recognize()``.

    RapidOCR's own ``limit_side_len`` only resizes the tensor fed to the
    detection model; it does not shrink the intermediate image-processing
    buffers, which scale with the INPUT image and are the real RSS driver
    on 4K / multi-monitor captures. This cap runs before RapidOCR ever sees
    the image, so it shrinks every downstream buffer, not just the model
    input.
    """
    h, w = img.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_long_edge:
        return img, 1.0
    scale = max_long_edge / float(long_edge)
    import cv2
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized, scale


def _load_image_as_ndarray(image: Any) -> Optional[np.ndarray]:
    """Convert supported image inputs to numpy ndarray (BGR/RGB uint8).

    Returns None if the input is not a type we can preprocess (falls through
    to RapidOCR's own loader).
    """
    if isinstance(image, np.ndarray):
        return image
    if isinstance(image, bytes):
        arr = np.frombuffer(image, dtype=np.uint8)
        import cv2
        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return decoded
    if isinstance(image, (str, Path)):
        import cv2
        decoded = cv2.imread(str(image), cv2.IMREAD_COLOR)
        return decoded
    return None

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

    def __init__(
        self,
        *,
        lang: str = "ch",
        intra_op_num_threads: Optional[int] = None,
        inter_op_num_threads: Optional[int] = None,
    ) -> None:
        # 'ch' covers both Chinese and English; 'en' is English-only and
        # marginally faster but loses our bilingual coverage.
        self._lang = lang
        # ONNX Runtime thread caps. ``None`` means "use ORT defaults"
        # (one thread per physical core) — the right answer for the
        # interactive desktop_tool singleton where each call is one-shot
        # and the user is waiting. The activity_monitor drain instance
        # passes explicit values from _constants.OCR_DRAIN_*_NUM_THREADS
        # so back-to-back drain doesn't saturate the box.
        self._intra_op_num_threads = intra_op_num_threads
        self._inter_op_num_threads = inter_op_num_threads
        self._engine: Any = None
        self._logger = get_logger()
        # Wall-clock of the last recognize() call. Read by
        # PersonalityMonitor's idle-flush gate (get_local_ocr_last_used_ts)
        # to decide whether the interactive singleton is safe to tear down —
        # 0.0 means "never used" (e.g. only prewarmed via _ensure_engine()).
        self.last_used_ts: float = 0.0

    def _ensure_engine(self) -> Optional[str]:
        """Load the RapidOCR engine on first use. Returns None on success."""
        if self._engine is not None:
            return None
        rapid_kwargs: dict = {}
        if self._intra_op_num_threads is not None:
            rapid_kwargs["intra_op_num_threads"] = self._intra_op_num_threads
        if self._inter_op_num_threads is not None:
            rapid_kwargs["inter_op_num_threads"] = self._inter_op_num_threads
        # RapidOCR's UpdateParameters reads only the bare (no-prefix)
        # thread kwargs and copies Global → Det/Cls/Rec via
        # update_global_to_module. Per-stage ``det_intra_op_num_threads``
        # would be silently overwritten by that copy, so we pass the
        # bare names.
        self._engine = RapidOCR(**rapid_kwargs)
        return None

    def recognize(self, image: Union[str, bytes, Any]) -> OCRResult:
        """Run OCR on the supplied image. Returns OCRResult with boxes
        and a newline-joined full_text. Catches its own exceptions —
        callers don't need to wrap.

        ``image`` accepts any input that RapidOCR's ``__call__`` accepts:
          * ``str`` / ``Path`` — file path on disk (PNG / JPEG)
          * ``bytes`` — raw encoded image bytes (PNG / JPEG / etc.)
            Used by PersonalityMonitor's deferred-OCR drain to feed
            JPEG bytes from its in-memory ring without round-tripping
            through disk.
          * ``numpy.ndarray`` — pre-decoded RGB array (H, W, 3 uint8).
            Used by desktop_tool / activity_monitor when the frame is
            already in memory as a numpy array.

        Preprocessing: dark-background images (terminals, CMD) are
        automatically inverted before detection to compensate for PP-OCR's
        dark-on-light training bias. Inputs whose long edge exceeds
        ``_constants.OCR_MAX_LONG_EDGE_PX`` are downscaled before detection
        (see ``_cap_long_edge``); returned box coordinates are scaled back
        to the ORIGINAL input's pixel space, so this is transparent to
        every caller.
        """
        self.last_used_ts = time.time()
        err = self._ensure_engine()
        if err is not None:
            return OCRResult("", error=err)
        t0 = time.time()
        scale = 1.0
        try:
            ocr_input = image
            arr = _load_image_as_ndarray(image)
            if arr is not None:
                if _C.OCR_RESIZE_CAP_ENABLED:
                    arr, scale = _cap_long_edge(arr, _C.OCR_MAX_LONG_EDGE_PX)
                ocr_input = _preprocess_for_ocr(arr)
            # RapidOCR returns (results, elapse_seconds) where each
            # result is [polygon_points, text, score]. Older versions
            # used a different ordering; we defensively unpack.
            result, _elapse = self._engine(ocr_input)
        except Exception as exc:
            return OCRResult("", error=f"recognize failed: {exc}",
                             elapsed_ms=int((time.time() - t0) * 1000))
        elapsed_ms = int((time.time() - t0) * 1000)

        # Map coordinates back to the ORIGINAL (pre-downscale) pixel space
        # so the "bbox is pixel coordinates of the input" contract holds
        # unchanged for every caller, whether or not the resize cap fired.
        inv = 1.0 / scale if scale != 1.0 else 1.0
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
                xs = [int(p[0] * inv) for p in polygon]
                ys = [int(p[1] * inv) for p in polygon]
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
_local_ocr_background: Optional[LocalOCR] = None


def get_local_ocr(config_manager: Any = None) -> LocalOCR:
    """Return the process-wide :class:`LocalOCR` singleton.

    Builds it on first call from the optional ``vision.ocr:`` subsection of
    ``handq_config.yaml``.  Defaults are sensible (lang='ch') so most
    deployments need no config block at all.

    This singleton uses ONNX Runtime's default thread pool (one thread per
    physical core) — appropriate for interactive callers like desktop_tool
    where the user is waiting on a single find_element result. The
    activity_monitor's OCR drain should NOT use this singleton; see
    :func:`get_local_ocr_background` for the thread-capped variant.
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


def get_local_ocr_background(config_manager: Any = None) -> LocalOCR:
    """Return the process-wide background :class:`LocalOCR` singleton.

    Distinct from :func:`get_local_ocr` so the activity_monitor's OCR drain
    can run with capped ONNX thread pools without slowing down the
    interactive desktop_tool path. Thread budget comes from
    ``long_term_memory._constants.OCR_DRAIN_*_NUM_THREADS`` (default 2 / 1).

    Cold-start cost is paid once at first use (~600 ms); RAM overhead vs
    the interactive singleton is small (the RapidOCR model bundle is
    ~10 MB; each instance allocates its own ONNX sessions).
    """
    global _local_ocr_background
    if _local_ocr_background is not None:
        return _local_ocr_background
    from ..long_term_memory import _constants as C
    lang = "ch"
    if config_manager is not None:
        try:
            vision_section = config_manager.get_section("vision") or {}
            section = vision_section.get("ocr") or {}
            lang = str(section.get("lang", "ch")).strip() or "ch"
        except Exception:
            pass
    _local_ocr_background = LocalOCR(
        lang=lang,
        intra_op_num_threads=C.OCR_DRAIN_INTRA_OP_NUM_THREADS,
        inter_op_num_threads=C.OCR_DRAIN_INTER_OP_NUM_THREADS,
    )
    return _local_ocr_background


def flush_local_ocr(*, background: bool = True, interactive: bool = True) -> int:
    """Drop the selected singleton(s) and reclaim their memory.

    Called by :class:`~..personality.service.PersonalityMonitor`'s idle-flush
    gate once the shared OCR idle signal has held long enough (see
    ``_constants.py`` §11.8) — never from an interactive code path, so this
    is safe to make as thorough as possible.

    RapidOCR sets ``enable_cpu_mem_arena=False`` on its own onnxruntime
    sessions (arena is OFF), so dropping the last reference to the engine and
    forcing a GC pass actually returns the freed InferenceSession memory to
    the OS rather than leaving it held in a growing arena. Returns the count
    of instances actually closed (0 when nothing was loaded).
    """
    global _local_ocr, _local_ocr_background
    closed = 0
    targets = []
    if interactive:
        targets.append("_local_ocr")
    if background:
        targets.append("_local_ocr_background")
    for attr in targets:
        inst = globals()[attr]
        globals()[attr] = None
        if inst is None:
            continue
        try:
            inst.close()
            closed += 1
        except Exception:
            pass
    if closed:
        gc.collect()
    return closed


def get_local_ocr_last_used_ts() -> float:
    """Wall-clock time of the interactive singleton's last ``recognize()``
    call. Returns 0.0 if the singleton was never built or never actually
    used for recognition (e.g. only prewarmed via ``_ensure_engine()``).

    This is the authoritative "is OCR actually being used" signal for the
    idle-flush gate — robust to desktop_tool's read-only actions
    (screenshot / find_element / snapshot), which never take the
    cross-session desktop ownership lock that a lock-based check would
    otherwise have to rely on.
    """
    return _local_ocr.last_used_ts if _local_ocr is not None else 0.0


def is_local_ocr_loaded() -> bool:
    """True iff the interactive singleton currently holds a LocalOCR
    instance (regardless of whether its ONNX engine has been lazily built
    yet)."""
    return _local_ocr is not None

