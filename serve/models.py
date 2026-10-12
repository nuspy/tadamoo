"""The models folders and what is in them, for the Loader, the Settings page and the desktop app.

The owner names one or more folders (kept next to the load profiles, in the user's data folder: models.json).
Every .basalt and .gguf file under them is described from its header alone - the .basalt 64 KiB table with its meta
and experts.layout sections, the GGUF key/value block and tensor directory; no weights are read - cached by path,
size and modification time, and given a status for Basalt:

  ready         a .basalt file the engine can open (magic, version, size); "Verifica" checks every section's SHA-256
  convert       a GGUF set the converter and the engine support: Qwen3.8-Flash-Next's geometry (qwen4exp), every
                shard present, expert formats the GPU kernels cover (src/kernels/cuda/iq_kernels.cu GU_FMTS / D_FMTS)
  incompatible  anything else, with the reason (another architecture, expert formats without kernels, a missing
                shard, an unfinished download, a vision projector or a drafter file, shown as an attachment)

Jobs, one at a time, run in the background inside the server's job object: verify a .basalt (tools/model_file.py
verify, one reused 16 MiB buffer) and convert a GGUF set (tools/convert/convert.py, after tools/convert/mtp_fetch.py
when no drafter source is on disk). A conversion starts only from a plan the page showed and the owner confirmed (its
token): the size of the new file, the disk and its free space. It is refused without room on the disk, or with the
commit at 75% or more of its limit.

    python -m serve.models dirs                        # the folders and the suggestions, as JSON
    python -m serve.models set-dirs DIR [DIR ...]      # check and save the folders
    python -m serve.models json [--rescan] [--config FILE]   # the models with their status, as JSON
    python -m serve.models list [--rescan]             # the same, one line each
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

GiB, MiB = 1 << 30, 1 << 20
DIRS_MAX = 8
WALK_DEPTH_MAX = 8                     # folders below a models folder
WALK_FILES_MAX = 20000                 # entries looked at per folder, so a whole disk cannot stall the page
META_MAX = 64 << 20                    # a .basalt meta section larger than this is not read
CACHE_VERSION = 3
COMMIT_MAX = 0.75                      # a conversion starts only below this share of the commit limit
DISK_MARGIN = 2 * GiB                  # free space left on the disk after a conversion
MTP_SECTION_BYTES = 116099072 + 707788800      # mtp/dense + mtp/experts in a converted file
MTP_FETCH_BYTES = 5214306726           # the drafter's BF16 tensors mtp_fetch.py writes
EXTRA_SECTION_BYTES = 32 * MiB         # tokenizer, profile, layout, meta and the vision text vocabulary

# Qwen3.8-Flash-Next as the engine runs it
GEOMETRY = {"arch": "qwen4exp", "n_embd": 2560, "n_ff": 640, "n_layer": 48, "n_expert_max": 512}
# expert formats the GPU kernels cover (iq_kernels.cu GU_FMTS / D_FMTS); the engine refuses a model without them
GU_TYPES = {16, 17, 18, 21, 22, 23, 29, 42, 12, 13, 6, 8}
D_TYPES = {20, 23, 42, 7, 6, 8}
EXPERT_RE = re.compile(r"^blk\.(\d+)\.ffn_(gate|up|down|gate_up)_exps\.weight$")
SHARD_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)
QUANT_RE = re.compile(r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ[1-4]_(?:XXS|XS|NL|S|M)|Q[2-8]_K(?:_(?:XL|L|M|S|P))?|Q[2-8]_[01]"
                      r"|PQ2_0|BF16|F16|F32|MXFP4|NVFP4))(?![A-Za-z0-9])", re.I)
# llama.cpp's general.file_type, for a name without the quantization in it
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
              12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
              19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL",
              26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
              38: "MXFP4"}

_lock = threading.RLock()
_cache: dict | None = None             # path -> {"size", "mtime", "info"}
_facts: dict = {}                      # (path, size, mtime) -> loader.model_facts()


def type_name(t: int) -> str:
    from gguf_reader import GGML_TYPES
    return GGML_TYPES.get(int(t), f"type{t}")


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _posix(p) -> str:
    return Path(os.path.abspath(str(p))).as_posix()


def _say(it: str, en: str) -> dict:
    return {"it": it, "en": en}


def gb(n) -> str:
    """GiB written "GB" with an Italian comma, as Windows Explorer shows file and disk sizes."""
    return f"{(n or 0) / GiB:,.1f}".replace(",", " ").replace(".", ",").replace(" ", ".")


# ------------------------------------------------------------------------------------------------ the folders
def store_path() -> Path:
    from serve import userdata
    return userdata.path("models.json")


def read_store() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    dirs = data.get("dirs")
    data["dirs"] = [d for d in dirs if isinstance(d, str) and d.strip()] if isinstance(dirs, list) else []
    if not isinstance(data.get("output_dir"), str):
        data["output_dir"] = None
    return data


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.stem + ".", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def write_store(data: dict) -> None:
    with _lock:
        _write_json(store_path(), data)


def dirs() -> list[str]:
    return read_store()["dirs"]


class DirsError(ValueError):
    """Folders that cannot be used: {index: {"it": ..., "en": ...}} and a message."""

    def __init__(self, errors: dict, message: str):
        super().__init__(message)
        self.errors = errors


def check_dir(raw) -> tuple[str | None, dict | None]:
    """(the folder as stored, None) or (None, the reason in Italian and English)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, _say("Scrivi il percorso di una cartella.", "Give a folder's path.")
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    if not os.path.isabs(text):
        return None, _say("Serve un percorso completo, per esempio D:\\Modelli o /home/nome/models.",
                          "Give a full path, e.g. D:\\Models or /home/name/models.")
    p = Path(text)
    if not p.exists():
        return None, _say("Questa cartella non esiste.", "This folder does not exist.")
    if not p.is_dir():
        return None, _say("È un file, non una cartella.", "This is a file, not a folder.")
    if p.parent == p:
        return None, _say("Scegli una cartella, non un disco intero (la ricerca sarebbe troppo lunga).",
                          "Choose a folder, not a whole disk (the search would take too long).")
    try:
        next(os.scandir(p), None)
    except OSError:
        return None, _say("Non riesco a leggere questa cartella (permessi?).", "This folder cannot be read (permissions?).")
    return str(p), None


def set_dirs(given, output_dir=...) -> dict:
    """Check and save the folders (DirsError with each bad one); the store as saved."""
    if not isinstance(given, list):
        raise DirsError({}, 'send {"dirs": ["D:\\\\Models", ...]}')
    if len(given) > DIRS_MAX:
        raise DirsError({}, f"at most {DIRS_MAX} folders")
    out, errors, seen = [], {}, set()
    for i, raw in enumerate(given):
        path, why = check_dir(raw)
        if why:
            errors[str(i)] = why
            continue
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    if errors:
        first = next(iter(errors.values()))
        raise DirsError(errors, first["en"])
    data = read_store()
    data["dirs"] = out
    if output_dir is not ...:
        data["output_dir"] = output_dir
    write_store(data)
    return data


def _fixed_drives() -> list[str]:
    """The local fixed disks (C:\\, D:\\, ...); network and removable drives are left out, they can be slow."""
    if os.name != "nt":
        return []
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    out = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i) and k32.GetDriveTypeW(f"{letter}:\\") == 3:      # DRIVE_FIXED
            out.append(f"{letter}:\\")
    return out


