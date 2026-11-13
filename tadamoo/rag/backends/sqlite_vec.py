"""The default storage: one SQLite file for a project. Words are searched with the database's own FTS5, vectors with the `sqlite-vec` extension (float32, cosine) when it
can be loaded. Without the extension the vectors are searched in the application as the fallback does; the vectors are always kept in the binary column too, so the
file can be moved to a machine that has the extension or has not, and the index of the extension is built again when it is missing."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag import search as ranking
from tadamoo.rag.backends.base import MAIN
from tadamoo.rag.backends.sql import SQLITE, SqlBackend, table_token
from tadamoo.rag.passage import searchable


def load_vec(conn: sqlite3.Connection) -> bool:
    """Loads sqlite-vec into the connection; False when the package is not installed or the extension cannot be loaded."""
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except Exception:
        return False


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.Error:
        return False


class SqliteVecBackend(SqlBackend):
    name = "sqlite_vec"

    def __init__(self, path: str | Path, prefix: str = "rag_", extension: bool = True) -> None:
        self.path = Path(path)
        self.vec, self.fts, self.want_extension = False, False, extension
        super().__init__(self._connect_file, SQLITE, prefix, label=str(self.path))
        self._open()

    def _connect_file(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self.vec = load_vec(conn) if self.want_extension else False
        self.fts = has_fts5(conn)
        return conn

    @property
    def native_vectors(self) -> bool:                                  # type: ignore[override]
        return self.vec

    @property
    def native_words(self) -> bool:                                    # type: ignore[override]
        return self.fts

    def info(self):
        out = super().info()
        out["sqlite"] = sqlite3.sqlite_version
        out["extension"] = "sqlite-vec" if self.vec else ("not loaded" if self.want_extension else "off")
        return out

    # -- words: FTS5 over the passage as it is searched, its words made plain the way the application's own index makes them -------------------------

    @property
    def _fts(self) -> str:
        return self.t("fts")

    def _after_prepare(self) -> None:
        if not self.fts:
            return
        self.run(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._fts} USING fts5(search, archive_id UNINDEXED, pos UNINDEXED, tokenize='unicode61')").close()
        stored = int(self.one(f"SELECT COUNT(*) FROM {self.t('chunks')}")[0])
        if stored != int(self.one(f"SELECT COUNT(*) FROM {self._fts}")[0]):         # written while FTS5 was not there: made again
            with self.tx():
                self.run(f"DELETE FROM {self._fts}").close()
                for aid in self.archive_ids():
                    self._after_chunks(aid, self.read_chunks(aid))

    def _before_chunks(self, aid: str) -> None:
        if self.fts and self.one("SELECT 1 FROM sqlite_master WHERE name = ?", (self._fts,)):
            self.run(f"DELETE FROM {self._fts} WHERE archive_id = ?", (aid,)).close()

    def _after_chunks(self, aid: str, chunks: Sequence[Any]) -> None:
        if self.fts:
            self.many(f"INSERT INTO {self._fts} (search, archive_id, pos) VALUES (?, ?, ?)",
                      [(" ".join(ranking.tokens(searchable(c))), aid, i) for i, c in enumerate(chunks)])

    def word_search(self, aid: str, query: str, k: int) -> Optional[List[Tuple[int, float]]]:
        if not self.fts:
            return None
        words = list(dict.fromkeys(ranking.tokens(query)))
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        with self.lock:
            rows = self.all(f"SELECT pos, bm25({self._fts}) FROM {self._fts} WHERE {self._fts} MATCH ? AND archive_id = ? ORDER BY bm25({self._fts}) LIMIT ?", (match, aid, int(k)))
        return [(int(pos), -float(score)) for pos, score in rows]

    # -- vectors: a vec0 table for each archive and slot, built from the binary column ------------------------------------------------------------

    def _index_vectors(self, aid: str, slot: str, data: np.ndarray) -> str:
        if not self.vec:
            return ""
        table = self.t("v_") + table_token(aid, slot)
        self.run(f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[{int(data.shape[1])}] distance_metric=cosine)").close()
        self.many(f"INSERT INTO {table} (rowid, embedding) VALUES (?, ?)", [(i, data[i].tobytes()) for i in range(data.shape[0])])
        return table

    def _drop_index(self, table: str) -> None:
        try:
            self.run(f"DROP TABLE IF EXISTS {table}").close()
        except sqlite3.Error:
            pass                                                       # the file was made where the extension was and is read where it is not

    def vector_search(self, aid: str, query: np.ndarray, k: int, slot: str = MAIN) -> List[Tuple[int, float]]:
        if not self.vec:
            return super().vector_search(aid, query, k, slot)
        with self.lock:
            note = self.vector_note(aid, slot)
            if note is None or note["dims"] != query.shape[0]:
                return []
            row = self.one(f"SELECT tbl FROM {self.t('vectors')} WHERE archive_id = ? AND slot = ?", (aid, slot))
            table = row[0] if row else ""
            if not table:                                              # the file has vectors and no index yet: made now, once
                matrix = self.read_vectors(aid, slot)
                with self.tx():
                    table = self._index_vectors(aid, slot, matrix)
                    self.run(f"UPDATE {self.t('vectors')} SET tbl = ? WHERE archive_id = ? AND slot = ?", (table, aid, slot)).close()
            rows = self.all(f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ? ORDER BY distance", (np.asarray(query, dtype=np.float32).tobytes(), max(1, int(k))))
        return [(int(i), 1.0 - float(d)) for i, d in rows]
