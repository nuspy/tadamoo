"""Dettatura vocale globale (come Handy): hotkey PUSH-TO-HOLD -> STT -> incolla.

TIENI PREMUTA la hotkey (default Alt+Spazio): registra dal microfono di sistema
finche' resta premuta; al RILASCIO whisper trascrive e il testo viene incollato
nell'app in primo piano (clipboard + Ctrl+V). NON passa dal cervello: e' pura
dettatura (voce -> testo digitato), come la trascrizione di Handy.

Alt+Spazio e' una scorciatoia di sistema Windows (menu della finestra): su
Windows la si sopprime con un hook low-level proprio, senza iniettare eventi
sintetici. Hotkey, appunti e incolla passano dal livello `desktop` (Windows e
Linux X11); questo modulo non conosce il sistema.

La trascrizione usa l'UNICO STT del sistema (`engines/stt_shared`): un processo
whisper separato (`engines/dictation_stt_helper`), lo stesso che serve le
sessioni voce, il push-to-talk e /stt/stream. Sta in un processo suo perche'
in-process divideva il GIL con server, discovery del telefono, avatar e
copilota, e bastava un thread Python occupato per rendere whisper lento di un
ordine di grandezza (vedi l'helper). La dettatura e' SEMPRE attiva e calda:
ne' "Sospendi" ne' alcun altro percorso scarica lo STT.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .config import Config
from .desktop import Binding, Desktop, DesktopUnsupported, KeyHook, parse_hotkey, platform
from .stt_client import STT_RATE

log = logging.getLogger("callbridge.dictation")

# Gestore attivo nel processo: lo raggiungono gli endpoint /components/*.
_current: "DictationManager | None" = None

def current() -> "DictationManager | None":
    return _current


def _open_microphone(callback):
    """Flusso di ingresso del microfono di sistema (mono, STT_RATE)."""
    import sounddevice as sd
    return sd.InputStream(samplerate=STT_RATE, channels=1, dtype="float32",
                          callback=callback)


class DictationManager:
    """Hotkey push-to-hold di dettatura: registra mentre premuta, trascrive al rilascio."""

    def __init__(self, cfg: Config, overlay=None, desktop: Desktop | None = None,
                 open_stream=_open_microphone) -> None:
        global _current
        self.cfg = cfg
        self._overlay = overlay          # overlay del desktop: icona + livello voce
        self._desk = desktop or platform()
        self._open_stream = open_stream  # callback -> flusso con start/stop/close
        self._recording = False
        self._frames: list[np.ndarray] = []
        self._stream = None
        self._lock = threading.Lock()
        self._hook: KeyHook | None = None
        self._unsupported = ""           # perche' la dettatura non puo' partire qui
        self._why_off = ""               # perche' e' ferma (disabilitata, hotkey non valida, hook non avviato)
        # L'UNICO STT del sistema (lo stesso delle sessioni voce): non ne
        # esiste piu' uno della dettatura.
        from .engines import _get_stt_engine
        self._stt = _get_stt_engine(cfg)
        self.last: dict | None = None    # tempi dell'ultima dettatura (per la GUI)
        _current = self

    # --- ciclo di vita ---------------------------------------------------

    @property
    def active(self) -> bool:
        return self._hook is not None

    def start(self, force: bool = False) -> bool:
        """Installa la hotkey e carica il modello in background.

        `force` ignora `dictation_enabled` (avvio esplicito dalla GUI)."""
        if self._hook is not None:
            return True
        self._why_off = ""
        if not self.cfg.dictation_enabled and not force:
            self._why_off = "disabilitata (dictation_enabled=false)"
            log.info("Dettatura disabilitata (dictation_enabled=false)")
            return False
        hk = self.cfg.dictation_hotkey
        mods, key = parse_hotkey(hk)
        if key is None:
            self._why_off = f"hotkey {hk!r} senza tasto principale valido"
            log.error("Dettatura: hotkey %r senza tasto principale valido", hk)
            return False
        try:
            hook = self._desk.key_hook(
                "dettatura", [Binding(mods, key, self._begin, self._end)])
        except DesktopUnsupported as e:
            self._unsupported = str(e)
            log.error("Dettatura non disponibile: %s", e)
            return False
        if not hook.start():
            self._why_off = f"hook non avviato per la hotkey {hk!r}"
            log.error("Dettatura: hook non avviato per hotkey %r", hk)
            return False
        self._hook = hook
        threading.Thread(target=self._carica_stt, name="dictation-stt-load",
                         daemon=True).start()
        log.info("Dettatura attiva: %s (TIENI premuto per dettare, rilascia per "
                 "trascrivere e incollare)", hk)
        return True

    def _carica_stt(self) -> None:
        try:
            self._stt.load()       # idempotente: se e' gia' caldo non fa nulla
        except Exception:  # noqa: BLE001 - stato ed errore sono gia' nello STT
            pass

    def stop(self) -> None:
        """Toglie la sola hotkey (uscita del bridge). NON tocca lo STT: e' unico,
        serve anche alle sessioni voce e deve restare sempre caldo."""
        if self._hook is not None:
            self._hook.stop()
            self._hook = None
        with self._lock:
            self._recording = False
            self._close_stream()
            self._frames = []
        self._set_overlay(False)
        log.info("Dettatura: hotkey rimossa")

    def status(self) -> dict:
        if self._hook is None:
            state = ("unsupported" if self._unsupported
                     else "disabled" if not self.cfg.dictation_enabled else "stopped")
        elif self._stt.state == "running":
            state = "running"
        elif self._stt.state == "error":
            state = "error"
        else:
            state = "loading"
        return {
            "state": state,
            "active": self._hook is not None,
            "hotkey": self.cfg.dictation_hotkey,
            "model": self._stt.info.get("model") or self.cfg.stt_model,
            "device": self._stt.info.get("device"),
            "precision": self._stt.info.get("precision"),
            "vram_mb": self._stt.info.get("vram_mb"),
            "pid": self._stt.pid,
            "detail": self._unsupported or (self._why_off if self._hook is None else "") or self._stt.error,
            "last": self.last,
        }

    # --- overlay -----------------------------------------------------------

    def _set_overlay(self, active: bool, level: float = 0.0,
                     phase: str = "recording") -> None:
        if self._overlay is None:
            return
        try:
            self._overlay.set_dictation(active, level, phase)
        except Exception:  # noqa: BLE001 - l'overlay non deve rompere la dettatura
            pass

    # --- registrazione push-to-hold -------------------------------------

    def _begin(self) -> None:
        """Callback di pressione: apre il microfono e inizia a registrare."""
        with self._lock:
            if self._recording:
                return
            self._frames = []

            def cb(indata, frames, t, status):  # noqa: ANN001 - firma sounddevice
                mono = indata[:, 0]
                self._frames.append(mono.copy())
                # Livello voce per l'overlay: RMS con guadagno percettivo.
                rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)) if mono.size else 0.0)
                self._set_overlay(True, min(1.0, rms * 12.0))

            try:
                self._stream = self._open_stream(cb)
                self._stream.start()
            except Exception as e:  # noqa: BLE001 - microfono non disponibile
                log.error("Dettatura: apertura microfono fallita: %s", e)
                self._stream = None
                return
            self._recording = True
            self._set_overlay(True, 0.0)
            log.info("Dettatura: in ascolto (rilascia per trascrivere)")

    def _end(self) -> None:
        """Callback di rilascio: chiude il microfono e trascrive."""
        with self._lock:
            if not self._recording:
                self._set_overlay(False)
                return
            self._recording = False
            self._close_stream()
            frames = self._frames
            self._frames = []
        if not frames:
            self._set_overlay(False)
            log.info("Dettatura: nessun audio catturato")
            return
        audio = np.concatenate(frames)
        dur = len(audio) / STT_RATE
        if dur < 0.3:
            self._set_overlay(False)
            log.info("Dettatura: registrazione troppo breve (%.2fs)", dur)
            return
        self._set_overlay(True, 0.0, "transcribing")
        # Trascrizione + incolla fuori dal thread dei callback (non bloccarlo).
        threading.Thread(target=self._transcribe_and_paste, args=(audio,),
                         name="dictation-stt", daemon=True).start()

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    def _transcribe_and_paste(self, audio: np.ndarray) -> None:
        from .engines.whisper_stt import _e_allucinazione
        dur = len(audio) / STT_RATE
        t0 = time.perf_counter()
        try:
            text, ms = self._stt.transcribe_timed(audio, self.cfg.stt_language or None)
        except Exception as e:  # noqa: BLE001 - errore trascrizione
            self._set_overlay(False)
            log.error("Dettatura: trascrizione fallita: %s", e)
            return
        totale = time.perf_counter() - t0
        self._set_overlay(False)
        # Tempi nel log: senza, "la dettatura e' lenta" non si distingueva da
        # "ho parlato a lungo" (c'erano solo pressione e risultato).
        self.last = {"audio_s": round(dur, 1), "stt_ms": ms,
                     "total_ms": int(totale * 1000),
                     "xrt": round(dur / max(totale, 1e-3), 1)}
        log.info("Dettatura: %.1fs di audio trascritti in %d ms (whisper %d ms, %.1fx realtime)",
                 dur, int(totale * 1000), ms, dur / max(totale, 1e-3))
        if not text:
            log.info("Dettatura: trascrizione vuota")
            return
        if _e_allucinazione(text):
            # Frase-fantasma di whisper sul silenzio: meglio niente che
            # "Sottotitoli a cura di..." incollato nel documento.
            log.info("Dettatura: scartata frase-fantasma (%r)", text[:60])
            return
        log.info("Dettatura -> %s", text)
        self._paste(text)

    # --- incolla nel testo attivo ---------------------------------------

    def _paste(self, text: str) -> None:
        """Incolla `text` nell'app in primo piano via clipboard + tasto di incolla
        (Ctrl+V, o quello dei terminali).

        Con `dictation_keep_clipboard` (predefinito) il testo dettato RESTA negli
        appunti come dopo un Ctrl+C: si puo' incollare di nuovo e la cronologia
        degli appunti lo registra. Altrimenti i passaggi sono marcati "da non
        registrare" e il contenuto precedente (testo o immagine) viene rimesso."""
        clip = self._desk.clipboard
        keep = self.cfg.dictation_keep_clipboard
        prev = None if keep else clip.read(honor_exclude=False)
        if not clip.write_text(text, exclude=not keep):
            return
        try:
            time.sleep(0.05)
            clip.send_paste()
        except Exception as e:  # noqa: BLE001 - invio tasti fallito
            log.error("Dettatura: invio del tasto di incolla fallito: %s", e)
        if prev is not None and prev[0] in ("text", "image"):
            def restore() -> None:
                time.sleep(0.4)
                if prev[0] == "text":
                    clip.write_text(prev[1], exclude=True)
                else:
                    clip.write_image(prev[1], exclude=True)
            threading.Thread(target=restore, daemon=True).start()
