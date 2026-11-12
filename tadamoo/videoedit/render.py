"""Drawing the video frame by frame: the virtual camera over each source, the cross-dissolves between shots, the presenter's window over B-roll, and the animated graphics.

Everything on the screen at a moment is a function of the time, so the animations are smooth by construction: a source frame is warped by the camera state of that moment (camera.py),
two shots overlap in a short dissolve, the story's side card and bar are slid with an easing curve from their place off the edge to their place on the picture. Frames go straight
into one ffmpeg encode, with the voice; the settings of that encode are compose.X264 so that the video can be joined to the sigla by stream copy."""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from tadamoo.videoedit import camera, compose

from tadamoo.videoedit.frame import Box
from tadamoo.videoedit.frame import Frame

EASINGS: Dict[str, Callable[[float], float]] = {
    "ease_out": lambda u: 1 - (1 - min(1.0, max(0.0, u))) ** 3,
    "ease_in": lambda u: min(1.0, max(0.0, u)) ** 3,
    "ease_in_out": camera.smooth,
    "linear": lambda u: min(1.0, max(0.0, u)),
}


# ---- reading sources
def decode(path: Path, start: float, seconds: float, fps: int, speed: float = 1.0, size: Optional[Tuple[int, int]] = None) -> List[np.ndarray]:
    """The frames of a video from `start`, `seconds` long as shown (a slowed clip reads less of the source), at `fps`, as RGB arrays; the last frame repeats when the video ends early."""
    info = compose.probe(path)
    w, h = size or (info["width"], info["height"])
    count = max(1, round(seconds * fps))
    vf = f"setpts=PTS/{speed:.4f},fps={fps}" + (f",scale={w}:{h}:flags=lanczos" if size else "")
    done = subprocess.run([compose.tool("ffmpeg"), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{seconds * speed + 0.3:.3f}", "-i", str(path), "-vf", vf,
                           "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if done.returncode != 0 and not done.stdout:
        raise compose.ComposeError(f"{path.name} could not be decoded: {done.stderr.decode('utf-8', 'replace')[-200:]}")
    frames = [np.frombuffer(done.stdout[i * w * h * 3:(i + 1) * w * h * 3], dtype=np.uint8).reshape(h, w, 3) for i in range(len(done.stdout) // (w * h * 3))]
    if not frames and start > 0:                                          # asked beyond the end of the video: its last picture is held
        done = subprocess.run([compose.tool("ffmpeg"), "-v", "error", "-sseof", "-0.2", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24"] +
                              (["-vf", f"scale={w}:{h}:flags=lanczos"] if size else []) + ["-"], capture_output=True)
        frames = [np.frombuffer(done.stdout[i * w * h * 3:(i + 1) * w * h * 3], dtype=np.uint8).reshape(h, w, 3) for i in range(len(done.stdout) // (w * h * 3))]
    if not frames:
        raise compose.ComposeError(f"{path.name} has no picture at {start:.2f} s")
    while len(frames) < count:
        frames.append(frames[-1])
    return frames


@dataclass
class Source:
    """A run of frames that can be asked for at a time: `frame(t)` is the picture `t` seconds after its start (clamped to the run)."""
    frames: List[np.ndarray]
    fps: int
    t0: float = 0.0                                # how long before the shot's start the run begins (read ahead for a dissolve)

    def frame(self, t: float) -> np.ndarray:
        i = round((t + self.t0) * self.fps)
        return self.frames[min(len(self.frames) - 1, max(0, i))]


# ---- the camera on a picture
def warp(img: np.ndarray, state: camera.State, face: Tuple[float, float], canvas: Frame, sharpen: bool = True) -> Image.Image:
    """The picture seen through the camera: the face of the picture (`face`, 0 to 1) is put at (`state.fx`, `state.fy`) of the screen, the window zoomed in by `state.zoom`
    over the picture covering the screen, rolled by `state.roll` degrees. The window is kept inside the picture."""
    src = Image.fromarray(img)
    sw, sh = src.size
    W, H = canvas.width, canvas.height
    scale = max(W / sw, H / sh) * state.zoom
    theta = math.radians(state.roll)
    margin = 1 + abs(math.sin(theta)) * 0.35
    hw, hh = W / 2 / scale * margin, H / 2 / scale * margin
    cx = min(max(face[0] * sw - (state.fx - 0.5) * W / scale, hw), max(hw, sw - hw))
    cy = min(max(face[1] * sh - (state.fy - 0.5) * H / scale, hh), max(hh, sh - hh))
    cos, sin = math.cos(theta) / scale, math.sin(theta) / scale
    coeffs = (cos, -sin, cx - cos * W / 2 + sin * H / 2, sin, cos, cy - sin * W / 2 - cos * H / 2)
    out = src.transform((W, H), Image.AFFINE, coeffs, resample=Image.BICUBIC)
    if sharpen and state.zoom > 1.15:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=int(20 + 40 * min(1.0, state.zoom - 1.0)), threshold=2))
    return out


# ---- graphics that move
def rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def paste_alpha(dst: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """The RGBA layer over `dst` (RGB) at (x, y); the parts off the picture are cut."""
    if x >= dst.width or y >= dst.height or x + layer.width <= 0 or y + layer.height <= 0:
        return
    dst.paste(layer.convert("RGB"), (x, y), layer.getchannel("A"))


def window(canvas: Frame, share: float, margin: int, corner: int, border: int) -> Tuple[Box, Image.Image, Image.Image]:
    """The presenter's small window: its place (top right), the rounded mask, and a shadow-and-border layer the size of the window plus its blur."""
    w = round(canvas.width * share)
    h = round(w * canvas.height / canvas.width)
    m = canvas.px(margin)
    box = Box(canvas.width - w - m, m, w, h)
    return box, rounded_mask((w, h), canvas.px(corner)), rounded_mask((w, h), canvas.px(corner))


@dataclass
class StoryGraphics:
    """The pictures that slide over one story: its side card (at the start), then its bar (until the end), and the evidence cards of its facts."""
    start: float
    end: float
    card: Image.Image
    bar: Image.Image
    cards: Dict[int, Image.Image] = field(default_factory=dict)      # evidence row number -> card


@dataclass
class Style:
    card_hold_s: float = 7.0
    slide_s: float = 0.6
    bar_in_s: float = 0.6
    bar_hold_s: float = 0.0                       # 0: until the story ends
    ease_in: str = "ease_out"                     # a graphic arriving slows down as it arrives
    ease_out: str = "ease_in"                     # a graphic leaving speeds up as it leaves
    pip_share: float = 0.30
    pip_margin: int = 14
    pip_corner: int = 9
    pip_border: int = 2
    dissolve_frames: int = 6


def _slide(layer: Image.Image, frame: Frame, side: str, progress: float) -> Tuple[int, int]:
    """Where a layer the size of the frame is when it has slid `progress` (0 = off the picture, 1 = in place) from the right or from the left."""
    return (round((1 - progress) * frame.width), 0) if side == "right" else (round(-(1 - progress) * frame.width), 0)


def graphics_state(g: StoryGraphics, t: float, style: Style, evidence_window: Optional[Tuple[float, float, int]]) -> List[Tuple[Image.Image, Tuple[int, int]]]:
    """The layers to draw at absolute time `t` for a story, each with its position."""
    out: List[Tuple[Image.Image, Tuple[int, int]]] = []
    local = t - g.start
    if local < 0 or t > g.end:
        return out
    frame = Frame(*g.card.size)
    hold, slide = style.card_hold_s, style.slide_s
    if local < hold + slide:
        arrive = EASINGS[style.ease_in](local / slide)
        leave = EASINGS[style.ease_out]((local - hold) / slide) if local > hold else 0.0
        out.append((g.card, _slide(g.card, frame, "right", arrive * (1 - leave))))
    bar_start = hold + slide
    if local >= bar_start:
        end_local = (g.end - g.start)
        if style.bar_hold_s > 0:
            end_local = min(end_local, bar_start + style.bar_in_s + style.bar_hold_s)
        arrive = EASINGS[style.ease_in]((local - bar_start) / style.bar_in_s)
        leave = EASINGS[style.ease_out]((local - (end_local - style.slide_s)) / style.slide_s) if local > end_local - style.slide_s else 0.0
        if local <= end_local:
            out.append((g.bar, _slide(g.bar, frame, "left", arrive * (1 - leave))))
    if evidence_window is not None:
        e0, e1, row = evidence_window
        if e0 <= t <= e1 and row in g.cards:
            arrive = EASINGS[style.ease_in]((t - e0) / style.slide_s)
            leave = EASINGS[style.ease_out]((t - (e1 - style.slide_s)) / style.slide_s) if t > e1 - style.slide_s else 0.0
            out.append((g.cards[row], _slide(g.cards[row], frame, "right", arrive * (1 - leave))))
    return out


# ---- the shots
@dataclass
class Shot:
    t0: float                                      # seconds on the video's clock
    t1: float
    story: int                                     # 0: no story graphics (the welcome and the farewell)
    main: Callable[[float], Image.Image]           # the picture `t` seconds after the shot's start (may be asked a little before 0 or after the end, for a dissolve)
    pip: Optional[Callable[[float], Image.Image]] = None
    evidence: Optional[int] = None


def compose_frame(shots: Sequence[Shot], f: int, fps: int, canvas: Frame, stories: Dict[int, StoryGraphics], style: Style, pip_box: Box, pip_mask: Image.Image,
                  label: Optional[Image.Image]) -> Image.Image:
    t = f / fps
    k = next((i for i, s in enumerate(shots) if s.t0 <= t < s.t1), len(shots) - 1)
    half = style.dissolve_frames / 2 / fps
    parts: List[Tuple[Image.Image, float]] = []

    def picture(shot: Shot) -> Image.Image:
        img = shot.main(t - shot.t0).copy()
        if shot.pip is not None:
            small = shot.pip(t - shot.t0).resize((pip_box.w, pip_box.h), Image.LANCZOS)
            shadow = Image.new("RGBA", (pip_box.w + 24, pip_box.h + 24), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((12, 14, pip_box.w + 12, pip_box.h + 14), radius=canvas.px(style.pip_corner), fill=(0, 0, 0, 120))
            shadow = shadow.filter(ImageFilter.GaussianBlur(5))
            paste_alpha(img, shadow, pip_box.x - 12, pip_box.y - 12)
            if style.pip_border:
                ring = Image.new("L", (pip_box.w + 2 * style.pip_border, pip_box.h + 2 * style.pip_border), 0)
                ImageDraw.Draw(ring).rounded_rectangle((0, 0, ring.width - 1, ring.height - 1), radius=canvas.px(style.pip_corner) + style.pip_border, fill=255)
                img.paste((255, 255, 255), (pip_box.x - style.pip_border, pip_box.y - style.pip_border), ring)
            img.paste(small, (pip_box.x, pip_box.y), pip_mask)
        return img

    main = picture(shots[k])
    if k + 1 < len(shots) and t >= shots[k].t1 - half:                     # the shot is ending: the next one comes in
        u = camera.smooth((t - (shots[k].t1 - half)) / (2 * half))
        main = Image.blend(main, picture(shots[k + 1]), u)
    elif k > 0 and t < shots[k].t0 + half:                                 # the shot has just begun: the one before goes out
        u = camera.smooth((t - (shots[k].t0 - half)) / (2 * half))
        main = Image.blend(picture(shots[k - 1]), main, u)
    # the story's graphics belong to the story that is on screen at this time
    story = shots[k].story
    if story and story in stories:
        window = (shots[k].t0 + 0.2, shots[k].t1 - 0.2, shots[k].evidence) if shots[k].evidence else None
        for layer, (x, y) in graphics_state(stories[story], t, style, window):
            paste_alpha(main, layer, x, y)
    if label is not None:
        paste_alpha(main, label, 0, 0)
    return main


def encode(frames: Callable[[int], Image.Image], count: int, fps: int, canvas: Frame, voice: Path, out: Path,
           progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Every frame (`frames(f)` for f in 0..count) and the voice into one MP4, with the settings of compose.X264 and AAC at the sigla's sample rate."""
    cmd = [compose.tool("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{canvas.width}x{canvas.height}", "-r", str(fps), "-i", "-",
           "-i", str(voice), "-map", "0:v", "-map", "1:a", *compose.X264, "-r", str(fps), "-c:a", "aac", "-b:a", "128k", "-ar", str(compose.AUDIO_RATE), "-ac", "2",
           "-af", "apad", "-t", f"{count / fps:.3f}", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for f in range(count):
            proc.stdin.write(frames(f).convert("RGB").tobytes())             # type: ignore[union-attr]
            if progress is not None and f % 120 == 0:
                progress(f, count)
        proc.stdin.close()                                                    # type: ignore[union-attr]
        err = proc.stderr.read().decode("utf-8", "replace")                   # type: ignore[union-attr]
        if proc.wait() != 0:
            raise compose.ComposeError("the encode failed: " + err.strip()[-300:])
    finally:
        if proc.poll() is None:
            proc.kill()
    return out
