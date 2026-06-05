"""Per-monitor state and adaptive cadence logic.

The activity service holds one :class:`MonitorState` per physical display.
The state caches:

- the ``MonitorTier`` for cadence selection
- the previous frame's perceptual hash for de-dup
- the last accepted OCR excerpt for novelty comparison
- the buffered samples awaiting flush
- timestamps that drive both promotion (immediate on activity) and
  demotion (delayed by ``ACTIVITY_TIER_DEMOTE_GRACE_SEC`` so a single
  read-and-think pause doesn't ping-pong).

Tier transitions are driven by the activity service every loop tick
based on:

  * input recency on this monitor (cursor pos + global last-input time)
  * frame-hash novelty (an animated screen is "active" even if the
    user isn't touching the keyboard)

Why we keep this state on its own rather than inline in the service:
isolating it makes the per-monitor logic testable without instantiating
a full asyncio service, and it documents what the *invariant* per
monitor is.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from ..long_term_memory.models import (
    ActivitySample, MonitorInfo, MonitorTier,
)
from ..long_term_memory import _constants as C

_logger = logging.getLogger("handq.activity.state")


@dataclass
class MonitorState:
    info: MonitorInfo
    tier: MonitorTier = MonitorTier.WARM
    last_capture_ts: float = 0.0
    last_activity_ts: float = field(default_factory=time.time)
    last_promotion_ts: float = field(default_factory=time.time)
    last_demotion_candidate_ts: float = 0.0
    last_hash: Optional[int] = None
    last_text: str = ""
    # Wall-clock timestamp of the last frame whose perceptual_hash was
    # accepted as "novel" (i.e. pushed into the ring buffer). Read by the
    # OCR-drain gate to decide whether the monitor has been visually quiet
    # long enough for OCR to run without competing with on-screen
    # animation / video. 0.0 means "never seen a novel frame yet".
    last_screen_change_ts: float = 0.0
    # Ring of recently-accepted OCR texts. The single ``last_text`` field
    # above is insufficient for dedup when the user briefly alt-tabs
    # away — a single foreign window OCR overwrites it, then the
    # original screen looks "novel" again on its next capture and gets
    # re-forwarded. Keep ACTIVITY_TEXT_HISTORY_SIZE most-recent accepted
    # texts so the Jaccard check looks at the whole recent window. The
    # deque's maxlen handles eviction automatically.
    recent_texts: deque = field(
        default_factory=lambda: deque(maxlen=C.ACTIVITY_TEXT_HISTORY_SIZE)
    )
    buffer: List[ActivitySample] = field(default_factory=list)
    buffer_started_ts: float = 0.0

    # ── Cadence ─────────────────────────────────────────────────────────────

    def interval_seconds(self) -> float:
        """How long until the next sample on this monitor."""
        if self.tier == MonitorTier.HOT:
            return C.ACTIVITY_TIER_HOT_INTERVAL_SEC
        if self.tier == MonitorTier.WARM:
            return C.ACTIVITY_TIER_WARM_INTERVAL_SEC
        if self.tier == MonitorTier.COLD:
            return C.ACTIVITY_TIER_COLD_INTERVAL_SEC
        return C.ACTIVITY_TIER_DORMANT_INTERVAL_SEC

    def due(self, now: float) -> bool:
        return (now - self.last_capture_ts) >= self.interval_seconds()

    # ── Tier transitions ────────────────────────────────────────────────────

    def note_activity(self, now: float) -> None:
        """Record that input or visible content moved on this monitor.

        Promotion is immediate so a click is reflected on the very next
        loop tick. We also update ``last_promotion_ts`` to delay any
        downgrade.
        """
        self.last_activity_ts = now
        if self.tier != MonitorTier.HOT:
            _logger.debug(
                "monitor %d promoted %s -> hot",
                self.info.index, self.tier.value,
            )
        self.tier = MonitorTier.HOT
        self.last_promotion_ts = now
        self.last_demotion_candidate_ts = 0.0

    def evaluate_demotion(self, now: float, *, system_idle_seconds: float) -> None:
        """Consider downgrading the tier based on quiet time.

        Demotion ladder, gated by recency-of-activity AND the smoothing
        grace window:

            HOT     →  WARM     when activity older than HOT_RECENCY
                                AND grace elapsed since promotion
            WARM    →  COLD     when activity older than WARM_RECENCY
                                AND grace elapsed
            COLD    →  DORMANT  when activity older than COLD_RECENCY
                                OR system_idle_seconds > GLOBAL_IDLE
        """
        since_activity = now - self.last_activity_ts
        since_promotion = now - self.last_promotion_ts
        if since_promotion < C.ACTIVITY_TIER_DEMOTE_GRACE_SEC:
            return  # too soon

        # Global override: if the entire machine has been idle long
        # enough, every monitor goes DORMANT — the user isn't there.
        if system_idle_seconds >= C.ACTIVITY_GLOBAL_IDLE_PAUSE_SEC:
            if self.tier != MonitorTier.DORMANT:
                _logger.debug(
                    "monitor %d -> dormant (global idle %.0fs)",
                    self.info.index, system_idle_seconds,
                )
            self.tier = MonitorTier.DORMANT
            return

        # Per-monitor demotion ladder.
        new_tier = self.tier
        if self.tier == MonitorTier.HOT and since_activity > C.ACTIVITY_HOT_RECENCY_SEC:
            new_tier = MonitorTier.WARM
        elif self.tier == MonitorTier.WARM and since_activity > C.ACTIVITY_WARM_RECENCY_SEC:
            new_tier = MonitorTier.COLD
        elif self.tier == MonitorTier.COLD and since_activity > C.ACTIVITY_COLD_RECENCY_SEC:
            new_tier = MonitorTier.DORMANT
        if new_tier != self.tier:
            _logger.debug(
                "monitor %d demoted %s -> %s (idle %.0fs)",
                self.info.index, self.tier.value, new_tier.value, since_activity,
            )
            self.tier = new_tier

    # ── Buffer ─────────────────────────────────────────────────────────────

    def append_sample(self, sample: ActivitySample, now: float) -> None:
        if not self.buffer:
            self.buffer_started_ts = now
        self.buffer.append(sample)

    def flush_due(self, now: float) -> bool:
        if not self.buffer:
            return False
        if len(self.buffer) >= C.ACTIVITY_BUFFER_FLUSH_AFTER_N:
            return True
        if (now - self.buffer_started_ts) >= C.ACTIVITY_BUFFER_FLUSH_AFTER_SEC:
            return True
        return False

    def drain_buffer(self) -> List[ActivitySample]:
        out = self.buffer
        self.buffer = []
        self.buffer_started_ts = 0.0
        return out
