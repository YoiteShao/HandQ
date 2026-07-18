"""Cheap perceptual hash + text-similarity helpers.

The activity service uses these BEFORE OCR (image hash) and AFTER OCR
(text Jaccard) to decide whether the current capture is novel enough
to be worth pushing into LTM. Both are deliberately simple:

- Image hash: 16x16 grayscale → mean threshold → 256-bit fingerprint.
  Hamming distance gives a coarse "are these the same screen" signal.
  False positives (two visually similar but logically different
  screens) are accepted because the text-similarity stage catches them.

- Text Jaccard: shingled token set ratio. Cheap, language-agnostic, and
  resilient to small UI chrome differences (clock minute changing,
  scrollbar position).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Set

_logger = logging.getLogger("handq.activity.diff")


# ── Perceptual hash ────────────────────────────────────────────────────────


def perceptual_hash_array(
    arr: Any, *, downsample_px: int = 16,
) -> Optional[int]:
    """Same fingerprint as :func:`perceptual_hash` but takes a (H, W, 3|4)
    numpy ndarray. Avoids the disk round-trip on the in-memory capture
    path. The bit pattern is interchangeable with the file-based
    function — Hamming distance is comparable across both.

    Implementation notes:
      * BT.601 luminance (0.299 R + 0.587 G + 0.114 B) computed in float32
        — the per-channel weights match what PIL's ``convert("L")`` would
        produce, so the two hashes can be cross-compared.
      * Nearest-neighbour resize via slicing (np.linspace + fancy index)
        — matches PIL's ``Image.NEAREST`` resampling.
      * Bit-pack against the mean, same as the file version.

    Returns None on any failure (numpy missing, malformed array, etc.) so
    callers can fall back to the conservative "treat as novel" path.
    """
    try:
        import numpy as np
    except Exception:
        return None
    try:
        if arr is None or arr.ndim < 2:
            return None
        if arr.ndim == 3 and arr.shape[2] >= 3:
            r = arr[..., 0].astype(np.float32)
            g = arr[..., 1].astype(np.float32)
            b = arr[..., 2].astype(np.float32)
            gray = 0.299 * r + 0.587 * g + 0.114 * b
        else:
            gray = arr.astype(np.float32)
        h, w = gray.shape[:2]
        if h <= 0 or w <= 0:
            return None
        ys = np.linspace(0, h - 1, downsample_px).astype(np.int32)
        xs = np.linspace(0, w - 1, downsample_px).astype(np.int32)
        small = gray[ys[:, None], xs[None, :]]
    except Exception:
        _logger.debug("perceptual_hash_array failed", exc_info=True)
        return None
    mean = float(small.mean())
    bits = 0
    flat = small.ravel().tolist()
    for i, v in enumerate(flat):
        if v >= mean:
            bits |= (1 << i)
    return bits


def hamming(a: int, b: int) -> int:
    """Bit count of (a XOR b) — the number of differing pixels."""
    return bin(a ^ b).count("1")


# ── Text shingling ─────────────────────────────────────────────────────────


def _shingle(text: str, *, n: int = 3) -> Set[str]:
    """Word-level n-gram set. Whitespace-tokenised, case-folded.

    For OCR'd UI text this works well because most screens have several
    distinctive multi-word phrases (window titles, menu items, error
    messages). 3-grams cover phrasing variations like "File > Open"
    vs "File Open" while still being discriminative.
    """
    tokens = text.lower().split()
    if len(tokens) < n:
        # Fall back to single tokens so very short OCR texts still get a
        # comparable set (Jaccard on identical short text is 1.0, which
        # is what we want — same screen).
        return set(tokens)
    return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def text_jaccard(a: str, b: str, *, n: int = 3) -> float:
    """Jaccard similarity in [0, 1]. 1.0 means identical shingle set."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    sa, sb = _shingle(a, n=n), _shingle(b, n=n)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def excerpt(text: str, max_chars: int) -> str:
    """Trim *text* to *max_chars*, preserving a leading whitespace marker
    when truncated. Caller is responsible for sanitising newlines.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"
