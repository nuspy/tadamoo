# Figures of documents

A chart, a diagram, a map of a plasmid, a photo or a whole sheet of drawings is not text, and a reader that gives text only loses it: in a patent the figure sheets
hold the labels (gene names, axes, legends) that the description may not repeat. The archive keeps each figure of a PDF as an **image** and as a **passage of its own**
(kind `figure`), so a search finds it by what it says, and the agent can look at it, show it in the chat and put it in a Word document, with its citation.

Code: `tadamoo/rag/ingest/figures.py` (finding, cutting, labelling), `ocr_worker.py` (the words inside an image), the picture events of `docling_worker.py` and
`docling_client.py`, the store (`figure_set`, `figure_rows`, `install_figures`, `figure`), `tadamoo/rag/lookup.py` (`figure`), `tadamoo/rag/enrich.py` (the model's description),
`tadamoo/tools/docx_edit.py` (`insert_picture`). Tests: `tests/test_rag_figures.py`, `tests/test_docx_pictures.py`.

## Finding and cutting them out

Where a figure is comes from the reader, in order:

1. **Docling's pictures and charts** (`figures` events of the worker): the box on the page as fractions (top left is 0, 0) and the caption Docling linked to it.
2. **A page that gave no text and no picture** (`empty_pages`, a scanned sheet of drawings): the whole page is one figure (`source: sheet`).
3. For a PDF read by the **basic reader**, PyMuPDF finds the images and the drawn regions of each page (a page that is one image over its whole area is a scan, not a figure).

Pieces smaller than 2% of the page or thinner than 7% of its side are logos and rules, and are dropped; two boxes that overlap by 70% are one. The picture is cut from the
page drawn by PyMuPDF (170 dpi, the long side at most 1600 px) at that box with a small margin, so a caption just outside it is read, and saved as
`images/<document id>/<n>.webp` with a thumbnail `<n>_t.webp` (256 px). Only PDFs have figures for now; the setting `figures` (on by default) turns the step off.
Cutting a composite sheet into its sub-figures is not done: a sheet with several figures is one image with all its labels listed.

## What is kept with each figure

| Field | From |
|---|---|
| `label`, `number` | the caption (`FIG. 1A`, `Figure 3`, `图2`), else a line of the image that starts with a figure name |
| `caption` | Docling's, else the caption line of the page (when one figure is on it), else none |
| `refs` | up to three sentences of the document that name the figure: first the entry of the brief description of the drawings (`FIG. 1A is ...`), then the text that discusses it; never its own page |
| `ocr` | the words inside the image, read by RapidOCR (torch, CPU) of the Docling environment; confident lines only (0.6), each once, 700 characters; on a whole sheet the running header and footer are left out unless they name a figure |
| `image`, `thumb`, `width`, `height`, `sha`, `box`, `source`, `page` | the cut |

The record of a document's figures is `figures/<document id>.json`; a **passage** is made from it (`figure_rows`): `text` is `FIG. 1A (page 4): caption`, `The text says: ...`,
`Text in the figure: ...`, so it is searched (words and meaning) and embedded like any passage; `kind` is `figure`, `figure` the label, `caption`, `image`, `thumb`, `fig` (its number
in the document), `image_sha`, and `facts.figure` the number (the filter `figure`). A figure belongs to the section of the passage on its page or the one before. A document's meta
says `figures`, and its coverage `figures` and `figure_pages` (pages whose only content is a figure): such a page is no longer reported as empty.

Cutting again (`reindex`) rebuilds the figure passages from the saved record; a refresh of a source (`tadamoo/rag/sources`) reads the document again, cuts its figures again and
replaces the old ones in the same commit (a reading with none leaves none); removing a document removes its images. Restoring an older version of a document brings back its text,
not its figures. An archive made before this keeps no figures until its documents are added again (the original file is needed: archives do not keep it).

## A model's description

The enrichment job (`docs/rag-ingest.md`, "Descriptions by a model") also has the model **look** at each figure, one call each, with the picture and the text that came with it,
and write in the language set (Italian by default) what the figure shows (kind, axes or parts, main result) naming the entities, components, labels and units, plus the questions
it answers. They become the passage's `summary` and `questions`, in the searched text, so a question in Italian meets a figure whose labels are English. The function is
`rag_enrich` (the technical session when it fits, or the route the person set); the picture goes to a model outside this PC only for an archive marked `cloud_ok`. A model that cannot
see images ends this step with a note (the text descriptions are kept and the archive is indexed all the same). The output is read as strict JSON, cut to size, stripped of markup and
screened for instructions, like the text descriptions. Not measured: a real run with the local model (it needs the vision projector).

## For the agent and the chat

- `knowledge_search(filters: {kind: "figure"})` finds figures; a result has a `figure` (`id`, `label`, `caption`) and the citation `[archive / document, p. 4, FIG. 1A, section]`. The
  passages put in front of a request show a figure with a line that names its id. Other filters: `figure` (a label), `language`, `document`, `gene` (from the words in the figure).
- `knowledge_figure(id)` gives the caption, the words inside it, what the document says of it, the model's description when there is one, a `credit` line (`caption (source: document, p. N)`)
  and, when the conversation has a working folder, a PNG copy in `.tadamoo/artifacts/figures/` (`path`) with:
  - `show`: a `![credit](tadamoo-path:...)` line, so the chat page draws it in the answer, as it does a picture made by `generate_image`;
  - `look`: the model sees it with `analyze_image(path, question, crop?)` (the loaded model's vision, or the capability the person chose);
  - the `docx_edit` `insert_picture` and the `docx_create` `picture` block put it in a Word document (`docs/docx-tools.md`).
- The state note names the figures of each document (`US12274733B2.pdf 361 p. en 14 fig.`); the fixed block and the tool descriptions say when to use them: the person asks for a
  diagram, map, chart or "the figure of ...", or the answer would be clearer with one; always cite the document and page.

## Rights and privacy

Figures are data from documents: caption, OCR text and descriptions are untrusted text, fenced as data in every result (`knowledge_search`, `knowledge_figure` are untrusted tools),
and a model's description is screened for instructions. Licence and copyright of a figure are the person's business; the source and page are always given. `knowledge_figure` asks the
resolver (`docs/rag-resolve.md`) first: on a route that leaves this PC, an archive that is not `cloud_ok` gives no image and no text (`withheld`), and a figure id of an archive that is not
this conversation's is `none`. The copy in the working folder is made only after that verdict; an image that is then sent by `analyze_image` goes to the model the conversation uses.

## Measured

A scratch archive (never the real one) of the figure pages of four ER100 patents: the text blocks of the rebuilt archive, the figures from copies of the PDFs, Docling run only on the 19 figure
pages (8 of the digital CN patent, 11 sheets that had given no text) on the CPU in 300 s, OCR on the CPU (about 3 s a sheet). Result: **25 figures** (CN 14, US 3, WO2024031059 4, WO2024155957 4); the
11 empty pages all became figures with their labels (`FIG. 10B`, `FIG. 20`, `FIG. 2K`, ...) and the words inside them (axis titles, gene names such as MSRA, SIRT1, FOXO3, legends in Chinese), and
Docling's duplicate pictures (a page read twice) were merged. The text of a figure passage was read by eye on a dozen of them: the labels are right; the OCR of tilted or very small text is noisy
(`ves-d εe ud wn`), and the caption of a sheet is the `FIG.` line inside the image.

Retrieval, 16 questions written from the pictures (8 English, 4 Chinese, 4 Italian), **words only** (no embedder, no model description; the GPU was not asked for), the figure counted when a hit of the right
document and page is a figure passage:

| | English | Chinese | Italian | all |
|---|---|---|---|---|
| a figure is the first hit among all passages | 6/8 | 1/4 | 1/4 | 8/16 |
| a figure is in the first three, among all passages | 7/8 | 1/4 | 1/4 | 9/16 |
| in the first three with `filters: {kind: "figure"}` | 8/8 | 2/4 | 1/4 | 11/16 |
| **the same questions, text passages only** (a passage whose pages hold the figure) | 0/8 | 0/4 | 0/4 | 0/16 |

Without figure passages none of these can be answered: the labels are only in the image. What the words cannot do is also clear: the Italian questions about English or Chinese figures
find a figure only through shared tokens (gene names), and the Chinese ones are held back by the word index (it takes a run of Chinese characters as one word). Those two are what the embedder (meaning
across languages) and the model's description in Italian are for; neither was run here. **Left for the live phase**: the same questions with the embedder and the reranker; the enrichment run with the
local model's vision (how long a figure takes, whether the descriptions name the entities); and whether a multimodal embedder (an image and its text in one vector) beats the text embedding of the caption,
labels and description: not measured, and not adopted.
