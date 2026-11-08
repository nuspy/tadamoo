"""Conversazione vocale TURN-BASED su modem SIM800C (audio a file).

Il SIM800C non espone l'audio della chiamata su USB: l'unica via e' a **file**
(verificato il file system; CMEDPLAY/CREC presenti, da validare con la SIM che
raggiungano davvero uplink/downlink). Questo modulo orchestra un turno alla volta,
come Shift+F1:

  AI parla:   testo -> Handy TTS -> PCM -> AMR-NB -> FSWRITE -> CMEDPLAY (nella call)
  AI ascolta: CREC (finestra fissa) -> FSREAD -> AMR -> PCM 16k -> Handy STT -> testo
  cervello:   testo utente -> brain.respond_stream() -> testo risposta -> AI parla ...

NB: senza un audio live non c'e' VAD sul downlink, quindi si registra una
FINESTRA FISSA (`gsm_record_window_s`) per turno. Da rifinire quando arriva la SIM
(path esatto di CREC, modo CMEDPLAY verso l'uplink, durata finestra).
"""
from __future__ import annotations

import logging
import time

import numpy as np

from .config import Config
from .gsm_audio import dvi_to_pcm, pcm_to_amr
from .gsm_modem import GsmModem
from .stt_client import HandySttClient
from .tts_client import HandyTtsClient
from .turn_engine import configure_tts

log = logging.getLogger("callbridge.gsm.call")


