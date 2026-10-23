"""File checkpoint and rewind manager.

Keeps the exact relative layout and content of the workspace. Snapshots live outside the workspace,
so the agent's own tools cannot reach them; a rewind first takes a safety snapshot of the current
state, so even a rewind can be undone.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".basalt_checkpoints"}
_CHECKPOINT_ID = re.compile(r"^cp_\d+_\d+$")


def default_storage(workspace: Path) -> Path:
    try:
        from serve.userdata import user_data_dir
        base = user_data_dir()
    except Exception:  # the harness also runs without the server package
        base = Path(tempfile.gettempdir()) / "basalt"
    return base / "checkpoints" / hashlib.sha1(str(workspace).lower().encode("utf-8")).hexdigest()[:16]


class CheckpointManager:
    def __init__(self, workspace_dir: str | Path, storage_dir: Optional[str | Path] = None) -> None:
        self.workspace = Path(workspace_dir).resolve()
        self.storage = Path(storage_dir).resolve() if storage_dir is not None else default_storage(self.workspace)
        if self.storage == self.workspace or self.workspace in self.storage.parents:
            raise ValueError("checkpoint storage must not sit inside the workspace the agent can write to")
        self.storage.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._lock = threading.Lock()
        self.last_safety_checkpoint: Optional[str] = None

    def _inside_storage(self, p: Path) -> bool:
        try:
            p.relative_to(self.storage)
            return True
        except ValueError:
            return False

    def _get_workspace_files(self) -> List[Path]:
        files: List[Path] = []
        for p in self.workspace.rglob("*"):
            if p.is_symlink() or not p.is_file() or self._inside_storage(p):
                continue
            if any(part in IGNORED_DIRS for part in p.relative_to(self.workspace).parts):
                continue
            files.append(p)
        return files

    def _new_id(self) -> str:
        with self._lock:
            while True:
                self._seq += 1
                cp_id = f"cp_{int(time.time() * 1000)}_{self._seq}"
                if not (self.storage / cp_id).exists():
                    return cp_id

    def _checked_rel(self, rel_str: str) -> Path:
        rel = Path(rel_str)
        target = (self.workspace / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or self.workspace not in target.parents:
            raise ValueError(f"Checkpoint entry '{rel_str}' points outside the workspace")
        return rel

    def create(self, description: str = "") -> str:
        """Snapshots every tracked workspace file, keeping relative paths."""
        cp_id = self._new_id()
        cp_dir = self.storage / cp_id
        data_dir = cp_dir / "data"
        data_dir.mkdir(parents=True)
        records = []
        for f in self._get_workspace_files():
            rel = f.relative_to(self.workspace)
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            records.append(str(rel).replace("\\", "/"))
        meta = {"checkpoint_id": cp_id, "timestamp": time.time(), "description": description, "files": records}
        tmp = cp_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(cp_dir / "meta.json")      # a snapshot without meta.json is incomplete and never listed
        return cp_id

    def restore(self, checkpoint_id: str, delete_new: bool = True) -> bool:
        """Puts the workspace back as it was at `checkpoint_id`.

        Files created since are removed when `delete_new` is true. The state before the rewind is
        saved first and named in `last_safety_checkpoint`."""
        if not _CHECKPOINT_ID.match(checkpoint_id):
            raise ValueError(f"Invalid checkpoint id: {checkpoint_id!r}")
        cp_dir = self.storage / checkpoint_id
        meta_file = cp_dir / "meta.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        rels = [self._checked_rel(r) for r in meta.get("files", [])]     # validate everything before touching anything
        snapshot = {str(r).replace("\\", "/") for r in rels}

        self.last_safety_checkpoint = self.create(f"before rewind to {checkpoint_id}")

        for rel in rels:
            dest = self.workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cp_dir / "data" / rel, dest)

        if delete_new:
            for f in self._get_workspace_files():
                if str(f.relative_to(self.workspace)).replace("\\", "/") not in snapshot:
                    f.unlink(missing_ok=True)
            for d in sorted((p for p in self.workspace.rglob("*") if p.is_dir() and not p.is_symlink()),
                            key=lambda p: len(p.parts), reverse=True):
                if self._inside_storage(d) or any(part in IGNORED_DIRS for part in d.relative_to(self.workspace).parts):
                    continue
                try:
                    d.rmdir()          # only empty folders go
                except OSError:
                    pass
        return True

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.storage.is_dir():
            return out
        for d in sorted(self.storage.iterdir(), key=lambda p: (len(p.name), p.name)):
            meta_file = d / "meta.json"
            if meta_file.is_file():
                try:
                    out.append(json.loads(meta_file.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
        return out
