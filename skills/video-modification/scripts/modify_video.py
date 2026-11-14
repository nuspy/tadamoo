"""A video changed in what it shows, through TaDaMoo's media client (a heavy job: read the skill's heavy-swap notes first).

    python modify_video.py VIDEO OUT.mp4 --mode replace --target "yellow Fiat 500" --prompt "a red Ferrari drives along the street" [--reference ferrari.png]
    python modify_video.py VIDEO OUT.mp4 --mode edit --prompt "the same street at night"
    python modify_video.py VIDEO OUT.mp4 --mode restyle --reference look.png [--prompt TEXT]
                           [--strength N] [--guide N] [--grow N] [--seed N] [--url http://127.0.0.1:7232]

The original is not changed; the result is a new file with the sound of the original.
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

from serve import userdata
from tadamoo.media import client, limits
from tadamoo.media.settings import MediaSettings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("output")
    ap.add_argument("--mode", default="replace", choices=limits.MODIFY["modes"])
    ap.add_argument("--prompt", default="")
    ap.add_argument("--target", default="", help="replace: what to swap, in a few English words")
    ap.add_argument("--reference", default="", help="replace: a picture of the new thing; restyle: the picture whose look is taken")
    ap.add_argument("--strength", type=float)
    ap.add_argument("--guide", type=float)
    ap.add_argument("--grow", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    args = ap.parse_args()
    request = limits.modify_request(str(Path(args.video).resolve()), args.prompt, args.mode, args.target, str(Path(args.reference).resolve()) if args.reference else "", args.strength, args.guide, args.grow, args.seed)
    settings = MediaSettings(userdata.path("media.json")).read()
    out = Path(args.output)
    job = out.parent / f".modify-{int(time.time())}"                 # the job's own files (a copy of the video, the tracking) are made here and removed
    got = client.run("modify", {k: v for k, v in request.items() if k != "notes"}, job / out.name, settings, url=args.url, progress=lambda e: None)
    shutil.move(got["path"], str(out))
    shutil.rmtree(job, ignore_errors=True)
    print({**got, "path": str(out), "seed": request["seed"], "mode": request["mode"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
