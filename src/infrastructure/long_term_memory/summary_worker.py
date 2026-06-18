"""Daily / weekly / monthly activity summary worker.

Generates obs_summaries rows by reading obs_semantic_events over a
period and asking an LLM to produce ``moments_json`` (top highlights)
and ``summary_text`` (narrative).

Why an in-worker cron (not the Scheduler)
-----------------------------------------
HandQ's ``src/infrastructure/scheduler/`` is the user-facing scheduler:
each task is a USER PROMPT that the bridge dispatches through the agent.
A summary worker is an LTM-internal background job — it doesn't produce
a user-visible "task being run". Forcing it through Scheduler would
require a sentinel prompt and special-case routing in the bridge's
``dispatch`` handler.

Cleaner: this worker self-clocks via an hourly poll inside ``run()``
that checks the wall-clock 22:00 deadline + "is yesterday's row absent
from obs_summaries". The cadence guarantees are equivalent to a cron
(daily ≥22:00 yesterday-rollup, idempotent on restart) without the
Scheduler vocabulary mismatch.

For ad-hoc generation, the IPC layer can call ``run_one_period``
directly via the (future) admin command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

_logger = logging.getLogger("handq.ltm.summary_worker")

MAX_EVENTS_PER_PROMPT: int = 60
TOP_MOMENTS: int = 10


class SummaryWorker:
    """Periodic worker that rolls up obs_semantic_events into obs_summaries.

    Two modes:
      * Background loop: wakes every WORKER_TICK and checks if the daily
        deadline has been crossed; produces yesterday's summary once per day.
      * Scheduler-cron triggered: external scheduler can call
        ``run_one_period(date_iso, type_)`` directly.
    """

    WORKER_TICK_SECONDS: float = 3600.0  # check once per hour

    def __init__(self, store, llm_services: Optional[list] = None) -> None:
        self._store = store
        self._llm_services = llm_services or []
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        _logger.info(
            "SummaryWorker started (tick=%.0fs, llm_available=%s)",
            self.WORKER_TICK_SECONDS, bool(self._llm_services),
        )
        try:
            while not self._stopped.is_set():
                try:
                    await self._maybe_run_daily()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("SummaryWorker tick failed")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=self.WORKER_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            _logger.info("SummaryWorker cancelled")
            raise

    def stop(self) -> None:
        self._stopped.set()

    async def _maybe_run_daily(self) -> None:
        """Produce yesterday's summary if today is fresh & yesterday isn't done."""
        now = datetime.now()
        # Only run after 22:00 local; before that the day isn't "over"
        if now.hour < 22:
            return
        yesterday = (now - timedelta(days=1)).date().isoformat()
        existing = await self._store.get_obs_summary(date=yesterday, type_="daily")
        if existing:
            return
        await self.run_one_period(date_iso=yesterday, type_="daily")

    async def run_one_period(self, *, date_iso: str, type_: str = "daily") -> bool:
        """Generate one (date, type) summary. Idempotent via ON CONFLICT."""
        try:
            t_start, t_end = _period_range(date_iso, type_)
        except ValueError:
            _logger.warning("invalid period date_iso=%s type=%s", date_iso, type_)
            return False
        events = await self._store._fetchall(
            "SELECT id, title, description, category, entities, apps, "
            "time_range_start, time_range_end, task_worthy "
            "FROM obs_semantic_events "
            "WHERE time_range_end >= ? AND time_range_end < ? "
            "ORDER BY time_range_end ASC LIMIT ?",
            (t_start, t_end, MAX_EVENTS_PER_PROMPT * 2),
        )
        if not events:
            _logger.info("no semantic events for %s %s", type_, date_iso)
            return False
        # Trim to MAX_EVENTS_PER_PROMPT, keeping task-worthy ones first.
        worthy = [e for e in events if e[8]]
        other = [e for e in events if not e[8]]
        keep = (worthy + other)[:MAX_EVENTS_PER_PROMPT]

        if self._llm_services:
            try:
                moments, narrative, model = await self._llm_rollup(
                    keep, type_=type_, date_iso=date_iso,
                )
            except Exception:
                _logger.exception("LLM rollup failed; using heuristic")
                moments, narrative, model = self._heuristic_rollup(keep, date_iso, type_)
        else:
            moments, narrative, model = self._heuristic_rollup(keep, date_iso, type_)

        await self._store.upsert_obs_summary(
            date=date_iso, type_=type_,
            moments=moments, summary_text=narrative,
            generated_model=model,
        )
        _logger.info(
            "summary written: %s %s — %d moments (%d events sampled)",
            type_, date_iso, len(moments), len(keep),
        )
        return True

    async def _llm_rollup(
        self, events: list, *, type_: str, date_iso: str,
    ) -> tuple:
        """Call LLM to produce moments + narrative."""
        # Compact event projection for the prompt
        rows = []
        for ev in events:
            _id, title, desc, cat, ents_json, apps_json, t_start, t_end, worthy = ev
            try:
                ents = json.loads(ents_json) if ents_json else []
            except (json.JSONDecodeError, TypeError, ValueError):
                ents = []
            rows.append({
                "title": title, "category": cat or "other",
                "entities": ents[:5],
                "started_at": datetime.fromtimestamp(int(t_start) / 1000).isoformat(),
            })
        prompt = (
            f"You are summarizing a user's {type_} computer activity. "
            f"Date: {date_iso}.\n\n"
            f"Events (in chronological order):\n"
            f"{json.dumps(rows, ensure_ascii=False, indent=2)[:6000]}\n\n"
            f"Return STRICT JSON:\n"
            f"  moments: list of up to {TOP_MOMENTS} highlight moments, each "
            f"    {{title, time (ISO), summary, importance:0-1}}\n"
            f"  summary_text: 2-4 sentence narrative of the {type_}\n"
        )
        svc = self._llm_services[0]
        model = getattr(svc, "model", "unknown")
        text = await _llm_chat_text(svc, [{"role": "user", "content": prompt}])
        if not text:
            return [], "", model
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            parsed = json.loads(text)
            return (
                parsed.get("moments", [])[:TOP_MOMENTS],
                parsed.get("summary_text", ""),
                model,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return [], "", model

    @staticmethod
    def _heuristic_rollup(events: list, date_iso: str, type_: str) -> tuple:
        """No-LLM fallback: top-N task-worthy events become moments verbatim."""
        moments: list = []
        worthy = [e for e in events if e[8]][:TOP_MOMENTS]
        for ev in worthy:
            _id, title, desc, cat, _ents, _apps, t_start, _t_end, _worthy = ev
            moments.append({
                "title": title[:140],
                "time": datetime.fromtimestamp(int(t_start) / 1000).isoformat(),
                "summary": (desc or "")[:200],
                "importance": 0.5,
                "category": cat or "other",
            })
        narrative = (
            f"{type_.capitalize()} summary for {date_iso}: "
            f"{len(events)} semantic events recorded, "
            f"{len(worthy)} flagged task-worthy."
        )
        return moments, narrative, "heuristic"


async def _llm_chat_text(svc, messages: list) -> Optional[str]:
    """Drive AnthropicStreamingService.chat_stream and return result.content."""
    if not hasattr(svc, "chat_stream"):
        return None
    try:
        async for ev in svc.chat_stream(messages=messages, json_mode=False):
            result = getattr(ev, "result", None)
            if result is not None:
                return getattr(result, "content", None)
    except Exception:
        _logger.exception("LLM chat_stream failed in summary_worker")
    return None


def _period_range(date_iso: str, type_: str) -> tuple:
    """Return (start_ms, end_ms) for a (date_iso, type) period in unix ms."""
    if type_ == "daily":
        d = date.fromisoformat(date_iso)
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1)
    elif type_ == "weekly":
        # date_iso expected as YYYY-W## per plan; tolerate ISO date too.
        if "W" in date_iso:
            year, _, week = date_iso.partition("-W")
            d = date.fromisocalendar(int(year), int(week), 1)
        else:
            d = date.fromisoformat(date_iso)
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=7)
    elif type_ == "monthly":
        # YYYY-MM
        year, _, month = date_iso.partition("-")
        d = date(int(year), int(month), 1)
        start = datetime(d.year, d.month, d.day)
        if d.month == 12:
            end = datetime(d.year + 1, 1, 1)
        else:
            end = datetime(d.year, d.month + 1, 1)
    else:
        raise ValueError(f"unknown type_: {type_!r}")
    # obs_semantic_events.time_range_start/end are stored in unix
    # MILLISECONDS (inherited from obs_snapshots.captured_at, which the
    # observer writes as int(ts * 1000)), so the period bounds must be
    # milliseconds too — otherwise the time_range_end window never matches.
    return (int(start.timestamp() * 1000), int(end.timestamp() * 1000))
