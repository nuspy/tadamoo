"""Filesystem tools with workspace sandboxing and diff recording."""
from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

MAX_RECORDED_TEXT = 200_000        # larger files keep their diff but not the full before/after text
MAX_LISTED = 500


class WorkspaceSandbox:
    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.changes: List[Dict[str, Any]] = []

    def resolve_safe(self, rel_path: str | Path) -> Path:
        """Resolves path and enforces that it remains strictly inside self.root."""
        path = (self.root / rel_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{rel_path}' escapes workspace boundary")
        return path

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
