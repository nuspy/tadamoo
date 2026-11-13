"""Stand-ins for the picture and video tests: a folder that looks like LTX Studio (its `core` registry and two model workers that load nothing) and a
VRAM broker that answers the three lease routes over HTTP. Nothing here touches a GPU."""
import base64
import http.server
import json
import sys
import textwrap
import threading
from pathlib import Path

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64

WORKER = r'''
import json, os, sys, time
params_file = sys.argv[1]
p = json.load(open(params_file))
out = p["output"]
open(os.path.join(os.path.dirname(params_file), "started.pid"), "w").write(str(os.getpid()))
print("[fake] loading", flush=True)
prompt = p["prompt"]
if "fail" in prompt:
    print("ERRORE: the model did not load", flush=True)
    sys.exit(1)
if "hang" in prompt:
    time.sleep(120)
if "slow" in prompt:
    for i in range(1, 400):
        sys.stdout.write("  %d/400 [00:01<00:02\r" % i)
        sys.stdout.flush()
        time.sleep(0.1)
for i in range(1, 4):
    sys.stdout.write("  %d/3 [00:01<00:02, 1.00it/s]\r" % i)
    sys.stdout.flush()
if "nofile" not in prompt:
    open(out, "wb").write(@@DATA@@)
print("OK:" + out, flush=True)
'''


RIFE_RUNNER = r'''
import sys
src, out, factor = sys.argv[1], sys.argv[2], sys.argv[3]
open(out + ".args", "w").write(" ".join(sys.argv[1:]))
if "fail" in src:
    print("ERRORE: rife failed", flush=True)
    sys.exit(1)
for i in (20, 40):
    print("PROG:%d/40" % i, flush=True)
open(out, "wb").write(open(src, "rb").read() + b"x" * int(factor))
print("OK:" + out, flush=True)
'''

SCALER_RUNNER = r'''
import sys
src, out = sys.argv[1], sys.argv[2]
open(out + ".args", "w").write(" ".join(sys.argv[1:]))
print("[flashvsrplus] working", flush=True)
open(out, "wb").write(open(src, "rb").read() + b"y")
print("[flashvsrplus] OK -> " + out, flush=True)
'''


