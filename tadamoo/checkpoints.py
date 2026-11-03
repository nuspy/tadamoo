"""File checkpoint and rewind manager.

A checkpoint keeps the previous content of the files an action is about to change, from any folder: the first time a
checkpoint is told about a file it copies the file as it is (or notes that the file did not exist), and a restore puts each
file back or removes the ones that were created. There is no size limit, no total limit and no rotation: only the files that
change are copied, and deleting checkpoints is the person's choice. Snapshots live outside the workspace, so the agent's own
tools cannot reach them; a restore first takes a safety checkpoint of the files it is about to overwrite, so even a rewind can
be undone.

What is covered is what the action names: the path of a file tool, and the existing files and redirect targets a shell command's
text names. A change a shell command makes to something its text does not name (a folder, a file found by a program) is not
covered.

Checkpoints made by the former whole-folder design (a `files` list of relative paths and a `data` folder) are still listed and
restored.
"""
from __future__ import annotations

import filecmp
import glob
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

log = logging.getLogger("tadamoo.checkpoints")

MODES = ("on", "soft", "off")
_CHECKPOINT_ID = re.compile(r"^cp_\d+_\d+$")
FILE_PATH_TOOLS = ("write_file", "edit_file", "delete_file", "move_file")
_LEGACY_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", ".tadamoo_checkpoints", ".tadamoo", "build", "dist", ".git/objects")
_LEGACY_GIT_KEPT = ("config", "hooks")
_NULL_TARGETS = ("nul", "/dev/null", "$null")


def _data_root() -> Path:
    try:
        from serve.userdata import user_data_dir
        return user_data_dir()
    except Exception:  # the harness also runs without the server package
        return Path(tempfile.gettempdir()) / "tadamoo"


def default_storage(workspace: Path) -> Path:
    return _data_root() / "checkpoints" / hashlib.sha1(str(workspace).lower().encode("utf-8")).hexdigest()[:16]


def settings_file() -> Path:
    return _data_root() / "checkpoints.json"


