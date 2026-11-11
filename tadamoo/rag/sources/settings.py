"""The settings of the refresh of archives, in `<rag folder>/sources.json`: how many earlier versions of a document are kept and for how long, and how polite a refresh
is to the sites it reads (pause between requests, size and time caps, robots.txt, who it says it is)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from tadamoo.rag.errors import RagError

FILE = "sources.json"
DEFAULTS: Dict[str, Any] = {"keep_versions": 3, "keep_days": 90, "min_interval_s": 1.0, "max_download_mb": 50, "request_timeout_s": 30, "max_run_minutes": 30,
                            "respect_robots": True, "contact": "", "wait_minutes": 20}
RANGES = {"keep_versions": (0, 20), "keep_days": (0, 3650), "min_interval_s": (0.0, 60.0), "max_download_mb": (1, 200), "request_timeout_s": (3, 300),
          "max_run_minutes": (1, 720), "wait_minutes": (0, 720)}


def _number(value: Any, lo: float, hi: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and lo <= value <= hi


class SyncSettings:
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
            for key, (lo, hi) in RANGES.items():
                if _number(raw.get(key), lo, hi):
                    out[key] = type(DEFAULTS[key])(raw[key])
            if isinstance(raw.get("respect_robots"), bool):
                out["respect_robots"] = raw["respect_robots"]
            if isinstance(raw.get("contact"), str):
                out["contact"] = raw["contact"].strip()[:200]
        return out

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in changes if k not in DEFAULTS]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            now = self.read()
            for key, (lo, hi) in RANGES.items():
                if key in changes:
                    if not _number(changes[key], lo, hi):
                        raise RagError(f"{key}: a number from {lo:g} to {hi:g}")
                    now[key] = type(DEFAULTS[key])(changes[key])
            if "respect_robots" in changes:
                if not isinstance(changes["respect_robots"], bool):
                    raise RagError("respect_robots: true or false")
                now["respect_robots"] = changes["respect_robots"]
            if "contact" in changes:
                value = changes["contact"]
                if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
                    raise RagError("contact: a web address or an e-mail address of at most 200 characters, or empty")
                now["contact"] = value.strip()
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(FILE + ".tmp")
            tmp.write_text(json.dumps(now, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
            return now
