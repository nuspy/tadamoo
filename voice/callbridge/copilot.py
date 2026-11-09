"""The copilot: a private suggester for the person during a call or meeting (Shift+F4 or the Voice tab).

It listens to the OTHER party only: the PC's output, captured as loopback (the person's own microphone is never opened). For each thing the
other party says it shows a line of context and N suggested replies in a private window that is kept out of screen capture. What is said is
untrusted text: it reaches the DIRECT brain only (no tools), fenced as data, and what comes back is shown to the person and nowhere else.
Nothing is written to disk. With "reply with audio" a clicked reply is spoken, through the same virtual microphone and the same rules as the
meeting bot (a device the person installed; never over someone speaking)."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from . import sospensione
from .meetbot import speak

log = logging.getLogger("callbridge.copilot")


@dataclass
class CopilotEntry:
    seq: int
    utterance: str                # what the other party said
    body: str = ""                # the suggestion, as it is written
    done: bool = False


def prompt_for(cfg, n: int, utterance: str) -> str:
    return cfg.copilot_preamble.format(n=n) + "<other_party>\n" + speak.fence(utterance) + "\n</other_party>"


class CopilotSession:
    """loopback -> speech recognition -> direct brain -> the window. `process` handles one utterance and is what the loop calls."""

    def __init__(self, cfg, brains, panel, stt_factory, source_factory, voice=None, floor=None) -> None:
        self.cfg, self.brains, self.panel = cfg, brains, panel
        self._stt_factory, self._source_factory = stt_factory, source_factory
        self.voice, self.floor = voice, floor or speak.Floor()
        self.num_responses = max(1, min(6, int(cfg.copilot_num_responses)))
        self._stop = threading.Event()
        self._seq = 0
        self._stt = self._source = None
        self._thread: threading.Thread | None = None

    def set_num_responses(self, n: int) -> None:
        self.num_responses = max(1, min(6, int(n)))

    def start(self) -> None:
        self._stt = self._stt_factory(self.cfg.copilot_lang)
        self._source = self._source_factory()
        self._source.start(self._stt.send_audio)
        self._thread = threading.Thread(target=self._loop, name="copilot-brain", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for closer in (getattr(self._source, "stop", None), getattr(self._stt, "close", None), getattr(self.voice, "close", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._stt.speech_events.get_nowait()
                self.floor.heard()
            except (queue.Empty, AttributeError):
                pass
            try:
                text = self._stt.utterances.get(timeout=0.4).strip()
            except queue.Empty:
                continue
            if text:
                self.floor.heard()
                self.process(text)

    def process(self, text: str) -> CopilotEntry:
        """Show what the other party said, then the suggestion as the direct brain writes it."""
        self._seq += 1
        entry = CopilotEntry(self._seq, text)
        self.panel.add_entry(entry)
        try:
            for chunk in speak.direct_of(self.brains).ask_stream(prompt_for(self.cfg, self.num_responses, text)):
                if self._stop.is_set():
                    break
                if isinstance(chunk, str) and chunk:
                    entry.body += chunk
                    self.panel.update_entry(entry)
        except Exception as e:  # noqa: BLE001 (the brain is down)
            entry.body += f"\n[error: {e}]"
        finally:
            entry.done = True
            self.panel.update_entry(entry)
        return entry

    def speak(self, text: str) -> bool:
        """Say a reply the person clicked. False, with a note in the window, when the rules do not allow it."""
        if self.voice is None:
            return False
        why = self.voice.block_reason()
        if why:
            self.panel.add_note(why)
            return False
        threading.Thread(target=self.voice.say, args=(text.strip().lstrip("-*• ").strip(),), name="copilot-speak", daemon=True).start()
        return True


class CopilotController:
    """Opens and closes the copilot: the hotkey toggles it, the Voice tab asks for it."""

    def __init__(self, cfg, brains, desktop_factory, stt_factory, source_factory, voice_factory=None) -> None:
        self.cfg, self.brains = cfg, brains
        self._desktop_factory, self._stt_factory, self._source_factory, self._voice_factory = desktop_factory, stt_factory, source_factory, voice_factory
        self._lock = threading.Lock()
        self._session: CopilotSession | None = None
        self.note = ""

    @property
    def is_active(self) -> bool:
        return self._session is not None

    def state(self) -> dict:
        c = self.cfg
        return {"enabled": bool(c.copilot_enabled), "active": self.is_active, "replies": c.copilot_num_responses, "audio_reply": c.copilot_audio_reply,
                "hide_from_capture": c.copilot_hide_from_capture, "note": self.note}

    def toggle(self) -> None:
        with self._lock:
            if self._session is None:
                sospensione.richiedi_attivi()
                self._activate()
            else:
                self._deactivate()

    def _activate(self) -> None:
        c = self.cfg
        self.note = ""
        holder: list[CopilotSession] = []
        panel = self._desktop_factory().new_suggestions(
            hide_from_capture=bool(c.copilot_hide_from_capture), font_size=c.copilot_font_size, count=c.copilot_num_responses,
            audio_reply=bool(c.copilot_audio_reply), on_speak=lambda t: holder and holder[0].speak(t),
            on_count=lambda n: holder and holder[0].set_num_responses(n))
        opened, why = panel.open()
        if not opened:
            self.note = why
            log.warning("copilot: %s", why)
            return
        voice = self._voice_factory() if self._voice_factory else None
        session = CopilotSession(c, self.brains, panel, self._stt_factory, self._source_factory, voice=voice, floor=getattr(voice, "floor", None))
        holder.append(session)
        try:
            session.start()
        except Exception as e:  # noqa: BLE001 (STT not loaded, no loopback)
            self.note = f"the copilot cannot listen: {e}"
            panel.close()
            return
        self._session = session

    def _deactivate(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.stop()
            session.panel.close()
            log.info("copilot: closed")
