"""Round 2 of the speed study: the attention micro-benchmark in each environment, then the sweeps with three runs per cell, one after the other (one job on the GPU at a time)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = "E:/AI/speedlab"
OUT = "E:/AI/Output/speed_study/20261114"
WORK = Path(LAB) / "work"


def run(cmd, log):
    with open(WORK / log, "ab") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)


def main() -> int:
    plan = []
    for i in (1, 2, 3):
        plan += [(i + 4, "pair_base"), (i, "s2b_sage2"), (i, "s2b_fb10")]
    for runs, cells in plan:
        run([sys.executable, str(HERE / "sweep.py"), "--out", OUT, "--runs", str(runs), cells], "sweep8.out")
    print("finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
