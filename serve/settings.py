"""The settings page's view of the --config file: the main fields, checked as the server and engine check them, written
back with every other key kept. Changes take effect when the server is started again."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import tempfile
import threading
from pathlib import Path

from serve.config import PARALLEL_MAX, REMOVED_ENGINE_KEYS, SIDE_SLOT_DEFAULT, SIDE_SLOT_MIN, build_parser, parse_settings
from serve.engine.vision import VISION_MODES, vision_mode

KV_MODES = ("fp16", "int8", "q4_0", "k8v4")          # the engine's --kv (q4 is read as q4_0)
SPEC_MAX = 8                                         # kVerifyMaxT: the longest verify window
CONTEXT_MAX = 2147483647 - 8                         # the engine keeps positions below 2^31
VRAM_RESERVE_MIN_MIB = 256                         # the engine refuses a smaller reserve
PREFILL_CEILINGS = (8192, 16384, 32768)              # --prefill auto:N
BODY_MAX = 64 << 10
_write_lock = threading.Lock()                       # one save at a time

# (section, key): the fields the page edits; `model` is shown, never written
FIELDS = (("server", "port"), ("server", "parallel"), ("server", "vision"),
          ("engine", "max-context"), ("engine", "kv"), ("engine", "spec"),
          ("engine", "expert-cache"), ("engine", "prefill"))
READ_ONLY = (("server", "model"),)
# the Loader's fields: the settings page's engine fields and the memory and slot switches
LOADER_FIELDS = (("server", "port"), ("server", "parallel"), ("server", "slot-full-context"), ("server", "side-slot"), ("server", "vision"),
                 ("server", "vision-projector"), ("server", "vision-hold"),
                 ("engine", "max-context"), ("engine", "kv"), ("engine", "prefill"), ("engine", "spec"),
                 ("engine", "relaxed-drafting"), ("engine", "expert-cache"), ("engine", "resident-experts"),
                 ("engine", "vram-reserve-mib"), ("engine", "ple-io"), ("engine", "pool-workers"),
                 # memory and cache
                 ("engine", "kv-resident"), ("engine", "resident-budget-gib"), ("engine", "mmap-experts"),
                 ("engine", "adapt-swaps"), ("engine", "prefill-cpu-share"), ("engine", "vram-elastic"),
                 ("engine", "keep-vram-commit"), ("engine", "expert-profile-save"),
                 ("engine", "expert-profile-save-every"),
                 # the conversation cache in RAM
                 ("engine", "prompt-cache"), ("engine", "prompt-cache-every"), ("engine", "prompt-cache-root"),
                 ("engine", "conversation-cache-mib"), ("engine", "conversation-cache-slots"),
                 ("engine", "conversation-cache-min-free-mib"),
                 # context extension, fixed for the run
                 ("engine", "rope-scaling"), ("engine", "rope-scale"), ("engine", "rope-freq-base"),
                 ("server", "grammar"), ("server", "reasoning-budget-tokens"))
# the secret of the changes through /api/*: its own route, never part of the values the pages read back
CONTROL_FIELDS = (("server", "control-secret"), ("server", "control-secret-value"))
CONTROL_SECRET_MAX = 200
# the model a load profile or the Loader writes (with the API name, when the file sets one)
MODEL_FIELDS = (("server", "model"), ("server", "alias"))
PLE_IO_MODES = ("direct", "mmap", "ram")             # the engine's --ple-io
VISION_HOLDS = ("keep", "release")                   # the image encoder's weights stay in RAM, or are read again for each batch of images
POOL_WORKERS_MAX = 256
KV_RESIDENT_MIN = 20480                              # the engine keeps at least this many cells of KV in VRAM
ROPE_SCALINGS = ("none", "linear", "yarn")           # the engine's --rope-scaling
ADAPT_SWAPS_MAX = 1 << 20
PROMPT_CACHE_MAX = 1024                              # checkpoints (about 118 MB of RAM each)
CACHE_MIB_MAX = 1 << 24                              # MiB: the conversation cache's budget and floor
CONVERSATION_SLOTS_MAX = 4096
RESIDENT_BUDGET_MAX_GIB = 65536
PROFILE_SAVE_EVERY_MAX = 525600                      # minutes: a year
PATH_MAX = 1024
# fields an older saved file, profile or form may still carry: ignored, and dropped when the file is next saved
REMOVED_KEYS = frozenset(f"engine.{k}" for k in REMOVED_ENGINE_KEYS)
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


def _number(value, what: str, lo: float | None = None, hi: float | None = None, expected: str = "a number") -> int | float:
    """A finite number in lo..hi (a whole one is returned as an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{what}: expected {expected}, not {value!r}")
    try:
        n = float(str(value).strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except ValueError:
        raise ValueError(f"{what}: expected {expected}, not {value!r}") from None
    if not math.isfinite(n):
        raise ValueError(f"{what}: expected {expected}, not {value!r}")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        bound = f"{lo:g}..{hi:g}" if lo is not None and hi is not None else f"{lo:g} or more" if lo is not None else f"at most {hi:g}"
        raise ValueError(f"{what}: expected {bound}, not {value!r}")
    return int(n) if n.is_integer() and abs(n) < 1e15 else n


def _path(value, what: str) -> str:
    """A file path for the engine to write: one line, no NUL, not a flag."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > PATH_MAX or any(c in text for c in "\n\r\0") or text.startswith("-"):
        raise ValueError(f"{what}: the path of a file, not {value!r}")
    return text


def check_value(section: str, key: str, value):
    """The value as it goes into the file, or ValueError with the rule it breaks (the server's and engine's own)."""
    what = f"{section}.{key}"
    if key == "port":
        return _whole(value, what, 1, 65535)
    if key == "parallel":
        return _whole(value, what, 1, PARALLEL_MAX)
    if key == "side-slot":                               # the technical session: 0 (off) or its context in tokens
        if isinstance(value, bool):
            return SIDE_SLOT_DEFAULT if value else 0
        n = _whole(value, what, 0, CONTEXT_MAX)
        if 0 < n < SIDE_SLOT_MIN:
            raise ValueError(f"{what}: 0 (off) or a number of tokens from {SIDE_SLOT_MIN}, not {value!r}")
        return n
    if key == "vision":
        value = {"on": "0", "peer": "1"}.get(str(value).strip().lower(), value)   # the old names of a card-numbered encoder
        try:
            return vision_mode(value)
        except ValueError:
            raise ValueError(f"{what}: one of {', '.join(VISION_MODES)}, not {value!r}") from None
    if key == "relaxed-drafting":
        if value is True:
            return 0.5                                   # the engine's own default for the bare flag
        try:
            p = float(str(value).strip()) if not isinstance(value, bool) and isinstance(value, (int, float, str)) else None
        except ValueError:
            p = None
        if p is None or not 0 < p <= 1:
            raise ValueError(f"{what}: a number above 0 and up to 1 (the probability a draft needs), or leave it off, not {value!r}")
        return p
    if key == "vision-hold":
        text = value.strip().lower() if isinstance(value, str) else ""
        if text not in VISION_HOLDS:
            raise ValueError(f"{what}: one of {', '.join(VISION_HOLDS)}, not {value!r}")
        return text
    if key == "vision-projector":
        text = value.strip() if isinstance(value, str) else ""
        if not text or len(text) > 1024 or any(c in text for c in "\n\r\0"):
            raise ValueError(f"{what}: auto, embedded or the path of a projector file, not {value!r}")
        return text.lower() if text.lower() in ("auto", "embedded") else text
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
    if key == "control-secret-value":
        text = value if isinstance(value, str) else ""
        if not text.strip() or len(text) > CONTROL_SECRET_MAX or any(not 32 <= ord(c) <= 126 for c in text):
            raise ValueError(f"{what}: one line of letters, digits and signs without accents, up to {CONTROL_SECRET_MAX} characters")
        return text
    if key in ("resident-experts", "slot-full-context", "mmap-experts", "vram-elastic", "keep-vram-commit", "grammar", "control-secret"):
        if not isinstance(value, bool):
            raise ValueError(f"{what}: true or false, not {value!r}")
        return value
    if key == "vram-reserve-mib":
        return _whole(value, what, VRAM_RESERVE_MIN_MIB, 1 << 20)
    if key == "pool-workers":
        return _whole(value, what, 1, POOL_WORKERS_MAX)
    if key == "ple-io":
        text = str(value).strip().lower() if isinstance(value, str) else None
        if text not in PLE_IO_MODES:
            raise ValueError(f"{what}: one of {', '.join(PLE_IO_MODES)}, not {value!r}")
        return text
    if key == "kv-resident":
        try:
            n = _whole(value, what, 0, CONTEXT_MAX)
        except ValueError:
            raise ValueError(f"{what}: 0 (all the KV in VRAM) or a number of cells from {KV_RESIDENT_MIN}, not {value!r}") from None
        if 0 < n < KV_RESIDENT_MIN:
            raise ValueError(f"{what}: 0 (all the KV in VRAM) or a number of cells from {KV_RESIDENT_MIN}, not {n}")
        return n
    if key == "resident-budget-gib":
        n = _number(value, what, None, RESIDENT_BUDGET_MAX_GIB, "a number of GiB above 0")
        if n <= 0:
            raise ValueError(f"{what}: a number of GiB above 0, or leave it empty for auto, not {value!r}")
        return n
    if key == "adapt-swaps":
        return _whole(value, what, 0, ADAPT_SWAPS_MAX)
    if key == "prefill-cpu-share":
        return _number(value, what, 0, 1, "a number from 0 to 1")
    if key == "expert-profile-save":
        return _path(value, what)
    if key == "expert-profile-save-every":
        return _number(value, what, 0, PROFILE_SAVE_EVERY_MAX, "a number of minutes (0 = on exit only)")
    if key == "prompt-cache":
        return _whole(value, what, 0, PROMPT_CACHE_MAX)
    if key in ("prompt-cache-every", "prompt-cache-root", "reasoning-budget-tokens"):
        return _whole(value, what, 0, CONTEXT_MAX)
    if key in ("conversation-cache-mib", "conversation-cache-min-free-mib"):
        return _whole(value, what, 0, CACHE_MIB_MAX)
    if key == "conversation-cache-slots":
        return _whole(value, what, 0, CONVERSATION_SLOTS_MAX)
    if key == "rope-scaling":
        text = value.strip().lower() if isinstance(value, str) else None
        if text not in ROPE_SCALINGS:
            raise ValueError(f"{what}: one of {', '.join(ROPE_SCALINGS)}, not {value!r}")
        return text
    if key == "rope-scale":
        return _number(value, what, 1, 1024, "a factor of 1 or more")
    if key == "rope-freq-base":
        n = _number(value, what, 0, 1e12, "0 (the model's own) or a base above 1")
        if 0 < n <= 1:
            raise ValueError(f"{what}: 0 (the model's own) or a base above 1, not {value!r}")
        return n
    raise ValueError(f"{what}: not a field this page sets")


def check_combination(eng: dict) -> None:
    """ValueError for engine settings that cannot go together; `eng` maps engine keys (normalized, no section) to values."""
    if str(eng.get("kv", "")).lower() == "k8v4" and eng.get("kv-resident") not in (None, False, 0, "0"):
        raise ValueError("engine.kv: k8v4 does not work with kv-resident streaming")
    if eng.get("mmap-experts") is True and (eng.get("resident-experts") is True
                                            or eng.get("resident-budget-gib") not in (None, False)):
        raise ValueError("engine.mmap-experts: not together with resident-experts or resident-budget-gib "
                         "(mmap-experts keeps the experts in the file mapping)")


def check_model_value(key: str, value):
    """server.model (a .basalt file the engine can open, as a POSIX path) or server.alias (a name), as written by a
    load profile or the Loader; ValueError with the reason."""
    if key == "model":
        from serve import models
        try:
            return models.check_basalt(value)
        except ValueError as e:
            raise ValueError(f"server.model: {e}") from None
    if key == "alias":
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"server.alias: a name of 1 to 200 characters, not {value!r}")
        return value.strip()
    raise ValueError(f"server.{key}: not a model field")


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
        if name in REMOVED_KEYS:
            continue
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
        value = check_model_value(key, value) if (section, key) in MODEL_FIELDS else check_value(section, key, value)
        if present:
            if sec[present[0]] != value:
                changed.append(name)
            sec[present[0]] = value
            for k in present[1:]:
                del sec[k]
        else:
            sec[key] = value
            changed.append(name)
    if isinstance(out.get("engine"), dict):
        for k in [k for k in out["engine"] if norm(k) in REMOVED_ENGINE_KEYS]:
            del out["engine"][k]
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
        check_combination({norm(k): v for k, v in obj["engine"].items()})
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tadamoo-settings-", suffix=".json", dir=folder)
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


def control_secret_state(path: str) -> dict:
    """Whether the file asks for the secret and whether it holds one; the secret itself is never returned."""
    values = values_of(read_file(path), CONTROL_FIELDS)
    return {"enabled": values.get("server.control-secret") is True, "set": bool(values.get("server.control-secret-value"))}


def model_of(path: str) -> str | None:
    """The config file's server.model as an absolute path (relative paths are the config file's folder's)."""
    model = values_of(read_file(path)).get("server.model")
    if not isinstance(model, str) or not model:
        return None
    return model if os.path.isabs(model) else str(Path(path).resolve().parent / model)


def set_model(path: str, model: str, name: str | None = None) -> dict:
    """Write `model` (a .basalt file) as the config file's server.model; when the model changes and the file names
    the model for the API (server.alias), that name becomes `name`. {"changed": [...], "values": {...}}."""
    current = model_of(path)
    changes = {"server.model": model}
    same = current is not None and os.path.normcase(os.path.abspath(current)) == os.path.normcase(os.path.abspath(model))
    if same:
        return {"changed": [], "values": values_of(read_file(path), MODEL_FIELDS)}
    if name and "server.alias" in values_of(read_file(path), MODEL_FIELDS):
        changes["server.alias"] = name
    return save(path, changes, MODEL_FIELDS)


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
    fd, tmp = tempfile.mkstemp(prefix=".tadamoo-settings-", suffix=".json", dir=folder)
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
