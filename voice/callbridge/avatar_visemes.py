"""Mouth sprites for the viseme mode of the phone and of the ESP32, made from one photo with no GPU and no model.

Geometry. The two mouth corners (poster pixels) give the mouth centre, its width `mw` and the roll of the head. The poster is cut in an
upright frame centred on the mouth, `FRAME_W_MOUTHS` x `FRAME_H_MOUTHS` mouth widths, turned by the roll so that the lip line is horizontal and
scaled down when it would pass `MAX_FRAME_W` pixels. Every viseme is that same frame with only the mouth changed:

  - the lower lip and chin drop by `open` mouth widths at the middle of the mouth and not at all at the corners (a parabola in x), the upper
    lip rises by `lift`; the pixels in between are redrawn as the inside of the mouth: dark, with the upper teeth (`teeth`) and the tongue
    (`tongue`) in proportion to the opening;
  - the mouth is narrowed or widened about its centre by `wide` (a pucker for O and U, a smile for I);
  - a closed mouth (PP) gets a darker lip line.

The PNG of a viseme is RGBA: the alpha is an ellipse around the mouth with a soft edge and the pixels outside it are the photo's own, so a
renderer that draws the sprite over the poster, with or without alpha, changes the mouth and nothing else. `visemes/frame.json` gives the frame:
`inv` is the 2x3 affine matrix that maps a sprite pixel to a poster pixel (the convention of wav2lip/box.json), so the sprite is drawn with
that transform over the poster. Made from the photo alone: a mouth shape taken from a real video of the person is not done."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .avatar_assets import FRAME_FILE, VISEMES_DIR as FOLDER
from .viseme_gen import VISEMI

log = logging.getLogger("callbridge.avatar_visemes")

FRAME_W_MOUTHS = 2.2
FRAME_H_MOUTHS = 1.7
MAX_FRAME_W = 384
MIN_MOUTH_PX = 6.0


@dataclass(frozen=True)
class Shape:
    """One mouth shape; lengths are in mouth widths."""
    open: float = 0.0       # how far the lower lip drops at the middle
    lift: float = 0.0       # how far the upper lip rises at the middle
    wide: float = 1.0       # horizontal scale of the mouth about its centre
    teeth: float = 0.0      # upper teeth: share of the opening they cover, 0 to 1
    tongue: float = 0.0     # tongue: how much of the opening it fills, 0 to 1
    press: bool = False     # a closed mouth with a darker lip line


SHAPES = {
    "sil": Shape(),
    "PP": Shape(wide=0.96, press=True),
    "FF": Shape(open=0.07, teeth=1.0),
    "TH": Shape(open=0.10, teeth=0.8, tongue=0.6),
    "DD": Shape(open=0.14, teeth=0.7, tongue=0.5),
    "kk": Shape(open=0.18, wide=0.98, teeth=0.4, tongue=0.7),
    "CH": Shape(open=0.14, wide=0.78, teeth=0.6),
    "SS": Shape(open=0.07, wide=1.08, teeth=1.0),
    "nn": Shape(open=0.10, teeth=0.6, tongue=0.4),
    "RR": Shape(open=0.14, wide=0.85, teeth=0.4, tongue=0.4),
    "aa": Shape(open=0.42, lift=0.06, wide=1.04, teeth=0.5, tongue=0.6),
    "E": Shape(open=0.26, wide=1.08, teeth=0.8, tongue=0.4),
    "I": Shape(open=0.12, wide=1.14, teeth=1.0, tongue=0.2),
    "O": Shape(open=0.30, wide=0.70, teeth=0.2, tongue=0.4),
    "U": Shape(open=0.18, wide=0.56, tongue=0.3),
}
assert set(SHAPES) == set(VISEMI)

_TEETH = np.array([232, 236, 238], np.float32)
_TONGUE = np.array([90, 100, 200], np.float32)


def frame_of(img: np.ndarray, p1, p2) -> tuple[np.ndarray, dict]:
    """The upright mouth frame cut from `img` and its description (size, `inv`, mouth centre and width in sprite pixels, roll)."""
    p1, p2 = np.array(p1, dtype=float), np.array(p2, dtype=float)
    d = p2 - p1
    mw = float(np.hypot(*d))
    if mw < MIN_MOUTH_PX:
        raise ValueError("the mouth is too small in the photo to make mouth shapes from it")
    roll = math.degrees(math.atan2(d[1], d[0]))
    mc = (p1 + p2) / 2.0
    scale = min(1.0, MAX_FRAME_W / (FRAME_W_MOUTHS * mw))
    fw, fh = int(round(FRAME_W_MOUTHS * mw * scale)), int(round(FRAME_H_MOUTHS * mw * scale))
    m = cv2.getRotationMatrix2D((float(mc[0]), float(mc[1])), roll, scale)
    m[:, 2] += (fw / 2.0 - mc[0], fh / 2.0 - mc[1])
    crop = cv2.warpAffine(img, m, (fw, fh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    inv = cv2.invertAffineTransform(m)
    frame = {"ver": 1, "size": [fw, fh], "inv": [round(float(v), 6) for v in inv.reshape(-1)], "poster_w": int(img.shape[1]),
             "poster_h": int(img.shape[0]), "mouth": {"cx": fw / 2.0, "cy": fh / 2.0, "w": round(mw * scale, 2)}, "angle_deg": round(roll, 2),
             "alpha": True}
    return crop, frame


def _lip_colour(crop: np.ndarray, cx: float, cy: float, mw: float) -> np.ndarray:
    h = max(1, int(mw * 0.04))
    x0, x1 = int(cx - mw * 0.3), int(cx + mw * 0.3)
    band = crop[max(0, int(cy) - h):int(cy) + h + 1, max(0, x0):max(x1, x0 + 1)]
    return np.median(band.reshape(-1, 3), axis=0).astype(np.float32) if band.size else np.array([90, 90, 160], np.float32)


def _smooth(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def render(crop: np.ndarray, cx: float, cy: float, mw: float, shape: Shape) -> np.ndarray:
    """The BGRA sprite of `shape` on the upright frame `crop`, the mouth centred at (cx, cy) and `mw` pixels wide."""
    h, w = crop.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = xs - cx, ys - cy
    half = mw / 2.0
    reach = max(half * shape.wide * 0.92, 1.0)
    profile = np.clip(1.0 - (dx / reach) ** 2, 0.0, 1.0) ** 0.8
    drop, lift = shape.open * mw * profile, shape.lift * mw * profile
    fall = _smooth(1.0 - np.abs(dy) / (0.9 * mw))
    band = np.exp(-((dy / (0.5 * mw)) ** 2))
    src_x = cx + dx / (1.0 + (shape.wide - 1.0) * band)
    src_y = np.where(dy > 0, ys - drop * fall, ys + lift * fall)
    moved = cv2.remap(crop, src_x.astype(np.float32), src_y.astype(np.float32), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE).astype(np.float32)

    lip = _lip_colour(crop, cx, cy, mw)
    inside = np.clip(np.minimum(drop - dy, dy + lift), 0.0, 1.0)[..., None]
    depth = np.clip((dy + lift) / np.maximum(drop + lift, 1e-3), 0.0, 1.0)[..., None]
    cavity = np.maximum(lip * 0.22, np.array([14, 12, 32], np.float32)) + np.zeros_like(moved)
    cavity = np.where(depth < shape.teeth * 0.5, _TEETH * 0.96, cavity)
    in_tongue = (depth > 1.0 - shape.tongue * 0.6) & (np.abs(dx)[..., None] < reach * 0.7)
    cavity = np.where(in_tongue, _TONGUE * 0.5 + lip * 0.5, cavity)
    out = moved * (1.0 - inside) + cavity * inside
    if shape.press:
        line = ((np.abs(dy) <= max(1.0, 0.016 * mw) / 2) & (np.abs(dx) <= half * shape.wide))[..., None]
        out = np.where(line, out * 0.3 + lip * 0.2, out)

    rx, ry, oy = half * 1.6, mw * (0.38 + shape.open * 0.75), shape.open * mw * 0.4
    radius = np.sqrt((dx / rx) ** 2 + ((dy - oy) / ry) ** 2)
    alpha = np.clip((1.0 - radius) / 0.25, 0.0, 1.0)
    rgb = np.where(alpha[..., None] > 0, out, crop.astype(np.float32))
    return np.dstack([np.clip(rgb, 0, 255), alpha * 255.0]).astype(np.uint8)


def write_sprites(img: np.ndarray, p1, p2, dest: Path) -> dict:
    """Writes `dest/visemes/<V>.png` for every viseme and `frame.json`; returns the frame. `p1`, `p2` are the mouth corners in poster pixels."""
    crop, frame = frame_of(img, p1, p2)
    out = dest / FOLDER
    out.mkdir(parents=True, exist_ok=True)
    cx, cy, mw = frame["mouth"]["cx"], frame["mouth"]["cy"], frame["mouth"]["w"]
    for name in VISEMI:
        if not cv2.imwrite(str(out / f"{name}.png"), render(crop, cx, cy, mw, SHAPES[name])):
            raise OSError(f"the sprite {name} could not be written")
    frame["visemes"] = list(VISEMI)
    (out / FRAME_FILE).write_text(json.dumps(frame), encoding="utf-8")
    log.info("avatar_visemes: %d sprites of %dx%d in %s", len(VISEMI), frame["size"][0], frame["size"][1], out)
    return frame


def sheet_png(folder: Path, columns: int = 5) -> bytes | None:
    """One PNG with every sprite drawn over the photo's own frame, labelled, for a person to look at; None when the folder has no sprites."""
    out = folder / FOLDER
    tiles = []
    base = cv2.imread(str(out / "sil.png"), cv2.IMREAD_UNCHANGED)
    if base is None:
        return None
    for name in VISEMI:
        sprite = cv2.imread(str(out / f"{name}.png"), cv2.IMREAD_UNCHANGED)
        if sprite is None or sprite.shape != base.shape:
            return None
        alpha = sprite[..., 3:4].astype(np.float32) / 255.0
        tile = (sprite[..., :3] * alpha + base[..., :3] * (1.0 - alpha)).astype(np.uint8)
        cv2.putText(tile, name, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(tile, name, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
        tiles.append(tile)
    while len(tiles) % columns:
        tiles.append(np.zeros_like(tiles[0]))
    sheet = np.vstack([np.hstack(tiles[i:i + columns]) for i in range(0, len(tiles), columns)])
    ok, data = cv2.imencode(".png", sheet)
    return data.tobytes() if ok else None
