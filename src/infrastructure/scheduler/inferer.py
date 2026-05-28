"""LLM-based schedule inference (single-use, scheduler-internal).

Surface: ``infer_schedule(prompt, config) -> str``.

Design mirrors :mod:`long_term_memory.reranker`'s factory pattern:

  * The bridge does NOT hold a long-lived LLM service for schedule
    inference — it's a rare event (only on ``cron_create``) and a
    permanent httpx pool would be wasted state.
  * Each ``infer_schedule`` call resolves the ``from_data`` role
    (UI label: ``helper``) via :func:`role_resolver.resolve_role_models`,
    builds one :class:`AnthropicStreamingService` per model in that
    role, drives them through :func:`llm_pool.call_with_fallback`, and
    closes them all when the call returns.

The LLM is asked to return a JSON object ``{"schedule": "..."}``;
output is parsed via :func:`utils.try_parse_json` (handles fenced
blocks, prose, json-repair quirks). The extracted schedule string is
re-validated through :func:`scheduler.schedule.parse_schedule` to
refuse anything we can't actually run.

If anything goes wrong (no API key, no models in from_data/agent
roles, parse failure, every fallback exhausted, timeout, malformed
grammar from the LLM) we fall back to ``daily 09:00`` so
:func:`store.create` never refuses purely because the LLM hiccupped.
Daily 9am is a safe default — pinned to a known time, won't burn
quota, and the user can immediately see the wrong schedule in the
panel and recreate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

from .schedule import parse_schedule

_logger = logging.getLogger("handq.scheduler.inferer")

_FALLBACK = "daily 09:00"

_INFERENCE_MAX_TOKENS = 96
_INFERENCE_TIMEOUT_SECONDS = 20.0

_USER_TEMPLATE = """You produce a schedule string for a recurring task.

Allowed grammar (pick exactly one):
  every N seconds                     (N >= 1)
  every N minutes                     (N >= 1)
  every N hours                       (N >= 1)
  daily HH:MM                         (24-hour local time)
  daily HH:MM:SS
  weekly DOW HH:MM                    (DOW = mon|tue|wed|thu|fri|sat|sun)
  weekly DOW HH:MM:SS

Examples:
  task: "Summarise yesterday's PRs"            -> {{"schedule": "daily 09:00"}}
  task: "Send weekly status report"            -> {{"schedule": "weekly fri 17:00"}}
  task: "Check CI every 30 minutes"            -> {{"schedule": "every 30 minutes"}}
  task: "Monitor server health"                -> {{"schedule": "every 5 minutes"}}
  task: "Heartbeat every 30 seconds"           -> {{"schedule": "every 30 seconds"}}

Task: "{prompt}"

Return ONLY a JSON object with a single key "schedule":

{{"schedule": "<one of the allowed grammar forms>"}}

No prose, no markdown fences."""


def _build_llm_services(config: dict) -> List[Any]:
    """Build the LLM service pool for schedule inference.

    Resolution mirrors :func:`long_term_memory.reranker._build_llm_services_for_rerank`:
      1. Need ``llm.API_KEY`` — return [] otherwise (caller falls back).
      2. Pull models from the ``from_data`` role (UI label: ``helper``);
         if empty, fall back to the ``agent`` role so we can still
         answer when the user only filled in agent-tier models.
      3. Construct one :class:`AnthropicStreamingService` per model —
         :func:`llm_pool.call_with_fallback` will try them in order.

    The caller is responsible for closing each returned service.
    """
    try:
        from src.infrastructure.role_resolver import resolve_role_models
        from src.infrastructure.anthropic_streaming_service import (
            AnthropicStreamingService,
        )
    except Exception:
        _logger.exception("failed to import LLM stack for schedule inference")
        return []

    llm_cfg = (config or {}).get("llm") or {}
    api_key = llm_cfg.get("API_KEY")
    if not api_key:
        return []

    roles = resolve_role_models(llm_cfg)
    models = list(roles.get("from_data") or [])
    if not models:
        models = list(roles.get("agent") or [])
    if not models:
        _logger.warning(
            "schedule inference: from_data and agent roles both empty",
        )
        return []

    services: List[Any] = []
    for m in models:
        try:
            services.append(AnthropicStreamingService(
                api_key=api_key,
                model=m,
                max_tokens=_INFERENCE_MAX_TOKENS,
                temperature=0.0,                  # deterministic output
                max_retries=2,
            ))
        except Exception:
            _logger.exception(
                "failed to construct schedule-inference service for model %r", m,
            )
    return services


async def infer_schedule(prompt: str, config: dict) -> str:
    """Build a one-shot LLM service pool from *config*, ask it (with
    fallback across the pool) to infer a schedule for *prompt*, then
    close every service.

    Always returns a string compatible with
    :func:`scheduler.schedule.parse_schedule`. Falls back to
    ``daily 09:00`` on any error so callers can save the task without
    juggling exception paths.
    """
    if not prompt or not prompt.strip():
        return _FALLBACK

    services = _build_llm_services(config)
    if not services:
        _logger.warning("schedule inference: no LLM services; using fallback")
        return _FALLBACK

    try:
        from src.infrastructure.llm_pool import call_with_fallback

        chat_kwargs = dict(
            messages=[
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(prompt=prompt.strip()),
                },
            ],
            json_mode=True,
            max_tokens=_INFERENCE_MAX_TOKENS,
            temperature=0.0,
        )

        try:
            result = await asyncio.wait_for(
                call_with_fallback(
                    services,
                    chat_kwargs,
                    on_fallback=lambda idx, exc: _logger.warning(
                        "schedule inference: service[%d] failed (%s); "
                        "trying next", idx - 1, exc,
                    ),
                ),
                timeout=_INFERENCE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "schedule inference timed out after %.1fs; falling back",
                _INFERENCE_TIMEOUT_SECONDS,
            )
            return _FALLBACK

        # Same JSON-parse pipeline used by planner / receptionist /
        # decision: handles fenced blocks, prose wrappers, json_repair
        # tricks. Returns dict on success, original str on failure.
        from src.infrastructure.utils import try_parse_json
        parsed = try_parse_json(result.content or "")
        if not isinstance(parsed, dict):
            _logger.warning(
                "schedule inference: response not parseable JSON: %r",
                (result.content or "")[:200],
            )
            return _FALLBACK

        candidate = str(parsed.get("schedule") or "").strip()
        if not candidate:
            _logger.warning(
                "schedule inference: missing 'schedule' key in JSON: %r",
                parsed,
            )
            return _FALLBACK

        # Final gate — refuse anything parse_schedule can't handle.
        parse_schedule(candidate)
        _logger.info(
            "inferred schedule: prompt=%r → %r",
            prompt[:60], candidate,
        )
        return candidate
    except Exception as exc:
        _logger.warning(
            "schedule inference failed (%s); falling back to %r",
            exc, _FALLBACK,
        )
        return _FALLBACK
    finally:
        # Single-use: drain every httpx pool so we don't leave dangling
        # keep-alive connections.
        for svc in services:
            try:
                await svc.close()
            except Exception:
                _logger.warning(
                    "schedule inference: service.close failed",
                    exc_info=True,
                )