class GsmCallSession:
    """Una conversazione turn-based su una chiamata GSM gia' stabilita."""

    def __init__(self, cfg: Config, modem: GsmModem, brain) -> None:
        self.cfg = cfg
        self.modem = modem
        self.brain = brain
        self._active = False
        self._ended = False
        # il modem segnala la fine chiamata via on_call_ended -> _ended
        self.voice = cfg.default_voice
        self.lang = cfg.default_lang

    # --- API alto livello -------------------------------------------------

    def run_outgoing(self, number: str, greeting: str = "Pronto?") -> None:
        log.info("GSM: chiamo %s", number)
        if not self.modem.dial(number):
            log.error("GSM: dial fallito (SIM/registrazione?). Stato: %s", self.modem.status())
            return
        # Attendiamo che risponda (best-effort: il SIM800 non da' sempre un URC
        # affidabile di "answered"; diamo qualche secondo poi partiamo).
        time.sleep(self.cfg.gsm_answer_wait_s)
        self._conversation(opening=greeting)

    def run_answer(self) -> None:
        log.info("GSM: rispondo alla chiamata in arrivo")
        if not self.modem.answer():
            log.error("GSM: answer fallito")
            return
        time.sleep(1.0)
        self._conversation(opening="Pronto?")

    # --- ciclo conversazione ---------------------------------------------

    def _conversation(self, opening: str | None) -> None:
        self._active = True
        self._ended = False
        # Paracadute: senza un URC di fine chiamata affidabile (e come difesa se
        # un NO CARRIER va perso), il loop registrerebbe/parlerebbe all'infinito
        # su una linea gia' morta. Limiti: durata massima e turni vuoti di fila.
        call_deadline = time.time() + self.cfg.gsm_max_call_s
        empty_turns = 0
        try:
            if opening:
                self._speak(opening)
            while self._active and not self._ended:
                if time.time() > call_deadline:
                    log.info("GSM: durata massima chiamata (%.0fs) raggiunta, chiudo",
                             self.cfg.gsm_max_call_s)
                    break
                user = self._listen(self.cfg.gsm_record_window_s)
                if self._ended:
                    break
                if not user:
                    empty_turns += 1
                    if empty_turns >= self.cfg.gsm_max_empty_turns:
                        log.info("GSM: %d turni vuoti consecutivi, chiudo", empty_turns)
                        break
                    log.info("GSM: nessun parlato nel turno, riascolto")
                    continue
                empty_turns = 0
                log.info("GSM utente -> %s", user)
                reply = self._think(user)
                if reply:
                    self._speak(reply)
        finally:
            self.modem.hangup()
            self._active = False
            log.info("GSM: conversazione terminata")

    def _think(self, text: str) -> str:
        """Interroga il cervello (qualsiasi tipo) e raccoglie la risposta testuale."""
        try:
            chunks = self.brain.respond_stream(text, "remote")
            return "".join(c for c in chunks if isinstance(c, str)).strip()
        except Exception as e:  # noqa: BLE001
            log.error("GSM: brain.respond_stream errore: %s", e)
            return ""

    # --- AI parla (TTS -> AMR -> CMEDPLAY) -------------------------------

    def _speak(self, text: str) -> None:
        log.info("GSM AI -> %s", text)
        tts = HandyTtsClient(self.cfg.handy_tts_url())
        try:
            tts.start()
            configure_tts(tts, self.cfg, self.voice, self.lang)
            tts.send_text(text)
            tts.end()
            segs, rate = self._drain_tts(tts)
        finally:
            tts.close()
        if not segs:
            log.warning("GSM: TTS non ha prodotto audio")
            return
        pcm = np.concatenate(segs)
        amr = pcm_to_amr(pcm, rate)
        f = self.cfg.gsm_tts_file
        if not self.modem.fs_write(f, amr):
            log.error("GSM: FSWRITE del TTS fallito")
            return
        if not self.modem.play_media(f, volume=self.cfg.gsm_play_volume):
            log.error("GSM: CMEDPLAY fallito")
            return
        # Attende la durata della clip (niente URC affidabile di fine play).
        dur = len(pcm) / float(rate)
        self._sleep_interruptible(dur + 0.5)

    def _drain_tts(self, tts: HandyTtsClient, grace: float = 1.5,
                   hard_cap: float = 30.0, empty_grace: float = 6.0):
        """Raccoglie i segmenti audio finche' la coda resta vuota per `grace` s."""
        segs: list[np.ndarray] = []
        rate = 24000
        start = time.time()
        deadline = start + hard_cap
        last = start
        while time.time() < deadline:
            try:
                seg = tts.audio.get(timeout=0.2)
            except Exception:  # queue.Empty
                now = time.time()
                if segs and (now - last) > grace:
                    break
                # Nessun segmento entro empty_grace: TTS giu'/errore, non
                # aspettare l'intero hard_cap (30 s di silenzio in chiamata).
                if not segs and (now - start) > empty_grace:
                    break
                continue
            rate = seg.sample_rate
            segs.append(np.asarray(seg.samples, dtype="f4"))
            last = time.time()
        return segs, rate

    # --- AI ascolta (CREC -> AMR -> STT) ---------------------------------

    def _listen(self, window_s: float) -> str:
        fid = self.cfg.gsm_record_id
        # canale 2 = voce dell'interlocutore (downlink)
        if not self.modem.record_start(file_id=fid, channel=2):
            log.error("GSM: CREC start fallito")
            return ""
        self._sleep_interruptible(window_s)
        self.modem.record_stop()
        path = self.modem.record_path(fid)
        dvi = self.modem.fs_read(path)
        if not dvi:
            log.warning("GSM: nessuna registrazione letta (%s)", path)
            return ""
        try:
            pcm = dvi_to_pcm(dvi, in_rate=8000, out_rate=16000)
        except Exception as e:  # noqa: BLE001
            log.error("GSM: decodifica DVI/ADPCM fallita: %s", e)
            return ""
        return self._stt(pcm)

    def _stt(self, pcm16k: np.ndarray) -> str:
        stt = HandySttClient(self.cfg.handy_stt_url())
        try:
            stt.start()
            # invia in blocchi da ~100ms
            step = 1600
            for i in range(0, len(pcm16k), step):
                stt.send_audio(pcm16k[i:i + step])
                time.sleep(0.005)
            # attende che lo STT finalizzi (sul silenzio di coda della finestra)
            time.sleep(1.2)
            parts = []
            while True:
                try:
                    parts.append(stt.utterances.get(timeout=0.3))
                except Exception:  # queue.Empty
                    break
            return " ".join(parts).strip()
        finally:
            stt.close()

    # --- util -------------------------------------------------------------

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._ended:
            time.sleep(0.1)

    def mark_ended(self) -> None:
        self._ended = True

    @property
    def ended(self) -> bool:
        return self._ended


# --- CLI di test -----------------------------------------------------------

def _main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Chiamata vocale turn-based via SIM800C")
    ap.add_argument("number", help="numero da chiamare, es. +39333xxxxxxx")
    ap.add_argument("--greeting", default="Pronto, sono l'assistente. Come posso aiutarti?")
    ap.add_argument("--brain", choices=["direct", "harness"], default="")
    args = ap.parse_args()

    cfg = Config.load()

    from .brains import make_brain
    brain = make_brain(cfg)
    if args.brain:
        brain.set_mode(args.brain)

    session = None
    modem = GsmModem(
        cfg.gsm_port, cfg.gsm_baud,
        on_call_ended=lambda: session.mark_ended() if session else None,
    )
    modem.open()
    st = modem.status()
    log.info("Stato modem: %s", st)
    if not st.get("registered"):
        log.warning("Modem NON registrato alla rete (SIM assente o PIN?). La chiamata fallira'.")
    session = GsmCallSession(cfg, modem, brain)
    try:
        session.run_outgoing(args.number, greeting=args.greeting)
    finally:
        modem.close()


if __name__ == "__main__":
    _main()