def make_ltx(root: Path, missing=(), video_checkpoint=True, avatar=True, avatar_missing=(), rife=True, scaler_missing=(), edit_checkpoint=True, edit_loras=("t2v", "r2v", "edit", "restyle")) -> Path:
    """A folder with the parts of LTX Studio the worker reads: `core` (paths, releases, job parameters) and `workers/`."""
    (root / "core").mkdir(parents=True)
    (root / "workers").mkdir()
    (root / "workers" / "qwen_image21_infer.py").write_text(WORKER.replace("@@DATA@@", repr(PNG)), encoding="utf-8")
    (root / "workers" / "ltx2_infer.py").write_text(WORKER.replace("@@DATA@@", repr(MP4)), encoding="utf-8")
    (root / "longcat" / "LongCat-Video").mkdir(parents=True)
    (root / "workers" / "longcat_avatar_infer.py").write_text(WORKER.replace("@@DATA@@", repr(MP4)), encoding="utf-8")
    (root / "core" / "longcat_service.py").write_text(textwrap.dedent(f'''
        from pathlib import Path
        ROOT = Path({str(root / "longcat")!r})
        VENV_PY = Path({sys.executable!r})
        WORKER = Path({str(root / "workers" / "longcat_avatar_infer.py")!r})
        INSTALLED = {avatar!r}
        def is_installed():
            return INSTALLED
        def missing_weights(precision):
            return {list(avatar_missing)!r}
        '''), encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "rife_run.py").write_text(RIFE_RUNNER, encoding="utf-8")
    (root / "core" / "rife.py").write_text(f"_INF_PY = {sys.executable!r}\n\n\ndef available():\n    return {rife!r}\n", encoding="utf-8")
    (root / "workers" / "flashvsrplus_run.py").write_text(SCALER_RUNNER, encoding="utf-8")
    (root / "core" / "flashvsrplus_service.py").write_text(textwrap.dedent(f'''
        from pathlib import Path
        CODE_DIR = Path({str(root / "code")!r})
        VENV_PY = Path({sys.executable!r})
        RUNNER = Path({str(root / "workers" / "flashvsrplus_run.py")!r})
        def ckpt_root():
            return Path({str(root / "ckpt")!r})
        def preflight(mode="turbo"):
            return {list(scaler_missing)!r}
        '''), encoding="utf-8")
    (root / "models").mkdir()
    for name in ("ltx-2.5-22b-distilled-transformer-nvfp4.safetensors",):
        if video_checkpoint:
            (root / "models" / name).write_bytes(b"x")
    (root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "core" / "model_paths.py").write_text(textwrap.dedent(f'''
        def entry(i):
            return {{"id": i}}
        def models_root():
            return "x"
        MISSING = {list(missing)!r}
        def exists(i):
            return not i.startswith("absent") and i not in MISSING
        '''), encoding="utf-8")
    (root / "core" / "weights_catalog.py").write_text(f"MISSING = {list(missing)!r}\n\n\ndef is_present(entry, root):\n    return entry['id'] not in MISSING\n", encoding="utf-8")
    (root / "core" / "img_gen_job.py").write_text(f"_ARCH_RUNNER = {{'qwen_image21': ({sys.executable!r}, {str(root / 'workers' / 'qwen_image21_infer.py')!r})}}\n", encoding="utf-8")
    (root / "core" / "ltx_release.py").write_text(textwrap.dedent(f'''
        import os
        def runner(release):
            assert release in ("2.3", "2.5")
            return {sys.executable!r}, {str(root / "workers" / "ltx2_infer.py")!r}
        def missing_components(release):
            return []
        def checkpoints(release):
            d = {str(root / "models23")!r} if release == "2.3" else {str(root / "models")!r}
            return [os.path.join(d, n) for n in sorted(os.listdir(d))]
        def is_distilled(name):
            return "distilled" in os.path.basename(name).lower()
        '''), encoding="utf-8")
    add_edit_engine(root, edit_checkpoint, edit_loras)
    (root / "core" / "ltx_video_job.py").write_text(textwrap.dedent('''
        def build_params(*, output, prompt, width, height, num_frames, fps, seed, model, ltx_release, negative="worst quality", steps=8, guidance=3.0, image_path=""):
            return {"output": output, "prompt": prompt, "width": width, "height": height, "num_frames": num_frames, "fps": fps, "seed": seed,
                    "model": model, "ltx_release": ltx_release, "negative": negative, "steps": steps, "guidance": guidance, "image_path": image_path}
        '''), encoding="utf-8")
    return root


PROBE = r'''
import re, types
def probe(path, count_frames=False):
    text = open(path, "rb").read().decode("latin-1")
    frames = int((re.search(r"frames=(\d+)", text) or [0, 65])[1])
    return types.SimpleNamespace(width=1920, height=1080, fps=25.0, nb_frames=frames, duration=frames / 25.0, has_audio="audio=1" in text, codec="h264", pix_fmt="yuv420p")
'''


