"""The settings of the knowledge archives, in `<rag folder>/settings.json` (a file, so the folder of archives lists none of it): the Python of a
Docling environment (empty: found by itself), what to do with passages that read as instructions to an assistant, and the semantic search
(embedding model, reranker, how the two scores are blended, when a passage is too far from the question to count)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict

from tadamoo.rag.errors import RagError

FILE = "settings.json"
DEFAULTS: Dict[str, Any] = {"docling_python": "", "docling_device": "auto", "flagged": "keep",
                            "embed_enabled": True, "embed_python": "", "embed_models_dir": "", "embed_model": "", "rerank": True, "rerank_model": "",
                            "lexical_weight": 0.05, "min_cosine": 0.3, "min_rerank": 0.2, "idle_s": 120,
                            "enrich_language": "Italian", "enrich_batch": 5}
FLAGGED = ("keep", "drop")
DEVICES = ("auto", "cpu")
FLAGS = ("embed_enabled", "rerank")
PATHS = ("docling_python", "embed_python")
NAMES = ("embed_model", "rerank_model")                 # the name of a model folder, not a path
RANGES = {"lexical_weight": (0.0, 0.3), "min_cosine": (0.0, 0.9), "min_rerank": (0.0, 0.95), "idle_s": (10, 3600), "enrich_batch": (1, 12)}
LANGUAGE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ ]{1,28}[A-Za-zÀ-ÿ]$")


def _number(value: Any, lo: float, hi: float) -> Any:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and lo <= value <= hi
    return value if ok else None


class RagSettings:
    def __init__(self, root: str | Path) -> None:
        self.file = Path(root) / FILE
        self._lock = threading.Lock()
        self._seen: tuple = (None, {})

    def read(self) -> Dict[str, Any]:
        try:
            stamp = self.file.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if self._seen[0] == stamp:
            return dict(self._seen[1])
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        out = dict(DEFAULTS)
        if isinstance(raw, dict):
            for key in PATHS:
                if isinstance(raw.get(key), str):
                    out[key] = raw[key].strip()[:500]
            if raw.get("docling_device") in DEVICES:
                out["docling_device"] = raw["docling_device"]
            if raw.get("flagged") in FLAGGED:
                out["flagged"] = raw["flagged"]
            for key in FLAGS:
                if isinstance(raw.get(key), bool):
                    out[key] = raw[key]
            if isinstance(raw.get("enrich_language"), str) and LANGUAGE.match(raw["enrich_language"].strip()):
                out["enrich_language"] = raw["enrich_language"].strip()
            for key in NAMES + ("embed_models_dir",):
                if isinstance(raw.get(key), str):
                    out[key] = raw[key].strip()[:500]
            for key, (lo, hi) in RANGES.items():
                if _number(raw.get(key), lo, hi) is not None:
                    out[key] = type(DEFAULTS[key])(raw[key])
        self._seen = (stamp, out)
        return dict(out)

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in changes if k not in DEFAULTS]
        if extra:
            raise RagError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            now = self.read()
            for key in PATHS:
                if key in changes:
                    value = changes[key]
                    if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
                        raise RagError(f"{key}: the path of a Python, or empty")
                    value = value.strip()
                    if value and not Path(value).is_file():
                        raise RagError(f"{key}: there is no such file")
                    now[key] = value
            if "docling_device" in changes:
                if changes["docling_device"] not in DEVICES:
                    raise RagError("docling_device: auto (the GPU when the server can lend it) or cpu")
                now["docling_device"] = changes["docling_device"]
            if "flagged" in changes:
                if changes["flagged"] not in FLAGGED:
                    raise RagError("flagged: keep or drop")
                now["flagged"] = changes["flagged"]
            for key in FLAGS:
                if key in changes:
                    if not isinstance(changes[key], bool):
                        raise RagError(f"{key}: true or false")
                    now[key] = changes[key]
            if "enrich_language" in changes:
                value = changes["enrich_language"]
                if not isinstance(value, str) or not LANGUAGE.match(value.strip()):
                    raise RagError("enrich_language: the name of a language, such as Italian")
                now["enrich_language"] = value.strip()
            if "embed_models_dir" in changes:
                value = changes["embed_models_dir"]
                if not isinstance(value, str) or len(value) > 500 or "\x00" in value or (value.strip() and not Path(value.strip()).is_dir()):
                    raise RagError("embed_models_dir: a folder that holds the model folders, or empty")
                now["embed_models_dir"] = value.strip()
            for key in NAMES:
                if key in changes:
                    value = changes[key]
                    if not isinstance(value, str) or len(value) > 200 or any(c in value for c in "/\\x00") or value.strip() in (".", ".."):
                        raise RagError(f"{key}: the name of a model folder (no path), or empty")
                    now[key] = value.strip()
            for key, (lo, hi) in RANGES.items():
                if key in changes:
                    if _number(changes[key], lo, hi) is None:
                        raise RagError(f"{key}: a number from {lo:g} to {hi:g}")
                    now[key] = type(DEFAULTS[key])(changes[key])
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(FILE + ".tmp")
            tmp.write_text(json.dumps(now, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
            self._seen = (None, {})
            return now
