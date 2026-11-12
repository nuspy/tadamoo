"""Measuring a finished picture: where it jumps (seams) and how the presenter moves (mouth opening, head, shoulders). Objective numbers for a video that a person will judge by eye.

`seams(path)` finds every transition between two frames that is not a smooth move of the picture: a jump of the face (the nose tip, in face heights), of the face's pixels, of the
whole picture's brightness histogram or of its colour. `classify` then says, for each one, whether it is a designed cut (the plan says so), whether it falls on a graphic event
(a card sliding in, a bar, a window: where nothing under the graphic may change) or whether it is accidental. `mouth(path)` gives the opening of the inner lips as a share of the face
height, for each frame, with its percentiles, and `motion(path)` the head and shoulder speeds; the same measures on a reference video give the amplitude to aim at.

Face and body points come from MediaPipe's landmarkers, whose model files are looked for in `MODELS` (outside the repository)."""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MODELS = Path(r"E:\Modelli\basalt\qa")
NOSE, LIP_UP, LIP_DOWN, LIP_LEFT, LIP_RIGHT, FOREHEAD, CHIN, EYE_L, EYE_R = 1, 13, 14, 78, 308, 10, 152, 33, 263
SHOULDER_L, SHOULDER_R = 11, 12


def frames(path: str, width: int = 0):
    """(index, time_s, BGR image) for each frame of the video; `width` scales them down."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    i = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if width and img.shape[1] != width:
            img = cv2.resize(img, (width, round(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
        yield i, i / fps, img
        i += 1
    cap.release()


def fps_of(path: str) -> float:
    import cv2
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    cap.release()
    return fps


def _landmarker(kind: str = "face"):
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
    if kind == "face":
        opts = vision.FaceLandmarkerOptions(base_options=mpp.BaseOptions(model_asset_path=str(MODELS / "face_landmarker.task")), running_mode=vision.RunningMode.IMAGE, num_faces=1)
        return vision.FaceLandmarker.create_from_options(opts)
    opts = vision.PoseLandmarkerOptions(base_options=mpp.BaseOptions(model_asset_path=str(MODELS / "pose_landmarker.task")), running_mode=vision.RunningMode.IMAGE)
    return vision.PoseLandmarker.create_from_options(opts)


def _mp_image(bgr):
    import cv2
    import mediapipe as mp
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def pct(values: Sequence[float], ps: Sequence[int] = (10, 50, 75, 90, 95, 99)) -> Dict[str, float]:
    vals = sorted(v for v in values if v == v)
    if not vals:
        return {}
    out = {}
    for p in ps:
        k = (len(vals) - 1) * p / 100
        lo, hi = math.floor(k), math.ceil(k)
        out[f"p{p}"] = round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 4)
    out["max"] = round(vals[-1], 4)
    out["mean"] = round(statistics.fmean(vals), 4)
    return out


@dataclass
class FaceTrack:
    fps: float
    width: int = 0                                                             # the width of the frames the points are in
    times: List[float] = field(default_factory=list)
    nose: List[Optional[Tuple[float, float]]] = field(default_factory=list)      # pixels
    height: List[float] = field(default_factory=list)                          # face height in pixels (forehead to chin)
    aperture: List[float] = field(default_factory=list)                        # inner lips, share of face height
    width_ratio: List[float] = field(default_factory=list)                     # mouth width, share of the distance between the eye corners
    roll: List[float] = field(default_factory=list)                            # degrees, from the eye line
    box: List[Optional[Tuple[int, int, int, int]]] = field(default_factory=list)


def face_track(path: str, step: int = 1, width: int = 0) -> FaceTrack:
    track = FaceTrack(fps=fps_of(path))
    lm = _landmarker("face")
    for i, t, img in frames(path, width):
        if i % step:
            continue
        h, w = img.shape[:2]
        track.width = w
        res = lm.detect(_mp_image(img))
        track.times.append(t)
        if not res.face_landmarks:
            track.nose.append(None); track.height.append(float("nan")); track.aperture.append(float("nan")); track.width_ratio.append(float("nan")); track.roll.append(float("nan")); track.box.append(None)
            continue
        pts = [(p.x * w, p.y * h) for p in res.face_landmarks[0]]
        fh = math.dist(pts[FOREHEAD], pts[CHIN])
        track.nose.append(pts[NOSE])
        track.height.append(fh)
        track.aperture.append(math.dist(pts[LIP_UP], pts[LIP_DOWN]) / fh)
        track.width_ratio.append(math.dist(pts[LIP_LEFT], pts[LIP_RIGHT]) / max(1e-6, math.dist(pts[EYE_L], pts[EYE_R])))
        dx, dy = pts[EYE_R][0] - pts[EYE_L][0], pts[EYE_R][1] - pts[EYE_L][1]
        track.roll.append(math.degrees(math.atan2(dy, dx)))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        track.box.append((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))))
    lm.close()
    return track


def mouth(track: FaceTrack) -> Dict[str, Any]:
    """The mouth opening over the frames: percentiles of the inner-lip distance as a share of the face height, and of its change from frame to frame. Frames at rest (a closed mouth)
    pull the low percentiles down; the high ones (p90, p99) are the width of the movement while speaking."""
    ap = [a for a in track.aperture if a == a]
    speaking = [a for a in ap if a > 0.012]
    out: Dict[str, Any] = {"frames": len(ap), "aperture": pct(ap), "aperture_while_open": pct(speaking), "mouth_width_ratio": pct([w for w in track.width_ratio if w == w])}
    d = [abs(b - a) * track.fps for a, b in zip(track.aperture, track.aperture[1:]) if a == a and b == b]
    out["aperture_speed_per_s"] = pct(d)
    return out


def head_motion(track: FaceTrack) -> Dict[str, Any]:
    """Head speed (nose tip, in face heights per second), its roll and the roll's speed. A camera move shows up here too: measure the raw take for the presenter alone."""
    speeds, rolls = [], []
    prev = None
    for n, h, r in zip(track.nose, track.height, track.roll):
        if n is not None and prev is not None and prev[0] is not None and h == h:
            speeds.append(math.dist(n, prev[0]) / prev[1] * track.fps)
        prev = (n, h) if n is not None else prev
        if r == r:
            rolls.append(r)
    roll_speed = [abs(b - a) * track.fps for a, b in zip(rolls, rolls[1:])]
    return {"speed_faceheights_per_s": pct(speeds), "rms_speed": round(math.sqrt(statistics.fmean(s * s for s in speeds)), 4) if speeds else None,
            "roll_deg": pct(rolls, (5, 50, 95)), "roll_speed_deg_per_s": pct(roll_speed), "roll_std_deg": round(statistics.pstdev(rolls), 3) if len(rolls) > 1 else None}


