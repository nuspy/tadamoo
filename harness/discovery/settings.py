"""The person's settings for the online search: which sources are asked, the weights of the score and its minimum, whether the
best candidate above the minimum is installed without asking, and the plugin marketplaces to look in. One file, discovery.json,
in the capabilities folder of the user data folder.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from harness.discovery.scoring import DEFAULT_MIN_SCORE, DEFAULT_WEIGHTS

SOURCES = ("registry", "npm", "pypi", "github", "skills")
MARKETPLACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}/[A-Za-z0-9_.\-]{1,100}$")
MAX_MARKETPLACES = 20
FILE = "discovery.json"


def defaults() -> Dict[str, Any]:
    return {"sources": {s: True for s in SOURCES}, "weights": dict(DEFAULT_WEIGHTS), "min_score": DEFAULT_MIN_SCORE,
            "auto_install": False, "marketplaces": [], "base_urls": {}}


class DiscoverySettings:
    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / FILE

    def load(self) -> Dict[str, Any]:
        """The settings with the defaults filled in; a missing or damaged file, or a value out of range, gives the default."""
        out = defaults()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return out
        if not isinstance(data, dict):
            return out
        if isinstance(data.get("sources"), dict):
            out["sources"].update({k: v for k, v in data["sources"].items() if k in SOURCES and isinstance(v, bool)})
        if isinstance(data.get("weights"), dict):
            out["weights"].update({k: float(v) for k, v in data["weights"].items()
                                   if k in DEFAULT_WEIGHTS and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100})
        score = data.get("min_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 100:
            out["min_score"] = int(score)
        out["auto_install"] = data.get("auto_install") is True
        if isinstance(data.get("marketplaces"), list):
            out["marketplaces"] = [m for m in data["marketplaces"] if isinstance(m, str) and MARKETPLACE.match(m)][:MAX_MARKETPLACES]
        if isinstance(data.get("base_urls"), dict):           # for tests and mirrors; the page does not write it
            out["base_urls"] = {k: v for k, v in data["base_urls"].items() if isinstance(k, str) and isinstance(v, str) and re.match(r"^https?://", v)}
        return out

    def save(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Applies the changes that are valid; raises ValueError with what to fix for one that is not. `base_urls` is never written here."""
        current = self.load()
        if "sources" in changes:
            src = changes["sources"]
            if not isinstance(src, dict) or any(k not in SOURCES or not isinstance(v, bool) for k, v in src.items()):
                raise ValueError("sources: turn each of " + ", ".join(SOURCES) + " on or off")
            current["sources"].update(src)
        if "weights" in changes:
            w = changes["weights"]
            if not isinstance(w, dict) or any(k not in DEFAULT_WEIGHTS or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100
                                              for k, v in w.items()):
                raise ValueError("weights: a number from 0 to 100 for each of " + ", ".join(DEFAULT_WEIGHTS))
            current["weights"].update({k: float(v) for k, v in w.items()})
            if not any(v > 0 for v in current["weights"].values()):
                raise ValueError("weights: at least one must be above zero")
        if "min_score" in changes:
            v = changes["min_score"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
                raise ValueError("min_score: a number from 0 to 100")
            current["min_score"] = int(v)
        if "auto_install" in changes:
            if not isinstance(changes["auto_install"], bool):
                raise ValueError("auto_install: true or false")
            current["auto_install"] = changes["auto_install"]
        if "marketplaces" in changes:
            m = changes["marketplaces"]
            if not isinstance(m, list) or len(m) > MAX_MARKETPLACES or any(not isinstance(x, str) or not MARKETPLACE.match(x.strip()) for x in m):
                raise ValueError("marketplaces: a list of GitHub repositories written owner/repo")
            current["marketplaces"] = list(dict.fromkeys(x.strip() for x in m))
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        data = {k: current[k] for k in ("sources", "weights", "min_score", "auto_install", "marketplaces")}
        if isinstance(stored, dict) and isinstance(stored.get("base_urls"), dict):
            data["base_urls"] = stored["base_urls"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return self.load()

    def enabled(self) -> List[str]:
        return [s for s, on in self.load()["sources"].items() if on]
