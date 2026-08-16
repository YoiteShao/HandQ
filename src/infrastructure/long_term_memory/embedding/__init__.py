"""Embedding provider abstraction.

The DreamWorker writes new entries through ``provider.embed(...)``. When the
provider is ``available=False`` every embedding call short-circuits and
recall falls back to FTS-only ranking. The live provider is
:class:`~.onnx_local.OnnxEmbedder` (local bge-small-zh-v1.5 via fastembed);
:class:`~.http_api.HttpApiEmbedder` (remote QGenie gateway) remains
available as an alternate ``EMBEDDING_PROVIDER`` choice in ``_constants.py``.
"""
from .base import EmbeddingProvider, cosine, from_config, vec_from_bytes, vec_to_bytes
from .onnx_local import OnnxEmbedder

__all__ = [
    "EmbeddingProvider",
    "OnnxEmbedder",
    "cosine",
    "from_config",
    "vec_from_bytes",
    "vec_to_bytes",
]
