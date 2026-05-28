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
    "interval <S>"        →  raw seconds (>= SCHEDULER_MIN_INTERVAL_SEC).
                             Mostly for tests; users don't see this.

Anything else raises ``ScheduleSyntaxError`` so the IPC layer can
surface the message to the renderer.

Calculation: ``next_run_at`` always returns an integer unix-seconds.
For "every X" forms the next fire is ``last + interval`` (or now+interval
when last is 0). For "daily" / "weekly" forms we anchor to local time
and roll forward to the next future occurrence.
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
    weekly_dow_hms) is set."""
    interval_seconds: Optional[int] = None
    daily_hms: Optional[tuple] = None         # (h, m, s)
    weekly: Optional[tuple] = None            # (dow_idx, h, m, s)


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
        unit_seconds = {
            "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
            "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
            "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
        }
        seconds = n * unit_seconds[unit]
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

    raise ScheduleSyntaxError(
        "unrecognised schedule. Examples: "
        "'every 30 seconds', 'every 5 minutes', 'every 2 hours', "
        "'daily 09:00', 'daily 09:00:30', 'weekly mon 09:00'"
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
