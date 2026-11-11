"""A channel to the person that the other people of a meeting or call cannot hear: a question aloud on the person's own output (headphones) and the answer taken from the
person's own microphone.

It exists for the archives whose `others_ok` is ask_voice (`rag_client.VoiceAsker`). It is only made when the person named an output device for it (`meetings.private_output`,
a part of the device's name); the virtual microphone of the meeting, the loopback and the default device are never used, so nothing of it reaches the meeting. Without it the
question stays in the dialog on the screen. The words the person says are taken from their microphone only, through the service's speech recognizer; nothing is kept."""
from __future__ import annotations

import logging
import queue
from collections.abc import Callable

import numpy as np

from .meetbot.speak import Floor, MeetingVoice
from .rag_client import VoiceAsker

log = logging.getLogger("callbridge.private")
MIC_RATE = 16000


class MicSource:
    """The system microphone as a source of mono float32 audio at MIC_RATE: `start(callback)`, `stop()`."""

    def __init__(self, rate: int = MIC_RATE, open_stream: Callable | None = None) -> None:
        self.rate, self._open, self._stream = rate, open_stream, None

    def start(self, callback) -> None:
        if self._open is None:
            import sounddevice as sd
            self._open = lambda cb: sd.InputStream(samplerate=self.rate, channels=1, dtype="float32", callback=cb)
        self._stream = self._open(lambda indata, frames, t, status: callback(np.asarray(indata[:, 0], dtype=np.float32).copy()))
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class PrivateChannel:
    def __init__(self, device: str, tts_factory: Callable | None, sink_factory: Callable[[str], object], stt_factory: Callable[[str], object],
                 mic_factory: Callable[[], object] = MicSource, configure: Callable | None = None, names: Callable[[], list[str]] = lambda: [],
                 lang: str = "en") -> None:
        self.lang, self._stt_factory, self._mic_factory = lang, stt_factory, mic_factory
        match = next((n for n in names() if device and device.lower() in n.lower()), None) if device else None
        self.device = match
        self._voice = MeetingVoice(enabled=True, mic_name=match, tts_factory=tts_factory, sink_factory=sink_factory, floor=Floor(), lang=lang, configure=configure)

    @property
    def available(self) -> bool:
        return not self._voice.block_reason()

    def say(self, text: str) -> bool:
        """The question on the private output, at once (nobody to wait for: this channel is only the person's). False when there is none."""
        return self.available and self._voice.say(text, wait_for_pause=False)

    def listen(self, timeout_s: float) -> str | None:
        """What the person says next into their own microphone, as text; None when nothing was said or the recognizer or the microphone failed."""
        stt = mic = None
        try:
            stt = self._stt_factory(self.lang)
            mic = self._mic_factory()
            mic.start(stt.send_audio)
            return stt.utterances.get(timeout=timeout_s).strip() or None
        except queue.Empty:
            return None
        except Exception as e:  # noqa: BLE001 - no microphone, no recognizer
            log.warning("the answer could not be listened to: %s", e)
            return None
        finally:
            for closer in (getattr(mic, "stop", None), getattr(stt, "close", None)):
                if closer is not None:
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass

    def asker(self, wait_s: float = 25.0) -> VoiceAsker | None:
        """The asker for `rag_client.Knowledge`, or None when the channel cannot play a question."""
        return VoiceAsker(self.say, self.listen, wait_s, self.lang) if self.available else None
