"""LLM-based schedule inference (single-use, scheduler-internal).

Surface: ``infer_schedule(prompt, config) -> InferResult``.

Design:

  * The bridge does NOT hold a long-lived LLM service for schedule
    inference — it's a rare event (only on ``cron_create``) and a
    permanent httpx pool would be wasted state.
  * Each ``infer_schedule`` call resolves ``llm.helper_models`` (with
    fallback to ``llm.models``) via :func:`role_resolver.resolve_models_and_helper`,
    builds one :class:`AnthropicStreamingService` per model, drives them
    through :func:`llm_pool.call_with_fallback`, and closes them all
    when the call returns.

The LLM is asked to return a JSON object ``{"schedule": "...",
"prompt": "..."}``:

  * ``schedule`` is the cadence in our grammar (recurring or one-shot).
    The current local time is injected into the prompt so the LLM can
    resolve relative expressions like "明天9点" or "in 5 minutes".
  * ``prompt`` is the original task with the time/cadence language
    stripped. The bridge feeds *that* to the agent at fire-time so the
    agent doesn't re-interpret "一分钟后…" and add a second delay on
    top of the schedule that already absorbed it.

Output is parsed via :func:`utils.try_parse_json` (handles fenced
blocks, prose, json-repair quirks). The schedule is normalised
(``once in N`` → ``once at <abs>``) and re-validated through
:func:`scheduler.schedule.parse_schedule` to refuse anything we can't
actually run.

If anything goes wrong (no API key, no helper / main models, parse
failure, every fallback exhausted, timeout, malformed grammar from
the LLM) we fall back to ``daily 09:00`` and the *original* prompt
so :func:`store.create` never refuses purely because the LLM hiccupped.
Daily 9am is a safe default — pinned to a known time, won't burn
quota, and the user can immediately see the wrong schedule in the
panel and recreate.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, List, NamedTuple, Optional

from .schedule import normalize_schedule, parse_schedule

_logger = logging.getLogger("handq.scheduler.inferer")

_FALLBACK_SCHEDULE = "daily 09:00"


class InferResult(NamedTuple):
    """Output of :func:`infer_schedule`.

    - ``schedule``: validated (and one-shot-normalised) schedule string.
    - ``prompt``  : the user-facing prompt with time/cadence language
                    stripped. Empty string is allowed and means "fall
                    back to the original prompt at dispatch time" — the
                    bridge's :meth:`accept_scheduled_task` honours that.
    - ``ok``      : True when the schedule was genuinely inferred from the
                    prompt; False when inference failed and we fell back to
                    ``daily 09:00``. The bridge surfaces this to the UI so a
                    silent fallback (e.g. a transient LLM blip that turns
                    "1分钟后…" into "daily 09:00") is VISIBLE, not buried.
    """
    schedule: str
    prompt: str
    ok: bool = True


_INFERENCE_MAX_TOKENS = 256
_INFERENCE_TIMEOUT_SECONDS = 20.0

_USER_TEMPLATE = """You produce a schedule string AND a cleaned task prompt.

The user's current local time is: {now_iso} ({now_dow})

Allowed schedule grammar (pick exactly one):
  every N seconds                     (N >= 1, recurring)
  every N minutes                     (N >= 1, recurring)
  every N hours                       (N >= 1, recurring)
  daily HH:MM                         (24-hour local time, recurring)
  daily HH:MM:SS
  weekly DOW HH:MM                    (DOW = mon|tue|wed|thu|fri|sat|sun)
  weekly DOW HH:MM:SS
  once in N seconds                   (one-shot, relative to "now" above)
  once in N minutes
  once in N hours
  once at YYYY-MM-DD HH:MM            (one-shot, absolute local time)
  once at YYYY-MM-DD HH:MM:SS

Examples:
  task: "Summarise yesterday's PRs"
    -> {{"schedule": "daily 09:00", "prompt": "Summarise yesterday's PRs"}}
  task: "Send weekly status report"
    -> {{"schedule": "weekly fri 17:00", "prompt": "Send weekly status report"}}
  task: "Check CI every 30 minutes"
    -> {{"schedule": "every 30 minutes", "prompt": "Check CI"}}
  task: "Heartbeat every 30 seconds"
    -> {{"schedule": "every 30 seconds", "prompt": "Heartbeat"}}
  task: "一分钟后告诉我teams有哪些人找我"
    -> {{"schedule": "once in 1 minute", "prompt": "告诉我teams有哪些人找我"}}
  task: "明天9点检查邮件"
    -> {{"schedule": "once at <tomorrow's date> 09:00", "prompt": "检查邮件"}}

