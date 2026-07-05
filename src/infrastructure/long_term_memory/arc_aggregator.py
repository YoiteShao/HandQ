"""Activity arc aggregator — groups obs_sessions into obs_arcs.

Background worker (spawned by LongTermMemory.init alongside SessionAggregator
and DreamWorker) that periodically scans for closed sessions not yet assigned
to an arc and binds them into continuous activity arcs.

Unlike SessionAggregator (which cuts on app switch), ArcAggregator cuts ONLY
on real idle gaps (>= ARC_IDLE_GAP_MS). Continuous app switching (VS Code →
Browser → Terminal) stays within one arc. This gives SemanticExtractor enough
cross-app context to distill genuine workflow patterns.

Arc lifecycle:
  1. Aggregator polls for unassigned closed sessions (ended_at IS NOT NULL,
     arc_id IS NULL).
  2. Sessions are sorted by started_at and assigned to arcs based on gap
     between consecutive sessions:
       gap < ARC_IDLE_GAP_MS  → same arc
       gap >= ARC_IDLE_GAP_MS → close current arc, start new one
  3. Duration cap: arcs exceeding ARC_MAX_DURATION_MS are closed even without
     a gap (prevents unbounded growth from continuous 8h coding sessions).
  4. Closed arcs with session_count >= ARC_MIN_SESSIONS get
     semantic_status='pending' for SemanticExtractor; below that threshold
     they get 'skip'.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import List, Optional

from . import _constants as C

_logger = logging.getLogger("handq.ltm.arc_aggregator")


class ArcAggregator:
    """Async worker that turns obs_sessions into obs_arcs."""

    def __init__(self, store) -> None:
        self._store = store
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        _logger.info(
            "ArcAggregator started (tick=%.0fs, idle_gap=%dmin)",
            C.ARC_AGGREGATOR_TICK_SEC,
            C.ARC_IDLE_GAP_MS // 60_000,
        )
        try:
            while not self._stopped.is_set():
                try:
                    n = await self._tick()
                    if n:
                        _logger.debug("ArcAggregator closed %d arcs", n)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("ArcAggregator tick failed")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=C.ARC_AGGREGATOR_TICK_SEC,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        _logger.info("ArcAggregator stopped")

    def stop(self) -> None:
        self._stopped.set()

    async def _tick(self) -> int:
        rows = await self._store.list_sessions_unassigned_to_arc(limit=200)
        if not rows:
            return 0

        # rows: (id, started_at, ended_at, frame_os, frame_host)
        sessions = [
            {
                "id": r[0],
                "started_at": r[1],
                "ended_at": r[2],
                "frame_os": r[3],
                "frame_host": r[4],
            }
            for r in rows
        ]

        arcs_closed = 0
        current_arc_sessions: List[dict] = []
        current_arc_start: Optional[int] = None

        for sess in sessions:
            if not current_arc_sessions:
                current_arc_sessions.append(sess)
                current_arc_start = sess["started_at"]
                continue

            prev = current_arc_sessions[-1]
            gap = sess["started_at"] - (prev["ended_at"] or prev["started_at"])

            duration = sess["started_at"] - (current_arc_start or sess["started_at"])
            force_close = duration >= C.ARC_MAX_DURATION_MS

            if gap >= C.ARC_IDLE_GAP_MS or force_close:
                arcs_closed += await self._close_arc(current_arc_sessions)
                current_arc_sessions = [sess]
                current_arc_start = sess["started_at"]
            else:
                current_arc_sessions.append(sess)

        # Remaining sessions: check if we can close them.
        # Only close if there's evidence the arc ended (the last session
        # ended long enough ago that the idle gap has passed).
        if current_arc_sessions:
            last = current_arc_sessions[-1]
            now_ms = int(asyncio.get_event_loop().time() * 1000)
            last_end = last["ended_at"] or last["started_at"]
            idle_since_last = now_ms - last_end

            duration = last_end - (current_arc_start or last["started_at"])

            if idle_since_last >= C.ARC_IDLE_GAP_MS or duration >= C.ARC_MAX_DURATION_MS:
                arcs_closed += await self._close_arc(current_arc_sessions)
            # else: arc is still open — leave sessions unassigned until
            # more arrive or idle gap is reached

        return arcs_closed

    async def _close_arc(self, sessions: List[dict]) -> int:
        if not sessions:
            return 0

        arc_id = str(uuid.uuid4())
        started_at = sessions[0]["started_at"]
        ended_at = sessions[-1]["ended_at"] or sessions[-1]["started_at"]
        session_count = len(sessions)

        frame_os = sessions[0].get("frame_os")
        frame_host = sessions[0].get("frame_host")

        status = "pending" if session_count >= C.ARC_MIN_SESSIONS else "skip"

        await self._store.insert_obs_arc(
            arc_id=arc_id,
            started_at=started_at,
            ended_at=ended_at,
            session_count=session_count,
            frame_os=frame_os,
            frame_host=frame_host,
            semantic_status=status,
        )

        session_ids = [s["id"] for s in sessions]
        await self._store.assign_sessions_to_arc(session_ids, arc_id)

        _logger.info(
            "Arc %s closed: %d sessions, %dmin span, status=%s",
            arc_id[:8], session_count,
            (ended_at - started_at) // 60_000, status,
        )
        return 1
