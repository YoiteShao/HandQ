"""Shared model-prefix routing for LLM service construction.

Every caller that turns a config model string (``"azure::gpt-5.4"``,
``"anthropic::claude-4-5-haiku"``, ...) into a concrete :class:`LLMService`
must agree on which adapter each prefix family maps to — an Azure/OpenAI
model wrapped in :class:`AnthropicStreamingService` silently sends
``temperature`` on every request, which reasoning models like
``azure::gpt-5.4`` reject with a 400.

:func:`create_llm_service` is the single source of truth for that mapping.
``src.bridge.stdio_bridge`` uses it for the main agent/session pools;
background subsystems (schedule inference, LTM rerank/triage/retriage) must
use it too instead of hardcoding an adapter class.
"""
from __future__ import annotations

import logging
from typing import Any

from .anthropic_streaming_service import AnthropicStreamingService
from .llm_service import LLMService
from .openai_streaming_service import OpenAIStreamingService

_logger = logging.getLogger("handq.llm_service_factory")


def create_llm_service(api_key: str, model: str, **kwargs: Any) -> LLMService:
    """Create the concrete LLM service for *model* based on its provider prefix.

    HandQ stores model identifiers directly in YAML. The prefix is used only
    to choose the adapter; the full model string is still passed through to
    the backend because the YOUR-AI-ENDPOINT OpenAI-compatible endpoint uses
    identifiers such as ``azure::gpt-5.4``.

    Routing is STRICT and bidirectional — every known prefix family has an
    explicit branch so there is no silent fall-through that could wrap an
    Azure/OpenAI model in :class:`AnthropicStreamingService` (or vice versa).
    Unknown prefixes default to :class:`OpenAIStreamingService` with a logged
    warning.

    ``**kwargs`` (e.g. ``max_tokens``, ``temperature``, ``max_retries``) is
    forwarded unchanged to whichever adapter constructor is selected — both
    accept the same superset of keyword arguments.
    """
    model_lower = (model or "").lower()

    # ── Anthropic models (served via Anthropic API / YOUR-AI-ENDPOINT Bedrock proxy) ──
    if model_lower.startswith("anthropic::"):
        return AnthropicStreamingService(api_key=api_key, model=model, **kwargs)

    # ── OpenAI-compatible models (Azure, OpenAI direct, bare GPT/o-series) ──
    if (
        model_lower.startswith("azure::")
        or model_lower.startswith("openai::")
        or model_lower.startswith("gpt-")
        or model_lower.startswith(("o1", "o3", "o4"))
    ):
        return OpenAIStreamingService(api_key=api_key, model=model, **kwargs)

    # ── Unknown prefix — warn and fall back to OpenAI-compatible ────────────
    _logger.warning(
        "create_llm_service: unrecognised model prefix for %r; "
        "defaulting to OpenAIStreamingService — add an explicit branch if "
        "this is a new provider family.",
        model,
    )
    return OpenAIStreamingService(api_key=api_key, model=model, **kwargs)
