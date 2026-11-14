"""Pure-speed ranking: every cell of the list run in turns (round after round, so that all cells see the same PC), 4 runs each, with the PC's load noted before each run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = "E:/AI/Output/speed_study/20261114/quiet"
CELLS = ["q_base", "q_sage2", "q_tea03", "q_g1", "q_s2_tea03", "q_s2_g1", "q_tea03_g1", "q_s2_tea03_g1", "q_s2_fb10", "q_tea02", "q_tea05"]


def main() -> int:
    with open("E:/AI/speedlab/work/quiet.out", "ab") as log:
        for run in range(1, 5):
            for cell in CELLS:
                subprocess.run([sys.executable, str(HERE / "sweep.py"), "--out", OUT, "--runs", str(run), cell], stdout=log, stderr=subprocess.STDOUT)
    print("finished-quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
