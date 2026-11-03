"""Filesystem tools with workspace sandboxing and diff recording."""
from __future__ import annotations

import difflib
import hashlib
import io
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from tadamoo import editing, syntax_check
from tadamoo.editing import indent_block
from tadamoo.environment import similar_names

MAX_RECORDED_TEXT = 200_000        # larger files keep their diff but not the full before/after text
MAX_LISTED = 500
READ_CHARS = 40_000                # what one read_file answer may hold; a longer file is read in ranges
SCAN_BYTES = 256_000_000           # how much of a file is read to count its lines
# what a shell command may have changed is found by comparing the workspace's text files before and after it
SKIP_DIRS = frozenset({".git", ".tadamoo", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                       ".idea", ".vscode"})
MAX_WATCHED_FILES = 5000
MAX_WATCHED_BYTES = 30_000_000     # the text kept in memory to compare against
MAX_SHELL_CHANGES = 50             # recorded for one command; a command that writes more is a build, not an edit
FINGERPRINT_BYTES = 4_000_000      # a file up to this size is told apart by a hash of its bytes, a larger one by its modified time and size
BLOCK_LINES = 2000                 # most lines of one block read around an anchor line
MIN_SHRINK_CHECK = 200             # a file shorter than this (characters) may be rewritten to anything
# text a model leaves in a whole-file write where it means "the rest is as before"
PLACEHOLDER = re.compile(
    r"\.\.\.\s*(?:the\s+)?(?:rest|remainder|remaining|other|previous|existing|unchanged|same)\b"
    r"|\b(?:rest|remainder)\s+of\s+(?:the\s+)?(?:file|code|content|class|function|implementation|module)\b"
    r"|\bexisting\s+code\b|\b(?:code|content|file|implementation)\s+(?:remains?|stays?|is)\s+(?:the\s+)?(?:same|unchanged)\b"
    r"|\bunchanged\s*\.\.\.|\bsame\s+as\s+before\b|\bkeep\s+(?:the\s+)?existing\b",
    re.I)


def touches_git(parts: Sequence[str]) -> bool:
    """Whether a path (its parts below the workspace) is, or is inside, a `.git` folder or file, however the name is spelled."""
    for part in parts:
        name = part.lower().rstrip(". ")
        if name == ".git" or name == "git~1" or part.lower().startswith(".git:"):
            return True
    return False


def refusal(message: str, **extra: Any) -> Dict[str, Any]:
    """The answer of a tool the harness declined to run (a guard, not a failure of the call): it is returned, not raised, so it does not count
    as an error of the tool."""
    return {"status": "refused", "harness_refusal": True, "message": message, **extra}


