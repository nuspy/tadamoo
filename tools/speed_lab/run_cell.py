"""One timed run of the LTX 2.5 clip job (the winning "ltx25t_12" of the motion matrix) in a given environment and configuration.

    python run_cell.py --cell NAME --run N --python PY --out DIR [--env K=V ...] [--teacache T]

The job is the one `tests/motion_matrix.py` made: the same still, recording, prompt, seed, 896x512, 169 frames, 12 steps, saved at crf 4. It is started as a process of its own at below-normal
priority (as TaDaMoo starts a job) through `ltx_bench.py`; the wall time, the stage times, the peak VRAM (the process's own counters and `nvidia-smi` samples) and the exit status are written to
DIR/NAME/runN/result.json, the log to log.txt, the clip to take.mp4."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

LTX = Path("E:/AI/ltxstudio")
SOURCE = Path("E:/AI/Output/motion_matrix/20261114-run1/jobs/ltx25t_12/params.json")
HERE = Path(__file__).resolve().parent


def sample_vram(stop: threading.Event, peak: list) -> None:
    while not stop.is_set():
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, creationflags=0x08000000).stdout.split()
            peak[0] = max(peak[0], int(out[0]))
        except Exception:
            pass
        stop.wait(0.4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", required=True)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--python", default="E:/AI/speedlab/ltx25/Scripts/python.exe")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env", nargs="*", default=[])
    ap.add_argument("--prompt-suffix", default="", help="added to the prompt: a prompt the embedding cache has not seen")
    ap.add_argument("--teacache", type=float, default=0.0)
    ap.add_argument("--timeout", type=int, default=1500)
    args = ap.parse_args()
    run = Path(args.out) / args.cell / f"run{args.run}"
    run.mkdir(parents=True, exist_ok=True)
    params = json.loads(SOURCE.read_text(encoding="utf-8"))
    params["output"] = str(run / "take.mp4")
    if args.prompt_suffix:
        params["prompt"] += args.prompt_suffix
    if args.teacache:
        params["teacache_thresh"] = args.teacache
    (run / "params.json").write_text(json.dumps(params), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True", "LTX_X264_CRF": "4", "LTX_X264_PRESET": "medium"}
    env.update(dict(kv.split("=", 1) for kv in args.env))
    for k in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)
    argv = [args.python, "-u", str(HERE / "ltx_bench.py"), str(LTX / "workers" / "ltx2_infer.py"), str(run / "params.json")]
    import psutil
    cpu_before = psutil.cpu_percent(interval=2.0)                          # how busy the PC is before the run starts (the job itself is not running yet)
    gpu_before = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
    stop, peak = threading.Event(), [0]
    threading.Thread(target=sample_vram, args=(stop, peak), daemon=True).start()
    t0 = time.time()
    with open(run / "log.txt", "wb") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(LTX), creationflags=0x4000 | 0x08000000)
        try:
            code = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            code = -9
    wall = time.time() - t0
    stop.set()
    text = (run / "log.txt").read_text(encoding="utf-8", errors="replace")
    stages = [float(x) for x in re.findall(r"\[SL\] stage \d \(denoise incl\. load\) ([\d.]+) s", text)]
    mem = re.search(r"peak_allocated_gib ([\d.]+) peak_reserved_gib ([\d.]+)", text)
    fw = re.search(r"transformer_forwards (\d+) total ([\d.]+) s", text)
    bd = re.search(r"transformer_builds (\d+) total ([\d.]+) s", text)
    result = {"cpu_busy_before_pct": cpu_before, "gpu_before": gpu_before, "build_s": float(bd.group(2)) if bd else None, "forwards": int(fw.group(1)) if fw else None, "forward_s": float(fw.group(2)) if fw else None, "cell": args.cell, "run": args.run, "env": args.env, "teacache": args.teacache, "returncode": code, "wall_s": round(wall, 1), "stage_s": stages,
              "peak_alloc_gib": float(mem.group(1)) if mem else None, "peak_reserved_gib": float(mem.group(2)) if mem else None,
              "nvsmi_peak_mib": peak[0], "ok": code == 0 and (run / "take.mp4").is_file(), "attn_line": next((l for l in text.splitlines() if l.startswith("[SL] attention")), ""), "versions": next((l for l in text.splitlines() if l.startswith("[SL] torch")), ""),
              "error_tail": "" if code == 0 else "\n".join(text.splitlines()[-12:])}
    (run / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "error_tail"}), flush=True)
    if result["error_tail"]:
        print(result["error_tail"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