def default_model() -> str | None:
    """The model the server starts with when neither --model nor the profile names one ("Usa come modello
    predefinito per il server"), per user next to the folders."""
    m = read_store().get("default_model")
    return m if isinstance(m, str) and m else None


def set_default_model(path) -> str | None:
    """Save (a .basalt the engine can open) or clear (None) the default server model; ValueError with the reason."""
    value = check_basalt(path) if path else None
    data = read_store()
    data["default_model"] = value
    write_store(data)
    return value


def suggestions() -> list[str]:
    """Folders where models usually are that exist on this PC, found now: LM Studio's, the Hugging Face cache,
    ~/models, and a Models or Modelli folder at the top of each local disk."""
    home = Path.home()
    cands = [home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models",
             Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME") or home / ".cache" / "huggingface")
             / ("" if os.environ.get("HF_HUB_CACHE") else "hub"), home / "models", home / "Models"]
    for root in _fixed_drives():
        cands += [Path(root) / "Models", Path(root) / "Modelli"]
    out, seen = [], set()
    for c in cands:
        try:
            if c.is_dir() and _norm(c) not in seen:
                seen.add(_norm(c))
                out.append(str(c))
        except OSError:
            continue
    return out


# ------------------------------------------------------------------------------------------------ GGUF headers
class _Header:
    """A GGUF v2/v3 header read through a buffered file: arrays larger than 64 items are skipped, not kept."""

    def __init__(self, path: str):
        self.f = open(path, "rb", buffering=1 << 20)

    def close(self):
        self.f.close()

    def read(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError("the header ends early (an incomplete file?)")
        return b

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def string(self, keep=True):
        n = self.u64()
        if n > 64 << 20:
            raise ValueError("a header string longer than 64 MiB")
        if not keep:
            self.f.seek(n, 1)
            return None
        return self.read(n).decode("utf-8", "replace")

    SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    FMT = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}

    def value(self, t: int, keep=True):
        if t == 8:
            return self.string(keep)
        if t == 9:
            et, count = self.u32(), self.u64()
            small = count <= 64 and keep
            if et == 8:
                items = [self.string(small) for _ in range(count)]
                return items if small else {"len": count}
            if et == 9:
                raise ValueError("nested arrays are not read")
            size = self.SIZES.get(et)
            if size is None:
                raise ValueError(f"unknown value type {et}")
            if not small:
                self.f.seek(size * count, 1)
                return {"len": count}
            return list(struct.unpack(f"<{count}{self.FMT[et]}", self.read(size * count)))
        size = self.SIZES.get(t)
        if size is None:
            raise ValueError(f"unknown value type {t}")
        v = struct.unpack("<" + self.FMT[t], self.read(size))[0]
        return v if keep else None


def read_gguf(path: str) -> dict:
    """The key/value block (big arrays as {"len": n}) and the tensor directory of one GGUF file."""
    from gguf_reader import GGUF_MAGIC
    h = _Header(path)
    try:
        if struct.unpack("<I", h.read(4))[0] != GGUF_MAGIC:
            raise ValueError("not a GGUF file")
        version = h.u32()
        if version not in (2, 3):
            raise ValueError(f"GGUF version {version}")
        n_tensors, n_kv = h.u64(), h.u64()
        if n_tensors > 1 << 20 or n_kv > 1 << 20:
            raise ValueError("an implausible header")
        meta = {}
        for _ in range(n_kv):
            key = h.string()
            meta[key] = h.value(h.u32(), keep=not key.startswith("tokenizer.ggml."))
        tensors = []
        for _ in range(n_tensors):
            name = h.string()
            nd = h.u32()
            if nd > 8:
                raise ValueError(f"tensor {name} has {nd} dimensions")
            shape = list(struct.unpack(f"<{nd}Q", h.read(8 * nd)))
            t, off = h.u32(), h.u64()
            tensors.append((name, shape, t, off))
        align = meta.get("general.alignment") if isinstance(meta.get("general.alignment"), int) else 32
        pos = h.f.tell()
        data_start = (pos + align - 1) // align * align
    finally:
        h.close()
    return {"version": version, "meta": meta, "tensors": tensors, "data_start": data_start}


def _tensor_bytes(shape, t) -> int | None:
    from gguf_reader import BLOCK_GEOMETRY
    geom = BLOCK_GEOMETRY.get(type_name(t))
    if geom is None:
        return None
    n = 1
    for d in shape:
        n *= int(d)
    if n % geom[0]:
        return None
    return n // geom[0] * geom[1]


def _elements(shape) -> int:
    n = 1
    for d in shape:
        n *= int(d)
    return n


def gguf_info(path: str, size: int) -> dict:
    """What the scan keeps of one GGUF file (small, JSON)."""
    try:
        g = read_gguf(path)
    except (OSError, ValueError, struct.error, UnicodeDecodeError) as e:
        return {"kind": "gguf", "error": str(e)}
    m = g["meta"]
    arch = m.get("general.architecture") if isinstance(m.get("general.architecture"), str) else None
    k = (lambda name: m.get(f"{arch}.{name}")) if arch else (lambda name: None)  # noqa: E731
    info = {"kind": "gguf", "arch": arch, "name": m.get("general.name") if isinstance(m.get("general.name"), str) else None,
            "type": m.get("general.type") if isinstance(m.get("general.type"), str) else None,
            "file_type": m.get("general.file_type") if isinstance(m.get("general.file_type"), int) else None,
            "split_count": int(m.get("split.count") or 1) if isinstance(m.get("split.count"), int) else 1,
            "split_no": int(m.get("split.no") or 0) if isinstance(m.get("split.no"), int) else 0,
            "n_embd": k("embedding_length"), "n_ff": k("expert_feed_forward_length"), "n_layer": k("block_count"),
            "n_expert": k("expert_count"), "nextn": k("nextn_predict_layers"),
            "proj_dim": m.get("clip.vision.projection_dim"), "tensors": len(g["tensors"])}
    experts, unknown, end = [], [], 0
    dense_bytes, compat, quant_dense = 0, False, False
    from_types = {}
    form_of = None
    for name, shape, t, off in g["tensors"]:
        nb = _tensor_bytes(shape, t)
        if nb is None:
            unknown.append(f"{name} ({type_name(t)})")
            continue
        end = max(end, g["data_start"] + off + nb)
        mm = EXPERT_RE.match(name)
        if mm:
            experts.append([int(mm.group(1)), mm.group(2), int(t), [int(d) for d in shape], nb])
            continue
        if arch not in (GEOMETRY["arch"], None):         # later shards carry no architecture key
            continue
        if form_of is None:
            if str(ROOT / "tools" / "convert") not in sys.path:
                sys.path.insert(0, str(ROOT / "tools" / "convert"))
            import model_sections as S                   # numpy; only for the qwen4exp files
            form_of = S.form_of
            native_ple, not_in_dense = S.NATIVE_PLE_KEY, S.NOT_IN_DENSE
        if name in not_in_dense:
            continue
        tn, form = type_name(t), form_of(name)
        from_types[tn] = from_types.get(tn, 0) + 1
        floats = tn in ("BF16", "F32", "F16")
        if floats:
            if form is None or form == tn or (tn == "F32" and form == "F16"):
                dense_bytes += nb
            else:
                dense_bytes += _elements(shape) * (4 if form == "F32" else 2)
                if not (form == "F32" and tn in ("F16", "BF16")):
                    compat = True                        # F32 / F16 -> BF16 may round: --compat-bf16
        elif form is not None and not (name == "blk.1.ple_key.weight" and tn in native_ple):
            dense_bytes += _elements(shape) * 2          # dequantized to BF16 by --compat-bf16
            compat = quant_dense = True
    info.update(experts=experts, unknown=unknown[:8], unknown_count=len(unknown),
                truncated=bool(end and size < end), data_end=end, dense_bytes=dense_bytes, compat_bf16=compat,
                quant_dense=quant_dense)
    return info


