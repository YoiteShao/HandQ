"""HTTP-API embedding provider — OpenAI-compatible /v1/embeddings via QGenie.

Mirrors the pattern in :mod:`src.infrastructure.vision.llm`: an
``AsyncOpenAI`` client pointed at the QGenie gateway, sharing
``llm.API_KEY`` and tolerating the internal CA via ``verify_ssl=False``.

Failure policy:
- Transient errors (network, 5xx) propagate; recall.py degrades to BM25
  ordering for that one query.
- Persistent failures (auth, model not found): logged once on init, then
  ``available`` flips to False at runtime and recall stays on FTS-only.

Why not subclass VisionClient: vision wants chat completion + image
encoding; embedding wants /v1/embeddings + plain text. Different
responsibilities, no shared state worth merging.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from .base import EmbeddingProvider

_logger = logging.getLogger("handq.ltm.embedding.http")


class HttpApiEmbedder(EmbeddingProvider):
    """Calls an OpenAI-compatible ``/v1/embeddings`` endpoint."""

    available = True

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        dims: int,
        verify_ssl: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not endpoint or not api_key or not model:
            raise ValueError(
                "HttpApiEmbedder requires endpoint, api_key, and model "
                "(see _constants.py for defaults)"
            )
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._dims = int(dims)
        self._verify_ssl = verify_ssl
        self._timeout = timeout

        # Lazy: only build the underlying SDK / httpx clients on first call,
        # so import-time has no network cost. Same idiom as VisionClient.
        self._http: Any = None
        self._client: Any = None
        self._init_failed: bool = False

    # ── EmbeddingProvider interface ─────────────────────────────────────────

    @property
    def provider(self) -> str:  # type: ignore[override]
        return "http_api"

    @property
    def model(self) -> str:  # type: ignore[override]
        return self._model

    @property
    def dims(self) -> int:  # type: ignore[override]
        return self._dims

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Document-side embedding. Inputs are sent bare."""
        if not texts:
            return []
        return await self._call(texts)

    async def embed_query(self, text: str) -> List[float]:
        """Query-side embedding with Qwen3-style instruction prefix.

        Wraps the raw query text with the constants-defined instruction
        so the embedding lands closer to relevant documents in the same
        space. Verified live: prefixed queries widen the gap between the
        correct document and noise vs bare-query embedding.
        """
        from .. import _constants as C
        prefixed = (
            f"Instruct: {C.EMBEDDING_QUERY_INSTRUCTION}\nQuery: {text}"
        )
        out = await self._call([prefixed])
        return out[0] if out else []

    async def _call(self, texts: List[str]) -> List[List[float]]:
        """Single HTTP roundtrip — shared by embed / embed_query."""
        self._ensure_client()
        if self._init_failed or self._client is None:
            raise RuntimeError("HttpApiEmbedder client failed to initialise")
        try:
            resp = await self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
        except Exception:
            _logger.exception(
                "embeddings.create failed (model=%s, n=%d)",
                self._model, len(texts),
            )
            raise
        out = [list(item.embedding) for item in resp.data]
        if len(out) != len(texts):
            raise RuntimeError(
                f"embedding count mismatch: requested {len(texts)}, got {len(out)}"
            )
        return out

    # ── Lazy SDK init ───────────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        if self._client is not None or self._init_failed:
            return
        try:
            from openai import AsyncOpenAI
        except ImportError:
            _logger.error(
                "HttpApiEmbedder needs the `openai` package; recall will "
                "stay on FTS-only. Run: pip install openai",
            )
            self._init_failed = True
            self.available = False
            return
        try:
            import httpx
        except ImportError:
            _logger.error(
                "HttpApiEmbedder needs `httpx`; recall will stay on FTS-only.",
            )
            self._init_failed = True
            self.available = False
            return
        try:
            self._http = httpx.AsyncClient(
                verify=self._verify_ssl, timeout=self._timeout,
            )
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._endpoint,
                http_client=self._http,
                timeout=self._timeout,
            )
        except Exception:
            _logger.exception(
                "HttpApiEmbedder client construction failed; "
                "recall will stay on FTS-only",
            )
            self._init_failed = True
            self.available = False

    async def aclose(self) -> None:
        """Best-effort cleanup of the httpx client."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
            self._client = None
