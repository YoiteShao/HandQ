"""Personality — adaptive per-monitor activity capture for HandQ.

Architecture
------------
``PersonalityMonitor`` is an asyncio service spawned by ``bridge_main.py``
alongside :class:`LongTermMemory`. One instance per bridge process.

The "personality" name reflects the user-facing intent: HandQ adapts to
the user's environment, tools, and project conventions by lightly
observing what they have on screen. The mechanism (per-monitor screen
sampling) is named the way it's spoken about in the product, not by the
technical noun.

For each physical display, the service runs a small state machine that
chooses how often to sample the screen based on **input recency on that
display** rather than wall-clock cadence (per the design constraint —
idle monitors must not be polled at the same rate as an active one).

Pipeline per capture::

    1. Skip if foreground window matches a sensitive pattern.
    2. mss screenshot (one process-wide ``mss`` instance per loop).
    3. Perceptual hash; if too close to previous → unlink and skip OCR.
    4. RapidOCR (shared :class:`LocalOCR` singleton from vision package).
    5. Unlink the PNG file (privacy + disk hygiene).
    6. If the OCR text is too short / too similar to a recently accepted
       sample → drop.
    7. Otherwise write the observation to ``obs_snapshots`` +
       ``obs_ocr_frames`` — the single sink for activity capture.
       Downstream the SemanticExtractor abstracts these into
       ``obs_semantic_events`` and the DreamWorker triages those into
       ``mem_entries``.

Shutdown
--------
``PersonalityMonitor.shutdown()`` cancels the background task. Any
unprocessed ring frames are spilled to disk best-effort so the next
launch's OCR-drain worker can pick them up.
"""
from __future__ import annotations

from .service import PersonalityMonitor

__all__ = ["PersonalityMonitor"]
