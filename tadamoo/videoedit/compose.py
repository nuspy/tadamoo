"""Editing with ffmpeg: one file for each shot, the shots joined, and the opening and closing sigla edited in untouched.

A shot is a canvas of the video's size on which moving pictures are laid (the presenter, a B-roll clip, each cropped, zoomed, slowed or pushed in) with transparent
graphics over them (given as layers), and the voice under it. Every shot comes out in the same format so that joining is a copy and nothing is re-encoded twice. No text of
the script reaches a command line: texts are drawn into PNG files; ffmpeg gets numbers and paths.

The sigla are the person's own files and are never touched: their pictures and their sound are joined by stream copy. For that the body of the video is encoded with
the settings that give the sigla's own parameter sets (the same profile, level, frame rate, size, `crf` and preset), its sound is AAC-LC at the sigla's sample rate, and
the one thing an encoder of another version writes differently (the colour information in the sequence parameter set, which does not change how a picture is decoded)
is replaced, in the body's stream, by the sigla's. `verify_untouched` then compares, packet by packet, the hash of the sigla's pictures in the output with the originals."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from tadamoo.videoedit.frame import Box, Frame

TAIL_S = 0.35                                      # a breath after the last word of a block
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "3.1", "-crf", "11", "-pix_fmt", "yuv420p"]
AUDIO_RATE = 32000


class ComposeError(RuntimeError):
    pass


@dataclass
class Layer:
    """A moving picture on the canvas. `crop` is (x, y, zoom): where the window sits (0 to 1 across the picture) and how far it is zoomed in (1 = the whole picture);
    `push` zooms in a further share of the picture over the shot (a slow push-in); `speed` below 1 slows the clip."""
    path: Path
    start: float = 0.0
    speed: float = 1.0
    box: Optional[Box] = None
    crop: Tuple[float, float, float] = (0.5, 0.5, 1.0)
    push: float = 0.0


@dataclass
class ShotSpec:
    duration: float
    layers: List[Layer]
    overlays: List[Path] = field(default_factory=list)
    voice: Optional[Path] = None
    voice_start: float = 0.0
    back: str = "0x0a1228"


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ComposeError(f"{name} is not on the PATH")
    return found


def run(args: Sequence[object]) -> None:
    done = subprocess.run([tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise ComposeError("ffmpeg failed: " + (done.stderr.strip().splitlines() or ["no message"])[-1][:300])


def probe(path: Path) -> dict:
    """`{duration, width, height, fps, has_audio, has_video, audio_rate, ...}` of a media file."""
    done = subprocess.run([tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise ComposeError(f"{path.name} could not be read: {done.stderr.strip()[-200:]}")
    info = json.loads(done.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    num, _, den = str(video.get("avg_frame_rate") or "0/1").partition("/")
    return {"duration": float(info.get("format", {}).get("duration") or 0), "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "fps": (float(num) / float(den or 1)) if float(den or 1) else 0.0, "has_audio": bool(audio), "has_video": bool(video), "audio_rate": int(audio.get("sample_rate") or 0),
            "video_codec": video.get("codec_name", ""), "pix_fmt": video.get("pix_fmt", ""), "profile": video.get("profile", ""), "level": video.get("level", 0),
            "timescale": int(str(video.get("time_base") or "1/1").partition("/")[2] or 1)}


def frame_count(path: Path) -> int:
    """The number of video frames that decode (counted, not read from the container)."""
    done = subprocess.run([tool("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip().isdigit():
        raise ComposeError(f"{Path(path).name}: the frames could not be counted: {done.stderr.strip()[-200:]}")
    return int(done.stdout.strip())


def frame(video: Path, at: float, out: Path) -> Path:
    run(["-ss", f"{at:.3f}", "-i", video, "-frames:v", "1", "-q:v", "2", out])
    return out


# ---- one shot
def _layer_graph(i: int, layer: Layer, canvas: Frame, fps: int, duration: float) -> Tuple[str, Box]:
    box = layer.box or Box(0, 0, canvas.width, canvas.height)
    cx, cy, zoom = layer.crop
    head = f"[{i}:v]setpts=(PTS-STARTPTS)/{layer.speed:.4f},fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS"
    if layer.push > 0:
        frames = max(2, round(duration * fps))
        graph = (f"{head},scale={box.w * 2}:{box.h * 2}:force_original_aspect_ratio=increase:flags=lanczos,crop={box.w * 2}:{box.h * 2},"
                 f"zoompan=z='{zoom:.4f}*(1+{layer.push:.4f}*on/{frames})':x='(iw-iw/zoom)*{cx:.4f}':y='(ih-ih/zoom)*{cy:.4f}':d=1:s={box.w}x{box.h}:fps={fps}")
    else:
        sw, sh = max(box.w, round(box.w * zoom)), max(box.h, round(box.h * zoom))
        graph = (f"{head},scale={sw}:{sh}:force_original_aspect_ratio=increase:flags=lanczos,"
                 f"crop={box.w}:{box.h}:x='(iw-ow)*{cx:.4f}':y='(ih-oh)*{cy:.4f}'")
    return f"{graph},setsar=1[l{i}]", box


def render_shot(spec: ShotSpec, canvas: Frame, fps: int, out: Path) -> Path:
    """The shot as an MP4 with its voice (AAC at the sigla's sample rate)."""
    inputs: List[str] = []
    graphs: List[str] = []
    boxes: List[Box] = []
    for i, layer in enumerate(spec.layers):
        g, box = _layer_graph(i, layer, canvas, fps, spec.duration)
        inputs += ["-ss", f"{layer.start:.3f}", "-t", f"{spec.duration * layer.speed + 0.5:.3f}", "-i", str(layer.path)]
        graphs.append(g)
        boxes.append(box)
    n = len(spec.layers)
    for path in spec.overlays:
        inputs += ["-loop", "1", "-framerate", str(fps), "-i", str(path)]
    if spec.voice is not None:
        inputs += ["-ss", f"{spec.voice_start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.voice)]
    chain = f"color=c={spec.back}:s={canvas.width}x{canvas.height}:r={fps}:d={spec.duration:.3f}[bg0]"
    last = "bg0"
    for i, box in enumerate(boxes):
        chain += f";[{last}][l{i}]overlay={box.x}:{box.y}:shortest=0[b{i}]"
        last = f"b{i}"
    for j in range(len(spec.overlays)):
        chain += f";[{last}][{n + j}:v]overlay=0:0:shortest=0[o{j}]"
        last = f"o{j}"
    chain += f";[{last}]format=yuv420p,trim=duration={spec.duration:.3f}[v]"
    if spec.voice is not None:
        audio = f";[{n + len(spec.overlays)}:a]aresample={AUDIO_RATE},aformat=channel_layouts=stereo,apad,atrim=duration={spec.duration:.3f},asetpts=PTS-STARTPTS[s]"
    else:
        audio = f";anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=duration={spec.duration:.3f}[s]"
    run([*inputs, "-filter_complex", ";".join(graphs) + ";" + chain + audio, "-map", "[v]", "-map", "[s]", "-t", f"{spec.duration:.3f}", "-r", fps, *X264,
         "-c:a", "aac", "-b:a", "128k", "-ar", AUDIO_RATE, "-ac", "2", out])
    return out


def join_copy(parts: Sequence[Path], out: Path, plain_packets: bool = False) -> Path:
    """The files one after the other by stream copy (the concat demuxer). `plain_packets`: the muxer adds the parameter sets to the first picture of the output; they are
    taken out again, so that every picture's packet is the one in its source file."""
    listing = out.with_suffix(".txt")
    listing.write_text("".join("file '{}'\n".format(str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''")) for p in parts), encoding="utf-8")
    try:
        run(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", *(["-bsf:v", "filter_units=remove_types=7|8"] if plain_packets else []), out])
    finally:
        listing.unlink(missing_ok=True)
    return out


def convert_fps(src: Path, fps: int, out: Path) -> Path:
    """The video at another frame rate (a frame repeated or dropped where the rates do not match), encoded as the body is, its sound copied."""
    run(["-i", src, "-vf", f"fps={fps}", "-r", str(fps), *X264, "-c:a", "copy", out])
    return out


# ---- the sigla, untouched
def parameter_sets(path: Path) -> Dict[int, bytes]:
    """The SPS (type 7) and PPS (type 8) of a file's H.264 stream, as the first IDR carries them."""
    raw = Path(str(path) + ".annexb.tmp")
    run(["-i", path, "-map", "0:v:0", "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", raw])
    data = raw.read_bytes()
    raw.unlink(missing_ok=True)
    found: Dict[int, bytes] = {}
    for m in re.finditer(b"\x00\x00\x00\x01", data[:400000]):
        kind = data[m.end()] & 0x1F
        if kind in (7, 8) and kind not in found:
            end = data.find(b"\x00\x00\x01", m.end())
            found[kind] = data[m.end():end if end > 0 else None].rstrip(b"\x00")
    return found


def match_parameter_sets(body: Path, sigla: Path, out: Path) -> Path:
    """The body's stream with its SPS and PPS replaced by the sigla's (stream copy: no picture is touched). Both must describe the same picture format: codec, size,
    pixel format, profile, level and frame rate are compared first, and a difference is an error (the body would have to be encoded again with other settings)."""
    a, b = probe(body), probe(sigla)
    for key in ("video_codec", "width", "height", "pix_fmt", "profile", "level"):
        if a[key] != b[key]:
            raise ComposeError(f"the video cannot be joined to the sigla without re-encoding: {key} is {a[key]} and the sigla's is {b[key]}")
    if abs(a["fps"] - b["fps"]) > 0.01:
        raise ComposeError(f"the frame rate is {a['fps']:.3f} and the sigla's is {b['fps']:.3f}")
    mine, theirs = parameter_sets(body), parameter_sets(sigla)
    raw = Path(str(out) + ".annexb.tmp")
    run(["-i", body, "-map", "0:v:0", "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", raw])
    data = raw.read_bytes()
    for kind in (7, 8):
        if mine.get(kind) is None or theirs.get(kind) is None:
            raise ComposeError("a stream has no parameter sets")
        data = data.replace(b"\x00\x00\x00\x01" + mine[kind], b"\x00\x00\x00\x01" + theirs[kind])
    raw.write_bytes(data)
    fps = str(round(b["fps"])) if abs(b["fps"] - round(b["fps"])) < 0.01 else f"{b['fps']:.5f}"
    run(["-framerate", fps, "-f", "h264", "-i", raw, "-i", body, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-video_track_timescale", b["timescale"], out])
    raw.unlink(missing_ok=True)
    return out


def packet_hashes(path: Path, stream: str = "v:0") -> List[str]:
    """The hash of each packet of a stream, in order, as ffmpeg writes it with stream copy."""
    done = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", str(path), "-map", f"0:{stream}", "-c", "copy", "-f", "framemd5", "-"], capture_output=True, text=True)
    if done.returncode != 0:
        raise ComposeError(f"{path.name} could not be hashed: {done.stderr.strip()[-200:]}")
    return [line.rsplit(",", 1)[-1].strip() for line in done.stdout.splitlines() if line and not line.startswith("#")]


def join_with_sigla(opening: Optional[Path], body: Path, closing: Optional[Path], out: Path) -> Path:
    """The opening sigla, the body and the closing sigla by stream copy. The body is first given the sigla's parameter sets (`match_parameter_sets`)."""
    reference = opening or closing
    fitted = body
    if reference is not None:
        fitted = match_parameter_sets(body, reference, body.with_name(body.stem + "_fitted.mp4"))
        if opening is not None and closing is not None and probe(opening)["audio_rate"] != probe(closing)["audio_rate"]:
            raise ComposeError("the two sigla have different sound formats")
    return join_copy([p for p in (opening, fitted, closing) if p is not None], out, plain_packets=True)


def verify_untouched(output: Path, original: Path, where: str) -> Dict[str, object]:
    """Whether the sigla's pictures and sound are in the output exactly: the packet hashes of `original` are the first (`where` = "start") or the last ("end") ones of the output.
    The first sound packet of an AAC track is the encoder's priming, before time zero, and the mp4 muxer does not carry it over: it is left out of the comparison and counted."""
    result: Dict[str, object] = {"file": original.name, "where": where}
    for label, stream in (("video", "v:0"), ("audio", "a:0")):
        want, got = packet_hashes(original, stream), packet_hashes(output, stream)
        part = got[:len(want)] if where == "start" else got[len(got) - len(want):]
        skip = 1 if label == "audio" else 0
        same = sum(1 for x, y in zip(want[skip:], part[skip:]) if x == y)
        result[label] = {"packets": len(want), "identical": same + skip * 0, "compared": len(want) - skip, "same": same == len(want) - skip and len(part) == len(want),
                         "hash": hashlib.sha256("".join(want).encode()).hexdigest()[:16]}
    result["untouched"] = bool(result["video"]["same"] and result["audio"]["same"])      # type: ignore[index]
    return result
