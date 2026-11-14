"""Replays one nvcc command of a failed ninja build log, alone, so that its flags can be changed one at a time.

    python replay_nvcc.py LOG OUT_DIR [--drop FLAG ...] [--keep-threads]

Reads the first `[1/N] ...nvcc ...` line of LOG, takes out the dependency-file options, points the output to OUT_DIR, drops the flags named by --drop, and runs it inside the Visual Studio build environment
with the CUDA copy in E:/AI/speedlab/cuda131. Prints the exit code and the last lines."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

VCVARS = r'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("out")
    ap.add_argument("--drop", nargs="*", default=[])
    args = ap.parse_args()
    lines = Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines()
    cmd = next(l for l in lines if re.match(r"\[\d+/\d+\] .*nvcc", l))
    cmd = re.sub(r"^\[\d+/\d+\] ", "", cmd)
    cmd = re.sub(r"-MD -MF \S+ ", "", cmd)
    target = "-o " + str(Path(args.out) / "t.obj")
    cmd = re.sub(r"-o \S+\.obj", lambda m: target, cmd)
    for flag in args.drop:
        cmd = cmd.replace(flag, "")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    bat = Path(args.out) / "run.bat"
    bat.write_text("\r\n".join(["@echo off", f'call "{VCVARS}" -vcvars_ver=14.44', r"set CUDA_HOME=E:\AI\speedlab\cuda131", r"set PATH=%CUDA_HOME%\bin;%PATH%", "set NVCC_APPEND_FLAGS=-Xcompiler /Zc:preprocessor",
                               r"cd /d E:\AI\speedlab\build\SageAttention2", cmd + f' > "{Path(args.out) / "out.txt"}" 2>&1', f'echo rc=%errorlevel% >> "{Path(args.out) / "out.txt"}"', ""]), encoding="ascii")
    subprocess.run(["cmd", "/c", str(bat)])
    tail = (Path(args.out) / "out.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
    print("\n".join(t[:200] for t in tail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
