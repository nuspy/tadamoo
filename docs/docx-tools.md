# Word tools (`docx_outline`, `docx_read`, `docx_edit`, `docx_create`)

Four harness tools to change Word files (`.docx`). The model supplies text and addresses; the file is changed by code
(`tadamoo/tools/docx_edit.py`, run-level and tracked-change markup in `tadamoo/tools/docx_runs.py`, the descriptions and schemas in
`tadamoo/tools/docx_schemas.py`), on the CPU, with python-docx. They are registered with the other file tools, only when python-docx
is installed (`tadamoo/requirements.txt`).

These tools are for **editing**. Reading a scanned or complex document to understand it (PDF, Word, Excel to Markdown) is the
knowledge tool `read_document`, not these: the outline and the reader here show only what an edit needs.

## Addresses

Counted from 1 over the body of the document:

- paragraph `p12`, range `p10-p20`;
- table `t2`, cell `t2.r3.c1` (row 3, cell 1 of the row; a merged cell counts once);
- `end` and `start` in `insert_paragraph`.

Paragraphs inside tables are not numbered `pN`; they are reached through the cell. Headers, footers, footnotes, text boxes and content
controls are not addressable and not listed.

## The tools

| Tool | Effect | Does |
|---|---|---|
| `docx_outline(path, detail?)` | pure | Headings with level and address, paragraphs per section, tables (address, rows x columns, first row), image and comment counts, tracked changes present. `detail` lists every paragraph with style and a short preview. |
| `docx_read(path, target)` | pure | The full text at an address: paragraph or range with style and the bold and italic stretches, a table as Markdown, a cell. |
| `docx_edit(path, edits, track_changes?)` | reversible | Several edits in one call, applied together. |
| `docx_create(path, blocks, overwrite?)` | reversible | A new document from blocks: `heading` (level 0 is the title), `paragraph` (`bold`, `italic`, `style`), `bullet`, `numbered`, `table` (`rows`, `header`, `style`), `picture` (`image`, `caption?`, `width_cm?`, `alt?`, `numbered?`), `page_break`. Refuses an existing file unless `overwrite` is true. |

`docx_outline` and `docx_read` are marked untrusted: a document may come from outside, so its text is returned in a data block
(`tadamoo/untrusted.py`) like attachments and web pages, and is data, not instructions.

## `docx_edit`

Each edit is an object with a `kind`:

| kind | fields |
|---|---|
| `replace_text` | `target` (default: the whole document, tables included), `find`, `replace`, `count` (default 1; 0 for all) |
| `set_paragraph` | `target` (one paragraph), `text`, `style?` |
| `insert_paragraph` | `after` or `before` (`pN`, `tN`, `end`, `start`), `text`, `style?` |
| `delete_paragraph` | `target` (`pN` or a range) |
| `set_cell` | `target` (`tN.rR.cC`), `text` |
| `insert_table_row` | `table`, `after_row` (0 puts it first), `cells` (one text per cell of the row) |
| `delete_table_row` | `table`, `row` |
| `add_comment` | `target`, `text`, `author?` (default TaDaMoo), `find?` to anchor on exact words |
| `set_style` | `target` (paragraphs or a table), `style` |
| `insert_picture` | `after` or `before` (`pN`, `tN`, `end`, `start`), `image` (a PNG, JPEG, GIF, BMP or TIFF file of the workspace; a WebP file is refused: convert it), `caption?`, `width_cm?` (default 14, cut to the width of the text), `alt?`, `numbered?` (default true) |

Rules:

- **Addresses are resolved against the original document** (paragraphs, tables, rows and cells as numbered before the call), so
  edits of one call do not shift each other, and several insertions after one paragraph keep the order they were given. What a
  `find` matches is the text as the earlier edits of the call left it.
- **Atomic.** Edits are applied to the open document; if any fails, nothing is written and the answer lists every failure
  (`edit N (kind target): reason`). The file is written to a temporary file next to it, opened again to check it, then moved over
  the original, so an error at any step leaves the original untouched.
