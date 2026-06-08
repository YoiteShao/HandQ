"""Router — maps a goal onto an execution pattern, or fails safe to single-loop.

Three tiers, cheapest first:

    embedding-match (zero LLM)  ──high-confidence──►  pattern
            │ low-confidence
            ▼
    cheap LLM classifier  ──►  {pattern_id | "freeform"}
            │ "freeform" / still low-confidence
            ▼
    FAIL-SAFE ─► single AgentNode (always correct)

The robustness guarantee: a wrong decision degrades to "single loop", never to
"broken". Both the embedder and the classifier are injected so the router is
fully testable without a model (pass ``None`` to skip a tier).

**Exemplar source**: the embedding tier scores the goal against pattern
exemplars. By default the exemplar pool is the builtin patterns (see
:mod:`patterns`); pass an :class:`ExemplarStore` to add user-defined patterns
and auto-promoted exemplars on top. The Router caches embeddings keyed by
the store's generation counter, so the cache invalidates exactly once per
real change (a tight feedback loop with no per-call re-embedding cost).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Optional, Protocol

from . import patterns
from .exemplars import ExemplarStore
from .patterns import FREEFORM

# How a decision was reached — useful for telemetry and the detector's traces.
METHOD_EMBEDDING = "embedding"
METHOD_CLASSIFIER = "classifier"
METHOD_FAILSAFE = "failsafe"


@dataclass(frozen=True)
class RouteDecision:
    """Result of routing one goal.

    ``pattern_id == FREEFORM`` means "run a bare single AgentNode" — the universal
    fallback. ``confidence`` is the score that justified the tier that decided.
    """

    pattern_id: str
    confidence: float
    method: str

    @property
    def is_freeform(self) -> bool:
        return self.pattern_id == FREEFORM


class Embedder(Protocol):
    """Embeds text to a vector. Reuses the long-term-memory dense encoder.

    ``embed`` may be sync (returns the vector) or async (returns an awaitable);
    the router awaits whichever it gets, so a fast deterministic test embedder
    and the real async LTM provider both fit."""

    def embed(self, text: str): ...


class LongTermMemoryEmbedder:
    """Adapts a long-term-memory ``EmbeddingProvider`` to the router's Embedder.

    The router scores a goal against pattern exemplars; in asymmetric-retrieval
    terms both sides are *queries*, so we route through ``embed_query`` (async).
    The provider is injected — this adapter pulls in no ``src`` import itself, so
    the orchestration layer stays importable without the embedding stack. When
    the provider is unavailable (FTS-only deployment), construction is rejected
    so the router cleanly falls through to the classifier / fail-safe tiers."""

    def __init__(self, provider) -> None:
        if not getattr(provider, "available", False):
            raise ValueError("embedding provider is not available (FTS-only?)")
        self._provider = provider

    async def embed(self, text: str) -> list[float]:
        return await self._provider.embed_query(text)


# A cheap classifier: goal -> pattern_id (or FREEFORM). Async to allow one LLM
# call. Returns None when it cannot decide, so the router falls through.
Classifier = Callable[[str], Awaitable[Optional[str]]]


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


# Sentinel telling _ensure_exemplars: "we built the cache from the legacy
# builtin-only path; rebuild if a store ever shows up later." Real generation
# counters are non-negative integers from ExemplarStore.
_BUILTIN_GEN = -1
_NO_CACHE = -2


class Router:
    def __init__(
        self,
        *,
        embedder: Optional[Embedder] = None,
        classifier: Optional[Classifier] = None,
        embed_threshold: float = 0.75,
        exemplar_store: Optional[ExemplarStore] = None,
    ) -> None:
        self._embedder = embedder
        self._classifier = classifier
        self._embed_threshold = embed_threshold
        # Optional overlay; None ⇒ score against builtin patterns only.
        self._exemplar_store = exemplar_store
        # Cache: list[(pattern_id, vec)] paired with the generation it was
        # built at. Comparing _exemplar_gen to the store's current generation
        # tells us in O(1) whether the cache is still good.
        self._exemplar_vecs: Optional[list[tuple[str, list[float]]]] = None
        self._exemplar_gen: int = _NO_CACHE

    async def _embed(self, text: str) -> list[float]:
        """Call the injected embedder, awaiting it if it is async."""
        vec = self._embedder.embed(text)
        if inspect.isawaitable(vec):
            vec = await vec
        return vec

    def _current_gen(self) -> int:
        """Generation we should rebuild the cache against."""
        return self._exemplar_store.generation if self._exemplar_store else _BUILTIN_GEN

    def _exemplar_iter(self) -> Iterable[tuple[str, str]]:
        """Source of (pattern_id, text) tuples — store overlay or builtin."""
        if self._exemplar_store is not None:
            return self._exemplar_store.all_exemplars()
        return patterns.all_exemplars()

    async def _ensure_exemplars(self) -> None:
        if self._embedder is None:
            return
        target_gen = self._current_gen()
        if self._exemplar_vecs is not None and self._exemplar_gen == target_gen:
            return  # cache fresh, nothing to do
        # Cold cache or store mutated since last build — re-embed.
        self._exemplar_vecs = [
            (sid, await self._embed(text))
            for sid, text in self._exemplar_iter()
        ]
        self._exemplar_gen = target_gen

    async def _embedding_match(self, goal: str) -> Optional[RouteDecision]:
        if self._embedder is None:
            return None
        await self._ensure_exemplars()
        if not self._exemplar_vecs:
            return None
        gvec = await self._embed(goal)
        best_id, best_score = FREEFORM, 0.0
        for sid, vec in self._exemplar_vecs:
            score = _cosine(gvec, vec)
            if score > best_score:
                best_id, best_score = sid, score
        if best_score >= self._embed_threshold:
            return RouteDecision(best_id, best_score, METHOD_EMBEDDING)
        return None

    async def classify(self, goal: str) -> RouteDecision:
        # Tier 1 — zero-LLM embedding match against pattern exemplars.
        decision = await self._embedding_match(goal)
        if decision is not None:
            return decision

        # Tier 2 — one cheap LLM classification call.
        if self._classifier is not None:
            pattern_id = await self._classifier(goal)
            # Accept either a builtin pattern_id or one in the user overlay.
            if pattern_id and pattern_id != FREEFORM and self._knows_pattern(pattern_id):
                return RouteDecision(pattern_id, 1.0, METHOD_CLASSIFIER)

        # Tier 3 — fail-safe. Always correct, possibly slower.
        return RouteDecision(FREEFORM, 0.0, METHOD_FAILSAFE)

    def _knows_pattern(self, pattern_id: str) -> bool:
        """Whether ``pattern_id`` is recognised (builtin OR user overlay)."""
        if self._exemplar_store is not None and pattern_id in self._exemplar_store.all_pattern_ids():
            return True
        return patterns.get(pattern_id) is not None
