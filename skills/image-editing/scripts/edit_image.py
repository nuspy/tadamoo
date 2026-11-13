"""One picture edited from reference photographs, through TaDaMoo's media client (a heavy job: read the skill's heavy-swap notes first).

    python edit_image.py REF [REF ...] OUT.png --prompt "TEXT" [--width N] [--height N] [--steps N] [--seed N] [--negative TEXT] [--url http://127.0.0.1:7232]

The references are named image 1, image 2 in the prompt, in the order given here; the last path is the result.
"""
import argparse
import sys
from pathlib import Path

from serve import userdata
from tadamoo.media import client, limits
from tadamoo.media.settings import MediaSettings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="the reference photographs, then the result")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--negative", default="")
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    args = ap.parse_args()
    if len(args.paths) < 2:
        ap.error("give at least one reference and the result")
    *refs, out = args.paths
    request = limits.image_request(args.prompt, args.width, args.height, args.steps, args.seed, args.negative, refs)
    settings = MediaSettings(userdata.path("media.json")).read()
    got = client.run("image", {k: v for k, v in request.items() if k != "notes"}, Path(out), settings, url=args.url, progress=lambda e: None)
    print({**got, "seed": request["seed"], "adjusted": request["notes"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
