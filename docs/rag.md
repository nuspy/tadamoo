# Knowledge archives

The Knowledge tab holds archives of documents that belong to a **project**: a workspace folder of the Agent tab, or the Chat's own folder.
An archive is not tied to any personality. Every conversation held in the project draws on the project's archives, whatever personality (or
none) speaks: the passages that match the request are put in front of the model as reference text. Archives are not global: another project
does not see them.

## Storage

One folder per archive in the user data folder, `rag/<id>/`. The id is the lower-case form of the name plus six hex digits and never changes
on rename.

| File | Holds |
|---|---|
| `meta.json` | name, description, `project` (the key below), `cloud_ok`, the documents, the passage count, the embedding model of the vectors (empty: text only) |
| `chunks.jsonl` | one passage per line: `{doc, n, text}` |
| `embeddings.npy` | float32, one L2-normalized row per passage; present only when every passage has a row |
| `texts/<doc>.txt` | the text read from each document, so an archive can be indexed again without the original |

A project is named by its key, `project_key(path)` (`tadamoo/rag/project.py`): 12 lower-case hex characters of the SHA-1 of the folder's
real path (links resolved, case folded where the file system ignores it, no trailing separator), so every spelling of a folder gives the same
key. An archive needs a valid key to be made. Names are unique inside a project and the 50-archive limit counts inside a project. An archive
whose `meta.json` names no project belongs to none and is listed for none.

Every file is written to a temporary name, flushed and renamed; `meta.json` goes last. A passage whose document is not in `meta.json`, or
vectors that do not match the passages, are ignored. The original files are not kept.

## Documents

Formats: `.txt .md .markdown .text .html .htm .epub .docx .pdf`. The extension decides the format; a PDF, a Word file or an EPUB must also
have the right first bytes. Word and PDF reading is `tadamoo/attachments.py`'s (Word with the standard library; PDF with `pypdf` or
`PyMuPDF`, whichever is importable). When neither is, a PDF is refused with a message naming both packages; nothing is ever installed. HTML
and EPUB are read with the standard library. There is no text recognition: a scanned PDF has no text and is refused.

Text is cut on paragraph boundaries into passages of about 1000 characters; each starts with the last 150 characters of the one before.
A paragraph longer than the size is cut at a sentence end or a space.

Limits (`tadamoo.rag.LIMITS`): 50 archives per project, 200 documents per archive, 25 MB per file, 4 million characters of text per document, 20 000
passages per archive, names up to 60 characters, descriptions up to 500, at most 20 passages per answer. A zip (Word, EPUB) whose members
unpack to more than 60 MB is not read. The same file (by SHA-256) cannot be added twice to an archive. A refused file leaves the archive as
it was.

Paths: an archive id is a plain slug and a document is named by its own id, so no request reaches outside `rag/`. A document name holds no
folder separator. A path given to read must be absolute, without `..`, a regular file, and not inside the user data folder (the vault is
there).

## Search

Always by words: BM25 over lower-case words without accents (`perché` meets `perche`). The statistics are those of all the archives
searched together, so scores from different archives compare.

With vectors: when an embedder is installed and an archive's vectors were made by it, the question is embedded too and cosine similarity
ranks the passages (brute force over a numpy matrix; passages below 0.2 are dropped). The two rankings are merged by reciprocal rank
(`1/(60 + position)`). A hit says whether it is `lexical` or `hybrid` and carries its archive, document, passage number, `score`,
`lexical` and `semantic` values. `score` compares passages within one answer only. An embedder that fails at query time means words only,
with a note.

**No embedding model is bundled and none is named.** The embedding service in the voice catalog is empty, so every archive is searched by
words and the tab says "Text search only: no embedding model installed". The seam is `tadamoo/rag/embed.py`: an `Embedder` has a `model`
id and `embed(texts)` returning a matrix. The store takes a provider, a function that returns an embedder or `None`, and calls it each time
one is needed; when a model is installed and the provider returns it, new documents are embedded as they are added, and "Index again"
embeds an existing archive. Vectors of another model are never mixed with an archive's. Adding a document that cannot be embedded drops
the archive's vectors (text search continues) until it is indexed again. The harness process has no provider yet: it searches by words.