def add_edit_engine(root: Path, checkpoint=True, loras=("t2v", "r2v", "edit", "restyle")) -> None:
    """The parts of LTX Studio the `modify` kind reads: its LTX 2.3 checkpoint folder, the editing LoRAs, the tracking and the video preparation, with stand-ins that do no work."""
    (root / "models23").mkdir(exist_ok=True)
    if checkpoint:
        (root / "models23" / "ltx-2.3-22b-dev-fp8.safetensors").write_bytes(b"x")
        (root / "models23" / "PinkCherry_FineTune_fp8scaled_v1_8_LTX23.safetensors").write_bytes(b"x")
    lora_dir = root / "loras"
    (lora_dir / "LTX-2").mkdir(parents=True, exist_ok=True)
    names = {"t2v": "ltx23_inpaint_masked_t2v_rank128_v1_10000steps.safetensors", "r2v": "ltx23_inpaint_masked_r2v_rank32_v1_3000steps.safetensors",
             "edit": "ltx23_edit_anything_global_rank128_v1_9000steps_adamw.safetensors"}
    for key, name in names.items():
        if key in loras:
            (lora_dir / "LTX-2" / name).write_bytes(b"x")
    (root / "core" / "video_ic_prep.py").write_text(PROBE, encoding="utf-8")
    (root / "core" / "video_edit_modes.py").write_text(textwrap.dedent('''
        import types
        DEFAULT_OVERLAP = 9
        def get_spec(key):
            return types.SimpleNamespace(key=key, max_frames_per_segment=121, max_long_edge=1280)
        def clamp_resolution(w, h, spec):
            k = min(1.0, spec.max_long_edge / max(w, h))
            return max(64, round(w * k / 64) * 64), max(64, round(h * k / 64) * 64), k < 1.0
        '''), encoding="utf-8")
    (root / "core" / "video_edit_ic.py").write_text(textwrap.dedent(f'''
        import os, types
        class IcJobError(Exception):
            pass
        def distilled_lora_ok(release):
            return True
        def missing_weight(spec):
            return "" if {"restyle" in loras!r} else "weight restyle missing"
        def build_job(cfg, out_dir, stamp, output, log=None):
            if not cfg.get("style_image"):
                raise IcJobError("immagine di stile mancante")
            return types.SimpleNamespace(params={{"prompt": cfg["prompt"], "control_type": "restyle", "control_video": cfg["video"], "ic_frame0_image": cfg["style_image"], "output": output,
                                                 "model": cfg.get("model"), "ltx_release": cfg.get("ltx_release"), "seed": cfg["seed"]}}, source=cfg["video"])
        def mux_audio(video, audio_src):
            out = video[:-4] + "_audio.mp4"
            open(out, "wb").write(open(video, "rb").read() + b"AUDIO")
            return out
        def finalize(job, result, log=None, cleanup=True):
            return mux_audio(result, job.source)
        '''), encoding="utf-8")
    (root / "core" / "video_inpaint.py").write_text(textwrap.dedent(f'''
        import os, sys
        INF_PY = {sys.executable!r}
        LORA_DIR = {str(lora_dir)!r}
        def lora_abspath(rel):
            return os.path.join(LORA_DIR, rel.replace("/", os.sep))
        def _lora(cfg):
            return "LTX-2/ltx23_inpaint_masked_r2v_rank32_v1_3000steps.safetensors" if cfg.get("ref_image") else "LTX-2/ltx23_inpaint_masked_t2v_rank128_v1_10000steps.safetensors"
        def missing_weight(cfg):
            lp = lora_abspath(_lora(cfg))
            return "" if os.path.exists(lp) else "weight missing: " + lp
        def inpaint_params(cfg, out):
            p = {{"prompt": cfg["prompt"], "edit_lora": _lora(cfg), "control_strength": cfg["guide"], "mask_dilate": cfg["mask_dilate"], "width": cfg["width"], "height": cfg["height"],
                 "num_frames": cfg["num_frames"], "fps": cfg["fps"], "seed": cfg["seed"], "output": out, "model": cfg["model"], "ltx_release": cfg["ltx_release"]}}
            if cfg.get("ref_image"):
                p["image_path"], p["ref_strength"] = cfg["ref_image"], cfg["ref_strength"]
            if p["num_frames"] > 97:
                p["chunk_frames"] = 97
            return p
        def fit_reference(params, out_dir, stamp, mode=None):
            return ""
        def attach_tracking(params, src, obj, guide_out, dilate, *, run=None, log=None):
            r = run([sys.executable, "-c", "import sys; print('GUIDE:' + sys.argv[1])", obj], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if "GUIDE:" not in r.stdout:
                raise RuntimeError("tracking failed")
            open(os.path.splitext(src)[0] + "_tracked.txt", "w").write(obj + " " + str(dilate))
            params["inpaint_masks_dir"] = os.path.splitext(src)[0] + "_track"
            params["control_video"] = src
            return params
        '''), encoding="utf-8")


class Broker:
    """The lease routes of the server, on a port of this PC: grants (or refuses) and keeps a list of what it was asked."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer                               # None: grant; else (status, body) to send to a lease request
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                data = json.loads(body) if body else {}
                broker.calls.append((self.path, data))
                if self.path == "/api/vram/lease":
                    status, out = broker.answer or (200, {"ok": True, "lease": {"id": "ab" * 8, "owner": data["owner"], "mib": data["mib"], "granted_mib": data["mib"], "hold_s": 60}})
                elif self.path == "/api/heavy/begin":              # no engine here that could step aside: only the free VRAM counts
                    status, out = 409, {"ok": False, "reason": "no_local_engine", "message": "nothing to free", "message_it": "niente da liberare"}
                else:
                    status, out = 200, {"ok": True}
                raw = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def paths(self):
        return [p for p, _ in self.calls]

    def lease_calls(self):
        return [c for c in self.calls if c[0] == "/api/vram/lease"]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
