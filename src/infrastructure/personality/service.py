"""Async PersonalityMonitor service.

Owns one asyncio task that loops every few hundred ms, polls each
:class:`MonitorState`, captures only the monitors whose adaptive
cadence says it's time, and writes interesting frames into the LTM
observation pipeline as ``obs_snapshots`` + ``obs_ocr_frames`` rows.

Public API
----------
- ``PersonalityMonitor.start(loop)``      — kick off the background task.
- ``PersonalityMonitor.shutdown()``       — graceful cancel + ring spill.
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
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import yaml

from ..long_term_memory import _constants as C
from ..long_term_memory.frame_inference import infer_frame
from ..long_term_memory.models import (
    ActivitySample, MonitorTier,
)
from ..long_term_memory.uia_worker import get_uia_worker
from ..vision.ocr import LocalOCR, get_local_ocr_background
from ..vision.storage import ScreenshotStore
from .capturer import MonitorCapturer, get_foreground_window_rect
from .frame_diff import (
    excerpt, hamming, perceptual_hash_array, text_jaccard,
)
from .input_idle import (
    cursor_in_monitor, cursor_pos, enumerate_monitors,
    foreground_app_name, foreground_hwnd, foreground_window_title,
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


def _infer_sample_frame(sample: ActivitySample) -> Optional[dict]:
    """Infer LTM 2.0 frame from one ActivitySample's foreground signals."""
    proc = sample.foreground_app or ""
    title = sample.foreground_window or ""
    if not proc and not title:
        return None
    return infer_frame(proc, title)


