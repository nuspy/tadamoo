"""A config file for a test, in the v2 shape, written the way the tests were first written (one flat "server" and "engine" object).

`dumps(old)` takes that flat object and returns the JSON text of the v2 file: what stays with a model goes in its entry (a placeholder model when the
object names none), the rest in "engine" and "server"."""
from __future__ import annotations

import copy
import json

from serve.config import CONFIG_VERSION, ENTRY_OPTIONS, MODEL_OPTIONS, config_name

PLACEHOLDER = "m.basalt"


def v2(old: dict) -> dict:
    old = copy.deepcopy(old)
    if "models" in old:
        return {"version": CONFIG_VERSION, **{k: v for k, v in old.items() if k != "version"}}
    server, entry, shared = {}, {}, {}
    for key, value in old.get("server", {}).items():
        name = config_name(key)
        if name in ENTRY_OPTIONS:
            entry[name] = value
        elif name in MODEL_OPTIONS:
            shared[name] = value
        else:
            server[name] = value
    for key, value in old.get("engine", {}).items():
        name = config_name(key)
        (entry if name in ENTRY_OPTIONS else shared)[name] = value
    entry.setdefault("model", PLACEHOLDER)
    out = {"version": CONFIG_VERSION, "models": [entry]}
    if shared:
        out["engine"] = shared
    if server:
        out["server"] = server
    return out


def dumps(old: dict, indent: int | None = None) -> str:
    return json.dumps(v2(old), indent=indent)
