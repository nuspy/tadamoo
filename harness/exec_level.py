"""How freely the code the model writes runs: contained, free, or as administrator.

`contained` keeps a command inside a memory and process cap and a time cap, hides the PC's secrets from its environment, refuses the
programs on the deny list and keeps the person's secret files out of reach. `free` runs it with the person's own rights on files and
network, with no cap, with the vault's values in its environment, and nothing refused here: the permission level in front of the
tool is the only gate. `admin` is `free` run elevated. The choice is the person's, kept in one file that a running conversation
reads again when it changes. Where nothing is configured (a library caller, a test) the level is `contained`.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional

LEVELS = ("contained", "free", "admin")
DEFAULT = "free"
FILE = "exec-level.json"

_source: Optional[Callable[[], str]] = None


class Prefs:
    """The person's choice, in a small file; a missing or damaged file is the default."""

    def __init__(self, path: str | Path, default: str = DEFAULT) -> None:
        self.path, self.default = Path(path), default
        self._lock = threading.Lock()
        self._stamp: Optional[tuple] = None
        self._value = default

    def read(self) -> str:
        with self._lock:
            try:
                st = self.path.stat()
            except OSError:
                self._stamp, self._value = None, self.default
                return self._value
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8")).get("level")
                except (OSError, ValueError, AttributeError):
                    value = None
                self._stamp, self._value = stamp, (value if value in LEVELS else self.default)
            return self._value

    def write(self, level: str) -> str:
        if level not in LEVELS:
            raise ValueError(f"level: one of {', '.join(LEVELS)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"level": level}), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.read()


def configure(source: Optional[Callable[[], str] | str | Path]) -> None:
    """Sets where this process learns the level: a function, a file, or None for contained."""
    global _source
    if source is None or callable(source):
        _source = source
    else:
        _source = Prefs(source).read


def current() -> str:
    level = _source() if _source is not None else "contained"
    return level if level in LEVELS else "contained"


def is_elevated() -> bool:
    """Whether this process runs as administrator (Windows) or root; code it starts has the same rights."""
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def contained() -> bool:
    return current() == "contained"