# ------------------------------------------------------------------------------------------------ .basalt headers
def basalt_info(path: str, size: int) -> dict:
    """The table, the meta section (name, sources, file type) and experts.layout (expert formats) of a .basalt."""
    from model_file import HEADER, HEADER_BYTES, ModelFile, VERSION
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER.size)
        if len(head) < HEADER.size or head[:8] != b"BASALTMF":
            return {"kind": "basalt", "error": "magic", "detail": "not a .basalt model file"}
        _, version, _, file_bytes, _ = HEADER.unpack_from(head, 0)
        if version != VERSION:
            return {"kind": "basalt", "error": "version", "version": version, "want": VERSION}
        if size < file_bytes or size < HEADER_BYTES:
            return {"kind": "basalt", "error": "truncated", "size": size, "want": max(file_bytes, HEADER_BYTES)}
        mf = ModelFile(path)
    except (OSError, ValueError, struct.error) as e:
        return {"kind": "basalt", "error": "unreadable", "detail": str(e)}
    sec = {n: s for n, (o, s, d) in mf.sections.items()}
    info = {"kind": "basalt", "sections": len(sec), "experts_bytes": sec.get("experts", 0),
            "has_mtp": "mtp/dense" in sec, "has_vision": "vision/mmproj" in sec,
            "mmproj_bytes": sec.get("vision/mmproj", 0), "has_meta": "meta" in sec}
    missing = [n for n in ("meta", "dense", "experts", "experts.layout") if n not in sec]
    if missing:
        info["error"] = "sections"
        info["missing"] = missing
        return info
    if sec["meta"] <= META_MAX:
        try:
            meta = json.loads(mf.read("meta"))
            kv = meta.get("gguf_kv") if isinstance(meta.get("gguf_kv"), dict) else {}
            tensors = meta.get("tensors") if isinstance(meta.get("tensors"), dict) else {}
            info.update(name=meta.get("name") if isinstance(meta.get("name"), str) else None,
                        sources=[s.get("path") for s in meta.get("sources") or [] if isinstance(s, dict)][:8],
                        file_type=kv.get("general.file_type") if isinstance(kv.get("general.file_type"), int) else None,
                        gguf_name=kv.get("general.name") if isinstance(kv.get("general.name"), str) else None,
                        ple_bytes=int((tensors.get("per_layer_token_embd.weight") or {}).get("bytes") or 0))
        except (OSError, ValueError, AttributeError):
            info["meta_unreadable"] = True
    try:
        layout = mf.read("experts.layout").decode("utf-8", "replace")
        gu, d = set(), set()
        for line in layout.splitlines():
            f = line.split()
            if len(f) == 5 and not line.startswith("#") and all(x.isdigit() for x in f):
                gu.add(int(f[1]))
                d.add(int(f[2]))
        info.update(gu_types=sorted(gu), d_types=sorted(d))
    except OSError:
        pass
    return info


# ------------------------------------------------------------------------------------------------ the cache
def _cache_path() -> Path:
    from serve.engine.modelfile import cache_root
    return cache_root() / "models-scan.json"


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(_cache_path().read_text(encoding="utf-8"))
            _cache = data.get("files", {}) if isinstance(data, dict) and data.get("v") == CACHE_VERSION else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _write_json(_cache_path(), {"v": CACHE_VERSION, "files": _cache or {}})
    except OSError:
        pass


def file_info(path: str, st=None) -> dict:
    """The header description of one file, from the cache when its size and modification time are the same."""
    st = st or os.stat(path)
    key = _norm(path)
    with _lock:
        cache = _load_cache()
        hit = cache.get(key)
        if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime_ns:
            return hit["info"]
    info = basalt_info(path, st.st_size) if path.lower().endswith(".basalt") else gguf_info(path, st.st_size)
    with _lock:
        _load_cache()[key] = {"size": st.st_size, "mtime": st.st_mtime_ns, "info": info}
    return info


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = {}
        _facts.clear()
        _save_cache()


