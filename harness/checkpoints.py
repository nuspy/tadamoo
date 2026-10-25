"""File checkpoint and rewind manager.

Keeps the exact relative layout and content of the workspace. Snapshots live outside the workspace,
so the agent's own tools cannot reach them; a rewind first takes a safety snapshot of the current
state, so even a rewind can be undone.

A snapshot is bounded: heavy folders (dependencies, caches, build output, git objects) are left out, a file over
a size limit is skipped with a warning in the log, a snapshot stops at the total limit, and when the stored
snapshots together pass the total limit the oldest are deleted. Inside `.git` only the hooks and the config are
kept, because those are what can make a later git command run code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

log = logging.getLogger("basalt.checkpoints")

DEFAULT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", ".basalt_checkpoints", ".basalt", "build", "dist", ".git/objects")
GIT_KEPT = ("config", "hooks")                       # what is kept of a .git folder
MB = 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 50 * MB                     # a larger file is not copied
DEFAULT_MAX_TOTAL_BYTES = 2048 * MB                  # all snapshots together, and one snapshot by itself
_CHECKPOINT_ID = re.compile(r"^cp_\d+_\d+$")


def default_storage(workspace: Path) -> Path:
    try:
        from serve.userdata import user_data_dir
        base = user_data_dir()
    except Exception:  # the harness also runs without the server package
        base = Path(tempfile.gettempdir()) / "basalt"
    return base / "checkpoints" / hashlib.sha1(str(workspace).lower().encode("utf-8")).hexdigest()[:16]


def _env_mb(name: str, fallback: int) -> int:
    try:
        return max(1, int(float(os.environ[name]) * MB))
    except (KeyError, ValueError):
        return fallback


def _git_index(parts: Sequence[str]) -> Optional[int]:
    return next((i for i, p in enumerate(parts) if p == ".git"), None)


class CheckpointManager:
    """`exclude` lists folders by name (at any depth) or by path (`.git/objects`); the environment adds to it with
    BASALT_CHECKPOINT_EXCLUDE (comma separated) and sets the limits with BASALT_CHECKPOINT_MAX_FILE_MB and
    BASALT_CHECKPOINT_MAX_TOTAL_MB when the arguments are not given."""

    def __init__(self, workspace_dir: str | Path, storage_dir: Optional[str | Path] = None, *, exclude: Optional[Iterable[str]] = None,
                 max_file_bytes: Optional[int] = None, max_total_bytes: Optional[int] = None) -> None:
        self.workspace = Path(workspace_dir).resolve()
        self.storage = Path(storage_dir).resolve() if storage_dir is not None else default_storage(self.workspace)
        if self.storage == self.workspace or self.workspace in self.storage.parents:
            raise ValueError("checkpoint storage must not sit inside the workspace the agent can write to")
        self.storage.mkdir(parents=True, exist_ok=True)
        extra = [e.strip() for e in os.environ.get("BASALT_CHECKPOINT_EXCLUDE", "").split(",") if e.strip()]
        self.exclude = tuple(tuple(e.replace("\\", "/").strip("/").split("/")) for e in (DEFAULT_EXCLUDES if exclude is None else tuple(exclude)) + tuple(extra))
        self.max_file_bytes = max_file_bytes if max_file_bytes is not None else _env_mb("BASALT_CHECKPOINT_MAX_FILE_MB", DEFAULT_MAX_FILE_BYTES)
        self.max_total_bytes = max_total_bytes if max_total_bytes is not None else _env_mb("BASALT_CHECKPOINT_MAX_TOTAL_MB", DEFAULT_MAX_TOTAL_BYTES)
        self._seq = 0
        self._lock = threading.Lock()
        self._pinned: set[str] = set()                 # never rotated away: the checkpoint being restored
        self.last_safety_checkpoint: Optional[str] = None

    def _inside_storage(self, p: Path) -> bool:
        try:
            p.relative_to(self.storage)
            return True
        except ValueError:
            return False

    # -- what is tracked ------------------------------------------------------------------------------------

    def _excluded(self, parts: Sequence[str]) -> bool:
        """Whether a folder (given by its path parts below the workspace) is left out."""
        for e in self.exclude:
            n = len(e)
            if any(tuple(parts[i:i + n]) == e for i in range(len(parts) - n + 1)):
                return True
        return False

    @staticmethod
    def _git_tracked(parts: Sequence[str], is_dir: bool) -> bool:
        """Inside a .git folder only the config file and the hooks are kept."""
        i = _git_index(parts)
        if i is None:
            return True
        rest = parts[i + 1:]
        if not rest or rest[0] == "hooks":
            return True
        return rest[0] == "config" and len(rest) == 1 and not is_dir

    def _walk(self) -> Iterator[Tuple[Path, Tuple[str, ...], int]]:
        """(file, path parts below the workspace, size) of every tracked file, in a stable order."""
        for folder, dirs, names in os.walk(self.workspace):
            base = Path(folder)
            rel = base.relative_to(self.workspace).parts
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink() and not self._inside_storage(base / d)
                             and not self._excluded(rel + (d,)) and self._git_tracked(rel + (d,), True))
            for name in sorted(names):
                full = base / name
                try:
                    if full.is_symlink() or not full.is_file():
                        continue
                    size = full.stat().st_size
                except OSError:
                    continue
                if self._git_tracked(rel + (name,), False):
                    yield full, rel + (name,), size

    # -- identity and validation ----------------------------------------------------------------------------

    def _new_dir(self) -> Tuple[str, Path]:
        """A folder for a new checkpoint; creating it is what claims the name, so two managers cannot take the same one."""
        while True:
            with self._lock:
                self._seq += 1
                cp_id = f"cp_{int(time.time() * 1000)}_{self._seq}"
            cp_dir = self.storage / cp_id
            try:
                cp_dir.mkdir()
                return cp_id, cp_dir
            except FileExistsError:
                continue

    def _checked_rel(self, rel_str: Any) -> Path:
        if not isinstance(rel_str, str) or not rel_str or "\x00" in rel_str:
            raise ValueError(f"Checkpoint entry {rel_str!r} is not a path")
        rel = Path(rel_str)
        target = (self.workspace / rel).resolve()
        if rel.is_absolute() or rel.drive or ".." in rel.parts or self.workspace not in target.parents:
            raise ValueError(f"Checkpoint entry '{rel_str}' points outside the workspace")
        return rel

    # -- create, restore, list ------------------------------------------------------------------------------

    def create(self, description: str = "") -> str:
        """Snapshots the tracked workspace files, keeping relative paths; then deletes the oldest snapshots over the total limit."""
        cp_id, cp_dir = self._new_dir()
        data_dir = cp_dir / "data"
        try:
            data_dir.mkdir()
            records: List[str] = []
            skipped: List[Dict[str, Any]] = []
            total = 0
            for f, parts, size in self._walk():
                rel_text = "/".join(parts)
                if size > self.max_file_bytes:
                    skipped.append({"path": rel_text, "reason": "too large", "bytes": size})
                    log.warning("checkpoint %s: %s skipped, %d bytes is over the %d byte file limit", cp_id, rel_text, size, self.max_file_bytes)
                    continue
                if total + size > self.max_total_bytes:
                    skipped.append({"path": rel_text, "reason": "snapshot limit", "bytes": size})
                    log.warning("checkpoint %s: %s skipped, the snapshot would pass %d bytes", cp_id, rel_text, self.max_total_bytes)
                    continue
                dest = data_dir.joinpath(*parts)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                except OSError as exc:
                    skipped.append({"path": rel_text, "reason": f"unreadable: {exc}", "bytes": size})
                    log.warning("checkpoint %s: %s skipped (%s)", cp_id, rel_text, exc)
                    continue
                total += size
                records.append(rel_text)
            meta = {"checkpoint_id": cp_id, "timestamp": time.time(), "description": description, "files": records, "bytes": total,
                    "skipped": skipped}
            tmp = cp_dir / "meta.json.tmp"
            tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            tmp.replace(cp_dir / "meta.json")      # a snapshot without meta.json is incomplete and never listed
        except BaseException:
            shutil.rmtree(cp_dir, ignore_errors=True)
            raise
        self._rotate(keep={cp_id})
        return cp_id

    def _rotate(self, keep: set[str]) -> None:
        """Deletes the oldest complete snapshots until all together fit the total limit; the new one and a pinned one stay."""
        metas = self.list_checkpoints()
        sizes = {m["checkpoint_id"]: int(m["bytes"]) if isinstance(m.get("bytes"), int) else self._dir_bytes(self.storage / m["checkpoint_id"])
                 for m in metas}
        total = sum(sizes.values())
        for m in metas:
            if total <= self.max_total_bytes:
                break
            cp_id = m["checkpoint_id"]
            if cp_id in keep or cp_id in self._pinned:
                continue
            shutil.rmtree(self.storage / cp_id, ignore_errors=True)
            total -= sizes[cp_id]
            log.info("checkpoint %s deleted to keep the stored snapshots under %d bytes", cp_id, self.max_total_bytes)

    @staticmethod
    def _dir_bytes(folder: Path) -> int:
        return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())

    def restore(self, checkpoint_id: str, delete_new: bool = True) -> bool:
        """Puts the workspace back as it was at `checkpoint_id`.

        Files created since are removed when `delete_new` is true, except those a snapshot never covers (excluded folders,
        files that were too large). The state before the rewind is saved first and named in `last_safety_checkpoint`."""
        if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.match(checkpoint_id):
            raise ValueError(f"Invalid checkpoint id: {checkpoint_id!r}")
        cp_dir = self.storage / checkpoint_id
        meta_file = cp_dir / "meta.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found")
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        files = meta.get("files", [])
        if not isinstance(files, list):
            raise ValueError("Checkpoint file list is damaged")
        rels = [self._checked_rel(r) for r in files]     # validate everything before touching anything
        for rel in rels:
            if not (cp_dir / "data" / rel).is_file():
                raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' is missing its copy of {rel.as_posix()}")
        snapshot = {r.as_posix() for r in rels}
        left_alone = {str(s.get("path")) for s in meta.get("skipped", []) if isinstance(s, dict)}

        self._pinned.add(checkpoint_id)
        try:
            self.last_safety_checkpoint = self.create(f"before rewind to {checkpoint_id}")
        finally:
            self._pinned.discard(checkpoint_id)

        for rel in rels:
            dest = self.workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cp_dir / "data" / rel, dest)

        if delete_new:
            for f, parts, size in list(self._walk()):
                rel_text = "/".join(parts)
                if rel_text not in snapshot and rel_text not in left_alone and size <= self.max_file_bytes:
                    f.unlink(missing_ok=True)
            for d in sorted((p for p in self.workspace.rglob("*") if p.is_dir() and not p.is_symlink()),
                            key=lambda p: len(p.parts), reverse=True):
                parts = d.relative_to(self.workspace).parts
                if self._inside_storage(d) or self._excluded(parts) or _git_index(parts) is not None:
                    continue
                try:
                    d.rmdir()          # only empty folders go
                except OSError:
                    pass
        return True

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """The complete checkpoints, oldest first."""
        out: List[Dict[str, Any]] = []
        if not self.storage.is_dir():
            return out
        for d in self.storage.iterdir():
            meta_file = d / "meta.json"
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(meta, dict) and isinstance(meta.get("checkpoint_id"), str):
                    out.append(meta)
        return sorted(out, key=lambda m: (m.get("timestamp", 0), len(m["checkpoint_id"]), m["checkpoint_id"]))
