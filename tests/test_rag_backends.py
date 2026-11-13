"""The storage of the knowledge archives: the SQL backends (SQLite with FTS5 and sqlite-vec, the generic exact fallback, PostgreSQL with a stand-in connection), the schema and its
versions, a database that has other tables, the router of projects, and the import of the old file format."""
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag import query as rag_query
from tadamoo.rag.backends import Router, clean_config, legacy, open_backend, store_password, vault_password
from tadamoo.rag.backends.base import MAIN, RT
from tadamoo.rag.backends.postgres_pgvector import PostgresBackend
from tadamoo.rag.backends.sql import POSTGRES, SQLITE, SqlBackend, migrations
from tadamoo.rag.backends.sqlite_vec import SqliteVecBackend

PROJ = project_key("p")
vec = pytest.importorskip  # noqa: F841  (the real sqlite-vec is tried where a test says so)


def meta_of(aid="a-1", chunks=2, model="m", dim=3):
    return {"id": aid, "name": "A", "description": "d", "project": PROJ, "cloud_ok": False, "others_ok": "ask_dialog", "created": 1.0, "updated": 2.0, "chunks": chunks, "embed_model": model,
            "embed_dim": dim, "embed_version": "v", "ingest": {"normalizer": 1}, "documents": [{"id": "d1", "name": "x.txt", "sha256": "s", "chunks": chunks, "info": {"kind": "patent"}}]}


CHUNKS = [{"doc": "d1", "n": 0, "text": "the quick brown fox jumps", "path": "x", "page_start": 1, "page_end": 1, "kind": "text", "section": "d1.s0", "facts": {"lang": "en"}},
          {"doc": "d1", "n": 1, "text": "perche la volpe e veloce", "path": "x > y", "page_start": 2, "page_end": 3, "kind": "figure", "section": "d1.s0", "flag": "instructions",
           "summary": "a fox", "questions": [], "figure": "FIG. 1", "fig": 1, "box": [0.1, 0.2, 0.3, 0.4]}]
SECTIONS = {"d1.s0": {"doc": "d1", "id": "d1.s0", "parent": "", "path": "x", "page_start": 1, "page_end": 3, "text": "all of it", "level": 1, "summary": "s"}}
VECTORS = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)


@pytest.fixture
def db(tmp_path):
    b = SqliteVecBackend(tmp_path / "x.sqlite")
    b.prepare()
    yield b
    b.close()


# -- one archive through a backend --------------------------------------------------------------------------

def test_an_archive_comes_back_exactly_as_it_was_written(db):
    meta = meta_of()
    db.commit("a-1", meta, CHUNKS, SECTIONS, VECTORS, "m", "v")
    assert db.read_meta("a-1") == meta and db.read_chunks("a-1") == CHUNKS and db.read_sections("a-1") == SECTIONS
    assert np.array_equal(db.read_vectors("a-1"), VECTORS) and db.vector_note("a-1")["model"] == "m" and db.counts("a-1") == {"chunks": 2, "documents": 1, "sections": 1, "vectors": 2}
    assert db.archive_ids() == ["a-1"] and db.archive_ids(PROJ) == ["a-1"] and db.archive_ids("other") == [] and db.read_metas()[0] == meta


def test_a_change_is_one_transaction_and_every_write_moves_the_revision(db):
    db.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    first = db.revision("a-1")
    db.write_meta("a-1", {**meta_of(), "name": "B"})
    assert db.revision("a-1") == first + 1 and db.read_chunks("a-1") == CHUNKS          # the passages stay
    bad = [dict(CHUNKS[0]), {"n": 1}]                                                       # a passage with no text: the commit fails whole
    with pytest.raises(Exception):
        db.commit("a-1", meta_of(), bad, None, None)
    assert db.read_chunks("a-1") == CHUNKS and db.read_meta("a-1")["name"] == "B" and db.counts("a-1")["vectors"] == 2
    db.commit("a-1", meta_of(chunks=1), CHUNKS[:1], None, None)                             # no vectors: the old ones go; sections None: they stay
    assert db.vector_note("a-1") is None and db.read_vectors("a-1") is None and list(db.read_sections("a-1")) == ["d1.s0"]


