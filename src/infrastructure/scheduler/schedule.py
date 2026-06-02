"""Schedule grammar parser.

We deliberately reject full crontab syntax — it's overkill for the
HandQ surface and makes the parsing code longer than the rest of the
scheduler combined. These forms cover every realistic scheduled-prompt
use case we've seen:

    "every <N> seconds"   →  N >= 1 (subject to SCHEDULER_MIN_INTERVAL_SEC)
    "every <N> minutes"   →  N >= 1
    "every <N> hours"     →  N >= 1
    "daily HH:MM[:SS]"    →  fires once per local day at the given clock
    "weekly <DOW> HH:MM[:SS]" → DOW = mon..sun; once per week
    "once at YYYY-MM-DD HH:MM[:SS]" → one-shot; fires once at that local time
    "interval <S>"        →  raw seconds (>= SCHEDULER_MIN_INTERVAL_SEC).
                             Mostly for tests; users don't see this.

Relative one-shot forms ("once in N seconds/minutes/hours") are NOT
accepted by ``parse_schedule`` directly — they're time-of-call
dependent and would re-anchor on every call. Use
:func:`normalize_schedule` to convert them to ``once at <abs>`` before
storing; the bridge does this in its ``cron_create`` handler.

Anything else raises ``ScheduleSyntaxError`` so the IPC layer can
surface the message to the renderer.

Calculation: ``next_run_at`` always returns an integer unix-seconds.
For "every X" forms the next fire is ``last + interval`` (or now+interval
when last is 0). For "daily" / "weekly" forms we anchor to local time
and roll forward to the next future occurrence. For "once at" the
returned timestamp is the absolute fire moment (may be in the past if
the bridge was offline when it was due — the scheduler will catch up
on next boot, then disable the task via :meth:`store.mark_finished`).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..long_term_memory import _constants as C


class ScheduleSyntaxError(ValueError):
    """Raised when a schedule string can't be parsed."""


_DOWS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class _ParsedSchedule:
    """Internal AST. Exactly one of (interval_seconds, daily_hms,
    weekly_dow_hms, fire_at_unix) is set."""
    interval_seconds: Optional[int] = None
    daily_hms: Optional[tuple] = None         # (h, m, s)
    weekly: Optional[tuple] = None            # (dow_idx, h, m, s)
    fire_at_unix: Optional[int] = None        # one-shot: absolute unix-seconds


_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
}


def normalize_schedule(spec: str) -> str:
    """Pre-pass that converts relative one-shot forms into absolute.

    ``parse_schedule`` is strict about ``once at <absolute>`` because
    parsing a relative form ("once in 1 minute") at multiple call sites
    would re-anchor the fire time each time. The bridge calls this
    helper after LLM inference (and accepts both forms from the LLM)
    so the stored schedule is always self-consistent.

    Idempotent — strings that aren't ``once in ...`` pass through
    unchanged. Invalid units raise ``ScheduleSyntaxError`` to surface
    the typo before it reaches the store.
    """
    raw = (spec or "").strip().lower()
    m = re.fullmatch(
        r"once\s+in\s+(\d+)\s+(second|seconds|sec|secs|s|"
        r"minute|minutes|min|mins|m|"
        r"hour|hours|hr|hrs|h)",
        raw,
    )
    if not m:
        return spec
    n = int(m.group(1))
    delta = n * _UNIT_SECONDS[m.group(2)]
    fire_at = int(time.time()) + delta
    return "once at " + datetime.fromtimestamp(fire_at).strftime(
        "%Y-%m-%d %H:%M:%S",
    )


def is_one_shot(spec: str) -> bool:
    """True iff *spec* is a one-shot schedule (fires once then disables).

    Tolerates parse failures (returns False) so callers can use this
    as a cheap branch without try/except.
    """
    try:
        return parse_schedule(spec).fire_at_unix is not None
    except ScheduleSyntaxError:
        return False


