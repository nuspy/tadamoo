"""What an avatar gives to a phone or to the ESP32: the files, the manifest that lists them with their SHA-256, the zip, and the Wav2Lip model.

Published paths (relative, forward slashes): `poster.jpg`, `wav2lip/face.png`, `wav2lip/box.json`, `visemes/<V>.png` for every viseme of
viseme_gen.py, `visemes/frame.json`. The manifest is `{version, avatar, created, render_modes, files: {path: sha256}}`; `render_modes` is what
the files allow: `wav2lip` (face and box), `visemes` (the 15 sprites and their frame), `video` (a clip prepared for MuseTalk on the PC)."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

from . import models_store
from .viseme_gen import VISEMI

VERSION = 1
VISEMES_DIR = "visemes"
FRAME_FILE = "frame.json"
MANIFEST = "manifest.json"
MODEL_ID = "wav2lip-int8"
MODEL_FILE = "wav2lip_int8.onnx"
WAV2LIP_FILES = ("wav2lip/face.png", "wav2lip/box.json")
VISEME_FILES = tuple(f"{VISEMES_DIR}/{v}.png" for v in VISEMI) + (f"{VISEMES_DIR}/{FRAME_FILE}",)

_sums: dict[tuple, str] = {}
_lock = threading.Lock()


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file, remembered while its size and modification time stay the same."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _sums:
            return _sums[key]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    with _lock:
        _sums[key] = h.hexdigest()
        if len(_sums) > 256:
            _sums.pop(next(iter(_sums)))
    return h.hexdigest()


def _have(folder: Path, files) -> bool:
    return all((folder / f).is_file() for f in files)


def files_of(folder: Path) -> dict[str, Path]:
    """{published path: file} of everything the folder holds for a phone."""
    out = {}
    for rel in ("poster.jpg",) + WAV2LIP_FILES + VISEME_FILES:
        if (folder / rel).is_file():
            out[rel] = folder / rel
    return out


def render_modes(folder: Path, video: bool = False) -> list[str]:
    """The modes the avatar's files allow, in the order of the degradation chain; `video` says a clip is prepared for MuseTalk."""
    modes = []
    if _have(folder, ("poster.jpg",) + WAV2LIP_FILES):
        modes.append("wav2lip")
    if _have(folder, ("poster.jpg",) + VISEME_FILES):
        modes.append("visemes")
    if video:
        modes.append("video")
    return modes


def manifest(folder: Path, name: str, created: str = "", video: bool = False) -> dict:
    return {"version": VERSION, "avatar": name, "created": created, "render_modes": render_modes(folder, video),
            "files": {rel: sha256_of(path) for rel, path in files_of(folder).items()}}


def zip_bytes(folder: Path, man: dict) -> bytes:
    """The files of the manifest and the manifest itself, in one zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in man["files"]:
            z.write(folder / rel, rel)
        z.writestr(MANIFEST, json.dumps(man, indent=1))
    return buf.getvalue()


def model_path(base: str | None = None) -> Path | None:
    """The Wav2Lip file the phones download, or None when it is not installed."""
    if not models_store.is_installed(MODEL_ID, base):
        return None
    return Path(models_store.folder("avatar", MODEL_ID, base)) / MODEL_FILE
