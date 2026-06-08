"""Routing sub-package — goal classification and pattern matching."""
from .router import Router, RouteDecision, Embedder, LongTermMemoryEmbedder, METHOD_EMBEDDING, METHOD_CLASSIFIER, METHOD_FAILSAFE
from .patterns import FREEFORM, Pattern, PATTERNS
from .detector import TemplateDetector, Trace, Candidate
from .exemplars import ExemplarStore
from .exemplar_builder import ExemplarBuilder

__all__ = [
    "Router", "RouteDecision", "Embedder", "LongTermMemoryEmbedder",
    "METHOD_EMBEDDING", "METHOD_CLASSIFIER", "METHOD_FAILSAFE",
    "FREEFORM", "Pattern", "PATTERNS",
    "TemplateDetector", "Trace", "Candidate",
    "ExemplarStore", "ExemplarBuilder",
]
