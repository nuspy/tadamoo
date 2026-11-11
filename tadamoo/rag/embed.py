"""The embedding side of the archives: an interface and the vector arithmetic. No model is bundled and none is named here.

An embedder turns texts into vectors; its `model` id is stored with an archive's vectors, and vectors of another model are never mixed with
them. With no embedder (the provider returns None, or raises) every archive is searched by text alone. A provider is a function the store
calls each time it needs an embedder, so one that appears later (a model installed, a service started) switches hybrid search on.

An embedder may also have `version` (what makes its vectors incompatible with another version of itself), `embed_query(text)` (a question
is embedded differently from a passage), `rerank(query, passages)` (a score for each passage, or None when it cannot be done now) and
`device` (where it runs now) and `batch` (how many passages to ask for at once); the search uses whatever it has."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Protocol, Sequence

import numpy as np

from tadamoo.rag.errors import RagError

BATCH = 32
Progress = Callable[[Dict[str, Any]], None]


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """A matrix with one row per passage."""


EmbedderProvider = Callable[[], Optional[Embedder]]


def no_embedder() -> Optional[Embedder]:
    return None


def compatible(meta: Dict[str, Any], embedder: Optional[Embedder]) -> bool:
    """Whether the vectors an archive holds (`embed_model`, `embed_version` of its meta) were made by `embedder`'s model and version."""
    return bool(meta.get("embed_model") and embedder is not None and embedder.model == meta["embed_model"]
                and meta.get("embed_version", "") == str(getattr(embedder, "version", "")))


def normalize(matrix: np.ndarray) -> np.ndarray:
    """Float32 rows of length 1 (an all-zero row stays zero), so a dot product is a cosine."""
    m = np.asarray(matrix, dtype=np.float32)
    if m.ndim != 2:
        raise ValueError("embeddings must be a matrix")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def embed_query(embedder: Embedder, text: str) -> np.ndarray:
    """The normalized vector of a question: by the embedder's own way of embedding a question when it has one."""
    own = getattr(embedder, "embed_query", None)
    got = own(text) if own is not None else embedder.embed([text])
    return normalize(np.asarray(got, dtype=np.float32).reshape(1, -1))[0]


def embed_all(embedder: Embedder, texts: Sequence[str], progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> np.ndarray:
    """Normalized vectors for all `texts`, asked for in batches; raises ValueError when the embedder answers with the wrong number of rows and RagError
    (`cancelled`) when `cancel` is set. `progress` gets {"event": "progress", "page": done, "pages": total, "device"} after each batch."""
    parts = []
    done = 0
    i = 0
    if progress is not None and texts:
        progress({"event": "progress", "page": 0, "pages": len(texts), "device": ""})                       # a new count: the passages
    while i < len(texts):
        if cancel is not None and cancel.is_set():
            raise RagError("the indexing was cancelled", "cancelled")
        step = max(1, int(getattr(embedder, "batch", BATCH)))                   # a slow embedder asks for small batches, so a cancel is seen soon
        batch = list(texts[i:i + step])
        i += len(batch)
        got = np.asarray(embedder.embed(batch), dtype=np.float32)
        if got.ndim != 2 or got.shape[0] != len(batch) or (parts and got.shape[1] != parts[0].shape[1]):
            raise ValueError("the embedder returned vectors that do not match the texts")
        parts.append(got)
        done += len(batch)
        if progress is not None:
            progress({"event": "progress", "page": done, "pages": len(texts), "device": str(getattr(embedder, "device", "") or "")})
    return normalize(np.vstack(parts)) if parts else np.zeros((0, 0), dtype=np.float32)