def test_a_deleted_archive_leaves_nothing_in_any_table(db):
    db.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    db.put_blob("a-1", "blocks", "d1", b"x")
    db.write_figures("a-1", "d1", [{"n": 1}])
    db.write_sources("a-1", {"sources": []})
    db.enrich_add("a-1", [{"k": "k1", "summary": "s"}])
    db.put_vectors("a-1", RT, VECTORS, "small", "v", {"fingerprint": "f"})
    db.delete_archive("a-1")
    for table in ("archives", "documents", "chunks", "sections", "vectors", "vec_blob", "figures", "blobs", "sources", "enrich", "fts"):
        assert db.one(f"SELECT COUNT(*) FROM rag_{table}")[0] == 0, table


def test_what_hangs_on_an_archive_is_kept_in_its_database(db):
    db.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    db.put_blob("a-1", "image", "d1/1.webp", b"\x00\x01")
    db.put_blob("a-1", "image", "d1/2.webp", b"\x02")
    db.put_blob("a-1", "image", "d2/1.webp", b"\x03")
    assert db.get_blob("a-1", "image", "d1/1.webp") == b"\x00\x01" and db.list_blobs("a-1", "image") == ["d1/1.webp", "d1/2.webp", "d2/1.webp"]
    db.delete_blobs("a-1", "image", prefix="d1/")
    assert db.list_blobs("a-1", "image") == ["d2/1.webp"] and db.get_blob("a-1", "image", "nope") is None
    db.write_figures("a-1", "d1", [{"n": 1, "label": "FIG. 1"}])
    assert db.read_figures("a-1", "d1") == [{"n": 1, "label": "FIG. 1"}] and db.read_figures("a-1", "d9") == []
    db.write_figures("a-1", "d1", [])
    assert db.read_figures("a-1", "d1") == []
    db.enrich_add("a-1", [{"k": "x", "summary": "one"}, {"k": "y", "summary": "two"}])
    db.enrich_add("a-1", [{"k": "x", "summary": "three"}])
    assert db.enrich_read("a-1")["x"]["summary"] == "three" and len(db.enrich_read("a-1")) == 2
    db.enrich_clear("a-1")
    assert db.enrich_read("a-1") == {} and db.read_sources("a-1") is None
    db.write_sources("a-1", {"sources": [1]})
    assert db.read_sources("a-1") == {"sources": [1]}


def test_the_second_vectors_are_a_slot_of_their_own(db):
    db.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    db.put_vectors("a-1", RT, np.array([[0, 0, 1], [0, 1, 0]], dtype=np.float32), "small", "q", {"fingerprint": "f1"})
    assert db.vector_note("a-1", RT)["note"] == {"fingerprint": "f1"} and db.vector_note("a-1", RT)["gen"] == 1 and db.vector_note("a-1")["model"] == "m"
    db.put_vectors("a-1", RT, np.array([[0, 0, 1], [1, 0, 0]], dtype=np.float32), "small", "q", {"fingerprint": "f2"})
    assert db.vector_note("a-1", RT)["gen"] == 2 and db.read_vectors("a-1", RT)[1].tolist() == [1, 0, 0]
    assert db.vector_search("a-1", np.array([0, 0, 1], dtype=np.float32), 1, RT) == [(0, 1.0)]
    db.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")                       # the archive's own commit leaves the other slot alone
    assert db.vector_note("a-1", RT) is not None


