"""Auto-promote successful goals into pattern exemplars.

When a goal was Tier-2 (cheap classifier) routed, ran successfully, and
ended without partial/paused outcomes, that goal is **evidence** that it
belongs to the pattern the classifier picked. Adding its text as an exemplar
into the :class:`ExemplarStore` lets next time's near-identical phrasing
hit Tier-1 (zero-LLM embedding match) directly.

This is the proactivity loop applied to *routing* (mirroring what the
detector does for *workflow pattern promotion*). A deployment that keeps
running over time gets cheaper and faster Router decisions for free.

Three guard rails make this safe:

  * **Cosine dedup** — a candidate too similar to an existing exemplar
    (≥ ``dedup_threshold``) carries no new information; reject it.
  * **Per-pattern cap with LRU eviction** — auto bucket bounded; oldest
    entries get evicted when full. User-written exemplars never evict.
  * **Embed cache** — every pattern exemplar is embedded at most once
    per Builder instance, so growing the auto pool stays cheap.

If the embedder is unavailable or fails, ``consider`` returns False
without raising — the auto path is best-effort, the run is unaffected.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Optional, Union

from .exemplars import ExemplarStore
from .router import Embedder, _cosine


async def _maybe_await(value: Union[Any, Awaitable[Any]]) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ExemplarBuilder:
    """Coordinator-side hook that turns successful runs into Tier-1 exemplars.

    Construct once with the same embedder the Router uses (typically the
    LTM dense provider). Pass the same ``ExemplarStore`` the Router was
    built with so additions show up on the very next Router cache rebuild.
    """

    def __init__(
        self,
        store: ExemplarStore,
        embedder: Embedder,
        *,
        dedup_threshold: float = 0.95,
        max_auto_per_pattern: int = 50,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._dedup = dedup_threshold
        self._cap = max_auto_per_pattern
        # Cache: text -> embedding vector. Keeps embed-of-existing-exemplar
        # off the hot path. Bounded loosely by the per-pattern cap × #patterns,
        # so it can't grow unboundedly under normal use.
        self._vec_cache: dict[str, list[float]] = {}

    async def consider(
        self,
        pattern_id: str,
        goal: str,
        *,
        source: str = "",
    ) -> bool:
        """Maybe add ``goal`` as an auto-exemplar for ``pattern_id``.

        Returns True if added; False if rejected (unknown pattern, embedder
        failed, or cosine-deduped against an existing exemplar). Failures
        are silent — auto-promotion is a quality-of-life feature; not
        adding doesn't hurt the actual run that just completed.
        """
        if pattern_id not in self._store.all_pattern_ids():
            return False
        text = goal.strip()
        if not text:
            return False

        try:
            gvec = await self._embed(text)
        except Exception:
            return False  # embedder transient failure → skip

        # Compare against every existing exemplar for this pattern.
        for sid, ex_text in self._store.all_exemplars():
            if sid != pattern_id:
                continue
            try:
                ex_vec = await self._embed(ex_text)
            except Exception:
                continue
            if _cosine(gvec, ex_vec) >= self._dedup:
                return False  # too similar — no new information

        # Add + maybe evict + persist.
        self._store.add_auto_exemplar(pattern_id, text, source=source)
        if self._store.auto_count(pattern_id) > self._cap:
            self._store.evict_oldest_auto(pattern_id, keep=self._cap)
        self._store.save()
        return True

    # ── internal ─────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        """Embed with caching by exact text. Sync/async embedder both work."""
        if text in self._vec_cache:
            return self._vec_cache[text]
        vec = await _maybe_await(self._embedder.embed(text))
        self._vec_cache[text] = vec
        return vec
