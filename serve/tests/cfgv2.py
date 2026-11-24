"""A config file for a test, in the v2 shape, written the way the tests were first written (one flat "server" and "engine" object).

`dumps(old)` takes that flat object and returns the JSON text of the v2 file: what stays with a model goes in its entry (a placeholder model when the
object names none), the rest in "engine" and "server", as tools/migrations/config_v1_to_v2.py moves it."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "migrations"))
from config_v1_to_v2 import migrated  # noqa: E402

PLACEHOLDER = "m.basalt"


def v2(old: dict) -> dict:
    old = copy.deepcopy(old)
    if "models" in old:
        return {"version": 2, **{k: v for k, v in old.items() if k != "version"}}
    server = old.setdefault("server", {})
    if not any(str(k).strip().lower().replace("_", "-").removeprefix("--") == "model" for k in server):
        server["model"] = PLACEHOLDER
    return migrated(old)


def dumps(old: dict, indent: int | None = None) -> str:
    return json.dumps(v2(old), indent=indent)
