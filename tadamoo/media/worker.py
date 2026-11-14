"""Runs one picture or video job of LTX Studio under a VRAM lease and prints what happens as JSON lines on stdout. Run by any Python, never imported.

    python worker.py --job JOB.json          the job: {kind, ltx_dir, url, mib, wait_s, python?, video_model?, output, request}
    python worker.py --check --ltx-dir DIR   {"image": {...}, "video": {...}, ...}: is each kind usable (code, environment, weights)

Lines:  {"event": "start", "kind"}  {"event": "lease", "ok", "granted_mib", "reason", "message", "message_it"}  {"event": "refused", "reason", "message",
        "message_it"}  {"event": "progress", "step", "steps"}  {"event": "log", "line"}  {"event": "done", "path", "bytes", "seconds", "lease"}  or
        {"event": "error", "message"}.

The models are LTX Studio's own workers, run as they are in the Python LTX Studio names for them; nothing of TaDaMoo is imported here but the standard-library
lease client, read by its path. The lease is asked for before anything touches the GPU and is held until the job ends; with no lease the job does not
start. Closing stdin ends the job: the model process and what it started are killed and the lease is given back. Parts of LTX Studio's `core` that are
imported here (its registry of paths and releases) are standard library only."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

LEASE_CLIENT = Path(__file__).resolve().parents[1] / "heavy.py"
OWNER = {"image": "image", "video": "video", "avatar": "avatar", "upscale": "upscale", "interpolate": "interpolate", "modify": "modify"}
PURPOSE = {"image": "making a picture", "video": "making a clip", "avatar": "making a talking head", "upscale": "enlarging a video", "interpolate": "adding frames to a video", "modify": "changing what a video shows"}
MINUTES = {"image": 2, "video": 6, "avatar": 20, "upscale": 10, "interpolate": 3, "modify": 30}          # what a swap tells the person the job takes
STEP = re.compile(r"(\d+)/(\d+)\s*\[")                   # a progress bar: "  3/8 [00:12<00:20"
PROG = re.compile(r"^PROG:(\d+)/(\d+)")                  # the frame counter of the frame-interpolation runner
HTTP_SLACK_S = 135.0                                      # the call that asks for the lease may last as long as the wait plus the engine's own time


class JobError(Exception):
    pass


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def lease_module():
    spec = importlib.util.spec_from_file_location("tadamoo_heavy", LEASE_CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stdin_closed() -> bool:
    """Whether the other end of stdin is gone, asked without waiting for anything to be written."""
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
            available = ctypes.c_ulong(0)
            return not ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
        import select
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(readable) and not sys.stdin.buffer.peek(1)
    except Exception:
        return False


def kill_tree(proc: "subprocess.Popen") -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def use_ltx(folder: str):
    """LTX Studio's `core` package, importable from its folder."""
    root = str(Path(folder))
    if root not in sys.path:
        sys.path.insert(0, root)
    for name in [n for n in sys.modules if n == "core" or n.startswith("core.")]:
        if getattr(sys.modules[name], "__file__", "") and not str(sys.modules[name].__file__).startswith(root):
            del sys.modules[name]                         # another folder's core is not this one's


def image_plan(folder: str, spec: dict) -> dict:
    """What it takes to make a picture: the worker's command, its parameters, and what is missing on disk."""
    use_ltx(folder)
    img_gen_job, model_paths, weights_catalog = (importlib.import_module("core." + n) for n in ("img_gen_job", "model_paths", "weights_catalog"))
    py, script = img_gen_job._ARCH_RUNNER["qwen_image21"]
    missing = []
    for entry_id in ("qwen21-configs", "qwen21-vae", "qwen21-dit-nvfp4", "qwen21-te-nvfp4"):
        entry = model_paths.entry(entry_id)
        if not entry or not weights_catalog.is_present(entry, str(model_paths.models_root())):
            missing.append(entry_id)
    req = spec.get("request") or {}
    params = {"prompt": req.get("prompt", ""), "width": req.get("width"), "height": req.get("height"), "steps": req.get("steps"), "seed": req.get("seed"),
              "negative": req.get("negative") or None, "ref_images": req.get("refs") or [], "output": spec["output"], "arch": "qwen_image21", "qwen21_dit": "qwen21-dit-nvfp4", "qwen21_te": "nvfp4",
              "need_vram_gb": max(4.0, int(spec.get("mib") or 0) / 1024 - 2.0), "need_commit_gb": 6, "parent_pid": os.getpid()}
    missing += [f"the reference {r}" for r in (req.get("refs") or []) if not Path(r).is_file()]
    return {"python": spec.get("python") or py, "script": script, "params": params, "missing": missing, "env": {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}}


