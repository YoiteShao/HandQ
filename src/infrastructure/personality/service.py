"""Async PersonalityMonitor service.

Owns one asyncio task that loops every few hundred ms, polls each
:class:`MonitorState`, captures only the monitors whose adaptive
cadence says it's time, and forwards interesting frames into the LTM
candidate pipeline as ``ACTIVITY_OBSERVER`` rows.

Public API
----------
- ``PersonalityMonitor.start(loop)``      — kick off the background task.
- ``PersonalityMonitor.shutdown()``       — graceful cancel + final flush.
- ``PersonalityMonitor.pause()`` / ``resume()`` — let the bridge gate
  capture in response to UI commands or the bridge entering an
  exclusive activity (e.g. desktop-tool takeover).
- ``PersonalityMonitor.snapshot_status()`` — JSON-friendly dict for the
  ``personality_status`` IPC message.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..long_term_memory import _constants as C
from ..long_term_memory.candidates import submit_activity_observer
from ..long_term_memory.models import (
    ActivitySample, MonitorInfo, MonitorTier,
)
from ..vision.ocr import LocalOCR, get_local_ocr
from ..vision.storage import ScreenshotStore
from .capturer import MonitorCapturer
from .frame_diff import excerpt, hamming, perceptual_hash, text_jaccard
from .input_idle import (
    cursor_in_monitor, cursor_pos, enumerate_monitors,
    foreground_app_name, foreground_window_title, system_idle_seconds,
)
from .monitor_state import MonitorState

_logger = logging.getLogger("handq.personality")


# Hard cap on ``_daily_buffer`` length so a 24h+ uptime can't accumulate
# unbounded memory if the user happens to skip the 22:00 summary window
# (laptop closed, RDP session paused, etc.). Once we cross 4x the daily
# summary cap we ring-buffer — keep the latest, drop the oldest.
_DAILY_BUFFER_HARD_CAP: int = C.ACTIVITY_DAILY_SUMMARY_MAX_SAMPLES * 4

# How often to re-check the display topology for hot-plug / unplug /
# resolution changes. Cheap (one mss enumeration call) so we can afford
# this faster than tier transitions, but not every tick — on the common
# no-change path we'd burn ~30 enumerations / min for nothing.
_MONITOR_RECONCILE_INTERVAL_SEC: float = 30.0


def _load_config_dict(config_path: Optional[Path]) -> Dict[str, Any]:
    """Read handq_config.yaml — same contract as LongTermMemory._load_config.

    Returns ``{}`` if the path is None or unreadable so the personality
    service still boots; callers fall back to ScreenshotStore defaults.
    """
    if config_path is None:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        _logger.warning("PersonalityMonitor: config not found at %s; using defaults",
                        config_path)
        return {}
    except Exception:
        _logger.exception("PersonalityMonitor: config read failed at %s", config_path)
        return {}
    return data if isinstance(data, dict) else {}


class PersonalityMonitor:
    """Background activity capture service.

    Lifecycle
    ---------
    The bridge entrypoint constructs one of these alongside
    :class:`LongTermMemory` and awaits :meth:`start`. The instance lives
    for the whole bridge process; ``shutdown`` cancels the loop and
    flushes any remaining buffered samples best-effort.

    The instance is INERT until ``start()`` returns — calling
    :meth:`pause` / :meth:`resume` before start is a no-op so the
    bridge can wire IPC handlers safely during init.

    Configuration contract
    ----------------------
    The constructor takes a ``config_path`` (mirroring
    ``LongTermMemory.init``); we read **only** the ``screenshots:``
    section from it, which drives ``ScreenshotStore`` retention
    bounds. Everything else (sampling cadence, hash thresholds,
    daily-summary hour, sensitive-window patterns, …) lives in
    ``long_term_memory/_constants.py §11`` so users cannot silently
    degrade capture quality by editing the YAML.
    """

    def __init__(
        self,
        *,
        ltm,
        screenshot_root: str,
        config_path: Optional[Path] = None,
    ) -> None:
        self._ltm = ltm
        self._config_path = config_path
        self._config = _load_config_dict(config_path)
        screenshots_cfg = self._config.get("screenshots") or {}
        self._store = ScreenshotStore(
            root=screenshot_root, config_section=screenshots_cfg,
        )
        self._capturer = MonitorCapturer()
        self._ocr: Optional[LocalOCR] = None
        self._task: Optional[asyncio.Task] = None
        self._paused: bool = False
        self._monitors: List[MonitorState] = []
        # ── User-facing personalization knobs ─────────────────────────
        # Two settings live in handq_config.yaml's ``personalization:``
        # section — both expressed as user agency over their own data:
        #
        #   personalization:
        #     enabled: bool                  ← let HandQ remember context
        #                                      across sessions
        #     excluded_apps: [regex, ...]    ← additional title-match
        #                                      patterns that mean "skip
        #                                      this window"
        #
        # The deliberately neutral framing ("tailoring to your tools and
        # conventions") avoids triggering the "I'm being watched" feel.
        # Internal cadence / threshold knobs all live in
        # ``_constants.py §11`` because the wrong value silently
        # degrades capture quality / privacy.
        personalization = self._config.get("personalization") or {}
        self._user_enabled = bool(
            personalization.get("enabled", C.ACTIVITY_MONITOR_ENABLED)
        )
        # Built-in patterns are merged with the user's; either side can
        # protect a window category. We do NOT let yaml *replace* the
        # built-in list — that would let a misconfiguration silently
        # disable the password-manager guard.
        user_patterns = personalization.get("excluded_apps") or []
        self._sensitive_patterns: List[re.Pattern] = []
        for pattern_str in list(C.ACTIVITY_SENSITIVE_WINDOW_PATTERNS) + list(user_patterns):
            try:
                self._sensitive_patterns.append(re.compile(pattern_str))
            except re.error:
                _logger.warning(
                    "PersonalityMonitor: ignoring invalid excluded_apps regex %r",
                    pattern_str,
                )
        # Day rollover guard: we emit at most one daily-summary per
        # local calendar date, regardless of how many ticks fall inside
        # the summary hour. Stored as the local-date string we last
        # emitted for.
        self._last_daily_date: str = ""
        self._daily_buffer: List[ActivitySample] = []
        # Tracks the last time we re-enumerated displays. Set in start()
        # so we don't redundantly re-enumerate on the very first tick.
        self._last_reconcile_ts: float = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self._user_enabled:
            _logger.info(
                "PersonalityMonitor: personalization.enabled=false; not starting",
            )
            return
        if not C.ACTIVITY_MONITOR_ENABLED:
            _logger.info(
                "PersonalityMonitor disabled via constants (ACTIVITY_MONITOR_ENABLED=False)"
            )
            return
        # Hard platform gate: PersonalityMonitor is Windows-only because
        # input_idle.py wraps Win32 APIs (GetLastInputInfo,
        # GetForegroundWindow, EnumDisplayMonitors). On non-Windows we
        # would silently capture screens without per-display tier
        # state — that's a privacy regression, not a portable feature.
        # bridge_main.py is Windows-only in production; this guard is
        # defence-in-depth in case someone runs the bridge on Linux
        # for development.
        if sys.platform != "win32":
            _logger.info(
                "PersonalityMonitor: non-Windows platform (%s); not starting "
                "(production target is Windows only)", sys.platform,
            )
            return
        # Refresh monitor list on every start so a hot-plug between
        # process restarts picks up new displays.
        self._monitors = [
            MonitorState(info=info) for info in enumerate_monitors()
        ]
        if not self._monitors:
            _logger.warning(
                "PersonalityMonitor: no monitors enumerated; capture disabled "
                "(this is expected on headless / non-Windows builds)",
            )
            return
        _logger.info(
            "PersonalityMonitor starting: %d monitors", len(self._monitors),
        )
        for m in self._monitors:
            _logger.info("  monitor %d: %s bbox=%s primary=%s",
                         m.info.index, m.info.label, m.info.bbox, m.info.primary)
        self._last_reconcile_ts = time.time()
        # Lazily build the OCR client; first capture pays the cold-start.
        try:
            self._ocr = get_local_ocr()
        except Exception:
            _logger.exception("PersonalityMonitor: OCR init failed; will retry on first frame")
        self._task = asyncio.create_task(self._run(), name="activity-monitor")

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                # Bound the cancel wait. The capture loop may be
                # mid-OCR (run_in_executor); cancellation propagates
                # but the executor thread continues to completion.
                # 5s is generous for the asyncio half; the OCR thread
                # finishes on its own timeline and is GC'd.
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                _logger.warning(
                    "PersonalityMonitor task did not finish within 5s of cancel"
                )
            except Exception:
                _logger.exception("PersonalityMonitor task crashed during shutdown")
        # Final best-effort flush so any tail of the day isn't lost.
        try:
            await self._final_flush()
        except Exception:
            _logger.exception("PersonalityMonitor final flush failed")
        try:
            self._capturer.close()
        except Exception:
            pass
        _logger.info("PersonalityMonitor shut down")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def snapshot_status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._task and not self._task.done()),
            "paused": bool(self._paused),
            "monitors": [
                {
                    "index": m.info.index,
                    "label": m.info.label,
                    "primary": m.info.primary,
                    "bbox": list(m.info.bbox),
                    "tier": m.tier.value,
                    "buffer_size": len(m.buffer),
                    "last_capture_ts": int(m.last_capture_ts),
                    "last_activity_ts": int(m.last_activity_ts),
                }
                for m in self._monitors
            ],
            "daily_buffered": len(self._daily_buffer),
            "last_daily_date": self._last_daily_date,
        }

    # ── Main loop ───────────────────────────────────────────────────────────

    async def _run(self) -> None:
        # The fast tick (every ~1s) is just a "do anything need doing?"
        # poll. The expensive operations (capture + OCR) only fire when
        # a monitor's adaptive interval is up. This keeps idle CPU near
        # zero on a fully dormant multi-monitor setup.
        try:
            while True:
                if self._paused:
                    await asyncio.sleep(1.0)
                    continue
                try:
                    await self._tick()
                except Exception:
                    _logger.exception("PersonalityMonitor tick error")
                # Daily summary: cheap; runs every tick because the
                # rollover guard prevents duplicate fires.
                try:
                    await self._maybe_emit_daily_summary()
                except Exception:
                    _logger.exception("PersonalityMonitor daily summary error")
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            _logger.info("PersonalityMonitor loop cancelled")
            return

    async def _tick(self) -> None:
        now = time.time()
        await self._reconcile_monitors(now)
        idle = system_idle_seconds()
        idle = idle if idle is not None else 0.0
        cur = cursor_pos()
        title = foreground_window_title()
        app = foreground_app_name()
        sensitive = self._is_sensitive_window(title)
        # Update tiers first so capture decisions reflect current state.
        for m in self._monitors:
            self._update_tier(m, now, cur, idle)
        # Now pick monitors due for a capture.
        for m in self._monitors:
            if m.tier == MonitorTier.DORMANT and not m.due(now):
                continue
            if not m.due(now):
                continue
            m.last_capture_ts = now
            if sensitive and self._cursor_on_monitor(m, cur):
                _logger.debug(
                    "skip capture monitor=%d sensitive_window=%r",
                    m.info.index, title,
                )
                continue
            try:
                await self._capture_and_process(m, title, app, now)
            except Exception:
                _logger.exception("capture/process failed monitor=%d", m.info.index)
        # Per-monitor flush check.
        for m in self._monitors:
            if m.flush_due(now):
                samples = m.drain_buffer()
                if samples:
                    await self._emit_candidate(samples, monitor=m)

    # ── Topology reconciliation ────────────────────────────────────────────

    async def _reconcile_monitors(self, now: float) -> None:
        """Re-enumerate displays and reconcile :attr:`_monitors`.

        Runs at most once every ``_MONITOR_RECONCILE_INTERVAL_SEC``.
        Identifies "the same monitor" across enumerations by its
        ``(left, top)`` virtual-screen corner — stable across resolution
        changes (the OS keeps the corner pinned), only flips when the
        user rearranges displays in Windows settings or unplugs one.

        Behaviour:
          - Vanished corners → drain the monitor's pending buffer to
            LTM, then drop the state. Drain is best-effort; a failure
            here only loses the unflushed tail.
          - New corners → append a fresh ``MonitorState`` with a new
            ``index`` (max existing + 1) so it doesn't collide with a
            stamped-but-vanished display.
          - Resolution flip on a stable corner → update bbox/label in
            place, preserve tier/buffer/text-history, drop ``last_hash``
            (perceptual hash was computed against a different pixel
            area and would dedupe a genuinely-different frame).
          - Any topology change → close the mss client so the next
            capture rebuilds it; mss caches the monitor table inside
            and would otherwise hand out stale region coords.

        An empty enumeration is treated as a transient miss (RDP race,
        display momentarily off) and ignored — keeping the prior list
        is safer than dropping every monitor.
        """
        if (now - self._last_reconcile_ts) < _MONITOR_RECONCILE_INTERVAL_SEC:
            return
        self._last_reconcile_ts = now
        try:
            new_infos = enumerate_monitors()
        except Exception:
            _logger.exception("monitor reconcile: enumerate failed")
            return
        if not new_infos:
            return

        new_by_corner = {(i.bbox[0], i.bbox[1]): i for i in new_infos}
        existing_by_corner = {(m.info.bbox[0], m.info.bbox[1]): m for m in self._monitors}

        topology_changed = False

        # Vanished → drain + drop.
        kept: List[MonitorState] = []
        for m in self._monitors:
            corner = (m.info.bbox[0], m.info.bbox[1])
            if corner in new_by_corner:
                kept.append(m)
                continue
            topology_changed = True
            _logger.info(
                "monitor reconcile: removed index=%d bbox=%s label=%r",
                m.info.index, m.info.bbox, m.info.label,
            )
            if m.buffer:
                samples = m.drain_buffer()
                try:
                    await self._emit_candidate(samples, monitor=m)
                except Exception:
                    _logger.exception(
                        "monitor reconcile: flush failed for monitor %d",
                        m.info.index,
                    )
        self._monitors = kept

        # In-place updates on stable corners (resolution / label flip).
        for corner, m in existing_by_corner.items():
            new_info = new_by_corner.get(corner)
            if new_info is None:
                continue
            if new_info.bbox != m.info.bbox or new_info.label != m.info.label:
                topology_changed = True
                _logger.info(
                    "monitor reconcile: updated index=%d bbox %s -> %s",
                    m.info.index, m.info.bbox, new_info.bbox,
                )
                m.info.bbox = new_info.bbox
                m.info.label = new_info.label
                m.info.primary = new_info.primary
                m.last_hash = None

        # New corners → append with fresh indices.
        next_index = max((m.info.index for m in self._monitors), default=0) + 1
        for corner, info in new_by_corner.items():
            if corner in existing_by_corner:
                continue
            topology_changed = True
            info.index = next_index
            next_index += 1
            _logger.info(
                "monitor reconcile: added index=%d bbox=%s label=%r",
                info.index, info.bbox, info.label,
            )
            self._monitors.append(MonitorState(info=info))

        if topology_changed:
            try:
                self._capturer.close()
            except Exception:
                _logger.debug(
                    "monitor reconcile: capturer close failed", exc_info=True,
                )

    # ── Tier transitions ───────────────────────────────────────────────────

    def _update_tier(
        self,
        m: MonitorState,
        now: float,
        cur: Optional[tuple],
        global_idle: float,
    ) -> None:
        # Cursor-on-monitor + global input recency = activity proxy.
        # Without per-monitor input granularity (Windows doesn't expose
        # it), this is the best heuristic: if you're touching a key /
        # moving the mouse AND the cursor is on this monitor, this is
        # the active monitor.
        if cur is not None and global_idle <= 1.5 and cursor_in_monitor(cur, m.info):
            m.note_activity(now)
            return
        m.evaluate_demotion(now, system_idle_seconds=global_idle)

    def _cursor_on_monitor(self, m: MonitorState, cur: Optional[tuple]) -> bool:
        if cur is None:
            return False
        return cursor_in_monitor(cur, m.info)

    def _is_sensitive_window(self, title: str) -> bool:
        if not title:
            return False
        for p in self._sensitive_patterns:
            if p.search(title):
                return True
        return False

    # ── Capture pipeline ───────────────────────────────────────────────────

    async def _capture_and_process(
        self,
        m: MonitorState,
        title: str,
        app: str,
        now: float,
    ) -> None:
        out_path = os.path.join(
            # ScreenshotStore.subdir("ephemeral") gives us
            # %USERPROFILE%\HandQ\personality\ephemeral\ — the only
            # directory the personality monitor ever writes to.
            # Filename is fixed per monitor so each capture overwrites
            # the previous one; we unlink it immediately after OCR
            # so disk usage stays at < N small files (N = monitor count).
            self._store.subdir(C.PERSONALITY_FRAMES_SUBDIR),
            f"frame_m{m.info.index}.png",
        )
        # Capture is sync I/O; offload so the loop stays responsive.
        loop = asyncio.get_running_loop()
        captured = await loop.run_in_executor(
            None, self._capturer.capture, m.info, out_path,
        )
        if not captured:
            return
        try:
            # Image-hash de-dup BEFORE OCR — cheapest gate.
            ph = await loop.run_in_executor(None, perceptual_hash, captured)
            if ph is not None and m.last_hash is not None:
                if hamming(ph, m.last_hash) <= C.ACTIVITY_FRAME_HASH_DELTA_THRESHOLD:
                    _logger.debug(
                        "monitor %d frame deduped (hamming<=%d)",
                        m.info.index, C.ACTIVITY_FRAME_HASH_DELTA_THRESHOLD,
                    )
                    return
            # OCR.
            if self._ocr is None:
                try:
                    self._ocr = get_local_ocr()
                except Exception:
                    _logger.exception("OCR init failed; dropping frame")
                    return
            ocr_result = await loop.run_in_executor(
                None, self._ocr.recognize, captured,
            )
            if not ocr_result.ok:
                _logger.debug(
                    "monitor %d ocr error: %s", m.info.index, ocr_result.error,
                )
                return
            text = (ocr_result.full_text or "").strip()
            if len(text) < C.ACTIVITY_OCR_MIN_CHARS:
                return
            # Dedup against the ring of recently-forwarded texts on this
            # monitor, not just the single last_text. Without the ring,
            # a brief alt-tab pattern (VSCode → Slack → VSCode) accepts
            # the second VSCode capture as "novel" because last_text is
            # the Slack window. Take the MAX Jaccard across the ring so
            # any sufficiently-similar prior screen blocks acceptance.
            jaccards = [text_jaccard(text, prev) for prev in m.recent_texts] \
                if m.recent_texts else []
            if m.last_text:
                jaccards.append(text_jaccard(text, m.last_text))
            jacc = max(jaccards) if jaccards else 0.0
            if jacc >= C.ACTIVITY_OCR_TEXT_JACCARD_BAR:
                _logger.debug(
                    "monitor %d text near-duplicate (max jacc=%.2f over %d prior)",
                    m.info.index, jacc, len(jaccards),
                )
                # Update the hash so we don't keep re-OCRing the same screen,
                # but don't forward.
                m.last_hash = ph if ph is not None else m.last_hash
                return
            # Accept.
            sample = ActivitySample(
                monitor_index=m.info.index,
                timestamp=int(now),
                foreground_window=title[:160],
                foreground_app=app[:80],
                text_excerpt=excerpt(text, C.ACTIVITY_OCR_EXCERPT_MAX_CHARS),
                tier=m.tier,
                novelty=1.0 - jacc,
            )
            m.last_hash = ph if ph is not None else m.last_hash
            m.last_text = text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4]
            # Push into the ring AFTER acceptance so we never compare the
            # incoming text against itself. Truncate to the same max as
            # last_text so the per-text memory cost stays bounded.
            m.recent_texts.append(text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4])
            m.append_sample(sample, now)
            self._daily_buffer.append(sample)
            # Ring-buffer guard: if the user closes the laptop right
            # before 22:00 every day, the daily-summary hour never
            # arrives and the buffer would otherwise grow without
            # bound. Trim oldest entries above the hard cap.
            if len(self._daily_buffer) > _DAILY_BUFFER_HARD_CAP:
                drop = len(self._daily_buffer) - _DAILY_BUFFER_HARD_CAP
                self._daily_buffer = self._daily_buffer[drop:]
        finally:
            # ALWAYS unlink the PNG. Privacy + zero-disk-accumulation
            # invariant. Wrapped in try because the file may already be
            # gone (Windows AV scanners occasionally race us).
            try:
                if not C.ACTIVITY_KEEP_FRAME_FILES:
                    os.unlink(captured)
            except OSError:
                pass

    # ── Candidate emission ─────────────────────────────────────────────────

    async def _emit_candidate(
        self, samples: List[ActivitySample], *, monitor: MonitorState,
    ) -> None:
        if not samples:
            return
        raw_text = self._render_samples(samples, monitor=monitor)
        try:
            await submit_activity_observer(
                ltm=self._ltm,
                raw_text=raw_text,
                source_ref=f"activity:m{monitor.info.index}:{int(time.time())}",
                daily_summary=False,
                monitor_index=monitor.info.index,
                sample_count=len(samples),
            )
            _logger.info(
                "activity candidate emitted monitor=%d samples=%d",
                monitor.info.index, len(samples),
            )
        except Exception:
            _logger.exception("submit_activity_observer failed")

    def _render_samples(
        self, samples: List[ActivitySample], *, monitor: MonitorState,
    ) -> str:
        lines: List[str] = [
            f"# Activity observation",
            f"Monitor {monitor.info.index} ({monitor.info.label})",
            f"Samples: {len(samples)}",
            "",
        ]
        for s in samples:
            ts = datetime.fromtimestamp(s.timestamp).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"## {ts} [{s.tier.value}] {s.foreground_app or '?'}")
            if s.foreground_window:
                lines.append(f"window: {s.foreground_window}")
            if s.text_excerpt:
                lines.append("text excerpt:")
                lines.append(s.text_excerpt)
            lines.append("")
        return "\n".join(lines)

    # ── Daily summary ──────────────────────────────────────────────────────

    async def _maybe_emit_daily_summary(self) -> None:
        now_dt = datetime.now()
        # Only fire during the configured hour and only once per day.
        if now_dt.hour != C.ACTIVITY_DAILY_SUMMARY_HOUR_LOCAL:
            return
        date_key = now_dt.strftime("%Y-%m-%d")
        if self._last_daily_date == date_key:
            return
        if not self._daily_buffer:
            self._last_daily_date = date_key
            return
        samples = self._daily_buffer[-C.ACTIVITY_DAILY_SUMMARY_MAX_SAMPLES:]
        raw_text = self._render_daily_summary(date_key, samples)
        try:
            await submit_activity_observer(
                ltm=self._ltm,
                raw_text=raw_text,
                source_ref=f"daily:{date_key}",
                daily_summary=True,
                monitor_index=-1,
                sample_count=len(samples),
            )
            _logger.info(
                "daily activity summary emitted date=%s samples=%d",
                date_key, len(samples),
            )
            self._last_daily_date = date_key
            self._daily_buffer = []
        except Exception:
            _logger.exception("daily summary submit failed")

    def _render_daily_summary(
        self, date_key: str, samples: List[ActivitySample],
    ) -> str:
        # Aggregate by foreground app for a denser, more LLM-digestible
        # summary than dumping raw samples — keeps prompt cost bounded.
        from collections import Counter, defaultdict
        app_counter: Counter = Counter()
        per_app_text: dict = defaultdict(list)
        per_app_titles: dict = defaultdict(set)
        for s in samples:
            app = s.foreground_app or "(unknown)"
            app_counter[app] += 1
            if s.text_excerpt:
                per_app_text[app].append(s.text_excerpt[:200])
            if s.foreground_window:
                per_app_titles[app].add(s.foreground_window[:120])
        top = app_counter.most_common(10)
        lines: List[str] = [
            f"# Daily activity summary {date_key}",
            f"Total accepted samples: {len(samples)}",
            f"Distinct apps: {len(app_counter)}",
            "",
            "## Top apps by sample count",
        ]
        for app, n in top:
            lines.append(f"- {app}: {n} samples")
            titles = sorted(per_app_titles.get(app, []))[:5]
            if titles:
                lines.append("  windows: " + "; ".join(titles))
            snippets = per_app_text.get(app, [])[:3]
            for sn in snippets:
                lines.append(f"  excerpt: {sn}")
        return "\n".join(lines)

    # ── Final flush on shutdown ────────────────────────────────────────────

    async def _final_flush(self) -> None:
        for m in self._monitors:
            if m.buffer:
                samples = m.drain_buffer()
                await self._emit_candidate(samples, monitor=m)
