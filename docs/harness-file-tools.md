# File tools of the agent

`read_file`, `write_file`, `edit_file`, `search_code` and `tree` work inside the workspace folder only. The code is in
`harness/tools/filesystem.py` (the sandbox and the guards), `harness/editing.py` (finding and replacing text),
`harness/syntax_check.py`, `harness/gitignore.py` and `harness/tools/search_code.py`, `tree.py`, `walk.py`.

## edit_file

Fields: `path`, then either `target` and `replacement`, or `edits`: a list of `{target, replacement}`. Edits are applied in
order, each to the result of the one before, and either all apply or none does. One call makes one checkpoint and one Changes entry.
`replace_all` changes every exact occurrence; `expected_count` does the same and fails if the count differs. Both work on exact text
only, and both can be set on one edit or, as a default, on the whole call.

Matching is done on LF text, in passes from strict to tolerant. The strictest pass that finds anything decides:

1. exact;
2. each line compared without its trailing whitespace;
3. each line compared without its indentation; the replacement is moved to the file's indentation (tabs or spaces as the file has
   them, a two-space step scaled to a four-space one);
4. Unicode-normalised: NFKC, smart quotes, dashes and odd spaces folded to plain ones.

A pass other than the exact one is applied only when it finds exactly one place, and the result's `notes` name the lines. Several
places: the error lists their line numbers and asks for more context. No place: the error gives the closest window of the file
(difflib ratio, at least 45%) with line numbers and the real lines around it, a diff of what was sent against what the file has
with whitespace made visible (`→` tab, `·` space, `⍽` another space character), and numbered next steps.

Special cases: an empty, whitespace-only or identical target gets its own message ("do not re-send this call unchanged"). An edit
that was already applied (the target is gone and the replacement is there) is a success with `changed: false`, no write and no
Changes entry. Line endings are restored on write (CRLF stays CRLF; in a mixed file lines the edit did not touch keep their own
ending). The write is read back and compared; a difference is an error.

A file that failed to take an edit twice in a row gets a fresh numbered excerpt of the file in the error; the third time the error
tells the model to read the file again and to prefer `write_file` for a large change. A successful edit or write resets the count.

## write_file and read stamps

The sandbox remembers, per file, what was read of it (line ranges and a hash of the content; modified time and size for files over
4 MB). A whole-file `write_file` over an existing, non-empty file is refused unless the file was read in full since it last
changed. It is also refused when the new text carries a "rest unchanged" style marker that the old text did not, or is under half the
old length (files under 200 characters are exempt). The refusal is an answer, not an error: `{"status": "refused",
"harness_refusal": true, "message": ...}`, so it does not count as a failure of the tool. `overwrite: true` skips the guards. Text a
tool wrote itself counts as known; a command that changed the file makes it stale.

An identical `read_file` of an unchanged file answers `{"status": "unchanged"}`; the third in a row is refused the same way. A
different range, a change of the file or a write starts the count again.

## Syntax check

After every `edit_file` and `write_file`, the new text of `.py`, `.json`, `.toml` (needs `tomllib` or `tomli`), `.yaml` and `.yml`
(needs PyYAML; custom tags are fine) is parsed in process, and `.js`, `.mjs`, `.cjs` with `node --check` only when `node` is on the
PATH. If the file parsed before (or is new) and no longer does, the answer carries `syntax_error` with line and column. The setting
`edit_guard` (`--edit-guard`, `AgentConfig.edit_guard`) is `warn` (default: written, with a `warning`), `block` (nothing is written and
the call fails) or `off`. There is no Settings page control for it yet.

## read_file

`numbered: true` puts the line number and a tab before each line. `anchor_line` returns the block around that line found by
indentation instead of a range: the block the line opens, or else the one it is in, plus `max_levels - 1` blocks above it (default
1); comment and decorator lines right above the block and a closing bracket line at its end belong to it; `include_siblings` adds
the blocks beside the outermost one. At most 2000 lines (`limit` lowers it).

## search_code and tree

`search_code(pattern, path?, glob?, context?, max_results?)`: a Python regular expression over the workspace's text files, with no
need for ripgrep. It skips `.gitignore` matches (nested files, negation, `**`), the usual dependency and build folders, binary files
and files over 1 MB, and anything the file tools may not read. Hits are `path:line: text`, context lines `path-line- text`, at most 60
hits, then a note to refine the search. `tree(path?, depth?)`: the folder as an indented tree (depth 2 by default, 8 at most) with
the number of lines of each file and of each folder; the totals count everything below, whatever the depth. Both are pure tools: they
run in plan mode and in read-only mode.
