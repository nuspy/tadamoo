# File tools of the agent

`read_file`, `write_file`, `edit_file`, `search_code` and `tree` work in the workspace folder; a path outside it follows the permission
level (below). The code is in
`tadamoo/tools/filesystem.py` (the sandbox and the guards), `tadamoo/editing.py` (finding and replacing text),
`tadamoo/syntax_check.py`, `tadamoo/gitignore.py` and `tadamoo/tools/search_code.py`, `tree.py`, `walk.py`.

## Paths outside the workspace

Inside the workspace every path passes. Outside it `WorkspaceSandbox.resolve_safe` asks `PermissionManager.outside_path`, which puts the
call to the level as one that is not a workspace edit: *Automatic* lets it go, *Ask first* and *Read only* ask, *Plan only* asks for a read and holds back a change,
and a deny rule refuses it. The question names the path; one answer covers it, and for `list_directory`, `search_code`, `tree`, `repo_map`
and `repo_diagram` also what is below it, so a walk asks once. What was allowed at one level is asked again at another. The person's
secrets are refused at every level, and a sandbox with no permission manager keeps the folder boundary. Paths outside the workspace are
named whole in answers, in the search hits and in the Changes entries.

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

## Pieces of a file

A long text is worked by blocks, without typing again what is already there (`tadamoo/tools/pieces.py`). A block is a run of whole lines; in
prose a paragraph is a line.

- **The map.** `read_file(path, outline=true)`: the lines and words of the file, its **version** (a short hash of the text) and one row for each
  Markdown section (`first-last`, words, level and title; a heading inside a code fence is not one). A file with no headings says so. A file
  with over 200 sections lists its top levels.
- **Reading a block.** `section` reads the section with that title (the whole title ignoring case, else the start of one, else a part of
  one; it must fit one section, or the answer lists the candidates). `words=[from, to]` reads the whole lines that hold those words,
  counted from 0, and says which words and lines it gave. Both end with the version.
- **Editing a block.** `edit_file` with `lines=[first, last]` or `section`, and `action`: `replace` (the default), `delete`, `insert_after`,
  `insert_before` or `move_after` (with `after`: `{line: n}`, 0 being the start, or `{section: title}`). The text comes from `text` or from
  `source`. A section is found by its title in the file as it is, so it never goes stale. **Lines need the version** they were read from: after
  any change the numbers are not the same, so a call with another version is refused and the answer carries the map of the file now. The answer of a
  successful edit has the new lines of the changed place, the number of lines and the new version. Line endings are kept; the syntax check
  and the owner rules of the other edits apply.
- **Copying a text.** `write_file` and the text of an edit take `source` instead of the text: `{message: "last"}` (a number: 1 the last
  reply, 2 the one before; replies that call a tool are not counted) or `{file: path}`, with `section`, `lines` or `words` to take a part.
  The harness copies the text; the model never types it again. The guards of `write_file` hold: a whole-file write over a file with half of
  its text asks for `overwrite=true`. A reply folded away is not there; the folded originals are a file under `.tadamoo/artifacts`, which a
  file source reads.

## search_code and tree

`search_code(pattern, path?, glob?, context?, max_results?)`: a Python regular expression over the workspace's text files, with no
need for ripgrep. It skips `.gitignore` matches (nested files, negation, `**`), the usual dependency and build folders, binary files
and files over 1 MB, and anything the file tools may not read. Hits are `path:line: text`, context lines `path-line- text`, at most 60
hits, then a note to refine the search. `tree(path?, depth?)`: the folder as an indented tree (depth 2 by default, 8 at most) with
the number of lines of each file and of each folder; the totals count everything below, whatever the depth. Both are pure tools: they
run in plan mode and in read-only mode.
