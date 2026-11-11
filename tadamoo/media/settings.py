"""The settings of the picture, video and talking-head tools, in one small JSON file (`media.json` in the user data folder): on or off, where LTX Studio
lives, the Pythons of its environments (empty: the ones LTX Studio itself names), the VRAM each kind of job asks for, how long a job may
wait for the lease and for how long it may be silent."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from tadamoo.media.errors import MediaError

DEFAULT_LTX_DIRS = tuple(p for p in (os.environ.get("TADAMOO_LTX_STUDIO", ""), "E:/AI/LTXStudio") if p)
VIDEO_MODEL = "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors"
DEFAULTS: Dict[str, Any] = {"enabled": True, "ltx_dir": "", "image_python": "", "video_python": "", "video_model": "",
                            "image_mib": 11000, "video_mib": 25000, "avatar_mib": 25000, "wait_s": 600, "idle_s": 900}
FLAGS = ("enabled",)
FOLDERS = ("ltx_dir",)
FILES = ("image_python", "video_python")
RANGES = {"image_mib": (2000, 60000), "video_mib": (2000, 60000), "avatar_mib": (2000, 60000), "wait_s": (0, 3600), "idle_s": (30, 7200)}


def _number(value: Any, lo: float, hi: float) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _text(value: Any, limit: int = 500) -> bool:
    return isinstance(value, str) and len(value) <= limit and "\x00" not in value


def ltx_dir_of(configured: str) -> str:
    """The LTX Studio folder: the configured one when it exists, else the first default that does; "" when none."""
    if configured:
        return configured if Path(configured).is_dir() else ""
    return next((d for d in DEFAULT_LTX_DIRS if Path(d).is_dir()), "")


class MediaSettings:
    def __init__(self, file: str | Path) -> None:
        self.file = Path(file)
        self._lock = threading.Lock()

    def read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        out = dict(DEFAULTS)
        if isinstance(raw, dict):
            for key in FLAGS:
                if isinstance(raw.get(key), bool):
                    out[key] = raw[key]
            for key in FOLDERS + FILES + ("video_model",):
                if _text(raw.get(key)):
                    out[key] = raw[key].strip()
            for key, (lo, hi) in RANGES.items():
                if _number(raw.get(key), lo, hi):
                    out[key] = raw[key]
        return out

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        extra = [k for k in changes if k not in DEFAULTS]
        if extra:
            raise MediaError("unknown field: " + ", ".join(sorted(map(str, extra))[:5]))
        with self._lock:
            now = self.read()
            for key in FLAGS:
                if key in changes:
                    if not isinstance(changes[key], bool):
                        raise MediaError(f"{key}: true or false")
                    now[key] = changes[key]
            for key in FOLDERS:
                if key in changes:
                    value = changes[key]
                    if not _text(value) or (value.strip() and not Path(value.strip()).is_dir()):
                        raise MediaError(f"{key}: the folder of LTX Studio, or empty to look for it")
                    now[key] = value.strip()
            for key in FILES:
                if key in changes:
                    value = changes[key]
                    if not _text(value) or (value.strip() and not Path(value.strip()).is_file()):
                        raise MediaError(f"{key}: the python of an environment, or empty to use the one LTX Studio names")
                    now[key] = value.strip()
            if "video_model" in changes:
                value = changes["video_model"]
                if not _text(value, 200) or any(c in value for c in "/\\"):
                    raise MediaError("video_model: the file name of an LTX 2.5 checkpoint (no path), or empty")
                now["video_model"] = value.strip()
            for key, (lo, hi) in RANGES.items():
                if key in changes:
                    if not _number(changes[key], lo, hi):
                        raise MediaError(f"{key}: a whole number from {lo} to {hi}")
                    now[key] = changes[key]
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(self.file.name + ".tmp")
            tmp.write_text(json.dumps(now, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
            return now