def test_a_big_matrix_is_stored_in_blocks_and_read_back_whole(db):
    rng = np.random.default_rng(1)
    m = rng.normal(size=(700, 16)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    chunks = [{"doc": "d1", "n": i, "text": f"word{i} common", "path": "", "page_start": 1, "page_end": 1, "kind": "text", "section": "d1.s0"} for i in range(700)]
    db.commit("a-1", meta_of(chunks=700, dim=16), chunks, None, m, "m", "v")
    assert db.one("SELECT COUNT(*) FROM rag_vec_blob")[0] == 3 and np.array_equal(db.read_vectors("a-1"), m)
    top = db.vector_search("a-1", m[321], 3)
    assert top[0][0] == 321 and abs(top[0][1] - 1.0) < 1e-5 and len(top) == 3


# -- words -----------------------------------------------------------------------------------------------------

def test_words_are_searched_by_the_database_over_the_same_plain_words_the_application_uses(db):
    texts = ["Il gatto dorme sul divano", "La barca a vela ha una chiglia profonda", "Il risotto si fa con riso e brodo caldo", "Perché la chiglia tiene la barca dritta", "The keel of the yacht"]
    chunks = [{"doc": "d1", "n": i, "text": t, "path": "", "page_start": 1, "page_end": 1, "kind": "text", "section": "d1.s0"} for i, t in enumerate(texts)]
    db.commit("a-1", meta_of(chunks=5, model=""), chunks, None, None)
    assert db.native_words
    found = [i for i, _ in db.word_search("a-1", "perche chiglia", 5)]
    assert set(found[:2]) == {1, 3} and found[0] == 3 and db.word_search("a-1", "keel", 5)[0][0] == 4 and db.word_search("a-1", "zzzz", 5) == [] and db.word_search("a-1", " ", 5) == []
    assert db.word_search("a-1", "chiglia", 5, ) is not None and db.word_search("other", "chiglia", 5) == []


# -- the generic fallback ----------------------------------------------------------------------------------------

def test_any_sql_database_works_with_the_vectors_searched_by_the_application(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "plain.db"), isolation_level=None, check_same_thread=False)
    b = SqlBackend(lambda: conn, SQLITE, "kb_")
    info = b.prepare()
    assert info["backend"] == "sql_exact_app" and info["vectors"] == "app" and info["words"] == "app" and b.word_search("a-1", "x", 3) is None
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    assert b.read_chunks("a-1") == CHUNKS and b.vector_search("a-1", np.array([0, 1, 0], dtype=np.float32), 2)[0] == (1, 1.0)
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'rag\\_%' ESCAPE '\\'").fetchall()               # only the prefix it was given


def test_a_database_that_has_other_tables_keeps_them_and_gets_its_own_with_a_prefix(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "shared.db"), isolation_level=None, check_same_thread=False)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users VALUES (1, 'ada')")
    b = SqlBackend(lambda: conn, SQLITE, "kb_")
    b.prepare()
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    b.delete_archive("a-1")
    assert conn.execute("SELECT * FROM users").fetchall() == [(1, "ada")]
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names - {"users"} == {f"kb_{t}" for t in ("schema", "archives", "documents", "chunks", "sections", "vectors", "vec_blob", "figures", "blobs", "sources", "enrich")}
    with pytest.raises(RagError):
        SqlBackend(lambda: conn, SQLITE, "bad prefix;")


def test_the_schema_has_a_version_and_a_newer_one_is_applied_by_itself(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "v.db"), isolation_level=None, check_same_thread=False)
    b = SqlBackend(lambda: conn, SQLITE)
    assert b.schema_version() == 0 and b.prepare()["schema"] == 1
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    later = SqlBackend(lambda: conn, SQLITE)
    later.migration_list = lambda p, d: migrations(p, d) + [(2, [f"ALTER TABLE {p}archives ADD COLUMN note TEXT"])]
    assert later.prepare()["schema"] == 2 and later.read_meta("a-1") == meta_of()                    # nothing lost, nothing run twice
    assert later.prepare()["schema"] == 2


def test_a_file_made_where_the_extension_was_is_read_where_it_is_not(tmp_path, monkeypatch):
    from tadamoo.rag.backends import sqlite_vec as mod
    b = SqliteVecBackend(tmp_path / "m.sqlite")
    b.prepare()
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    b.close()
    again = SqliteVecBackend(tmp_path / "m.sqlite", extension=False)
    again.prepare()
    assert again.read_vectors("a-1").tolist() == VECTORS.tolist() and again.vector_search("a-1", np.array([1, 0, 0], dtype=np.float32), 1) == [(0, 1.0)]
    assert again.info()["extension"] == "off" and again.native_words


