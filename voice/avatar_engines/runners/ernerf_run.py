"""Renders speech with a trained ER-NeRF through Linly-Talker's NeRFTalk wrapper and writes the frames (see _frames.py).

Started by voice/avatar_engines/ernerf.py in the engine environment with the code pack as its working directory. `--check` imports what the
wrapper needs (PyTorch3D and the four compiled CUDA modules) and stops, so a missing or mismatched build is named."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import shutil
import sys
import uuid

import _frames

COMPILED = ("freqencoder", "gridencoder", "raymarching", "shencoder")


def check(pack: str) -> int:
    sys.path.insert(0, os.path.join(pack, "NeRF"))
    found, missing = {}, {}
    for name in ("pytorch3d",) + COMPILED:
        try:
            module = importlib.import_module(name)
            found[name] = str(getattr(module, "__version__", "ok"))
        except Exception as e:                                   # noqa: BLE001 (each one is named, whatever it raised)
            missing[name] = f"{type(e).__name__}: {e}"
    if missing:
        _frames.emit(event="error", message="not importable: " + "; ".join(f"{k} ({v})" for k, v in missing.items()))
        return 1
    _frames.emit(event="done", check=True, modules=found)
    return 0


def load_upstream(pack: str):
    sys.path.insert(0, pack)
    spec = importlib.util.spec_from_file_location("linly_nerftalk", os.path.join(pack, "TFG", "NeRFTalk.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--checkpoint")
    ap.add_argument("--pose")
    ap.add_argument("--asr-model", default="deepspeech")
    ap.add_argument("--audio")
    ap.add_argument("--frames-out")
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(args.pack)
    if args.check:
        return check(args.pack)
    work = os.path.join("work", uuid.uuid4().hex[:12])
    os.makedirs(work)
    try:
        _frames.emit(event="progress", phase="loading", percent=0)
        upstream = load_upstream(args.pack)
        upstream.opt.workspace = work
        upstream.opt.asr_model = args.asr_model
        wav = os.path.join(work, "in.wav")
        shutil.copyfile(args.audio, wav)
        nerf = upstream.NeRFTalk()
        nerf.init_model(args.checkpoint, args.pose)
        _frames.emit(event="progress", phase="rendering", percent=0)
        video = nerf.predict(wav)
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
