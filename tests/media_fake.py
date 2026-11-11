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


def make_ltx(root: Path, missing=(), video_checkpoint=True, avatar=True, avatar_missing=()) -> Path:
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
        '''), encoding="utf-8")
    (root / "core" / "weights_catalog.py").write_text(f"MISSING = {list(missing)!r}\n\n\ndef is_present(entry, root):\n    return entry['id'] not in MISSING\n", encoding="utf-8")
    (root / "core" / "img_gen_job.py").write_text(f"_ARCH_RUNNER = {{'qwen_image21': ({sys.executable!r}, {str(root / 'workers' / 'qwen_image21_infer.py')!r})}}\n", encoding="utf-8")
    (root / "core" / "ltx_release.py").write_text(textwrap.dedent(f'''
        import os
        def runner(release):
            assert release == "2.5"
            return {sys.executable!r}, {str(root / "workers" / "ltx2_infer.py")!r}
        def missing_components(release):
            return []
        def checkpoints(release):
            d = {str(root / "models")!r}
            return [os.path.join(d, n) for n in sorted(os.listdir(d))]
        '''), encoding="utf-8")
    (root / "core" / "ltx_video_job.py").write_text(textwrap.dedent('''
        def build_params(*, output, prompt, width, height, num_frames, fps, seed, model, ltx_release, negative="worst quality", steps=8, guidance=3.0):
            return {"output": output, "prompt": prompt, "width": width, "height": height, "num_frames": num_frames, "fps": fps, "seed": seed,
                    "model": model, "ltx_release": ltx_release, "negative": negative, "steps": steps, "guidance": guidance}
        '''), encoding="utf-8")
    return root


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
