"""The production enlargement job (`client.run("upscale", ...)`, the worker's plan and `flashvsr_launch.py`) on real clips: frames, duration, size and sound in and out.

    python enhance_check.py --out FOLDER CLIP.mp4 [CLIP.mp4 ...]

Each clip is enlarged 2x Turbo through the same call the newscast and the tool `enhance_video` make, under a stand-in broker on a port of its own (nothing of the running app is used). The result is
counted frame by frame (ffprobe -count_frames), with the duration of the picture and of the sound; result.json in FOLDER."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def streams(path: Path) -> dict:
    out = json.loads(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames,duration,width,height,r_frame_rate,sample_rate", "-of", "json", str(path)],
                                    capture_output=True, text=True).stdout)
    res = {}
    for s in out["streams"]:
        res[s["codec_type"]] = {k: s[k] for k in ("nb_read_frames", "duration", "width", "height", "r_frame_rate", "sample_rate") if k in s}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("clips", nargs="+")
    args = ap.parse_args()
    from media_fake import Broker
    from serve.userdata import user_data_dir
    from tadamoo.media import client
    from tadamoo.media.settings import MediaSettings
    media = MediaSettings(user_data_dir() / "media.json").read()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = json.loads((out / "result.json").read_text(encoding="utf-8")) if (out / "result.json").is_file() else {}
    broker = Broker()
    try:
        for clip in args.clips:
            clip = Path(clip)
            target = out / f"{clip.parent.parent.name}_{clip.parent.name}_{clip.stem}_enh.mp4"
            began = time.monotonic()
            got = client.run("upscale", {"video": str(clip), "scale": 2.0, "mode": "turbo"}, target, media, url=broker.url, held=True,
                             progress=lambda e: print("  ", e.get("line") or e.get("event"), flush=True) if e.get("event") == "log" and "[launch]" in (e.get("line") or "") else None)
            before, after = streams(clip), streams(target)
            results[str(clip)] = {"in": before, "out": after, "job_seconds": got["work_seconds"], "wall_seconds": round(time.monotonic() - began, 1),
                                  "same_frames": before["video"]["nb_read_frames"] == after["video"]["nb_read_frames"]}
            print(clip.name, json.dumps(results[str(clip)]), flush=True)
    finally:
        broker.stop()
    (out / "result.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
