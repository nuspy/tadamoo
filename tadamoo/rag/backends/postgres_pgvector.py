"""The second storage: PostgreSQL, with the `pgvector` extension for vectors (the `halfvec` type, cosine, an HNSW index) and the database's own text search
(a `tsvector` column with a GIN index) for words.

A vector column is `halfvec(dims)`, and the same type is in the index and in the query, so the index is used; HNSW indexes halfvec up to 4000 dimensions (the 4B embedding
model has 2560), above that the vectors are searched without an index. When the extension is not in the database (or `vectors` says `blob`) this is the fallback:
the vectors are a binary column and the application does the search. The tables carry a prefix and live in the database or schema the connection names, next to
whatever else is there."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag import search as ranking
from tadamoo.rag.backends.base import MAIN, BackendError
from tadamoo.rag.backends.sql import POSTGRES, SqlBackend, table_token
from tadamoo.rag.passage import searchable

HNSW_MAX = 4000                    # dimensions of a halfvec that an HNSW index takes
MIN_PGVECTOR = (0, 7)              # the first version with halfvec


def driver_name() -> str:
    """`psycopg`, `psycopg2` or "" (none installed)."""
    for name in ("psycopg", "psycopg2"):
        try:
            __import__(name)
            return name
        except ImportError:
            continue
    return ""


def connect_postgres(params: Dict[str, Any], password: str = "") -> Any:
    """A connection in autocommit mode from `params` (`dsn`, or host, port, dbname, user, sslmode); the password is given apart and never kept."""
    kwargs = {k: v for k, v in params.items() if k in ("host", "port", "dbname", "user", "sslmode") and v not in ("", None)}
    kwargs["connect_timeout"] = int(params.get("connect_timeout") or 10)
    if password:
        kwargs["password"] = password
    name = driver_name()
    if name == "psycopg":
        import psycopg
        return psycopg.connect(params["dsn"], autocommit=True, **{k: v for k, v in kwargs.items() if k in ("password", "connect_timeout")}) if params.get("dsn") else psycopg.connect(autocommit=True, **kwargs)
    if name == "psycopg2":
        import psycopg2
        conn = psycopg2.connect(params["dsn"], **{k: v for k, v in kwargs.items() if k in ("password", "connect_timeout")}) if params.get("dsn") else psycopg2.connect(**kwargs)
        conn.autocommit = True
        return conn
    raise BackendError("no PostgreSQL driver is installed: install psycopg (the Database section offers it)")


def vector_literal(row: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in row) + "]"


def parse_version(text: str) -> Tuple[int, ...]:
    out: List[int] = []
    for part in str(text).split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


class PostgresBackend(SqlBackend):
    name = "postgres_pgvector"

    def __init__(self, params: Dict[str, Any], password: str = "", prefix: str = "rag_", vectors: str = "auto", connect: Optional[Callable[[], Any]] = None) -> None:
        """`vectors`: `auto` uses pgvector when the database has it, `blob` never does."""
        self.params, self.mode, self.pgvector = dict(params), vectors, ""
        label = params.get("dsn") and "dsn" or f"{params.get('host', 'localhost')}:{params.get('port', 5432)}/{params.get('dbname', '')}"
        super().__init__(connect or (lambda: connect_postgres(self.params, password)), POSTGRES, prefix, label=label)

    def _open(self) -> Any:
        fresh = self.conn is None
        conn = super()._open()
        if fresh:
            self.pgvector = self._detect_vector(conn)
        return conn

    @staticmethod
    def _detect_vector(conn: Any) -> str:
        """The version of pgvector enabled in this database ("" when it is not, or is older than halfvec)."""
        cur = conn.cursor()
        try:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
        except Exception:
            return ""
        finally:
            cur.close()
        return str(row[0]) if row and parse_version(row[0]) >= MIN_PGVECTOR else ""

    @property
    def native_vectors(self) -> bool:                                  # type: ignore[override]
        return bool(self.pgvector) and self.mode != "blob"

    @property
    def native_words(self) -> bool:                                    # type: ignore[override]
        return True

    def info(self) -> Dict[str, Any]:
        out = super().info()
        out["pgvector"] = self.pgvector or "no"
        return out

    # -- words: a tsvector made from the same plain words the application's index uses -----------------------------------------------------------

    def _search_text(self, chunk: Dict[str, Any]) -> Optional[str]:
        return " ".join(ranking.tokens(searchable(chunk)))

    def _after_prepare(self) -> None:
        self.run(f"ALTER TABLE {self.t('chunks')} ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(search_text, ''))) STORED").close()
        self.run(f"CREATE INDEX IF NOT EXISTS {self.t('chunks_tsv')} ON {self.t('chunks')} USING GIN (tsv)").close()

    def word_search(self, aid: str, query: str, k: int) -> Optional[List[Tuple[int, float]]]:
        words = list(dict.fromkeys(ranking.tokens(query)))
        if not words:
            return []
        expression = " | ".join("'" + w.replace("'", "''") + "'" for w in words)
        with self.lock:
            rows = self.all(f"SELECT pos, ts_rank(tsv, q) FROM {self.t('chunks')}, to_tsquery('simple', ?) AS q WHERE archive_id = ? AND tsv @@ q ORDER BY 2 DESC, pos LIMIT ?",
                            (expression, aid, int(k)))
        return [(int(pos), float(score)) for pos, score in rows]

    # -- vectors: a halfvec table with an HNSW index for each archive and slot ---------------------------------------------------------------------

    def _vec_table(self, aid: str, slot: str) -> str:
        return self.t("v_") + table_token(aid, slot)

    def _index_vectors(self, aid: str, slot: str, data: np.ndarray) -> str:
        if not self.native_vectors:
            return ""
        table, dims = self._vec_table(aid, slot), int(data.shape[1])
        self.run(f"CREATE TABLE {table} (pos INTEGER PRIMARY KEY, embedding halfvec({dims}) NOT NULL)").close()
        self.many(f"INSERT INTO {table} (pos, embedding) VALUES (?, ?::halfvec)", [(i, vector_literal(data[i])) for i in range(data.shape[0])])
        if dims <= HNSW_MAX:
            self.run(f"CREATE INDEX {table}_hnsw ON {table} USING hnsw (embedding halfvec_cosine_ops)").close()
        return table

    def _drop_index(self, table: str) -> None:
        self.run(f"DROP TABLE IF EXISTS {table}").close()

    def vector_search(self, aid: str, query: np.ndarray, k: int, slot: str = MAIN) -> List[Tuple[int, float]]:
        if not self.native_vectors:
            return super().vector_search(aid, query, k, slot)
        with self.lock:
            note = self.vector_note(aid, slot)
            if note is None or note["dims"] != query.shape[0]:
                return []
            row = self.one(f"SELECT tbl FROM {self.t('vectors')} WHERE archive_id = ? AND slot = ?", (aid, slot))
            table = row[0] if row else ""
            if not table:                                              # vectors stored while the extension was not there: indexed now, once
                matrix = self.read_vectors(aid, slot)
                with self.tx():
                    table = self._index_vectors(aid, slot, matrix)
                    self.run(f"UPDATE {self.t('vectors')} SET tbl = ? WHERE archive_id = ? AND slot = ?", (table, aid, slot)).close()
            literal = vector_literal(np.asarray(query, dtype=np.float32))
            rows = self.all(f"SELECT pos, 1 - (embedding <=> ?::halfvec) FROM {table} ORDER BY embedding <=> ?::halfvec LIMIT ?", (literal, literal, max(1, int(k))))
        return [(int(i), float(c)) for i, c in rows]
