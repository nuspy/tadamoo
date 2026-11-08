"""The embedding side of the archives: an interface and the vector arithmetic. No model is bundled and none is named here.

An embedder turns texts into vectors; its `model` id is stored with an archive's vectors, and vectors of another model are never mixed with
them. With no embedder (the provider returns None, or raises) every archive is searched by text alone. A provider is a function the store
calls each time it needs an embedder, so one that appears later (a model installed, a service started) switches hybrid search on."""
from __future__ import annotations

from typing import Callable, Optional, Protocol, Sequence

import numpy as np

BATCH = 32


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """A matrix with one row per text."""


EmbedderProvider = Callable[[], Optional[Embedder]]


def no_embedder() -> Optional[Embedder]:
    return None


def normalize(matrix: np.ndarray) -> np.ndarray:
    """Float32 rows of length 1 (an all-zero row stays zero), so a dot product is a cosine."""
    m = np.asarray(matrix, dtype=np.float32)
    if m.ndim != 2:
        raise ValueError("embeddings must be a matrix")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def embed_all(embedder: Embedder, texts: Sequence[str]) -> np.ndarray:
    """Normalized vectors for all `texts`, asked for in batches; raises ValueError when the embedder answers with the wrong number of rows."""
    parts = []
    for i in range(0, len(texts), BATCH):
        batch = list(texts[i:i + BATCH])
        got = np.asarray(embedder.embed(batch), dtype=np.float32)
        if got.ndim != 2 or got.shape[0] != len(batch) or (parts and got.shape[1] != parts[0].shape[1]):
            raise ValueError("the embedder returned vectors that do not match the texts")
        parts.append(got)
    return normalize(np.vstack(parts)) if parts else np.zeros((0, 0), dtype=np.float32)