def facts_for(path: str) -> dict:
    """loader.model_facts() of a .basalt, cached by path, size and modification time."""
    from serve import loader
    st = os.stat(path)
    key = (_norm(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _facts:
            return dict(_facts[key])
    facts = loader.model_facts(path)
    with _lock:
        _facts[key] = facts
    return dict(facts)


def check_basalt(path) -> str:
    """A .basalt file the engine can open, as a POSIX path; ValueError with the reason otherwise."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("a .basalt file's path")
    p = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isabs(p):
        raise ValueError(f"{path}: give the full path of the .basalt file")
    if not p.lower().endswith(".basalt"):
        raise ValueError(f"{path}: not a .basalt file (a GGUF model is converted first)")
    if not os.path.isfile(p):
        raise ValueError(f"{path}: no such file")
    info = file_info(p)
    if info.get("error"):
        raise ValueError(f"{path}: {_basalt_reason(info)['en']}")
    return _posix(p)


# ------------------------------------------------------------------------------------------------ the scan
def _walk(folder: str, errors: list) -> tuple[list[str], list[str]]:
    """(model files, folders with an MTP manifest) under `folder`: hidden folders skipped, depth and size bounded."""
    files, mtp, seen = [], [], 0
    base = folder.rstrip("\\/").count(os.sep)
    for here, subdirs, names in os.walk(folder, onerror=lambda e: errors.append(str(e))):
        depth = here.rstrip("\\/").count(os.sep) - base
        subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and depth < WALK_DEPTH_MAX)
        for n in sorted(names):
            seen += 1
            low = n.lower()
            if low.endswith(".gguf") or low.endswith(".basalt"):
                files.append(os.path.join(here, n))
            elif low == "mtp-manifest.json":
                mtp.append(here)
        if seen > WALK_FILES_MAX:
            errors.append(f"{folder}: more than {WALK_FILES_MAX} files, the rest was not looked at")
            break
    return files, mtp


def _quant(*names, file_type=None) -> str | None:
    for n in names:
        if not n:
            continue
        found = QUANT_RE.findall(Path(n).name if ("/" in n or "\\" in n) else n)
        if found:
            return found[-1].upper()
    return FILE_TYPES.get(file_type) if file_type is not None else None


def _types_text(gu, d) -> str:
    gu_n = "/".join(type_name(t) for t in gu) or "?"
    d_n = "/".join(type_name(t) for t in d) or "?"
    return gu_n if gu_n == d_n else f"{gu_n} + {d_n} (down)"


def _id(path: str) -> str:
    return hashlib.sha1(_norm(path).encode()).hexdigest()[:12]


def _basalt_reason(info: dict) -> dict:
    e = info.get("error")
    if e == "magic":
        return _say("Non è un file .basalt (intestazione sconosciuta).", "Not a .basalt file (unknown header).")
    if e == "version":
        return _say(f"File .basalt di versione {info.get('version')}: questo Basalt legge la versione {info.get('want')}.",
                    f".basalt version {info.get('version')}: this Basalt reads version {info.get('want')}.")
    if e == "truncated":
        return _say(f"File incompleto: {gb(info.get('size'))} di {gb(info.get('want'))} GB (copia o conversione "
                    "interrotta?).", f"Incomplete file: {gb(info.get('size'))} of {gb(info.get('want'))} GB (an "
                    "interrupted copy or conversion?).")
    if e == "sections":
        miss = ", ".join(info.get("missing") or [])
        return _say(f"Mancano sezioni necessarie: {miss}.", f"Required sections are missing: {miss}.")
    return _say(f"Illeggibile: {info.get('detail') or e}.", f"Unreadable: {info.get('detail') or e}.")


def _basalt_item(path: str, size: int, info: dict) -> dict:
    stem = Path(path).stem
    item = {"id": _id(path), "kind": "basalt", "path": _posix(path), "files": [_posix(path)],
            "dir": _posix(Path(path).parent), "name": info.get("name") or stem, "size_bytes": size,
            "experts_bytes": info.get("experts_bytes") or 0, "mtp": "present" if info.get("has_mtp") else None,
            "vision": bool(info.get("has_vision")), "role": "model", "notes": []}
    item["quant"] = _quant(stem, *(info.get("sources") or []), info.get("name") or "", file_type=info.get("file_type"))
    if info.get("gu_types"):
        item["expert_types"] = _types_text(info["gu_types"], info.get("d_types") or [])
    if info.get("error"):
        item.update(status="incompatible", reason=_basalt_reason(info))
        return item
    item.update(status="ready", reason=_say("Pronto da caricare.", "Ready to load."))
    if info.get("ple_bytes"):
        item["ple_bytes"] = info["ple_bytes"]
    if info.get("meta_unreadable"):
        item["notes"].append(_say("La sezione meta non si legge: usa «Verifica».", "The meta section cannot be read: "
                                  "use Verify."))
    return item


def _gguf_role(info: dict, name: str) -> str:
    if info.get("arch") == "clip" or info.get("type") == "mmproj" or name.lower().startswith("mmproj"):
        return "mmproj"
    layers = {e[0] for e in info.get("experts") or []}
    if info.get("arch") == GEOMETRY["arch"] and (info.get("nextn") or 0) > 0 and len(layers) <= 1:
        return "drafter"
    return "model"


def _classify(group: dict, mtp_sources: list[str]) -> dict:
    """A GGUF model (one file or its shards): status, reason, notes and the conversion's settings."""
    infos, paths, sizes = group["infos"], group["paths"], group["sizes"]
    first = infos[0]
    item = {"id": _id(paths[0]), "kind": "gguf", "path": _posix(paths[0]), "files": [_posix(p) for p in paths],
            "dir": _posix(Path(paths[0]).parent), "name": first.get("name") or group["stem"], "size_bytes": sum(sizes),
            "role": "model", "notes": [], "arch": first.get("arch")}
    item["quant"] = _quant(group["stem"], first.get("name") or "", file_type=first.get("file_type"))
    experts = [e for i in infos for e in i.get("experts") or []]
    item["experts_bytes"] = sum(e[4] for e in experts)

    def no(it, en):
        item.update(status="incompatible", reason=_say(it, en))
        return item

    if group.get("missing"):
        miss = ", ".join(group["missing"][:4])
        return no(f"Mancano parti del modello ({len(group['missing'])} di {group['total']}): {miss}.",
                  f"Parts of the model are missing ({len(group['missing'])} of {group['total']}): {miss}.")
    bad = [Path(p).name for p, i in zip(paths, infos) if i.get("error")]
    if bad:
        err = next(i["error"] for i in infos if i.get("error"))
        return no(f"Intestazione GGUF illeggibile in {', '.join(bad)}: {err}.",
                  f"Unreadable GGUF header in {', '.join(bad)}: {err}.")
    cut = [Path(p).name for p, i in zip(paths, infos) if i.get("truncated")]
    if cut:
        return no(f"File incompleto (download interrotto?): {', '.join(cut)}.",
                  f"Incomplete file (an interrupted download?): {', '.join(cut)}.")
    arch = first.get("arch")
    if arch != GEOMETRY["arch"]:
        return no(f"Architettura «{arch or '?'}»: Basalt esegue solo Qwen3.8-Flash-Next (qwen4exp).",
                  f"Architecture “{arch or '?'}”: Basalt runs only Qwen3.8-Flash-Next (qwen4exp).")
    unknown = sum(i.get("unknown_count") or 0 for i in infos)
    if unknown:
        what = ", ".join(u for i in infos for u in i.get("unknown") or [])[:200]
        return no(f"{unknown} tensori in formati che il convertitore non legge: {what}.",
                  f"{unknown} tensors in formats the converter does not read: {what}.")
    geo = []
    for key, want, label in (("n_embd", GEOMETRY["n_embd"], "embedding"), ("n_ff", GEOMETRY["n_ff"], "FFN esperti"),
                             ("n_layer", GEOMETRY["n_layer"], "strati")):
        if first.get(key) != want:
            geo.append(f"{label} {first.get(key)} invece di {want}")
    if isinstance(first.get("n_expert"), int) and first["n_expert"] > GEOMETRY["n_expert_max"]:
        geo.append(f"{first['n_expert']} esperti invece di al massimo {GEOMETRY['n_expert_max']}")
    if geo:
        return no("Geometria diversa da Qwen3.8-Flash-Next: " + "; ".join(geo) + ".",
                  "Not Qwen3.8-Flash-Next's geometry: " + "; ".join(geo).replace(" invece di ", " instead of ")
                  .replace("strati", "layers").replace("FFN esperti", "expert FFN")
                  .replace("esperti invece di al massimo", "experts instead of at most") + ".")
    if not experts:
        return no("Nessun tensore degli esperti (blk.N.ffn_*_exps.weight).",
                  "No expert tensors (blk.N.ffn_*_exps.weight).")
    layers = {}
    for layer, role, t, shape, nb in experts:
        layers.setdefault(layer, {})[role] = t
    n_layer = first.get("n_layer") or 0
    gaps = [str(n) for n in range(n_layer) if not {"gate", "up", "down"} <= set(layers.get(n, {}))
            and not {"gate_up", "down"} <= set(layers.get(n, {}))]
    if gaps:
        return no(f"Strati senza tutti gli esperti (gate, up, down): {', '.join(gaps[:6])}.",
                  f"Layers without all their experts (gate, up, down): {', '.join(gaps[:6])}.")
    if any("gate_up" in r for r in layers.values()):
        return no("Esperti gate e up fusi in un solo tensore: il convertitore li vuole separati.",
                  "Fused gate/up expert tensors: the converter needs them separate.")
    gu = sorted({r["gate"] for r in layers.values()} | {r["up"] for r in layers.values()})
    d = sorted({r["down"] for r in layers.values()})
    item["expert_types"] = _types_text(gu, d)
    bad_gu, bad_d = [t for t in gu if t not in GU_TYPES], [t for t in d if t not in D_TYPES]
    if bad_gu or bad_d:
        parts_it, parts_en = [], []
        if bad_gu:
            names = "/".join(type_name(t) for t in bad_gu)
            parts_it.append(f"gate/up in {names}")
            parts_en.append(f"gate/up in {names}")
        if bad_d:
            names = "/".join(type_name(t) for t in bad_d)
            parts_it.append(f"down in {names}")
            parts_en.append(f"down in {names}")
        return no(f"Esperti {' e '.join(parts_it)}: il motore non ha kernel per questi formati (né su GPU né su CPU).",
                  f"Experts with {' and '.join(parts_en)}: the engine has no kernels for these formats (GPU or CPU).")
    if any(r["gate"] != r["up"] for r in layers.values()):
        return no("In alcuni strati gate e up hanno formati diversi: il convertitore li vuole uguali.",
                  "In some layers gate and up differ in format: the converter needs them equal.")
    # convertible
    item.update(status="convert", reason=_say("Si può convertire in .basalt.", "Can be converted to .basalt."))
    item["compat_bf16"] = any(i.get("compat_bf16") for i in infos)
    item["dense_bytes"] = sum(i.get("dense_bytes") or 0 for i in infos)
    if item["compat_bf16"]:
        item["notes"].append(_say("Alcune proiezioni piccole vanno portate a BF16 (--compat-bf16, arrotondamento "
                                  "registrato nel file).", "Some small projections are converted to BF16 "
                                  "(--compat-bf16, the rounding is recorded in the file)."))
    if mtp_sources:
        item["mtp"] = "source"
        item["mtp_source"] = mtp_sources[0]
    else:
        item["mtp"] = "fetch"
    if first.get("n_expert") and first["n_expert"] < GEOMETRY["n_expert_max"]:
        item["notes"].append(_say(f"Modello sfoltito: {first['n_expert']} esperti per strato.",
                                  f"A pruned model: {first['n_expert']} experts per layer."))
    return item


def _attach(models: list, extras: list) -> None:
    """Give each GGUF model the vision projector and the drafter of its folder; mark those files as attachments."""
    by_dir = {}
    for m in models:
        if m["kind"] == "gguf" and m.get("arch") == GEOMETRY["arch"]:
            by_dir.setdefault(m["dir"], []).append(m)
    for x in extras:
        owners = by_dir.get(x["dir"], [])
        owner = owners[0]["name"] if owners else None
        x["attached_to"] = owner
        if x["role"] == "mmproj":
            fits = x.get("proj_dim") in (None, GEOMETRY["n_embd"])
            if owner and fits:
                x["reason"] = _say(f"Proiettore visivo (mmproj) di «{owner}»: entra nel .basalt con la conversione.",
                                   f"Vision projector (mmproj) of “{owner}”: it goes into the .basalt when converted.")
            else:
                x["reason"] = _say("Proiettore visivo (mmproj): non è un modello da caricare da solo.",
                                   "Vision projector (mmproj): not a model to load on its own.")
            for m in owners:
                if fits and not m.get("mmproj"):
                    m["mmproj"] = x["path"]
                    m["vision"] = True
        else:
            x["reason"] = _say(f"File del drafter MTP{f' di «{owner}»' if owner else ''}: non è un modello da solo; "
                               "Basalt usa il drafter MTP originale di Qwen.",
                               f"MTP drafter file{f' of “{owner}”' if owner else ''}: not a model on its own; Basalt "
                               "uses Qwen's original MTP drafter.")
    for m in models:
        if m["kind"] == "gguf" and m.get("status") == "convert":
            m.setdefault("vision", False)
            m["estimate_bytes"] = (m["size_bytes"] + m.get("dense_bytes", 0) + MTP_SECTION_BYTES +
                                   EXTRA_SECTION_BYTES + (_size(m.get("mmproj")) if m.get("mmproj") else 0))


def _size(p) -> int:
    try:
        return os.path.getsize(p)
    except (OSError, TypeError):
        return 0


def scan(folders: list[str] | None = None, rescan: bool = False) -> dict:
    """Every model under the folders, with its status; headers only, cached."""
    folders = dirs() if folders is None else folders
    if rescan:
        with _lock:
            _load_cache().clear()
    errors, files, mtp = [], [], []
    for d in folders:
        if not os.path.isdir(d):
            errors.append(f"{d}: the folder is not there")
            continue
        f, m = _walk(d, errors)
        files += f
        mtp += m
    seen, unique = set(), []
    for p in files:
        if _norm(p) not in seen:
            seen.add(_norm(p))
            unique.append(p)
    mtp_ok = [_posix(m) for m in dict.fromkeys(mtp) if (Path(m) / "tensors").is_dir() or any(Path(m).glob("*.bin"))]
    infos = {}
    for p in unique:
        try:
            st = os.stat(p)
            infos[p] = (st.st_size, file_info(p, st))
        except OSError as e:
            errors.append(f"{p}: {e}")
    with _lock:
        _save_cache()
    models, extras = [], []
    groups = {}
    for p, (size, info) in infos.items():
        if p.lower().endswith(".basalt"):
            models.append(_basalt_item(p, size, info))
            continue
        name = Path(p).name
        m = SHARD_RE.match(name)
        key = (_norm(Path(p).parent), m.group(1).lower(), m.group(3)) if m else (_norm(p), name, "1")
        g = groups.setdefault(key, {"stem": m.group(1) if m else Path(p).stem, "total": int(m.group(3)) if m else 1,
                                    "parts": {}, "dir": str(Path(p).parent)})
        g["parts"][int(m.group(2)) if m else 1] = (p, size, info)
    for g in groups.values():
        nums = sorted(g["parts"])
        paths = [g["parts"][n][0] for n in nums]
        g["paths"], g["sizes"], g["infos"] = paths, [g["parts"][n][1] for n in nums], [g["parts"][n][2] for n in nums]
        g["missing"] = []
        if g["total"] > 1:
            g["missing"] = ["%s-%05d-of-%05d.gguf" % (g["stem"], n, g["total"]) for n in range(1, g["total"] + 1)
                            if n not in g["parts"]]
        first_info = g["parts"][nums[0]][2]
        role = "model" if first_info.get("error") or nums[0] != 1 else _gguf_role(first_info, Path(paths[0]).name)
        if role == "model":
            models.append(_classify(g, mtp_ok))
        else:
            extras.append({"id": _id(paths[0]), "kind": "gguf", "path": _posix(paths[0]),
                           "files": [_posix(p) for p in paths], "dir": _posix(g["dir"]),
                           "name": first_info.get("name") or g["stem"], "size_bytes": sum(g["sizes"]),
                           "role": role, "status": "incompatible", "arch": first_info.get("arch"),
                           "proj_dim": first_info.get("proj_dim"), "notes": [],
                           "quant": _quant(g["stem"], file_type=first_info.get("file_type"))})
    _attach(models, extras)
    order = {"ready": 0, "convert": 1, "incompatible": 2}
    out = sorted(models + extras, key=lambda m: (order.get(m["status"], 3), m["role"] != "model", m["name"].lower(),
                                                 m["path"].lower()))
    counts = {s: sum(1 for m in out if m["status"] == s) for s in order}
    return {"dirs": folders, "configured": bool(folders), "models": out, "counts": counts, "mtp_sources": mtp_ok,
            "errors": errors[:20], "scanned_at": time.time()}


def find(model_id: str, folders: list[str] | None = None) -> dict | None:
    for m in scan(folders)["models"]:
        if m["id"] == model_id or _norm(m["path"]) == _norm(model_id):
            return m
    return None


def describe(path: str) -> dict:
    """One model file's entry with its status, inside the models folders or not (its own folder is scanned for its
    shards and attachments); status "missing" when there is no such file."""
    if not path or not os.path.isfile(path):
        return {"path": _posix(path) if path else None, "status": "missing",
                "reason": _say("Il file non esiste.", "There is no such file.")}
    for m in scan([str(Path(path).parent)])["models"]:
        if any(_norm(f) == _norm(path) for f in m["files"]):
            return m
    return {"path": _posix(path), "status": "incompatible",
            "reason": _say("Non è un file .basalt né un modello GGUF.", "Neither a .basalt file nor a GGUF model.")}


SOURCES = {"argument": ("--model", "--model"), "profile": ("il modello del profilo", "the profile's model"),
           "default": ("il modello predefinito per il server", "the default server model"),
           "config": ("il modello del file di configurazione", "the config file's model")}


def resolve(model: str | None = None, profile: str | None = None, config: str | None = None) -> dict:
    """The model a start uses: --model, else the load profile's, else the default server model, else the config
    file's; with its status. {"model", "source", "status", "reason", "item"}; source None when none is set."""
    from serve import profiles, settings
    cands = [("argument", model)]
    if profile:
        try:
            cands.append(("profile", profiles.get_model(profile)))
        except (OSError, ValueError):
            pass
    cands.append(("default", default_model()))
    if config:
        try:
            cands.append(("config", settings.model_of(config)))
        except (OSError, ValueError):
            pass
    for source, path in cands:
        if path:
            p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"'))))
            item = describe(p)
            return {"model": _posix(p), "source": source, "source_text": _say(*SOURCES[source]),
                    "status": item["status"], "reason": item["reason"], "item": item}
    return {"model": None, "source": None, "status": "none",
            "reason": _say("Nessun modello scelto: passa --model \"<percorso del file .basalt>\", oppure scegli il "
                           "modello nell'app o nel Loader e premi «Usa come modello predefinito per il server».",
                           "No model chosen: pass --model \"<path of the .basalt file>\", or pick the model in the app "
                           "or the Loader and press “Use as the default server model”."), "item": None}


# ------------------------------------------------------------------------------------------------ conversion plan
def _commit_share() -> float | None:
    from serve import loader
    s = loader.system_now()
    lim, used = s.get("commit_limit"), s.get("commit_used")
    return None if not lim or used is None else used / lim


def _disk_of(p: str) -> str:
    drive = os.path.splitdrive(os.path.abspath(p))[0]
    return drive or os.path.abspath(p).split(os.sep)[0] or "/"


def default_output_dir(listing: dict | None = None) -> str | None:
    st = read_store()
    if st.get("output_dir") and os.path.isdir(st["output_dir"]):
        return st["output_dir"]
    listing = listing or scan()
    for m in listing["models"]:
        if m["status"] == "ready":
            return str(Path(m["path"]).parent)
    return listing["dirs"][0] if listing["dirs"] else None


def out_name(model: dict) -> str:
    stem = re.sub(r"-\d{5}-of-\d{5}$", "", Path(model["path"]).stem)
    return stem + ".basalt"


class Refused(Exception):
    """A request the jobs cannot take: the HTTP status and the body."""

    def __init__(self, status: int, body: dict):
        super().__init__((body.get("error") or {}).get("message", ""))
        self.status, self.body = status, body


def convert_plan(model_id: str, out_dir: str | None = None, folders: list[str] | None = None) -> dict:
    """What a conversion would do and need: the new file and its size, the disk and its free space, the commit, the
    command, the problems (each with Italian and English text) and the token a confirmation must carry back."""
    listing = scan(folders)
    model = next((m for m in listing["models"] if m["id"] == model_id or _norm(m["path"]) == _norm(model_id)), None)
    if model is None:
        raise Refused(404, {"error": {"type": "not_found", "message": "no such model in the models folders"}})
    problems = []

    def problem(code, it, en):
        problems.append({"code": code, "text_it": it, "text": en})

    if model["status"] != "convert":
        problem("not_convertible", "Questo modello non si può convertire: " + model["reason"]["it"],
                "This model cannot be converted: " + model["reason"]["en"])
    out_dir = (out_dir or "").strip().strip('"') or default_output_dir(listing) or ""
    out_path = os.path.join(out_dir, out_name(model)) if out_dir else ""
    free = None
    if not out_dir or not os.path.isabs(out_dir):
        problem("out_dir", "Scegli la cartella in cui scrivere il file .basalt (percorso completo).",
                "Choose the folder to write the .basalt file to (a full path).")
    elif not os.path.isdir(out_dir):
        problem("out_dir", "La cartella di uscita non esiste.", "The output folder does not exist.")
    else:
        try:
            free = shutil.disk_usage(out_dir).free
        except OSError:
            free = None
        if not os.access(out_dir, os.W_OK):
            problem("out_dir", "Nella cartella di uscita non si può scrivere.", "The output folder is not writable.")
        if os.path.exists(out_path):
            problem("exists", f"Esiste già {out_path}: spostalo o scegli un'altra cartella.",
                    f"{out_path} exists already: move it or choose another folder.")
    estimate = model.get("estimate_bytes") or 0
    fetch = model.get("mtp") == "fetch"
    fetch_dir = os.path.join(out_dir, "mtp-qwen") if out_dir else ""
    need = estimate + (MTP_FETCH_BYTES if fetch else 0) + DISK_MARGIN
    if free is not None and free < need:
        problem("disk", f"Spazio insufficiente su {_disk_of(out_dir)}: servono circa {gb(need)} GB (margine di "
                f"{gb(DISK_MARGIN)} GB compreso), liberi {gb(free)} GB.",
                f"Not enough space on {_disk_of(out_dir)}: about {gb(need)} GB needed ({gb(DISK_MARGIN)} GB margin "
                f"included), {gb(free)} GB free.")
    share = _commit_share()
    if share is not None and share >= COMMIT_MAX:
        problem("commit", f"La memoria impegnata (commit) è al {share * 100:.0f}% del limite: la conversione parte "
                f"solo sotto il {COMMIT_MAX * 100:.0f}%. Chiudi programmi pesanti e riprova.",
                f"The commit is at {share * 100:.0f}% of its limit: a conversion starts only below "
                f"{COMMIT_MAX * 100:.0f}%. Close heavy programs and try again.")
    if JOBS.running():
        problem("busy", "È già in corso una verifica o una conversione.", "A verification or a conversion is running.")
    args = ["--gguf", model["path"], "--out", _posix(out_path) if out_path else "", "--name",
            f"{model['name']}{' ' + model['quant'] if model.get('quant') and model['quant'] not in model['name'] else ''}"]
    if model.get("mmproj"):
        args += ["--mmproj", model["mmproj"]]
    else:
        args += ["--no-vision"]
    if model.get("compat_bf16"):
        args += ["--compat-bf16"]
    mtp_dir = model.get("mtp_source") or (_posix(fetch_dir) if fetch_dir else "")
    args += ["--mtp-source", mtp_dir]
    steps = (["fetch", "check", "convert"] if fetch else ["convert"])
    plan = {"model": {k: model.get(k) for k in ("id", "name", "path", "files", "quant", "size_bytes", "experts_bytes",
                                                "vision", "mmproj", "mtp", "mtp_source", "compat_bf16", "status")},
            "out_dir": _posix(out_dir) if out_dir else "", "out_path": _posix(out_path) if out_path else "",
            "estimate_bytes": estimate, "fetch_bytes": MTP_FETCH_BYTES if fetch else 0,
            "fetch_dir": _posix(fetch_dir) if fetch and fetch_dir else None, "disk": _disk_of(out_dir) if out_dir else "",
            "free_bytes": free, "need_bytes": need, "commit_share": share, "commit_max": COMMIT_MAX,
            "args": args, "steps": steps, "problems": problems, "ok": not problems}
    plan["token"] = hashlib.sha256(json.dumps([model["id"], plan["out_path"], estimate, args, steps],
                                              sort_keys=True).encode()).hexdigest()[:16]
    plan["summary"] = _say(
        f"Scrive un nuovo file di circa {gb(estimate)} GB sul disco {plan['disk'] or '?'} "
        f"(liberi {gb(free) if free is not None else '?'} GB)"
        + (f", dopo aver scaricato il drafter MTP originale di Qwen (circa {gb(MTP_FETCH_BYTES)} GB) in {fetch_dir}"
           if fetch else "") + ".",
        f"Writes a new file of about {gb(estimate).replace(',', '.')} GB on disk {plan['disk'] or '?'} "
        f"({gb(free).replace(',', '.') if free is not None else '?'} GB free)"
        + (f", after downloading Qwen's original MTP drafter (about {gb(MTP_FETCH_BYTES).replace(',', '.')} GB) to "
           f"{fetch_dir}" if fetch else "") + ".")
    return plan


# ------------------------------------------------------------------------------------------------ the jobs
PY = sys.executable
TOOLS = {"verify": [str(ROOT / "tools" / "model_file.py"), "verify"],
         "convert": [str(ROOT / "tools" / "convert" / "convert.py")],
         "fetch": [str(ROOT / "tools" / "convert" / "mtp_fetch.py"), "fetch"],
         "check": [str(ROOT / "tools" / "convert" / "mtp_fetch.py"), "verify"]}
LOG_LINES = 300


class Jobs:
    """One verification or conversion at a time, its progress and the tail of its output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.cancelled = False
        self.state = {"phase": "idle", "kind": None}
        self.log: list[str] = []

    def running(self) -> bool:
        with self.lock:
            return self.state.get("phase") == "running"

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "log": self.log[-60:]}

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _line(self, text: str):
        text = text.rstrip("\r\n")
        with self.lock:
            self.log.append(text)
            if len(self.log) > LOG_LINES:
                del self.log[:len(self.log) - LOG_LINES]
            f = self.state.get("log_file")
        if f:
            try:
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass

    def _begin(self, kind: str, **kw) -> None:
        with self.lock:
            if self.state.get("phase") == "running":
                raise Refused(409, {"error": {"type": "busy", "code": "busy",
                                              "message": "a verification or a conversion is running"}})
            from serve.engine.modelfile import cache_root
            logs = cache_root() / "models-jobs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.cancelled = False
            self.log = []
            self.state = {"phase": "running", "kind": kind, "started": time.time(), "finished": None, "error": None,
                          "progress": 0.0, "step": None, "steps": [], "log_file": str(logs / f"{stamp}-{kind}.log"),
                          **kw}

    def cancel(self) -> bool:
        with self.lock:
            proc = self.proc
            if self.state.get("phase") != "running":
                return False
            self.cancelled = True
        if proc is not None and proc.poll() is None:
            _kill_tree(proc)
        return True

    # ---- running a tool
    def _run(self, step: str, cmd: list[str], watch=None) -> int:
        self._set(step=step)
        self._line(f"$ {' '.join(cmd)}")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        try:
            from serve.util.winjob import contain
            contain(proc)                                # it ends with the server, however that ends
        except Exception:  # noqa: BLE001 - outside Windows, or no job: the process still runs
            pass
        with self.lock:
            self.proc = proc
            self.state["pid"] = proc.pid
        stop = threading.Event()
        if watch is not None:
            def poll():
                while not stop.wait(1.0):
                    try:
                        watch()
                    except Exception:  # noqa: BLE001 - a progress reading is not worth a failure
                        pass
            threading.Thread(target=poll, daemon=True).start()
        try:
            for line in proc.stdout:
                self._line(line)
                if watch is None and line.startswith("progress "):
                    try:
                        _, done, total = line.split()
                        self._set(progress=min(1.0, int(done) / max(1, int(total))), bytes_done=int(done),
                                  bytes_total=int(total))
                    except ValueError:
                        pass
            return proc.wait()
        finally:
            stop.set()
            with self.lock:
                self.proc = None

    def _end(self, code: int, what: str):
        if self.cancelled:
            self._set(phase="cancelled", finished=time.time(), error=None)
            self._line("[annullato]")
        elif code == 0:
            self._set(phase="done", finished=time.time(), progress=1.0)
        else:
            self._set(phase="failed", finished=time.time(), error=f"{what} (exit {code})")

    # ---- verify
    def start_verify(self, path: str) -> dict:
        try:
            p = check_basalt(path)
        except ValueError as e:
            raise Refused(400, {"error": {"type": "invalid_request_error", "message": str(e)}}) from None
        self._begin("verify", path=p, name=Path(p).name, steps=["verify"])

        def work():
            try:
                code = self._run("verify", [PY, *TOOLS["verify"], p])
                self._end(code, "a section does not match its SHA-256" if code == 3 else "the verification failed")
                if code == 0 and not self.cancelled:
                    self._set(result="ok")
                elif code == 3:
                    self._set(result="bad")
            except Exception as e:  # noqa: BLE001 - reported on the page
                self._set(phase="failed", finished=time.time(), error=str(e))
        threading.Thread(target=work, name="models-verify", daemon=True).start()
        return self.status()

    # ---- convert
    def start_convert(self, model_id: str, out_dir: str | None, token, confirm, folders=None) -> dict:
        plan = convert_plan(model_id, out_dir, folders)
        if confirm is not True or not token:
            raise Refused(409, {"error": {"type": "confirmation_required", "code": "confirm",
                                          "message": "a conversion writes a large new file: show the plan and send "
                                                     "it back with confirm: true and its token"}, "plan": plan})
        if token != plan["token"]:
            raise Refused(409, {"error": {"type": "confirmation_required", "code": "plan_changed",
                                          "message": "the plan changed since it was shown: confirm the new one"},
                                "plan": plan})
        if not plan["ok"]:
            raise Refused(422, {"error": {"type": "refused", "code": plan["problems"][0]["code"],
                                          "message": " ".join(x["text"] for x in plan["problems"])}, "plan": plan})
        self._begin("convert", name=plan["model"]["name"], out_path=plan["out_path"], steps=plan["steps"],
                    estimate_bytes=plan["estimate_bytes"], plan=plan)
        data = read_store()
        data["output_dir"] = plan["out_dir"]
        write_store(data)

        def work():
            try:
                if "fetch" in plan["steps"]:
                    fdir = plan["fetch_dir"]
                    os.makedirs(fdir, exist_ok=True)
                    code = self._run("fetch", [PY, *TOOLS["fetch"], "--out", fdir],
                                     watch=lambda: self._set(progress=min(0.99, _dir_bytes(fdir) / MTP_FETCH_BYTES) * 0.1))
                    if code != 0 or self.cancelled:
                        return self._end(code or 1, "the drafter download failed")
                    code = self._run("check", [PY, *TOOLS["check"], "--out", fdir])
                    if code != 0 or self.cancelled:
                        return self._end(code or 1, "the downloaded drafter does not match its SHA-256")
                base = 0.1 if "fetch" in plan["steps"] else 0.0
                partial = plan["out_path"] + ".partial"
                est = max(1, plan["estimate_bytes"])
                code = self._run("convert", [PY, *TOOLS["convert"], *plan["args"]],
                                 watch=lambda: self._set(progress=base + (1 - base) * min(0.99, _size(partial) / est),
                                                         bytes_done=_size(partial), bytes_total=est))
                self._end(code, "the conversion failed")
                if code == 0 and not self.cancelled:
                    clear_cache()
            except Exception as e:  # noqa: BLE001 - reported on the page
                self._set(phase="failed", finished=time.time(), error=str(e))
        threading.Thread(target=work, name="models-convert", daemon=True).start()
        return self.status()


def _dir_bytes(d: str) -> int:
    total = 0
    for here, _, names in os.walk(d):
        for n in names:
            total += _size(os.path.join(here, n))
    return total


def _kill_tree(proc) -> None:
    """End a job's process and whatever it started, by its PID (the venv's python.exe starts the interpreter as a
    child)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.kill()


JOBS = Jobs()


def _convert_now(model_id: str, out_dir, token, folders) -> int:
    """Run a confirmed conversion in this process, printing its output and progress (the start script's -Converti)."""
    try:
        JOBS.start_convert(model_id, out_dir, token, True, folders)
    except Refused as e:
        print(json.dumps(e.body, ensure_ascii=False))
        return 3
    shown, last = 0, -1
    try:
        while True:
            st = JOBS.status()
            with JOBS.lock:
                lines = JOBS.log[shown:]
                shown += len(lines)
            for line in lines:
                print(line, flush=True)
            pct = round((st.get("progress") or 0) * 100)
            if pct != last and st["phase"] == "running":
                print(f"progress {pct}%", flush=True)
                last = pct
            if st["phase"] != "running":
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        JOBS.cancel()
        while JOBS.running():
            time.sleep(0.2)
    st = JOBS.status()
    print(json.dumps({k: st.get(k) for k in ("phase", "error", "out_path")}, ensure_ascii=False), flush=True)
    return 0 if st["phase"] == "done" else 1


# ------------------------------------------------------------------------------------------------ the page's view
def listing(rescan: bool = False, config_model: str | None = None) -> dict:
    """GET /api/models: the folders, the models with their status, the running job."""
    st = read_store()
    out = scan(st["dirs"], rescan) if st["dirs"] else {"dirs": [], "configured": False, "models": [], "counts": {},
                                                        "mtp_sources": [], "errors": [], "scanned_at": time.time()}
    out["suggestions"] = suggestions() if not st["dirs"] else []
    out["output_dir"] = st.get("output_dir")
    out["default_model"] = default_model()
    out["job"] = JOBS.status()
    if config_model:
        out["current"] = _posix(config_model)
        listed = any(_norm(m["path"]) == _norm(config_model) for m in out["models"])
        out["current_listed"] = listed
        if not listed and os.path.isfile(config_model):
            try:
                st_ = os.stat(config_model)
                item = _basalt_item(config_model, st_.st_size, file_info(config_model, st_))
                item["outside"] = True
                out["models"].insert(0, item)
            except OSError:
                pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m serve.models", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dirs", help="the models folders and the suggestions, as JSON")
    sd = sub.add_parser("set-dirs", help="check and save the models folders")
    sd.add_argument("dirs", nargs="*")
    js = sub.add_parser("json", help="the models with their status, as JSON")
    js.add_argument("--rescan", action="store_true")
    js.add_argument("--config", help="a config file whose model is listed even outside the folders")
    ls = sub.add_parser("list", help="the models, one line each")
    ls.add_argument("--rescan", action="store_true")
    df = sub.add_parser("default", help="the default server model (or --set FILE.basalt / --clear)")
    df.add_argument("--set", dest="set_to")
    df.add_argument("--clear", action="store_true")
    rs = sub.add_parser("resolve", help="the model a start uses (--model > profile > default > config), as JSON; "
                                        "exit 0 ready, 5 to convert, 6 not usable, 7 none chosen")
    rs.add_argument("--model")
    rs.add_argument("--profile")
    rs.add_argument("--config")
    cv = sub.add_parser("convert", help="the conversion plan of a GGUF model as JSON; with --yes --token, run it")
    cv.add_argument("--model", required=True, help="the GGUF file (shard 1 of a split model)")
    cv.add_argument("--out", help="the folder to write the .basalt file to")
    cv.add_argument("--yes", action="store_true", help="run the conversion (after the plan was shown and confirmed)")
    cv.add_argument("--token", help="the plan's token, from a run without --yes")
    a = ap.parse_args(argv)
    if a.cmd == "default":
        try:
            if a.clear:
                set_default_model(None)
            elif a.set_to:
                set_default_model(a.set_to)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(json.dumps({"default_model": default_model()}, ensure_ascii=False))
        return 0
    if a.cmd == "resolve":
        out = resolve(a.model, a.profile, a.config)
        print(json.dumps(out, ensure_ascii=False))
        return {"ready": 0, "convert": 5, "none": 7}.get(out["status"], 6)
    if a.cmd == "convert":
        item = describe(os.path.abspath(a.model))
        if item.get("status") != "convert":
            print(json.dumps({"ok": False, "status": item.get("status"), "reason": item.get("reason")},
                             ensure_ascii=False))
            return 6
        folders = [str(Path(a.model).parent)]
        try:
            plan = convert_plan(item["id"], a.out, folders)
        except Refused as e:
            print(json.dumps(e.body, ensure_ascii=False))
            return 6
        if not a.yes:
            print(json.dumps(plan, ensure_ascii=False))
            return 0 if plan["ok"] else 3
        return _convert_now(item["id"], a.out, a.token, folders)
    if a.cmd == "dirs":
        st = read_store()
        print(json.dumps({"dirs": st["dirs"], "configured": bool(st["dirs"]), "suggestions": suggestions(),
                          "output_dir": st.get("output_dir"), "file": str(store_path())}, ensure_ascii=False))
        return 0
    if a.cmd == "set-dirs":
        try:
            st = set_dirs(a.dirs)
        except DirsError as e:
            for i, why in e.errors.items():
                print(f"{a.dirs[int(i)]}: {why['it']}", file=sys.stderr)
            if not e.errors:
                print(str(e), file=sys.stderr)
            return 2
        print(json.dumps({"dirs": st["dirs"], "configured": bool(st["dirs"])}, ensure_ascii=False))
        return 0
    if a.cmd == "json":
        model = None
        if a.config:
            try:
                from serve import settings
                model = settings.values_of(settings.read_file(a.config)).get("server.model")
                if model and not os.path.isabs(model):
                    model = str(Path(a.config).resolve().parent / model)
            except (OSError, ValueError):
                model = None
        out = listing(a.rescan, model)
        out.pop("job", None)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    out = listing(a.rescan)
    label = {"ready": "Pronto", "convert": "Da convertire", "incompatible": "Non compatibile"}
    for m in out["models"]:
        size = f"{m['size_bytes'] / 1e9:.1f} GB"
        extra = []
        if m.get("quant"):
            extra.append(m["quant"])
        if m.get("experts_bytes"):
            extra.append(f"esperti {m['experts_bytes'] / GiB:.1f} GiB")
        if m.get("mtp"):
            extra.append({"present": "MTP", "source": "MTP da mtp-qwen", "fetch": "MTP da scaricare"}[m["mtp"]])
        if m.get("vision"):
            extra.append("visione")
        if m.get("estimate_bytes"):
            extra.append(f"diventa ~{m['estimate_bytes'] / GiB:.2f} GiB")
        print(f"[{label[m['status']]}] {m['name']} | {size} | {' · '.join(extra)}\n    {m['path']}\n    "
              f"{m['reason']['it']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
