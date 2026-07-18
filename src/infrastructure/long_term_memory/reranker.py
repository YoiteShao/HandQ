"""Reranker abstraction (3rd stage of the recall pipeline).

The recall pipeline mirrors yansu's three-stage design:

    ┌───────────────┐    ┌──────────────────┐    ┌─────────────────┐
    │ stage 1: FTS  │───▶│ stage 2: vector  │───▶│ stage 3: ML     │
    │ BM25 candi-   │    │ embedding cosine │    │ reranker (cross │
    │ date set      │    │ rerank           │    │ -encoder / LLM) │
    └───────────────┘    └──────────────────┘    └─────────────────┘

Each stage shrinks the candidate pool while improving precision.
``EmbeddingProvider`` covers stage 2 (see ``embedding/base.py``);
``Reranker`` covers stage 3.

Implementations:
- ``_NoOpReranker``    : disabled. recall pipeline skips stage 3.
- ``LlmReranker``      : sends top-N candidates + query to a helper LLM,
                         asks for relevance scores. First real
                         implementation in HandQ; default provider.
- ``CrossEncoderReranker`` (future): ONNX bge-reranker for offline /
                                      cost-sensitive deployments.
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import List

_logger = logging.getLogger("handq.ltm.reranker")


class Reranker(ABC):
    """Stage-3 reranker. Subclasses set ``available=True`` and implement
    ``rerank``. The contract: take (query, candidate_texts) → return new
    scores aligned with input order. Higher score = more relevant.
    """

    available: bool = False
    provider: str = "noop"
    model: str = "none"

    @abstractmethod
    async def rerank(self, query: str, candidate_texts: List[str]) -> List[float]:
        """Score each candidate against the query. Length must equal input."""
        ...


class _NoOpReranker(Reranker):
    """Disabled reranker — recall pipeline skips stage 3 when this is in use."""
    available = False
    provider = "noop"
    model = "none"

    async def rerank(self, query: str, candidate_texts: List[str]) -> List[float]:
        # Caller short-circuits on `available=False` and never reaches here.
        raise NotImplementedError("noop reranker has no implementation")


# ── LLM-as-reranker ─────────────────────────────────────────────────────────

_LLM_RERANK_SYSTEM = """\
You are a relevance scorer for a long-term memory recall system.

Given a query and a numbered list of candidate memory / knowledge entries,
score each candidate's relevance to the query on a 0.0–1.0 scale:

  1.0  = directly answers / matches the query intent
  0.7  = related and likely useful
  0.4  = tangentially related
  0.0  = irrelevant noise

Return ONLY a JSON object with a single key "scores":

{"scores": [0.95, 0.42, 0.10, 0.88, ...]}

The array MUST have exactly the same number of entries as the input list,
in the SAME order. No prose, no markdown fences, no explanations.

Conservatism: when in doubt, score lower. The downstream consumer
treats sub-0.3 candidates as noise."""

_LLM_RERANK_USER_TEMPLATE = """\
Query: {query}

Candidates ({n}):
{numbered_list}

