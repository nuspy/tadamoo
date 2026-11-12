"""Reads the text that is inside images with RapidOCR and prints it as JSON lines on stdout. Run by the Python of a Docling environment (it brings RapidOCR and
torch), never imported.

    python ocr_worker.py IMAGE [IMAGE ...]

Lines:  {"image": PATH, "lines": [{"text", "score", "box": [x0, y0, x1, y1]}]}  one for each image, in order (an image that cannot be read has "lines": [] and an "error");
        {"event": "done"} at the end, or {"event": "error", "message"} when the engine cannot start. The boxes are in pixels of the image. It runs on the CPU: the labels
of a figure are a few lines of text, and the GPU is not asked for."""
from __future__ import annotations

import json
import logging
import sys


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list) -> int:
    logging.disable(logging.CRITICAL)
    try:
        from rapidocr import EngineType, RapidOCR
        engine = RapidOCR(params={"Det.engine_type": EngineType.TORCH, "Cls.engine_type": EngineType.TORCH, "Rec.engine_type": EngineType.TORCH,
                                  "Global.log_level": "critical"})
    except Exception as exc:
        emit({"event": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return 1
    for path in argv:
        try:
            got = engine(path)
            lines = []
            if got is not None and getattr(got, "txts", None):
                for text, score, box in zip(got.txts, got.scores, got.boxes):
                    xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
                    lines.append({"text": str(text), "score": round(float(score), 3), "box": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]})
            emit({"image": path, "lines": lines})
        except Exception as exc:
            emit({"image": path, "lines": [], "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    emit({"event": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
