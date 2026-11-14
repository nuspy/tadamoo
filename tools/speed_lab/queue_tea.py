"""LTX Studio's TeaCache 0.03 with each attention backend: every cell in turns (round after round, so that all see the same PC), 4 runs each, the PC's load noted before each run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = "E:/AI/Output/speed_study/20261114/tea"
CELLS = ["t3_cudnn", "t3_sage1", "t3_sage2", "t3_fa4", "t3_efficient", "t3_t211_cudnn", "t3_fa2", "t3_xformers"]


def main() -> int:
    with open("E:/AI/speedlab/work/tea.out", "ab") as log:
        for run in range(1, 5):
            for cell in CELLS:
                subprocess.run([sys.executable, str(HERE / "sweep.py"), "--out", OUT, "--runs", str(run), cell], stdout=log, stderr=subprocess.STDOUT)
    print("finished-tea")
    return 0


if __name__ == "__main__":
    sys.exit(main())