def video_plan(folder: str, spec: dict) -> dict:
    try:
        from tadamoo.media import attention_env
    except ImportError:                                  # run by file, beside this one
        import attention_env
    use_ltx(folder)
    ltx_release, ltx_video_job = (importlib.import_module("core." + n) for n in ("ltx_release", "ltx_video_job"))
    py, script = ltx_release.runner("2.5")
    missing = [f"component {c}" for c in ltx_release.missing_components("2.5")]
    name = spec.get("video_model") or ""
    model = next((c for c in ltx_release.checkpoints("2.5") if os.path.basename(c) == name), "")
    if not model:
        missing.append(f"checkpoint {name}")
    req = spec.get("request") or {}
    if req.get("image") and not Path(req["image"]).is_file():
        missing.append(f"the picture {req['image']}")
    if req.get("audio") and not Path(req["audio"]).is_file():
        missing.append(f"the recording {req['audio']}")
    attention, warnings = spec.get("attention") or "cudnn", []
    python, attention, warning = attention_env.resolve(attention, spec.get("python") or py, spec.get("attention_env") or "")
    if warning:
        warnings.append(warning)
    teacache = float(spec.get("teacache") or 0)
    extra = {**({"audio_path": req["audio"]} if req.get("audio") else {}), **({"teacache_thresh": teacache} if teacache > 0 else {})}
    params = {}
    if not missing:
        params = ltx_video_job.build_params(extra=extra or None, **({"steps": int(req["steps"])} if req.get("steps") else {}), output=spec["output"], prompt=req.get("prompt", ""), width=req.get("width"), height=req.get("height"),
                                            num_frames=req.get("frames"), fps=req.get("fps"), seed=req.get("seed"), model=model, ltx_release="2.5",
                                            **({"negative": req["negative"]} if req.get("negative") else {}), **({"image_path": req["image"]} if req.get("image") else {}))
    plan = {"python": python, "script": script, "params": params, "missing": missing, "env": {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}, "warnings": warnings}
    if req.get("crf") is not None or attention != "cudnn":      # the saved video's quality and the attention: the worker has no setting for them, the launcher sets them from outside (ltx_launch.py)
        plan.update(script=str(Path(__file__).with_name("ltx_launch.py")), args=[script, "{params}"])
        if req.get("crf") is not None:
            plan["env"].update(LTX_X264_CRF=str(int(req["crf"])), LTX_X264_PRESET=str(req.get("crf_preset") or "medium"))
        if attention != "cudnn":
            plan["env"]["LTX_ATTENTION"] = attention
    return plan


def _video_input(req: dict, missing: list) -> str:
    video = req.get("video", "")
    if video and not Path(video).is_file():
        missing.append(f"the video {video}")
    return video


def interpolate_plan(folder: str, spec: dict) -> dict:
    """Frames made between the real ones of a video (RIFE), run by LTX Studio's own runner in its inference environment."""
    use_ltx(folder)
    rife = importlib.import_module("core.rife")
    req = spec.get("request") or {}
    missing = [] if rife.available() else ["RIFE (the code in workers/rife_pr and its flownet.pkl weights)"]
    video = _video_input(req, missing)
    return {"python": spec.get("python") or rife._INF_PY, "script": str(Path(folder) / "scripts" / "rife_run.py"), "args": [video, spec["output"], str(int(req.get("factor") or 2))],
            "params": {}, "missing": missing, "cwd": str(folder), "env": {}}


