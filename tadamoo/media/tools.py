"""`generate_image` and `generate_video`: pictures and clips made on request, for the Chat and the Agent.

A call runs one job (tadamoo/media/client.py) and returns when the file is made, which for a clip takes minutes: the job runs alone on the GPU, the
language model is slow meanwhile, and the Stop button ends it. The file is written into the conversation's working folder, under `media/` unless a path is given,
and never over a file that exists. The prompt is data for the model that makes the picture, nothing in it is run. When the server cannot lend the VRAM the
answer is `refused` with the broker's reason, so the caller can tell the person or choose another way; nothing is retried here."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from tadamoo.media import client, limits
from tadamoo.media.errors import Cancelled, MediaError, Refused
from tadamoo.media.settings import MediaSettings

KINDS = {"image": (".png", limits.image_request), "video": (".mp4", limits.video_request)}
FOLDER = "media"
WHY = {"not_enough": "the GPU does not have enough free VRAM even with the model's cache lent in full",
       "busy": "another job holds the VRAM (waited as long as the settings allow)",
       "unavailable": "the language model is not running with elastic VRAM, so the server cannot lend any",
       "no_engine": "the server does not answer or holds no model to borrow from",
       "disabled": "the VRAM broker is turned off", "engine_died": "the language model stopped while the VRAM was being lent"}


def usable(settings: Dict[str, Any], kind: str, command: Any = None) -> str:
    """"" when `kind` can be made now, else why not, in a sentence."""
    if not settings["enabled"]:
        return "picture and video making is turned off in the media settings"
    got = client.check(settings, command)
    state = got.get(kind) or {}
    if state.get("ready"):
        return ""
    if state.get("error"):
        return state["error"]
    if state.get("missing"):
        return "LTX Studio lacks: " + ", ".join(state["missing"])
    return "the environment of LTX Studio for this is not found"


def register_media_tools(registry: Any, sandbox: Any, settings: MediaSettings, work_dir: Path, url: str,
                         cancelled: Optional[Callable[[], bool]] = None, command: Any = None, kinds: Sequence[str] = ("image", "video")) -> None:
    """Offers `generate_image` and `generate_video` (those of `kinds`). `work_dir` holds each job's working files (kept only when a job fails); `command` replaces the worker (tests)."""
    from tadamoo.tools.registry import EffectClass

    def target(kind: str, path: str, seed: int) -> Path:
        ext = KINDS[kind][0]
        rel = path.strip() if isinstance(path, str) and path.strip() else f"{FOLDER}/{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{seed}{ext}"
        safe = sandbox.resolve_safe(rel)
        if safe.suffix.lower() != ext:
            raise MediaError(f"path: a {kind} is a {ext} file, '{rel}' must end in {ext}")
        if safe.exists():
            raise MediaError(f"'{rel}' already exists: choose another path, or leave it out")
        return safe

    def make(kind: str, request: Dict[str, Any], path: str) -> Dict[str, Any]:
        config = settings.read()
        why = usable(config, kind, command)
        if why:
            return {"status": "unavailable", "note": why}
        safe = target(kind, path, request["seed"])
        rel = safe.relative_to(sandbox.root).as_posix()
        blocked = sandbox._owner_block(rel, safe, append=False)
        if blocked:
            return blocked
        sandbox._guard_git(f"generate_{kind}", rel, safe)
        job = Path(work_dir) / f"{kind}-{int(time.time())}-{request['seed']}"
        made = job / safe.name
        try:
            got = client.run(kind, {k: v for k, v in request.items() if k != "notes"}, made, config, url=url, cancel=cancelled, command=command)
        except Refused as exc:
            return {"status": "refused", "reason": exc.reason, "note": f"{exc} ({WHY.get(exc.reason, exc.reason)}). Tell the person; nothing was made. Do not retry in a loop."}
        except Cancelled:
            return {"status": "cancelled", "note": "the job was stopped; nothing was kept"}
        except MediaError as exc:
            return {"status": "error", "note": f"{exc}. The working files are in {job}"}
        safe.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(got["path"], str(safe))
        shutil.rmtree(job, ignore_errors=True)
        result = sandbox._record(rel, "added", "", "", keep_text=False)
        show = (f"![caption](tadamoo-path:{rel})" if kind == "image" else f"[{safe.name}](tadamoo-path:{rel})")
        return {"status": "ok", "path": rel, "bytes": safe.stat().st_size, "seconds": got["seconds"], "lease": got["lease"], "seed": request["seed"],
                "width": request["width"], "height": request["height"], **({"frames": request["frames"], "fps": request["fps"]} if kind == "video" else {"steps": request["steps"]}),
                "show": "show it in the answer with " + show, **({"adjusted": request["notes"]} if request["notes"] else {}), "changes": result["kind"]}

    def generate_image(prompt: str, width: Optional[int] = None, height: Optional[int] = None, steps: Optional[int] = None, seed: Optional[int] = None,
                       negative: str = "", path: str = "") -> Dict[str, Any]:
        try:
            return make("image", limits.image_request(prompt, width, height, steps, seed, negative), path)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}

    def generate_video(prompt: str, width: Optional[int] = None, height: Optional[int] = None, frames: Optional[int] = None, fps: Optional[int] = None,
                       seed: Optional[int] = None, negative: str = "", path: str = "") -> Dict[str, Any]:
        try:
            return make("video", limits.video_request(prompt, width, height, frames, fps, seed, negative), path)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}

    i, v = limits.IMAGE, limits.VIDEO
    common = {"prompt": {"type": "string", "description": "What to show, in words (English works best); the picture maker reads it as a description, never as an order"},
              "negative": {"type": "string", "description": "What to keep out of it (optional)"},
              "seed": {"type": "integer", "description": "The same seed with the same prompt and size gives the same result; left out, a new one is chosen and returned"},
              "path": {"type": "string", "description": f"Where to save it, relative to the working folder; left out: {FOLDER}/ with a name made of the date and the seed. An existing file is never replaced"}}
    if "image" in kinds:
        registry.register(
            name="generate_image",
            description=("Makes a picture from a description with the local picture model and saves it as a PNG in the working folder. Use it when the person asks for a picture, "
                         "illustration, photo or drawing of something ('fammi un'immagine di...', 'genera una foto'). It uses the GPU alone for about half a minute "
                         "after the model is loaded; the language model may be slow meanwhile. Describe the subject, the setting, the light and the style in a few sentences. "
                         f"Sizes are rounded to a multiple of {i['multiple']}; the default is {i['width']}x{i['height']}. If the answer says `refused`, the PC had no VRAM to give: tell the person."),
            parameters={"type": "object", "properties": {**common,
                                                         "width": {"type": "integer", "description": f"Pixels, {i['side_min']} to {i['side_max']} (default {i['width']})"},
                                                         "height": {"type": "integer", "description": f"Pixels, {i['side_min']} to {i['side_max']} (default {i['height']})"},
                                                         "steps": {"type": "integer", "description": f"Denoising steps, 1 to {i['steps_max']} (default {i['steps']}); more is slower"}},
                        "required": ["prompt"]},
            effect=EffectClass.REVERSIBLE, handler=generate_image, timeout=None)
    if "video" in kinds:
        registry.register(
            name="generate_video",
            description=("Makes a short video clip (no sound) from a description with the local video model and saves it as an MP4 in the working folder. Use it when the person asks "
                         "for a video or an animation of something ('fammi un video di...'). It takes minutes and uses most of the GPU alone: the language model is very slow meanwhile, "
                         "so ask for it once, with a prompt that says what happens (subject, action, camera, light). "
                         f"A clip is {v['width']}x{v['height']}, {v['frames']} frames at {v['fps']} fps (about 2.6 seconds) unless asked otherwise, at most {v['frames_max']} frames. "
                         "If the answer says `refused`, the PC had no VRAM to give: tell the person."),
            parameters={"type": "object", "properties": {**common,
                                                         "width": {"type": "integer", "description": f"Pixels, {v['side_min']} to {v['side_max']}, rounded to a multiple of {v['multiple']} (default {v['width']})"},
                                                         "height": {"type": "integer", "description": f"Pixels, {v['side_min']} to {v['side_max']}, rounded to a multiple of {v['multiple']} (default {v['height']})"},
                                                         "frames": {"type": "integer", "description": f"Number of frames, {v['frames_min']} to {v['frames_max']}, rounded to 8 times a whole number plus 1 (default {v['frames']})"},
                                                         "fps": {"type": "integer", "description": f"Frames per second, {v['fps_min']} to {v['fps_max']} (default {v['fps']})"}},
                        "required": ["prompt"]},
            effect=EffectClass.REVERSIBLE, handler=generate_video, timeout=None)
