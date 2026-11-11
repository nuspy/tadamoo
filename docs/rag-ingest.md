# Reading and cutting documents (knowledge archives)

How a file becomes the passages of a knowledge archive (`docs/rag.md`), and how the model reads a document without adding it to one. All the code is
in `tadamoo/rag/ingest/`.

## The steps

1. **Read** the file into *blocks* (`docling_client.py` and the worker, or `basic.py`).
2. **Clean** the blocks (`normalize.py`).
3. **Cut** them into passages and sections (`chunker.py`).
4. **Look** at each passage for text that addresses an assistant (`tadamoo/inject_screen.py`).
5. **Index**: the archive keeps the passages, the sections and the blocks as read (`tadamoo/rag/store.py`).

The blocks as read are saved before step 2 (`blocks/<doc>.json.gz`), so a change of the cleaning or the cutting is applied again from them by
"Index again", without the original file and without Docling. `meta.json` keeps the versions in force (`ingest: {normalizer, chunker}`); when they
differ from the code's, the archive says `rechunk` and the next index brings it up to date.

## Blocks

A block is `{type, text, page, level?}`. Types: `heading` (with `level`, 1 is the top), `paragraph`, `list` (lines that start with `- `), `table`
(as Markdown), `code`, `caption`, `footnote`, and `page_break` (no text: the next blocks are on page `page`). `page` counts from 1, and is 0 for a
format that has no pages (text, HTML, Word without page breaks): such a passage is cited without a page. Markdown with `<!-- page -->` markers
reads into blocks and writes back from them (`blocks.py`), which is how a document is given again for any range of pages.

## The readers

**Docling** (`docling_worker.py`) is run by the Python of a Docling environment, as a subprocess that prints JSON lines: `start` (format, pages,
device), `progress` (page, pages), `blocks` and `done`, or `error`. It reads PDFs (scanned ones with OCR, and tables), Word, Excel (every sheet a
table), PowerPoint, HTML, Markdown and images. A PDF of more than 40 pages is converted in windows of 40, so the memory of a window is given back
before the next. The Python is the setting `docling_python`; when it is empty the first environment found is used: `docling-gpu`, then `docling`,
under `$TADAMOO_VENVS` and `E:/Dev/venvs`. `TADAMOO_DOCLING=off` says there is none (the tests set it). Nothing is installed from here.

The device is asked for first: the worker takes a lease of 3000 MiB from the server (`tadamoo/vram_lease.py`, `docs/vram-broker.md`) and
runs on the GPU when `.gpu_ok`, else on the CPU; the lease is released when the worker ends. The setting `docling_device` = `cpu` never asks. On the
CPU a page takes 4 to 14 seconds, on the GPU under a second.

The client (`docling_client.py`) sends progress to the job, ends the worker when the job is cancelled (it closes the worker's stdin; the worker
exits on end of input), kills it when it says nothing for 15 minutes or runs for 6 hours, and treats a worker that ends before `done`, or says
`error`, as a failure: the blocks it printed are never used. When Docling fails on a format the basic readers have, those read the file and the
document says why in its note; for `.xlsx`, `.pptx` and images the failure is the answer.

**Basic** (`basic.py`) needs nothing installed: text and Markdown (headings from `#`), HTML and EPUB, Word (headings from its styles), and PDF
with a text layer by pypdf or PyMuPDF, page by page. A one-line paragraph that starts with a number (`2.1 Method`) or is in capitals is taken for a
heading, unless that would make more than 30% of the blocks headings. The record of the document says `extraction: "basic"`, and the tab says that
scanned PDFs, images, Excel and PowerPoint need Docling.

## Cleaning

`normalize.py`, pure functions, `VERSION` 1. In order: control and zero-width characters out, odd spaces made plain, ligatures (`fi`) and
full-width Latin letters and digits made plain, accents composed (NFC); a word broken by a hyphen at the end of a line is joined when the next line
goes on in lower case (`reprogram-` / `ming`; not `Non-` / `Hodgkin`, not `1990-` / `2000`); lines wrapped by the page become one line (list items,
table rows, quotes and indented lines start a new one); lines of running text that are mostly noise (fewer than 45% letters and digits, or a run
of single letters and marks, as OCR makes of a logo) are dropped; spaces collapse.

