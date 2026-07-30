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

import httpx
from httpx import ConnectError
from openai import AsyncOpenAI

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
        # Holds the task that closes a stranded http client when SDK init
        # fails, so the cleanup task isn't garbage-collected mid-flight.
        self._cleanup_task: Optional[asyncio.Task] = None

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
        """Single HTTP roundtrip — shared by embed / embed_query.

        Retry policy: 3 attempts with 2/4/8s exponential backoff. Without
        this, a transient QGenie failure (cold-start past timeout, brief
        gateway 5xx) caused the warmup callsite in DreamWorker to swallow
        the exception and never re-attempt — leaving the chunk permanently
        missing an embedding until backfill happened to retry. Backfill
        runs at most every cycle, but it also has no retry, so persistent
        outages would still leak chunks. Retrying here gives every layer
        above us a much higher chance of seeing a successful embed.
        """
        self._ensure_client()
        if self._init_failed or self._client is None:
            raise RuntimeError("HttpApiEmbedder client failed to initialise")
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = await self._client.embeddings.create(
                    model=self._model,
                    input=texts,
                )
                out = [list(item.embedding) for item in resp.data]
                if len(out) != len(texts):
                    raise RuntimeError(
                        f"embedding count mismatch: requested {len(texts)}, got {len(out)}"
                    )
                return out
            except Exception as exc:
                last_exc = exc
                if attempt == 2:
                    # ConnectError on the final attempt is almost always
                    # cold-boot network-not-ready (VPN/proxy/DNS still
                    # warming up — recovers on its own within a few minutes).
                    # Log as WARNING so it doesn't pollute the ERROR stream;
                    # recall has already fallen back to BM25 for this query
                    # and the next dream tick will retry naturally.
                    is_connect_err = (
                        isinstance(exc, ConnectError)
                        or exc.__class__.__name__ in {"ConnectError", "APIConnectionError"}
                    )
                    if is_connect_err:
                        _logger.warning(
                            "embeddings.create unreachable after %d attempts "
                            "(model=%s, n=%d): %s — likely cold-boot network "
                            "not ready; will retry on next tick",
                            attempt + 1, self._model, len(texts), exc,
                        )
                    else:
                        _logger.exception(
                            "embeddings.create failed after %d attempts (model=%s, n=%d)",
                            attempt + 1, self._model, len(texts),
                        )
                    raise
                # 2s, 4s, (no third sleep). Bounded total wait ~6s, still
                # well under the dream worker's 60s tick.
                backoff = 2.0 * (2 ** attempt)
                _logger.warning(
                    "embeddings.create attempt %d/3 failed (model=%s, n=%d): %s; "
                    "retrying in %.1fs",
                    attempt + 1, self._model, len(texts), exc, backoff,
                )
                await asyncio.sleep(backoff)
        # Unreachable: the loop either returns or re-raises.
        if last_exc:
            raise last_exc
        raise RuntimeError("embeddings.create exited retry loop without result")

    # ── Lazy SDK init ───────────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        if self._client is not None or self._init_failed:
            return
        try:
            self._http = httpx.AsyncClient(
                verify=self._verify_ssl, timeout=self._timeout,
                # trust_env=False: do NOT honour HTTP(S)_PROXY / system proxy
                # env vars. The QGenie gateway is an INTERNAL address
                # (10.x.x.x); routing embedding calls through a corporate
                # proxy makes them fail (APIConnectionError / RemoteProtocol
                # / ReadTimeout), which silently degraded LTM recall to
                # BM25-only. Diagnosed live 2026-07-30: with the default
                # trust_env=True the SDK embed call died in ~2.8s; with
                # trust_env=False the same call returned a 1024-dim vector.
                # The bare chat path tolerated the proxy intermittently,
                # which is why only dense recall was affected.
                trust_env=False,
            )
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._endpoint,
                http_client=self._http,
                timeout=self._timeout,
                # AsyncOpenAI defaults to max_retries=2 (3 SDK attempts).
                # We already wrap _call in our own 3-attempt retry loop, so
                # without this the real attempt count is 3*3 = 9 per embed
                # — turning a transient ConnectError into a ~3.5min storm
                # before bubbling up. Keep retry policy in one layer.
                max_retries=0,
            )
        except Exception:
            _logger.exception(
                "HttpApiEmbedder client construction failed; "
                "recall will stay on FTS-only",
            )
            if self._http is not None:
                # We're called from _call() which is async, so a running
                # loop should exist. Use the modern API; if for some reason
                # there's no running loop (e.g. _ensure_client invoked from
                # a sync test harness), the http client will be cleaned up
                # when the process exits.
                try:
                    loop = asyncio.get_running_loop()
                    # Hold a reference so the task isn't GC'd before completion.
                    self._cleanup_task = loop.create_task(self._http.aclose())
                except RuntimeError:
                    pass
                self._http = None
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
