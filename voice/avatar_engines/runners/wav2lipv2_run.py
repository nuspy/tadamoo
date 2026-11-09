"""Runs Linly-Talker's Wav2Lipv2 on one photo and one audio file and writes the frames (see _frames.py).

Started by voice/avatar_engines/wav2lipv2.py in the engine environment with the code pack as its working directory; it loads nothing from the
network. `--check` loads the models and stops."""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import uuid

import _frames

BATCH = 16


def load_upstream(pack: str):
    sys.path.insert(0, pack)
    spec = importlib.util.spec_from_file_location("linly_wav2lipv2", os.path.join(pack, "TFG", "Wav2Lipv2.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Wav2Lipv2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--image")
    ap.add_argument("--audio")
    ap.add_argument("--frames-out")
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(args.pack)
    # The upstream script reads the type of its input from the text after the first dot of the path, so its files sit in a path with no other dot.
    work = os.path.join("work", uuid.uuid4().hex[:12])
    os.makedirs(work)
    try:
        _frames.emit(event="progress", phase="loading", percent=0)
        model = load_upstream(args.pack)(checkpoint_path=os.path.join(args.engine_dir, "checkpoints", "wav2lipv2.pth"),
                                         pretrained_model_dir=os.path.join(args.engine_dir, "models"))
        if args.check:
            _frames.emit(event="done", check=True)
            return 0
        import cv2
        image = os.path.join(work, "src.jpg")
        cv2.imwrite(image, cv2.imread(args.image))
        wav = os.path.join(work, "in.wav")
        shutil.copyfile(args.audio, wav)
        out = os.path.join(work, "out.mp4")
        _frames.emit(event="progress", phase="rendering", percent=0)
        model.run(image, wav, batch_size=BATCH, enhance=False, outfile=out, fps=args.fps)
        count, width, height = _frames.write_frames(out, args.frames_out, args.height)
        _frames.emit(event="done", frames=count, width=width, height=height, fps=args.fps)
        return 0
    except Exception as e:                                       # noqa: BLE001 (the engine reports it; nothing else can)
        _frames.emit(event="error", message=f"{type(e).__name__}: {e}")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for name in ("tempface.wav", "tempface.mp4"):             # the upstream script leaves these in its working directory
            try:
                os.remove(name)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
