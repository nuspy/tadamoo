"""Pieces every kind of import shares: the problems it reports, reading a source folder without trusting it, the manifest, and the
transaction that leaves nothing half-written.

Nothing here runs imported code: sources are only read, hashed and copied."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.merge.session import Unit  # noqa: F401  (what an update merges: the kinds make them)

NAME =re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".work"})
SKIP_FILES = frozenset({".DS_Store", "Thumbs.db"})
MAX_FILES = 2000
MAX_BYTES = 100 << 20
MAX_FILE = 50 << 20
DIFF_FILE = 200_000
LIST_FILES = 60
REPARSE_POINT = 0x400


class Problem(ValueError):
    """Why something cannot be done, in words for the person; `code` is for the page and `data` carries what it may show."""

    def __init__(self, code: str, message: str, **data: Any) -> None:
        super().__init__(message)
        self.code, self.data = code, data


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise Problem("bad_name", f"The {what} '{str(name)[:40]}' is not usable: letters, digits, dot, dash and underscore only, 64 at most.")
    return name


def is_link(path: Path) -> bool:
    """A symbolic link or, on Windows, a junction or any other reparse point: never followed."""
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return True


def safe_join(base: Path, rel: Any) -> Path:
    """`rel` inside `base`, or a Problem: no absolute path, no drive, no `..`, nothing that resolves outside."""
    text = str(rel or "")
    parts = re.split(r"[\\/]+", text)
    if not text or Path(text).is_absolute() or re.match(r"^[A-Za-z]:", text) or text.startswith(("/", "\\")) or ".." in parts:
        raise Problem("outside", f"'{text[:80]}' points outside the folder it belongs to.")
    target = (base / text).resolve()
    if not target.is_relative_to(base.resolve()):
        raise Problem("outside", f"'{text[:80]}' points outside the folder it belongs to.")
    return target


@dataclass
class Tree:
    """What a source folder holds, as it will be copied: relative path to hash."""
    root: Path
    files: Dict[str, str] = field(default_factory=dict)
    sizes: Dict[str, int] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return sha_text("\n".join(f"{rel}\0{self.files[rel]}" for rel in sorted(self.files)))

    @property
    def total(self) -> int:
        return sum(self.sizes.values())


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan_tree(root: Path) -> Tree:
    """Reads a folder: every regular file under the limits, links and tool folders left out (links are listed in `skipped`)."""
    root = Path(root)
    tree = Tree(root)
    if not root.is_dir() or is_link(root):
        raise Problem("no_source", f"There is no folder at {root}.")
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        keep = []
        for d in sorted(dirs):
            if d in SKIP_DIRS:
                continue
            if is_link(here / d):
                tree.skipped.append((here / d).relative_to(root).as_posix() + " (link)")
                continue
            keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            p = here / n
            rel = p.relative_to(root).as_posix()
            if n in SKIP_FILES or n.endswith(".pyc"):
                continue
            if is_link(p) or not p.is_file():
                tree.skipped.append(rel + " (link)")
                continue
            size = p.stat().st_size
            if size > MAX_FILE:
                raise Problem("too_big", f"{rel} is larger than {MAX_FILE >> 20} MB: it is not a skill or a plugin.")
            tree.files[rel], tree.sizes[rel] = _file_sha(p), size
            if len(tree.files) > MAX_FILES or tree.total > MAX_BYTES:
                raise Problem("too_big", f"The folder holds more than {MAX_FILES} files or {MAX_BYTES >> 20} MB: it is not a skill or a plugin.")
    if not tree.files:
        raise Problem("empty", "The folder holds no files to copy.")
    return tree


def copy_tree(tree: Tree, dest: Path) -> None:
    """Copies what `scan_tree` found into a new folder and checks the copy against the hash taken: a source that changed meanwhile is refused."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in sorted(tree.files):
        target = safe_join(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree.root / rel, target)
    if scan_tree(dest).hash != tree.hash:
        raise Problem("changed_meanwhile", "The source changed while it was being copied. Nothing was imported; try again.")


def diff_trees(old: Tree, new: Tree) -> Dict[str, Any]:
    """The short account of what a re-import would change: files added, removed and changed (with the lines added and removed in text files)."""
    added = sorted(set(new.files) - set(old.files))
    removed = sorted(set(old.files) - set(new.files))
    changed = []
    for rel in sorted(set(old.files) & set(new.files)):
        if old.files[rel] == new.files[rel]:
            continue
        row: Dict[str, Any] = {"file": rel}
        try:
            if old.sizes[rel] <= DIFF_FILE and new.sizes[rel] <= DIFF_FILE:
                row.update(line_delta((old.root / rel).read_text(encoding="utf-8"), (new.root / rel).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            pass
        changed.append(row)
    return {"added": added[:LIST_FILES], "removed": removed[:LIST_FILES], "changed": changed[:LIST_FILES],
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}}


def line_delta(old: str, new: str) -> Dict[str, int]:
    lines = [x for x in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0) if x[:1] in "+-" and x[:3] not in ("+++", "---")]
    return {"plus": sum(1 for x in lines if x[0] == "+"), "minus": sum(1 for x in lines if x[0] == "-")}


