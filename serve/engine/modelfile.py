"""Engine settings, tokenizer and vision inputs for a .basalt model file, unpacked once into the cache directory."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from model_file import ModelFile, meta_sampling  # noqa: E402

VISION_EXE = "basalt-vision"


def cache_root() -> Path:
    return Path(os.environ.get("BASALT_CACHE") or Path.home() / ".cache" / "basalt")


def cache_dir(mf: ModelFile) -> Path:
    """One cache directory per model, named by the meta section's sha256 prefix."""
    return cache_root() / mf.sections["meta"][2].hex()[:16]


def write_section(mf: ModelFile, name: str, dest: Path) -> None:
    """Copy a section to `dest` via a temporary name so a partial file is never used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    off, size = mf.section(name)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".")
    try:
        with os.fdopen(fd, "wb") as out, open(mf.path, "rb") as src:
            src.seek(off)
            left = size
            while left > 0:
                b = src.read(min(left, 64 << 20))
                if not b:
                    raise IOError(f"{mf.path}: short read in {name}")
                out.write(b)
                left -= len(b)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def extract_tokenizer(mf: ModelFile) -> Path:
    """The `tokenizer/*` sections as a directory, reused when present."""
    out = cache_dir(mf) / "tokenizer"
    if (out / "vocab.json").exists():
        return out
    names = [n for n in mf.names() if n.startswith("tokenizer/")]
    if not any(n == "tokenizer/vocab.json" for n in names):
        raise ValueError(f"{mf.path}: no tokenizer sections")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out.parent, prefix="tokenizer."))
    try:
        for n in names:
            (tmp / n.split("/", 1)[1]).write_bytes(mf.read(n))
        try:
            os.rename(tmp, out)
        except OSError:                          # another process finished first
            if not (out / "vocab.json").exists():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def gpu_count(visible_env: str | None = None) -> int:
    """The NVIDIA GPUs this process can see: CUDA_VISIBLE_DEVICES when set, else what nvidia-smi lists."""
    v = os.environ.get("CUDA_VISIBLE_DEVICES") if visible_env is None else visible_env
    if v is not None:
        return len([x for x in v.split(",") if x.strip() and x.strip() != "-1"])
    try:
        out = subprocess.run(["nvidia-smi", "--list-gpus"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if line.startswith("GPU "))


def projector_file(mf: ModelFile, vdir: Path, chosen: str | None = None) -> Path:
    """The projector GGUF the engine is given: the one chosen (a file read in place), else the model file's own, unpacked into the
    cache; written again with the types the encoder reads when it has others (serve/projector.py). ValueError names what is wrong."""
    from serve import models, projector
    if chosen:
        src, off = os.path.abspath(chosen), 0
        if not os.path.isfile(src):
            raise ValueError(f"the vision projector {chosen} is not there")
        size, label = os.path.getsize(src), "the chosen vision projector"
    else:
        src, (off, size), label = mf.path, mf.section("vision/mmproj"), "the vision projector in this model file"
    g = models.read_gguf(src, off)
    problem = models.projector_problem(g)
    if problem:
        raise ValueError(f"{label} is not one the engine reads: {problem['en']}")
    fix = models.projector_fix(g)
    if not fix["tensors"]:
        if chosen:
            return Path(src)
        dest = vdir / "mmproj.gguf"
        if not dest.exists() or dest.stat().st_size != size:
            write_section(mf, "vision/mmproj", dest)
        return dest
    dest = vdir / projector.made_name(src, off, size, mf.sections["meta"][2].hex() if not chosen else "")
    if not dest.exists() or dest.stat().st_size != fix["bytes"]:
        projector.rewrite(src, str(dest), off)
    for old in vdir.glob("mmproj-*.bf16.gguf"):
        if old != dest:
            try:
                old.unlink()
            except OSError:
                pass
    return dest


def vision_config(mf: ModelFile, engine_bin: str, sidecar: bool = True, projector: str | None = None) -> dict:
    """The vision sidecar's config (the projector alone without `sidecar`); `projector` is a projector file to use in place of the
    model file's own; ValueError names what is missing."""
    side = Path(engine_bin).resolve().parent / VISION_EXE
    if os.name == "nt" and not side.is_file() and side.with_suffix(".exe").is_file():
        side = side.with_suffix(".exe")                 # the Windows build's binasalt-vision.exe
    if "vision/mmproj" not in mf.sections and not projector:
        raise ValueError("this model file has no vision projector (vision/mmproj); drop --vision")
    if sidecar and not side.is_file():
        raise ValueError(f"{side} not found (build the vision sidecar), or drop --vision")
    if "vision/text_vocab" not in mf.sections:
        raise ValueError("this model file has no vision/text_vocab section; drop --vision")
    vdir = cache_dir(mf) / "vision"
    vocab = vdir / "text_vocab.gguf"
    if not vocab.exists() or vocab.stat().st_size != mf.section("vision/text_vocab")[1]:
        write_section(mf, "vision/text_vocab", vocab)
    return {"vision_bin": str(side), "mmproj": str(projector_file(mf, vdir, projector)), "model": str(vocab)}


def model_config(path: str, engine_bin: str, gpus: int | None = None, vision: bool | str = False,
                 engine_words: list[str] | None = None, projector: str | None = None) -> dict:
    """The engine settings the file implies; `engine_words` follow its arguments as given (the engine's parser lets a
    later flag win), `vision` (True, "peer"
    or "cpu") adds the sidecar and "dynamic" the in-engine encoder (ValueError if it cannot be set up), reading `projector`
    when one is given. A second visible GPU becomes the peer tier."""
    mf = ModelFile(path)
    meta = json.loads(mf.read("meta"))
    path = os.path.abspath(path)
    words = list(engine_words or [])
    args = ["--model", path]   # settings come from the engine defaults and the server
    if "--peer-device" not in words and (gpu_count() if gpus is None else gpus) > 1:
        args += ["--peer-device", "1"]
    args += words
    cdir = cache_dir(mf)
    cdir.mkdir(parents=True, exist_ok=True)
    cfg = {"engine_bin": os.path.abspath(engine_bin), "args": args, "model_name": meta.get("name") or "basalt",
           "sampling": meta_sampling(meta)[0], "log": str(cdir / "engine.log"),
           "tokenizer": str(extract_tokenizer(mf))}
    v = vision_config(mf, engine_bin, sidecar=vision != "dynamic", projector=projector) if vision else None
    if v:
        v["mode"] = "on" if vision is True else vision
        v["gpu"] = vision != "cpu"
        cfg["vision"] = v
        if vision == "dynamic":     # the engine encodes: it reads the projector itself
            args += ["--vision-mmproj", v["mmproj"]]
        if "--vision" not in args:   # image embeddings need --vision
            args.append("--vision")
    return cfg
