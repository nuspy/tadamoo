# Changing documents that already exist (Word, Excel, PowerPoint, PDF)

Reading a document and writing a new one are different jobs from changing one. A change must keep what the file already holds: styles, lists,
headers, formulas, layouts, pictures, charts. The harness has a set of tools for each format; the model is told about them in the prompt
(`tadamoo/awareness.py`, `editing_line`, built from the tools that are registered) and in the `document-editing` skill (`skills/document-editing/`).

| Format | Tools | Library | Code |
|---|---|---|---|
| Word `.docx` | `docx_outline`, `docx_read`, `docx_edit`, `docx_create` | python-docx, lxml | [docx-tools.md](docx-tools.md) |
| Excel `.xlsx` | `xlsx_outline`, `xlsx_read`, `xlsx_edit`, `xlsx_create` | openpyxl | `tadamoo/tools/xlsx_edit.py` |
| PowerPoint `.pptx` | `pptx_outline`, `pptx_read`, `pptx_edit`, `pptx_create` | python-pptx | `tadamoo/tools/pptx_edit.py` |
| PDF | `pdf_outline`, `pdf_find`, `pdf_edit` | pypdf, pikepdf, pdfplumber, reportlab | `tadamoo/tools/pdf_edit.py` |

The libraries are in `requirements.txt`; a tool exists only where its library is installed. PyMuPDF is not used (AGPL).

## What they share

- **Look, change, check.** An outline says what the file holds and where; a reader shows the exact place; one `*_edit` call applies several edits.
- **All or nothing.** Every edit of a call is checked first; if any fails nothing is written and the answer lists each failure.
- **A write cannot leave half a file.** The result goes to a temporary file beside the target, is opened again, and only then replaces the target.
- **The original is kept on request.** `output` writes a new file. A PDF does it by default (`name.edited.pdf`); `in_place=true` replaces the original.
- **Checkpoints and rewind** treat each edit tool like `edit_file`; the `output` file is a path the checkpoint covers too.
- Text read from a file is data, never instructions (`untrusted`).

## Word

Addresses `pN`, `tN`, `tN.rR.cC`, all of a call resolved against the document before the call. `replace_text` works across runs and keeps the formatting;
with `track_changes` the edits are Word revisions. Headers, footers, footnotes, endnotes and text boxes are reached with `replace_text` and the targets
`headers`, `footers`, `footnotes`, `endnotes`, `textboxes`, or `everywhere` (the body and all of them); `docx_outline` lists what the file keeps there and
`docx_read` shows it. Not reached: content controls, fields' results, comments' replies.

## Excel

Edits run in order, each on the workbook as the earlier ones left it: `set_cell`, `set_range`, `append_rows`, `find_replace` (text cells), `set_format`,
`set_width`, `add_sheet`, `rename_sheet`, `delete_sheet`, `insert_rows`, `delete_rows`, `insert_cols`, `delete_cols`. A string starting with `=` is a formula.

- openpyxl does not calculate: formulas are stored without results and Excel calculates them when the file is opened. The tool says so.
- A workbook with charts, pivot tables, slicers, timelines, macros, embedded objects, form controls or sparklines is refused whole (a save would drop them).
- Row and column shifts are refused where formulas, merged cells or tables would point at the wrong cells; so are renaming and deleting a sheet that formulas use.
- To read or add up a large table, ask it in SQL with `query_attachment` (an attached file) rather than reading rows.

## PowerPoint

`replace_text` (shapes, tables, notes; keeps the formatting), `set_text`, `set_cell`, `set_notes`, `add_slide`, `delete_slide`, `move_slide`, `insert_picture`,
`add_textbox`, `delete_shape`. Slides are `sN`, shapes `sN.shM` (the shape id the outline shows), cells `sN.shM.rR.cC`. python-pptx keeps the parts it does not
understand (charts, SmartArt, media), so they survive; their own text is not searched.

## PDF

A PDF is not a document that reflows: its text is placed word by word. Each edit takes the PDF as bytes and returns bytes.

- **Exact:** `fill_form`, `delete_pages`, `keep_pages`, `reorder_pages`, `rotate_pages`, `append_pdf`, `add_text`, `add_image`, `watermark`, `highlight`, `set_metadata`.
- **`replace_text`** rewrites the string in the page's own content stream (pikepdf). It works only in a simple font (not a Type0 font, not a custom letter table), and, in a
  subset font, only with letters the font already shows on that page. It is all occurrences or none unless `partial=true`. Measured on real files, most PDFs made by Word,
  a browser or LaTeX are not like that, and the tool says why.
- **`cover_text`** draws a box over the words and writes the new ones on top (Helvetica, at the old size). The old words stay in the file's text layer; the tool says so, and
  it must never be used to remove something confidential.
- Only Western European (cp1252) letters can be written. A scanned page has no text to find or replace; it is read with `read_document` (OCR).
- Where the PDF came from a file the person still has, changing that file and exporting again is better, and the prompt says to offer it.
