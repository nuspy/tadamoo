"""Numbers for a clip: the face sharpness of `tests/motion_matrix.py`, the sharpness of three face regions normalised to 256 px wide, and PSNR / SSIM against a reference clip.

    python metrics.py [--ref REF.mp4] [--out FILE.json] CLIP.mp4 [CLIP.mp4 ...]

face_sharpness: the method of motion_matrix.sharpness (mean over the frames of the variance of the Laplacian of the face box MediaPipe finds, the picture scaled to 832 wide).
regions: for each frame the face landmarks are found on the 832-wide picture; eyes, centre of the face (nose and mouth) and hair (a strip above the forehead) are cut from the picture at its own
resolution, each scaled to 256 px wide, and the variance of the Laplacian is averaged over the frames. A bigger picture shows more real detail, and also more invented detail or artefacts: the number says
how much fine contrast there is, not whether it is right.
psnr_db / ssim: mean over the frames against the reference of the same size (the reference is scaled to the clip's size when they differ)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EYES = (33, 133, 159, 145, 362, 263, 386, 374, 70, 300)           # both eyes and the brows
CENTRE = (1, 2, 98, 327, 61, 291, 0, 17)                          # nose and mouth
OVAL_TOP, CHIN = 10, 152


def lap_var(img, width: int = 256) -> float:
    import cv2
    h, w = img.shape[:2]
    if w < 4 or h < 4:
        return float("nan")
    img = cv2.resize(img, (width, max(4, round(h * width / w))), interpolation=cv2.INTER_AREA if w > width else cv2.INTER_CUBIC)
    return float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def face_sharpness(path: str) -> float:
    """Same as tests/motion_matrix.sharpness."""
    import cv2
    from tadamoo.videoedit import qa
    track = qa.face_track(str(path), 1, 832)
    values = []
    for (_, _, img), box in zip(qa.frames(str(path), 832), track.box):
        if box is None:
            continue
        x0, y0, x1, y1 = box
        crop = img[max(0, y0):max(1, y1), max(0, x0):max(1, x1)]
        values.append(float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
    return round(sum(values) / len(values), 1) if values else 0.0


def regions(path: str) -> dict:
    from tadamoo.videoedit import qa
    lm = qa._landmarker("face")
    acc = {"eyes": [], "centre": [], "hair": []}
    small = qa.frames(str(path), 832)
    for (i, _, img_small), (_, _, full) in zip(small, qa.frames(str(path))):
        res = lm.detect(qa._mp_image(img_small))
        if not res.face_landmarks:
            continue
        pts = np.array([(p.x, p.y) for p in res.face_landmarks[0]])
        H, W = full.shape[:2]

        def cut(idx, pad_x, pad_y, top=None):
            xs, ys = pts[list(idx), 0], pts[list(idx), 1]
            x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
            w, h = x1 - x0, y1 - y0
            x0, x1, y0, y1 = x0 - pad_x * w, x1 + pad_x * w, y0 - pad_y * h, y1 + pad_y * h
            if top is not None:
                y0, y1 = top
            a, b, c, d = int(max(0, x0 * W)), int(min(W, x1 * W)), int(max(0, y0 * H)), int(min(H, y1 * H))
            return full[c:d, a:b]
        face_h = abs(pts[CHIN, 1] - pts[OVAL_TOP, 1])
        oval = pts[[10, 234, 454, 152], 0]
        acc["eyes"].append(lap_var(cut(EYES, 0.15, 0.6)))
        acc["centre"].append(lap_var(cut(CENTRE, 0.2, 0.2)))
        top = pts[OVAL_TOP, 1]
        hair = cut((10, 234, 454), 0.0, 0.0, top=(max(0.0, top - 0.30 * face_h), top + 0.02 * face_h))
        acc["hair"].append(lap_var(hair))
    lm.close()
    return {k: (round(float(np.nanmean(v)), 1) if v else None) for k, v in acc.items()}


def against(path: str, ref: str) -> dict:
    import cv2
    from skimage.metrics import peak_signal_noise_ratio, structural_similarity
    a, b = cv2.VideoCapture(str(path)), cv2.VideoCapture(str(ref))
    psnr, ssim = [], []
    while True:
        ok1, f1 = a.read()
        ok2, f2 = b.read()
        if not (ok1 and ok2):
            break
        if f2.shape != f1.shape:
            f2 = cv2.resize(f2, (f1.shape[1], f1.shape[0]), interpolation=cv2.INTER_CUBIC)
        psnr.append(peak_signal_noise_ratio(f2, f1, data_range=255))
        ssim.append(structural_similarity(cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY), cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY), data_range=255))
    return {"psnr_db": round(float(np.mean(psnr)), 2), "psnr_min_db": round(float(np.min(psnr)), 2), "ssim": round(float(np.mean(ssim)), 4), "frames_compared": len(psnr)} if psnr else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref")
    ap.add_argument("--out")
    ap.add_argument("--no-regions", action="store_true")
    ap.add_argument("clips", nargs="+")
    args = ap.parse_args()
    out = {}
    for clip in args.clips:
        row = {"face_sharpness": face_sharpness(clip)}
        if not args.no_regions:
            row["regions"] = regions(clip)
        if args.ref and Path(clip).resolve() != Path(args.ref).resolve():
            row.update(against(clip, args.ref))
        out[clip] = row
        print(clip, json.dumps(row), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
