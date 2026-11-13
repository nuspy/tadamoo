# The database of a project's archives

Every project keeps its knowledge archives in **one database**: the passages, their words, their vectors, the documents' readings, the figures and what a model wrote about
them. Nothing else on the disk belongs to an archive. The code is in `tadamoo/rag/backends/`; everything that reads or writes an archive (indexing, the search with its
reranker, enrichment, figures, the refresh of sources, coverage, the resolver, the voice functions, the plan step) goes through its one interface, `Backend`.

## Which database

| Kind | What it is | Words | Vectors |
|---|---|---|---|
| SQLite file (the default) | `rag/db/<project>.sqlite`, made when the project first needs it | FTS5 | the `sqlite-vec` extension, float32, cosine |
| PostgreSQL | any server from 12 (`halfvec` needs pgvector 0.7) | `tsvector` column with a GIN index | pgvector `halfvec(dims)` with an HNSW index |
| MariaDB, MySQL, another | any DB-API driver | the application | a binary column, searched by the application |

The **fallback** is the same everywhere: the vectors are a binary column (float32, 256 rows to a value), a search loads the archive's vectors and multiplies, exactly as the
old `embeddings.npy` did. It is what any database gets that has no vector extension: MariaDB, MySQL, a PostgreSQL without pgvector, a SQLite where `sqlite-vec` is not
installed or cannot be loaded. Binary or bit quantization is not used anywhere.

A SQLite file keeps the vectors in the binary column **and** in a `vec0` table when the extension is there, and a PostgreSQL with pgvector keeps them in the binary column
and in a `halfvec` table: the binary column is the truth, the index is built from it, rebuilt when it is missing (a file moved to a machine without the extension
still reads, and gets its index where the extension is). The type of a vector column is `halfvec(dims)`; the HNSW index is made on that column (up to 4000 dimensions, which
covers the 2560 of the 4B model; above that the search has no index) and the query casts with the same `::halfvec`, so the index is used. The dimensions of each archive are
recorded in the `vectors` table beside the model and its version.

## The settings of a project's database

`rag/db/<project>.json`, written by *Knowledge > Database* or `python -m tadamoo.rag db config`:

```json
{"kind": "postgres", "host": "db.example", "port": 5432, "dbname": "kb", "user": "me", "sslmode": "require", "vault": "rag-db-<project>", "prefix": "rag_", "vectors": "auto"}
```

`kind` is `sqlite` (`path`), `postgres`, `mysql` or `other` (`driver`, the Python module, `dsn`, with `{password}` where the password goes, and `dialect`). **The password is
never in this file or in any other**: the page sends it once, it goes to the vault of this PC (entry `rag-db-<project>`, field `value`), and the settings name only the entry.
`vectors` is `auto` (use pgvector when the database has it), `blob` (never) or `file` (Plan B below). A project with no file has its SQLite file.

## The schema

Created when the database is first prepared, inside a new database or inside one that already has other tables: every table has the prefix (`rag_`, settable) and no other
table is read or changed. A `schema` table holds the version; `prepare` brings a database up to the version in force by running the migrations above its own, each in a
transaction. The tables are `archives`, `documents`, `chunks` (with `facts`, the passage's tags, as JSON), `sections`, `vectors`, `vec_blob`, `figures`, `blobs` (readings,
images, kept versions), `sources` and `enrich`, and the search structures (`fts`, the `rag_v_*` vector tables). A change of an archive is one transaction. Each archive has a
revision that moves with every change: it is what lets a reader keep what it loaded until the archive changes and what the Knowledge tab's flash looks at.

## Connect and prepare

*Knowledge > Database > Connect and prepare* (or `python -m tadamoo.rag db prepare --project KEY`) runs three steps as a job, each reporting what it found:

1. **Connect.** Reach the database (a SQLite file is created; a missing local PostgreSQL database is created when asked and the user may), and show the server version, the
   user and whether the user may create tables. A user who may not stops here, with that said. A missing driver is named as a component to install.
2. **Vector extension.** PostgreSQL: present, or *available on the server but not enabled* (then `CREATE EXTENSION vector` is run; a user who may not is given the exact
   statement to ask an administrator for), or *not available* (then the steps for the system are shown, `apt` or `dnf` for Linux, `brew` for macOS, the build with `nmake` for Windows (it needs the Visual Studio C++ build tools; the steps shown are the exact commands), and TaDaMoo runs the package command itself only on this PC, with administrator rights, when the person asks for it). An older pgvector is updated or refused.
   If it cannot be had, **Plan B**, the person's choice: the vectors in a binary column of the same PostgreSQL searched by the application, or in a local sqlite-vec file (the
   rest stays in PostgreSQL; the two are written one after the other, not in one transaction). SQLite: the extension is loaded, installed but not loadable, or not installed.
