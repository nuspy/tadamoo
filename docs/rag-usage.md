# Getting the archives used

An archive that the model does not use is worth nothing. This page is about what the harness does so that a conversation with an archive attached
searches it for facts instead of reading the same documents page by page, and what was measured. Storage and search are in [rag.md](rag.md) and
[rag-embeddings.md](rag-embeddings.md).

## What went wrong, from the logs

A comparison of two agent sessions on seven patent PDFs (one with the archive of the same seven files attached, one without) ended with the same
answer quality and almost no use of the archive: one `knowledge_search` in the whole session, the query "ER100", every page read with
`read_document`. The log of the session with the archive showed five causes:

1. **The model was never told what the archive held.** The `[state]` note said `ER100 patents (7 documents, text search; Seven ER100 related patent
   documents)`. The model's own reasoning on its first step: the archive "has 7 documents - matches the 7 PDFs", and it went on to read the PDFs.
2. **The note said `text search` for a semantic archive.** The agent's process has no embedder of its own and asks the server to search, but
   `RagStore.status` looked only at the local embedder. A process with a `remote` now reports `hybrid` for an archive that holds vectors.
3. **The tool description said to use `knowledge_search` when the person asks to look something up, or when the passages added to the request do not
   cover it.** Nothing said that it is the fast way to find a specific fact, and the request itself ("usa l'OCR per leggerli") sent the model to
   `read_document`.
4. **The passages put in front of the request were chosen by one query, the whole 650-character request.** A long request is about its subject in
   general, so the closest passages were the general ones (four from the shortest document); they were also far from what the answer needed.
5. **The one search was a bad one and taught distrust.** The model looked for the name "ER100", which is the archive's label and appears in no
   document; two unrelated passages came back, and its next thought was that the search finds nothing.

The search itself was good: asked the fact in its own words, it returns the right page first (the facts the answers missed, in the experiments of the
361-page patent, are at the top of a search for them).

## What the model is told

| Where | What |
|---|---|
| Fixed block (`awareness.self_model`) | An archive holds its documents already read and indexed: to find a specific fact, number, name or claim, when the page is unknown, or across several documents, use `knowledge_search` (one search for each point), then `knowledge_read` for the pages around it; the passages added to a request are a first sample; read a known page directly. |
| `[state]` note, `knowledge=` (`tadamoo/rag/card.py`) | Each archive with its documents: name, pages, language (`FR2941464A1.pdf 20 p. fr`), how it is searched (by meaning and by words, or by words only). File names are cut to plain characters and 40 long: they are not trusted text. Told again only when an archive or the attached list changes, as a note, never in the prefix. |
| `knowledge_search` description | When to use it (before reading files page by page; specific facts; page unknown; several documents), one search for each point, worded as the document would word it, `queries` for several in one call. |
| Result of a file read (`tadamoo/rag/nudge.py`) | When `read_document` or `read_file` opens a file whose name is a document of an archive in use: once at the first read ("this file is also in the archive X, already read and indexed: `knowledge_search` finds the page, `knowledge_read` gives pages with no OCR wait") and once after four reads of it; at most six hints in a conversation. |

## The tools

- `knowledge_search(query | queries, archive?, document?, limit?, expand?)`. `queries` takes up to six searches in one call; the results are
  interleaved by rank (the best of each query first), each passage names the query that found it (`for`), and a passage found twice is given once.
  `document` limits the search to one document (matched like an archive name; ambiguity returns the candidates). `limit` is passages for each query
  (8, or 5 with several queries). A passage is `{cite, archive, document, page, section, score, text}` plus `flagged: true` when it reads as
  instructions to an assistant (its text then starts with the usual warning). The answer carries `mode` (`hybrid` or `lexical`) and, when the search
  was degraded (no reranker, the embedder failed), the note. At most 12000 characters.
- `knowledge_read(document, pages?, archive?)`: the Markdown of the pages from the reading saved when the document was added (the `blocks` of the archive's database),
  with `<!-- page N -->` markers, at most 20000 characters; no OCR and no wait, whether or not the original file still exists. Without `pages` it gives
  the **outline**: the sections of the document with their pages (repeated headings merged into one entry), at most 80. The same cloud rule as search
  applies: for a request that leaves this PC only archives with `cloud_ok` are read.
- `knowledge_list` adds, for the archives in use, the documents with pages and language.

Both are untrusted tools: their results are fenced as data. A flagged passage in a read page is marked in place (or left out, as the `flagged`
setting says).

## The passages put in front of a long request

`ProjectRecall.message` (`tadamoo/rag/recall.py`) asks one query for a request shorter than `request_chars` (200), as before. A longer one is asked as
**questions** (`tadamoo/rag/expand.py`): the technical session writes at most `queries` (6) short searches, one for each distinct thing the request asks
about, in the language of the documents (it is shown their names, pages and languages; the use is `recall_queries`, off with the other uses in the
technical-session dialog, and never sent to another provider). Without a session, or when it fails, the request is cut at its commas outside
brackets, colons, semicolons and sentence ends, keeping clauses of three words or more; up to twice the cap are tried, until `queries` of them found
something. Clauses cut by code are only trusted when the reranker judged them (a stray phrase such as "cite the patent and the page" has a near
passage too): without a reranker they give nothing, and the request is asked as it always was. The whole request, cut to 600 characters, is asked last.

Each question keeps its best three passages; a question taken from the request must reach a rerank score of 0.35 (a question a model wrote: or a cosine of
0.5 when there was no reranker) so that a stray phrase of the request does not bring in its nearest passage; then the block is filled one rank at a time across the
questions, at most three passages of one document, at most eight passages and 9000 characters, each passage once in the conversation. The closing
line names the questions and says the block is a first sample. The questions after the first are asked for at most 15 seconds in all (the first
may wait for the embedder to load). The injection flags, the cloud rule and the fence are unchanged. A conversation with no archive makes no call.

Whether a long request should also be planned into sub-questions by the harness first: no. The model plans its own work (its checklist), and the
searches it needs are cheap; what was missing was telling it that the archive answers them. A planner stage would be a second system next to the
Planner and the skills.

## What was measured

`tools/rag_usage_check.py` replays a request against the retrieval and says which facts (with their true pages) reach the model through each way in;
no language model is called. The ER100 case, 14 facts in the experimental and claims parts of the 361-page US patent, the reranker off
(the GPU had no room to lend), a fact counted when a passage of its document starts on one of its pages:

| Way in | Facts reached |
|---|---|
| Turn start, the whole request as one query (before) | 0 of 14 (4 passages, all general) |
| Turn start, the request split by code | 0 of 14: no reranker, so no clause is trusted and the block is empty |
| Turn start, six questions as a technical session would write them | 12 of 14 |
| `knowledge_search`, five searches on the aspects | 13 of 14 |
| `knowledge_search`, a search worded on each missed fact | 14 of 14 |

The three facts that no answer contained: the optic-nerve claim is reached by the turn-start questions and by any search for the claims; the Tet1/Tet2
knockdown (pp. 223-225) only by a search that names it; the axon tracing (p. 230) by a search on efficacy and by one that names it. What cannot be
measured offline is whether the model makes those searches: that is the live test.
