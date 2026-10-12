"""The settings page's view of the --config file: the main fields, checked as the server and engine check them, written
back with every other key kept. Changes take effect when the server is started again."""
from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import threading
from pathlib import Path

from serve.config import PARALLEL_MAX, build_parser, parse_settings
from serve.engine.vision import VISION_MODES, vision_mode

KV_MODES = ("fp16", "int8", "q4_0", "k8v4")          # the engine's --kv (q4 is read as q4_0)
SPEC_MAX = 8                                         # kVerifyMaxT: the longest verify window
CONTEXT_MAX = 2147483647 - 8                         # the engine keeps positions below 2^31
PREFILL_CEILINGS = (8192, 16384, 32768)              # --prefill auto:N
BODY_MAX = 64 << 10
_write_lock = threading.Lock()                       # one save at a time

# (section, key): the fields the page edits; `model` is shown, never written
FIELDS = (("server", "port"), ("server", "parallel"), ("server", "vision"),
          ("engine", "max-context"), ("engine", "kv"), ("engine", "spec"), ("engine", "spec-min-p"),
          ("engine", "expert-cache"), ("engine", "prefill"))
READ_ONLY = (("server", "model"),)
# the Loader's fields: the settings page's engine fields and the memory and slot switches
LOADER_FIELDS = (("server", "port"), ("server", "parallel"), ("server", "slot-full-context"), ("server", "vision"),
                 ("engine", "max-context"), ("engine", "kv"), ("engine", "prefill"), ("engine", "spec"),
                 ("engine", "spec-min-p"), ("engine", "expert-cache"), ("engine", "resident-experts"),
                 ("engine", "vram-reserve-mib"), ("engine", "ple-io"), ("engine", "pool-workers"))
PLE_IO_MODES = ("direct", "mmap", "ram")             # the engine's --ple-io
POOL_WORKERS_MAX = 256
RESTART_NOTE = "Saved to the config file. The running server keeps its settings until it is started again."


def norm(key) -> str:
    """A config key as the server reads it: lowercase, dashes, no leading --."""
    return str(key).strip().lower().replace("_", "-").removeprefix("--")


def _whole(value, what: str, lo: int, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{what}: expected a whole number, not {value!r}")
    try:
        n = int(str(value).strip()) if isinstance(value, str) else int(value)
    except ValueError:
        raise ValueError(f"{what}: expected a whole number, not {value!r}") from None
    if isinstance(value, float) and value != n:
        raise ValueError(f"{what}: expected a whole number, not {value!r}")
    if n < lo or (hi is not None and n > hi):
        raise ValueError(f"{what}: expected {lo}..{hi}, not {n}" if hi is not None else f"{what}: expected {lo} or more, not {n}")
    return n


def check_value(section: str, key: str, value):
    """The value as it goes into the file, or ValueError with the rule it breaks (the server's and engine's own)."""
    what = f"{section}.{key}"
    if key == "port":
        return _whole(value, what, 1, 65535)
    if key == "parallel":
        return _whole(value, what, 1, PARALLEL_MAX)
    if key == "vision":
        try:
            return vision_mode(value)
        except ValueError:
            raise ValueError(f"{what}: one of {', '.join(VISION_MODES)}, not {value!r}") from None
    if key == "max-context":
        return _whole(value, what, 1, CONTEXT_MAX)
    if key == "kv":
        text = str(value).strip().lower() if isinstance(value, str) else None
        text = "q4_0" if text == "q4" else text
        if text not in KV_MODES:
            raise ValueError(f"{what}: one of {', '.join(KV_MODES)}, not {value!r}")
        return text
    if key == "spec":
        return _whole(value, what, 0, SPEC_MAX)
    if key == "spec-min-p":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"{what}: a number from 0 to 1, not {value!r}")
        return value
    if key == "expert-cache":
        if isinstance(value, str) and value.strip().lower() == "auto":
            return "auto"
        try:
            return _whole(value, what, 0)
        except ValueError:
            raise ValueError(f"{what}: auto or a whole number of expert slots (0 = none), not {value!r}") from None
    if key == "prefill":
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "auto" or text in (f"auto:{n}" for n in PREFILL_CEILINGS):
                return text
        try:
            return _whole(value, what, 0)
        except ValueError:
            raise ValueError(f"{what}: auto, auto:8192, auto:16384, auto:32768 or a chunk size in tokens, "
                             f"not {value!r}") from None
    if key in ("resident-experts", "slot-full-context"):
        if not isinstance(value, bool):
            raise ValueError(f"{what}: true or false, not {value!r}")
        return value
    if key == "vram-reserve-mib":
        return _whole(value, what, 0, 1 << 20)
    if key == "pool-workers":
        return _whole(value, what, 1, POOL_WORKERS_MAX)
    if key == "ple-io":
        text = str(value).strip().lower() if isinstance(value, str) else None
        if text not in PLE_IO_MODES:
            raise ValueError(f"{what}: one of {', '.join(PLE_IO_MODES)}, not {value!r}")
        return text
    raise ValueError(f"{what}: not a field this page sets")


