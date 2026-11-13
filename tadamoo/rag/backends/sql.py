"""A storage on any SQL database that has a DB-API driver: the schema, its versions, every read and write, and an exact search done in the application. This is the
fallback for a database with no vector extension (MariaDB, MySQL, a PostgreSQL without pgvector, a SQLite without sqlite-vec): the vectors are a binary column, and a
search loads the vectors of the archive and multiplies. The backends with an extension (sqlite_vec.py, postgres_pgvector.py) are this class with a faster search.

The tables carry a prefix (`rag_`) so that they can live inside a database that has others, which are never touched. SQL is written with `?` and turned into the
driver's own placeholder."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag.backends.base import MAIN, Backend, BackendError
from tadamoo.rag.errors import RagError

SCHEMA_VERSION = 1
BLOCK_ROWS = 256                             # vectors kept together in one binary value


@dataclass(frozen=True)
class Dialect:
    name: str
    ph: str = "?"
    key: str = "VARCHAR(190)"
    text: str = "TEXT"
    blob: str = "BLOB"
    real: str = "REAL"


SQLITE = Dialect("sqlite")
POSTGRES = Dialect("postgres", "%s", blob="BYTEA", real="DOUBLE PRECISION")
MYSQL = Dialect("mysql", "%s", text="LONGTEXT", blob="LONGBLOB", real="DOUBLE")
DIALECTS = {d.name: d for d in (SQLITE, POSTGRES, MYSQL)}

CHUNK_BASE = ("doc", "n", "text", "path", "page_start", "page_end", "kind", "section")
CHUNK_OPT = ("flag", "summary", "questions", "facts")
SECTION_BASE = ("doc", "id", "parent", "path", "page_start", "page_end", "text")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def migrations(p: str, d: Dialect) -> List[Tuple[int, List[str]]]:
    """The schema as a list of (version, statements): a database is brought up by running those above its version."""
    k, t, b, r = d.key, d.text, d.blob, d.real
    v1 = [
        f"CREATE TABLE {p}schema (version INTEGER NOT NULL)",
        f"CREATE TABLE {p}archives (id {k} PRIMARY KEY, project {k}, name {t}, rev INTEGER NOT NULL, updated {r}, chunks INTEGER, embed_model {t}, embed_dim INTEGER, data {t} NOT NULL)",
        f"CREATE INDEX {p}archives_project ON {p}archives (project)",
        f"CREATE TABLE {p}documents (archive_id {k} NOT NULL, doc_id {k} NOT NULL, pos INTEGER NOT NULL, name {t}, sha256 {k}, chunks INTEGER, data {t} NOT NULL, PRIMARY KEY (archive_id, doc_id))",
        f"CREATE TABLE {p}chunks (archive_id {k} NOT NULL, pos INTEGER NOT NULL, doc {k} NOT NULL, n INTEGER NOT NULL, text {t} NOT NULL, path {t}, page_start INTEGER, page_end INTEGER, "
        f"kind {k}, section {k}, flag {t}, summary {t}, questions {t}, facts {t}, search_text {t}, data {t}, PRIMARY KEY (archive_id, pos))",
        f"CREATE INDEX {p}chunks_doc ON {p}chunks (archive_id, doc)",
        f"CREATE TABLE {p}sections (archive_id {k} NOT NULL, section_id {k} NOT NULL, pos INTEGER NOT NULL, doc {k}, parent {k}, path {t}, page_start INTEGER, page_end INTEGER, "
        f"text {t}, data {t}, PRIMARY KEY (archive_id, section_id))",
        f"CREATE TABLE {p}vectors (archive_id {k} NOT NULL, slot {k} NOT NULL, tbl {k}, dims INTEGER NOT NULL, model {t}, version {k}, rows_n INTEGER NOT NULL, note {t}, gen INTEGER NOT NULL, "
        f"PRIMARY KEY (archive_id, slot))",
        f"CREATE TABLE {p}vec_blob (archive_id {k} NOT NULL, slot {k} NOT NULL, pos INTEGER NOT NULL, n INTEGER NOT NULL, v {b} NOT NULL, PRIMARY KEY (archive_id, slot, pos))",
        f"CREATE TABLE {p}figures (archive_id {k} NOT NULL, doc {k} NOT NULL, data {t} NOT NULL, PRIMARY KEY (archive_id, doc))",
        f"CREATE TABLE {p}blobs (archive_id {k} NOT NULL, kind {k} NOT NULL, k {k} NOT NULL, data {b} NOT NULL, PRIMARY KEY (archive_id, kind, k))",
        f"CREATE TABLE {p}sources (archive_id {k} PRIMARY KEY, data {t} NOT NULL)",
        f"CREATE TABLE {p}enrich (archive_id {k} NOT NULL, k {k} NOT NULL, data {t} NOT NULL, PRIMARY KEY (archive_id, k))",
    ]
    return [(1, v1)]


class SqlBackend(Backend):
    name = "sql_exact_app"

    def __init__(self, connect: Callable[[], Any], dialect: Dialect = SQLITE, prefix: str = "rag_", label: str = "") -> None:
        if not prefix.replace("_", "").isalnum():
            raise RagError("the table prefix is letters, digits and underscores")
        self._connect, self.d, self.p, self.label = connect, dialect, prefix, label
        self.conn: Any = None
        self.lock = threading.RLock()
        self._depth = 0
        self._matrices: Dict[Tuple[str, str], Tuple[int, np.ndarray]] = {}
        self.migration_list = migrations

    # -- connection and transactions -------------------------------------------------------------------------

    def _open(self) -> Any:
        if self.conn is None:
            try:
                self.conn = self._connect()
            except Exception as e:
                raise BackendError(f"the database could not be reached: {type(e).__name__}: {str(e)[:200]}") from None
        return self.conn

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                finally:
                    self.conn = None

    def q(self, sql: str) -> str:
        return sql if self.d.ph == "?" else sql.replace("?", self.d.ph)

    def run(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cur = self._open().cursor()
        cur.execute(self.q(sql), tuple(params))
        return cur

    def all(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        cur = self.run(sql, params)
        try:
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Tuple[Any, ...]]:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        cur = self._open().cursor()
        try:
            cur.executemany(self.q(sql), [tuple(r) for r in rows])
        finally:
            cur.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """One transaction: everything inside is kept together or undone. The connection is in autocommit mode and the transaction is explicit, so a read outside one never
        leaves a transaction open."""
        with self.lock:
            conn = self._open()
            outer = self._depth > 0
            self._depth += 1
            try:
                if not outer:
                    self._statement("BEGIN")
                yield
                if not outer:
                    self._statement("COMMIT")
            except BaseException:
                if not outer:
                    try:
                        self._statement("ROLLBACK")
                    except Exception:
                        pass
                    self._matrices.clear()
                raise
            finally:
                self._depth -= 1

    def _statement(self, sql: str) -> None:
        cur = self._open().cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()

    def t(self, name: str) -> str:
        return self.p + name

    # -- the schema ------------------------------------------------------------------------------------------

    def schema_version(self) -> int:
        with self.lock:
            try:
                row = self.one(f"SELECT version FROM {self.t('schema')}")
            except Exception:
                return 0
            return int(row[0]) if row else 0

    def prepare(self) -> Dict[str, Any]:
        with self.lock:
            have = self.schema_version()
            for version, statements in self.migration_list(self.p, self.d):
                if version <= have:
                    continue
                with self.tx():
                    for statement in statements:
                        self.run(statement).close()
                    self.run(f"DELETE FROM {self.t('schema')}").close()
                    self.run(f"INSERT INTO {self.t('schema')} (version) VALUES (?)", (version,)).close()
                have = version
            self._after_prepare()
            return self.info()

    def _after_prepare(self) -> None:
        """What a backend with an extension adds to the schema (its search tables)."""

    def info(self) -> Dict[str, Any]:
        return {"backend": self.name, "dialect": self.d.name, "schema": self.schema_version(), "vectors": "native" if self.native_vectors else "app",
                "words": "native" if self.native_words else "app", "prefix": self.p, "label": self.label}

    # -- archives --------------------------------------------------------------------------------------------

    def archive_ids(self, project: Optional[str] = None) -> List[str]:
        with self.lock:
            if project is None:
                return [r[0] for r in self.all(f"SELECT id FROM {self.t('archives')} ORDER BY id")]
            return [r[0] for r in self.all(f"SELECT id FROM {self.t('archives')} WHERE project = ? ORDER BY id", (project,))]

    def revisions(self) -> Dict[str, int]:
        with self.lock:
            return {r[0]: int(r[1]) for r in self.all(f"SELECT id, rev FROM {self.t('archives')}")}

    def revision(self, aid: str) -> Optional[int]:
        with self.lock:
            row = self.one(f"SELECT rev FROM {self.t('archives')} WHERE id = ?", (aid,))
            return int(row[0]) if row else None

    def _documents(self, aid: str) -> List[Dict[str, Any]]:
        return [json.loads(r[0]) for r in self.all(f"SELECT data FROM {self.t('documents')} WHERE archive_id = ? ORDER BY pos", (aid,))]

    def read_meta(self, aid: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.one(f"SELECT data FROM {self.t('archives')} WHERE id = ?", (aid,))
            if row is None:
                return None
            return {**json.loads(row[0]), "documents": self._documents(aid)}

    def read_metas(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.lock:
            sql = f"SELECT id, data FROM {self.t('archives')}" + ("" if project is None else " WHERE project = ?")
            rows = self.all(sql, () if project is None else (project,))
            docs: Dict[str, List[Dict[str, Any]]] = {}
            for aid, data in self.all(f"SELECT archive_id, data FROM {self.t('documents')} ORDER BY archive_id, pos"):
                docs.setdefault(aid, []).append(json.loads(data))
            return [{**json.loads(data), "documents": docs.get(aid, [])} for aid, data in rows]

    def _put_meta(self, aid: str, meta: Dict[str, Any]) -> None:
        body = {k: v for k, v in meta.items() if k != "documents"}
        row = self.one(f"SELECT rev FROM {self.t('archives')} WHERE id = ?", (aid,))
        rev = (int(row[0]) if row else 0) + 1
        self.run(f"DELETE FROM {self.t('archives')} WHERE id = ?", (aid,)).close()
        self.run(f"INSERT INTO {self.t('archives')} (id, project, name, rev, updated, chunks, embed_model, embed_dim, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (aid, str(meta.get("project") or ""), str(meta.get("name") or ""), rev, float(meta.get("updated") or 0), int(meta.get("chunks") or 0),
                  str(meta.get("embed_model") or ""), int(meta.get("embed_dim") or 0), dumps(body))).close()
        self.run(f"DELETE FROM {self.t('documents')} WHERE archive_id = ?", (aid,)).close()
        self.many(f"INSERT INTO {self.t('documents')} (archive_id, doc_id, pos, name, sha256, chunks, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  [(aid, d["id"], i, str(d.get("name") or ""), str(d.get("sha256") or ""), int(d.get("chunks") or 0), dumps(d)) for i, d in enumerate(meta.get("documents") or [])])

    def write_meta(self, aid: str, meta: Dict[str, Any]) -> None:
        with self.tx():
            self._put_meta(aid, meta)

    def commit(self, aid: str, meta: Dict[str, Any], chunks: Sequence[Dict[str, Any]], sections: Optional[Dict[str, Dict[str, Any]]],
               vectors: Optional[np.ndarray], model: str = "", version: str = "") -> None:
        with self.tx():
            self._put_meta(aid, meta)
            self.run(f"DELETE FROM {self.t('chunks')} WHERE archive_id = ?", (aid,)).close()
            self._before_chunks(aid)
            rows = [self._chunk_row(aid, i, c) for i, c in enumerate(chunks)]
            self.many(f"INSERT INTO {self.t('chunks')} (archive_id, pos, doc, n, text, path, page_start, page_end, kind, section, flag, summary, questions, facts, search_text, data) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._after_chunks(aid, chunks)
            if sections is not None:
                self.run(f"DELETE FROM {self.t('sections')} WHERE archive_id = ?", (aid,)).close()
                self.many(f"INSERT INTO {self.t('sections')} (archive_id, section_id, pos, doc, parent, path, page_start, page_end, text, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [self._section_row(aid, i, sid, s) for i, (sid, s) in enumerate(sections.items())])
            if vectors is not None:
                self._put_vectors(aid, MAIN, vectors, model, version, None)
            else:
                self._drop_vectors(aid, MAIN)

    def _before_chunks(self, aid: str) -> None:
        """Hook: a backend with its own text index clears it for this archive."""

    def _after_chunks(self, aid: str, chunks: Sequence[Dict[str, Any]]) -> None:
        """Hook: a backend with its own text index fills it from the passages just written."""

    def _search_text(self, chunk: Dict[str, Any]) -> Optional[str]:
        return None

    @staticmethod
    def _opt(chunk: Dict[str, Any], key: str) -> Optional[str]:
        return dumps(chunk[key]) if key in ("questions", "facts") and key in chunk else (chunk[key] if key in chunk else None)

    def _chunk_row(self, aid: str, pos: int, c: Dict[str, Any]) -> Tuple[Any, ...]:
        extra = {k: v for k, v in c.items() if k not in CHUNK_BASE and k not in CHUNK_OPT}
        return (aid, pos, c["doc"], int(c["n"]), c["text"], c.get("path", ""), int(c.get("page_start") or 0), int(c.get("page_end") or 0), c.get("kind", "text"), c.get("section", ""),
                self._opt(c, "flag"), self._opt(c, "summary"), self._opt(c, "questions"), self._opt(c, "facts"), self._search_text(c), dumps(extra) if extra else None)

    @staticmethod
    def _chunk_dict(row: Tuple[Any, ...]) -> Dict[str, Any]:
        doc, n, text, path, ps, pe, kind, section, flag, summary, questions, facts, data = row
        out: Dict[str, Any] = {"doc": doc, "n": int(n), "text": text, "path": path or "", "page_start": int(ps or 0), "page_end": int(pe or 0), "kind": kind, "section": section}
        if flag is not None:
            out["flag"] = flag
        if summary is not None:
            out["summary"] = summary
        if questions is not None:
            out["questions"] = json.loads(questions)
        if facts is not None:
            out["facts"] = json.loads(facts)
        if data:
            out.update(json.loads(data))
        return out

    def _section_row(self, aid: str, pos: int, sid: str, s: Dict[str, Any]) -> Tuple[Any, ...]:
        extra = {k: v for k, v in s.items() if k not in SECTION_BASE}
        return (aid, sid, pos, s.get("doc", ""), s.get("parent", ""), s.get("path", ""), int(s.get("page_start") or 0), int(s.get("page_end") or 0), s.get("text", ""),
                dumps(extra) if extra else None)

    def read_chunks(self, aid: str) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.all(f"SELECT doc, n, text, path, page_start, page_end, kind, section, flag, summary, questions, facts, data FROM {self.t('chunks')} "
                            "WHERE archive_id = ? ORDER BY pos", (aid,))
            return [self._chunk_dict(r) for r in rows]

    def read_sections(self, aid: str) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            out: Dict[str, Dict[str, Any]] = {}
            for sid, doc, parent, path, ps, pe, text, data in self.all(
                    f"SELECT section_id, doc, parent, path, page_start, page_end, text, data FROM {self.t('sections')} WHERE archive_id = ? ORDER BY pos", (aid,)):
                row = {"doc": doc, "id": sid, "parent": parent or "", "path": path or "", "page_start": int(ps or 0), "page_end": int(pe or 0), "text": text or ""}
                if data:
                    row.update(json.loads(data))
                out[sid] = row
            return out

    def counts(self, aid: str) -> Dict[str, int]:
        with self.lock:
            def n(table: str) -> int:
                return int((self.one(f"SELECT COUNT(*) FROM {self.t(table)} WHERE archive_id = ?", (aid,)) or (0,))[0])
            note = self.vector_note(aid, MAIN)
            return {"chunks": n("chunks"), "documents": n("documents"), "sections": n("sections"), "vectors": int(note["rows"]) if note else 0}

    def delete_archive(self, aid: str) -> None:
        with self.tx():
            for slot in [r[0] for r in self.all(f"SELECT slot FROM {self.t('vectors')} WHERE archive_id = ?", (aid,))]:
                self._drop_vectors(aid, slot)
            self._before_chunks(aid)
            for table in ("chunks", "sections", "documents", "figures", "blobs", "sources", "enrich", "archives"):
                self.run(f"DELETE FROM {self.t(table)} WHERE " + ("id" if table == "archives" else "archive_id") + " = ?", (aid,)).close()
            self._matrices = {k: v for k, v in self._matrices.items() if k[0] != aid}

    # -- vectors (a binary column; an extension adds its own index on top) ---------------------------------

    def vector_note(self, aid: str, slot: str = MAIN) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.one(f"SELECT dims, model, version, rows_n, note, gen FROM {self.t('vectors')} WHERE archive_id = ? AND slot = ?", (aid, slot))
            if row is None:
                return None
            return {"dims": int(row[0]), "model": row[1] or "", "version": row[2] or "", "rows": int(row[3]), "note": json.loads(row[4]) if row[4] else {}, "gen": int(row[5])}

    def put_vectors(self, aid: str, slot: str, vectors: np.ndarray, model: str, version: str, note: Optional[Dict[str, Any]] = None) -> None:
        with self.tx():
            self._put_vectors(aid, slot, vectors, model, version, note)

    def _put_vectors(self, aid: str, slot: str, vectors: np.ndarray, model: str, version: str, note: Optional[Dict[str, Any]]) -> None:
        data = np.ascontiguousarray(vectors, dtype=np.float32)
        if data.ndim != 2 or not data.shape[0]:
            raise RagError("the vectors are a matrix with one row for each passage")
        before = self.vector_note(aid, slot)
        self._drop_vectors(aid, slot)
        blocks = [(aid, slot, i, int(min(BLOCK_ROWS, data.shape[0] - i)), data[i:i + BLOCK_ROWS].tobytes()) for i in range(0, data.shape[0], BLOCK_ROWS)]
        self.many(f"INSERT INTO {self.t('vec_blob')} (archive_id, slot, pos, n, v) VALUES (?, ?, ?, ?, ?)", blocks)
        table = self._index_vectors(aid, slot, data)
        self.run(f"INSERT INTO {self.t('vectors')} (archive_id, slot, tbl, dims, model, version, rows_n, note, gen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (aid, slot, table, int(data.shape[1]), model, version, int(data.shape[0]), dumps(note) if note else None, (before["gen"] if before else 0) + 1)).close()
        self._matrices.pop((aid, slot), None)

    def _index_vectors(self, aid: str, slot: str, data: np.ndarray) -> str:
        """Hook: a backend with a vector index builds it here and returns the name of its table ("" for none)."""
        return ""

    def drop_vectors(self, aid: str, slot: str) -> None:
        with self.tx():
            self._drop_vectors(aid, slot)

    def _drop_vectors(self, aid: str, slot: str) -> None:
        row = self.one(f"SELECT tbl FROM {self.t('vectors')} WHERE archive_id = ? AND slot = ?", (aid, slot))
        if row is not None and row[0]:
            self._drop_index(row[0])
        self.run(f"DELETE FROM {self.t('vec_blob')} WHERE archive_id = ? AND slot = ?", (aid, slot)).close()
        self.run(f"DELETE FROM {self.t('vectors')} WHERE archive_id = ? AND slot = ?", (aid, slot)).close()
        self._matrices.pop((aid, slot), None)

    def _drop_index(self, table: str) -> None:
        """Hook: a backend with a vector index drops its table."""

    def read_vectors(self, aid: str, slot: str = MAIN) -> Optional[np.ndarray]:
        with self.lock:
            note = self.vector_note(aid, slot)
            if note is None:
                return None
            held = self._matrices.get((aid, slot))
            if held is not None and held[0] == note["gen"]:
                return held[1]
            parts = [np.frombuffer(bytes(v), dtype=np.float32).reshape(int(n), note["dims"]) for n, v in
                     self.all(f"SELECT n, v FROM {self.t('vec_blob')} WHERE archive_id = ? AND slot = ? ORDER BY pos", (aid, slot))]
            if not parts:
                return None
            matrix = np.vstack(parts)
            self._matrices[(aid, slot)] = (note["gen"], matrix)
            return matrix

    def vector_search(self, aid: str, query: np.ndarray, k: int, slot: str = MAIN) -> List[Tuple[int, float]]:
        """Exact: the cosine of every row with `query` (the rows and the query are normalized), the best `k`."""
        matrix = self.read_vectors(aid, slot)
        if matrix is None or matrix.shape[1] != query.shape[0]:
            return []
        cos = matrix @ np.asarray(query, dtype=np.float32)
        k = max(1, min(int(k), cos.shape[0]))
        top = np.argpartition(-cos, k - 1)[:k] if k < cos.shape[0] else np.arange(cos.shape[0])
        top = top[np.argsort(-cos[top], kind="stable")]
        return [(int(i), float(cos[i])) for i in top]

    # -- what hangs on an archive ----------------------------------------------------------------------------

    def put_blob(self, aid: str, kind: str, key: str, data: bytes) -> None:
        with self.tx():
            self.run(f"DELETE FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ? AND k = ?", (aid, kind, key)).close()
            self.run(f"INSERT INTO {self.t('blobs')} (archive_id, kind, k, data) VALUES (?, ?, ?, ?)", (aid, kind, key, bytes(data))).close()

    def get_blob(self, aid: str, kind: str, key: str) -> Optional[bytes]:
        with self.lock:
            row = self.one(f"SELECT data FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ? AND k = ?", (aid, kind, key))
            return bytes(row[0]) if row else None

    def list_blobs(self, aid: str, kind: str) -> List[str]:
        with self.lock:
            return [r[0] for r in self.all(f"SELECT k FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ? ORDER BY k", (aid, kind))]

    def delete_blobs(self, aid: str, kind: str, key: Optional[str] = None, prefix: Optional[str] = None) -> None:
        with self.tx():
            if key is not None:
                self.run(f"DELETE FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ? AND k = ?", (aid, kind, key)).close()
            elif prefix is not None:
                for k in [r[0] for r in self.all(f"SELECT k FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ?", (aid, kind)) if r[0].startswith(prefix)]:
                    self.run(f"DELETE FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ? AND k = ?", (aid, kind, k)).close()
            else:
                self.run(f"DELETE FROM {self.t('blobs')} WHERE archive_id = ? AND kind = ?", (aid, kind)).close()

    def read_figures(self, aid: str, doc: str) -> List[Dict[str, Any]]:
        with self.lock:
            row = self.one(f"SELECT data FROM {self.t('figures')} WHERE archive_id = ? AND doc = ?", (aid, doc))
            data = json.loads(row[0]) if row else []
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def write_figures(self, aid: str, doc: str, records: Sequence[Dict[str, Any]]) -> None:
        with self.tx():
            self.run(f"DELETE FROM {self.t('figures')} WHERE archive_id = ? AND doc = ?", (aid, doc)).close()
            if records:
                self.run(f"INSERT INTO {self.t('figures')} (archive_id, doc, data) VALUES (?, ?, ?)", (aid, doc, dumps(list(records)))).close()

    def read_sources(self, aid: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.one(f"SELECT data FROM {self.t('sources')} WHERE archive_id = ?", (aid,))
            return json.loads(row[0]) if row else None

    def write_sources(self, aid: str, data: Dict[str, Any]) -> None:
        with self.tx():
            self.run(f"DELETE FROM {self.t('sources')} WHERE archive_id = ?", (aid,)).close()
            self.run(f"INSERT INTO {self.t('sources')} (archive_id, data) VALUES (?, ?)", (aid, dumps(data))).close()

    def enrich_read(self, aid: str) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return {k: json.loads(d) for k, d in self.all(f"SELECT k, data FROM {self.t('enrich')} WHERE archive_id = ?", (aid,))}

    def enrich_add(self, aid: str, rows: Iterable[Dict[str, Any]]) -> None:
        rows = list(rows)
        with self.tx():
            for r in rows:
                self.run(f"DELETE FROM {self.t('enrich')} WHERE archive_id = ? AND k = ?", (aid, r["k"])).close()
            self.many(f"INSERT INTO {self.t('enrich')} (archive_id, k, data) VALUES (?, ?, ?)", [(aid, r["k"], dumps(r)) for r in rows])

    def enrich_clear(self, aid: str) -> None:
        with self.tx():
            self.run(f"DELETE FROM {self.t('enrich')} WHERE archive_id = ?", (aid,)).close()


def table_token(aid: str, slot: str) -> str:
    """A short name for the vector table of an archive and slot."""
    return hashlib.sha1(aid.encode("utf-8")).hexdigest()[:12] + ("" if slot == MAIN else "_" + slot)
