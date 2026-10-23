"""Filesystem tools with workspace sandboxing and diff recording."""
from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


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

    def read_file(self, path: str, max_bytes: int = 100_000) -> str:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return safe_path.read_text(encoding="utf-8", errors="replace")[:max_bytes]

    def write_file(self, path: str, content: str) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        old_content = ""
        kind = "added"
        if safe_path.exists():
            old_content = safe_path.read_text(encoding="utf-8", errors="replace")
            kind = "modified"

        safe_path.write_text(content, encoding="utf-8")

        # Generate unified diff
        diff_lines = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        diff_text = "".join(diff_lines)
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

        change_record = {
            "file": path,
            "kind": kind,
            "added": added,
            "removed": removed,
            "diff": diff_text,
        }
        self.changes.append(change_record)
        return {
            "status": "success",
            "file": path,
            "kind": kind,
            "added": added,
            "removed": removed,
        }

    def edit_file(self, path: str, target: str, replacement: str) -> Dict[str, Any]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        old_content = safe_path.read_text(encoding="utf-8")
        if target not in old_content:
            raise ValueError(f"Target string not found in {path}")

        new_content = old_content.replace(target, replacement, 1)
        safe_path.write_text(new_content, encoding="utf-8")

        diff_lines = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        diff_text = "".join(diff_lines)
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

        change_record = {
            "file": path,
            "kind": "modified",
            "added": added,
            "removed": removed,
            "diff": diff_text,
        }
        self.changes.append(change_record)
        return {
            "status": "success",
            "file": path,
            "kind": "modified",
            "added": added,
            "removed": removed,
        }

    def list_directory(self, path: str = ".") -> List[Dict[str, Any]]:
        safe_path = self.resolve_safe(path)
        if not safe_path.is_dir():
            raise NotADirectoryError(f"Directory not found: {path}")

        entries = []
        for p in sorted(safe_path.iterdir()):
            entries.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else 0,
            })
        return entries
