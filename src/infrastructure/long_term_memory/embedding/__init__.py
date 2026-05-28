"""Embedding provider abstraction.

The DreamWorker writes new entries through ``provider.embed(...)``. When the
provider is ``available=False`` (the v1 default) every embedding call
short-circuits and recall falls back to FTS-only ranking. Hooking up a real
provider (P2: ONNX bge-small-zh) only requires writing a new subclass that
sets ``available=True`` and implements ``embed``; nothing else in the system
needs to change.
"""
from .base import EmbeddingProvider, cosine, from_config, vec_from_bytes, vec_to_bytes

__all__ = [
    "EmbeddingProvider",
    "cosine",
    "from_config",
    "vec_from_bytes",
    "vec_to_bytes",
]
