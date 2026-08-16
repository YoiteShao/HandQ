"""Local ONNX embedding provider — bge-small-zh-v1.5 via fastembed.

Same model + vendored assets as ``controller_v2/resume_index.py`` (not
imported from there: ``infrastructure`` must not depend on
``controller_v2``, so the small path-resolution helpers are duplicated —
same rationale ``resume_index.py`` itself gives for duplicating
``bridge_main._INSTALL_DIR``'s algorithm rather than importing it).

Threading: fastembed/onnxruntime is a blocking, synchronous call. The
``EmbeddingProvider`` ABC declares ``embed``/``embed_query`` as
``async def`` because callers (``triage.py``'s DreamWorker, and
``recall.py``'s chat-turn-hot-path ``embed_query``) ``await`` them
directly on the shared bridge event loop. Every model call here is
dispatched via ``asyncio.to_thread`` under ``self._lock`` — identical to
``resume_index.py``'s ``build()``/``search()`` pattern — so a batch embed
never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from .base import EmbeddingProvider

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_MODEL_DIMS = 512
_VENDORED_MODEL_DIRNAME = "bge-small-zh-v1.5"

# Batch size for every ``model.embed(...)`` call. MUST stay small.
#
# fastembed's default batch_size is 256. Because each batch pads to its
# longest member and transformer attention is O(batch x heads x seq^2), a
# large batch spikes RSS hard — and the onnxruntime CPU arena (which
# fastembed leaves ENABLED, unlike rapidocr) RETAINS that peak for the life
# of the process. Measured on resume_index's identical embedder: a 256-item
# batch drove RSS from a ~285MB floor to 4724MB and never released it;
# batch_size=1 held the transient embed peak to ~+31MB over the floor with
# no measurable time penalty, and bit-identical output vectors
# (max_abs_diff 0.0). See resume_index.EMBED_BATCH_SIZE for the full table.
#
# The DreamWorker backfills up to DREAM_BACKFILL_STARTUP (50) chunks per
# embed() call, each up to CHUNK_MAX_CHARS (800), so without this cap the
# LTM path would build the same oversized single batch at startup.
_EMBED_BATCH_SIZE = 1


def _install_dir() -> Path:
    """Directory next to the bridge entry point — same algorithm as
    ``bridge_main._INSTALL_DIR`` / ``resume_index.py._install_dir()``,
    independently implemented per this module's own layout
    (``src/infrastructure/long_term_memory/embedding/`` is three levels
    under the repo root)."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(__file__).parent.parent.parent.parent.parent.resolve()


def _vendored_model_dir() -> Path:
    return _install_dir() / "assets" / "models" / _VENDORED_MODEL_DIRNAME


def _model_cache_dir() -> Path:
    """Fallback cache_dir for fastembed's own HuggingFace-Hub download path
    — only reached when the vendored copy is absent (dev checkout that
    hasn't pulled assets/models/ yet). Pinned under the HandQ root, same
    as ``resume_index.py._model_cache_dir()``, instead of fastembed's
    default ``%TEMP%\\fastembed_cache`` (a dev-mode download should
    survive across HandQ upgrades, not vanish on a temp-dir sweep)."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / "HandQ" / "models"


class OnnxEmbedder(EmbeddingProvider):
    """Local bge-small-zh-v1.5 embedding via fastembed (pure onnxruntime,
    no torch, no network). Symmetric — bge has no query/document
    instruction-prefix convention (unlike Qwen3-Embedding), so
    ``embed_query`` is left at the ABC default (bare ``embed([text])[0]``)."""

    available = True

    def __init__(self) -> None:
        self._model: Optional[Any] = None
        self._lock = asyncio.Lock()

    # ── EmbeddingProvider interface ─────────────────────────────────────────

    @property
    def provider(self) -> str:  # type: ignore[override]
        return "onnx_local"

    @property
    def model(self) -> str:  # type: ignore[override]
        return _MODEL_NAME

    @property
    def dims(self) -> int:  # type: ignore[override]
        return _MODEL_DIMS

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Document-side embedding. Bare text, no decoration."""
        if not texts:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._embed_sync, texts)

    # ── Blocking half — only ever called via asyncio.to_thread ─────────────

    def _embed_sync(self, texts: List[str]) -> List[List[float]]:
        model = self._ensure_model()
        return [list(v) for v in model.embed(texts, batch_size=_EMBED_BATCH_SIZE)]

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            vendored = _vendored_model_dir()
            if vendored.is_dir():
                # specific_model_path: no HuggingFace cache-layout matching,
                # no network reachability check at all — the offline/
                # air-gapped path. assets/models/bge-small-zh-v1.5/ ships
                # with the build (electron/package.json build.extraFiles).
                self._model = TextEmbedding(
                    model_name=_MODEL_NAME,
                    specific_model_path=str(vendored),
                )
            else:
                # Dev-mode fallback — assets/models/ not pulled yet.
                self._model = TextEmbedding(
                    model_name=_MODEL_NAME,
                    cache_dir=str(_model_cache_dir()),
                )
        return self._model
