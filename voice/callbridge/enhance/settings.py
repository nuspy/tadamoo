"""What the person chose for the avatar's acceleration and the one rule that turns it into the worker's size and rate.

`voice.json` `avatar.enhance`:
    {"upscale": "none|basic|rtx_video", "scale": 1.5|2|3|4, "interpolate": "none|basic", "factor": 2|3,
     "worker_fps_cap": 0|5..60, "worker_height_cap": 0|120..2160, "rtx_video_path": "<absolute path of the SDK's DLL>"}
A value that is missing or invalid takes its default (everything off), so a damaged file never turns a stage on.

THE RULE. The outputs (preview, virtual camera, other subscribers) always get the size and rate they were configured for: the target.
With a stage on, the worker renders less and the enhancer restores the target:
  * height: the worker renders `ceil(target_height / scale)`, or `worker_height_cap` when that is set (never above the target); every frame
    the enhancer sends out has exactly the target size, whatever the provider's own factor.
  * rate: the worker renders `ceil(target_fps / factor)`, or `worker_fps_cap` when that is set (never above the target); the enhancer sends
    `ceil(target_fps / worker_fps)` frames out for each frame in.
A cap of a stage that is off is ignored: with no upscaling the worker renders at the target height, with no interpolation at the target rate."""
from __future__ import annotations

import math
from dataclasses import dataclass

UPSCALE = ("none", "basic", "rtx_video")
INTERPOLATE = ("none", "basic")
SCALES = (1.5, 2.0, 3.0, 4.0)
FACTORS = (2, 3)
# The scales each upscaling provider takes. rtx_video lists the whole numbers only: the SDK's own list is UNVERIFIED and comes with its binding.
PROVIDER_SCALES = {"none": SCALES, "basic": SCALES, "rtx_video": (2.0, 3.0, 4.0)}
FPS_CAP = (5, 60)
HEIGHT_CAP = (120, 2160)
MAX_OUT_PER_IN = 4
MIN_WORKER_FPS = 5


@dataclass(frozen=True)
class EnhanceSettings:
    upscale: str = "none"
    scale: float = 2.0
    interpolate: str = "none"
    factor: int = 2
    worker_fps_cap: int = 0
    worker_height_cap: int = 0
    rtx_video_path: str = ""

    @property
    def active(self) -> bool:
        return self.upscale != "none" or self.interpolate != "none"

    def as_dict(self) -> dict:
        return {"upscale": self.upscale, "scale": self.scale, "interpolate": self.interpolate, "factor": self.factor,
                "worker_fps_cap": self.worker_fps_cap, "worker_height_cap": self.worker_height_cap, "rtx_video_path": self.rtx_video_path}


def _int_in(value, lo: int, hi: int) -> int:
    """0 (automatic) or a whole number between `lo` and `hi`; anything else is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if lo <= value <= hi else 0


def parse(stored) -> EnhanceSettings:
    """The settings in a stored `enhance` object, each invalid value replaced by its default; a scale the provider does not take turns that stage off."""
    stored = stored if isinstance(stored, dict) else {}
    upscale = stored.get("upscale") if stored.get("upscale") in UPSCALE else "none"
    scale = stored.get("scale")
    scale = float(scale) if isinstance(scale, (int, float)) and not isinstance(scale, bool) and float(scale) in SCALES else 2.0
    if scale not in PROVIDER_SCALES[upscale]:
        upscale = "none"
    interpolate = stored.get("interpolate") if stored.get("interpolate") in INTERPOLATE else "none"
    factor = stored.get("factor") if stored.get("factor") in FACTORS and not isinstance(stored.get("factor"), bool) else 2
    path = stored.get("rtx_video_path")
    path = path if isinstance(path, str) and path.lower().endswith(".dll") and "\0" not in path and len(path) <= 500 else ""
    return EnhanceSettings(upscale, scale, interpolate, factor, _int_in(stored.get("worker_fps_cap"), *FPS_CAP),
                           _int_in(stored.get("worker_height_cap"), *HEIGHT_CAP), path)


def _even(n: int) -> int:
    return n + (n % 2)


@dataclass(frozen=True)
class Plan:
    """What the worker is asked to render and what the enhancer sends out."""
    active: bool
    worker_height: int        # 0 = the worker keeps its own height
    worker_fps: int
    out_height: int
    out_fps: int
    out_per_in: int           # frames out for each frame in (1 without interpolation)

    def as_dict(self) -> dict:
        return {"active": self.active, "worker_height": self.worker_height, "worker_fps": self.worker_fps, "out_height": self.out_height,
                "out_fps": self.out_fps, "out_per_in": self.out_per_in}


def plan(s: EnhanceSettings, target_fps: int, target_height: int) -> Plan:
    """The one rule (see the module text): the target is what the outputs get, the worker renders less when a stage is on."""
    fps = max(MIN_WORKER_FPS, int(target_fps or 25))
    height = _even(max(2, int(target_height or 480)))
    worker_height, worker_fps = height, fps
    if s.upscale != "none":
        worker_height = min(s.worker_height_cap, height) if s.worker_height_cap else _even(math.ceil(height / s.scale))
        worker_height = _even(max(2, worker_height))
    if s.interpolate != "none":
        worker_fps = min(s.worker_fps_cap, fps) if s.worker_fps_cap else math.ceil(fps / s.factor)
        worker_fps = max(math.ceil(fps / MAX_OUT_PER_IN), MIN_WORKER_FPS, worker_fps)
        worker_fps = min(worker_fps, fps)
    out_per_in = math.ceil(fps / worker_fps) if s.interpolate != "none" else 1
    return Plan(s.active, worker_height if s.upscale != "none" else 0, worker_fps if s.interpolate != "none" else 0, height, fps, out_per_in)