def parse_schedule(spec: str) -> _ParsedSchedule:
    raw = (spec or "").strip().lower()
    if not raw:
        raise ScheduleSyntaxError("schedule is empty")

    m = re.fullmatch(
        r"every\s+(\d+)\s+(second|seconds|sec|secs|s|"
        r"minute|minutes|min|mins|m|"
        r"hour|hours|hr|hrs|h)",
        raw,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = n * _UNIT_SECONDS[unit]
        if seconds < C.SCHEDULER_MIN_INTERVAL_SEC:
            raise ScheduleSyntaxError(
                f"interval too short: {seconds}s "
                f"(min {C.SCHEDULER_MIN_INTERVAL_SEC}s)"
            )
        return _ParsedSchedule(interval_seconds=seconds)

    m = re.fullmatch(r"interval\s+(\d+)", raw)
    if m:
        seconds = int(m.group(1))
        if seconds < C.SCHEDULER_MIN_INTERVAL_SEC:
            raise ScheduleSyntaxError(
                f"interval too short: {seconds}s "
                f"(min {C.SCHEDULER_MIN_INTERVAL_SEC}s)"
            )
        return _ParsedSchedule(interval_seconds=seconds)

    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        s = int(m.group(3)) if m.group(3) else 0
        _validate_clock(h, mi, s)
        return _ParsedSchedule(daily_hms=(h, mi, s))

    m = re.fullmatch(
        r"weekly\s+(mon|tue|wed|thu|fri|sat|sun)\s+"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
        raw,
    )
    if m:
        dow, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
        s = int(m.group(4)) if m.group(4) else 0
        _validate_clock(h, mi, s)
        return _ParsedSchedule(weekly=(_DOWS[dow], h, mi, s))

    m = re.fullmatch(
        r"once\s+at\s+"
        r"(\d{4})-(\d{2})-(\d{2})[\sT]+"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
        raw,
    )
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = int(m.group(4)), int(m.group(5))
        s = int(m.group(6)) if m.group(6) else 0
        _validate_clock(h, mi, s)
        try:
            dt = datetime(y, mo, d, h, mi, s)
        except ValueError as exc:
            raise ScheduleSyntaxError(f"invalid date: {exc}") from None
        return _ParsedSchedule(fire_at_unix=int(dt.timestamp()))

    raise ScheduleSyntaxError(
        "unrecognised schedule. Examples: "
        "'every 30 seconds', 'every 5 minutes', 'every 2 hours', "
        "'daily 09:00', 'daily 09:00:30', 'weekly mon 09:00', "
        "'once at 2026-06-02 14:30'"
    )


def _validate_clock(h: int, m: int, s: int = 0) -> None:
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ScheduleSyntaxError(
            f"invalid clock: {h:02d}:{m:02d}:{s:02d}"
        )


def next_fire(spec: str, *, last_run_at: int = 0) -> int:
    """Compute the next fire timestamp for *spec*.

    *last_run_at* is the unix-seconds of the previous fire (0 if never).
    Returns an int unix-seconds in the future. The Scheduler stores this
    on the task row and only considers a task due when wall-clock has
    caught up.
    """
    parsed = parse_schedule(spec)
    now = int(time.time())

    if parsed.fire_at_unix is not None:
        # One-shot: absolute timestamp baked in at parse time. The
        # store calls this once at create; subsequent fires never
        # happen (mark_finished disables the task).
        return parsed.fire_at_unix

    if parsed.interval_seconds is not None:
        base = last_run_at if last_run_at else now
        # No-skip semantics: return base + interval even if it's already
        # in the past. The scheduler will fire-then-recompute, so missed
        # triggers get caught up serially (one after the other) rather
        # than being silently dropped. The user explicitly chose this:
        # "不要跳过，宁愿堆积".
        return base + parsed.interval_seconds

    if parsed.daily_hms is not None:
        h, mi, s = parsed.daily_hms
        return _next_clock_match(now, h, mi, s, dow=None)

    assert parsed.weekly is not None
    dow, h, mi, s = parsed.weekly
    return _next_clock_match(now, h, mi, s, dow=dow)


def _next_clock_match(
    now_unix: int, h: int, m: int, s: int, *, dow: Optional[int],
) -> int:
    """Given a target hour/minute/second (and optionally a target weekday),
    return the next future unix-seconds matching it in local time."""
    now_dt = datetime.fromtimestamp(now_unix)
    candidate = now_dt.replace(hour=h, minute=m, second=s, microsecond=0)
    if candidate <= now_dt:
        candidate = candidate + timedelta(days=1)
    if dow is not None:
        # Weekday() returns 0 = Monday. Walk forward at most 7 days.
        for _ in range(7):
            if candidate.weekday() == dow:
                break
            candidate = candidate + timedelta(days=1)
    return int(candidate.timestamp())
