"""The check of an engines Python chosen in a page: the path is kept, and later started, only when it names a Python interpreter on a local
disk. A network path, a file with another name, a missing file, a link that loops and a file in a folder anyone may write are refused."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

NAME = re.compile(r"(python[0-9.]*|py)(\.exe)?", re.IGNORECASE)


def _network(path: str) -> bool:
    return path.startswith(("\\\\", "//", "\\/", "/\\"))


def _world_writable(folder: Path) -> bool:
    """A folder any user may write in (POSIX mode bits; Windows ACLs are not read)."""
    if os.name == "nt":
        return False
    try:
        return bool(folder.stat().st_mode & stat.S_IWOTH)
    except OSError:
        return False


def checked_python(path: str) -> str:
    """The path as given (quotes and spaces around it removed), "" for "", or ValueError with a plain reason."""
    path = (path or "").strip().strip('"').strip()
    if not path:
        return ""
    if _network(path):
        raise ValueError("the engines Python must be on a disk of this PC, not on a network path")
    if not os.path.isabs(path) or (os.name == "nt" and not re.match(r"[A-Za-z]:[\\/]", path)):
        raise ValueError("give the full path of the Python, starting with the drive")
    if not NAME.fullmatch(Path(path).name):
        raise ValueError("the file must be a Python interpreter (python, python3, python.exe, py.exe)")
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("give the full path of a Python executable that exists") from None
    if _network(str(real)):
        raise ValueError("the engines Python must be on a disk of this PC, not on a network path")
    if not real.is_file() or not NAME.fullmatch(real.name):
        raise ValueError("give the full path of a Python executable that exists")
    if _world_writable(Path(path).parent) or _world_writable(real.parent):
        raise ValueError("the engines Python is in a folder anyone may write in: choose one in a folder of your own")
    return path