Not touched: CJK text and punctuation, half-width kana, and accents (Unicode NFKC would change them). Tables lose the padding of their cells and keep
their words; code keeps every space.

Page numbers (`12`, `Page 3 of 10`, `xiv`) at the top or bottom of a page are dropped. A header or footer is a block, among the first or last two
of a page, whose text with its numbers and case taken out is the same on at least 3 pages and a quarter of the pages that have text; it is dropped
everywhere. A page with fewer than 4 blocks has none.

## Cutting

`chunker.py`, `VERSION` 1. The headings make a tree of sections; a passage is inside one section, and its `path` is `Document > Section >
Subsection`. A token is counted as 3.5 characters.

| Kind of text | Becomes |
|---|---|
| paragraphs and captions | gathered into passages of about 350 tokens; small neighbours are merged, a passage under 120 tokens takes the next paragraph up to 600 |
| a paragraph over 600 tokens | cut at sentence ends (Latin and CJK) into pieces of about 350; each piece after the first starts with the last sentence of the one before when that is short (this is the only overlap) |
| a sentence that cannot be cut | cut by size, at a space |
| a list | whole inside a passage; over 600 tokens it is cut by items |
| a table | one passage; over 600 tokens it is cut by rows, each piece starting with the header row and its separator |
| code | one passage; over 600 tokens it is cut by lines |

`split_prose(text, valleys=...)` is the seam for a splitter that knows where the meaning changes: it may return the sentences that start a new
topic, and the cuts are made there. Without it the cuts are by size. A passage carries `path`, `page_start`, `page_end`, `kind` (`text`, `list`,
`table`, `code`), `section` (the id of its section, `<doc>.s<n>`) and, when flagged, `flag`.

A section keeps its own text (8000 characters at most) in `sections.jsonl` with its parent, path and pages. A hit gives its `section`; the section
is read with `GET /api/rag/archives/<id>/sections/<section>` or `knowledge_search(expand: true)` (small to big).

## Passages that address an assistant

A paragraph that matches the phrases of `inject_screen.detect` ("ignore previous instructions", "if you are an AI", "new instructions:" and the like,
in English and Italian) is flagged, and is never merged with one that is not, so the rest of the text is not lost. The document counts them (`flagged`),
the tab shows the count, and the recall shows a flagged passage with a line saying that it is data (or leaves it out: setting `flagged` = `drop`).
Nothing here asks a model.

## Adding a big file

`POST /api/rag/archives/<id>/upload` and `/documents` return a job (`serve/rag_jobs.py`): a thread that reads, cleans, cuts and indexes, reports `stage`
(`reading`, `cutting`, `indexing`) and the page, and can be cancelled. The archive is changed in one step at the end, under its lock; a file that
fails, is refused or is cancelled leaves nothing behind (not even its blocks). The page polls the job and shows one line.

## `read_document`

A tool for the model, in every role, read-only (`PURE`) and `untrusted` (`reading.py`). `read_document(path, pages="1-20", format="markdown")`
takes a path relative to the working folder or absolute (never one of the person's secrets), reads the file through the same readers, and returns
the Markdown of the pages asked for with `<!-- page N -->` markers and `pages_total`; `format: "text"` gives the plain text. A page range is `1-20`,
`5`, `30-` or `-4`. One answer holds 60 000 characters at most, and says which range to ask for next. The reading of a file is kept in
`<rag folder>/.reading/` keyed by its SHA-256, the reader and the versions, so asking for other pages of a big file does not read it again; a
changed file or a new reader is read again. The description of the tool tells the model to use it for scanned PDFs, images of text, Word, Excel and
PowerPoint files and for "read / OCR this document" in any language, and the self-description says so too.

## Tests

`tests/test_rag_ingest.py` (cleaning, blocks, cutting with a real excerpt of Docling's Markdown of a patent in `tests/data/`, flags, citations,
versions), `tests/test_rag_docling.py` (the worker with stand-in workers, the fall-back, the job, `read_document`, the state note) and one check of
the real worker on a two-page PDF with the CPU environment, skipped when it is not there.
