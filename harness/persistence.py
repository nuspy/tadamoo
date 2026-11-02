"""The "work until done" switch: whether the agent goes on by itself while a request is not met.

One small file in the user data folder holds the person's choice; it is on unless they turn it off, and every conversation reads it again when it
changes, so the choice holds across conversations, new ones and restarts. Where nothing is configured (a library caller, a test) it is off."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional

FILE = "work-until-done.json"
DEFAULT = True

_source: Optional[Callable[[], bool]] = None


class Prefs:
    """The person's choice, in a small file; a missing or damaged file is the default."""

    def __init__(self, path: str | Path, default: bool = DEFAULT) -> None:
        self.path, self.default = Path(path), default
        self._lock = threading.Lock()
        self._stamp: Optional[tuple] = None
        self._value = default

    def read(self) -> bool:
        with self._lock:
            try:
                st = self.path.stat()
            except OSError:
                self._stamp, self._value = None, self.default
                return self._value
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8")).get("on")
                except (OSError, ValueError, AttributeError):
                    value = None
                self._stamp, self._value = stamp, (value if isinstance(value, bool) else self.default)
            return self._value

    def write(self, on: bool) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"on": bool(on)}), encoding="utf-8")
        os.replace(tmp, self.path)
        return self.read()


def configure(source: Optional[Callable[[], bool] | str | Path]) -> None:
    """Sets where this process learns the choice: a function, a file, or None for off."""
    global _source
    _source = source if source is None or callable(source) else Prefs(source).read


def enabled() -> bool:
    return bool(_source()) if _source is not None else False
