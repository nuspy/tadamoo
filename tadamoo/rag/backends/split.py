"""Plan B for a PostgreSQL that has no vector extension: the metadata, passages and words stay in PostgreSQL, the vectors (and their search) go to a local SQLite file
with sqlite-vec. The two are written one after the other, not in one transaction: a vector file that is behind is noticed by its row count and indexing again repairs it."""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import numpy as np

from tadamoo.rag.backends.base import MAIN, Backend


class SplitBackend(Backend):
    name = "split"

    def __init__(self, main: Backend, vectors: Backend) -> None:
        self.main, self.vec = main, vectors

    def __getattr__(self, item: str) -> Any:
        return getattr(self.main, item)

    @property
    def native_vectors(self) -> bool:                                  # type: ignore[override]
        return self.vec.native_vectors

    @property
    def native_words(self) -> bool:                                    # type: ignore[override]
        return self.main.native_words

    def prepare(self) -> Dict[str, Any]:
        info = self.main.prepare()
        self.vec.prepare()
        return {**info, "backend": "split", "vectors_in": self.vec.info()["label"], "vectors": "native" if self.vec.native_vectors else "app"}

    def info(self) -> Dict[str, Any]:
        return {**self.main.info(), "backend": "split", "vectors_in": self.vec.info()["label"], "vectors": "native" if self.vec.native_vectors else "app"}

    def close(self) -> None:
        self.main.close()
        self.vec.close()

    def commit(self, aid: str, meta: Dict[str, Any], chunks: Sequence[Dict[str, Any]], sections: Optional[Dict[str, Dict[str, Any]]],
               vectors: Optional[np.ndarray], model: str = "", version: str = "") -> None:
        self.main.commit(aid, meta, chunks, sections, None)
        if vectors is not None:
            self.vec.put_vectors(aid, MAIN, vectors, model, version)
        else:
            self.vec.drop_vectors(aid, MAIN)

    def delete_archive(self, aid: str) -> None:
        self.main.delete_archive(aid)
        for slot in ("main", "rt"):
            self.vec.drop_vectors(aid, slot)

    def counts(self, aid: str) -> Dict[str, int]:
        note = self.vec.vector_note(aid, MAIN)
        return {**self.main.counts(aid), "vectors": int(note["rows"]) if note else 0}

    def read_vectors(self, aid: str, slot: str = MAIN):
        return self.vec.read_vectors(aid, slot)

    def vector_note(self, aid: str, slot: str = MAIN):
        return self.vec.vector_note(aid, slot)

    def put_vectors(self, aid: str, slot: str, vectors: np.ndarray, model: str, version: str, note: Optional[Dict[str, Any]] = None) -> None:
        self.vec.put_vectors(aid, slot, vectors, model, version, note)

    def drop_vectors(self, aid: str, slot: str) -> None:
        self.vec.drop_vectors(aid, slot)

    def vector_search(self, aid: str, query: np.ndarray, k: int, slot: str = MAIN):
        return self.vec.vector_search(aid, query, k, slot)
