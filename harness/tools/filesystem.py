"""Filesystem tools with workspace sandboxing and diff recording."""
from __future__ import annotations

import difflib
import io
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from harness.environment import similar_names

MAX_RECORDED_TEXT = 200_000        # larger files keep their diff but not the full before/after text
MAX_LISTED = 500
READ_CHARS = 40_000                # what one read_file answer may hold; a longer file is read in ranges
SCAN_BYTES = 256_000_000           # how much of a file is read to count its lines
# what a shell command may have changed is found by comparing the workspace's text files before and after it
SKIP_DIRS = frozenset({".git", ".basalt", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                       ".idea", ".vscode"})
MAX_WATCHED_FILES = 5000
MAX_WATCHED_BYTES = 30_000_000     # the text kept in memory to compare against
MAX_SHELL_CHANGES = 50             # recorded for one command; a command that writes more is a build, not an edit


def touches_git(parts: Sequence[str]) -> bool:
    """Whether a path (its parts below the workspace) is, or is inside, a `.git` folder or file, however the name is spelled."""
    for part in parts:
        name = part.lower().rstrip(". ")
        if name == ".git" or name == "git~1" or part.lower().startswith(".git:"):
            return True
    return False


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

    def resolve_safe(self, rel_path: str | Path) -> Path:
        """Resolves path and enforces that it remains strictly inside self.root."""
        path = (self.root / rel_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{rel_path}' escapes workspace boundary. The working folder is {self.root}; "
                                  "file tools reach only paths inside it, relative to it.")
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

    def read_file(self, path: str, offset: int = 1, limit: int = 0, max_chars: int = READ_CHARS) -> str:
        """The text of a file, whole or lines `offset` to `offset + limit - 1` (1-based; limit 0 means to the end). When the answer
        is not the whole file, a last line says which lines it holds, how many the file has, and where to go on."""
        try:
            offset, limit = max(1, int(offset)), max(0, int(limit))
        except (TypeError, ValueError):
            raise ValueError("offset and limit must be whole numbers: offset is the first line to read (from 1), limit how many lines (0 for all)")
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}.{self._where(path, safe_path)}")
        taken: List[str] = []
        size = total = scanned = cut_length = 0
        exact = True                                           # the whole file was counted
        collecting = True
        capped = False                                         # the character limit ended the answer before the lines wanted did
        edited = self.read_through(safe_path) if self.read_through is not None else None     # the editor's own copy, when it offers one
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
                line = raw.decode("utf-8", errors="replace")
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
            return "" if total >= offset or (total == 0 and offset == 1) else f"[offset {offset} is past the end: the file has {count} lines]"
        last = offset + len(taken) - 1
        text = "".join(taken)
        if offset == 1 and exact and last >= total and not cut_length:
            return text
        notes = [f"lines {offset}-{last} of {count} shown"]
        if cut_length:
            notes.append(f"line {last} has {cut_length} characters and only the first {len(text)} are shown")
        if capped:
            notes.append(f"one answer holds at most {max_chars} characters")
        if last < total or not exact:
            notes.append(f"read_file with offset={last + 1} shows the next lines")
        return text + ("" if text.endswith("\n") else "\n") + "[" + "; ".join(notes) + "]"

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

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if safe_path.is_dir():
            raise IsADirectoryError(f"'{path}' is a folder, not a file.{self._where(path, safe_path)}")
        self._guard_git("write_file", path, safe_path)
        old_content, kind = "", "added"
        if safe_path.exists():
            old_content, kind = self._read(safe_path), "modified"
        self._write(safe_path, content)
        return self._record(path, kind, old_content, content)

    def edit_file(self, path: str, target: str, replacement: str) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}.{self._where(path, safe_path)}")
        if not target:
            raise ValueError("The text to replace must not be empty")
        self._guard_git("edit_file", path, safe_path)

        old_content = self._read(safe_path)
        if "\r\n" in old_content and "\r\n" not in target:        # the model writes \n; the file uses CRLF
            target = target.replace("\n", "\r\n")
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        count = old_content.count(target)
        if count == 0:
            raise ValueError(f"Target string not found in {path}")
        if count > 1:
            raise ValueError(f"Target string appears {count} times in {path}; include more surrounding text so it is unique")

        new_content = old_content.replace(target, replacement, 1)
        self._write(safe_path, new_content)
        return self._record(path, "modified", old_content, new_content)

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
