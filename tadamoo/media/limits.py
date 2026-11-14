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


REFS_MAX = 4


def _refs(refs: Any) -> List[str]:
    """Pictures the model takes as references (an edit of what they show, named image 1, image 2 in the prompt): paths on this PC."""
    if refs in (None, ""):
        return []
    if not isinstance(refs, (list, tuple)) or len(refs) > REFS_MAX or any(not isinstance(r, str) or not r.strip() or "\x00" in r for r in refs):
        raise MediaError(f"refs: a list of up to {REFS_MAX} paths of pictures on this PC")
    return [r.strip() for r in refs]


def image_request(prompt: Any, width: Any = None, height: Any = None, steps: Any = None, seed: Any = None, negative: Any = "", refs: Any = None) -> Dict[str, Any]:
    w, h, notes = _size(IMAGE, width, height)
    if negative is not None and not isinstance(negative, str):
        raise MediaError("negative: text, or empty")
    return {"refs": _refs(refs), "prompt": _prompt(prompt, "picture"), "width": w, "height": h, "steps": _whole("steps", IMAGE["steps"] if steps is None else steps, 1, IMAGE["steps_max"]),
            "seed": _seed(seed), "negative": (negative or "").strip()[:PROMPT_MAX], "notes": notes}


def _path(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MediaError(f"{name}: the path of a file on this PC")
    return value.strip()


def video_request(prompt: Any, width: Any = None, height: Any = None, frames: Any = None, fps: Any = None, seed: Any = None, negative: Any = "",
                  image: Any = None) -> Dict[str, Any]:
    """A clip from words, or from a picture (`image`, the path of its first picture) and words."""
    w, h, notes = _size(VIDEO, width, height)
    n = _whole("frames", VIDEO["frames"] if frames is None else frames, VIDEO["frames_min"], VIDEO["frames_max"])
    rounded = (n - 1) // 8 * 8 + 1                    # the model takes a whole number of 8-frame steps plus the first frame
    if rounded != n:
        notes.append(f"{n} frames were rounded to {rounded}, the model takes 8 steps of frames plus one")
    f = _whole("fps", VIDEO["fps"] if fps is None else fps, VIDEO["fps_min"], VIDEO["fps_max"])
    if negative is not None and not isinstance(negative, str):
        raise MediaError("negative: text, or empty")
    return {"prompt": _prompt(prompt, "clip"), "width": w, "height": h, "frames": rounded, "fps": f, "seed": _seed(seed),
            "negative": (negative or "").strip()[:PROMPT_MAX], "image": "" if image in (None, "") else _path("image", image), "notes": notes}


ENHANCE = {"scale_min": 1.0, "scale_max": 4.0, "factor_min": 2, "factor_max": 4, "modes": ("turbo", "base")}


def enhance_request(video: Any, scale: Any = None, factor: Any = None, mode: Any = "turbo") -> Dict[str, Any]:
    """A better video from an existing one, with the size, the frames and the duration it had: `scale` is the size FlashVSR+ works at, 1 to 4 times (2: it works at twice the size and the result is brought back
    to the video's own size; 1: it restores at the video's size), `factor` times the frames a second (2 to 4, frames made between the real ones). Neither given: the one enhancer of the product,
    FlashVSR+ in its Turbo mode working at 2x."""
    if scale in (None, "") and factor in (None, ""):
        scale = 2.0
    if mode not in ENHANCE["modes"]:
        raise MediaError("mode: " + " or ".join(ENHANCE["modes"]))
    out: Dict[str, Any] = {"video": _path("video", video), "scale": 0.0, "factor": 0, "mode": mode, "notes": []}
    if scale not in (None, ""):
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not ENHANCE["scale_min"] <= scale <= ENHANCE["scale_max"]:
            raise MediaError(f"scale: a number from {ENHANCE['scale_min']:g} to {ENHANCE['scale_max']:g}")
        out["scale"] = float(scale)
    if factor not in (None, ""):
        out["factor"] = _whole("factor", factor, ENHANCE["factor_min"], ENHANCE["factor_max"])
    return out


AVATAR = {"resolutions": ("480p", "720p"), "ref_img_index": (0, 60), "mask_frame_range": (0, 12)}


def avatar_request(image: Any, audio: Any, prompt: Any = "", resolution: Any = "480p", seed: Any = None, negative: Any = "", ref_img_index: Any = None,
                   mask_frame_range: Any = None) -> Dict[str, Any]:
    """A talking head: a portrait and a voice recording, made into a video as long as the recording. Both are paths on this PC, read by the model process.
    `ref_img_index` and `mask_frame_range` are the two knobs of how the first picture anchors the later segments of a long take; left out, the model's own values are used."""
    for name, value in (("image", image), ("audio", audio)):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise MediaError(f"{name}: the path of a file on this PC")
    if resolution not in AVATAR["resolutions"]:
        raise MediaError("resolution: " + " or ".join(AVATAR["resolutions"]))
    if not isinstance(prompt, str) or len(prompt) > PROMPT_MAX:
        raise MediaError(f"prompt: text of at most {PROMPT_MAX} characters, or empty")
    if not isinstance(negative, str) or len(negative) > PROMPT_MAX:
        raise MediaError(f"negative: text of at most {PROMPT_MAX} characters, or empty")
    out = {"image": image.strip(), "audio": audio.strip(), "prompt": prompt.strip(), "resolution": resolution, "seed": _seed(seed), "negative": negative.strip(), "notes": []}
    for name, value in (("ref_img_index", ref_img_index), ("mask_frame_range", mask_frame_range)):
        if value is not None:
            out[name] = _whole(name, value, *AVATAR[name])
    return out


MODIFY = {"modes": ("replace", "edit", "restyle"), "strength": (0.1, 1.0), "guide": (0.1, 1.0), "grow": (0, 64), "seconds_max": 20, "object_max": 200,
          "defaults": {"replace": 0.8, "edit": 1.0, "restyle": 1.0}, "guide_default": 0.4, "grow_default": 24}


def _unit(name: str, value: Any, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise MediaError(f"{name}: a number from {lo:g} to {hi:g}")
    return float(value)


def modify_request(video: Any, prompt: Any = "", mode: Any = "replace", obj: Any = "", reference: Any = "", strength: Any = None, guide: Any = None, grow: Any = None,
                   seed: Any = None) -> Dict[str, Any]:
    """A change made to the content of a video that exists (not an edit of its cut): `replace` swaps what a word names (`obj`) for what the prompt says or for the picture `reference`,
    `edit` changes the whole video by words, `restyle` gives it the look of the picture `reference`. A request names the video; the clip's length is checked by the worker, which reads it."""
    if mode not in MODIFY["modes"]:
        raise MediaError("mode: " + ", ".join(MODIFY["modes"]))
    out: Dict[str, Any] = {"mode": mode, "video": _path("video", video), "object": "", "reference": "", "notes": []}
    if mode == "restyle":
        if not isinstance(prompt, str) or len(prompt) > PROMPT_MAX or "\x00" in prompt:
            raise MediaError(f"prompt: text of at most {PROMPT_MAX} characters, or empty")
        out["prompt"] = prompt.strip()
    else:
        out["prompt"] = _prompt(prompt, "change")
    if mode == "replace":
        if not isinstance(obj, str) or not obj.strip() or len(obj) > MODIFY["object_max"] or "\x00" in obj:
            raise MediaError(f"object: what to replace, in a few English words (at most {MODIFY['object_max']} characters)")
        out["object"] = obj.strip()
    elif obj not in (None, ""):
        raise MediaError("object: only for the mode replace")
    if reference not in (None, ""):
        if mode == "edit":
            raise MediaError("reference: only for the modes replace and restyle")
        out["reference"] = _path("reference", reference)
    elif mode == "restyle":
        raise MediaError("reference: the picture whose look the video takes")
    out["strength"] = _unit("strength", MODIFY["defaults"][mode] if strength is None else strength, *MODIFY["strength"])
    if guide is not None and mode != "replace":
        raise MediaError("guide: only for the mode replace")
    out["guide"] = _unit("guide", MODIFY["guide_default"] if guide is None else guide, *MODIFY["guide"]) if mode == "replace" else 0.0
    if grow is not None and mode != "replace":
        raise MediaError("grow: only for the mode replace")
    out["grow"] = _whole("grow", MODIFY["grow_default"] if grow is None else grow, *MODIFY["grow"]) if mode == "replace" else 0
    out["seed"] = _seed(seed)
    out["keep_audio"], out["seconds_max"] = True, MODIFY["seconds_max"]
    return out
