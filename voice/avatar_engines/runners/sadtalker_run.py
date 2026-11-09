"""Runs Linly-Talker's SadTalker wrapper on one photo and one audio file and writes the frames (see _frames.py).

Started by voice/avatar_engines/sadtalker.py in the engine environment with the run folder (the vendored code and the weights) as its working directory; the wrapper loads nothing from
the network when its weights are in place. `--check` loads the models and stops."""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import uuid

import _frames

SIZE = 256
PREPROCESS = "crop"
CONFIG = "src/config"


def load_upstream(pack: str):
    sys.path.insert(0, pack)
    spec = importlib.util.spec_from_file_location("linly_sadtalker", os.path.join(pack, "TFG", "SadTalker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SadTalker


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--image")
    ap.add_argument("--audio")
    ap.add_argument("--frames-out")
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(args.pack)
    work = os.path.join("work", uuid.uuid4().hex[:12])
    os.makedirs(work)
    try:
        _frames.emit(event="progress", phase="loading", percent=0)
        model = load_upstream(args.pack)(checkpoint_path=os.path.join(args.engine_dir, "checkpoints"), config_path=CONFIG)
        if args.check:
            _frames.emit(event="done", check=True)
            return 0
        image = os.path.join(work, "src" + (os.path.splitext(args.image)[1].lower() or ".png"))
        shutil.copyfile(args.image, image)
        wav = os.path.join(work, "in.wav")
        shutil.copyfile(args.audio, wav)
        _frames.emit(event="progress", phase="rendering", percent=0)
        video = model.test2(image, wav, preprocess=PREPROCESS, still_mode=False, use_enhancer=False, batch_size=1, size=SIZE, fps=args.fps,
                            result_dir=os.path.join(work, "out"))
        count, width, height = _frames.write_frames(video, args.frames_out, args.height)
        _frames.emit(event="done", frames=count, width=width, height=height, fps=args.fps)
        return 0
    except Exception as e:                                       # noqa: BLE001 (the engine reports it; nothing else can)
        _frames.emit(event="error", message=f"{type(e).__name__}: {e}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
