"""Runs any command at below-normal priority, timing it and sampling the VRAM, and writes result.json and log.txt in a folder.

    python timed_run.py --dir FOLDER [--env K=V ...] [--cwd DIR] [--timeout S] -- COMMAND ARGS...

result.json: wall seconds, exit code, the `nvidia-smi` memory peak (MiB, the whole GPU, so it includes what the other programs hold), the last 15 lines of the log on failure."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path


def sample(stop: threading.Event, peak: list) -> None:
    while not stop.is_set():
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, creationflags=0x08000000).stdout.split()
            peak[0] = max(peak[0], int(out[0]))
        except Exception:
            pass
        stop.wait(0.4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--env", nargs="*", default=[])
    ap.add_argument("--cwd")
    ap.add_argument("--timeout", type=int, default=3000)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1"}
    env.update(dict(kv.split("=", 1) for kv in args.env))
    stop, peak = threading.Event(), [0]
    threading.Thread(target=sample, args=(stop, peak), daemon=True).start()
    t0 = time.time()
    with open(folder / "log.txt", "wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=args.cwd, creationflags=0x4000 | 0x08000000)
        try:
            code = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            code = -9
    wall = time.time() - t0
    stop.set()
    tail = "\n".join((folder / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-15:]) if code else ""
    result = {"cmd": cmd, "exit": code, "wall_s": round(wall, 1), "nvsmi_peak_mib": peak[0], "tail": tail}
    (folder / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cmd"}), flush=True)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
