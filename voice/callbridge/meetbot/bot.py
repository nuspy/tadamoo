"""MeetBotSession: una riunione = un bot (join -> capture -> STT -> leave).

Pipeline (modellata su CopilotSession):
  - thread Playwright (proprietario di browser/page: Playwright sync NON e'
    thread-safe, tutte le operazioni di pagina passano da una coda comandi)
  - thread capture: loopback -> resample 16k -> HandySttClient
  - thread brain: stt.utterances -> coalesce -> TranscriptStore (+ speaker
    dalla SpeakerTimeline)
  - il poll dell'active-speaker/partecipanti gira NEL thread Playwright

Stati: joining -> waiting_admit -> in_call -> leaving -> ended | error.
A leave completata il chiamante (api_meetings) avvia l'analisi.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from ..config import Config
from ..stt_client import HandySttClient
from ..util import coalesce_utterances
from .adapters import adapter_for
from .audio import WindowsLoopbackAudio
from .transcribe import SpeakerTimeline, TranscriptStore

log = logging.getLogger("callbridge.meetbot")

SPEAKER_POLL_S = 0.4


class MeetBotSession:
    def __init__(self, cfg: Config, url: str, display_name: str | None = None,
                 artifacts_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.url = url
        self.display_name = display_name or cfg.meetbot_display_name
        self.session_id = time.strftime("%Y%m%d-%H%M%S") + "-meet"
        self.state = "joining"
        self.error: str | None = None
        self.platform: str | None = None

        root = artifacts_dir or Path(__file__).resolve().parents[2] / \
            cfg.meetbot_artifacts_dir
        self.dir = root / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.transcript = TranscriptStore(self.dir / "transcript.jsonl")
        self.timeline = SpeakerTimeline()

        self._stop = threading.Event()
        self._stt: HandySttClient | None = None
        self._audio: WindowsLoopbackAudio | None = None
        self._threads: list[threading.Thread] = []
        # Coda comandi per il thread Playwright ("leave", "screenshot").
        self._pw_cmds: "queue.Queue[str]" = queue.Queue()

    # --- ciclo di vita ------------------------------------------------------

    def start(self) -> None:
        """Avvia il thread Playwright (non bloccante): l'esito del join si
        osserva su self.state."""
        t = threading.Thread(target=self._playwright_main,
                             name="meetbot-playwright", daemon=True)
        t.start()
        self._threads.append(t)

    def leave(self) -> None:
        self._pw_cmds.put("leave")

    def status(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "url": self.url,
            "platform": self.platform,
            "participants": list(self.transcript.participants),
            "transcript_tail": self.transcript.tail(8),
            "error": self.error,
        }

    # --- thread Playwright ----------------------------------------------------

    def _playwright_main(self) -> None:
        adapter = adapter_for(self.url)
        if adapter is None:
            self.state = "error"
            self.error = "piattaforma non supportata (url non riconosciuto)"
            return
        self.platform = adapter.platform
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.state = "error"
            self.error = "playwright non installato (pip install playwright; playwright install chromium)"
            return

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=False,
                    args=["--use-fake-ui-for-media-stream",  # niente popup permessi
                          "--autoplay-policy=no-user-gesture-required"])
                context = browser.new_context(
                    permissions=[],  # nega cam/mic: il bot ascolta dal loopback
                    locale="it-IT")
                page = context.new_page()
                page.goto(self.url, timeout=60000)
                res = adapter.join(page, self.display_name)
                if not res.ok:
                    self.state = "error"
                    self.error = res.message
                    self._screenshot(page, "join-error")
                    browser.close()
                    return
                self.state = res.state  # in_call | waiting_admit
                # Sala d'attesa: aspetta l'ammissione fino a 10 min.
                waited = 0.0
                while (self.state == "waiting_admit" and waited < 600
                       and not self._stop.is_set()):
                    time.sleep(2.0)
                    waited += 2.0
                    # Un "leave" dalla GUI durante la lobby deve avere effetto
                    # subito, non restare in coda fino all'ammissione (o 10 min).
                    try:
                        if self._pw_cmds.get_nowait() == "leave":
                            log.info("meetbot: leave richiesto in sala d'attesa")
                            self.state = "leaving"
                            adapter.leave(page)
                            browser.close()
                            return
                    except queue.Empty:
                        pass
                    if adapter.in_call(page):
                        self.state = "in_call"
                if self.state != "in_call":
                    self.state = "error"
                    self.error = self.error or "non ammesso entro 10 minuti"
                    self._screenshot(page, "not-admitted")
                    browser.close()
                    return

                log.info("meetbot %s: in chiamata su %s",
                         self.session_id, self.platform)
                self._start_pipeline()

                # Loop di servizio: poll speaker + comandi + fine riunione.
                last_incall = time.monotonic()
                while not self._stop.is_set():
                    try:
                        cmd = self._pw_cmds.get(timeout=SPEAKER_POLL_S)
                        if cmd == "leave":
                            break
                    except queue.Empty:
                        pass
                    info = adapter.speakers(page)
                    self.timeline.record(info.active)
                    if info.participants:
                        self.transcript.note_participants(info.participants)
                    if adapter.in_call(page):
                        last_incall = time.monotonic()
                    elif time.monotonic() - last_incall > 15:
                        log.info("meetbot: la riunione risulta terminata")
                        break

                self.state = "leaving"
                adapter.leave(page)
                browser.close()
            except Exception as e:  # noqa: BLE001
                log.error("meetbot: errore Playwright: %s", e)
                self.state = "error"
                self.error = str(e)
            finally:
                self._teardown_pipeline()
                if self.state != "error":
                    self.state = "ended"

    def _screenshot(self, page, tag: str) -> None:
        try:
            page.screenshot(path=str(self.dir / f"{tag}.png"))
        except Exception:  # noqa: BLE001
            pass

    # --- pipeline audio/STT/brain ---------------------------------------------

    def _start_pipeline(self) -> None:
        self._stt = HandySttClient(self.cfg.handy_stt_url())
        self._stt.start()
        self._audio = WindowsLoopbackAudio(
            self.cfg.copilot_loopback_samplerate, self._stt)
        self._audio.start()
        t = threading.Thread(target=self._brain_loop,
                             name="meetbot-brain", daemon=True)
        t.start()
        self._threads.append(t)

    def _teardown_pipeline(self) -> None:
        self._stop.set()
        if self._audio is not None:
            self._audio.stop()
        if self._stt is not None:
            try:
                self._stt.close()
            except Exception:  # noqa: BLE001
                pass

    def _brain_loop(self) -> None:
        """STT -> coalescing -> transcript con attribuzione del parlante."""
        assert self._stt is not None
        while not self._stop.is_set():
            try:
                first = self._stt.utterances.get(timeout=0.4)
            except queue.Empty:
                continue
            t_from = time.monotonic()
            text = coalesce_utterances(self._stt.utterances, first,
                                       self.cfg.ptt_turn_pause_s,
                                       stop=self._stop)
            t_to = time.monotonic()
            if not text.strip():
                continue
            speaker = self.timeline.dominant(t_from - 3.0, t_to)
            seg = self.transcript.add(text.strip(), speaker=speaker,
                                      spoken_from=t_from, spoken_to=t_to)
            log.info("meetbot STT [%s]: %.80s", seg["speaker"], text)
