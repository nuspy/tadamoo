"""`generate_image`, `generate_video`, `enhance_video` and `modify_video`: pictures, clips, better versions of clips and changes to what a clip shows, made on request, for the Chat and the Agent.

A call runs one job (tadamoo/media/client.py) and returns when the file is made, which for a clip takes minutes: the job runs alone on the GPU, the
language model is slow meanwhile, and the Stop button ends it. The file is written into the conversation's working folder, under `media/` unless a path is given,
and never over a file that exists. The prompt is data for the model that makes the picture, nothing in it is run. When the server cannot lend the VRAM the
answer is `refused` with the broker's reason, so the caller can tell the person or choose another way; nothing is retried here."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.media import client, limits
from tadamoo.media.errors import Cancelled, MediaError, Refused
from tadamoo.media.settings import MediaSettings

KINDS = {"image": (".png", limits.image_request), "video": (".mp4", limits.video_request), "enhance": (".mp4", limits.enhance_request), "modify": (".mp4", limits.modify_request)}
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
    """Offers `generate_image`, `generate_video`, `enhance_video` and `modify_video` (those of `kinds`). `work_dir` holds each job's working files (kept only when a job fails); `command` replaces the worker (tests)."""
    from tadamoo.tools.registry import EffectClass

    def target_path(kind: str, path: str, tag: str, tool: str) -> Path:
        ext = KINDS[kind][0]
        rel = path.strip() if isinstance(path, str) and path.strip() else f"{FOLDER}/{tag}{ext}"
        safe = sandbox.resolve_safe(rel, tool)
        if safe.suffix.lower() != ext:
            raise MediaError(f"path: the result is a {ext} file, '{rel}' must end in {ext}")
        if safe.exists():
            raise MediaError(f"'{rel}' already exists: choose another path, or leave it out")
        return safe

    def source(path: str, tool: str, what: str) -> str:
        """A file of the working folder that a job reads: pictures to start from, a video to improve."""
        got = sandbox.resolve_safe(path, tool)
        if not got.is_file():
            raise MediaError(f"{what}: '{path}' is not a file in the working folder")
        return str(got)

    def job_failed(exc: Exception, job: Path) -> Dict[str, Any]:
        if isinstance(exc, Refused):
            return {"status": "refused", "reason": exc.reason, "note": f"{exc} ({WHY.get(exc.reason, exc.reason)}). Tell the person; nothing was made. Do not retry in a loop."}
        if isinstance(exc, Cancelled):
            return {"status": "cancelled", "note": "the job was stopped; nothing was kept"}
        return {"status": "error", "note": f"{exc}. The working files are in {job}"}

    def make(kind: str, request: Dict[str, Any], path: str) -> Dict[str, Any]:
        config = settings.read()
        why = usable(config, kind, command)
        if why:
            return {"status": "unavailable", "note": why}
        safe = target_path(kind, path, f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{request['seed']}", f"generate_{kind}")
        rel = sandbox._shown(safe)
        blocked = sandbox._owner_block(rel, safe, append=False)
        if blocked:
            return blocked
        sandbox._guard_git(f"generate_{kind}", rel, safe)
        job = Path(work_dir) / f"{kind}-{int(time.time())}-{request['seed']}"
        made = job / safe.name
        try:
            got = client.run(kind, {k: v for k, v in request.items() if k != "notes"}, made, config, url=url, cancel=cancelled, command=command)
        except (Refused, Cancelled, MediaError) as exc:
            return job_failed(exc, job)
        safe.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(got["path"], str(safe))
        shutil.rmtree(job, ignore_errors=True)
        result = sandbox._record(rel, "added", "", "", keep_text=False)
        show = (f"![caption](tadamoo-path:{rel})" if kind == "image" else f"[{safe.name}](tadamoo-path:{rel})")
        return {"status": "ok", "path": rel, "bytes": safe.stat().st_size, "seconds": got["seconds"], "lease": got["lease"], "seed": request["seed"],
                "width": request["width"], "height": request["height"], **({"frames": request["frames"], "fps": request["fps"]} if kind == "video" else {"steps": request["steps"]}),
                "show": "show it in the answer with " + show, **({"adjusted": request["notes"]} if request["notes"] else {}), "changes": result["kind"]}

    def generate_image(prompt: str, width: Optional[int] = None, height: Optional[int] = None, steps: Optional[int] = None, seed: Optional[int] = None,
                       negative: str = "", path: str = "", refs: Optional[List[str]] = None) -> Dict[str, Any]:
        try:
            paths = [source(r, "generate_image", "refs") for r in refs] if isinstance(refs, list) else refs
            return make("image", limits.image_request(prompt, width, height, steps, seed, negative, paths), path)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}

    def generate_video(prompt: str, width: Optional[int] = None, height: Optional[int] = None, frames: Optional[int] = None, fps: Optional[int] = None,
                       seed: Optional[int] = None, negative: str = "", path: str = "", image: str = "") -> Dict[str, Any]:
        try:
            first = source(image, "generate_video", "image") if image else None
            return make("video", limits.video_request(prompt, width, height, frames, fps, seed, negative, first), path)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}

    def enhance_video(path: str, scale: Optional[float] = None, factor: Optional[int] = None, output: str = "", mode: str = "turbo") -> Dict[str, Any]:
        """The video is enlarged first (so the frames made in between are made at the final size) and the frames are added after."""
        try:
            request = limits.enhance_request(path, scale, factor, mode)
            request["video"] = source(path, "enhance_video", "path")
            steps = [k for k, wanted in (("upscale", request["scale"]), ("interpolate", request["factor"])) if wanted]
            config = settings.read()
            for step in steps:
                why = usable(config, step, command)
                if why:
                    return {"status": "unavailable", "note": why}
            safe = target_path("enhance", output, f"{Path(path).stem}-enhanced-{time.strftime('%Y%m%d-%H%M%S')}", "enhance_video")
            rel = sandbox._shown(safe)
            blocked = sandbox._owner_block(rel, safe, append=False)
            if blocked:
                return blocked
            sandbox._guard_git("enhance_video", rel, safe)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}
        job = Path(work_dir) / f"enhance-{int(time.time())}"
        try:
            got = client.enhance(request, job, config, url=url, cancel=cancelled, command=command)
        except (Refused, Cancelled, MediaError) as exc:
            return job_failed(exc, job)
        current, seconds = got["path"], got["seconds"]
        safe.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(current, str(safe))
        shutil.rmtree(job, ignore_errors=True)
        result = sandbox._record(rel, "added", "", "", keep_text=False)
        return {"status": "ok", "path": rel, "bytes": safe.stat().st_size, "seconds": seconds, "steps": got["steps"], "scale": request["scale"] or None, "factor": request["factor"] or None,
                "show": f"show it in the answer with [{safe.name}](tadamoo-path:{rel})", "changes": result["kind"]}

    def modify_video(path: str, prompt: str = "", mode: str = "replace", target: str = "", reference: str = "", strength: Optional[float] = None, guide: Optional[float] = None,
                     grow: Optional[int] = None, seed: Optional[int] = None, output: str = "") -> Dict[str, Any]:
        """A copy of the video with its content changed; the original is kept."""
        try:
            video = source(path, "modify_video", "path")
            ref = source(reference, "modify_video", "reference") if reference else ""
            request = limits.modify_request(video, prompt, mode, target, ref, strength, guide, grow, seed)
            config = settings.read()
            why = usable(config, "modify", command)
            if why:
                return {"status": "unavailable", "note": why}
            safe = target_path("modify", output, f"{Path(path).stem}-{request['mode']}-{time.strftime('%Y%m%d-%H%M%S')}-{request['seed']}", "modify_video")
            rel = sandbox._shown(safe)
            blocked = sandbox._owner_block(rel, safe, append=False)
            if blocked:
                return blocked
            sandbox._guard_git("modify_video", rel, safe)
        except MediaError as exc:
            return {"status": "error", "note": str(exc)}
        job = Path(work_dir) / f"modify-{int(time.time())}-{request['seed']}"
        try:
            got = client.run("modify", {k: v for k, v in request.items() if k != "notes"}, job / safe.name, config, url=url, cancel=cancelled, command=command)
        except (Refused, Cancelled, MediaError) as exc:
            return job_failed(exc, job)
        safe.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(got["path"], str(safe))
        shutil.rmtree(job, ignore_errors=True)
        result = sandbox._record(rel, "added", "", "", keep_text=False)
        return {"status": "ok", "path": rel, "bytes": safe.stat().st_size, "seconds": got["seconds"], "lease": got["lease"], "mode": request["mode"], "seed": request["seed"],
                "show": f"show it in the answer with [{safe.name}](tadamoo-path:{rel})", "changes": result["kind"]}

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
                                                         "steps": {"type": "integer", "description": f"Denoising steps, 1 to {i['steps_max']} (default {i['steps']}); more is slower"},
                                                         "refs": {"type": "array", "items": {"type": "string"},
                                                                  "description": "Up to 4 pictures of the working folder to edit or to take the person, the place or the style from; the prompt calls them image 1, image 2 and says what changes"}},
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
                                                         "fps": {"type": "integer", "description": f"Frames per second, {v['fps_min']} to {v['fps_max']} (default {v['fps']})"},
                                                         "image": {"type": "string", "description": "A picture of the working folder that the clip starts from; left out, the clip is made from the words alone"}},
                        "required": ["prompt"]},
            effect=EffectClass.REVERSIBLE, handler=generate_video, timeout=None)
    if "enhance" in kinds:
        e = limits.ENHANCE
        registry.register(
            name="enhance_video",
            description=("Makes a better copy of a video of the working folder: larger (more pixels, with the detail an AI enlarger invents) and/or smoother (more frames a second, made between the real "
                         "ones, the length does not change). Use it when a clip looks small, soft or jerky. It takes minutes per clip and uses most of the GPU alone. The original is kept. "
                         "If the answer says `refused`, the PC had no VRAM to give: tell the person."),
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "The video to improve, relative to the working folder"},
                "scale": {"type": "number", "description": f"Times the width and height, {e['scale_min']:g} to {e['scale_max']:g} (2 doubles the size); left out: the size is kept"},
                "factor": {"type": "integer", "description": f"Times the frames a second, {e['factor_min']} to {e['factor_max']} (2 turns 25 into 50); left out: the frame rate is kept"},
                "mode": {"type": "string", "enum": list(e["modes"]), "description": "turbo (faster) or base (finer)"},
                "output": {"type": "string", "description": f"Where to save it, relative to the working folder; left out: {FOLDER}/ with the name of the video and the date"}},
                "required": ["path"]},
            effect=EffectClass.REVERSIBLE, handler=enhance_video, timeout=None)
    if "modify" in kinds:
        m = limits.MODIFY
        registry.register(
            name="modify_video",
            description=("Changes what a video of the working folder shows, with AI, and saves a copy (the original is kept; the sound is put back). `mode` replace: what `target` names (a few English words, "
                         "for example 'yellow Fiat 500') is followed through the clip and made again from the `prompt` or from the picture `reference` of the new thing (a red Ferrari). `edit`: the whole video "
                         "is changed by the `prompt`. `restyle`: the video takes the look of the picture `reference`. Use it for 'cambia la macchina gialla con una Ferrari rossa', not for cutting or joining "
                         f"clips. It is not exact: the new thing is invented frame by frame, a large or fast-moving subject can drift, and a clip is at most {m['seconds_max']} seconds. It takes many minutes and "
                         "uses most of the GPU alone; ask for it once, with a prompt that describes the result (what the new thing looks like and what it does). If the answer says `refused`, tell the person."),
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "The video to change, relative to the working folder"},
                "mode": {"type": "string", "enum": list(m["modes"]), "description": "replace (swap an object), edit (change the whole video by words) or restyle (the look of a picture); default replace"},
                "prompt": {"type": "string", "description": "What the result shows, in English: for replace the new thing and what it does, for edit what to change; optional for restyle"},
                "target": {"type": "string", "description": "replace only: what to swap, in a few English words ('yellow Fiat 500', 'person in the red coat')"},
                "reference": {"type": "string", "description": "A picture of the working folder: the new thing (replace), or the look to take (restyle)"},
                "strength": {"type": "number", "description": f"{m['strength'][0]:g} to {m['strength'][1]:g}: how closely the result follows the reference picture (replace) or the change asked (edit, restyle)"},
                "guide": {"type": "number", "description": f"replace only, {m['guide'][0]:g} to {m['guide'][1]:g} (default {m['guide_default']}): how closely the result keeps the motion and the shape of the original"},
                "grow": {"type": "integer", "description": f"replace only, {m['grow'][0]} to {m['grow'][1]} pixels (default {m['grow_default']}): how far the changed area goes beyond the object's outline"},
                "seed": {"type": "integer", "description": "The same seed with the same request gives the same result; left out, a new one is chosen and returned"},
                "output": {"type": "string", "description": f"Where to save it, relative to the working folder; left out: {FOLDER}/ with the name of the video, the mode and the date"}},
                "required": ["path"]},
            effect=EffectClass.REVERSIBLE, handler=modify_video, timeout=None)
