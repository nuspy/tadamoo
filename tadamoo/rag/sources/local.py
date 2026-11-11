"""Sources on this PC: one file, or a folder of files. A change is seen from the modification time and the size (one stat per file); when they moved, the file is
hashed, and a file whose content is the same (touched, copied back) is not read again."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError
from tadamoo.rag.sources.model import Fetched, Provider, Remote, SourceError, doc_name, need

MAX_FILES = 1000


def sha_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1 << 20), b""):
            sha.update(part)
    return sha.hexdigest()


def _inside(path: Path, roots: List[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _stamp(path: Path) -> Dict[str, Any]:
    st = path.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _copy(src: Path, into: Path, name: str, remote: Remote) -> Fetched:
    """A snapshot of the file in `into`: what is read is what was hashed, even if the original changes meanwhile."""
    into.mkdir(parents=True, exist_ok=True)
    target = into / ("f" + hashlib.sha1(remote.key.encode("utf-8")).hexdigest()[:10] + Path(name).suffix.lower())
    try:
        stamp = _stamp(src)
        shutil.copyfile(src, target)
    except OSError as e:
        raise SourceError(f"{src}: {e.strerror or e}", "offline") from None
    return Fetched(target, name, sha_of(target), target.stat().st_size, stamp, str(src), version="")


class FileProvider(Provider):
    kind = "file"
    keys = ["mtime_ns", "size"]

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        raw = need(config, "path")
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise RagError("path: a full path without '..'")
        real = path.resolve()
        if not real.is_file():
            raise RagError("there is no such file")
        if _inside(real, self.ctx.forbidden):
            raise RagError("that file is inside the TaDaMoo data folder and cannot be a source")
        if real.suffix.lower() not in self.ctx.formats():
            raise RagError(f"{real.suffix or 'a file without an extension'} is not a supported format: use " + ", ".join(self.ctx.formats()))
        return {"path": str(real)}

    def label(self, config: Dict[str, Any]) -> str:
        return Path(config["path"]).name

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        path = Path(config["path"])
        if not path.is_file():
            return [Remote(path.name, missing=True, note="the file is not there now (moved, renamed or deleted)", origin=str(path))]
        try:
            return [Remote(path.name, doc_name(path.stem, path.suffix.lower()), _stamp(path), path.stat().st_size, origin=str(path))]
        except OSError as e:
            raise SourceError(f"{path}: {e.strerror or e}", "offline") from None

    def confirm(self, config: Dict[str, Any], remote: Remote, doc: Dict[str, Any]) -> Optional[bool]:
        try:
            return sha_of(Path(remote.origin)) != doc.get("sha256")
        except OSError:
            return None

    def fetch(self, config: Dict[str, Any], remote: Remote, into: Path) -> Fetched:
        return _copy(Path(remote.origin), into, remote.name, remote)


class FolderProvider(FileProvider):
    kind = "folder"

    def validate(self, config: Dict[str, Any]) -> Dict[str, Any]:
        path = Path(need(config, "path"))
        if not path.is_absolute() or ".." in path.parts:
            raise RagError("path: a full path without '..'")
        real = path.resolve()
        if not real.is_dir():
            raise RagError("there is no such folder")
        if _inside(real, self.ctx.forbidden):
            raise RagError("that folder is inside the TaDaMoo data folder and cannot be a source")
        recursive = config.get("recursive", True)
        if not isinstance(recursive, bool):
            raise RagError("recursive: true or false")
        return {"path": str(real), "recursive": recursive}

    def label(self, config: Dict[str, Any]) -> str:
        return Path(config["path"]).name or config["path"]

    def list(self, config: Dict[str, Any], known: Dict[str, Dict[str, Any]], cancel: Optional[Callable[[], bool]] = None) -> List[Remote]:
        root = Path(config["path"])
        if not root.is_dir():
            raise SourceError(f"{root}: the folder is not there now", "offline")
        allowed, limit = set(self.ctx.formats()), self.ctx.document_bytes
        out: List[Remote] = []
        for here, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not _inside(Path(here, d).resolve(), self.ctx.forbidden)) if config.get("recursive", True) else []
            for name in sorted(files):
                path = Path(here, name)
                ext = path.suffix.lower()
                if name.startswith(".") or ext not in allowed:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not 0 < st.st_size <= limit:
                    continue
                rel = path.relative_to(root).as_posix()
                out.append(Remote(rel, doc_name(rel[: -len(ext)], ext), {"mtime_ns": st.st_mtime_ns, "size": st.st_size}, st.st_size, origin=str(path)))
        if len(out) > MAX_FILES:
            raise SourceError(f"the folder holds {len(out):,} files of a readable kind; an archive holds at most {MAX_FILES:,} documents: choose a smaller folder", "too_big")
        return out
