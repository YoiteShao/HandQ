"""Embedding provider base + cheap helpers.

Vectors are stored in embedding_cache.embedding as raw float32 bytes.
We use struct.pack/unpack rather than numpy so the runtime stays
dependency-free and Nuitka build size unchanged.
"""
from __future__ import annotations

import logging
import math
import struct
from abc import ABC, abstractmethod
from typing import Iterable, List, Sequence

_logger = logging.getLogger("handq.ltm.embedding")


class EmbeddingProvider(ABC):
    """Subclasses set ``available=True`` and implement ``embed``.

    Asymmetric retrieval: Qwen3-Embedding (and most modern bge-style
    encoders) want different prompting on the query side vs the document
    side. Subclasses override ``embed_query`` to add per-query
    instructions; ``embed`` handles documents (chunks at ingest time)
    bare. The default ``embed_query`` falls back to ``embed`` so a
    symmetric provider works without override.
    """

    available: bool = False
    provider: str = "fts_only"
    model: str = "none"
    dims: int = 0

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Document-side embedding. Bare text, no decoration."""
        ...

    async def embed_query(self, text: str) -> List[float]:
        """Query-side embedding. Override to add instruction prefix."""
        out = await self.embed([text])
        return out[0] if out else []


class _FTSOnlyProvider(EmbeddingProvider):
    """Disabled provider — recall falls back to BM25 only."""
    available = False
    provider = "fts_only"   # mirrored from _constants.PROVIDER_FTS_ONLY
    model = "none"
    dims = 0

    async def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError("fts_only provider does not produce vectors")


def from_config(config: dict) -> EmbeddingProvider:
    """Pick a provider from :mod:`_constants`.

    The provider choice is intentionally NOT user-configurable through
    yaml — it lives in :mod:`_constants` to keep tuning out of the
    user-facing surface. ``config`` is still accepted because we read
    ``llm.API_KEY`` from it (the only embedding parameter that genuinely
    must come from the user's own config).
    """
    from .. import _constants as C

    kind = C.EMBEDDING_PROVIDER

    if kind == C.PROVIDER_FTS_ONLY:
        return _FTSOnlyProvider()

    if kind == C.PROVIDER_HTTP_API:
        api_key = (config.get("llm") or {}).get("API_KEY")
        if not api_key:
            _logger.warning(
                "EMBEDDING_PROVIDER=%s but llm.API_KEY missing; "
                "falling back to %s",
                C.PROVIDER_HTTP_API, C.PROVIDER_FTS_ONLY,
            )
            return _FTSOnlyProvider()
        try:
            from .http_api import HttpApiEmbedder
        except Exception:
            _logger.exception(
                "failed to import HttpApiEmbedder; falling back to %s",
                C.PROVIDER_FTS_ONLY,
            )
            return _FTSOnlyProvider()
        try:
            return HttpApiEmbedder(
                endpoint=C.QGENIE_BASE_URL,
                api_key=api_key,
                model=C.EMBEDDING_MODEL,
                dims=C.EMBEDDING_DIMS,
                verify_ssl=C.QGENIE_VERIFY_SSL,
                timeout=C.EMBEDDING_TIMEOUT_SECONDS,
            )
        except Exception:
            _logger.exception(
                "HttpApiEmbedder construction failed; falling back to %s",
                C.PROVIDER_FTS_ONLY,
            )
            return _FTSOnlyProvider()

    if kind == C.PROVIDER_ONNX_LOCAL:
        try:
            from .onnx_local import OnnxEmbedder
            return OnnxEmbedder()
        except Exception:
            _logger.exception(
                "OnnxEmbedder construction failed; falling back to %s",
                C.PROVIDER_FTS_ONLY,
            )
            return _FTSOnlyProvider()

    _logger.warning(
        "unknown EMBEDDING_PROVIDER=%r; falling back to %s",
        kind, C.PROVIDER_FTS_ONLY,
    )
    return _FTSOnlyProvider()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def vec_to_bytes(vec: Iterable[float]) -> bytes:
    arr = list(vec)
    return struct.pack(f"{len(arr)}f", *arr)


def vec_from_bytes(b: bytes) -> List[float]:
    if not b:
        return []
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))
