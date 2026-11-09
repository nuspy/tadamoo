"""The audio of the meeting bot: what it hears (the PC's output, captured as loopback), where it speaks (a virtual microphone the person
installed) and how that device is found.

Sources and sinks are small objects behind two shapes, so that the session never names a system:

    source.start(on_pcm)   on_pcm(float32 mono 16 kHz) is called from a thread of the source; source.stop()
    sink.play(samples, rate, keep_going) -> bool    blocks while it plays; False when `keep_going()` turned false and it stopped early

Windows hears through WASAPI loopback (the `soundcard` package); Linux through the monitor of the default PulseAudio sink (`parec`). Both
capture everything the PC plays, not only the meeting. Nothing is installed or created: a virtual microphone is a device the person
installed (a virtual audio cable); it is found by its name, and when there is none the bot says so and does not speak."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable

import numpy as np

from ..audio_hfp import resample

log = logging.getLogger("callbridge.meetbot.audio")

STT_RATE = 16000
NO_VIRTUAL_MIC = "no virtual microphone found: install a virtual audio cable and pick it in Settings > Voice"
# Names of the playback side of the usual virtual audio cables (what the bot plays into; the meeting listens to the other side).
VIRTUAL_MIC_HINTS = ("cable input", "virtual cable", "vb-audio", "voicemeeter input", "voicemeeter aux input", "hi-fi cable", "virtual audio cable")


def find_virtual_mic(names: list[str], wanted: str = "") -> str | None:
    """The output device the bot speaks into: the one whose name contains `wanted` (the person's choice), else the first that looks like a
    virtual audio cable; None when there is none."""
    if wanted.strip():
        key = wanted.strip().lower()
        return next((n for n in names if key in n.lower()), None)
    return next((n for n in names if any(h in n.lower() for h in VIRTUAL_MIC_HINTS)), None)


def output_device_names() -> list[str]:
    """The names of the output devices of this PC (empty when audio cannot be queried)."""
    try:
        import sounddevice as sd
        return [d["name"] for d in sd.query_devices() if d.get("max_output_channels", 0) > 0]
    except Exception as e:  # noqa: BLE001 (no PortAudio, no device)
        log.warning("output devices cannot be listed: %s", e)
        return []


# ---------------------------------------------------------------------------------------- hearing
class WindowsLoopback:
    """The PC's output through WASAPI loopback, resampled to 16 kHz."""

    def __init__(self, rate: int = 48000) -> None:
        self._rate = rate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_pcm: Callable[[np.ndarray], None]) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_pcm,), name="meet-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self, on_pcm) -> None:
        if "soundcard" in sys.modules:                  # soundcard starts COM only in the thread that first imports it
            try:
                import ctypes
                ctypes.windll.ole32.CoInitializeEx(None, 0)
            except Exception:  # noqa: BLE001
                pass
        try:
            import soundcard as sc
        except ImportError:
            log.error("loopback capture needs the 'soundcard' package")
            return
        block = int(self._rate * 0.1)
        while not self._stop.is_set():
            try:
                speaker = sc.default_speaker()
                mic = sc.get_microphone(str(speaker.name), include_loopback=True)
                with mic.recorder(samplerate=self._rate, channels=1) as rec:
                    log.info("loopback from '%s' at %d Hz", speaker.name, self._rate)
                    while not self._stop.is_set():
                        data = rec.record(numframes=block)
                        mono = (data[:, 0] if getattr(data, "ndim", 1) > 1 else data).astype(np.float32)
                        on_pcm(resample(mono, self._rate, STT_RATE) if self._rate != STT_RATE else mono)
            except Exception as e:  # noqa: BLE001 (the device changed or is busy)
                if self._stop.is_set():
                    break
                log.warning("loopback interrupted (%s), trying again", e)
                self._stop.wait(1.0)


class PulseMonitor:
    """The monitor of the default PulseAudio sink, read with `parec` at 16 kHz. Written for Linux; not proven here."""

    def __init__(self, run=subprocess.run, popen=subprocess.Popen) -> None:
        self._run, self._popen = run, popen
        self._proc = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _monitor(self) -> str:
        out = self._run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5).stdout.strip()
        return out + ".monitor"

    def start(self, on_pcm: Callable[[np.ndarray], None]) -> None:
        if shutil.which("parec") is None and self._popen is subprocess.Popen:
            log.error("capture on Linux needs parec (pulseaudio-utils)")
            return
        self._stop.clear()
        self._proc = self._popen(["parec", "-d", self._monitor(), "--format=float32le", f"--rate={STT_RATE}", "--channels=1"], stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._read, args=(on_pcm,), name="meet-capture", daemon=True)
        self._thread.start()

    def _read(self, on_pcm) -> None:
        size = int(STT_RATE * 0.1) * 4
        while not self._stop.is_set():
            raw = self._proc.stdout.read(size)
            if not raw:
                break
            on_pcm(np.frombuffer(raw[:len(raw) - len(raw) % 4], dtype="<f4").copy())

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


def make_source(platform: str = sys.platform, rate: int = 48000):
    if platform == "win32":
        return WindowsLoopback(rate)
    if platform.startswith("linux"):
        return PulseMonitor()
    raise RuntimeError(f"the meeting bot cannot hear the PC's audio on {platform}")


# ---------------------------------------------------------------------------------------- speaking
class DeviceSink:
    """Plays into one output device by name (the virtual microphone), in blocks of 100 ms so that it can stop at once."""

    def __init__(self, name: str) -> None:
        self.name = name

    def play(self, samples: np.ndarray, rate: int, keep_going: Callable[[], bool] = lambda: True) -> bool:
        import sounddevice as sd
        index = next(i for i, d in enumerate(sd.query_devices()) if d["name"] == self.name and d.get("max_output_channels", 0) > 0)
        target = int(sd.query_devices(index)["default_samplerate"])
        pcm = resample(np.asarray(samples, dtype=np.float32), rate, target) if rate != target else np.asarray(samples, dtype=np.float32)
        block = int(target * 0.1)
        with sd.OutputStream(samplerate=target, device=index, channels=1, dtype="float32") as out:
            for start in range(0, len(pcm), block):
                if not keep_going():
                    return False
                out.write(pcm[start:start + block].reshape(-1, 1))
        return True
