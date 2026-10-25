"""Filesystem tools with workspace sandboxing and diff recording."""
from __future__ import annotations

import difflib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

MAX_RECORDED_TEXT = 200_000        # larger files keep their diff but not the full before/after text
MAX_LISTED = 500
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

    def resolve_safe(self, rel_path: str | Path) -> Path:
        """Resolves path and enforces that it remains strictly inside self.root."""
        path = (self.root / rel_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{rel_path}' escapes workspace boundary")
        return path

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

    def read_file(self, path: str, max_bytes: int = 100_000) -> str:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        with open(safe_path, "rb") as f:
            data = f.read(max_bytes + 1)
        if b"\x00" in data[:8000]:
            raise ValueError(f"'{path}' is a binary file")
        text = data[:max_bytes].decode("utf-8", errors="ignore" if len(data) > max_bytes else "replace")
        if len(data) > max_bytes:
            text += f"\n[... file continues; only the first {max_bytes} bytes were read ...]"
        return text

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
            raise IsADirectoryError(f"'{path}' is a directory")
        self._guard_git("write_file", path, safe_path)
        old_content, kind = "", "added"
        if safe_path.exists():
            old_content, kind = self._read_text(safe_path), "modified"
        self._write_text(safe_path, content)
        return self._record(path, kind, old_content, content)

    def edit_file(self, path: str, target: str, replacement: str) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if not target:
            raise ValueError("The text to replace must not be empty")
        self._guard_git("edit_file", path, safe_path)

        old_content = self._read_text(safe_path)
        if "\r\n" in old_content and "\r\n" not in target:        # the model writes \n; the file uses CRLF
            target = target.replace("\n", "\r\n")
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        count = old_content.count(target)
        if count == 0:
            raise ValueError(f"Target string not found in {path}")
        if count > 1:
            raise ValueError(f"Target string appears {count} times in {path}; include more surrounding text so it is unique")

        new_content = old_content.replace(target, replacement, 1)
        self._write_text(safe_path, new_content)
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
            raise NotADirectoryError(f"Directory not found: {path}")

        entries: List[Dict[str, Any]] = []
        for p in sorted(safe_path.iterdir()):
            if len(entries) >= MAX_LISTED:
                entries.append({"name": f"[... more than {MAX_LISTED} entries; list a subfolder ...]", "is_dir": False, "size": 0})
                break
            entries.append({"name": p.name, "is_dir": p.is_dir(), "size": p.stat().st_size if p.is_file() else 0})
        return entries
