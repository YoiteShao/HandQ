"""Semantic extractor — LLM-abstracts obs_sessions into obs_semantic_events.

Background worker (spawned by LongTermMemory.init alongside DreamWorker
and SessionAggregator). For each closed session with
``semantic_status='pending'`` it:

  1. Pulls the session + its snapshots + ocr_frames
  2. Builds a JSON-mode LLM prompt asking for title/description/category/
     entities/apps/task_worthy + worth_memory/worth_knowledge/worth_skill
  3. Writes obs_semantic_events with frame from the parent session
  4. Marks session ``semantic_status='done'``

Frame discipline
----------------
The LLM may REFINE confidence (e.g. notice OCR text contradicting the
process signal) but **MAY NOT WIDEN** the frame — the process signal is
the floor. If the LLM disagrees about the frame, confidence is reduced.

Fallback
--------
If no LLM is configured, a heuristic skeleton extractor produces a
minimal event row (title=primary_window_title, worth_* all false) so the
pipeline still progresses. Phase 7 cleanup may remove this fallback once
the bridge always ships an LLM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import List, Optional

from . import _constants as C
from .models import SemanticStatus
from .pii import PIIFilter

_logger = logging.getLogger("handq.ltm.semantic_extractor")

EXTRACTOR_TICK_SECONDS: float = 60.0
MIN_SNAPSHOTS_FOR_LLM: int = 3
MAX_SNAPSHOTS_PER_PROMPT: int = 20


class SemanticExtractor:
    """Async worker: obs_sessions(pending) → obs_semantic_events."""

    def __init__(
        self,
        store,
        llm_services: Optional[list] = None,
        pii_filter: Optional[PIIFilter] = None,
    ) -> None:
        self._store = store
        self._llm_services = llm_services or []
        # PII gate applied to the LLM/heuristic verdict BEFORE writing to
        # obs_semantic_events. Closes a defense-in-depth gap where a token
        # echoed in a terminal could survive in title/description for the
        # 30-day TTL even if the downstream promotion path drops it. None
        # is back-compat for tests that don't construct one.
        self._pii = pii_filter
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        _logger.info(
            "SemanticExtractor started (tick=%.0fs, llm_available=%s)",
            EXTRACTOR_TICK_SECONDS, bool(self._llm_services),
        )
        try:
            while not self._stopped.is_set():
                try:
                    n = await self._tick()
                    if n:
                        _logger.debug("SemanticExtractor processed %d sessions", n)
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
        """One pass over pending closed sessions."""
        sessions = await self._store.list_sessions_pending_extraction(limit=8)
        if not sessions:
            return 0

        processed = 0
        for s in sessions:
            (sid, trigger_kind, started_at, ended_at, frame_os, frame_host,
             primary_process, primary_window_title, snap_count, apps_json) = s
            try:
                event_id = await self._extract_one(
                    session_id=sid,
                    trigger_kind=trigger_kind,
                    started_at=started_at,
                    ended_at=ended_at or started_at,
                    frame_os=frame_os,
                    frame_host=frame_host,
                    primary_process=primary_process,
                    primary_window_title=primary_window_title,
                    apps_json=apps_json,
                )
                # _extract_one returns None only when the session has no
                # usable snapshots (all rejected by dedup). Mark those SKIPPED
                # rather than DONE so the audit trail distinguishes "nothing to
                # extract" from "extracted an event" — and no empty event row
                # is written.
                final_status = (
                    SemanticStatus.DONE.value if event_id
                    else SemanticStatus.SKIPPED.value
                )
                await self._store.set_obs_session_status(
                    sid, semantic_status=final_status,
                )
                if event_id:
                    processed += 1
            except Exception:
                _logger.exception("extract failed sid=%s", sid[:8])
                # Mark as skipped so it doesn't block other sessions.
                try:
                    await self._store.set_obs_session_status(
                        sid, semantic_status=SemanticStatus.SKIPPED.value,
                    )
                except Exception:
                    pass
        return processed

    async def _extract_one(
        self,
        *,
        session_id: str,
        trigger_kind: str,
        started_at: int,
        ended_at: int,
        frame_os: Optional[str],
        frame_host: Optional[str],
        primary_process: Optional[str],
        primary_window_title: Optional[str],
        apps_json: Optional[str],
    ) -> Optional[str]:
        # Pull snapshots + OCR text. Cap the prompt size.
        snapshot_rows = await self._store._fetchall(
            "SELECT id, captured_at, window_title, process_name, ax_text "
            "FROM obs_snapshots WHERE session_id=? ORDER BY captured_at ASC LIMIT ?",
            (session_id, MAX_SNAPSHOTS_PER_PROMPT),
        )
        if len(snapshot_rows) < 1:
            return None

        # Collect OCR text per snapshot.
        ocr_excerpts: List[str] = []
        for sr in snapshot_rows:
            snap_id = sr[0]
            ocr_rows = await self._store._fetchall(
                "SELECT text FROM obs_ocr_frames WHERE snapshot_id=? LIMIT 2",
                (snap_id,),
            )
            for or_ in ocr_rows:
                if or_[0]:
                    ocr_excerpts.append(or_[0][:400])

        # Try LLM extraction. Falls back to heuristic.
        verdict = None
        if self._llm_services and len(snapshot_rows) >= MIN_SNAPSHOTS_FOR_LLM:
            try:
                verdict = await self._llm_extract(
                    primary_process=primary_process,
                    primary_window_title=primary_window_title,
                    trigger_kind=trigger_kind,
                    frame_os=frame_os, frame_host=frame_host,
                    snapshots=snapshot_rows, ocr_excerpts=ocr_excerpts,
                )
            except Exception:
                _logger.exception("LLM extract failed; falling back to heuristic")

        if verdict is None:
            verdict = self._heuristic_extract(
                trigger_kind=trigger_kind,
                primary_process=primary_process,
                primary_window_title=primary_window_title,
                apps_json=apps_json,
                snapshot_count=len(snapshot_rows),
            )

        # PII gate. The verdict is built from raw OCR excerpts + window
        # titles, which is the highest on-screen-secret risk surface
        # (terminal token echoes, tokenized URLs in title bars, env-var
        # assignments). Even though DreamWorker._promote_one_event has
        # its own PII gate, this row is persisted under the 30-day
        # obs_semantic_events TTL whether or not it's ever promoted —
        # so the at-rest exposure window has to close here.
        if self._pii is not None:
            blob = (verdict.get("title") or "") + "\n" + (verdict.get("description") or "")
            if self._pii.has_secret(blob):
                _logger.info(
                    "session %s skipped: PII detected in extracted title/description",
                    session_id[:8],
                )
                return None

        # Insert obs_semantic_events row. The partial UNIQUE on
        # ``(session_id) WHERE synthetic_origin IS NULL`` (migration v3)
        # makes a duplicate insert raise IntegrityError, which we treat
        # as "already extracted" — the previous run wrote the row but
        # crashed before set_obs_session_status, so this re-tick finds
        # the session still pending. Returning None routes through the
        # SKIPPED-status path in _tick, which is safe: the duplicate
        # row is now blocked at the SQL layer and the session moves
        # forward.
        try:
            event_id = await self._store.insert_obs_semantic_event(
                session_id=session_id,
                synthetic_origin=None,
                title=verdict.get("title", "(no title)"),
                description=verdict.get("description", ""),
                category=verdict.get("category"),
                entities=verdict.get("entities") or [],
                apps=verdict.get("apps") or [],
                time_range_start=started_at,
                time_range_end=ended_at,
                task_worthy=bool(verdict.get("task_worthy", False)),
                worth_memory=bool(verdict.get("worth_memory", False)),
                worth_knowledge=bool(verdict.get("worth_knowledge", False)),
                worth_skill=bool(verdict.get("worth_skill", False)),
                frame_os=frame_os,
                frame_host=frame_host,
                frame_confidence=float(verdict.get("frame_confidence", 0.5)),
            )
        except sqlite3.IntegrityError:
            _logger.info(
                "session %s already has a semantic event; treating as done",
                session_id[:8],
            )
            return None
        # G2: fill content_type on all snapshots of this session so the
        # observation table carries the LLM-inferred classification.
        cat = verdict.get("category")
        if cat and session_id:
            try:
                await self._store._execute(
                    "UPDATE obs_snapshots SET content_type=? WHERE session_id=?",
                    (cat, session_id),
                )
            except Exception:
                _logger.debug("content_type backfill failed", exc_info=True)
        return event_id

    async def _llm_extract(
        self,
        *,
        primary_process: Optional[str],
        primary_window_title: Optional[str],
        trigger_kind: str,
        frame_os: Optional[str], frame_host: Optional[str],
        snapshots: List, ocr_excerpts: List[str],
    ) -> Optional[dict]:
        """Call AnthropicStreamingService.chat_stream and parse JSON.

        Uses the StreamDoneEvent.result.content surface that
        :mod:`anthropic_streaming_service` exposes. Falls back to None on
        parse failure so the caller can take the heuristic path.
        """
        if not self._llm_services:
            return None

        ocr_blob = "\n---\n".join(ocr_excerpts[:8])
        prompt = (
            "You are extracting a structured semantic event from a continuous user "
            "work session. Output STRICT JSON, no prose.\n\n"
            f"Session metadata:\n"
            f"  trigger: {trigger_kind}\n"
            f"  primary_process: {primary_process}\n"
            f"  primary_window: {primary_window_title}\n"
            f"  frame: os={frame_os}, host={frame_host}\n"
            f"  snapshot_count: {len(snapshots)}\n\n"
            "OCR excerpts from the session:\n"
            f"{ocr_blob[:4000]}\n\n"
            "Produce JSON with these fields:\n"
            "  title: one-line summary, present tense\n"
            "  description: 1-3 sentence narrative\n"
            "  category: ssh_session|editing|browsing|meeting|debugging|other\n"
            "  entities: list of hostnames/project names mentioned\n"
            "  apps: list of process names involved\n"
            "  task_worthy: bool — is this work the user might want remembered\n"
            "  worth_memory: bool — produces durable user preference / context\n"
            "  worth_knowledge: bool — produces reusable team/project fact\n"
            "  worth_skill: bool — could become a reusable automation/script\n"
            "  frame_confidence: float 0.0-1.0\n\n"
            "Frame rule: if OCR contradicts the declared frame, LOWER confidence; "
            "do NOT switch the os/host (the process signal is authoritative).\n"
        )

        svc = self._llm_services[0]
        text = await _llm_chat_text(svc, [{"role": "user", "content": prompt}])
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            _logger.debug("LLM output not valid JSON: %s", text[:200])
        return None

    @staticmethod
    def _heuristic_extract(
        *,
        trigger_kind: str,
        primary_process: Optional[str],
        primary_window_title: Optional[str],
        apps_json: Optional[str],
        snapshot_count: int,
    ) -> dict:
        """Skeleton extractor when no LLM is configured.

        Produces a minimal event so the pipeline still progresses. The
        triage gate will see worth_* = False so nothing actually lands in
        mem_entries — which is the right behavior (no LLM means no
        confident worth judgment).
        """
        try:
            apps = json.loads(apps_json) if apps_json else []
        except (json.JSONDecodeError, TypeError, ValueError):
            apps = []
        title = primary_window_title or primary_process or "(unknown session)"
        return {
            "title": title[:120],
            "description": (
                f"Session of {snapshot_count} snapshots; trigger={trigger_kind}; "
                f"primary process={primary_process or 'unknown'}."
            ),
            "category": _heuristic_category(trigger_kind, primary_process),
            "entities": [],
            "apps": apps,
            "task_worthy": False,
            "worth_memory": False,
            "worth_knowledge": False,
            "worth_skill": False,
            "frame_confidence": 0.5,
        }


def _heuristic_category(trigger_kind: str, process: Optional[str]) -> str:
    if trigger_kind == "ssh_start":
        return "ssh_session"
    if trigger_kind == "rdp_start":
        return "remote_desktop"
    proc = (process or "").lower()
    if proc in ("chrome.exe", "msedge.exe", "firefox.exe"):
        return "browsing"
    if proc in ("code.exe", "cursor.exe", "pycharm64.exe", "idea64.exe", "studio64.exe"):
        return "editing"
    return "other"


async def _llm_chat_text(svc, messages: list) -> Optional[str]:
    """Drive AnthropicStreamingService.chat_stream and return the result.content.

    The streaming API yields events; the final ``StreamDoneEvent`` carries
    a ``LLMChatResult.content`` that we want. None on any error.
    """
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
