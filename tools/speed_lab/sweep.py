"""Runs a list of cells (configurations of the LTX 2.5 clip job) one after the other, several runs each, skipping the runs that already have a result.

    python sweep.py --out DIR --runs 3 NAME[,NAME...]      (names from CELLS below)

A cell is a Python (an environment of E:/AI/speedlab), the environment variables `ltx_bench.py` reads, and an optional TeaCache threshold of LTX Studio's worker. Run 1 of a cell is the cold one
(its compile and kernel caches are empty), runs 2 and later are warm."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = "E:/AI/speedlab"
PY = {"base": f"{LAB}/ltx25/Scripts/python.exe", "x": f"{LAB}/ltx25x/Scripts/python.exe", "t215": f"{LAB}/ltx25_t215/Scripts/python.exe", "t211": f"{LAB}/ltx25_t211/Scripts/python.exe", "s2": f"{LAB}/ltx25_s2/Scripts/python.exe", "fi": f"{LAB}/ltx25_fi/Scripts/python.exe", "s2b": f"{LAB}/ltx25_s2b/Scripts/python.exe", "s3": f"{LAB}/ltx25_s3/Scripts/python.exe"}

# name: (environment key, [env vars], teacache threshold)
CELLS: dict = {
    "base": ("base", [], 0.0),
    "sdpa_cudnn": ("base", ["SL_ATTN=cudnn"], 0.0),
    "sdpa_flash": ("base", ["SL_ATTN=sdpa_flash"], 0.0),
    "sdpa_efficient": ("base", ["SL_ATTN=efficient"], 0.0),
    "s2_sage2": ("s2", ["SL_ATTN=sage2"], 0.0),
    "t215_base": ("t215", [], 0.0),
    "t215_fa4": ("t215", ["SL_ATTN=fa4"], 0.0),
    "t211_base": ("t211", [], 0.0),
    "t211_fa2": ("t211", ["SL_ATTN=fa2"], 0.0),
    "t211_xformers": ("t211", ["SL_ATTN=xformers"], 0.0),
    "fi_flashinfer": ("fi", ["SL_ATTN=flashinfer"], 0.0),
    "s2_fb10": ("s2", ["SL_ATTN=sage2", "SL_CACHE=fb:0.10"], 0.0),
    "s2_fb13": ("s2", ["SL_ATTN=sage2", "SL_CACHE=fb:0.13"], 0.0),
    "x_fb_13": ("x", ["SL_CACHE=fb:0.13"], 0.0),
    "pair_base": ("base", [], 0.0),
    "pair_sage2": ("s2", ["SL_ATTN=sage2"], 0.0),
    "pair_fb10": ("x", ["SL_CACHE=fb:0.10"], 0.0),
    "pair_sage2_fb10": ("s2", ["SL_ATTN=sage2", "SL_CACHE=fb:0.10"], 0.0),
    "s2b_sage2": ("s2b", ["SL_ATTN=sage2"], 0.0),
    "s2b_fb10": ("s2b", ["SL_ATTN=sage2", "SL_CACHE=fb:0.10"], 0.0),
    "s3_sage3": ("s3", ["SL_ATTN=sage3"], 0.0),
    "s3_fb10": ("s3", ["SL_ATTN=sage3", "SL_CACHE=fb:0.10"], 0.0),
    "x_triton": ("x", [], 0.0),
    "x_fa4": ("x", ["SL_ATTN=fa4"], 0.0),
    "x_sage1": ("x", ["SL_ATTN=sage1"], 0.0),
    "x_compile": ("x", ["SL_COMPILE=default"], 0.0),
    "x_compile_ro": ("x", ["SL_COMPILE=reduce-overhead"], 0.0),
    "x_compile_cap": ("x", ["SL_COMPILE=capture"], 0.0),
    "x_teacache_ltxstudio_03": ("x", [], 0.03),
    "x_fb_00": ("x", ["SL_CACHE=fb:0.0"], 0.0),
    "x_guider_one": ("x", ["SL_GUIDER=one"], 0.0),
    "x_guider_cfg": ("x", ["SL_GUIDER=cfg"], 0.0),
    "x_fb_10": ("x", ["SL_CACHE=fb:0.10"], 0.0),
    "x_fb_17": ("x", ["SL_CACHE=fb:0.17"], 0.0),
    "x_tea_10": ("x", ["SL_CACHE=tea:0.10"], 0.0),
    "x_fb_05": ("x", ["SL_CACHE=fb:0.05"], 0.0),
    "x_fb_10": ("x", ["SL_CACHE=fb:0.10"], 0.0),
    "x_tea_05": ("x", ["SL_CACHE=tea:0.05"], 0.0),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("cells")
    args = ap.parse_args()
    for name in args.cells.split(","):
        envkey, env, tea = CELLS[name]
        for n in range(1, args.runs + 1):
            if (Path(args.out) / name / f"run{n}" / "result.json").is_file():
                continue
            cmd = [sys.executable, str(HERE / "run_cell.py"), "--cell", name, "--run", str(n), "--python", PY[envkey], "--out", args.out, "--teacache", str(tea), "--env", *env]
            print(f"== {name} run {n}", flush=True)
            subprocess.run(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
