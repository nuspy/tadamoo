"""A test clip made with the real ffmpeg, for the tests of the video editing and of the production."""
import io
import subprocess
import wave
from pathlib import Path

from tadamoo.videoedit import compose


def make_clip(path: Path, seconds=2.0, size="640x360", audio=False):
    args = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-ar", "32000"]
    subprocess.run([*args, *compose.X264, str(path)], check=True)
    return path


def wav_bytes(seconds, rate=24000):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x02" * int(rate * seconds))
    return out.getvalue()