class WorkspaceSandbox:
    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.changes: List[Dict[str, Any]] = []
        self._known: Dict[str, Tuple[int, int, Optional[str]]] = {}
        self.watch_off = False                       # set when the workspace is too large to compare around a command
        # (tool, {"path"}) -> (allowed, reason): asked before a write inside .git, where a hook or the config can make git run code
        self.confirm_git_write: Optional[Callable[[str, Dict[str, Any]], Tuple[bool, str]]] = None
        # an editor that holds the files: it gives a file's text (None: not known to it) and takes the text to write
        self.read_through: Optional[Callable[[Path], Optional[str]]] = None
        self.write_through: Optional[Callable[[Path, str], None]] = None
        # path -> the protected place it is or lies in, or "": the person's secrets are not opened by any file tool
        self.protected_hit: Optional[Callable[[Path], str]] = None
        # what happens when an edit or a write leaves a file that parsed before unparsable: off, warn (written, with a warning) or block
        self.edit_guard = syntax_check.DEFAULT_GUARD
        # per file: what was read of it and when (a whole-file write needs all of it, unchanged), the last read, and failed edits in a row
        self._stamps: Dict[str, Dict[str, Any]] = {}
        self._last_read: Dict[str, Tuple[Any, Any, int]] = {}
        self._edit_failures: Dict[str, int] = {}

    def resolve_safe(self, rel_path: str | Path) -> Path:
        """Resolves path and enforces that it remains strictly inside self.root."""
        path = (self.root / rel_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{rel_path}' escapes workspace boundary. The working folder is {self.root}; "
                                  "file tools reach only paths inside it, relative to it.")
        hit = self.protected_hit(path) if self.protected_hit else ""
        if hit:
            raise PermissionError(f"Access denied: '{rel_path}' is one of the person's secrets ({Path(hit).name}): the vault, the payment files, "
                                  "the browser's sign-ins, the control token and the phone keys are read only in the Wallet.")
        return path

    def _where(self, path: str, resolved: Path, want_dir: bool = False) -> str:
        """What is known about a path that is not there: the working folder and the names that exist and look like it."""
        text = f" The working folder is {self.root}; relative paths are relative to it."
        if resolved.exists():
            return text + f" '{path}' exists but is {'a folder' if resolved.is_dir() else 'a file'}, not {'a folder' if want_dir else 'a file'}."
        folder = next((p for p in resolved.parents if p.is_dir()), self.root)
        missing = resolved.relative_to(folder).parts[0]
        if folder != resolved.parent:
            text += f" The folder '{folder.joinpath(missing).relative_to(self.root).as_posix()}' does not exist."
        found = similar_names(folder, missing)
        if not want_dir and not found:
            found = self._same_name_below(resolved.name)
        where = "" if folder == self.root else f" in {folder.relative_to(self.root).as_posix()}"
        return text + (f" Similar names{where}: {', '.join(found)}." if found else f" No similar name{where}.")

    def _same_name_below(self, name: str, limit: int = 3) -> List[str]:
        """Paths below the working folder whose file name equals `name`, ignoring case; the search is bounded."""
        out: List[str] = []
        seen = 0
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                seen += 1
                if n.lower() == name.lower():
                    out.append((Path(folder) / n).relative_to(self.root).as_posix())
            if len(out) >= limit or seen > MAX_WATCHED_FILES:
                break
        return out[:limit]

    def _guard_git(self, tool: str, path: str, resolved: Path) -> None:
        if self.confirm_git_write is None or not touches_git(resolved.relative_to(self.root).parts):
            return
        allowed, reason = self.confirm_git_write(tool, {"path": path})
        if not allowed:
            raise PermissionError(reason)

    @staticmethod
    def _read_text(path: Path) -> str:
        """The file's text exactly as stored: no newline translation."""
        return path.read_bytes().decode("utf-8", errors="replace")

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        """Writes through a temporary file so a failure never leaves a half-written file; newlines are kept."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".basalt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content.encode("utf-8"))
            if path.exists():
                os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read(self, path: Path) -> str:
        edited = self.read_through(path) if self.read_through is not None else None
        return edited if edited is not None else self._read_text(path)

    def _write(self, path: Path, content: str) -> None:
        if self.write_through is not None:
            self.write_through(path, content)
        else:
            self._write_text(path, content)

    # -- what has been read of a file ---------------------------------------------------------------

    def _rel(self, safe_path: Path) -> str:
        rel = safe_path.relative_to(self.root).as_posix()
        return rel.lower() if sys.platform == "win32" else rel

    def _editor_text(self, safe_path: Path) -> Optional[str]:
        """The text the editor holds for the file, when there is an editor and it holds it. One call asks the editor; pass the answer on."""
        return self.read_through(safe_path) if self.read_through is not None else None

    @staticmethod
    def _fingerprint(safe_path: Path, edited: Optional[str]) -> Tuple[Any, ...]:
        """What identifies the file's current content: the editor's text (`edited`) when it holds the file, else a hash of its bytes (the
        modified time and size for a large file)."""
        if edited is not None:
            return ("text", hashlib.blake2b(edited.encode("utf-8", "replace"), digest_size=16).hexdigest())
        st = safe_path.stat()
        if st.st_size <= FINGERPRINT_BYTES:
            return ("bytes", hashlib.blake2b(safe_path.read_bytes(), digest_size=16).hexdigest())
        return ("file", st.st_mtime_ns, st.st_size)

    @staticmethod
    def _whole(stamp: Dict[str, Any]) -> bool:
        return bool(stamp["exact"]) and (stamp["total"] == 0 or stamp["ranges"] == [(1, stamp["total"])])

    def _note_read(self, rel: str, key: Tuple[Any, ...], first: int, last: int, total: int, exact: bool) -> None:
        stamp = self._stamps.get(rel)
        if stamp is None or stamp["key"] != key:
            stamp = self._stamps[rel] = {"key": key, "ranges": [], "total": total, "exact": exact}
        stamp["total"], stamp["exact"] = total, exact
        if last >= first:
            merged: List[Tuple[int, int]] = []
            for a, b in sorted(stamp["ranges"] + [(first, last)]):
                if merged and a <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            stamp["ranges"] = merged

    def _after_write(self, rel: str, safe_path: Path, text: str, whole: bool) -> None:
        """The file was written by a tool: its new content is known (all of it after a whole-file write; as much as before after an edit)."""
        key = self._fingerprint(safe_path, text if self.write_through is not None else None)
        stamp = self._stamps.get(rel)
        if whole or stamp is None:
            lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
            stamp = {"key": key, "ranges": [(1, lines)] if whole and lines else [], "total": lines, "exact": True}
        stamp["key"] = key
        self._stamps[rel] = stamp
        self._last_read.pop(rel, None)
        self._edit_failures.pop(rel, None)

    # -- reading -----------------------------------------------------------------------------------

    def read_file(self, path: str, offset: int = 1, limit: int = 0, max_chars: int = READ_CHARS, numbered: bool = False,
                  anchor_line: int = 0, max_levels: int = 1, include_siblings: bool = False) -> Any:
        """The text of a file, whole or lines `offset` to `offset + limit - 1` (1-based; limit 0 means to the end). When the answer
        is not the whole file, a last line says which lines it holds, how many the file has, and where to go on. With `anchor_line`, the
        block around that line by indentation instead (see `_read_block`). The same read made again of an unchanged file answers
        `unchanged`, and the third time is refused."""
        try:
            offset, limit, anchor_line, max_levels = max(1, int(offset)), max(0, int(limit)), max(0, int(anchor_line)), max(0, int(max_levels))
        except (TypeError, ValueError):
            raise ValueError("offset, limit, anchor_line and max_levels must be whole numbers: offset is the first line to read (from 1), "
                             "limit how many lines (0 for all)")
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}.{self._where(path, safe_path)}")
        edited = self._editor_text(safe_path)
        rel, key = self._rel(safe_path), self._fingerprint(safe_path, edited)
        signature = (offset, limit, max_chars, bool(numbered), anchor_line, max_levels, bool(include_siblings))
        before = self._last_read.get(rel)
        repeats = before[2] + 1 if before and before[0] == signature and before[1] == key else 1
        self._last_read[rel] = (signature, key, repeats)
        if repeats == 2:
            return {"status": "unchanged", "path": path,
                    "note": "This file has not changed since you read it with these arguments; its text is earlier in this conversation. "
                            "Go on with it, or read another range."}
        if repeats > 2:
            return refusal(f"{path} was read {repeats} times in a row with the same arguments and has not changed. Use the text you already have, "
                           "read a different range, or take the next step.")
        if anchor_line:
            text, first, last, total = self._read_block(safe_path, path, edited, anchor_line, max_levels, include_siblings, limit or BLOCK_LINES, max_chars, numbered)
            exact = True
        else:
            text, first, last, total, exact = self._read_slice(safe_path, path, edited, offset, limit, max_chars, numbered)
        self._note_read(rel, key, first, last, total, exact)
        return text

    def _read_slice(self, safe_path: Path, path: str, edited: Optional[str], offset: int, limit: int, max_chars: int,
                    numbered: bool) -> Tuple[str, int, int, int, bool]:
        """(answer, first line, last line, lines in the file, whether that count is exact); the lines are those fully shown."""
        taken: List[str] = []
        size = total = scanned = cut_length = 0
        exact = True                                           # the whole file was counted
        collecting = True
        capped = False                                         # the character limit ended the answer before the lines wanted did
        with (io.BytesIO(edited.encode("utf-8")) if edited is not None else open(safe_path, "rb")) as f:
            if b"\x00" in f.read(8000):
                raise ValueError(f"'{path}' is a binary file")
            f.seek(0)
            for raw in f:
                total += 1
                scanned += len(raw)
                if scanned > SCAN_BYTES:
                    exact = False
                    break
                if total < offset or not collecting:
                    continue
                if limit and total >= offset + limit:
                    collecting = False
                    continue
                line = (f"{total:>6}\t" if numbered else "") + raw.decode("utf-8", errors="replace")
                if size + len(line) > max_chars:
                    collecting, capped = False, True
                    if not taken:                              # one line longer than the limit is shown in part, and says so
                        taken.append(line[:max_chars])
                        cut_length = len(line)
                    continue
                taken.append(line)
                size += len(line)
        count = str(total) if exact else f"more than {total}"
        if not taken:
            note = "" if total >= offset or (total == 0 and offset == 1) else f"[offset {offset} is past the end: the file has {count} lines]"
            return note, offset, offset - 1, total, exact
        last = offset + len(taken) - 1
        text = "".join(taken)
        seen = last - 1 if cut_length else last                # a line shown in part has not been read
        if offset == 1 and exact and last >= total and not cut_length:
            return text, offset, seen, total, exact
        notes = [f"lines {offset}-{last} of {count} shown"]
        if cut_length:
            notes.append(f"line {last} has {cut_length} characters and only the first {len(text)} are shown")
        if capped:
            notes.append(f"one answer holds at most {max_chars} characters")
        if last < total or not exact:
            notes.append(f"read_file with offset={last + 1} shows the next lines")
        return text + ("" if text.endswith("\n") else "\n") + "[" + "; ".join(notes) + "]", offset, seen, total, exact

    def _read_block(self, safe_path: Path, path: str, edited: Optional[str], anchor: int, max_levels: int, siblings: bool, limit: int, max_chars: int,
                    numbered: bool) -> Tuple[str, int, int, int]:
        """The block around line `anchor`, found by indentation: the block the line is in, or opens, and `max_levels - 1` blocks above it; with
        `siblings`, also the blocks beside the outermost one. At most `limit` lines. Returns (answer, first, last, lines in the file)."""
        data = edited.encode("utf-8") if edited is not None else safe_path.read_bytes()
        if b"\x00" in data[:8000]:
            raise ValueError(f"'{path}' is a binary file")
        if len(data) > SCAN_BYTES:
            raise ValueError(f"'{path}' is too large to find a block in; read a range with offset and limit")
        lines = data.decode("utf-8", errors="replace").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
        total = len(lines)
        if not total or anchor > total:
            return f"[line {anchor} is past the end: the file has {total} lines]", 1, 0, total
        start, end = indent_block(lines, anchor - 1, max_levels, siblings)
        first, last = start + 1, end + 1
        shown = lines[start:min(end + 1, start + limit)]
        body = "\n".join((f"{start + 1 + i:>6}\t" if numbered else "") + ln for i, ln in enumerate(shown))
        notes = [f"block around line {anchor}: lines {first}-{last} of {total}"]
        if len(shown) < end - start + 1:
            notes.append(f"only the first {len(shown)} lines are shown; read_file with offset={first + len(shown)} shows the rest")
            last = first + len(shown) - 1
        if len(body) > max_chars:
            body = body[:max_chars]
            notes.append(f"one answer holds at most {max_chars} characters")
            last = first - 1                                   # a cut text has not been read in full
        return body + "\n[" + "; ".join(notes) + "]", first, last, total

    def _record(self, path: str, kind: str, old: str, new: str) -> Dict[str, Any]:
        diff_lines = list(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"))
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        small = len(old) <= MAX_RECORDED_TEXT and len(new) <= MAX_RECORDED_TEXT
        self.changes.append({"file": path, "kind": kind, "added": added, "removed": removed,
                             "diff": "".join(diff_lines), "old_text": old if small else None,
                             "new_text": new if small else None})
        return {"status": "success", "file": path, "kind": kind, "added": added, "removed": removed}

    # -- writing -----------------------------------------------------------------------------------

    def _overwrite_problem(self, path: str, rel: str, key: Tuple[Any, ...], old: str, new: str) -> str:
        """Why a whole-file write over `old` is refused (the file was not read in full, changed since, or the new text looks like a
        summary of it), or "" when it may go on."""
        stamp = self._stamps.get(rel)
        if stamp is None:
            return (f"{path} exists and you have not read it, so a whole-file write could throw its content away. Read it with read_file first "
                    "(or change only the part you mean with edit_file); to replace it without reading it, send overwrite=true.")
        if stamp["key"] != key:
            return (f"{path} changed on disk since you last read it (another program, a command or the person). Read it again with read_file before "
                    "writing it whole; to replace it anyway, send overwrite=true.")
        if not self._whole(stamp):
            return (f"You have read only part of {path} ({stamp['total']} lines), so a whole-file write could drop what you did not see. Read all of it "
                    "with read_file first, or use edit_file for the part you mean; to replace it anyway, send overwrite=true.")
        new_marks, old_marks = len(PLACEHOLDER.findall(new)), len(PLACEHOLDER.findall(old))
        if new_marks > old_marks:
            return (f"The new text of {path} says that part of the file is left as it was (for example \"... rest unchanged ...\"), but a whole-file write "
                    "replaces everything, so that part would be lost. Write the complete file, or use edit_file for the part you change; "
                    "if the file really has this text, send overwrite=true.")
        if len(old) >= MIN_SHRINK_CHECK and len(new) * 2 < len(old):
            return (f"The new text of {path} is under half the length of the file now ({len(new)} against {len(old)} characters). If part of the file "
                    "was left out by mistake, write it complete or use edit_file; if the file really should shrink this much, send overwrite=true.")
        return ""

    def _syntax_problem(self, path: str, old: Optional[str], new: str) -> str:
        """The syntax error the new text of a file brings (it parsed before; `old` is None for a new file), or "" if none or the check is off.
        With the guard at `block` the error is raised and nothing is written."""
        if self.edit_guard == "off":
            return ""
        error = syntax_check.regression(path, old, new)
        if error and self.edit_guard == "block":
            raise ValueError(f"Nothing was written: {path} would no longer parse ({error}). The file is unchanged. Fix the text and try again.")
        return error or ""

    def _check_written(self, path: str, safe_path: Path, expected: str) -> None:
        """Reads the file back; a write that did not land as sent is an error, not a success."""
        if self.write_through is None and self._read_text(safe_path) != expected:
            raise OSError(f"{path} was written but reading it back gives different text; another program may be changing it. Read it again before the next edit.")

    def write_file(self, path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if safe_path.is_dir():
            raise IsADirectoryError(f"'{path}' is a folder, not a file.{self._where(path, safe_path)}")
        self._guard_git("write_file", path, safe_path)
        rel = self._rel(safe_path)
        old_content, kind = "", "added"
        if safe_path.exists():
            edited = self._editor_text(safe_path)
            old_content, kind = (edited if edited is not None else self._read_text(safe_path)), "modified"
            if old_content and not overwrite:
                problem = self._overwrite_problem(path, rel, self._fingerprint(safe_path, edited), old_content, content)
                if problem:
                    return refusal(problem)
        error = self._syntax_problem(path, old_content if kind == "modified" else None, content)
        self._write(safe_path, content)
        self._check_written(path, safe_path, content)
        result = self._record(path, kind, old_content, content)
        self._after_write(rel, safe_path, content, whole=True)
        if error:
            result.update(syntax_error=error, warning=f"The file was written but no longer parses: {error}. Fix it with another edit.")
        return result

    @staticmethod
    def _edit_items(target: Optional[str], replacement: Optional[str], edits: Any, replace_all: bool, expected_count: Optional[int]) -> List[Dict[str, Any]]:
        if edits is not None:
            if target is not None or replacement is not None:
                raise editing.EditError("Send either 'target' and 'replacement' or an 'edits' list, not both.")
            if not isinstance(edits, list) or not edits:
                raise editing.EditError("'edits' must be a non-empty list of {target, replacement} objects.")
            defaults = {k: v for k, v in (("replace_all", replace_all), ("expected_count", expected_count)) if v}
            return [{**defaults, **e} if isinstance(e, dict) else e for e in edits]
        if target is None or replacement is None:
            raise editing.EditError("Give 'target' and 'replacement', or an 'edits' list of {target, replacement} objects.")
        return [{"target": target, "replacement": replacement, "replace_all": replace_all, "expected_count": expected_count}]

    def _edit_failed(self, path: str, rel: str, text: str, exc: "editing.EditError") -> "editing.EditError":
        """The error for a failed edit; from the second failure on file it carries a fresh excerpt, from the third it says to read the file again."""
        count = self._edit_failures[rel] = self._edit_failures.get(rel, 0) + 1
        message = str(exc)
        if count >= 2:
            message += f"\n\nThis is failed edit {count} on {path} in a row. The file now:\n" + editing.excerpt(text, exc.closest, exc.closest_end)
        if count >= 3:
            message += ("\n\nStop guessing the text: read the file again with read_file, then copy from what it shows. If the change is large, "
                        "write the whole file with write_file instead.")
        return editing.EditError(message, exc.closest, exc.closest_end)

    def edit_file(self, path: str, target: Optional[str] = None, replacement: Optional[str] = None, edits: Optional[List[Dict[str, Any]]] = None,
                  replace_all: bool = False, expected_count: Optional[int] = None) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}.{self._where(path, safe_path)}")
        items = self._edit_items(target, replacement, edits, replace_all, expected_count)
        self._guard_git("edit_file", path, safe_path)
        rel = self._rel(safe_path)

        old_content = self._read(safe_path)
        old_lf = editing.to_lf(old_content)
        try:
            outcome = editing.apply_edits(old_lf, items, path)
        except editing.EditError as exc:
            raise self._edit_failed(path, rel, old_lf, exc) from None
        new_content = editing.restore_endings(old_content, old_lf, outcome.text) if outcome.changed else old_content
        if new_content == old_content:
            self._edit_failures.pop(rel, None)
            return {"status": "success", "file": path, "changed": False,
                    "note": " ".join(outcome.notes) or "The file already has this text; nothing was changed."}
        error = self._syntax_problem(path, old_content, new_content)
        self._write(safe_path, new_content)
        self._check_written(path, safe_path, new_content)
        result = self._record(path, "modified", old_content, new_content)
        self._after_write(rel, safe_path, new_content, whole=False)
        if len(items) > 1:
            result["edits"] = len(items)
        if outcome.notes:
            result["notes"] = outcome.notes
        if error:
            result.update(syntax_error=error, warning=f"The file was written but no longer parses: {error}. Fix it with another edit.")
        return result

    # -- edits made by a shell command ----------------------------------------------------------

    def _scan(self) -> Optional[Dict[str, Tuple[int, int, Optional[str]]]]:
        """The workspace's files as (modified time, size, text or None for binary and large files); None when there are
        too many files or too much text to compare. Files that did not change since the last scan are not read again."""
        seen: Dict[str, Tuple[int, int, Optional[str]]] = {}
        kept = 0
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(folder, d))]
            for name in names:
                if name.startswith(".basalt-") and name.endswith(".tmp"):
                    continue
                full = os.path.join(folder, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if len(seen) >= MAX_WATCHED_FILES:
                    return None
                rel = Path(full).relative_to(self.root).as_posix()
                known = self._known.get(rel)
                if known and known[0] == st.st_mtime_ns and known[1] == st.st_size:
                    entry = known
                else:
                    text = None
                    if st.st_size <= MAX_RECORDED_TEXT:
                        try:
                            data = Path(full).read_bytes()
                        except OSError:
                            continue
                        if b"\x00" not in data[:8000]:
                            text = data.decode("utf-8", errors="replace")
                    entry = (st.st_mtime_ns, st.st_size, text)
                if entry[2] is not None:
                    kept += entry[1]
                    if kept > MAX_WATCHED_BYTES:
                        return None
                seen[rel] = entry
        return seen

    @contextmanager
    def watching(self) -> Iterator[None]:
        """Around a command: the text files it added, changed or deleted are recorded like the file tools' edits."""
        before = None if self.watch_off else self._scan()
        if before is None:
            self.watch_off = True
            self._known = {}
        else:
            self._known = before
        try:
            yield
        finally:
            if before is not None:
                after = self._scan()
                if after is None:
                    self.watch_off, self._known = True, {}
                else:
                    self._record_shell_changes(before, after)
                    self._known = after

    def _record_shell_changes(self, before: Dict[str, Tuple[int, int, Optional[str]]],
                              after: Dict[str, Tuple[int, int, Optional[str]]]) -> None:
        recorded = 0
        for rel in sorted(set(before) | set(after)):
            b, a = before.get(rel), after.get(rel)
            if b and a and (b[0], b[1]) == (a[0], a[1]):
                continue
            old = b[2] if b else ""
            new = a[2] if a else ""
            if old is None or new is None or (b and a and old == new):
                continue                                              # binary or large, or only the time changed
            if recorded >= MAX_SHELL_CHANGES:
                break
            self._record(rel, "added" if b is None else "deleted" if a is None else "modified", old, new)
            recorded += 1

    def list_directory(self, path: str = ".") -> List[Dict[str, Any]]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_dir():
            raise NotADirectoryError(f"Directory not found: {path}.{self._where(path, safe_path, want_dir=True)}")

        entries: List[Dict[str, Any]] = []
        for p in sorted(safe_path.iterdir()):
            if len(entries) >= MAX_LISTED:
                entries.append({"name": f"[... more than {MAX_LISTED} entries; list a subfolder ...]", "is_dir": False, "size": 0})
                break
            entries.append({"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0})
        return entries
