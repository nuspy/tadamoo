"""One picture from a description, through TaDaMoo's media client (a heavy job: read the skill's heavy-swap notes first).

    python make_image.py "PROMPT" OUT.png [--width N] [--height N] [--steps N] [--seed N] [--negative TEXT] [--url http://127.0.0.1:7232]
"""
import argparse
import sys
from pathlib import Path

from serve import userdata
from tadamoo.media import client, limits
from tadamoo.media.settings import MediaSettings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("out")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--negative", default="")
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    args = ap.parse_args()
    request = limits.image_request(args.prompt, args.width, args.height, args.steps, args.seed, args.negative)
    settings = MediaSettings(userdata.path("media.json")).read()
    got = client.run("image", {k: v for k, v in request.items() if k != "notes"}, Path(args.out), settings, url=args.url, progress=lambda e: None)
    print({**got, "seed": request["seed"], "adjusted": request["notes"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