Output the JSON object only."""


class LlmReranker(Reranker):
    """Use a chat LLM to rescore the fused candidate list.

    Cost: one LLM call per recall. At HandQ scale (one recall per task,
    bounded by RERANKER_INPUT_LIMIT candidates) this is ~$0.005/recall —
    cheap enough to enable by default when a caller wants it on.

    The implementation is deliberately decoupled from the DreamWorker's
    helper pool: we accept ``llm_services`` at construction so the
    factory can pass whatever pool the runtime decided to use (typically
    the same cheap tier as triage).
    """

    available = True
    provider = "llm"

    def __init__(
        self,
        *,
        llm_services: List,
        model_label: str = "",
        timeout_seconds: float = 30.0,
        input_limit: int = 15,
    ) -> None:
        self._services = list(llm_services)
        self.model = model_label or (
            getattr(self._services[0], "model", "unknown") if self._services else "none"
        )
        self._timeout = timeout_seconds
        self._input_limit = input_limit
        # Auto-disable if there is no underlying LLM service to call.
        if not self._services:
            self.available = False

    async def rerank(self, query: str, candidate_texts: List[str]) -> List[float]:
        if not candidate_texts:
            return []
        if not self._services:
            raise RuntimeError("LlmReranker constructed with no llm_services")

        # Cap input size — past ~20 the LLM scoring quality drops and the
        # prompt gets unwieldy. Caller upstream may pass more, we trim.
        capped = candidate_texts[: self._input_limit]

        from src.infrastructure.llm_pool import call_with_fallback

        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(capped))
        prompt_user = _LLM_RERANK_USER_TEMPLATE.format(
            query=query[:500],
            n=len(capped),
            numbered_list=numbered,
        )
        try:
            result = await asyncio.wait_for(
                call_with_fallback(
                    self._services,
                    dict(
                        messages=[
                            {"role": "system", "content": _LLM_RERANK_SYSTEM},
                            {"role": "user", "content": prompt_user},
                        ],
                        json_mode=True,
                        max_tokens=400,
                    ),
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            _logger.warning("LlmReranker timed out after %.1fs", self._timeout)
            raise

        scores = _parse_llm_scores(result.content or "", expected_n=len(capped))

        # Pad with 0.0 for any entries we trimmed via input_limit so the
        # caller's index alignment with the original list is preserved.
        if len(candidate_texts) > len(capped):
            scores = scores + [0.0] * (len(candidate_texts) - len(capped))
        return scores


def _parse_llm_scores(raw: str, *, expected_n: int) -> List[float]:
    """Tolerant parser for the rerank response.

    Accepts: bare JSON, fenced JSON, or JSON embedded in prose. Always
    returns a list of length expected_n; pads / truncates with 0.5
    (neutral) on length mismatch rather than raising, so a malformed
    LLM response degrades gracefully into "all candidates equally
    relevant" (the original fused order survives the sort).
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j <= i:
            _logger.warning("LlmReranker: no JSON object in response; using neutral scores")
            return [0.5] * expected_n
        s = s[i:j + 1]
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        _logger.warning("LlmReranker: invalid JSON; using neutral scores")
        return [0.5] * expected_n
    arr = d.get("scores")
    if not isinstance(arr, list):
        _logger.warning("LlmReranker: 'scores' not a list; using neutral scores")
        return [0.5] * expected_n

    out: List[float] = []
    for v in arr:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.5
        # Clamp to [0, 1] in case the LLM emits out-of-range values.
        out.append(max(0.0, min(1.0, f)))

    if len(out) < expected_n:
        out = out + [0.5] * (expected_n - len(out))
    elif len(out) > expected_n:
        out = out[:expected_n]
    return out


# ── Factory ─────────────────────────────────────────────────────────────────

def from_config(config: dict) -> Reranker:
    """Pick a reranker from :mod:`_constants`. ``config`` carries
    ``llm.API_KEY`` + ``llm.helper_models`` / ``llm.models`` for providers
    that need an LLM pool.
    """
    from . import _constants as C
    kind = C.RERANKER_PROVIDER

    if kind == C.RERANKER_NOOP:
        return _NoOpReranker()

    if kind == C.RERANKER_LLM:
        services = _build_llm_services_for_rerank(config)
        if not services:
            _logger.warning(
                "RERANKER_PROVIDER=%s but no LLM services available; "
                "falling back to noop", C.RERANKER_LLM,
            )
            return _NoOpReranker()
        return LlmReranker(
            llm_services=services,
            timeout_seconds=C.RERANKER_TIMEOUT_SECONDS,
            input_limit=C.RERANKER_INPUT_LIMIT,
        )

    # Forward-compatible: unknown provider degrades to no-op rather than crash.
    _logger.warning("unknown RERANKER_PROVIDER=%r; falling back to noop", kind)
    return _NoOpReranker()


def _build_llm_services_for_rerank(config: dict) -> List:
    """Build the LLM pool the reranker calls.

    Uses ``llm.helper_models`` (cheap pool for simple background tasks);
    falls back to ``llm.models`` when ``helper_models`` is empty. Latency
    matters here because rerank gates user-facing recall, so the cheaper
    helper pool is the right default.
    """
    try:
        from src.infrastructure.role_resolver import resolve_models_and_helper
        from src.infrastructure.anthropic_streaming_service import (
            AnthropicStreamingService,
        )
    except Exception:
        _logger.exception("failed to import LLM stack for rerank")
        return []

    llm_cfg = (config or {}).get("llm") or {}
    api_key = llm_cfg.get("API_KEY")
    if not api_key:
        return []

    main_models, helper_models = resolve_models_and_helper(llm_cfg)
    models = helper_models or main_models
    return [AnthropicStreamingService(model=m, api_key=api_key) for m in models]