- **`replace_text` keeps formatting.** The new text goes into the first run the match touches, so it keeps that run's formatting;
  a match that spans several runs (or a tab or line break) is handled. A new line in `replace` or `text` becomes a line break.
- **Pictures** (`insert_picture`, the `picture` block): a centred paragraph with the picture, `width_cm` wide at most as wide as the text, its alt text set (`alt`, else the caption), and under it the
  caption in the `Caption` style when the document has one, written `Figure N - caption` where N follows the figures already in the document (`numbered: false` writes the caption as it is). With
  `track_changes` both paragraphs are tracked insertions. The agent gets a picture from the knowledge archives with `knowledge_figure` (`docs/rag-figures.md`) and puts it after the paragraph that
  discusses it, with the figure's `credit` line (`caption (source: document, p. N)`) as the caption. A picture file outside the workspace, missing, larger than 25 MB or of another kind is refused.
- **New paragraphs** take the paragraph properties and the font of the paragraph they are next to, without bold and italic, unless
  that one is a heading, or `style` is given.
- **Refused:** a paragraph that holds an image or object cannot be rewritten with `set_paragraph` (use `replace_text` for its words);
  a paragraph that carries a section break cannot be deleted; the last row of a table cannot be deleted; an edit of a paragraph
  that an earlier edit of the call deleted fails.
- Unknown fields are named in the error, so a misspelt field is not ignored.

### Tracked changes

With `track_changes: true` the changes are written as Word revisions by author `TaDaMoo` with the date of the call:

- `replace_text`: the old runs in `w:del`, the new text in `w:ins`; `insert_paragraph`: the runs and the paragraph mark in `w:ins`;
  `delete_paragraph`: the runs and the paragraph mark in `w:del`; `set_paragraph` and `set_cell`: old text deleted, new text inserted;
  `insert_table_row` and `delete_table_row`: the row marked in `w:trPr`, the text in `w:ins` or `w:del`;
  `set_style` on paragraphs: a `w:pPrChange` holding the old properties.
- Not tracked: `set_style` on a table (the call fails and says so; run it with `track_changes` false) and `add_comment`
  (comments are not revisions; they are added as they are).
- A match inside a tracked insertion made by someone else cannot be replaced as a tracked change; the error says so.

## In the harness

- **Workspace.** The path goes through the same `resolve_safe` as `edit_file` (inside the working folder, not a protected secret,
  owner and `.git` guards).
- **Checkpoints and rewind.** `docx_edit` and `docx_create` are in `FILE_PATH_TOOLS` (`tadamoo/checkpoints.py`): the loop copies the
  `.docx` to a checkpoint before the call, and a rewind puts the original bytes back (a created file is removed). They are also in
  the rewind and reporting lists of file tools, so the Changes view and the mission card know them.
- **Changes view.** The change is recorded like `edit_file`'s, with a text diff (one line per paragraph and per table row, comments
  last; tracked insertions as `{+ +}` and deletions as `[- -]`). The old and new text are not kept (`old_text` and `new_text` are
  `None`), because the Agent tab's block review writes text back and a `.docx` is not text.
- **Permissions.** Reversible effect: workspace edits pass at the default level and ask where `edit_file` asks; they are in the
  `write` group, and the readers in the `read` group.
- **Roles.** The readers are in the read tools; the writers are in `EDIT_TOOLS`, so `ask` and `orchestrate` do not have them and
  `plan` may use them only on `.tadamoo/plans/*.md`. `build`, `debug` and `general` have all four.

## Limits

Only `.docx` (not `.doc`, `.docm`, `.odt`); a damaged or password-protected file is refused with that reason. Content outside the
body, nested tables and content controls are left as they are. Comments need python-docx 1.2 or later.
Tests: `tests/test_docx_edit.py` (fixtures are made with python-docx, none is committed).
