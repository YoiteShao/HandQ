"""Compatibility stub — re-exports from routing/router.py."""
from .routing.router import *  # noqa: F401,F403
from .routing.router import (
    Router, RouteDecision, Embedder, LongTermMemoryEmbedder,
    Classifier, METHOD_EMBEDDING, METHOD_CLASSIFIER, METHOD_FAILSAFE,
    _cosine,
)
