"""Moving the folder that holds the conversations' files: a checked copy, the original left where it is, and the new place kept only after the copy was verified.

`<base>/sessions/` holds one folder per conversation plus the records shared by all of them (the list, the compared answers, the message nodes).
They move together, because the shared records point at the conversations' folders by id. A running conversation has its folder open, so
nothing is moved while one runs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from serve import userdata

SUBDIR = "sessions"
STAGING = ".sessions-copying"


class MoveError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _inside(inner: Path, outer: Path) -> bool:
    inner, outer = os.path.normcase(os.path.abspath(inner)), os.path.normcase(os.path.abspath(outer))
    return inner == outer or inner.startswith(outer.rstrip("\\/") + os.sep)


def _files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mentions(files: List[Path], place: Path) -> int:
    """How many files name the old folder by its full path (the plain and the JSON-escaped spelling): those would still point at the original."""
    needles = {str(place), json.dumps(str(place))[1:-1]}
    count = 0
    for f in files:
        if f.suffix.lower() not in (".json", ".jsonl", ".md", ".txt") or f.stat().st_size > 20_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        count += any(n in text for n in needles)
    return count


def inspect(source_base: Path, dest_base: Path, live: int) -> Dict[str, Any]:
    """What a move to `dest_base` would copy and what stands in the way; nothing is written."""
    source, dest = Path(source_base) / SUBDIR, Path(dest_base) / SUBDIR
    problems: List[Tuple[str, str]] = []
    if not Path(dest_base).is_absolute():
        problems.append(("relative", "Give a full path, with the drive or the root."))
    if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(dest)):
        problems.append(("same", "The conversations are already kept there."))
    if _inside(dest_base, source) or _inside(source, dest):
        problems.append(("nested", "The new folder cannot be inside the conversations' folder, nor hold it."))
    if dest.exists() and any(dest.iterdir()):
        problems.append(("not_empty", f"{dest} already holds files."))
    if live:
        problems.append(("active", f"{live} conversation(s) are running: close them first."))
    files = _files(source) if source.is_dir() else []
    out = {"from": str(source), "to": str(dest), "files": len(files), "bytes": sum(f.stat().st_size for f in files),
           "paths_inside": _mentions(files, source) if files and not problems else 0, "problems": [{"code": c, "message": m} for c, m in problems]}
    out["ok"] = not problems
    return out


def move(source_base: Path, dest_base: Path, live: int) -> Dict[str, Any]:
    """Copies `<source>/sessions` to `<dest>/sessions`, checks every file by size and hash, and only then keeps `dest_base` as the new base."""
    report = inspect(source_base, dest_base, live)
    if not report["ok"]:
        first = report["problems"][0]
        raise MoveError(first["code"], first["message"])
    source, dest = Path(source_base) / SUBDIR, Path(dest_base) / SUBDIR
    staging = Path(dest_base) / STAGING
    try:
        if staging.exists():
            shutil.rmtree(staging)
        Path(dest_base).mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, staging)
        else:
            staging.mkdir()
        for original in _files(source) if source.is_dir() else []:
            copy = staging / original.relative_to(source)
            if not copy.is_file() or copy.stat().st_size != original.stat().st_size or _digest(copy) != _digest(original):
                raise MoveError("verify", f"{original.name} was not copied exactly; nothing was changed.")
        if dest.exists():
            dest.rmdir()
        os.replace(staging, dest)
    except MoveError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise MoveError("io", f"The copy failed: {e}") from e
    default = userdata.path("journal")
    userdata.set_sessions_base(None if os.path.normcase(os.path.abspath(dest_base)) == os.path.normcase(os.path.abspath(default)) else Path(dest_base))
    return {**report, "ok": True, "restart": True, "kept": str(source)}
