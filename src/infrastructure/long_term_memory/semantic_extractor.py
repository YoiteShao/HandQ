"""Semantic extractor — LLM-abstracts obs_arcs into obs_semantic_events.

Background worker (spawned by LongTermMemory.init alongside DreamWorker,
SessionAggregator, and ArcAggregator). For each closed arc with
``semantic_status='pending'`` and session_count >= ARC_MIN_SESSIONS it:

  1. Pulls all sessions within the arc + their snapshots + ocr_frames
  2. Renders a time-ordered activity sequence
  3. Sends to LLM with ARC_EXTRACTION_SYSTEM_PROMPT asking for workflow
     patterns, decision tendencies, tool preferences, etc.
  4. Writes obs_semantic_events with synthetic_origin=arc.id
  5. Marks arc ``semantic_status='done'``

Design shift (v2): session-level extraction is removed. Single-app OCR
fragments had <5% hit rate. Arc-level sees the full cross-app sequence
(VS Code → Browser → Terminal) which provides enough context for
meaningful pattern distillation.

The session-trajectory path (submit_session_complete) is unaffected — it
feeds candidates directly to DreamWorker through a separate channel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import List, Optional

from . import _constants as C
from .pii import PIIFilter

_logger = logging.getLogger("handq.ltm.semantic_extractor")

EXTRACTOR_TICK_SECONDS: float = 60.0


ARC_EXTRACTION_SYSTEM_PROMPT = """\
You distill a USER's working patterns from a sequence of desktop activity observations.

## Goal
Extract anything that helps understand HOW this person works — their thinking patterns,
decision-making approach, problem-solving flow, tool preferences, communication style.

## What to extract (any of these):
- WORKFLOW PATTERNS: "encounters error → searches first → then fixes → verifies immediately"
- DECISION TENDENCIES: "prioritizes latency over readability", "always checks logs before code"
- KNOWLEDGE DEPTH: "deep understanding of K8s networking", "new to React"
- WORK HABITS: "morning = coding, afternoon = review", "tests before commit"
- TOOL PREFERENCES: "uses ripgrep over grep", "prefers terminal over GUI"
- COMMUNICATION PATTERNS: "terse with code reviews", "detailed in design docs"

## What NOT to extract:
- Single isolated actions without pattern evidence (one search ≠ a habit)
- Routine UI operations (opening apps, clicking menus)
- Content the user is passively viewing without engagement
- Anything requiring fewer than 2 corroborating observations in this arc

## Quality gate:
- worth_storing=true ONLY when the pattern is evidenced by ≥2 observations in this arc
- A single observation is NOT enough — it might be a one-off

## Input format:
You receive a time-ordered sequence of activity sessions within one arc:
  [HH:MM] AppName — WindowTitle: <OCR excerpt or ax_text summary>

