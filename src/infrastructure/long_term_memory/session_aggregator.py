"""Session aggregator — groups obs_snapshots into obs_sessions.

Background worker (spawned by LongTermMemory.init alongside DreamWorker)
that periodically scans for unassigned snapshots and binds them into
continuous-work sessions, then marks each session ``ended_at`` once it
sees a boundary.

Trigger rules
-------------
A new session starts when:
- The foreground **process changes** (and the new process holds for >3s)
- A **terminal process** opens (mintty/wsl/wezterm) AND the window title
  shows ``user@host`` — distinct SSH session per host
- An **RDP/VNC process** opens — distinct remote session per target host
- After **idle ≥ 10 min** when the prior session's last seen frame
  differs from the new snapshot's frame

A session ENDS when any of the above triggers a new session. session
``ended_at`` is stamped at the last snapshot's timestamp.

Idempotency
-----------
Each session is keyed by
    ``f"{trigger_kind}:{frame_host}:{started_at // 60000}"``
The UNIQUE index on obs_sessions.session_key guarantees re-aggregation
under crash + replay collapses into the same row.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from . import _constants as C
from .models import TriggerKind, ObsEventKind
from .frame_inference import LINUX_SSH_PROCESSES, REMOTE_DESKTOP_PROCESSES

_logger = logging.getLogger("handq.ltm.session_aggregator")

# Tunable: poll cadence + idle-gap threshold + foreground-debounce.
AGGREGATOR_TICK_SECONDS: float = 30.0
IDLE_GAP_MS: int = 10 * 60 * 1000           # 10 min in milliseconds
FOREGROUND_DEBOUNCE_SECONDS: float = 3.0
# Force a session boundary every MAX_OPEN_SESSION_MS of CONTINUOUS work
# (no idle gap, no app switch). Without this, an 8h IDE session is one
# unbroken obs_sessions row that never reaches ``ended_at IS NOT NULL``,
# starving SemanticExtractor (and therefore mem_entries) of the entire
# span. 60 min keeps the abstraction grain useful (one chunk = one
# obvious work block) without being so chatty it drowns the semantic
# event stream.
MAX_OPEN_SESSION_MS: int = 60 * 60 * 1000   # 60 min


class SessionAggregator:
    """Async worker that turns obs_snapshots into obs_sessions."""

    def __init__(self, store) -> None:
        self._store = store
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        """Long-running coroutine. Cancel + await to stop cleanly."""
        _logger.info("SessionAggregator started (tick=%.0fs)", AGGREGATOR_TICK_SECONDS)
        try:
            while not self._stopped.is_set():
                try:
                    n = await self._tick()
                    if n:
                        _logger.debug("SessionAggregator assigned %d snapshots", n)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("SessionAggregator tick failed")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=AGGREGATOR_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            _logger.info("SessionAggregator cancelled")
            raise

    def stop(self) -> None:
        self._stopped.set()

    async def _tick(self) -> int:
        """One pass: assign all currently-unassigned snapshots into sessions."""
        rows = await self._store.list_unassigned_snapshots(limit=500)
        if not rows:
            return 0

        # Sort by captured_at ascending (the query already does ORDER BY ASC,
        # but assert).
        # Row shape: (id, captured_at, monitor_index, process_name,
        #             window_title, frame_json, system_idle_sec, tier)
        assigned = 0
        current_session_id: Optional[str] = None
        current_process: Optional[str] = None
        current_host: Optional[str] = None
        current_started_at: int = 0
        last_captured_at: int = 0
        current_apps: set = set()
        current_snapshot_ids: list = []

        for r in rows:
            snap_id, captured_at, monitor_idx, proc, title, frame_json, idle_sec, tier = r
            proc = (proc or "").lower()
            host = _extract_host(frame_json)

            new_session = False
            trigger = TriggerKind.APP_SWITCH

            if current_session_id is None:
                new_session = True
                trigger = _infer_trigger_kind(proc, host)
            elif captured_at - last_captured_at > IDLE_GAP_MS:
                new_session = True
                trigger = TriggerKind.IDLE_RESUME
            elif current_started_at and (captured_at - current_started_at) > MAX_OPEN_SESSION_MS:
                # Force a rollover: same process / host, but the session has
                # been open longer than MAX_OPEN_SESSION_MS. Without this an
                # all-day single-app workflow stays one unbroken session that
                # never reaches ``ended_at IS NOT NULL``, so SemanticExtractor
                # never abstracts it.
                new_session = True
                trigger = TriggerKind.LONG_RUN
            elif proc and proc != current_process:
                # Process changed — start new session (3s debounce is
                # implicit because we only see snapshots spaced by tick
                # interval; in practice consecutive snapshots are >3s apart
                # so debounce naturally falls out of sampling cadence).
                new_session = True
                trigger = _infer_trigger_kind(proc, host)
            elif host and host != current_host and host != "unknown":
                # Same process but new host (e.g. SSH'd to different machine).
                new_session = True
                trigger = TriggerKind.SSH_START

            if new_session:
                # Close previous session
                if current_session_id and current_snapshot_ids:
                    try:
                        await self._store.close_obs_session(
                            current_session_id,
                            ended_at=last_captured_at,
                            snapshot_count=len(current_snapshot_ids),
                            apps_seen=sorted(current_apps),
                        )
                    except Exception:
                        _logger.exception(
                            "close_obs_session failed sid=%s", current_session_id[:8],
                        )
                # Open new session
                # session_key includes process so two app_switch sessions with
                # the same frame_host (e.g. powershell.exe → chrome.exe both on
                # 'local') don't collide on the bucket-by-minute timestamp.
                session_key = (
                    f"{trigger.value}:{proc or 'none'}:"
                    f"{host or 'unknown'}:{captured_at // 60000}"
                )
                try:
                    current_session_id = await self._store.insert_obs_session(
                        session_key=session_key,
                        trigger_kind=trigger.value,
                        started_at=captured_at,
                        frame_os=_extract_os(frame_json),
                        frame_host=host,
                        primary_process=proc or None,
                        primary_window_title=title,
                    )
                    # Drop an obs_event marking the boundary — but skip for
                    # LONG_RUN, which is an internal rollover triggered by us
                    # rather than a state change observable to the user.
                    if trigger != TriggerKind.LONG_RUN:
                        await self._store.insert_obs_event(
                            session_id=current_session_id,
                            kind=ObsEventKind.FOREGROUND_CHANGE.value
                                if trigger == TriggerKind.APP_SWITCH
                                else (
                                    ObsEventKind.SSH_CONNECT.value
                                    if trigger == TriggerKind.SSH_START
                                    else (
                                        ObsEventKind.RDP_CONNECT.value
                                        if trigger == TriggerKind.RDP_START
                                        else ObsEventKind.IDLE_RESUME.value
                                    )
                                ),
                            data={"process": proc, "host": host},
                            sort_order=0,
                            occurred_at=captured_at,
                        )
                except Exception:
                    _logger.exception(
                        "insert_obs_session failed key=%s", session_key,
                    )
                    continue
                current_process = proc
                current_host = host
                current_started_at = captured_at
                current_apps = set()
                current_snapshot_ids = []

            # Bind snapshot to current session
            try:
                await self._store.assign_snapshot_to_session(snap_id, current_session_id)
                assigned += 1
            except Exception:
                _logger.exception("assign_snapshot_to_session failed snap=%s", snap_id[:8])
            if proc:
                current_apps.add(proc)
            current_snapshot_ids.append(snap_id)
            last_captured_at = captured_at

        # Close the trailing open session when one of:
        #   - it's been idle longer than IDLE_GAP_MS (the user walked away);
        #   - or it's been continuously open longer than MAX_OPEN_SESSION_MS
        #     (long-running workflow — force a boundary so SemanticExtractor
        #     can pick it up without waiting for an idle gap that may never
        #     come).
        # ``ended_at`` is stamped at the latest snapshot we saw — for the
        # MAX_OPEN_SESSION_MS branch this still represents the last observed
        # activity, and the next tick's first snapshot will start a fresh
        # session via the LONG_RUN branch above.
        if current_session_id and current_snapshot_ids:
            now_ms = int(time.time() * 1000)
            should_close = (
                now_ms - last_captured_at > IDLE_GAP_MS
                or now_ms - current_started_at > MAX_OPEN_SESSION_MS
            )
            if should_close:
                try:
                    await self._store.close_obs_session(
                        current_session_id,
                        ended_at=last_captured_at,
                        snapshot_count=len(current_snapshot_ids),
                        apps_seen=sorted(current_apps),
                    )
                except Exception:
                    _logger.exception(
                        "close_obs_session (trailing) failed sid=%s",
                        current_session_id[:8],
                    )

        return assigned


def _extract_host(frame_json: Optional[str]) -> Optional[str]:
    if not frame_json:
        return None
    import json
    try:
        d = json.loads(frame_json)
        return d.get("host") if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_os(frame_json: Optional[str]) -> Optional[str]:
    if not frame_json:
        return None
    import json
    try:
        d = json.loads(frame_json)
        return d.get("os") if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _infer_trigger_kind(process: str, host: Optional[str]) -> TriggerKind:
    proc = (process or "").lower()
    if proc in LINUX_SSH_PROCESSES and host and host != "unknown":
        return TriggerKind.SSH_START
    if proc in REMOTE_DESKTOP_PROCESSES:
        return TriggerKind.RDP_START
    return TriggerKind.APP_SWITCH
