"""The parameters of a picture or a clip: defaults, hard caps, and the rounding the models need. A value above a cap is refused with the cap in the
message; a size or a length that is not one the model takes is rounded, and the result says so."""
from __future__ import annotations

import secrets
from typing import Any, Dict, List, Tuple

from tadamoo.media.errors import MediaError

PROMPT_MAX = 4000
IMAGE = {"width": 1024, "height": 1024, "steps": 20, "side_min": 256, "side_max": 2048, "pixels_max": 2048 * 2048, "steps_max": 50, "multiple": 32}
VIDEO = {"width": 704, "height": 512, "frames": 65, "fps": 25, "side_min": 256, "side_max": 1280, "pixels_max": 1280 * 720, "frames_max": 161,
         "frames_min": 9, "fps_min": 8, "fps_max": 30, "multiple": 64}


def _whole(name: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise MediaError(f"{name}: a whole number from {lo} to {hi}")
    value = int(round(value))
    if not lo <= value <= hi:
        raise MediaError(f"{name}: {value} is outside {lo} to {hi}")
    return value


def _prompt(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaError(f"prompt: describe the {what} in words")
    if len(value) > PROMPT_MAX:
        raise MediaError(f"prompt: at most {PROMPT_MAX} characters")
    if "\x00" in value:
        raise MediaError("prompt: text only")
    return value.strip()


def _size(spec: Dict[str, int], width: Any, height: Any) -> Tuple[int, int, List[str]]:
    w = _whole("width", spec["width"] if width is None else width, spec["side_min"], spec["side_max"])
    h = _whole("height", spec["height"] if height is None else height, spec["side_min"], spec["side_max"])
    m = spec["multiple"]
    rw, rh = max(m, round(w / m) * m), max(m, round(h / m) * m)
    if rw * rh > spec["pixels_max"]:
        raise MediaError(f"size: {rw}x{rh} is more than {spec['pixels_max']} pixels in all; ask for a smaller picture")
    notes = [f"the size {w}x{h} was rounded to {rw}x{rh}, a multiple of {m}"] if (rw, rh) != (w, h) else []
    return rw, rh, notes


def _seed(seed: Any) -> int:
    if seed is None or seed == "":
        return secrets.randbelow(2 ** 31)
    return _whole("seed", seed, 0, 2 ** 31 - 1)


def image_request(prompt: Any, width: Any = None, height: Any = None, steps: Any = None, seed: Any = None, negative: Any = "") -> Dict[str, Any]:
    w, h, notes = _size(IMAGE, width, height)
    if negative is not None and not isinstance(negative, str):
        raise MediaError("negative: text, or empty")
    return {"prompt": _prompt(prompt, "picture"), "width": w, "height": h, "steps": _whole("steps", IMAGE["steps"] if steps is None else steps, 1, IMAGE["steps_max"]),
            "seed": _seed(seed), "negative": (negative or "").strip()[:PROMPT_MAX], "notes": notes}


def video_request(prompt: Any, width: Any = None, height: Any = None, frames: Any = None, fps: Any = None, seed: Any = None, negative: Any = "") -> Dict[str, Any]:
    w, h, notes = _size(VIDEO, width, height)
    n = _whole("frames", VIDEO["frames"] if frames is None else frames, VIDEO["frames_min"], VIDEO["frames_max"])
    rounded = (n - 1) // 8 * 8 + 1                    # the model takes a whole number of 8-frame steps plus the first frame
    if rounded != n:
        notes.append(f"{n} frames were rounded to {rounded}, the model takes 8 steps of frames plus one")
    f = _whole("fps", VIDEO["fps"] if fps is None else fps, VIDEO["fps_min"], VIDEO["fps_max"])
    if negative is not None and not isinstance(negative, str):
        raise MediaError("negative: text, or empty")
    return {"prompt": _prompt(prompt, "clip"), "width": w, "height": h, "frames": rounded, "fps": f, "seed": _seed(seed),
            "negative": (negative or "").strip()[:PROMPT_MAX], "notes": notes}