def body_motion(path: str, step: int = 1, width: int = 0) -> Dict[str, Any]:
    """Shoulder speed from the pose landmarker (in shoulder widths per second) and the motion energy below the chin from the optical flow (pixels per frame, share of the picture)."""
    import cv2
    import numpy as np
    pose = _landmarker("pose")
    prev_gray, flows, shoulders = None, [], []
    last = None
    fps = fps_of(path)
    for i, t, img in frames(path, width):
        if i % step:
            continue
        res = pose.detect(_mp_image(img))
        h, w = img.shape[:2]
        gray = cv2.cvtColor(cv2.resize(img, (w // 2, h // 2)), cv2.COLOR_BGR2GRAY)
        if res.pose_landmarks:
            p = res.pose_landmarks[0]
            ls, rs = (p[SHOULDER_L].x * w, p[SHOULDER_L].y * h), (p[SHOULDER_R].x * w, p[SHOULDER_R].y * h)
            span = max(1.0, math.dist(ls, rs))
            mid = ((ls[0] + rs[0]) / 2, (ls[1] + rs[1]) / 2)
            if last is not None:
                shoulders.append(math.dist(mid, last[0]) / last[1] * fps / step)
            last = (mid, span)
        if prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            mag = np.linalg.norm(flow, axis=2)
            flows.append(float(mag[mag.shape[0] // 2:].mean()))          # the lower half of the picture: shoulders, arms, hands
        prev_gray = gray
    pose.close()
    return {"shoulder_speed_widths_per_s": pct(shoulders), "lower_half_flow_px_per_frame": pct(flows)}


# ---- seams
@dataclass
class Seam:
    time_s: float
    frame: int
    face_jump: float                  # nose tip move in face heights, beyond the usual move of the neighbouring frames
    face_diff: float                  # the face region's pixel change (0..255)
    hist: float                       # change of the whole picture's brightness histogram (0..1)
    color: float                      # change of the mean colour of the whole picture (0..255)
    kind: str = "accidental"          # designed | graphic | accidental
    note: str = ""


def _robust_limit(values: Sequence[float], k: float, floor: float) -> float:
    vals = [v for v in values if v == v]
    if not vals:
        return floor
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals) * 1.4826
    return max(floor, med + k * max(mad, 1e-9))


def seams(path: str, track: Optional[FaceTrack] = None, k: float = 8.0, face_floor: float = 0.08, diff_floor: float = 10.0, hist_floor: float = 0.10, color_floor: float = 6.0) -> List[Seam]:
    """Every transition that is a jump and not a move: judged from the face (a nose tip that moves several tenths of a face height between two frames, a face region whose pixels
    change at once) or from the whole picture (the brightness histogram or the mean colour jumps). Limits are the larger of a floor and `k` robust deviations above the video's
    own usual change, so a video that moves a lot is judged against its own movement."""
    import cv2
    import numpy as np
    track = track or face_track(path)
    prev, rows = None, []
    for i, t, img in frames(path, 192):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [64], [0, 256]).ravel()
        hist = hist / hist.sum()
        mean = img.reshape(-1, 3).mean(axis=0)
        if prev is not None:
            fd = float("nan")
            box = track.box[i] if i < len(track.box) else None
            if box is not None:
                sx = img.shape[1] / (track.width or img.shape[1])
                x0, y0, x1, y1 = (int(v * sx) for v in box)
                x0, y0, x1, y1 = max(0, x0), max(0, y0), min(img.shape[1], x1), min(img.shape[0], y1)
                if x1 - x0 > 8 and y1 - y0 > 8:
                    fd = float(np.abs(gray[y0:y1, x0:x1].astype(np.float32) - prev[0][y0:y1, x0:x1].astype(np.float32)).mean())
            rows.append((i, t, fd, float(0.5 * np.abs(hist - prev[1]).sum()), float(np.abs(mean - prev[2]).max())))
        prev = (gray, hist, mean)
    jumps = []
    for k_i, (i, t, fd, hd, cd) in enumerate(rows):
        a, b = track.nose[i] if i < len(track.nose) else None, track.nose[i - 1] if 0 < i <= len(track.nose) else None
        if a is not None and b is not None and track.height[i] == track.height[i] and track.height[i] > 0:
            jumps.append(math.dist(a, b) / track.height[i])
        else:
            jumps.append(float("nan"))
    lim_j = _robust_limit(jumps, k, face_floor)
    lim_f = _robust_limit([r[2] for r in rows], k, diff_floor)
    lim_h = _robust_limit([r[3] for r in rows], k, hist_floor)
    lim_c = _robust_limit([r[4] for r in rows], k, color_floor)
    out: List[Seam] = []
    for (i, t, fd, hd, cd), j in zip(rows, jumps):
        if (j == j and j > lim_j) or (fd == fd and fd > lim_f) or hd > lim_h or cd > lim_c:
            out.append(Seam(round(t, 3), i, round(j, 4) if j == j else 0.0, round(fd, 2) if fd == fd else 0.0, round(hd, 4), round(cd, 2)))
    merged: List[Seam] = []
    for s in out:                                           # transitions next to each other are one seam
        if merged and s.frame - merged[-1].frame <= 2:
            m = merged[-1]
            merged[-1] = Seam(m.time_s, m.frame, max(m.face_jump, s.face_jump), max(m.face_diff, s.face_diff), max(m.hist, s.hist), max(m.color, s.color))
        else:
            merged.append(s)
    return merged


def classify(found: Sequence[Seam], cuts: Sequence[float], graphics: Sequence[float], fps: float = 24.0, cut_tol_frames: int = 2, graphic_tol_s: float = 0.5) -> List[Seam]:
    """Marks each seam: `designed` when a planned cut is within `cut_tol_frames`, else `graphic` when a graphic event is within `graphic_tol_s` (the picture under a graphic must not
    change, so this is a fault), else `accidental`. A designed cut that is also at a graphic event stays `designed` and says so in its note."""
    for s in found:
        near_cut = any(abs(s.time_s - c) <= cut_tol_frames / fps for c in cuts)
        near_graphic = [g for g in graphics if abs(s.time_s - g) <= graphic_tol_s]
        if near_cut:
            s.kind = "designed"
            s.note = "a planned cut" + (f", {len(near_graphic)} graphic event(s) within {graphic_tol_s}s" if near_graphic else "")
        elif near_graphic:
            s.kind = "graphic"
            s.note = f"at a graphic event ({near_graphic[0]:.2f}s)"
        else:
            s.kind = "accidental"
    return list(found)


def report(path: str, cuts: Sequence[float] = (), graphics: Sequence[float] = (), step: int = 1) -> Dict[str, Any]:
    track = face_track(path, step)
    found = classify(seams(path, track), cuts, graphics, fps_of(path))
    return {"video": str(path), "fps": track.fps, "mouth": mouth(track), "head": head_motion(track), "seams": [s.__dict__ for s in found],
            "faces_found": sum(1 for n in track.nose if n is not None), "frames": len(track.nose)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="seams, mouth opening and head motion of a video")
    ap.add_argument("video")
    ap.add_argument("--cuts", default="", help="planned cut times in seconds, comma separated")
    ap.add_argument("--graphics", default="", help="graphic event times in seconds, comma separated")
    ap.add_argument("--body", action="store_true", help="also the shoulder and lower-half motion")
    args = ap.parse_args(argv)
    nums = lambda s: [float(x) for x in s.split(",") if x.strip()]
    out = report(args.video, nums(args.cuts), nums(args.graphics))
    if args.body:
        out["body"] = body_motion(args.video)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