## Output (STRICT JSON, no prose):
{
  "worth_storing": bool,
  "title": str (≤120 chars, pattern-first, no "User..." prefix),
  "description": str (1-3 sentences, cite specific apps/commands/sequences observed),
  "category": "workflow|decision|knowledge|habit|communication|other",
  "apps": list[str],
  "frame_confidence": float (0.0-1.0)
}
"""


class SemanticExtractor:
    """Async worker: obs_arcs(pending) → obs_semantic_events."""

    def __init__(
        self,
        store,
        llm_services: Optional[list] = None,
        pii_filter: Optional[PIIFilter] = None,
    ) -> None:
        self._store = store
        self._llm_services = llm_services or []
        self._pii = pii_filter
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        _logger.info(
            "SemanticExtractor started (tick=%.0fs, llm_available=%s, mode=arc)",
            EXTRACTOR_TICK_SECONDS, bool(self._llm_services),
        )
        try:
            while not self._stopped.is_set():
                try:
                    n = await self._tick()
                    if n:
                        _logger.debug("SemanticExtractor processed %d arcs", n)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("SemanticExtractor tick failed")
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=EXTRACTOR_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            _logger.info("SemanticExtractor cancelled")
            raise

    def stop(self) -> None:
        self._stopped.set()

    async def _tick(self) -> int:
        """One pass over pending closed arcs."""
        arcs = await self._store.list_arcs_pending_extraction(limit=4)
        if not arcs:
            return 0

        processed = 0
        for arc_row in arcs:
            (arc_id, started_at, ended_at, session_count, frame_os, frame_host) = arc_row
            try:
                event_id = await self._extract_arc(
                    arc_id=arc_id,
                    started_at=started_at,
                    ended_at=ended_at or started_at,
                    frame_os=frame_os,
                    frame_host=frame_host,
                )
                status = "done" if event_id else "skip"
                await self._store.set_obs_arc_status(arc_id, status)
                if event_id:
                    processed += 1
            except Exception:
                _logger.exception("extract failed arc=%s", arc_id[:8])
                try:
                    await self._store.set_obs_arc_status(arc_id, "skip")
                except Exception:
                    pass
        return processed

    async def _extract_arc(
        self,
        *,
        arc_id: str,
        started_at: int,
        ended_at: int,
        frame_os: Optional[str],
        frame_host: Optional[str],
    ) -> Optional[str]:
        """Extract patterns from one arc's worth of activity."""
        session_ids = await self._store.get_arc_session_ids(arc_id)
        if not session_ids:
            return None

        # Build the time-ordered activity sequence for the LLM
        activity_lines: List[str] = []
        all_apps: List[str] = []

        for sid in session_ids:
            snapshots = await self._store._fetchall(
                "SELECT id, captured_at, window_title, process_name, ax_text "
                "FROM obs_snapshots WHERE session_id=? "
                "ORDER BY captured_at ASC LIMIT ?",
                (sid, C.ARC_MAX_SNAPSHOTS_PER_PROMPT // max(len(session_ids), 1)),
            )
            for snap in snapshots:
                snap_id, captured_at, win_title, proc_name, ax_text = snap
                # Get OCR text
                ocr_rows = await self._store._fetchall(
                    "SELECT text FROM obs_ocr_frames WHERE snapshot_id=? LIMIT 1",
                    (snap_id,),
                )
                ocr_text = ocr_rows[0][0][:300] if ocr_rows and ocr_rows[0][0] else ""
                content = ax_text[:300] if ax_text else ocr_text

                ts = time.strftime("%H:%M", time.localtime(captured_at / 1000))
                app = proc_name or "unknown"
                title = win_title or ""
                line = f"[{ts}] {app} — {title}: {content[:200]}"
                activity_lines.append(line)
                if proc_name and proc_name not in all_apps:
                    all_apps.append(proc_name)

        if len(activity_lines) < 2:
            return None

        # Cap total prompt size
        activity_lines = activity_lines[:C.ARC_MAX_SNAPSHOTS_PER_PROMPT]

        if not self._llm_services:
            return None

        verdict = await self._llm_extract_arc(activity_lines, frame_os, frame_host)
        if verdict is None:
            return None

        if not verdict.get("worth_storing"):
            return None

        # PII gate
        if self._pii is not None:
            blob = (verdict.get("title") or "") + "\n" + (verdict.get("description") or "")
            if self._pii.has_secret(blob):
                _logger.info("arc %s skipped: PII detected", arc_id[:8])
                return None

        event_id = await self._store.insert_obs_semantic_event(
            session_id=None,
            synthetic_origin=arc_id,
            title=verdict.get("title", "(no title)"),
            description=verdict.get("description", ""),
            category=verdict.get("category"),
            entities=[],
            apps=verdict.get("apps") or all_apps,
            time_range_start=started_at,
            time_range_end=ended_at,
            task_worthy=False,
            worth_memory=True,
            worth_knowledge=False,
            worth_skill=False,
            frame_os=frame_os,
            frame_host=frame_host,
            frame_confidence=float(verdict.get("frame_confidence", 0.7)),
        )
        return event_id

    async def _llm_extract_arc(
        self,
        activity_lines: List[str],
        frame_os: Optional[str],
        frame_host: Optional[str],
    ) -> Optional[dict]:
        if not self._llm_services:
            return None

        activity_text = "\n".join(activity_lines)
        user_prompt = (
            f"Activity arc ({len(activity_lines)} observations, "
            f"frame: os={frame_os}, host={frame_host}):\n\n"
            f"{activity_text[:6000]}"
        )

        svc = self._llm_services[0]
        text = await _llm_chat_text(
            svc,
            [
                {"role": "system", "content": ARC_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            _logger.debug("LLM output not valid JSON: %s", text[:200])
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed


async def _llm_chat_text(svc, messages: list) -> Optional[str]:
    """Drive AnthropicStreamingService.chat_stream and return the result.content."""
    if not hasattr(svc, "chat_stream"):
        _logger.warning("LLM service %s has no chat_stream method", type(svc).__name__)
        return None
    try:
        async for ev in svc.chat_stream(messages=messages, json_mode=False):
            result = getattr(ev, "result", None)
            if result is not None:
                return getattr(result, "content", None)
    except Exception:
        _logger.exception("LLM chat_stream failed")
    return None
