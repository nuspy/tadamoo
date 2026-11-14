"""The plan of a `modify` job: a change made to the content of a video that exists, with the LTX 2.3 editing LoRAs of LTX Studio. Read by `worker.py` (standard library only; LTX Studio's `core` is
imported from its folder, the model process is LTX Studio's own `ltx2_infer.py` in its LTX 2.3 environment).

    replace   SAM2 follows what the word `object` names through the clip, the area is made again from the prompt (LoRA t2v) or from a picture of the new thing (LoRA r2v); the rest of every frame is the original
    edit      the whole video is changed by words (Edit-anything IC-LoRA), the source video is the guide
    restyle   the video takes the look of a picture (ReStyle IC-LoRA, the picture replaces the first frame of the guide)

The editing LoRAs exist for LTX 2.3 only, so this kind runs a 2.3 checkpoint of its own (the dev one with LTX Studio's distilled LoRA, or a distilled one), never the 2.5 checkpoint the clips are made with.
The job works on a copy of the video inside its own folder: the tracking files SAM2 leaves next to its input go there and are deleted with the job. The sound of the original is put back on the result."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable, Dict, List

RELEASE = "2.3"
EDIT_LORA = "LTX-2/ltx23_edit_anything_global_rank128_v1_9000steps_adamw.safetensors"
PREFER = ("ltx-2.3-22b-distilled", "ltx-2.3-22b-dev")          # the official checkpoints by the start of their name, in the order they are taken
TRACKING = ("sam2-hiera-large", "grounding-dino-base")           # the catalogue ids of what follows the object
Registrar = Callable[..., "subprocess.Popen"]


def _r64(x: float) -> int:
    return max(64, int(round(float(x) / 64.0)) * 64)


def choose_checkpoint(release: Any, name: str) -> str:
    """The LTX 2.3 checkpoint to run: the one the settings name, else the official distilled one, else the official dev one; "" when there is none. A fine-tune is never taken by itself."""
    found = [c for c in release.checkpoints(RELEASE) if not c.lower().endswith(".gguf")]
    if name:
        return next((c for c in found if os.path.basename(c) == name), "")
    for start in PREFER:
        hit = next((c for c in found if os.path.basename(c).lower().startswith(start)), "")
        if hit:
            return hit
    return ""


def plan(folder: str, spec: dict, use_ltx: Callable[[str], None]) -> dict:
    """What it takes to make the change: the model process, its parameters, what is missing on disk, and the steps `pre` (the tracking, under the lease) and `post` (the sound put back)."""
    use_ltx(folder)
    rel, vi, vic, vem, prep, paths = (importlib.import_module("core." + n) for n in ("ltx_release", "video_inpaint", "video_edit_ic", "video_edit_modes", "video_ic_prep", "model_paths"))
    req = spec.get("request") or {}
    checking = spec.get("output") == "check"
    mode = req.get("mode") or "replace"
    py, script = rel.runner(RELEASE)
    missing: List[str] = [f"component {c}" for c in rel.missing_components(RELEASE)]
    model = choose_checkpoint(rel, spec.get("edit_model") or "")
    if not model:
        missing.append(f"checkpoint {spec.get('edit_model') or 'ltx-2.3-22b-dev-fp8.safetensors (an LTX 2.3 dev or distilled one)'}")
    elif not rel.is_distilled(model) and not vic.distilled_lora_ok(RELEASE):
        missing.append("the distilled LoRA of LTX 2.3 (ltx23-distilled-lora-384)")
    reference = req.get("reference") or ""
    if mode == "replace":
        missing += [m for m in (vi.missing_weight({"ref_image": reference}),) if m]
        missing += [f"weights {i}" for i in TRACKING if not paths.exists(i)]
        if not Path(vi.INF_PY).is_file():
            missing.append(f"the inference environment {vi.INF_PY}")
    elif mode == "edit":
        if not Path(vi.lora_abspath(EDIT_LORA)).is_file():
            missing.append(f"the LoRA {EDIT_LORA}")
    else:
        missing += [m for m in (vic.missing_weight(vem.get_spec("restyle")),) if m]
    base = {"python": spec.get("python") or py, "script": script, "missing": missing, "env": {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}}
    if checking:
        return {**base, "params": {}}
    src = req.get("video") or ""
    if not Path(src).is_file():
        return {**base, "params": {}, "missing": missing + [f"the video {src}"]}
    if reference and not Path(reference).is_file():
        return {**base, "params": {}, "missing": missing + [f"the picture {reference}"]}
    if missing:
        return {**base, "params": {}}
    info = prep.probe(src, count_frames=True)
    fps = int(round(info.fps or 25))
    seconds = (info.nb_frames or 0) / float(info.fps or 25)
    if not info.width or not info.height or info.nb_frames < 9:
        return {**base, "params": {}, "missing": [f"a video of at least 9 frames ({src})"]}
    if seconds > float(req.get("seconds_max") or 20):
        return {**base, "params": {}, "missing": [f"a video of at most {req.get('seconds_max') or 20} seconds (this one is {seconds:.0f}): cut the part to change first"]}
    output = spec["output"]
    work = Path(output).parent
    local = work / ("source" + (Path(src).suffix or ".mp4"))
    shutil.copyfile(src, local)
    frames = max(9, ((int(info.nb_frames) - 1) // 8) * 8 + 1)
    seed, strength, prompt = int(req.get("seed") or 0), float(req.get("strength") or 1.0), req.get("prompt") or ""
    track = sound = None
    if mode == "replace":
        cfg = {"prompt": prompt, "ref_image": reference, "ref_strength": strength, "cfg_scale": 6.0, "guide": float(req.get("guide") or 0.4), "mask_dilate": int(req.get("grow") or 0),
               "mask_blur": 6.0, "seed": seed, "video": str(local), "full_res": True, "keep_background": True, "keep_audio": False, "width": _r64(info.width), "height": _r64(info.height),
               "fps": fps, "num_frames": frames, "model": model, "ltx_release": RELEASE}
        params = vi.inpaint_params(cfg, output)
        if reference:
            try:
                vi.fit_reference(params, str(work), seed)           # the new thing whole in the frame instead of cropped (Pillow and numpy; kept as it is without them)
            except ImportError:
                pass

        def track(log: Callable[[str], None], popen: Registrar) -> None:
            def run(cmd, **kw):
                proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
                out, err = proc.communicate()
                return types.SimpleNamespace(stdout=out, stderr=err, returncode=proc.returncode)
            log("[modify] SAM2 follows the object through the clip")
            vi.attach_tracking(params, str(local), req["object"], str(work / "guide.mp4"), int(req.get("grow") or 0), run=run, log=log)
    elif mode == "edit":
        edit = vem.get_spec("edit")
        w, h, _ = vem.clamp_resolution(info.width, info.height, edit)
        params = {"prompt": prompt, "negative": "worst quality, blurry", "edit_lora": EDIT_LORA, "control_video": str(local), "control_strength": strength, "width": w, "height": h,
                  "num_frames": frames, "fps": fps, "seed": seed, "output": output, "model": model, "ltx_release": RELEASE}
        if frames > edit.max_frames_per_segment:                    # a longer clip is cut in segments of the guide and joined with a cross-fade
            params.update(chunk_frames=edit.max_frames_per_segment, overlap_frames=vem.DEFAULT_OVERLAP)
    else:
        cfg = {"mode": "restyle", "video": str(local), "prompt": prompt, "style_image": reference, "strength": strength, "seed": seed, "keep_audio": bool(req.get("keep_audio", True)),
               "res_mode": "source", "model": model, "ltx_release": RELEASE}
        try:
            job = vic.build_job(cfg, str(work), seed, output)
        except vic.IcJobError as exc:
            return {**base, "params": {}, "missing": [str(exc)]}
        params = job.params

        def sound() -> None:
            done = vic.finalize(job, output, cleanup=False)
            if done != output and Path(done).is_file():
                os.replace(done, output)
    if mode != "restyle" and req.get("keep_audio", True) and info.has_audio:
        def sound() -> None:
            done = vic.mux_audio(output, str(local))                 # the sound of the original under the result
            if done != output and Path(done).is_file():
                os.replace(done, output)
    return {**base, "params": params, "pre": track, "post": sound}


def popen_for(register: Callable[["subprocess.Popen"], None]) -> Registrar:
    """A Popen that tells `register` about the process it starts, so that the worker's stop can end it."""
    def popen(cmd, **kw):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(cmd, creationflags=flags, **kw)
        register(proc)
        return proc
    return popen
