"""The time limits that end silent work, read from one place: limits.json in the user data folder.

    {"beat_timeout_s": 120, "idle_s": null, "compare_wait_s": null, "tool_timeout_s": 3600}

A value of null or 0 means no limit. A name missing from the file takes the environment variable BASALT_LIMIT_<NAME>
(upper case), else the default of the caller, else DEFAULTS. `tool_timeout_s` is the one limit that always exists: null,
0 or a bad value in the file gives the default. The file is read again whenever it changes, so a new value reaches
the next tool call or check without a restart. The harness processes read the same file."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

FILE = "limits.json"
ENV_PREFIX = "BASALT_LIMIT_"
DEFAULTS: Dict[str, Optional[float]] = {
    "beat_timeout_s": None,        # silence from a conversation's process before it is stopped and restarted
    "idle_s": None,                # an idle conversation's process is closed (dormant) after this
    "compare_wait_s": None,        # how long a comparison waits for one side's answer
    "tool_timeout_s": 3600.0,      # one tool call (a command has its own, shorter limit); never unlimited
}
ALWAYS_SET = ("tool_timeout_s",)
_UNSET: Any = object()
_lock = threading.Lock()
_cache: Dict[str, Any] = {"key": None, "values": {}}


def file_path() -> Path:
    from serve import userdata
    return userdata.user_data_dir() / FILE


def _number(v: Any) -> Optional[float]:
    """A limit in seconds, None for no limit; ValueError for anything that is not a number."""
    if v is None or isinstance(v, bool) and not v:
        return None
    if isinstance(v, bool):
        raise ValueError("not a number")
    if isinstance(v, str):
        v = v.strip()
        if v.lower() in ("", "none", "null", "off", "unlimited"):
            return None
    n = float(v)
    if n != n:
        raise ValueError("not a number")
    return n if n > 0 else None


def _read() -> Dict[str, Any]:
    p = file_path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["values"]
        values: Dict[str, Any] = {}
        if key[1] is not None:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    values = data
            except (OSError, ValueError):
                values = {}
        _cache.update(key=key, values=values)
        return values


def get(name: str, default: Any = _UNSET) -> Optional[float]:
    """The limit `name` in seconds; None means no limit. The file wins, then the environment, then `default`, then DEFAULTS."""
    fallback = DEFAULTS.get(name) if default is _UNSET else default
    values = _read()
    raw: Any = _UNSET
    if name in values:
        raw = values[name]
    elif os.environ.get(ENV_PREFIX + name.upper()) is not None:
        raw = os.environ[ENV_PREFIX + name.upper()]
    out = fallback
    if raw is not _UNSET:
        try:
            out = _number(raw)
        except (TypeError, ValueError):
            out = fallback
    if out is None and name in ALWAYS_SET:
        out = fallback if fallback else DEFAULTS.get(name)
    return out


def current() -> Dict[str, Optional[float]]:
    """Every limit as it applies now (for a settings page)."""
    return {name: get(name) for name in DEFAULTS}


def save(values: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Write the given limits into the file (others kept); ValueError for an unknown name or a bad number."""
    clean: Dict[str, Optional[float]] = {}
    for name, v in values.items():
        if name not in DEFAULTS:
            raise ValueError(f"unknown limit: {name}")
        try:
            clean[name] = _number(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number of seconds, or empty for no limit") from None
    p = file_path()
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    old.update(clean)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return current()