def test_the_real_sqlite_vec_when_it_is_installed(tmp_path):
    pytest.importorskip("sqlite_vec", reason="sqlite-vec is not installed here: its search is not tried")
    b = SqliteVecBackend(tmp_path / "v.sqlite")
    b.prepare()
    if not b.vec:
        pytest.skip("this SQLite cannot load extensions")
    rng = np.random.default_rng(2)
    m = rng.normal(size=(50, 8)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    chunks = [{"doc": "d1", "n": i, "text": f"t{i}", "path": "", "page_start": 1, "page_end": 1, "kind": "text", "section": "d1.s0"} for i in range(50)]
    b.commit("a-1", meta_of(chunks=50, dim=8), chunks, None, m, "m", "v")
    assert b.native_vectors and b.vector_note("a-1")["gen"] == 1
    got = b.vector_search("a-1", m[7], 3)
    exact = SqlBackend.vector_search(b, "a-1", m[7], 3)
    assert [i for i, _ in got] == [i for i, _ in exact] and abs(got[0][1] - 1.0) < 1e-4


# -- PostgreSQL with a stand-in connection ---------------------------------------------------------------------------

class FakePg:
    """A connection that records the SQL it is given and answers the questions the flow asks from a script; it keeps no data."""

    def __init__(self, answers=None, fail=None):
        self.sql = []
        self.answers = answers or {}
        self.fail = fail or {}
        self.created = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def execute(self, sql, params=()):
        self.conn.sql.append((sql, tuple(params)))
        for needle, error in self.conn.fail.items():
            if needle in sql:
                raise error
        if sql.startswith("CREATE EXTENSION"):
            self.conn.created = True
        self.rows = []
        for needle, rows in self.conn.answers.items():
            if needle in sql:
                self.rows = rows(self.conn) if callable(rows) else rows
                break

    def executemany(self, sql, rows):
        for r in rows:
            self.execute(sql, r)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


def pg(**answers):
    base = {"current_setting('server_version')": [("17.2", "ragdb", True, True)] and [("me", "17.2", "ragdb", False, True)]}
    base.update(answers)
    return FakePg(base)


def test_postgres_writes_halfvec_tables_with_an_hnsw_index_and_asks_with_the_same_type(monkeypatch):
    conn = pg(**{"extversion": [("0.8.0",)]})
    b = PostgresBackend({"host": "h", "dbname": "d"}, connect=lambda: conn)
    b.prepare()
    sql = [s for s, _ in conn.sql]
    assert any("tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'" in s for s in sql) and any("USING GIN (tsv)" in s for s in sql) and b.native_vectors and b.native_words
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    sql = [s for s, _ in conn.sql]
    assert any(s.startswith("CREATE TABLE rag_v_") and "halfvec(3)" in s for s in sql) and any("USING hnsw (embedding halfvec_cosine_ops)" in s for s in sql)
    assert any("INSERT INTO rag_v_" in s and "%s::halfvec" in s for s in sql) and "BEGIN" in sql and "COMMIT" in sql
    conn.answers["FROM rag_vectors WHERE archive_id"] = [(3, "m", "v", 2, None, 1)]
    conn.answers["SELECT tbl FROM"] = [("rag_v_abc",)]
    b.vector_search("a-1", np.array([1, 0, 0], dtype=np.float32), 5)
    knn = [(s, p) for s, p in conn.sql if "<=>" in s][-1]
    assert knn[0].count("%s::halfvec") == 2 and "ORDER BY embedding <=> %s::halfvec LIMIT %s" in knn[0] and knn[1][0] == "[1,0,0]" and knn[1][2] == 5
    b.word_search("a-1", "chiglia barca", 7)
    words = [(s, p) for s, p in conn.sql if "to_tsquery" in s][-1]
    assert "tsv @@ q" in words[0] and words[1][0] == "'chiglia' | 'barca'" and words[1][2] == 7


def test_postgres_without_the_extension_keeps_the_vectors_in_a_binary_column(monkeypatch):
    conn = pg()                                                                          # no pgvector row
    b = PostgresBackend({"host": "h", "dbname": "d"}, connect=lambda: conn)
    b.prepare()
    assert not b.native_vectors and b.native_words and b.info()["pgvector"] == "no"
    b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    sql = [s for s, _ in conn.sql]
    assert not any("halfvec" in s for s in sql) and any("INSERT INTO rag_vec_blob" in s for s in sql)


def test_a_failed_statement_in_a_change_rolls_it_back(monkeypatch):
    conn = pg(**{"extversion": [("0.8.0",)]})
    conn.fail = {"INSERT INTO rag_sections": RuntimeError("disk full")}
    b = PostgresBackend({"host": "h", "dbname": "d"}, connect=lambda: conn)
    b.prepare()
    with pytest.raises(RuntimeError):
        b.commit("a-1", meta_of(), CHUNKS, SECTIONS, VECTORS, "m", "v")
    assert [s for s, _ in conn.sql][-1] == "ROLLBACK"


# -- the router, the settings of a project's database and the vault -------------------------------------------------------------

def test_every_project_has_a_database_of_its_own_and_an_archive_is_found_in_it(tmp_path):
    r = Router(tmp_path)
    one, two = r.for_project(PROJ), r.for_project(project_key("q"))
    assert one is not two and (tmp_path / "db" / f"{PROJ}.sqlite").is_file()
    one.write_meta("a-1", {**meta_of(), "project": PROJ})
    two.write_meta("b-1", {**meta_of("b-1"), "project": project_key("q")})
    assert r.find("a-1") is one and r.find("b-1") is two and r.find("zz") is None
    assert [b for b in r.backends(PROJ)] == [one] and r.backends("0" * 12) == [] and len(r.backends()) == 2
    again = Router(tmp_path)
    assert again.find("b-1") is not None
    r.close()
    again.close()


def test_the_settings_of_a_database_hold_no_password_and_name_a_vault_entry(tmp_path):
    r = Router(tmp_path, password=lambda entry: "pw-of-" + entry)
    saved = r.set_config(PROJ, {"kind": "postgres", "host": "db.example", "dbname": "kb", "user": "me", "vault": "rag-db-x", "prefix": "kb_"})
    assert json.loads((tmp_path / "db" / f"{PROJ}.json").read_text()) == saved and "password" not in json.dumps(saved) and r.config(PROJ)["vault"] == "rag-db-x"
    for bad in ({"kind": "postgres", "dbname": "d", "password": "secret"}, {"kind": "oracle"}, {"kind": "sqlite"}, {"kind": "postgres"}, {"kind": "postgres", "dbname": "d", "prefix": "1; drop"},
                {"kind": "postgres", "dbname": "d", "vectors": "bits"}, {"kind": "other", "driver": "x"}):
        with pytest.raises(RagError):
            clean_config(bad)
    seen = []
    r2 = Router(tmp_path, password=lambda entry: seen.append(entry) or "x", open_fn=lambda cfg, pw: seen.append((cfg["kind"], pw)) or SqliteVecBackend(tmp_path / "o.sqlite"))
    r2.set_config(PROJ, {"kind": "postgres", "dbname": "d", "vault": "rag-db-x"})
    r2.for_project(PROJ)
    assert seen == ["rag-db-x", ("postgres", "x")]
    r2.close()


def test_the_password_goes_in_the_vault_and_is_read_from_it(tmp_path):
    entry = store_password(tmp_path / "vault", PROJ, "s3cret")
    assert entry == f"rag-db-{PROJ}" and vault_password(tmp_path / "vault")(entry) == "s3cret"
    assert "s3cret" not in "".join(p.read_text(errors="ignore") for p in (tmp_path / "vault").rglob("*") if p.is_file() and p.suffix in (".json", ".txt"))
    with pytest.raises(RagError, match="vault"):
        vault_password(tmp_path / "vault")("rag-db-nothing")


# -- the same archive through the old way and through the database ----------------------------------------------------------------

CORPUS = ["Il gatto dorme sul divano tutto il giorno", "La barca a vela ha una chiglia profonda e una randa", "Il risotto si fa con riso e brodo caldo",
          "Perché la chiglia tiene la barca dritta sul vento", "The keel of the yacht keeps it upright in the wind", "Cats sleep on the sofa most of the day",
          "Rice is cooked slowly with hot stock and butter", "La vela maggiore si chiama randa e il fiocco sta a prua", "Il brodo si prepara con le verdure e la carne",
          "A yacht needs a deep keel to sail against the wind"] + [f"Paragrafo numero {i} su argomenti diversi come cantieri, motori e porti {i}" for i in range(30)]


class Fake:
    model, version = "fake-1", "v1"

    def embed(self, texts):
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, t in enumerate(texts):
            low = t.lower()
            out[i] = [sum(w in low for w in ("barca", "vela", "chiglia", "yacht", "keel", "randa", "wind", "vento")), sum(w in low for w in ("riso", "rice", "brodo", "stock", "risotto")),
                      sum(w in low for w in ("gatto", "cats", "sofa", "divano")), 0.1]
        return out

    def embed_query(self, text):
        return self.embed([text])[0]


def corpus_store(tmp_path, name="rag"):
    s = RagStore(tmp_path / name, embedder=lambda: Fake())
    aid = s.create("Corpus", project=PROJ)["id"]
    s.add_bytes(aid, "corpus.txt", ("\n\n".join(CORPUS)).encode("utf-8"))
    return s, aid


QUESTIONS = ["come si chiama la vela maggiore", "chiglia della barca", "keel of a yacht", "risotto con brodo", "where do cats sleep", "motori e porti"]


class Forced(SqliteVecBackend):
    """A SQLite backend that answers vector searches as if the database did them, so that the application's path for them is tried without the extension."""
    native_vectors = property(lambda self: True)


def test_the_database_search_gives_the_same_best_passages_as_the_application_search(tmp_path, monkeypatch):
    s, aid = corpus_store(tmp_path)
    assert s.get(aid)["mode"] == "hybrid" and s.get(aid)["chunks"] >= 1
    wanted = {}
    monkeypatch.setattr(rag_query, "NATIVE_FROM", 10 ** 9)
    for q in QUESTIONS:
        wanted[q] = [(h["chunk"], h["text"][:30]) for h in s.search([aid], q, top_k=3)["hits"]]
    s.router.close()
    monkeypatch.setattr(rag_query, "NATIVE_FROM", 0)
    monkeypatch.setattr("tadamoo.rag.backends.sqlite_vec.SqliteVecBackend.native_vectors", property(lambda self: True))
    t = RagStore(tmp_path / "rag", embedder=lambda: Fake())
    assert t._backend(aid).native_words and t._backend(aid).native_vectors
    for q in QUESTIONS:
        got = [(h["chunk"], h["text"][:30]) for h in t.search([aid], q, top_k=3)["hits"]]
        assert got == wanted[q], q
    t.router.close()


# -- the old file format -----------------------------------------------------------------------------------------------------

def make_legacy(root: Path, aid="old-abc123", project=PROJ):
    folder = root / aid
    (folder / "blocks").mkdir(parents=True)
    (folder / "images" / "d1").mkdir(parents=True)
    (folder / "figures").mkdir()
    (folder / "versions" / "d1").mkdir(parents=True)
    chunks = [{"doc": "d1", "n": i, "text": t, "path": "Doc", "page_start": 1, "page_end": 1, "kind": "text", "section": "d1.s0"} for i, t in enumerate(CORPUS[:6])]
    meta = {**meta_of(aid, chunks=6, dim=4, model="fake-1"), "embed_version": "v1", "project": project, "documents": [{"id": "d1", "name": "x.txt", "sha256": "abc", "chunks": 6, "bytes": 10, "info": {}}]}
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "chunks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in chunks) + "{half a line", encoding="utf-8")
    (folder / "sections.jsonl").write_text(json.dumps({**SECTIONS["d1.s0"]}) + "\n", encoding="utf-8")
    np.save(folder / "embeddings.npy", np.array([Fake().embed([c["text"]])[0] for c in chunks], dtype=np.float16))
    (folder / "blocks" / "d1.json.gz").write_bytes(gzip.compress(b"[]"))
    (folder / "images" / "d1" / "1.webp").write_bytes(b"IMG")
    (folder / "images" / "d1" / "1t.webp").write_bytes(b"THUMB")
    (folder / "figures" / "d1.json").write_text(json.dumps([{"n": 1, "page": 2}]), encoding="utf-8")
    (folder / "enrich.jsonl").write_text(json.dumps({"k": "e1", "kind": "p", "summary": "s", "questions": ["q?"]}) + "\n", encoding="utf-8")
    (folder / "sources.json").write_text(json.dumps({"version": 1, "sources": [{"id": "s1"}], "links": {}}), encoding="utf-8")
    (folder / "versions" / "d1" / "1.json.gz").write_bytes(gzip.compress(b"[1]"))
    np.save(folder / "embeddings_rt.npy", np.ones((6, 3), dtype=np.float16))
    (folder / "rt.json").write_text(json.dumps({"model": "small", "version": "q3e-1", "dim": 3, "passages": 6, "fingerprint": "ff", "built": 1}), encoding="utf-8")
    return folder


def tree_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        h.update(str(p.relative_to(folder)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_an_archive_in_the_old_format_is_copied_whole_into_its_projects_database_and_the_folder_is_left_alone(tmp_path):
    root = tmp_path / "rag"
    folder = make_legacy(root)
    before = tree_hash(folder)
    s = RagStore(root, embedder=lambda: Fake())
    assert [p.name for p in s.legacy()] == ["old-abc123"] and s.list() == []
    report = s.import_archive(folder)
    assert report["chunks"] == 6 and report["documents"] == 1 and report["sections"] == 1 and report["vectors"] == 6 and report["blocks"] == 1 and report["images"] == 2
    assert report["figures"] == 1 and report["enrichment"] == 1 and report["versions"] == 1 and report["second_vectors"] == 6
    assert tree_hash(folder) == before and s.legacy() == []
    b = s._backend("old-abc123")
    assert [c["text"] for c in b.read_chunks("old-abc123")] == CORPUS[:6]                       # the half line of a cut-off write cost nothing
    assert b.get_blob("old-abc123", "image", "d1/1.webp") == b"IMG" and b.read_figures("old-abc123", "d1") == [{"n": 1, "page": 2}] and b.enrich_read("old-abc123")["e1"]["summary"] == "s"
    assert b.read_sources("old-abc123")["sources"] == [{"id": "s1"}] and b.get_blob("old-abc123", "version", "d1/1") == (folder / "versions" / "d1" / "1.json.gz").read_bytes()
    assert b.vector_note("old-abc123", RT)["model"] == "small" and b.vector_note("old-abc123", RT)["note"]["fingerprint"] == "ff"
    assert s.get("old-abc123")["mode"] == "hybrid" and s.search(["old-abc123"], "keel of the yacht")["hits"][0]["text"].startswith("The keel")
    with pytest.raises(RagError, match="already in the database"):
        s.import_archive(folder)
    assert s.import_archive(folder, replace=True)["chunks"] == 6 and tree_hash(folder) == before
    s.router.close()


def test_an_import_that_stops_half_way_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "rag"
    folder = make_legacy(root)
    s = RagStore(root)
    backend = s.router.for_project(PROJ)
    monkeypatch.setattr(backend, "write_figures", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        s.import_archive(folder)
    assert backend.archive_ids() == [] and backend.one("SELECT COUNT(*) FROM rag_blobs")[0] == 0 and folder.is_dir()
    s.router.close()


def test_an_old_archive_with_vectors_that_do_not_fit_is_copied_without_them_and_says_so(tmp_path):
    root = tmp_path / "rag"
    folder = make_legacy(root)
    np.save(folder / "embeddings.npy", np.ones((2, 4), dtype=np.float16))
    s = RagStore(root)
    report = s.import_archive(folder)
    assert report["vectors"] == 0 and "index the archive again" in report["notes"][0] and s._backend("old-abc123").vector_note("old-abc123") is None
    s.router.close()


def test_the_command_line_shows_the_state_and_imports(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    from tadamoo.rag import __main__ as cli
    root = tmp_path / "rag"
    make_legacy(root)
    base = ["--rag-dir", str(root)]
    assert cli.main(base + ["db", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting"] == ["old-abc123"]
    assert cli.main(base + ["db", "import", "--all"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"][0]["chunks"] == 6
    assert cli.main(base + ["db", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["waiting"] == [] and out["projects"][0]["archives"] == 1 and out["projects"][0]["kind"] == "sqlite"
    assert cli.main(base + ["db", "import", "nothing-here"]) == 1
    assert "meta.json" in capsys.readouterr().err