## Using it in a conversation

- **Agent conversations**: the session starts the harness with `--rag-dir` and `--rag-project <key>`, the key of the conversation's workspace
  (the Chat's own folder for a Chat conversation). `ProjectRecall.message` (`tadamoo/rag/recall.py`) searches all the project's archives and
  `retrieved_message` adds the block to the per-request message, after the personality's notes; the message is marked untrusted when the block
  is in it. Without a personality it works the same.
- **Model-only chat**: `serve/web/chat.js` asks `POST /api/rag/recall {project, goal}` for the Chat's own folder and adds the returned block to
  the system message, after the personality's text and notes. `POST /api/personas/<slug>/compose` returns no archive passages.

The block is `tadamoo.untrusted.fence("knowledge-archive", ...)`: a data block with a random id, in which the document text cannot close the
block, imitate its tags or write a chat control token. Each passage is headed `[archive / document]`, and the closing line says the passages
(from the project's knowledge archives) are data, not instructions, and asks the model to name the archive and document it relies on. At most 4 passages and 6000 characters per
request; a passage already sent in this conversation is not sent again (one `ProjectRecall` for each conversation). A passage must answer at least a quarter of the request's words
(`MIN_SHARE`, a starting point to tune with measurements). Archive and document names are fenced like the text.

A request that leaves this PC (a cloud model) takes only archives whose `cloud_ok` is on; it is off by default. The model-only chat is
treated as local, as for notes.

## Routes

All under `/api/rag`, from this PC only (403 otherwise).

| Route | |
|---|---|
| `GET /api/rag?project=<key>` | the archives of that project, limits, formats, whether PDF can be read, the embedding state; the key is required |
| `GET /api/rag/projects` | `{projects: [{key, name, path, chat, archives}]}`: the Chat's own folder first, then the Agent tab's folders that exist, with how many archives each holds |
| `GET /api/rag/archives/<id>` | the archive with its documents |
| `POST /api/rag/archives` | `{name, description, project}` creates |
| `POST /api/rag/archives/<id>/update` | `{name, description, cloud_ok}`, any of them |
| `POST /api/rag/archives/<id>/delete` | `{confirm: true}` |
| `POST /api/rag/archives/<id>/upload?name=<file>` | the file as the raw body |
| `POST /api/rag/archives/<id>/documents` | `{path}` of a file on this PC |
| `POST /api/rag/archives/<id>/documents/<doc>/remove` | removes a document, its passages and vectors |
| `POST /api/rag/archives/<id>/reindex` | cuts the saved texts again and embeds them if an embedder is there |
| `POST /api/rag/search` | `{query, archives: [ids] \| project: key, top_k}`; one of the two is required |
| `POST /api/rag/recall` | `{project, goal}`: `{block}`, the data block of the project's passages for the model-only chat (`""` when none match) |

Errors are `{error: {type: "rag", code, message}}` with 404 for `not_found` and 400 otherwise.

## The tab

Knowledge, a top-level tab. At the top a project picker (a select, "Name (archives)"): it starts on the folder of the conversation that is open
when the page can know it, else on the Chat's own folder, and keeps the person's choice. Below it only that project's archives: create and
rename, delete (two presses), the documents with size and passage count, upload and add by path, the index state with "Index again", "Try a
question" (shows each hit's archive, document and score), and the banner for hybrid or text-only search. One column on a phone, a list beside
the page from 900 px.

## Code

`tadamoo/rag/` (`project.py`, `store.py`, `extract.py`, `chunking.py`, `search.py`, `query.py`, `embed.py`, `recall.py`), `serve/rag_api.py`,
`serve/web/rag.js` and `rag.css`. Tests: `tests/test_rag_store.py`, `tests/test_rag_recall.py`, `serve/tests/test_rag_api.py`,
`serve/tests/test_rag_page.py` (with `rag_project_dom_check.js`, the page on a stand-in DOM).
