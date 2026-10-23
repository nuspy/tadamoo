"""File checkpoint and rewind manager.

Preserves exact file hierarchy and content for reversible rollback.
Never deletes 0-byte files or flattens relative directories.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class CheckpointMeta:
    checkpoint_id: str
    timestamp: float
    description: str
    files_count: int


class CheckpointManager:
    def __init__(self, workspace_dir: str | Path, storage_dir: Optional[str | Path] = None) -> None:
        self.workspace = Path(workspace_dir).resolve()
        if storage_dir is None:
            self.storage = self.workspace / ".basalt_checkpoints"
        else:
            self.storage = Path(storage_dir).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)

    def _get_workspace_files(self) -> List[Path]:
        files: List[Path] = []
        for p in self.workspace.rglob("*"):
            if not p.is_file():
                continue
            # Skip checkpoint storage itself
            try:
                p.relative_to(self.storage)
                continue
            except ValueError:
                pass
            # Skip git and cache directories
            parts = p.parts
            if any(part in (".git", ".venv", "__pycache__", "node_modules") for part in parts):
                continue
            files.append(p)
        return files

    def create(self, description: str = "") -> str:
        """Takes an atomic snapshot of all tracked workspace files, preserving paths."""
        cp_id = f"cp_{int(time.time() * 1000)}"
        cp_dir = self.storage / cp_id
        cp_data_dir = cp_dir / "data"
        cp_data_dir.mkdir(parents=True, exist_ok=True)

        files = self._get_workspace_files()
        file_records = []

        for f in files:
            rel = f.relative_to(self.workspace)
            dest = cp_data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            file_records.append(str(rel).replace("\\", "/"))

        meta = {
            "checkpoint_id": cp_id,
            "timestamp": time.time(),
            "description": description,
            "files": file_records,
        }
        (cp_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return cp_id

    def restore(self, checkpoint_id: str) -> bool:
        """Restores workspace to the exact state captured at checkpoint_id."""
        cp_dir = self.storage / checkpoint_id
        meta_file = cp_dir / "meta.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found")

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        snapshot_files = set(meta.get("files", []))
        cp_data_dir = cp_dir / "data"

        # 1. Restore/overwrite snapshot files
        for rel_str in snapshot_files:
            rel = Path(rel_str)
            src = cp_data_dir / rel
            dest = self.workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        # 2. Remove files that were created after the snapshot
        current_files = self._get_workspace_files()
        for f in current_files:
            rel_str = str(f.relative_to(self.workspace)).replace("\\", "/")
            if rel_str not in snapshot_files:
                f.unlink(missing_ok=True)

        return True

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        out = []
        if not self.storage.is_dir():
            return out
        for d in sorted(self.storage.iterdir()):
            meta_file = d / "meta.json"
            if meta_file.is_file():
                try:
                    out.append(json.loads(meta_file.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
        return out
