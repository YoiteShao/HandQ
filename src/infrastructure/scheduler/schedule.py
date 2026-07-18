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
    "<min> <hour> <dom> <mon> <dow>" → standard 5-field cron (local time).
                             Each field is one of: "*", "*/n", "a-b", "a,b,c",
                             or a single integer. This is accepted so the agent
                             (and Claude-Code-aligned callers) can pass ordinary
                             cron; the friendly forms above are tried FIRST, so
                             a string only reaches the cron parser when it isn't
                             one of them.

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
    weekly_dow_hms, fire_at_unix, cron) is set."""
    interval_seconds: Optional[int] = None
    daily_hms: Optional[tuple] = None         # (h, m, s)
    weekly: Optional[tuple] = None            # (dow_idx, h, m, s)
    fire_at_unix: Optional[int] = None        # one-shot: absolute unix-seconds
    cron: Optional["_CronExpr"] = None        # standard 5-field cron expression


@dataclass
class _CronExpr:
    """Parsed standard 5-field cron expression (all sets are LOCAL time).

    Each field is expanded to the concrete set of matching integers at parse
    time so :func:`next_fire` can do a cheap membership test per candidate
    minute. Day-of-month and day-of-week follow the usual cron OR-semantics
    when BOTH are restricted (a minute matches if EITHER matches); when one is
    ``*`` the other alone decides — this mirrors vixie-cron behaviour.
    """
    minutes: frozenset       # 0-59
    hours: frozenset         # 0-23
    doms: frozenset          # 1-31 (day of month)
    months: frozenset        # 1-12
    dows: frozenset          # 0-6 (Monday=0, matching datetime.weekday())
    dom_restricted: bool     # True iff the day-of-month field was not "*"
    dow_restricted: bool     # True iff the day-of-week field was not "*"


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

    # Standard 5-field cron — tried LAST so the friendly forms above always
    # win. Returns None (fall through to the error below) when *raw* isn't 5
    # whitespace-separated tokens; raises when it IS 5 fields but malformed.
    cron = _parse_cron(raw)
    if cron is not None:
        return _ParsedSchedule(cron=cron)

    raise ScheduleSyntaxError(
        "unrecognised schedule. Examples: "
        "'every 30 seconds', 'every 5 minutes', 'every 2 hours', "
        "'daily 09:00', 'daily 09:00:30', 'weekly mon 09:00', "
        "'once at 2026-06-02 14:30', or standard 5-field cron "
        "'*/5 * * * *'"
    )


# Cron day-of-week uses 0-6 with BOTH 0 and 7 = Sunday (vixie convention).
# We normalise to datetime.weekday() space (Monday=0 .. Sunday=6) so the
# matcher can compare directly against ``candidate.weekday()``.
_CRON_FIELD_BOUNDS = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),      # 0 and 7 both mean Sunday before normalisation
}


def _parse_cron_field(field: str, kind: str) -> frozenset:
    """Expand one cron field into the concrete set of matching integers.

    Supports ``*``, ``*/step``, ``a-b``, ``a-b/step``, comma lists, and single
    values. Raises :class:`ScheduleSyntaxError` on anything else (or an
    out-of-range value) so the whole expression is rejected rather than
    silently matching nothing.
    """
    lo, hi = _CRON_FIELD_BOUNDS[kind]

    def _norm(vals: "set[int]") -> "set[int]":
        # Normalise cron Sunday (7) → datetime Sunday (6). Only dow uses 0-7;
        # for dow we also shift the whole space: cron 0=Sun..6=Sat →
        # datetime Mon=0..Sun=6.
        if kind != "dow":
            return vals
        out = set()
        for v in vals:
            cron_dow = 0 if v == 7 else v      # 7 → 0 (Sunday)
            # cron: 0=Sun,1=Mon,...,6=Sat  →  datetime: Mon=0,...,Sun=6
            out.add((cron_dow - 1) % 7)
        return out

    result: "set[int]" = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ScheduleSyntaxError(f"empty cron field segment in {kind!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise ScheduleSyntaxError(
                    f"invalid step {step_s!r} in {kind!r}"
                ) from None
            if step < 1:
                raise ScheduleSyntaxError(f"cron step must be >=1 in {kind!r}")
        else:
            base = part
        if base == "*":
            rng = range(lo, hi + 1)
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            try:
                a, b = int(a_s), int(b_s)
            except ValueError:
                raise ScheduleSyntaxError(
                    f"invalid range {base!r} in {kind!r}"
                ) from None
            if a > b:
                raise ScheduleSyntaxError(
                    f"reversed range {base!r} in {kind!r}"
                )
            rng = range(a, b + 1)
        else:
            if step != 1:
                raise ScheduleSyntaxError(
                    f"step requires a range or '*' in {kind!r}"
                )
            try:
                v = int(base)
            except ValueError:
                raise ScheduleSyntaxError(
                    f"invalid cron value {base!r} in {kind!r}"
                ) from None
            rng = range(v, v + 1)
        # Step is measured from this segment's range start (vixie semantics).
        for i, v in enumerate(rng):
            if step > 1 and i % step != 0:
                continue
            if not (lo <= v <= hi):
                raise ScheduleSyntaxError(
                    f"cron {kind} value {v} out of range [{lo},{hi}]"
                )
            result.add(v)
    normed = _norm(result)
    if not normed:
        raise ScheduleSyntaxError(f"cron {kind} field matched nothing")
    return frozenset(normed)


def _parse_cron(raw: str) -> Optional[_CronExpr]:
    """Parse a standard 5-field cron expression, or return None if *raw* is
    not shaped like 5 whitespace-separated fields.

    Returns None (not an error) when the token count isn't 5 so
    :func:`parse_schedule` can fall through to its "unrecognised" message for
    genuinely bad input; raises :class:`ScheduleSyntaxError` when it IS 5
    fields but one of them is malformed.
    """
    tokens = raw.split()
    if len(tokens) != 5:
        return None
    minute = _parse_cron_field(tokens[0], "minute")
    hour = _parse_cron_field(tokens[1], "hour")
    dom = _parse_cron_field(tokens[2], "dom")
    month = _parse_cron_field(tokens[3], "month")
    dow = _parse_cron_field(tokens[4], "dow")
    return _CronExpr(
        minutes=minute, hours=hour, doms=dom, months=month, dows=dow,
        dom_restricted=tokens[2].strip() != "*",
        dow_restricted=tokens[4].strip() != "*",
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

    if parsed.cron is not None:
        return _next_cron_match(now, parsed.cron)

    assert parsed.weekly is not None
    dow, h, mi, s = parsed.weekly
    return _next_clock_match(now, h, mi, s, dow=dow)


def _period_seconds(parsed: _ParsedSchedule) -> Optional[int]:
    """Best-effort nominal period of a recurring schedule, for jitter capping.

    Returns None for one-shot schedules. For cron this is only a rough
    estimate (the gap between the first two fires) — good enough to bound the
    10% jitter fraction, never used for actual scheduling.
    """
    if parsed.fire_at_unix is not None:
        return None
    if parsed.interval_seconds is not None:
        return parsed.interval_seconds
    if parsed.daily_hms is not None:
        return 86400
    if parsed.weekly is not None:
        return 7 * 86400
    if parsed.cron is not None:
        now = int(time.time())
        a = _next_cron_match(now, parsed.cron)
        b = _next_cron_match(a, parsed.cron)
        return max(60, b - a)
    return None


def _jitter_offset(seed: str, period_sec: int) -> int:
    """Deterministic non-negative jitter (seconds) for a recurring task.

    Derived from a stable hash of *seed* (the task id) so a given task always
    gets the same offset — reproducible and testable, unlike random(). Bounded
    by min(SCHEDULER_JITTER_MAX_FRACTION * period, SCHEDULER_JITTER_MAX_SEC).
    """
    import hashlib
    cap = min(
        int(period_sec * C.SCHEDULER_JITTER_MAX_FRACTION),
        C.SCHEDULER_JITTER_MAX_SEC,
    )
    if cap <= 0:
        return 0
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return h % (cap + 1)


def jittered_next_fire(spec: str, *, last_run_at: int = 0, seed: str = "") -> int:
    """Like :func:`next_fire`, plus a deterministic anti-thundering-herd jitter.

    - Recurring schedules: push the fire time LATER by a stable per-seed offset
      bounded to min(10% period, 15min).
    - One-shot schedules landing exactly on a :00 or :30 minute boundary: pull
      the fire time EARLIER by up to 90s (so cron-round one-shots don't all
      land on the same instant). One-shots not on those marks are unchanged.

    *seed* should be the task id. Empty seed disables jitter (returns
    :func:`next_fire` verbatim) — used by callers that want the raw time.
    """
    base = next_fire(spec, last_run_at=last_run_at)
    if not seed:
        return base
    parsed = parse_schedule(spec)
    if parsed.fire_at_unix is not None:
        # One-shot: only nudge if it lands on a :00 / :30 minute boundary.
        dt = datetime.fromtimestamp(base)
        if dt.second == 0 and dt.minute in (0, 30):
            import hashlib
            h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
            early = h % (C.SCHEDULER_ONESHOT_JITTER_EARLY_SEC + 1)
            return base - early
        return base
    period = _period_seconds(parsed)
    if not period:
        return base
    return base + _jitter_offset(seed, period)


def _next_cron_match(now_unix: int, cron: _CronExpr) -> int:
    """Return the next future unix-seconds matching *cron* in local time.

    Steps minute-by-minute from the next whole minute forward. Bounded to
    ~4 years of minutes so a satisfiable-but-sparse expression (e.g.
    "0 0 29 2 *", Feb 29) still resolves while a genuinely impossible one
    (e.g. "0 0 31 2 *") raises instead of looping forever.
    """
    start = datetime.fromtimestamp(now_unix).replace(second=0, microsecond=0)
    start = start + timedelta(minutes=1)   # strictly after now
    _MAX_STEPS = 366 * 24 * 60 * 4         # ~4 years of minutes
    candidate = start
    for _ in range(_MAX_STEPS):
        if (
            candidate.minute in cron.minutes
            and candidate.hour in cron.hours
            and candidate.month in cron.months
            and _cron_day_matches(candidate, cron)
        ):
            return int(candidate.timestamp())
        candidate = candidate + timedelta(minutes=1)
    raise ScheduleSyntaxError(
        "cron expression has no fire time within ~4 years "
        "(likely an impossible day/month combination)"
    )


def _cron_day_matches(candidate: datetime, cron: _CronExpr) -> bool:
    """Apply vixie cron day-of-month / day-of-week OR-semantics.

    When BOTH dom and dow are restricted, the day matches if EITHER matches.
    When only one is restricted, that one alone decides. When neither is
    restricted (both "*"), every day matches.
    """
    dom_ok = candidate.day in cron.doms
    dow_ok = candidate.weekday() in cron.dows
    if cron.dom_restricted and cron.dow_restricted:
        return dom_ok or dow_ok
    if cron.dom_restricted:
        return dom_ok
    if cron.dow_restricted:
        return dow_ok
    return True


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
