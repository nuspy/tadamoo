"""Shared stand-ins for the avatar tests: a models folder with every MuseTalk weight as an empty file, a config that points at the fake worker,
tiny photos and videos made with OpenCV, and a client wrapper that always stops its worker."""
import sys
from pathlib import Path

from callbridge import avatar_weights
from callbridge.config import Config

FAKE_WORKER = Path(__file__).resolve().parent / "fake_avatar_worker.py"


def put_weights(models: Path, skip=()):
    """Every weight file the worker needs, empty, except those named in `skip`."""
    where = models / "avatar" / avatar_weights.MODEL_ID
    for rel in avatar_weights.REQUIRED:
        if rel in skip:
            continue
        target = where / Path(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return where


def make_config(tmp_path: Path, **over) -> Config:
    cfg = Config()
    cfg.avatars_dir = str(tmp_path / "user" / "voice" / "avatars")
    cfg.avatar_python = sys.executable
    cfg.avatar_worker = str(FAKE_WORKER)
    cfg.avatar_start_timeout_s = 20.0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def photo_bytes(side=200, ext=".jpg") -> bytes:
    import cv2
    import numpy as np
    img = np.full((side, side, 3), 180, np.uint8)
    cv2.circle(img, (side // 2, side // 2), side // 3, (120, 150, 200), -1)
    ok, data = cv2.imencode(ext, img)
    assert ok
    return data.tobytes()


def video_file(path: Path, frames=12, side=160) -> Path:
    import cv2
    import numpy as np
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (side, side))
    for i in range(frames):
        out.write(np.full((side, side, 3), (i * 15) % 255, np.uint8))
    out.release()
    assert path.stat().st_size > 0
    return path


def wav_bytes(seconds=4.0, rate=16000) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(seconds * rate))
    return buf.getvalue()
