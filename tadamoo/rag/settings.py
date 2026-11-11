"""The settings of the knowledge archives, in `<rag folder>/settings.json` (a file, so the folder of archives lists none of it): the Python of a
Docling environment (empty: found by itself), and what to do with passages that read as instructions to an assistant."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from tadamoo.rag.errors import RagError

FILE = "settings.json"
DEFAULTS: Dict[str, Any] = {"docling_python": "", "docling_device": "auto", "flagged": "keep"}
FLAGGED = ("keep", "drop")
DEVICES = ("auto", "cpu")


class RagSettings:
    def __init__(self, root: str | Path) -> None:
        self.file = Path(root) / FILE
        self._lock = threading.Lock()

    def read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        out = dict(DEFAULTS)
        if isinstance(raw, dict):
            if isinstance(raw.get("docling_python"), str):
                out["docling_python"] = raw["docling_python"].strip()[:500]
            if raw.get("docling_device") in DEVICES:
                out["docling_device"] = raw["docling_device"]
            if raw.get("flagged") in FLAGGED:
                out["flagged"] = raw["flagged"]
        return out

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in changes if k not in DEFAULTS]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            now = self.read()
            if "docling_python" in changes:
                value = changes["docling_python"]
                if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
                    raise RagError("docling_python: the path of a Python, or empty")
                value = value.strip()
                if value and not Path(value).is_file():
                    raise RagError("docling_python: there is no such file")
                now["docling_python"] = value
            if "docling_device" in changes:
                if changes["docling_device"] not in DEVICES:
                    raise RagError("docling_device: auto (the GPU when the server can lend it) or cpu")
                now["docling_device"] = changes["docling_device"]
            if "flagged" in changes:
                if changes["flagged"] not in FLAGGED:
                    raise RagError("flagged: keep or drop")
                now["flagged"] = changes["flagged"]
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(FILE + ".tmp")
            tmp.write_text(json.dumps(now, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
            return now
