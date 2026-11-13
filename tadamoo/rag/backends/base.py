"""The interface every storage of the knowledge archives has. The store (tadamoo/rag/store.py) and everything else that reads or writes an archive goes through it;
nothing outside `tadamoo/rag/backends/` opens a database or a file of an archive.

An archive is: a `meta` (a dict with its `documents`), passages (dicts, in order, each with its tags in `facts`), sections, vectors (one row per passage, of one model),
and what hangs on it: the blocks each document was read into, the figures with their images, what a model wrote about the passages (the enrichment cache), the
state of its sources, and a second set of vectors for the real-time functions (`slot`). The position of a passage in the list is its identity in a search answer."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag.errors import RagError

MAIN, RT = "main", "rt"                     # the slots of vectors an archive may hold


class Backend:
    """What a storage does. `native_vectors` and `native_words` say whether `vector_search` and `word_search` are answered by the database itself; when they are not, the
    caller searches in memory with `read_vectors` and the passages."""

    name = ""
    native_vectors = False
    native_words = False

    # -- the database --------------------------------------------------------------------------------------

    def prepare(self) -> Dict[str, Any]:
        """Creates the tables (or brings them up to the schema in force) and returns `info()`."""
        raise NotImplementedError

    def info(self) -> Dict[str, Any]:
        """`{backend, dialect, schema, vectors, words, ...}`: what this storage is and how it searches."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- archives ------------------------------------------------------------------------------------------

    def archive_ids(self, project: Optional[str] = None) -> List[str]:
        raise NotImplementedError

    def revisions(self) -> Dict[str, int]:
        """Archive id -> a number that changes whenever the archive does (what lets a reader keep what it loaded until then)."""
        raise NotImplementedError

    def revision(self, aid: str) -> Optional[int]:
        """The revision of one archive (None: there is none)."""
        raise NotImplementedError

    def read_meta(self, aid: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def read_metas(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def write_meta(self, aid: str, meta: Dict[str, Any]) -> None:
        """Writes the meta (and its documents) alone: the passages and vectors stay as they are."""
        raise NotImplementedError

    def commit(self, aid: str, meta: Dict[str, Any], chunks: Sequence[Dict[str, Any]], sections: Optional[Dict[str, Dict[str, Any]]],
               vectors: Optional[np.ndarray], model: str = "", version: str = "") -> None:
        """Replaces the meta, the passages, the sections (None: they stay) and the vectors of an archive in one transaction."""
        raise NotImplementedError

    def delete_archive(self, aid: str) -> None:
        raise NotImplementedError

    def read_chunks(self, aid: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def read_sections(self, aid: str) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def counts(self, aid: str) -> Dict[str, int]:
        """`{chunks, documents, sections, vectors}` as stored: what a check compares with the meta."""
        raise NotImplementedError

    # -- vectors -------------------------------------------------------------------------------------------

    def read_vectors(self, aid: str, slot: str = MAIN) -> Optional[np.ndarray]:
        """The vectors of a slot, one float32 row per passage (not normalized again), or None."""
        raise NotImplementedError

    def vector_note(self, aid: str, slot: str = MAIN) -> Optional[Dict[str, Any]]:
        """`{model, version, dims, rows, note, gen}` of a slot, or None."""
        raise NotImplementedError

    def put_vectors(self, aid: str, slot: str, vectors: np.ndarray, model: str, version: str, note: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError

    def drop_vectors(self, aid: str, slot: str) -> None:
        raise NotImplementedError

    def vector_search(self, aid: str, query: np.ndarray, k: int, slot: str = MAIN) -> List[Tuple[int, float]]:
        """The `k` passages whose vectors are closest to `query` (both normalized): `(position, cosine)`, best first."""
        raise NotImplementedError

    def word_search(self, aid: str, query: str, k: int) -> Optional[List[Tuple[int, float]]]:
        """`(position, score)` of the passages that hold a word of `query`, best first, from the database's own text search; None when it has none."""
        return None

    # -- what hangs on an archive --------------------------------------------------------------------------

    def put_blob(self, aid: str, kind: str, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get_blob(self, aid: str, kind: str, key: str) -> Optional[bytes]:
        raise NotImplementedError

    def list_blobs(self, aid: str, kind: str) -> List[str]:
        raise NotImplementedError

    def delete_blobs(self, aid: str, kind: str, key: Optional[str] = None, prefix: Optional[str] = None) -> None:
        raise NotImplementedError

    def read_figures(self, aid: str, doc: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def write_figures(self, aid: str, doc: str, records: Sequence[Dict[str, Any]]) -> None:
        """Replaces the record of a document's figures (no records: it has none)."""
        raise NotImplementedError

    def read_sources(self, aid: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def write_sources(self, aid: str, data: Dict[str, Any]) -> None:
        raise NotImplementedError

    def enrich_read(self, aid: str) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def enrich_add(self, aid: str, rows: Iterable[Dict[str, Any]]) -> None:
        raise NotImplementedError

    def enrich_clear(self, aid: str) -> None:
        raise NotImplementedError


class BackendError(RagError):
    """The database could not be reached or answered with an error: the message says which."""