def read_mode(path: Optional[Path] = None) -> str:
    """The person's choice: "on", "soft" or "off"; "" when none is made, which follows the permission level."""
    try:
        value = json.loads((path or settings_file()).read_text(encoding="utf-8")).get("mode", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if value in MODES else ""


def write_mode(mode: str, path: Optional[Path] = None) -> str:
    """Keeps the choice; "" or "default" goes back to following the permission level."""
    if mode in ("", "default", None):
        mode = ""
    if mode not in MODES + ("",):
        raise ValueError(f"checkpoint mode must be one of {', '.join(MODES)} or default, not {mode!r}")
    target = path or settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    tmp.replace(target)
    return mode


def resolve_mode(mode: str, level: str) -> str:
    """The position that applies: the person's choice, or by default `soft` at the automatic level and `on` otherwise."""
    if mode in MODES:
        return mode
    return "soft" if level == "auto" else "on"


# -- which files an action names -------------------------------------------------------------------------------


def _tokens(command: str) -> Iterator[Tuple[str, bool, bool]]:
    """(word, is_redirect_target, is_program) of a shell command's text: quotes removed, operators split off; the program is the first word of a statement."""
    escapes = os.name != "nt"
    buf: List[str] = []
    quote = ""
    redirect = False
    quoted = False
    fresh = True
    i = 0

    def flush() -> Optional[Tuple[str, bool, bool]]:
        nonlocal redirect, quoted, fresh
        word = "".join(buf)
        buf.clear()
        was_quoted, quoted = quoted, False
        if not word and not was_quoted:
            return None
        if word.isdigit() and not was_quoted:
            return None
        out = (word, redirect, fresh and not redirect)
        redirect = False
        fresh = False
        return out

    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = ""
            else:
                buf.append(c)
        elif c in "'\"":
            quote, quoted = c, True
        elif escapes and c == "\\" and i + 1 < len(command):
            i += 1
            buf.append(command[i])
        elif c in " \t\r\n;|&(){}":
            item = flush()
            if item:
                yield item
            if c not in " \t":
                fresh = True
        elif c in "<>":
            item = flush()
            if item:
                yield item
            redirect = c == ">"
            while i + 1 < len(command) and command[i + 1] in "<>&":
                i += 1
        else:
            buf.append(c)
        i += 1
    item = flush()
    if item:
        yield item


def shell_paths(workspace: Path, command: str) -> List[Path]:
    """The existing files a command's text names (a pattern gives the files it matches) and the files it redirects output to."""
    found: Dict[str, Path] = {}

    def keep(p: Path) -> None:
        found.setdefault(os.path.normcase(str(p)), p)

    for word, redirect, program in _tokens(command):
        if program or "\x00" in word or word.lower() in _NULL_TARGETS:
            continue
        candidates = [word]
        if "=" in word and word.startswith("-"):
            candidates.append(word.split("=", 1)[1])
        for text in candidates:
            if not text or text.startswith("-"):
                continue
            try:
                expanded = os.path.expanduser(text)
                full = Path(expanded) if os.path.isabs(expanded) else workspace / expanded
                if any(ch in text for ch in "*?["):
                    for match in glob.glob(str(full), recursive=True):
                        m = Path(match)
                        if m.is_file() and not m.is_symlink():
                            keep(m.resolve())
                    continue
                if full.is_file():
                    keep(full.resolve())
                elif redirect and not full.exists():
                    keep(full.parent.resolve() / full.name)
            except (OSError, ValueError, RuntimeError):
                continue
    return list(found.values())


def call_paths(workspace: Path, name: str, args: Any) -> List[Path]:
    """The files a state-changing call is about to change, as far as its arguments say."""
    if not isinstance(args, dict):
        return []
    if name in FILE_PATH_TOOLS:
        paths = [args.get(k) for k in ("path", "source", "destination")]
        out = []
        for value in paths:
            if isinstance(value, str) and value and "\x00" not in value:
                p = Path(os.path.expanduser(value))
                try:
                    out.append((p if p.is_absolute() else workspace / p).resolve())
                except (OSError, ValueError, RuntimeError):
                    continue
        return out
    if name == "shell_command" and isinstance(args.get("command"), str):
        return shell_paths(workspace, args["command"])
    return []


# -- the manager -------------------------------------------------------------------------------------------------


class CheckpointManager:
    """`workspace_dir` is where relative paths resolve and where legacy checkpoints restore to; a checkpoint may still hold files
    from any folder. `mode` fixes the position ("on", "soft", "off") instead of reading the person's setting (`settings`, by
    default the file in the user data folder) each time `effective_mode` is asked."""

    def __init__(self, workspace_dir: str | Path, storage_dir: Optional[str | Path] = None, *, mode: Optional[str] = None,
                 settings: Optional[str | Path] = None) -> None:
        self.workspace = Path(workspace_dir).resolve()
        self.storage = Path(storage_dir).resolve() if storage_dir is not None else default_storage(self.workspace)
        if self.storage == self.workspace or self.workspace in self.storage.parents:
            raise ValueError("checkpoint storage must not sit inside the workspace the agent can write to")
        self.storage.mkdir(parents=True, exist_ok=True)
        if mode is not None and mode not in MODES:
            raise ValueError(f"checkpoint mode must be one of {', '.join(MODES)}, not {mode!r}")
        self.mode = mode
        self.settings = Path(settings) if settings is not None else None
        self._seq = 0
        self._lock = threading.Lock()
        self.last_safety_checkpoint: Optional[str] = None

    def effective_mode(self, level: str = "default") -> str:
        return resolve_mode(self.mode if self.mode is not None else read_mode(self.settings), level)

    def _inside_storage(self, p: Path) -> bool:
        try:
            p.relative_to(self.storage)
            return True
        except ValueError:
            return False

    # -- identity and storage -------------------------------------------------------------------------------

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

    @staticmethod
    def _valid_id(checkpoint_id: Any) -> str:
        if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.match(checkpoint_id):
            raise ValueError(f"Invalid checkpoint id: {checkpoint_id!r}")
        return checkpoint_id

    def _load(self, checkpoint_id: str) -> Dict[str, Any]:
        meta_file = self.storage / self._valid_id(checkpoint_id) / "meta.json"
        if not meta_file.is_file():
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found")
        return json.loads(meta_file.read_text(encoding="utf-8"))

    def _save(self, cp_dir: Path, meta: Dict[str, Any]) -> None:
        tmp = cp_dir / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        tmp.replace(cp_dir / "meta.json")      # a checkpoint without meta.json is incomplete and never listed

    # -- create, add ----------------------------------------------------------------------------------------

    def create(self, description: str = "", paths: Iterable[str | Path] = ()) -> str:
        """A new checkpoint holding the current state of `paths`."""
        cp_id, cp_dir = self._new_dir()
        try:
            (cp_dir / "files").mkdir()
            self._save(cp_dir, {"checkpoint_id": cp_id, "timestamp": time.time(), "description": description, "entries": [], "bytes": 0})
            self.add(cp_id, paths)
        except BaseException:
            shutil.rmtree(cp_dir, ignore_errors=True)
            raise
        return cp_id

    def add(self, checkpoint_id: str, paths: Iterable[str | Path]) -> int:
        """Keeps the current state of each file not yet in the checkpoint (the first state wins); returns how many were added."""
        cp_dir = self.storage / self._valid_id(checkpoint_id)
        with self._lock:
            meta = self._load(checkpoint_id)
            entries: List[Dict[str, Any]] = meta.setdefault("entries", [])
            known = {os.path.normcase(e["path"]) for e in entries}
            added = 0
            for raw in paths:
                path = Path(raw)
                if not path.is_absolute():
                    path = self.workspace / path
                path = path.resolve()
                key = os.path.normcase(str(path))
                if key in known or self._inside_storage(path):
                    continue
                entry: Dict[str, Any] = {"path": str(path), "existed": False, "stored": "", "bytes": 0}
                if path.is_file():
                    stored = f"f{len(entries) + 1}"
                    shutil.copy2(path, cp_dir / "files" / stored)
                    entry.update(existed=True, stored=stored, bytes=path.stat().st_size)
                elif path.exists():
                    continue      # a folder or a device: nothing to keep
                entries.append(entry)
                known.add(key)
                meta["bytes"] = int(meta.get("bytes", 0)) + entry["bytes"]
                added += 1
            if added:
                self._save(cp_dir, meta)
        return added

    # -- restore --------------------------------------------------------------------------------------------

    def restore(self, checkpoint_id: str, delete_new: bool = True) -> bool:
        """Puts the workspace back as it was when `checkpoint_id` was taken: the files that checkpoint and every later one kept return to
        their earliest kept state; files that did not exist then are removed when `delete_new` is true.

        The state before the restore is saved first and named in `last_safety_checkpoint`."""
        meta = self._load(checkpoint_id)
        if "entries" not in meta:
            return self._restore_legacy(checkpoint_id, meta, delete_new)
        ids = [m["checkpoint_id"] for m in self.list_checkpoints()]
        later = ids[ids.index(checkpoint_id):] if checkpoint_id in ids else [checkpoint_id]
        checked: List[Tuple[Path, Dict[str, Any], Path]] = []
        seen = set()
        for cp_id in later:
            cp_meta = meta if cp_id == checkpoint_id else self._load(cp_id)
            if "entries" not in cp_meta:
                continue
            entries = cp_meta["entries"]
            if not isinstance(entries, list):
                raise ValueError("Checkpoint entry list is damaged")
            cp_dir = self.storage / cp_id
            for e in entries:        # validate everything before touching anything
                raw = e.get("path") if isinstance(e, dict) else None
                if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
                    raise ValueError(f"Checkpoint entry {raw!r} is not an absolute path")
                path = Path(raw)
                if self._inside_storage(path):
                    raise ValueError(f"Checkpoint entry '{raw}' points into the checkpoint storage")
                if path in seen:
                    continue
                if e.get("existed") and not (cp_dir / "files" / str(e.get("stored", ""))).is_file():
                    raise FileNotFoundError(f"Checkpoint '{cp_id}' is missing its copy of {raw}")
                seen.add(path)
                checked.append((path, e, cp_dir))
        self.last_safety_checkpoint = self.create(f"before rewind to {checkpoint_id}", [p for p, _, _ in checked])
        problems: List[str] = []
        for path, e, cp_dir in checked:
            try:
                if e.get("existed"):
                    stored = cp_dir / "files" / e["stored"]
                    if path.is_file() and filecmp.cmp(stored, path, shallow=False):        # already as it was: a file in use is not rewritten
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stored, path)
                elif delete_new and path.is_file():
                    path.unlink()
                    self._drop_empty_parents(path.parent)
            except OSError as exc:
                problems.append(f"{path}: {exc}")
        if problems:
            raise OSError("Some files could not be put back: " + "; ".join(problems))
        return True

    def _drop_empty_parents(self, folder: Path) -> None:
        """A folder a rewound file was created in goes with it when it is empty, never the workspace itself or above."""
        while folder != self.workspace and self.workspace in folder.parents:
            try:
                folder.rmdir()
            except OSError:
                return
            folder = folder.parent

    # -- delete, list ---------------------------------------------------------------------------------------

    def delete(self, checkpoint_id: str) -> bool:
        """Removes one checkpoint; False when there is none by that id."""
        cp_dir = self.storage / self._valid_id(checkpoint_id)
        if not cp_dir.is_dir():
            return False
        shutil.rmtree(cp_dir)
        return True

    def delete_all(self) -> int:
        """Removes every checkpoint; returns how many."""
        count = 0
        if self.storage.is_dir():
            for d in list(self.storage.iterdir()):
                if d.is_dir() and _CHECKPOINT_ID.match(d.name):
                    shutil.rmtree(d, ignore_errors=True)
                    count += 1
        return count

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

    @staticmethod
    def summary(meta: Dict[str, Any]) -> Dict[str, Any]:
        """The short form a list shows: id, time, description, the files kept (path and whether it existed) and the bytes."""
        if "entries" in meta:
            files = [{"path": e.get("path", ""), "existed": bool(e.get("existed"))} for e in meta["entries"] if isinstance(e, dict)]
        else:
            files = [{"path": str(f), "existed": True} for f in meta.get("files", [])]
        return {"id": meta["checkpoint_id"], "time": meta.get("timestamp", 0), "description": meta.get("description", ""),
                "files": files, "bytes": int(meta.get("bytes", 0) or 0), "legacy": "entries" not in meta}

    # -- checkpoints of the former whole-folder design -------------------------------------------------------

    def _legacy_walk(self) -> Iterator[Tuple[Path, str]]:
        excluded = [tuple(e.split("/")) for e in _LEGACY_EXCLUDES]
        for folder, dirs, names in os.walk(self.workspace):
            base = Path(folder)
            rel = base.relative_to(self.workspace).parts

            def skip(parts: Tuple[str, ...], is_dir: bool) -> bool:
                if any(tuple(parts[i:i + len(e)]) == e for e in excluded for i in range(len(parts) - len(e) + 1)):
                    return True
                if ".git" in parts:
                    rest = parts[parts.index(".git") + 1:]
                    return bool(rest) and rest[0] != "hooks" and not (rest == ("config",) and not is_dir)
                return False

            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink() and not self._inside_storage(base / d) and not skip(rel + (d,), True))
            for name in sorted(names):
                full = base / name
                if full.is_file() and not full.is_symlink() and not skip(rel + (name,), False):
                    yield full, "/".join(rel + (name,))

    def _restore_legacy(self, checkpoint_id: str, meta: Dict[str, Any], delete_new: bool) -> bool:
        cp_dir = self.storage / checkpoint_id
        files = meta.get("files", [])
        if not isinstance(files, list):
            raise ValueError("Checkpoint file list is damaged")
        targets: List[Tuple[Path, Path]] = []
        for rel_text in files:
            rel = Path(rel_text) if isinstance(rel_text, str) and rel_text and "\x00" not in rel_text else None
            target = (self.workspace / rel).resolve() if rel is not None else None
            if rel is None or rel.is_absolute() or rel.drive or ".." in rel.parts or self.workspace not in target.parents:
                raise ValueError(f"Checkpoint entry {rel_text!r} points outside the workspace")
            if not (cp_dir / "data" / rel).is_file():
                raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' is missing its copy of {rel.as_posix()}")
            targets.append((cp_dir / "data" / rel, target))
        snapshot = {t for _, t in targets}
        left_alone = {str(s.get("path")) for s in meta.get("skipped", []) if isinstance(s, dict)}
        doomed = [f for f, rel in self._legacy_walk() if delete_new and f.resolve() not in snapshot and rel not in left_alone]
        self.last_safety_checkpoint = self.create(f"before rewind to {checkpoint_id}", [t for _, t in targets] + doomed)
        for src, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        for f in doomed:
            f.unlink(missing_ok=True)
            self._drop_empty_parents(f.parent)
        return True