3. **Tables and test.** The tables and indexes (GIN for words; HNSW is made with each archive's vectors), a test archive written, searched by words and by vector and removed, and
   the settings saved. Only a database that passed all three is saved.

The result of each step stays in the job (`data.steps`) and on the page. A step that needs a choice or a fix stops the job; connect again after doing it.

## Importing the old file format

Archives made before the database are folders `rag/<id>/` (`meta.json`, `chunks.jsonl`, `sections.jsonl`, `embeddings.npy`, `blocks/`, `images/`, `figures/`, `enrich.jsonl`,
`sources.json`, `versions/`, `embeddings_rt.npy` with `rt.json`). They are kept only as a source for the import, which copies **all of it** into the database of the archive's
project and reports the counts: passages, sections, documents, vectors with their model and size, readings, images, figures, enrichment entries, kept versions, second
vectors. It never changes or deletes the folder (delete it yourself when you have checked), refuses an archive that is already in the database (`replace` copies it again), leaves
nothing behind when it stops half way, and checks the stored counts against the folder's.

```
python -m tadamoo.rag db status                      # the database of each project, what is in it, the folders waiting
python -m tadamoo.rag db import old-archive-1a2b3c   # one archive (or --all, or --source DIR, or --replace)
```

In the page, *Knowledge > Database* shows how many archives of the project are waiting and an *Import into this database* button.

## Packages the databases need

`sqlite-vec` (pinned `0.1.6`), the PostgreSQL driver (`psycopg[binary]` `3.3.4`) and the MariaDB/MySQL driver (`pymysql` `1.1.1`) are components: the page lists each with its pinned
requirement, its size and whether it is installed here, and installs one with `pip install` into the Python the app runs in **only when its button is pressed**
(`python -m tadamoo.rag db components install ID` does the same). Nothing downloads on its own. After an install the databases are opened again so that the new package is used.

## HTTP

All for this PC, under the Knowledge routes of `docs/rag.md`; long things are jobs read at `GET /api/rag/jobs/<id>` (`stage`, `data`, `result`).

```
GET  /api/rag/db?project=KEY              kind and settings (never a password), what it is now, archives waiting, drivers present
POST /api/rag/db/config                   {project, config, password?}  or  {project, reset: true}
POST /api/rag/db/prepare                  {project, config?, password?, plan_b?, vec_file?, install_local?, create_db?}   a job
POST /api/rag/db/import                   {archives?, replace?}   a job
GET  /api/rag/db/components               the packages, their pinned requirement and size
POST /api/rag/db/components/install       {id}   a job
```

## Search

`query.run` is unchanged in what it computes: a passage scores its cosine plus a small share of its word score, the best twenty are reranked. From
`NATIVE_FROM` (1000) passages an archive's database does the two searches itself (`word_search`, `vector_search`) and the application only blends and reranks; below that the
application does them in memory, which gives the same answer without a round trip. The words are made plain the same way in both (accents dropped, one-letter words left
out). FTS5's BM25 and PostgreSQL's `ts_rank` are not the application's BM25: the order of the best few agrees on the test corpora, and a search that needs the word floor
(`min_share`) uses the application's own index. Passages carry their tags in `facts`; filters (`language`, `number`, `year`...) are applied to what the database returns.

## Code and tests

`backends/base.py` (the interface), `sql.py` (the schema, every read and write, the exact fallback), `sqlite_vec.py`, `postgres_pgvector.py`, `split.py` (Plan B with a vector file),
`prepare.py` (the three steps), `legacy.py` (the import), `components.py`, and `__init__.py` (`open_backend`, the `Router` of projects, the vault helpers). Tests:
`tests/test_rag_backends.py` (every operation on a real SQLite file, the database search against the application's, the other-tables and version cases, the router, the import),
`tests/test_rag_database_prepare.py` (each state of the vector extension with a stand-in PostgreSQL connection), `serve/tests/test_rag_db_api.py`. The real `sqlite-vec` and a real
PostgreSQL with pgvector are tried only where installed; the PostgreSQL SQL itself is checked as text against a stand-in connection.
