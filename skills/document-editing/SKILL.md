---
name: document-editing
description: Change an existing Word, Excel, PowerPoint or PDF file without losing what it already holds, with TaDaMoo's docx_*, xlsx_*, pptx_* and pdf_* tools. Use when the person asks to edit, correct, update, fill in, translate in place, review with tracked changes, add to or remove from a document that already exists.
tags: document, word, docx, excel, xlsx, powerpoint, pptx, pdf, edit, form, tracked changes
requires: python
---

# Editing documents that already exist

Reading a document and writing a new one are different jobs from changing one. A change must keep what the file already holds: styles, lists, headers, formulas, layouts, pictures, charts. Never
read a file as text, change the text and write it back (`write_file`, a Markdown round trip): it loses all of that. Use the tool of the format.

## The way, for every format

1. **Look.** `docx_outline` / `xlsx_outline` / `pptx_outline` / `pdf_outline` say what the file holds and where. Read the exact place before changing it (`docx_read`, `xlsx_read`, `pptx_read`; `pdf_find` for text on a page).
2. **Change.** One call of `*_edit` with every edit of the job. All edits apply or none: if the answer lists failures, nothing was written; correct the edits and send them again.
3. **Keep the original.** Send `output` with a new name (a PDF does it by itself, `name.edited.pdf`). Change the original in place only when the person asked for it.
4. **Check.** Read the changed place again with the `*_read` tool (or `pdf_find`) and tell the person what changed and where the result is.

## Word (.docx)

`docx_outline` → `docx_read` → `docx_edit`. Edits are `replace_text` (keeps the formatting across runs), `set_paragraph`, `insert_paragraph`, `set_cell`, table rows, comments, styles, pictures.
`track_changes=true` writes real Word revisions when the person wants to review them. Headers, footers, footnotes, endnotes and text boxes are reached with `replace_text` and the target `headers`,
`footers`, `footnotes`, `endnotes`, `textboxes`, or `everywhere` for the body and all of them: a change to a name or a date usually has to be made in all of them. Addresses of one call all refer to
the document as it was before the call.

## Excel (.xlsx)

`xlsx_outline` → `xlsx_read` → `xlsx_edit`. Edits run in order: `set_cell`, `set_range`, `append_rows`, `find_replace`, `set_format`, sheets, rows and columns. A string starting with `=` is a formula.
Excel calculates formulas when the file is opened: say so, do not read the result back and report it. A workbook with charts, pivot tables or macros is refused whole (saving would drop them): tell the
person, and offer a copy of the data with `xlsx_create`. Row and column shifts are refused where formulas or merged cells would end up wrong; add rows at the end with `append_rows`. To read or add up a
large table do not read rows: ask it in SQL with `query_attachment` (an attached file).

## PowerPoint (.pptx)

`pptx_outline` → `pptx_read` → `pptx_edit`. `replace_text` keeps the formatting; `set_text`, `set_cell`, `set_notes`, `add_slide` (name a layout from the outline), `delete_slide`, `move_slide`,
`insert_picture`, `add_textbox`. Charts, SmartArt and media stay as they are; their own text is not searched. Slide numbers shift after adding or deleting: edits run in order.

## PDF

A PDF is not a document that reflows: its text is placed word by word, so some changes are exact and some are not. Say which kind the request is.

- **Exact:** `fill_form` (names from `pdf_outline`), `delete_pages`, `keep_pages`, `reorder_pages`, `rotate_pages`, `append_pdf`, `add_text`, `add_image`, `watermark`, `highlight`, `set_metadata`.
- **Replacing words:** `replace_text` changes them where they are written, only in simple fonts that already have the letters, and all occurrences or none; most PDFs made by Word, a browser or LaTeX are
  not like that, and it says why. `cover_text` then hides the old words and writes the new ones over them; the old words stay in the file's text layer, so never use it to remove something
  confidential, and tell the person. Only Western European letters can be written.
- **A scan** has no text: it cannot be searched or replaced. Read it with `read_document`; to change it, write over it with `add_text` or change the source.
- **The better way** when the PDF came from a file the person still has (Word, Excel, PowerPoint): change that file with its tools, and let the person export the PDF again. Offer it.

## What to tell the person

What was changed (how many places), where the result is, and what could not be changed and why. Never say a change was made in places you did not check, and never describe a covered text as removed.