def _foreground_home_monitor_index(
    fg_rect: Optional[Tuple[int, int, int, int]],
    monitors: List[MonitorState],
) -> Optional[int]:
    """Return the index of the monitor the foreground window sits on.

    "Home monitor" = the monitor whose bbox has the greatest overlap area
    with ``fg_rect`` (left, top, right, bottom). Returns ``None`` when there
    is no rect or no positive overlap. Used to attach UIA ``ax_text`` to
    exactly one snapshot, regardless of how much of the monitor the window
    covers — unlike ``used_focus_rect``, which excludes maximized windows
    (coverage > 80%).
    """
    if not fg_rect:
        return None
    wl, wt, wr, wb = fg_rect
    best_idx: Optional[int] = None
    best_area = 0
    for m in monitors:
        ml, mt, mr, mb = m.info.bbox
        iw = min(wr, mr) - max(wl, ml)
        ih = min(wb, mb) - max(wt, mt)
        if iw <= 0 or ih <= 0:
            continue
        area = iw * ih
        if area > best_area:
            best_area = area
            best_idx = m.info.index
    return best_idx


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
    spills any unprocessed ring frames to disk best-effort.

    The instance is INERT until ``start()`` returns — calling
    :meth:`pause` / :meth:`resume` before start is a no-op so the
    bridge can wire IPC handlers safely during init.

    Configuration contract
    ----------------------
    The constructor takes a ``config_path`` (mirroring
    ``LongTermMemory.init``); we read **only** the ``screenshots:``
    section from it, which drives ``ScreenshotStore`` retention
    bounds. Everything else (sampling cadence, hash thresholds,
    sensitive-window patterns, …) lives in
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
        # Use the BACKGROUND singleton — capped ONNX thread pool so the
        # drain doesn't peg every core back-to-back when the IDLE gate
        # opens. desktop_tool keeps the full-fat singleton.
        try:
            self._ocr = get_local_ocr_background()
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
        try:
            self._capturer.close()
        except Exception:
            pass
        # Reap the UIA worker's PowerShell subprocess — the personality
        # service is its sole consumer, so it owns the lifecycle.
        try:
            await get_uia_worker().shutdown()
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
        # Read the foreground window once per tick and resolve its home
        # monitor (greatest bbox overlap). UIA ax_text is queried only for
        # that monitor's capture so a maximized window is still covered and
        # the structured text is attached to exactly one snapshot.
        fg_hwnd = foreground_hwnd()
        fg_home_idx = _foreground_home_monitor_index(
            get_foreground_window_rect(fg_hwnd) if fg_hwnd else None,
            self._monitors,
        )
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
                await self._capture_and_process(
                    m, title, app, now, fg_hwnd, fg_home_idx,
                )
            except Exception:
                _logger.exception("capture/process failed monitor=%d", m.info.index)

    # ── Topology reconciliation ────────────────────────────────────────────

    async def _reconcile_monitors(self, now: float) -> None:
        """Re-enumerate displays and reconcile :attr:`_monitors`.

        Runs at most once every ``_MONITOR_RECONCILE_INTERVAL_SEC``.
        Identifies "the same monitor" across enumerations by its
        ``(left, top)`` virtual-screen corner — stable across resolution
        changes (the OS keeps the corner pinned), only flips when the
        user rearranges displays in Windows settings or unplugs one.

        Behaviour:
          - Vanished corners → spill the monitor's unprocessed ring
            frames to disk, then drop the state. Spill is best-effort;
            a failure here only loses the in-flight frames.
          - New corners → append a fresh ``MonitorState`` with a new
            ``index`` (max existing + 1) so it doesn't collide with a
            stamped-but-vanished display.
          - Resolution flip on a stable corner → update bbox/label in
            place, preserve tier/text-history, drop ``last_hash``
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
        fg_hwnd: Optional[int],
        fg_home_idx: Optional[int],
    ) -> None:
        """Capture the monitor, decide if the frame is novel, and push it
        into the per-monitor ring buffer for later OCR.

        OCR is intentionally NOT run here — it's deferred to
        :meth:`_ocr_drain_loop` so it doesn't compete with the user's
        CPU during active work. See plan robust-gathering-shannon.md
        for the design rationale + bench numbers.

        ``fg_hwnd`` / ``fg_home_idx`` are the tick-scoped foreground window
        handle and its home-monitor index. When this monitor IS the home
        monitor we query the UIA worker for structured ``ax_text`` and carry
        it through the ring entry — bound to the correct window here rather
        than at drain time, when the foreground may have changed.
        """
        loop = asyncio.get_running_loop()
        # 1. Capture → ndarray (no disk I/O). Prefer focus-rect capture
        #    (foreground window bbox only) so OCR runs over fewer pixels;
        #    falls back to full monitor when foreground hwnd is unavailable
        #    or the foreground covers >80% of the monitor anyway.
        rgb, focus_rect, used_focus_rect = await loop.run_in_executor(
            None,
            lambda: self._capturer.capture_focus_rect(m.info, foreground_hwnd=fg_hwnd),
        )
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
        # 6. UIA structured text for the foreground window — only on its
        #    home monitor, so a multi-monitor desktop attaches ax_text to a
        #    single snapshot. Best-effort: query() returns None on any
        #    failure / non-Windows / timeout, leaving the fields unset.
        ax_text: Optional[str] = None
        parsed_json: Optional[dict] = None
        top_window_titles: Optional[List[str]] = None
        if fg_hwnd and m.info.index == fg_home_idx:
            uia = await get_uia_worker().query(fg_hwnd)
            if uia:
                ax_text = uia.get("ax_text") or None
                parsed_json = uia.get("parsed_json") or None
                top_window_titles = uia.get("top_window_titles") or None
        ring.append({
            "jpeg": jpeg,
            "ts": now,
            "title": title,
            "app": app,
            "tier": m.tier,
            "monitor_index": m.info.index,
            "focus_rect": focus_rect,
            "ocr_used_focus_rect": used_focus_rect,
            "ax_text": ax_text,
            "parsed_json": parsed_json,
            "top_window_titles": top_window_titles,
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
        text-similarity dedup and (if accepted) write the observation to
        ``obs_snapshots`` + ``obs_ocr_frames``.

        The Jaccard dedup history comes from one of two sources:
          * **Live monitor** — the originating ``MonitorState`` is still
            present; use its ``recent_texts`` / ``last_text`` (and update
            them with the accepted text so future frames dedup against it).
          * **Orphan** — the monitor disconnected between capture and
            drain (frame survived via spillover). Fall back to the
            ``recent_texts_snapshot`` shipped inside the spilled
            meta.json for dedup; there is no live state to update.
        """
        loop = asyncio.get_running_loop()
        if self._ocr is None:
            try:
                self._ocr = get_local_ocr_background()
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
        # LTM 2.0 obs_snapshots + obs_ocr_frames write path — the sole
        # sink for activity observations. Downstream the SemanticExtractor
        # abstracts these into obs_semantic_events and the DreamWorker
        # triages those into mem_entries.
        try:
            frame = _infer_sample_frame(sample)
            focus_rect = entry.get("focus_rect") if isinstance(entry, dict) else None
            used_focus_rect = bool(entry.get("ocr_used_focus_rect")) if isinstance(entry, dict) else False
            # UIA structured text captured at snapshot time (home monitor
            # only); absent for orphan/spilled frames — None-safe.
            ax_text = entry.get("ax_text") if isinstance(entry, dict) else None
            parsed_json = entry.get("parsed_json") if isinstance(entry, dict) else None
            top_window_titles = entry.get("top_window_titles") if isinstance(entry, dict) else None
            snapshot_id = await self._ltm._store.insert_obs_snapshot(
                captured_at=int(ts * 1000),
                monitor_index=monitor_index,
                window_title=title or None,
                process_name=app or None,
                top_window_titles=top_window_titles,
                ax_text=ax_text,
                parsed_json=parsed_json,
                frame=frame,
                focus_rect=focus_rect,
                ocr_used_focus_rect=used_focus_rect,
                system_idle_sec=None,
                novelty_score=1.0 - jacc,
                tier=tier.value,
            )
            await self._ltm._store.insert_obs_ocr_frame(
                snapshot_id=snapshot_id,
                text=text,
                confidence=getattr(ocr_result, "confidence", None),
                pipeline_version="rapidocr_v1.x",
                is_focus_rect=used_focus_rect,
            )
        except Exception:
            # Non-fatal — one dropped frame must not kill the drain loop —
            # but as the only sink the failure should be visible.
            _logger.exception("obs_snapshots write failed")
        if m is not None:
            # Live-monitor path — update the Jaccard dedup history so the
            # next frame on this monitor dedups against this accepted text.
            m.last_text = text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4]
            m.recent_texts.append(text[: C.ACTIVITY_OCR_EXCERPT_MAX_CHARS * 4])

