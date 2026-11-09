"""A stand-in for the ESP32 terminal and any other device that draws the avatar from visemes (docs/voice/esp32.md).

It speaks exactly what the firmware speaks to the gateway over a WebSocket: JSON text frames and PCM16 little-endian mono binary frames, the
first message `auth` with the token, the modes it can draw, a turn as `start`, PCM and `end` (or a typed `text`), and `interrupt`. It checks
the rules a firmware must keep (a frame has an even length, PCM comes only after `audio_start`, every audio segment has its viseme timeline
before its PCM) and turns the messages into the schedule the screen follows."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from websockets.sync.client import connect


class ProtocolError(AssertionError):
    """The gateway sent something a device cannot follow."""


@dataclass
class Segment:
    segment: int
    text: str
    duration_ms: int
    pcm_bytes: int
    visemes: list
    received: int = 0


@dataclass
class Playback:
    """What the device heard and saw of one reply."""
    sample_rate: int = 0
    segments: list = field(default_factory=list)
    interrupted: bool = False
    audio_ended: bool = False
    current: Segment | None = None

    def schedule(self) -> list[tuple[int, str]]:
        """[(milliseconds from the start of the audio, viseme)] for the whole reply: each segment starts where the audio before it ends."""
        out, start = [], 0.0
        for seg in self.segments:
            out += [(round(start + v["t"]), v["v"]) for v in seg.visemes]
            start += seg.received / 2 / self.sample_rate * 1000 if self.sample_rate else 0
        return out


class FakeDevice:
    def __init__(self, url: str, token: str, render_modes=("visemes",), device: str = "esp32") -> None:
        self.url, self.token, self.render_modes, self.device = url, token, list(render_modes), device
        self.ws = None
        self.ready: dict = {}
        self.messages: list[dict] = []
        self.sent_frames = 0

    # ---- connection
    def open(self) -> dict:
        """Connect, authenticate with the declared modes, and return `ready`."""
        self.ws = connect(self.url, open_timeout=5)
        self._send({"type": "auth", "token": self.token, "render_modes": self.render_modes, "device": self.device})
        if self._json()["type"] != "auth_ok":
            raise ProtocolError("the gateway did not accept the token")
        self.ready = self._json()
        if self.ready["type"] != "ready":
            raise ProtocolError(f"expected ready, got {self.ready['type']}")
        return self.ready

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()

    def __enter__(self) -> "FakeDevice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- what the device sends
    def _send(self, obj) -> None:
        self.ws.send(json.dumps(obj))

    def say(self, text: str, lang: str = "en") -> None:
        """A typed sentence: the same turn without the microphone."""
        self._send({"type": "text", "text": text, "lang": lang})

    def speak(self, pcm: bytes, rate: int = 16000, frame_bytes: int = 2048, lang: str = "en") -> None:
        """A hold-to-talk turn: `start`, the microphone's PCM16 in frames of even length, `end`."""
        self._send({"type": "start", "mode": "hold", "lang": lang, "sample_rate": rate})
        for i in range(0, len(pcm), frame_bytes):
            frame = pcm[i:i + frame_bytes]
            if len(frame) % 2:
                raise ProtocolError("a frame of PCM16 has an even length")
            self.ws.send(frame)
            self.sent_frames += 1
        self._send({"type": "end"})

    def interrupt(self) -> None:
        self._send({"type": "interrupt"})

    def report(self, mode: str, why: str) -> None:
        """The device cannot draw `mode`: the gateway answers with the next one."""
        self._send({"type": "render_failed", "mode": mode, "why": why})

    def send(self, obj: dict) -> None:
        self._send(obj)

    # ---- what the device receives
    def _json(self) -> dict:
        raw = self.ws.recv(timeout=10)
        if isinstance(raw, bytes):
            raise ProtocolError("PCM where a message was expected")
        msg = json.loads(raw)
        self.messages.append(msg)
        return msg

    def next_message(self) -> dict:
        return self._json()

    def listen(self, until: str = "done", on_pcm=None, play: Playback | None = None) -> Playback:
        """Follow the gateway until a message of type `until` (continuing `play` when given); `on_pcm(playback, nbytes)` runs for every PCM
        frame, which is how a test interrupts in the middle of a reply."""
        play = play or Playback()
        while True:
            raw = self.ws.recv(timeout=10)
            if isinstance(raw, bytes):
                if not play.sample_rate:
                    raise ProtocolError("PCM before audio_start")
                if len(raw) % 2:
                    raise ProtocolError("a frame of PCM16 has an even length")
                if play.current is None:
                    raise ProtocolError("PCM with no viseme timeline before it")
                play.current.received += len(raw)
                if on_pcm is not None:
                    on_pcm(play, len(raw))
                continue
            msg = json.loads(raw)
            self.messages.append(msg)
            kind = msg["type"]
            if kind == "audio_start":
                play.sample_rate = msg["sample_rate"]
            elif kind == "visemes":
                if play.current is not None and play.current.received != play.current.pcm_bytes:
                    raise ProtocolError("a viseme timeline came before the PCM of the previous segment was complete")
                play.current = Segment(msg["segment"], msg["text"], msg["duration_ms"], msg["pcm_bytes"], msg["visemes"])
                play.segments.append(play.current)
            elif kind == "audio_end":
                play.audio_ended = True
            elif kind == "interrupt":
                play.interrupted = True
            if kind == until:
                return play
