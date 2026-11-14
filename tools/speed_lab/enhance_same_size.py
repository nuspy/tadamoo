"""The same-size enhancement of the approved clip and of the Lilith clip: (a) FlashVSR+ at 2x then brought back to the input size, (b) FlashVSR+ at 1x (its native same-size mode).

    python enhance_same_size.py --out FOLDER CLIP.mp4 [CLIP.mp4 ...]

Each variant runs through the production job (`client.run("upscale", ...)`, `scale` 2.0 for (a) and 1.0 for (b)) under a stand-in broker; the seconds, the size, the frames and the duration are read back
for each, and the lossless working file of (a) is kept (FLASHVSR_KEEP_WORKING) so that the ways of scaling it down can be compared. result.json in FOLDER."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("clips", nargs="+")
    args = ap.parse_args()
    from enhance_check import streams
    from media_fake import Broker
    from serve.userdata import user_data_dir
    from tadamoo.media import client
    from tadamoo.media.settings import MediaSettings
    os.environ["FLASHVSR_KEEP_WORKING"] = "1"
    media = MediaSettings(user_data_dir() / "media.json").read()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = json.loads((out / "result.json").read_text(encoding="utf-8")) if (out / "result.json").is_file() else {}
    broker = Broker()
    try:
        for clip in map(Path, args.clips):
            name = clip.parent.name if clip.name == "video.mp4" else clip.stem
            for label, scale in (("a_x2_fit", 2.0), ("b_native_1x", 1.0)):
                target = out / f"{name}_{label}.mp4"
                began = time.monotonic()
                got = client.run("upscale", {"video": str(clip), "scale": scale, "mode": "turbo"}, target, media, url=broker.url, held=True)
                results[f"{name}/{label}"] = {"seconds": got["work_seconds"], "wall": round(time.monotonic() - began, 1), "in": streams(clip), "out": streams(target)}
                print(name, label, json.dumps(results[f"{name}/{label}"]), flush=True)
    finally:
        broker.stop()
    (out / "result.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
