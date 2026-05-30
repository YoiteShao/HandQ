"""ScreenshotStore — tiered scratch storage for vision artifacts.

Three categories, deliberately few. Anything the agent should keep
**long-term** belongs in the session directory under
``%USERPROFILE%\\HandQ\\History\\<id>\\``, not here. This store is
strictly scratch space.

| Category   | Producer (today)                  | Lifetime                              |
|------------|-----------------------------------|---------------------------------------|
| ephemeral  | browser.vision_query (Phase 1)    | LRU + age, every-write sweep;         |
|            | desktop.find_element (Phase 2)    | full purge at session boundary        |
| task       | browser.screenshot (default)      | Aged sweep at session close;          |
|            | desktop.screenshot  (Phase 2)     | retain_after_task_days bounds it      |
| activity   | activity_monitor    (Phase 3)     | Aged + LRU; producer-exclusive write  |

Each producer holds its OWN ScreenshotStore instance with a different
``root`` directory (browser → ``browser_profile/screenshots/``, desktop
→ ``desktop_shots/``, activity_monitor → ``activity/``). The
``ephemeral`` and ``task`` retention limits come from the
``handq_config.yaml`` ``screenshots:`` section (user-tunable). The
``activity`` retention is a debug-only backstop and lives in
``long_term_memory/_constants.py`` (``ACTIVITY_SCREENSHOT_*``) — see
``_category_cfg`` for the lookup.

Cleanup is amortised at write time (each ``enforce_retention`` call
runs LRU + age in one stat-sort-unlink pass) plus a full
``session_close_sweep`` at the session boundary. No background timer.

See ARCHITECTURE.md §1.6 for the full categorisation contract.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Tuple

from ..logger import get_logger


_CATEGORIES: Tuple[str, ...] = ("ephemeral", "task", "activity")


class ScreenshotStore:
    """Tiered file store for screenshots / vision input frames.

    Thread-safety: each method does at most one stat sweep + a series
    of unlinks. Concurrent writers from the same producer are fine
    because filenames are timestamped to microseconds. Retention is
    advisory — over-cap by a few files for a beat is harmless.
    """

    CATEGORIES: Tuple[str, ...] = _CATEGORIES

    def __init__(self, root: str, config_section: Any = None) -> None:
        """Bind a root directory and a ``screenshots:`` config section.

        ``config_section`` may be a dict (already extracted from
        :class:`ConfigManager`) or None. None means "use defaults" —
        useful for tests and for early-init code paths where the
        config manager hasn't been built yet.
        """
        self.root = os.path.abspath(root)
        self._cfg: Dict[str, Any] = config_section or {}
        self._logger = get_logger()

    # ── Path resolution ──────────────────────────────────────────────────────

    def subdir(self, category: str) -> str:
        """Return (and create) the directory for *category*.

        Unknown categories fall back to ``task`` rather than raising —
        a config typo or a bad caller never blocks a screenshot write.
        """
        cat = category if category in _CATEGORIES else "task"
        path = os.path.join(self.root, cat)
        os.makedirs(path, exist_ok=True)
        return path

    # ── Retention helpers ────────────────────────────────────────────────────

    def _category_cfg(self, category: str) -> Dict[str, Any]:
        # The `activity` tier is a debug-only backstop for the
        # activity_monitor (frames are normally unlinked the moment OCR
        # returns; this only matters when ACTIVITY_KEEP_FRAME_FILES is
        # flipped on for debugging). Its caps live in
        # long_term_memory/_constants.py alongside every other ACTIVITY_*
        # knob, NOT in handq_config.yaml — wrong values silently degrade
        # disk hygiene and we don't want users touching them.
        if category == "activity":
            from ..long_term_memory import _constants as _C
            return {
                "max_files": _C.ACTIVITY_SCREENSHOT_MAX_FILES,
                "max_age_days": _C.ACTIVITY_SCREENSHOT_MAX_AGE_DAYS,
            }
        return (self._cfg.get(category) or {}) if isinstance(self._cfg, dict) else {}

    def enforce_retention(self, category: str) -> None:
        """LRU + age sweep on one category. Called after every write so
        cleanup amortises across writes (no separate timer task).

        Limits:
          * ephemeral / task — read from ``handq_config.yaml``
            (``screenshots:`` section passed to ``__init__``)
          * activity         — read from ``long_term_memory/_constants.py``
            (``ACTIVITY_SCREENSHOT_*``); see ``_category_cfg``
        Fields per category:
          * ``max_files``       — drop oldest beyond this count
          * ``max_age_minutes`` — used by ephemeral
          * ``max_age_days``    — used by activity

        Best-effort: any IO error is logged at debug and swallowed.
        """
        cfg = self._category_cfg(category)
        max_files = int(cfg.get("max_files", 0) or 0)
        max_age_min = int(cfg.get("max_age_minutes", 0) or 0)
        max_age_days = float(cfg.get("max_age_days", 0) or 0)
        if max_files <= 0 and max_age_min <= 0 and max_age_days <= 0:
            return
        sub = self.subdir(category)
        try:
            entries = []
            for name in os.listdir(sub):
                p = os.path.join(sub, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                # Regular files only.
                if (st.st_mode & 0o170000) != 0o100000:
                    continue
                entries.append((p, st.st_mtime))
        except OSError:
            return
        if not entries:
            return
        now = time.time()
        cutoff = 0.0
        if max_age_min > 0:
            cutoff = max(cutoff, now - max_age_min * 60.0)
        if max_age_days > 0:
            cutoff = max(cutoff, now - max_age_days * 86400.0)
        if cutoff > 0:
            for p, mtime in entries:
                if mtime < cutoff:
                    self._unlink(p)
            entries = [(p, m) for (p, m) in entries if os.path.exists(p)]
        if max_files > 0 and len(entries) > max_files:
            entries.sort(key=lambda x: x[1])  # oldest first
            for p, _m in entries[: len(entries) - max_files]:
                self._unlink(p)

    def purge_category(self, category: str) -> int:
        """Delete every file in *category*. Returns count deleted.

        Used by ``session_close_sweep`` for the ephemeral tier and by
        emergency cleanup paths.
        """
        sub = self.subdir(category)
        n = 0
        try:
            for name in os.listdir(sub):
                if self._unlink(os.path.join(sub, name)):
                    n += 1
        except OSError:
            pass
        return n

    def purge_aged(self, category: str, days: float) -> int:
        """Delete files older than *days* in *category*. Returns count.

        ``days <= 0`` means "purge everything" (delegates to
        :meth:`purge_category`).
        """
        if days <= 0:
            return self.purge_category(category)
        sub = self.subdir(category)
        cutoff = time.time() - days * 86400.0
        n = 0
        try:
            for name in os.listdir(sub):
                p = os.path.join(sub, name)
                try:
                    if os.path.getmtime(p) < cutoff:
                        if self._unlink(p):
                            n += 1
                except OSError:
                    pass
        except OSError:
            pass
        return n

    def session_close_sweep(self) -> Dict[str, int]:
        """Run the session-boundary cleanup contract:

          * ephemeral → fully purged (vision work files do not cross
            sessions)
          * task      → aged sweep using
            ``screenshots.task.retain_after_task_days`` (default 1)

        Activity is NOT touched here — its lifecycle is owned by the
        activity_monitor service and its retention runs via
        :meth:`enforce_retention` on each capture.

        Returns ``{"ephemeral": n_eph, "task": n_task}`` for logging.
        """
        out = {"ephemeral": 0, "task": 0}
        try:
            out["ephemeral"] = self.purge_category("ephemeral")
        except Exception as exc:
            self._logger.debug(f"session_close_sweep ephemeral: {exc}",
                               component="ScreenshotStore")
        try:
            retain_days = float(self._category_cfg("task").get("retain_after_task_days", 1))
            out["task"] = self.purge_aged("task", retain_days)
        except Exception as exc:
            self._logger.debug(f"session_close_sweep task: {exc}",
                               component="ScreenshotStore")
        return out

    # ── Internal ─────────────────────────────────────────────────────────────

    def _unlink(self, path: str) -> bool:
        try:
            os.remove(path)
            return True
        except OSError:
            return False