def upscale_plan(folder: str, spec: dict) -> dict:
    """A video made larger (FlashVSR+), run by LTX Studio's own runner in its inference environment."""
    use_ltx(folder)
    service = importlib.import_module("core.flashvsrplus_service")
    req = spec.get("request") or {}
    mode = req.get("mode") or "turbo"
    missing = list(service.preflight(mode))
    video = _video_input(req, missing)
    args = [str(service.RUNNER), video, spec["output"], "--mode", mode, "--scale", f"{float(req.get('scale') or 2.0):g}", "--seed", "1", "--temporal-compression", "4", "--decode-every", "profile",
            "--code-dir", str(service.CODE_DIR), "--ckpt-root", str(service.ckpt_root())]
    # LTX Studio's runner, run through flashvsr_launch.py: the Triton block-sparse kernel (the runner's SDPA mask is refused at 2x), the picture saved at crf 4, and as many frames out as in
    return {"python": spec.get("python") or str(service.VENV_PY), "python_flags": ["-I"], "script": str(Path(__file__).with_name("flashvsr_launch.py")), "args": args, "params": {}, "missing": missing,
            "cwd": str(service.RUNNER.parent), "env": {"PYTHONIOENCODING": "utf-8", "FLASHVSR_CRF": "4"}}


def avatar_plan(folder: str, spec: dict) -> dict:
    """A portrait and a recording made into a talking video by LongCat-Video-Avatar, which LTX Studio runs in an environment of its own."""
    use_ltx(folder)
    service = importlib.import_module("core.longcat_service")
    precision = spec.get("avatar_precision") or "int8"
    missing = [] if service.is_installed() else ["the LongCat Avatar environment"]
    missing += [f"weights {w}" for w in (service.missing_weights(precision) if not missing else [])]
    req = spec.get("request") or {}
    params = {"mode": "ai2v", "image_path": req.get("image", ""), "audio_path": req.get("audio", ""), "prompt": req.get("prompt", ""), "resolution": req.get("resolution", "480p"),
              "aspect": "16:9", "resize_mode": "crop", "precision": precision, "seed": req.get("seed"), "vocal_separation": False, "output": spec["output"],
              **({"negative": req["negative"]} if req.get("negative") else {}), **{k: req[k] for k in ("ref_img_index", "mask_frame_range") if req.get(k) is not None},
              "need_vram_gb": max(8.0, int(spec.get("mib") or 0) / 1024 - 2.0), "need_commit_gb": 8, "parent_pid": os.getpid()}
    for key in ("image_path", "audio_path"):
        if not missing and params[key] and not Path(params[key]).is_file():
            missing.append(f"the file {params[key]}")
    return {"python": str(service.VENV_PY), "script": str(service.WORKER), "params": params, "missing": missing, "cwd": str(service.ROOT),
            "env": {"LONGCAT_CODE_DIR": str(service.ROOT / "LongCat-Video"), "PYTHONPATH": None, "PYTHONHOME": None}}


def modify_plan(folder: str, spec: dict) -> dict:
    """A change made to what a video shows (modify_job.py, the code beside this file)."""
    import modify_job
    return modify_job.plan(folder, spec, use_ltx)


PLANS = {"image": image_plan, "video": video_plan, "avatar": avatar_plan, "interpolate": interpolate_plan, "upscale": upscale_plan, "modify": modify_plan}


def check(folder: str, video_model: str, edit_model: str = "", avatar_precision: str = "int8") -> dict:
    out = {}
    for kind, plan in PLANS.items():
        try:
            got = plan(folder, {"mib": 0, "video_model": video_model, "edit_model": edit_model, "avatar_precision": avatar_precision, "output": "check", "request": {"prompt": "x", "width": 64, "height": 64, "frames": 9, "fps": 8, "seed": 0}})
            out[kind] = {"python": got["python"], "python_ok": Path(got["python"]).is_file(), "script_ok": Path(got["script"]).is_file(), "missing": got["missing"]}
            out[kind]["ready"] = out[kind]["python_ok"] and out[kind]["script_ok"] and not got["missing"]
        except Exception as exc:
            out[kind] = {"ready": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "missing": []}
    return out