def read_file(path: str) -> dict:
    """The config file's JSON object (OSError / ValueError when it cannot be read)."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("the config file is not a JSON object")
    return raw


def values_of(raw: dict, fields=FIELDS) -> dict:
    """{"server.port": value, ...} for the edited and read-only fields the file sets."""
    out = {}
    for section, key in (*fields, *READ_ONLY):
        sec = raw.get(section) if isinstance(raw.get(section), dict) else {}
        for k, v in sec.items():
            if norm(k) == key:
                out[f"{section}.{key}"] = v
    return out


def merge(raw: dict, changes: dict, fields=FIELDS) -> tuple[dict, list[str]]:
    """(new file object, the fields that changed). `changes` maps "section.key" to a value, or None to remove the key
    (back to the default). Existing keys keep their spelling and place; every other key is kept as it was."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('send {"changes": {"engine.spec": 7, ...}}')
    allowed = {f"{s}.{k}": (s, k) for s, k in fields}
    out = json.loads(json.dumps(raw))                  # a deep copy
    changed = []
    for name, value in changes.items():
        if name not in allowed:
            ro = {f"{s}.{k}" for s, k in READ_ONLY}
            raise ValueError(f"{name}: read-only here" if name in ro else f"{name}: not a field this page sets")
        section, key = allowed[name]
        sec = out.get(section, {})
        if not isinstance(sec, dict):
            raise ValueError(f'"{section}" in the config file is not an object')
        out[section] = sec
        present = [k for k in sec if norm(k) == key]
        if value is None:
            for k in present:
                del sec[k]
            if present:
                changed.append(name)
            continue
        value = check_value(section, key, value)
        if present:
            if sec[present[0]] != value:
                changed.append(name)
            sec[present[0]] = value
            for k in present[1:]:
                del sec[k]
        else:
            sec[key] = value
            changed.append(name)
    for section in ("server", "engine"):
        if section in out and out[section] == {} and section not in raw:
            del out[section]
    return out, changed


def _parser():
    """build_parser() with argparse's exit turned into a ValueError carrying its message."""
    ap = build_parser()

    def error(message):
        raise ValueError(message)
    ap.error = error
    return ap


def check_whole(obj: dict, path: str) -> None:
    """Load the new file exactly as `basalt serve --config` would (server options, engine section, parallel against
    batch/slots, vision), from a copy beside the real one so relative paths resolve the same; ValueError if not."""
    if isinstance(obj.get("engine"), dict):
        eng = {norm(k): v for k, v in obj["engine"].items()}
        if str(eng.get("kv", "")).lower() == "k8v4" and eng.get("kv-resident") not in (None, False, 0, "0"):
            raise ValueError("engine.kv: k8v4 does not work with kv-resident streaming")
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".basalt-settings-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        parse_settings(_parser(), ["--config", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def save(path: str, changes: dict, fields=FIELDS) -> dict:
    """Validate and write the changes; {"changed": [...], "values": {...}}. The old file is kept as FILE.bak."""
    with _write_lock:
        return _save(path, changes, fields)


def preview(path: str, changes: dict, fields=FIELDS) -> dict:
    """The file object the changes would give, checked as the server would load it; nothing is written."""
    new, _ = merge(read_file(path), changes, fields)
    check_whole(new, path)
    return new


def _save(path: str, changes: dict, fields=FIELDS) -> dict:
    raw = read_file(path)
    new, changed = merge(raw, changes, fields)
    if not changed:
        return {"changed": [], "values": values_of(raw, fields)}
    check_whole(new, path)
    text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".basalt-settings-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            Path(path + ".bak").write_bytes(Path(path).read_bytes())
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"changed": changed, "values": values_of(new, fields)}


def is_loopback(address: str) -> bool:
    """A client on this PC (127.0.0.0/8, ::1, or either mapped into IPv6)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback
