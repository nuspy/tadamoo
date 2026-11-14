"""Runs LTX Studio's `ltx2_infer.py` with the quality of the saved video set from the outside.

    python ltx_launch.py ltx2_infer.py PARAMS.json            with LTX_X264_CRF and LTX_X264_PRESET in the environment

LTX Studio's worker saves the clip with PyAV at a fixed `crf 19, preset veryfast`, which smears the fine detail of a face (the file is a fraction of what the picture needs). It has no parameter for it. This
launcher reads the worker's source, replaces that one setting with the one asked for and runs the result as the worker, which is what a `video_crf` parameter in the worker would do; if the setting is not found
(the worker changed) it stops and says so instead of saving at the old quality. Standard library only; run by the Python of the LTX environment."""
from __future__ import annotations

import os
import sys
from pathlib import Path

OLD = '"crf": "19", "preset": "veryfast"'


def main() -> int:
    script = Path(sys.argv[1])
    crf, preset = os.environ.get("LTX_X264_CRF", ""), os.environ.get("LTX_X264_PRESET", "")
    source = script.read_text(encoding="utf-8")
    if not (crf.isdigit() and preset.isalpha()):
        print("ERRORE: LTX_X264_CRF (a number) and LTX_X264_PRESET (a word) are needed", flush=True)
        return 2
    if OLD not in source:
        print(f"ERRORE: the save setting {OLD} is not in {script.name}: the worker changed, the launcher has to follow", flush=True)
        return 2
    code = compile(source.replace(OLD, f'"crf": "{crf}", "preset": "{preset}"'), str(script), "exec")
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    exec(code, {"__name__": "__main__", "__file__": str(script), "__builtins__": __builtins__})
    return 0


if __name__ == "__main__":
    sys.exit(main())