def keep_audio(source: str, out: Path) -> None:
    """Puts the sound of `source`, if it has any, back under the enlarged picture of `out` (the enlarger writes picture only); without ffmpeg or sound the file is left as it is."""
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    joined = out.with_name("joined-" + out.name)
    done = subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(out), "-i", source, "-map", "0:v:0", "-map", "1:a:0?", "-c", "copy", str(joined)],
                          capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    if done.returncode == 0 and joined.is_file():
        os.replace(joined, out)
    elif joined.exists():
        joined.unlink()


def present(folder: str, ids: list) -> dict:
    """Which weights of LTX Studio's catalogue are on disk, by catalogue id."""
    use_ltx(folder)
    model_paths = importlib.import_module("core.model_paths")
    root = str(model_paths.models_root())
    return {i: (os.path.isfile(os.path.join(root, i[5:].replace("/", os.sep))) if i.startswith("file:") else bool(model_paths.exists(i))) for i in ids}


def modules(folder: str, items: list) -> dict:
    """Whether Python packages import in the environment of a kind: `items` are `kind:module` (avatar:flash_attn); the answer is `{"present": {item: bool}, "venvs": {kind: python}}`.
    The environments are the ones LTX Studio names for the kinds; nothing is imported into this process."""
    venvs, present = {}, {}
    for item in items:
        kind, _, module = item.partition(":")
        try:
            if kind not in venvs:
                venvs[kind] = PLANS[kind](folder, {"mib": 0, "output": "check", "request": {"prompt": "x", "width": 64, "height": 64, "frames": 9, "fps": 8, "seed": 0}})["python"]
            done = subprocess.run([venvs[kind], "-I", "-c", f"import importlib.util as u,sys; sys.exit(0 if u.find_spec({module!r}) else 1)"], capture_output=True, timeout=60)
            present[item] = done.returncode == 0
        except Exception:
            present[item] = False
    return {"present": present, "venvs": venvs}


class Run:
    """The model process of one job: its output read line by line (a progress bar ends its lines with a carriage return), its end told."""

    def __init__(self, plan: dict, work: Path, cwd: str) -> None:
        params_file = work / "params.json"
        params_file.write_text(json.dumps(plan["params"]), encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", **plan["env"]}
        env = {k: v for k, v in env.items() if v is not None}          # a value of None takes the variable out of the model process's environment
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        argv = [plan["python"], *plan.get("python_flags", []), "-u", plan["script"], *([a.replace("{params}", str(params_file)) for a in plan["args"]] if "args" in plan else [str(params_file)])]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=plan.get("cwd") or cwd,
                                     creationflags=flags, start_new_session=os.name != "nt")
        self.tail: list = []
        self.result = ""

    def lines(self):
        buffer = b""
        while True:
            chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(1)
            if not chunk:
                break
            buffer += chunk
            parts = re.split(rb"[\r\n]+", buffer)
            buffer = parts.pop()
            for part in parts:
                text = part.decode("utf-8", "replace").strip()
                if text:
                    yield text
        if buffer.strip():
            yield buffer.decode("utf-8", "replace").strip()


