"""What the engine runners share: the events they print and the frames file they write.

A runner is started by an engine of voice/avatar_engines in the engine's own Python environment. It prints one JSON object per line on stdout
({"event": "progress"|"done"|"error", ...}) and writes the frames it made to a file: for each frame the header of avatar_client (b"AVI4", width,
height as little-endian uint32) followed by the I420 bytes. Both sides of that format are in this file and in pack.py."""
from __future__ import annotations

import json
import struct

MAGIC = b"AVI4"
HEADER = struct.Struct("<4sII")


def emit(**fields) -> None:
    print(json.dumps(fields), flush=True)


def even(n: int) -> int:
    return n - (n % 2)


def write_frames(video_path: str, out_path: str, height: int = 0) -> tuple[int, int, int]:
    """Decode `video_path` and write each frame to `out_path`, scaled down (never up) to `height` pixels when that is smaller than the frame.
    Returns (frames, width, height) of what was written."""
    import cv2
    cap = cv2.VideoCapture(video_path)
    count, size = 0, (0, 0)
    try:
        with open(out_path, "wb") as out:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                h, w = frame.shape[:2]
                if height and 0 < height < h:
                    w, h = max(2, round(w * height / h)), height
                w, h = max(2, even(w)), max(2, even(h))
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                out.write(HEADER.pack(MAGIC, w, h))
                out.write(cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes())
                count, size = count + 1, (w, h)
    finally:
        cap.release()
    if not count:
        raise RuntimeError("the video the engine made has no frames")
    return count, size[0], size[1]