def same(diff: Dict[str, Any]) -> bool:
    return not any(diff["counts"].values())


class Manifest:
    """What was imported: `items` keyed `kind:name`, in one JSON file written whole and atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(items, dict) else {}

    def save(self, items: Dict[str, Dict[str, Any]]) -> None:
        write_json(self.path, {"version": 1, "items": items})


def write_json(path: Path, data: Any) -> None:
    """Writes a JSON file so that a reader sees the old one or the new one, never part of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def replace(src: Any, dst: Any) -> None:
    """`os.replace` that waits a little when Windows refuses because something has the target open for a moment."""
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


class Txn:
    """The changes of one import, re-import or delete: if any step fails, what was done is undone in reverse order; if all succeed, what was
    set aside (the old copies) is thrown away."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self._undo: List[Callable[[], None]] = []
        self._commit: List[Callable[[], None]] = []

    def on_undo(self, fn: Callable[[], None]) -> None:
        self._undo.append(fn)

    def on_commit(self, fn: Callable[[], None]) -> None:
        self._commit.append(fn)

    def temp(self, label: str) -> Path:
        self.work.mkdir(parents=True, exist_ok=True)
        return self.work / f"{label}-{uuid.uuid4().hex[:8]}"

    def rollback(self) -> None:
        for fn in reversed(self._undo):
            try:
                fn()
            except Exception:                                    # undoing goes on to the next step whatever happens
                pass
        self._undo.clear()
        self._commit.clear()

    def commit(self) -> None:
        for fn in self._commit:
            try:
                fn()
            except Exception:
                pass
        self._undo.clear()
        self._commit.clear()

    # -- steps ---------------------------------------------------------------------------------------------

    def install_dir(self, staged: Path, final: Path) -> None:
        """Puts a folder built in the work area at `final`, the one that was there (if any) set aside until the end."""
        final.parent.mkdir(parents=True, exist_ok=True)
        aside: Optional[Path] = None
        if final.exists():
            aside = self.temp("old")
            shutil.move(str(final), str(aside))
        try:
            replace(staged, final)
        except BaseException:
            if aside is not None:
                shutil.move(str(aside), str(final))
            raise

        def undo() -> None:
            shutil.rmtree(final, ignore_errors=True)
            if aside is not None:
                shutil.move(str(aside), str(final))
        self.on_undo(undo)
        if aside is not None:
            self.on_commit(lambda: shutil.rmtree(aside, ignore_errors=True))

    def remove_dir(self, folder: Path) -> None:
        if not folder.exists():
            return
        aside = self.temp("gone")
        shutil.move(str(folder), str(aside))
        self.on_undo(lambda: shutil.move(str(aside), str(folder)))
        self.on_commit(lambda: shutil.rmtree(aside, ignore_errors=True))

    def backup_dir(self, folder: Path) -> None:
        """Keeps a copy of a folder that another store's code is about to rewrite; undoing puts it back as it was."""
        if not folder.exists():
            self.on_undo(lambda: shutil.rmtree(folder, ignore_errors=True))
            return
        keep = self.temp("keep")
        shutil.copytree(folder, keep)

        def undo() -> None:
            shutil.rmtree(folder, ignore_errors=True)
            shutil.move(str(keep), str(folder))
        self.on_undo(undo)
        self.on_commit(lambda: shutil.rmtree(keep, ignore_errors=True))

    def write_file(self, path: Path, data: Any) -> None:
        """Writes a JSON file; undoing restores what it held (or removes it)."""
        before = path.read_bytes() if path.is_file() else None
        write_json(path, data)

        def undo() -> None:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
        self.on_undo(undo)

    def remove_file(self, path: Path) -> None:
        if not path.is_file():
            return
        before = path.read_bytes()
        path.unlink()
        self.on_undo(lambda: path.write_bytes(before))


class Lock:
    """One import operation at a time in this process."""
    _lock = threading.RLock()

    def __enter__(self) -> "Lock":
        self._lock.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lock.release()


@dataclass
class Snapshot:
    """A source, read and not yet copied: everything the dialog shows and everything the copy needs."""
    kind: str
    name: str
    source: str
    hash: str
    version: str = ""
    description: str = ""
    tree: Optional[Tree] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    parts: List["Snapshot"] = field(default_factory=list)
    dependencies: List[Dict[str, Any]] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    original: Optional["Snapshot"] = None            # for a snapshot made from a merge: the source as it was read, which is what is kept as the original

    def public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "name": self.name, "version": self.version, "description": self.description[:400], "source": self.source,
                               "hash": self.hash, "warnings": list(self.warnings), **self.extra}
        if self.tree is not None:
            out.update(files=len(self.tree.files), bytes=self.tree.total, file_list=sorted(self.tree.files)[:LIST_FILES], skipped=self.tree.skipped[:20])
        if self.parts:
            out["parts"] = [{"kind": p.kind, "name": p.name, "description": p.description[:200]} for p in self.parts]
        if self.dependencies:
            out["dependencies"] = self.dependencies
        return out
