"""A better copy of a video, through TaDaMoo's media client: larger (FlashVSR+) and/or with more frames a second (RIFE). Heavy jobs: read the skill's heavy-swap notes first.

    python enhance.py VIDEO OUT.mp4 [--scale N] [--factor N] [--mode turbo|base] [--url http://127.0.0.1:7232]

The picture is enlarged first and the frames are made after, at the final size; each step is a job with its own lease and the work files of a step are kept next to OUT.mp4 until the end.
"""
import argparse
import shutil
import sys
from pathlib import Path

from serve import userdata
from tadamoo.media import client, limits
from tadamoo.media.settings import MediaSettings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("out")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--factor", type=int)
    ap.add_argument("--mode", default="turbo")
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    args = ap.parse_args()
    request = limits.enhance_request(args.video, args.scale, args.factor, args.mode)
    settings = MediaSettings(userdata.path("media.json")).read()
    out = Path(args.out)
    work = out.parent / (out.stem + ".work")
    got = client.enhance(request, work, settings, url=args.url, progress=lambda e: None)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(got["path"], str(out))
    shutil.rmtree(work, ignore_errors=True)
    print({"path": str(out), "seconds": got["seconds"], "steps": got["steps"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