def main() -> int:
    if "--check" in sys.argv:
        folder = sys.argv[sys.argv.index("--ltx-dir") + 1]
        model = sys.argv[sys.argv.index("--video-model") + 1] if "--video-model" in sys.argv else ""
        edit_model = sys.argv[sys.argv.index("--edit-model") + 1] if "--edit-model" in sys.argv else ""
        precision = sys.argv[sys.argv.index("--avatar-precision") + 1] if "--avatar-precision" in sys.argv else "int8"
        emit(check(folder, model, edit_model, precision))
        return 0
    if "--modules" in sys.argv:
        emit(modules(sys.argv[sys.argv.index("--ltx-dir") + 1], sys.argv[sys.argv.index("--modules") + 1].split(",")))
        return 0
    if "--present" in sys.argv:
        folder = sys.argv[sys.argv.index("--ltx-dir") + 1]
        emit(present(folder, sys.argv[sys.argv.index("--present") + 1].split(",")))
        return 0
    spec = json.loads(Path(sys.argv[sys.argv.index("--job") + 1]).read_text(encoding="utf-8"))
    kind = spec["kind"]
    state = {"proc": None, "lease": None}
    giving_back = threading.Lock()

    def give_back() -> None:
        """Gives the lease back once; a second caller waits until the first has finished, so the process never ends with the call still on its way."""
        with giving_back:
            lease, state["lease"] = state["lease"], None
            if lease is not None:
                lease.release()

    def stop() -> None:
        while not stdin_closed():
            time.sleep(0.5)
        if state["proc"] is not None:
            kill_tree(state["proc"])
        give_back()
        os._exit(3)

    threading.Thread(target=stop, daemon=True).start()
    began = time.monotonic()
    emit({"event": "start", "kind": kind})
    try:
        plan = PLANS[kind](spec["ltx_dir"], spec)
        for key in ("python", "script"):
            if not Path(plan[key]).is_file():
                raise JobError(f"LTX Studio's {key} is not there: {plan[key]}")
        if plan["missing"]:
            raise JobError("LTX Studio lacks weights this needs: " + ", ".join(plan["missing"]))
        module = lease_module()
        wait = float(spec.get("wait_s") or 0)
        state["lease"] = heavy = module.HeavyLease(OWNER[kind], int(spec["mib"]), PURPOSE[kind], minutes=MINUTES[kind], wait_s=wait, exclusive=True, held=bool(spec.get("held")),
                                                   base_url=spec.get("url") or None)
        heavy.acquire()
        lease = heavy.lease
        emit({"event": "lease", "ok": heavy.gpu_ok, "granted_mib": lease.granted_mib, "reason": heavy.reason, "message": heavy.message, "message_it": heavy.message_it,
              "swapped": heavy.swapped})
        if not heavy.gpu_ok:
            emit({"event": "refused", "reason": heavy.reason or "unavailable", "message": heavy.message, "message_it": heavy.message_it})
            return 4
        work = Path(spec["output"]).parent
        if plan.get("pre"):
            import modify_job

            def register(proc) -> None:
                state["proc"] = proc
            plan["pre"](lambda line: emit({"event": "log", "line": line[:200]}), modify_job.popen_for(register))
            state["proc"] = None
        warnings = list(plan.get("warnings") or [])
        for text in warnings:
            emit({"event": "log", "line": "[warning] " + text[:180]})
        run = Run(plan, work, spec["ltx_dir"])
        state["proc"] = run.proc
        for line in run.lines():
            run.tail = (run.tail + [line])[-30:]
            if line.startswith("[attention] WARNING"):
                warnings.append(line[len("[attention] "):])
            if line.startswith("OK:"):
                run.result = line[3:].strip()
                continue
            match = PROG.match(line) or STEP.search(line)
            if match:
                emit({"event": "progress", "step": int(match.group(1)), "steps": int(match.group(2))})
            elif line.startswith("["):
                emit({"event": "log", "line": line[:200]})
        code = run.proc.wait()
        out = Path(spec["output"])
        if code != 0 or not out.is_file():
            raise JobError(f"the model process ended with code {code} and made no file: " + " | ".join(run.tail[-4:])[-600:])
        if kind == "upscale":
            keep_audio(spec["request"]["video"], out)
        if plan.get("post"):
            plan["post"]()
        emit({"event": "done", "path": str(out), "bytes": out.stat().st_size, "seconds": round(time.monotonic() - began, 1), "warnings": warnings,
              "lease": {"id": lease.id or "", "owner": OWNER[kind], "granted_mib": lease.granted_mib, "swapped": heavy.swapped, "risky": heavy.risky}})
        return 0
    except JobError as exc:
        emit({"event": "error", "message": str(exc)})
        return 1
    except Exception as exc:
        emit({"event": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return 1
    finally:
        if state["proc"] is not None and state["proc"].poll() is None:
            kill_tree(state["proc"])
        give_back()


if __name__ == "__main__":
    raise SystemExit(main())
