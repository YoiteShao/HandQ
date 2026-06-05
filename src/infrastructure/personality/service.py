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
import io
import json
import logging
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import yaml

from ..long_term_memory import _constants as C
from ..long_term_memory.candidates import submit_activity_observer
from ..long_term_memory.models import (
    ActivitySample, MonitorInfo, MonitorTier,
)
from ..vision.ocr import LocalOCR, get_local_ocr
from ..vision.storage import ScreenshotStore
from .capturer import MonitorCapturer
from .frame_diff import (
    excerpt, hamming, perceptual_hash_array, text_jaccard,
)
from .input_idle import (
    cursor_in_monitor, cursor_pos, enumerate_monitors,
    foreground_app_name, foreground_window_title,
    is_session_locked, system_idle_seconds,
)
from .monitor_state import MonitorState

_logger = logging.getLogger("handq.personality")


def _encode_jpeg(rgb: Any) -> bytes:
    """Encode an RGB ndarray as JPEG bytes at the configured quality.

    Lives at module level (not as a method) so we can hand it directly
    to ``run_in_executor`` without binding ``self``. PIL is imported
    lazily because :mod:`mss` already pulls it in transitively but we
    don't want to make this module's top-level import sensitive to
    Pillow's presence on dev machines without it.
    """
    from PIL import Image
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=C.ACTIVITY_RING_JPEG_QUALITY,
             optimize=False)
    return buf.getvalue()


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

        # ── Deferred-OCR plumbing ─────────────────────────────────────
        # Per-monitor in-memory ring of (jpeg_bytes, ts, title, app, ...).
        # Capture pushes here; the OCR drain worker pops and runs OCR
        # only when the global gate is open (session locked, or input
        # idle ≥ N AND every monitor visually quiet ≥ M). Storing JPEG
        # bytes (not raw ndarrays) keeps the worst-case ceiling at
        # ~76 MB for 3 monitors @ maxlen=128 — see _constants §11.7.
        self._rings: Dict[int, Deque[Dict[str, Any]]] = {}
        self._ocr_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)
        self._drain_task: Optional[asyncio.Task] = None
        # Diagnostic counters for personality_status IPC.
        self._gate_open_now: bool = False
        self._ocr_drained_total: int = 0

        # ── Spillover (disk-backed bounded fallback) ─────────────────
        # See _constants §11.7.1. Triggers on ring overflow and on
        # monitor disconnect. Frame pair (jpeg + meta.json) lands under
        # screenshot_root/spillover/. Drain worker reads from here when
        # all in-memory rings are empty.
        self._spill_dir: Path = Path(screenshot_root) / C.PERSONALITY_SPILLOVER_SUBDIR
        # Monotonic counter avoids filename collisions when many frames
        # share the same wall-clock millisecond.
        self._spill_seq: int = 0
        self._spilled_total: int = 0           # diagnostics
        self._spill_recovered_total: int = 0   # diagnostics
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
        # Per-monitor rings (initialised here so vanished/added monitors
        # are reconciled in _reconcile_monitors).
        self._rings = {
            m.info.index: deque(maxlen=C.ACTIVITY_RING_MAXLEN)
            for m in self._monitors
        }
        # Spillover directory: lazy-created here, then we sweep stale
        # entries from previous runs (best-effort — failures swallowed
        # so a perms / disk problem can't block boot).
        try:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.get_running_loop().run_in_executor(
                None, self._purge_stale_spills,
            )
        except Exception:
            _logger.exception(
                "spillover: dir init / stale purge failed (continuing without spillover)"
            )
        self._task = asyncio.create_task(self._run(), name="activity-monitor")
        self._drain_task = asyncio.create_task(
            self._ocr_drain_loop(), name="activity-ocr-drain",
        )

    async def shutdown(self) -> None:
        # Drain task first — it captures _paused / _gate_open_now and
        # shouldn't continue OCRing after the main loop is gone.
        if self._drain_task and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await asyncio.wait_for(self._drain_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                _logger.warning(
                    "PersonalityMonitor drain task did not finish within 5s of cancel"
                )
            except Exception:
                _logger.exception("PersonalityMonitor drain task crashed during shutdown")
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
        # Spill any unprocessed ring frames to disk so a graceful close
        # doesn't drop them. The next bridge launch will see them in
        # spillover/ and the drain worker will pick them up on the
        # first idle window. Covers user-initiated close (menu exit /
        # Ctrl+C / `/clear` restart / Windows logoff with grace) but
        # NOT hard kills (OOM, power loss) which never reach this path.
        try:
            spilled_on_close = await self._spill_rings_on_shutdown()
            if spilled_on_close:
                _logger.info(
                    "shutdown: spilled %d unprocessed ring frames for next-launch drain",
                    spilled_on_close,
                )
        except Exception:
            _logger.exception("shutdown: ring spill failed (frames lost)")
        # Final best-effort flush so any tail of the day isn't lost.
        try:
            await self._final_flush()
        except Exception:
            _logger.exception("PersonalityMonitor final flush failed")
        try:
            self._capturer.close()
        except Exception:
            pass
        _logger.info(
            "PersonalityMonitor shut down (drained=%d, rings_left=%d)",
            self._ocr_drained_total,
            sum(len(r) for r in self._rings.values()),
        )

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
                    "ring_size": len(self._rings.get(m.info.index, ())),
                    "last_capture_ts": int(m.last_capture_ts),
                    "last_activity_ts": int(m.last_activity_ts),
                    "last_screen_change_ts": int(m.last_screen_change_ts),
                }
                for m in self._monitors
            ],
            "ring_size_total": sum(len(r) for r in self._rings.values()),
            "ocr_gate_open": bool(self._gate_open_now),
            "ocr_drained_total": int(self._ocr_drained_total),
            "spilled_total": int(self._spilled_total),
            "spill_recovered_total": int(self._spill_recovered_total),
            "spill_files_now": self._spill_count(),
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
            # Spill any unprocessed JPEG frames to disk before dropping
            # the monitor's state. The drain worker will pick them up on
            # the next idle window — see _ocr_one's orphan path. The
            # snapshot of recent_texts goes with each pair so Jaccard
            # dedup still works once the MonitorState is gone.
            ring = self._rings.pop(m.info.index, None)
            if ring:
                snapshot = list(m.recent_texts)
                loop = asyncio.get_running_loop()
                for entry in list(ring):
                    spilled = await loop.run_in_executor(
                        None, self._spill_to_disk, entry, snapshot,
                    )
                    if not spilled:
                        # disk full / cap reached — stop trying so we
                        # don't busy-loop on an immutable failure.
                        _logger.warning(
                            "monitor reconcile: spillover full while saving "
                            "orphan ring for monitor %d (%d frames lost)",
                            m.info.index, len(ring),
                        )
                        break
                ring.clear()
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
            # New monitors get a fresh empty ring; capture path will
            # populate it on the next due-tick.
            self._rings[info.index] = deque(maxlen=C.ACTIVITY_RING_MAXLEN)

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
        """Capture the monitor, decide if the frame is novel, and push it
        into the per-monitor ring buffer for later OCR.

        OCR is intentionally NOT run here — it's deferred to
        :meth:`_ocr_drain_loop` so it doesn't compete with the user's
        CPU during active work. See plan robust-gathering-shannon.md
        for the design rationale + bench numbers.
        """
        loop = asyncio.get_running_loop()
        # 1. Capture → ndarray (no disk I/O)
        rgb = await loop.run_in_executor(None, self._capturer.capture, m.info)
        if rgb is None:
            return
        # 2. Cheap visual novelty gate: perceptual_hash on the ndarray.
        #    Frames that match the prior accepted frame within the
        #    Hamming threshold are dropped before we pay the JPEG
        #    encode cost or queue any memory.
        ph = await loop.run_in_executor(None, perceptual_hash_array, rgb)
        if ph is not None and m.last_hash is not None:
            if hamming(ph, m.last_hash) <= C.ACTIVITY_FRAME_HASH_DELTA_THRESHOLD:
                _logger.debug(
                    "monitor %d frame deduped (hamming<=%d)",
                    m.info.index, C.ACTIVITY_FRAME_HASH_DELTA_THRESHOLD,
                )
                return
        # 3. Novel frame: stamp the screen-change timestamp (read by
        #    _gate_open) and update the last-hash for the next compare.
        m.last_screen_change_ts = now
        if ph is not None:
            m.last_hash = ph
        # 4. Encode JPEG bytes (~10 ms; quality=70 keeps frames around
        #    200 KB) and push into the per-monitor ring. The ring's
        #    deque(maxlen) silently drops the oldest entry on overflow,
        #    bounding RSS at ACTIVITY_RING_MAXLEN × ~200 KB regardless
        #    of how long the user is busy.
        try:
            jpeg = await loop.run_in_executor(None, _encode_jpeg, rgb)
        except Exception:
            _logger.exception("monitor %d JPEG encode failed", m.info.index)
            return
        ring = self._rings.setdefault(
            m.info.index, deque(maxlen=C.ACTIVITY_RING_MAXLEN),
        )
        # 5. Spillover guard: if the ring is at maxlen, peek the oldest
        #    entry and try to spill it to disk before the deque silently
        #    drops it on append. On spill failure (disk full / perms /
        #    cap reached) we let deque do its default thing — the user
        #    still gets the most recent 128 frames, no worse than before
        #    spillover existed.
        if len(ring) == ring.maxlen:
            oldest = ring[0]
            spilled = await loop.run_in_executor(
                None, self._spill_to_disk, oldest, list(m.recent_texts),
            )
            if spilled:
                ring.popleft()
        ring.append({
            "jpeg": jpeg,
            "ts": now,
            "title": title,
            "app": app,
            "tier": m.tier,
            "monitor_index": m.info.index,
        })

    # ── Spillover (disk-backed bounded fallback) ────────────────────────────
    #
    # Helpers for §11.7.1: write a single ring entry to disk, read the
    # oldest spilled entry back, and purge stale leftovers. The drain
    # worker treats spillover as "tier-2 storage" — only consulted when
    # all in-memory rings are empty.

    def _spill_count(self) -> int:
        """Count of spilled .jpg files currently on disk. Best-effort —
        returns 0 if the dir is missing / unreadable."""
        try:
            return sum(
                1 for p in self._spill_dir.iterdir()
                if p.suffix == ".jpg" and p.is_file()
            )
        except OSError:
            return 0

    def _purge_stale_spills(self) -> None:
        """Clean up spill files older than ``ACTIVITY_SPILL_MAX_AGE_HOURS``
        and enforce the ``ACTIVITY_SPILL_MAX_FILES`` cap (oldest first).

        Runs once at start() before the drain task spins up. Sync — the
        caller offloads it to the executor. Best-effort: any OSError is
        swallowed.
        """
        if not self._spill_dir.is_dir():
            return
        cutoff = time.time() - (C.ACTIVITY_SPILL_MAX_AGE_HOURS * 3600.0)
        pairs: List[Path] = []
        try:
            for jpg in sorted(self._spill_dir.iterdir()):
                if jpg.suffix != ".jpg" or not jpg.is_file():
                    continue
                try:
                    mtime = jpg.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    self._unlink_spill_pair(jpg)
                else:
                    pairs.append(jpg)
        except OSError:
            return
        # Cap by count: drop oldest excess.
        if len(pairs) > C.ACTIVITY_SPILL_MAX_FILES:
            pairs.sort(key=lambda p: p.name)  # filename embeds ts → FIFO
            for stale in pairs[: len(pairs) - C.ACTIVITY_SPILL_MAX_FILES]:
                self._unlink_spill_pair(stale)

    def _unlink_spill_pair(self, jpg_path: Path) -> None:
        """Delete a spill .jpg and its sibling .meta.json. Either may be
        missing (partial write, prior unlink) — both attempts swallowed."""
        for p in (jpg_path, jpg_path.with_suffix(".meta.json")):
            try:
                p.unlink()
            except OSError:
                pass

    def _spill_to_disk(
        self,
        entry: Dict[str, Any],
        recent_texts_snapshot: List[str],
    ) -> bool:
        """Persist a single ring entry as `<stem>.jpg` + `<stem>.meta.json`.

        Returns True on success. Returns False on any failure (disk full,
        perms, cap reached) so the caller can fall back to the deque's
        natural drop-oldest behaviour. SYNCHRONOUS — caller MUST offload
        to the executor.
        """
        if not self._spill_dir.is_dir():
            try:
                self._spill_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
        # Cap guard: refuse to write a new pair if we already hit the
        # file limit. The caller's deque will drop the in-RAM entry
        # naturally; we don't try to evict an even older spill here
        # because that would require I/O on the hot path.
        if self._spill_count() >= C.ACTIVITY_SPILL_MAX_FILES:
            return False
        idx = int(entry.get("monitor_index", 0))
        ts = float(entry.get("ts", time.time()))
        self._spill_seq += 1
        stem = f"m{idx}_{ts:.3f}_{self._spill_seq:06d}"
        jpg_path = self._spill_dir / f"{stem}.jpg"
        meta_path = self._spill_dir / f"{stem}.meta.json"
        # Truncate each recent_texts entry so meta.json stays small.
        max_chars = C.ACTIVITY_SPILL_RECENT_TEXT_MAX_CHARS
        truncated_history = [
            (t[:max_chars] if isinstance(t, str) else "")
            for t in (recent_texts_snapshot or [])
        ]
        tier_value = entry.get("tier")
        if isinstance(tier_value, MonitorTier):
            tier_str = tier_value.value
        elif tier_value is None:
            tier_str = ""
        else:
            tier_str = str(tier_value)
        meta = {
            "monitor_index": idx,
            "ts": ts,
            "title": str(entry.get("title", ""))[:160],
            "app": str(entry.get("app", ""))[:80],
            "tier": tier_str,
            "recent_texts_snapshot": truncated_history,
        }
        try:
            with open(jpg_path, "wb") as f:
                f.write(entry["jpeg"])
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except OSError:
            # Half-written pair — best-effort cleanup so a re-try
            # doesn't see an orphan .jpg without its .meta.json.
            self._unlink_spill_pair(jpg_path)
            return False
        self._spilled_total += 1
        return True

    async def _spill_rings_on_shutdown(self) -> int:
        """Persist every in-memory ring entry to disk on graceful close.

        Called from :meth:`shutdown` AFTER the capture and drain tasks
        have been cancelled. Uses the standard :meth:`_spill_to_disk`
        path, so the next bridge launch sees the same shape of files
        and the drain worker recovers them via the orphan path (the
        spilled meta.json carries each monitor's ``recent_texts``
        snapshot for Jaccard dedup).

        Returns the count of frames successfully spilled. Stops early
        if :meth:`_spill_to_disk` returns False (disk full / cap
        reached) — partial recovery is better than refusing to close.
        """
        if not self._rings:
            return 0
        loop = asyncio.get_running_loop()
        spilled = 0
        # Iterate over a copy because _spill_to_disk does no mutation
        # but we want to be defensive.
        for monitor_idx, ring in list(self._rings.items()):
            if not ring:
                continue
            m = next(
                (mm for mm in self._monitors if mm.info.index == monitor_idx),
                None,
            )
            snapshot = list(m.recent_texts) if m is not None else []
            for entry in list(ring):
                ok = await loop.run_in_executor(
                    None, self._spill_to_disk, entry, snapshot,
                )
                if not ok:
                    _logger.warning(
                        "shutdown spill: stopped at monitor %d (disk full or cap reached); "
                        "%d frames spilled, %d may be lost",
                        monitor_idx, spilled, len(ring) - spilled,
                    )
                    return spilled
                spilled += 1
            ring.clear()
        return spilled

    def _load_oldest_spill(self) -> Optional[Dict[str, Any]]:
        """Read the oldest spilled pair (by filename = ts) and return an
        entry-shaped dict augmented with ``recent_texts_snapshot`` for
        orphan-frame Jaccard. Removes the pair from disk only AFTER the
        OCR worker has accepted/rejected it — that's the caller's job
        via :meth:`_unlink_spill_pair` once OCR finishes.

        SYNCHRONOUS — caller offloads via run_in_executor. Returns None
        when the dir is empty or unreadable.
        """
        if not self._spill_dir.is_dir():
            return None
        try:
            jpgs = sorted(
                p for p in self._spill_dir.iterdir()
                if p.suffix == ".jpg" and p.is_file()
            )
        except OSError:
            return None
        if not jpgs:
            return None
        jpg_path = jpgs[0]
        meta_path = jpg_path.with_suffix(".meta.json")
        try:
            with open(jpg_path, "rb") as f:
                jpeg = f.read()
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                # Missing / corrupt meta — proceed with stub metadata so
                # we still get OCR'd text into LTM. Drop the pair after
                # OCR.
                meta = {
                    "monitor_index": -1, "ts": jpg_path.stat().st_mtime,
                    "title": "", "app": "", "tier": "",
                    "recent_texts_snapshot": [],
                }
        except OSError:
            return None
        return {
            "jpeg": jpeg,
            "ts": float(meta.get("ts", time.time())),
            "title": str(meta.get("title", "")),
            "app": str(meta.get("app", "")),
            "tier": meta.get("tier", ""),
            "monitor_index": int(meta.get("monitor_index", -1)),
            "recent_texts_snapshot": list(meta.get("recent_texts_snapshot", [])),
            "_spill_jpg_path": jpg_path,
        }

    # ── OCR drain worker (deferred OCR) ────────────────────────────────────

    async def _ocr_drain_loop(self) -> None:
        """Run OCR on queued frames whenever the global gate is open.

        Drain order:
          1. In-memory rings — pop the oldest entry across all monitors
             (round-robin by frame timestamp).
          2. Spillover on disk — when all rings are empty, fall back to
             reading spilled pairs in FIFO order. Frames may be orphaned
             (the originating monitor disappeared since capture), so
             ``_ocr_one`` knows how to handle a missing MonitorState.

        Gate semantics — see :meth:`_gate_open`. While the gate is
        closed (user busy / video playing) the loop sleeps. When open
        it pulls the next entry and OCRs it under
        :attr:`_ocr_semaphore` so we never run more than one OCR
        concurrently — which keeps RSS bounded (a single OCR call
        peaks at ~565 MB working memory) and prevents multi-core
        contention even when many frames are queued.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                if self._paused:
                    self._gate_open_now = False
                    await asyncio.sleep(C.ACTIVITY_OCR_DRAIN_POLL_SEC)
                    continue
                gate = self._gate_open()
                self._gate_open_now = gate
                if not gate:
                    await asyncio.sleep(C.ACTIVITY_OCR_DRAIN_POLL_SEC)
                    continue
                entry = self._pop_round_robin()
                if entry is None:
                    # Rings empty — try spillover. Disk read is offloaded
                    # so the loop stays responsive.
                    entry = await loop.run_in_executor(
                        None, self._load_oldest_spill,
                    )
                    if entry is not None:
                        self._spill_recovered_total += 1
                if entry is None:
                    # Both ring and spillover empty — sleep before next
                    # poll so we don't spin.
                    await asyncio.sleep(C.ACTIVITY_OCR_DRAIN_POLL_SEC)
                    continue
                async with self._ocr_semaphore:
                    try:
                        await self._ocr_one(entry)
                    except Exception:
                        _logger.exception(
                            "OCR drain failed for monitor %d frame ts=%s",
                            entry.get("monitor_index"), entry.get("ts"),
                        )
                # If this entry came from disk, unlink the pair AFTER
                # OCR finished (success OR failure — we don't keep
                # retrying the same frame indefinitely).
                spill_path = entry.get("_spill_jpg_path")
                if spill_path is not None:
                    await loop.run_in_executor(
                        None, self._unlink_spill_pair, spill_path,
                    )
        except asyncio.CancelledError:
            _logger.info("OCR drain loop cancelled")
            return

    def _gate_open(self) -> bool:
        """Return True iff OCR is allowed to run now.

        The gate opens on either:
          1. **Session locked** — strongest "user is gone" signal. If
             the input desktop is anything other than ``Default`` (i.e.
             Win+L, auto-lock, screensaver-with-password) we know
             nobody's at the machine, so OCR can run flat-out without
             competing.
          2. **Soft idle** — keyboard/mouse silent for
             ACTIVITY_OCR_GATE_INPUT_IDLE_SEC AND every monitor has
             been visually quiet for ACTIVITY_OCR_GATE_SCREEN_QUIET_SEC.
             Both signals together so we don't OCR while a video plays
             unattended (input-idle but screen still changing — would
             compete with hardware decode).
        """
        if is_session_locked():
            return True
        idle = system_idle_seconds() or 0.0
        if idle < C.ACTIVITY_OCR_GATE_INPUT_IDLE_SEC:
            return False
        now = time.time()
        for m in self._monitors:
            ts = m.last_screen_change_ts
            if ts and (now - ts) < C.ACTIVITY_OCR_GATE_SCREEN_QUIET_SEC:
                return False
        return True

    def _pop_round_robin(self) -> Optional[Dict[str, Any]]:
        """Pop the OLDEST entry across every non-empty ring.

        Choosing oldest-first across monitors (rather than e.g.
        round-robin by index) keeps draining fair when a chatty
        monitor has many queued frames AND a quiet monitor has a few
        very old ones — we want to avoid starving the quiet monitor's
        old context behind the chatty one's recent flood.
        """
        candidates: List[tuple[int, float]] = [
            (idx, ring[0]["ts"])
            for idx, ring in self._rings.items()
            if ring
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda kv: kv[1])
        return self._rings[candidates[0][0]].popleft()

    async def _ocr_one(self, entry: Dict[str, Any]) -> None:
        """Run OCR on a single ring (or spillover) entry, then run the
        text-similarity dedup and (if accepted) push an
        ``ActivitySample`` through the existing buffer/flush flow.

        Two paths:
          * **Live monitor** — the originating ``MonitorState`` is still
            present. Use its ``recent_texts`` / ``last_text`` for the
            Jaccard dedup and route the sample through
            ``m.append_sample`` so the buffer/flush logic owns it.
          * **Orphan** — the monitor disconnected between capture and
            drain (frame survived via spillover). Fall back to the
            ``recent_texts_snapshot`` shipped inside the spilled
            meta.json for dedup, and submit the sample directly to
            LTM as a one-shot candidate (we have no buffer to put it
            into and no future monitor will own the in-flight sample).
        """
        loop = asyncio.get_running_loop()
        if self._ocr is None:
            try:
                self._ocr = get_local_ocr()
            except Exception:
                _logger.exception("OCR drain: engine init failed; dropping frame")
                return
        monitor_index = int(entry.get("monitor_index", -1))
        m = next(
            (mm for mm in self._monitors if mm.info.index == monitor_index),
            None,
        )
        jpeg: bytes = entry["jpeg"]
        ts: float = float(entry["ts"])
        title: str = str(entry.get("title", ""))
        app: str = str(entry.get("app", ""))
        tier_value = entry.get("tier", m.tier if m is not None else None)
        # RapidOCR.recognize accepts bytes (image bytes are auto-decoded).
        ocr_result = await loop.run_in_executor(
            None, self._ocr.recognize, jpeg,
        )
        self._ocr_drained_total += 1
        if not ocr_result.ok:
            _logger.debug(
                "monitor %d ocr error: %s", monitor_index, ocr_result.error,
            )
            return
        text = (ocr_result.full_text or "").strip()
        if len(text) < C.ACTIVITY_OCR_MIN_CHARS:
            return
        # Pick the right history source for Jaccard dedup.
        if m is not None:
            history: List[str] = list(m.recent_texts)
            if m.last_text:
                history.append(m.last_text)
        else:
            history = list(entry.get("recent_texts_snapshot") or [])
        jaccards = [text_jaccard(text, prev) for prev in history] if history else []
        jacc = max(jaccards) if jaccards else 0.0
        if jacc >= C.ACTIVITY_OCR_TEXT_JACCARD_BAR:
            _logger.debug(
                "monitor %d text near-duplicate (max jacc=%.2f over %d prior, orphan=%s)",
                monitor_index, jacc, len(jaccards), m is None,
            )
            return
        # Build the sample. For orphan frames we still fill in the
        # tier (best-effort: spilled meta.json carried it as a string;
        # we look up the enum here so the final ActivitySample matches
        # the in-band shape).
        if isinstance(tier_value, MonitorTier):
            tier = tier_value
        else:
            try:
                tier = MonitorTier(str(tier_value))
            except Exception:
                tier = MonitorTier.WARM
        sample = ActivitySample(
            monitor_index=monitor_index,
            timestamp=int(ts),
            foreground_window=title[:160],
            foreground_app=app[:80],
            text_excerpt=excerpt(text, C.ACTIVITY_OCR_EXCERPT_MAX_CHARS),
            tier=tier,
            novelty=1.0 - jacc,
        )
        if m is not None:
            # Live-monitor path — feed the standard buffer/flush flow.
            m.last_text = text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4]
            m.recent_texts.append(text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4])
            m.append_sample(sample, time.time())
        else:
            # Orphan path — submit directly as a one-shot LTM candidate.
            # No m means no buffer to append to; we also skip
            # _emit_candidate's per-monitor preamble and render a
            # standalone block ourselves so the dream worker still has
            # readable context.
            try:
                raw = self._render_orphan_sample(sample)
                await submit_activity_observer(
                    ltm=self._ltm,
                    raw_text=raw,
                    source_ref=f"activity:m{monitor_index}:orphan:{int(ts)}",
                    daily_summary=False,
                    monitor_index=monitor_index,
                    sample_count=1,
                )
                _logger.info(
                    "orphan activity sample submitted monitor=%d ts=%s",
                    monitor_index, int(ts),
                )
            except Exception:
                _logger.exception("submit_activity_observer (orphan) failed")
        self._daily_buffer.append(sample)
        if len(self._daily_buffer) > _DAILY_BUFFER_HARD_CAP:
            drop = len(self._daily_buffer) - _DAILY_BUFFER_HARD_CAP
            self._daily_buffer = self._daily_buffer[drop:]

    def _render_orphan_sample(self, s: ActivitySample) -> str:
        """Render a one-shot ActivitySample as a tiny markdown block,
        matching the per-monitor preamble produced by
        :meth:`_render_samples` so dream-worker prompts see the same
        structure regardless of whether the source monitor is alive."""
        ts = datetime.fromtimestamp(s.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"# Activity observation (orphan — monitor disconnected)",
            f"Monitor {s.monitor_index} (no longer attached)",
            f"Samples: 1",
            "",
            f"## {ts} [{s.tier.value}] {s.foreground_app or '?'}",
        ]
        if s.foreground_window:
            lines.append(f"window: {s.foreground_window}")
        if s.text_excerpt:
            lines.append("text excerpt:")
            lines.append(s.text_excerpt)
        return "\n".join(lines)

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
        """Emit at most one daily-summary candidate per local calendar
        date. Called from the main tick loop (every ~1 s).

        Two interactions with the deferred-OCR pipeline make the naive
        "fire once at 22:00 if buffer non-empty" wrong:

        1. **Buffer-empty at 22:00 due to defer-OCR**: when the user
           works straight through 22:00 with no idle break, every
           captured frame is still sitting in the ring waiting for
           drain, so ``_daily_buffer`` (populated only AFTER OCR) is
           empty at the trigger moment. We must NOT mark
           ``_last_daily_date = today`` in that case — that would
           burn the whole 22:00–23:00 window even though drain may
           catch up later.

        2. **Drain mid-flight at 22:00**: if the user goes idle at
           21:55 and drain starts processing the day's ring, by 22:00
           only a fraction of frames have been OCR'd. Emitting then
           captures only that fraction; the rest fill the buffer
           AFTER the date_key was set and never enter today's summary.

        Strategy: stay live across the whole 22:00–22:59 window;
        in the first 30 minutes prefer to wait if drain is actively
        consuming pending work; from 22:30 onwards send what we have;
        at 23:00 (one-hour fallback) close the books even on an empty
        buffer so we don't cycle into tomorrow.
        """
        now_dt = datetime.now()
        date_key = now_dt.strftime("%Y-%m-%d")
        if self._last_daily_date == date_key:
            return  # already settled for today

        summary_hour = C.ACTIVITY_DAILY_SUMMARY_HOUR_LOCAL
        in_window = (now_dt.hour == summary_hour)
        past_deadline = (now_dt.hour == summary_hour + 1)
        if not (in_window or past_deadline):
            return  # off-window — wait for tomorrow's 22:00

        if not self._daily_buffer:
            # Bug fix: do NOT mark date_key as done just because buffer
            # is empty mid-window. Drain may catch up later in the
            # 22:00 hour. Only at the deadline (23:00) do we accept
            # that today truly had nothing to summarise.
            if past_deadline:
                self._last_daily_date = date_key
                _logger.info(
                    "daily activity summary skipped date=%s (no samples this day)",
                    date_key,
                )
            return

        # Buffer has at least one sample. Decide whether to emit now.
        # In the first 30 minutes of the window, if the drain worker is
        # actively consuming a substantial backlog, defer the emit so
        # late-arriving samples get included. After 30 minutes (or at
        # the 23:00 deadline) we send whatever we have.
        if in_window and now_dt.minute < 30:
            ring_size = sum(len(r) for r in self._rings.values())
            spill_size = self._spill_count()
            pending = ring_size + spill_size
            # ≥10 pending frames AND drain is unblocked = "wait for it"
            if pending >= 10 and self._gate_open_now and not self._paused:
                _logger.debug(
                    "daily summary deferred: pending=%d gate_open=True minute=%d",
                    pending, now_dt.minute,
                )
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
                "daily activity summary emitted date=%s samples=%d "
                "(in_window=%s past_deadline=%s)",
                date_key, len(samples), in_window, past_deadline,
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