Rules for "prompt":
- It is the task description with all time/cadence language removed.
- Will be fed verbatim to an agent at fire-time, so the agent must
  read it as "do this NOW" — no relative-time words ("明天", "一小时
  后", "每5分钟", "in 5 minutes", etc.).
- Keep it in the same language the user wrote in.
- If the user did not include any time language, copy the task as-is.

Task: "{prompt}"

Return ONLY a JSON object with both keys:

{{"schedule": "<one of the allowed grammar forms>", "prompt": "<cleaned task>"}}

No prose, no markdown fences."""


def _build_llm_services(config: dict) -> List[Any]:
    """Build the LLM service pool for schedule inference.

    Pulls from ``llm.helper_models`` (cheap pool for simple background tasks);
    falls back to ``llm.models`` when ``helper_models`` is empty so a config
    without an explicit helper pool still works.

    The caller is responsible for closing each returned service.
    """
    try:
        from src.infrastructure.role_resolver import resolve_models_and_helper
        from src.infrastructure.llm_service_factory import create_llm_service
    except Exception:
        _logger.exception("failed to import LLM stack for schedule inference")
        return []

    llm_cfg = (config or {}).get("llm") or {}
    api_key = llm_cfg.get("API_KEY")
    if not api_key:
        return []

    main_models, helper_models = resolve_models_and_helper(llm_cfg)
    models = helper_models or main_models
    if not models:
        _logger.warning(
            "schedule inference: helper_models and models both empty",
        )
        return []

    services: List[Any] = []
    for m in models:
        try:
            services.append(create_llm_service(
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


def _fallback(prompt: str) -> InferResult:
    """Fallback shape — daily 9am with the original prompt verbatim.

    ``ok=False`` marks this as a NON-inferred result so the bridge/UI can warn
    the user that the requested timing wasn't understood.
    """
    return InferResult(schedule=_FALLBACK_SCHEDULE, prompt=prompt or "", ok=False)


async def infer_schedule(prompt: str, config: dict) -> InferResult:
    """Build a one-shot LLM service pool from *config*, ask it (with
    fallback across the pool) to infer a schedule + cleaned prompt for
    *prompt*, then close every service.

    Always returns an :class:`InferResult` with a schedule string
    compatible with :func:`scheduler.schedule.parse_schedule` (after
    :func:`scheduler.schedule.normalize_schedule`). On any failure the
    schedule falls back to ``daily 09:00`` and the cleaned prompt
    falls back to the original — the bridge then normalises and stores
    without juggling exception paths.
    """
    if not prompt or not prompt.strip():
        return _fallback(prompt)

    services = _build_llm_services(config)
    if not services:
        _logger.warning("schedule inference: no LLM services; using fallback")
        return _fallback(prompt)

    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    now_dow = now.strftime("%A").lower()  # e.g. "tuesday"

    try:
        from src.infrastructure.llm_pool import call_with_fallback

        chat_kwargs = dict(
            messages=[
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        prompt=prompt.strip(),
                        now_iso=now_iso,
                        now_dow=now_dow,
                    ),
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
            return _fallback(prompt)

        # Same JSON-parse pipeline used by INTENT / agent decision calls:
        # handles fenced blocks, prose wrappers, json_repair
        # tricks. Returns dict on success, original str on failure.
        from src.infrastructure.utils import try_parse_json
        parsed = try_parse_json(result.content or "")
        if not isinstance(parsed, dict):
            _logger.warning(
                "schedule inference: response not parseable JSON: %r",
                (result.content or "")[:200],
            )
            return _fallback(prompt)

        candidate = str(parsed.get("schedule") or "").strip()
        if not candidate:
            _logger.warning(
                "schedule inference: missing 'schedule' key in JSON: %r",
                parsed,
            )
            return _fallback(prompt)

        # Normalise relative one-shot ("once in 1 minute") into absolute
        # ("once at 2026-06-02 14:31:00") before validation. parse_schedule
        # is strict about absolutes to avoid re-anchoring on every call.
        normalised = normalize_schedule(candidate)

        # Final gate — refuse anything parse_schedule can't handle.
        parse_schedule(normalised)

        cleaned = str(parsed.get("prompt") or "").strip()
        # The model is allowed to return the prompt verbatim when
        # there's no time language to strip. We only fall back to the
        # original if the model omitted the field entirely.
        dispatch_prompt = cleaned if cleaned else prompt.strip()

        _logger.info(
            "inferred schedule: prompt=%r → schedule=%r, dispatch=%r",
            prompt[:60], normalised, dispatch_prompt[:60],
        )
        return InferResult(schedule=normalised, prompt=dispatch_prompt)
    except Exception as exc:
        _logger.warning(
            "schedule inference failed (%s); falling back to %r",
            exc, _FALLBACK_SCHEDULE,
        )
        return _fallback(prompt)
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
