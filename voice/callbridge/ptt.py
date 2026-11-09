"""Push-to-talk: dialogo a voce utente <-> cervello (diretto o harness, vedi brains.py) via hotkey.

L'utente preme una combinazione di tasti (default `Ctrl+Space`) per
aprire/chiudere una sessione di conversazione che usa i device audio di
**default del sistema operativo** (mic e casse del PC) — distinti
dall'endpoint Bluetooth HFP usato per le telefonate.

Premi la hotkey: parte la sessione (mic -> STT -> cervello -> TTS -> casse).
Premila di nuovo: la sessione si chiude.
"""

from __future__ import annotations

import logging
import threading
import time

from pynput import keyboard

from .audio_hfp import negotiate_sample_rate, resample
from .config import Config
from .handy_client import ensure_models_loaded
from .streaming import StreamingSession

log = logging.getLogger("callbridge.ptt")

# Lunghezza minima di una battuta accodata al worker (caratteri). Sotto questa
# soglia le frasi si accorpano: piu' corta e' la battuta, piu' probabile che
# finisca prima che la successiva sia pronta — e fra le due si sente la pausa.
# Tenuta bassa di proposito: alzandola le frasi restano in coda fino a fine
# turno, e si torna ad aspettare. 60 caratteri sono ~4 secondi di parlato,
# abbastanza per coprire la sintesi della battuta seguente.
_MIN_BATTUTA_CHARS = 60


class PushToTalkSession(StreamingSession):
    """Sessione push-to-talk: `StreamingSession` sui device di default dell'OS,
    senza watcher di stato (non c'e' una telefonata da sorvegliare)."""

    # Comandi vocali (cambia lingua / nuova conversazione) e auto-rilevamento
    # lingua abilitati nel push-to-talk (nelle telefonate restano off).
    voice_commands_enabled = True
    # Il microfono e' quello della persona: puo' rispondere a una richiesta di approvazione.
    utterance_source = "mic"


class AvatarPushToTalkSession(PushToTalkSession):
    """PushToTalkSession che mostra anche l'avatar video (MuseTalk).

    L'audio della risposta NON esce dalle casse locali: viene riprodotto dal
    worker avatar (sul suo `avatar_audio_device`) con labiale sincronizzato,
    esattamente come nelle video call. Riusa il pattern PCM->WAV->speak_wav del
    copilota (`copilot._speak_worker`). Finche' il worker non e' pronto sulla
    persona (prima preparazione: minuti) ricade sulle casse locali, cosi' la
    voce funziona subito.
    """

    # L'avatar NON cambia mai voce: quella del manifest della persona vale
    # sempre, e col cambio lingua cambia solo la lingua di sintesi.
    #
    # Prima era False, e il commento lo ammetteva: al cambio lingua una persona
    # con voce clonata passava a una voce Kokoro. Non e' solo un timbro diverso
    # da quello scelto — e' un MOTORE diverso, e i motori TTS non stanno in
    # memoria insieme con questa VRAM. Il router scaricava XTTS per caricare
    # Kokoro e viceversa, e ogni giro costa decine di secondi: misurato
    # `motore 'xtts' pronto in 70.7s`. Bastava l'auto-rilevamento della lingua
    # a farlo scattare, senza che nessuno avesse chiesto niente.
    #
    # Il motivo storico ("il timbro clonato non e' nativo nelle altre lingue")
    # non vale per XTTS v2, che e' multilingue proprio per questo. Se il motore
    # della persona non parla la lingua richiesta, `_apply_language` resta
    # nella lingua di prima invece di cambiare voce.
    keep_voice = True
    # Streaming frase-per-frase: ogni frase completa viene sintetizzata e
    # accodata SUBITO al worker, che la rende mentre il bridge prepara la
    # successiva. Prima si aspettava l'intera risposta — con un TTS a 0,9x
    # realtime erano secondi di silenzio prima che l'avatar aprisse bocca.
    # `avatar_stream_sentences=False` in config torna al vecchio comportamento
    # (una battuta sola, nessun taglio fra le frasi).
    stream_tts = True

    def __init__(self, *args, avatar=None, avatar_persona: str = "",
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stream_tts = bool(getattr(self.cfg, "avatar_stream_sentences", True))
        self.avatar = avatar
        self.avatar_persona = avatar_persona
        # Riferimento all'HfpAudioIO aperto in run(): lo stasha `_uplink` (che
        # qui non riproduce nulla), cosi' il fallback-casse puo' riusare lo
        # stesso stream di uscita invece di aprirne un secondo.
        self._audio_io = None

    def _uplink(self, audio, tts) -> None:
        # Niente riproduzione automatica sulle casse: l'audio della risposta lo
        # instrada `_deliver_response` (al worker avatar, o alle casse in
        # fallback). Qui memorizziamo solo lo stream di uscita per il fallback.
        self._audio_io = audio

    def _interrupt_tts(self, tts) -> None:
        super()._interrupt_tts(tts)
        # Le frasi in attesa appartengono alla risposta abbandonata: dirle dopo
        # l'interruzione significherebbe rispondere a una domanda superata.
        self._coda_frasi = ""
        self._battuta_inviata = False
        if self.avatar is not None:
            try:
                self.avatar.interrupt()  # barge-in: tronca anche video/audio
            except Exception as e:  # noqa: BLE001 - worker non raggiungibile
                log.warning("Avatar: interrupt (barge-in) fallito: %s", e)

    def stop(self) -> None:
        if self.avatar is not None:
            try:
                self.avatar.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning("Avatar: interrupt in chiusura fallito: %s", e)
        super().stop()

    def _collect_pcm(self, tts, attesi_caratteri: int = 0):
        """Accumula i segmenti TTS di UNA battuta in un unico buffer float32.

        Non c'e' un marker di fine-sintesi nel protocollo, quindi finora si
        usava solo il silenzio: quattro secondi di coda vuota e si chiudeva.
        Con la GPU contesa fra LLM e MuseTalk i segmenti distano molto di piu',
        la battuta si chiudeva a META' e i segmenti rimasti in coda venivano
        raccolti dalla chiamata SUCCESSIVA — sentito come "non legge tutta la
        risposta, e al turno dopo legge la parte che manca".

        Il criterio in piu': si sa quanti caratteri si sono mandati, e quanti
        caratteri valga un secondo di parlato lo misura `chunk_policy` dal
        vivo. Finche' l'audio raccolto e' molto meno di quello atteso, il
        silenzio non significa "finito" ma "sta ancora sintetizzando".

        Ritorna (pcm, rate) oppure (None, rate) se nulla e' stato prodotto."""
        import queue as _q
        import time as _time

        import numpy as _np

        # Se il client sa dire quando ha finito (`busy()`, motori nativi), lo si
        # usa: l'attesa a vuoto sotto costava 4 s FISSI per ogni battuta, che
        # con la sintesi a meno di un secondo erano quasi tutto il ritardo tra
        # "risposta pronta" e "avatar che parla". Il fallback a tempo resta per
        # i client che non lo espongono (Handy via WebSocket).
        occupato = getattr(tts, "busy", None)
        from . import chunk_policy
        # Quanto parlato ci si aspetta da questo testo. E' l'unico modo di
        # sapere se la sintesi ha finito: il protocollo non manda un "ho
        # finito" e questo client non espone `busy()`.
        attesa_s = (attesi_caratteri / chunk_policy.caratteri_al_secondo()
                    if attesi_caratteri > 0 else 0.0)
        campioni = 0
        chunks, rate, last = [], 24000, _time.monotonic()

        def _scadenza() -> float:
            """Fino a quando si resta in attesa, dall'ultimo segmento ricevuto.

            Tre casi, e vanno tenuti distinti perche' confonderli e' costato
            due difetti opposti:

            - non e' ancora arrivato NIENTE: la prima sintesi puo' essere
              lenta (motore freddo), si concede molto;
            - e' arrivato tutto quello che il testo lascia prevedere: si
              chiude subito, e infatti il ramo sotto esce prima di arrivare
              qui;
            - ne manca un pezzo: il silenzio NON significa "finito" ma "sta
              ancora sintetizzando", e si aspetta in proporzione a quanto
              manca. Un tetto fisso di due secondi non bastava con la GPU
              contesa: visto dal vivo, di una battuta da 102 caratteri se ne
              raccoglievano 1,3s e il resto finiva nel turno DOPO.
            """
            if not chunks:
                return 30.0
            manca = attesa_s - campioni / max(rate, 1)
            if attesa_s > 0 and manca > 0:
                # 4s di silenzio normale + il tempo che quel pezzo dovrebbe
                # durare, con un tetto perche' una stima sbagliata non blocchi
                # il turno.
                return min(4.0 + manca * 1.5, 20.0)
            return 4.0

        while _time.monotonic() - last < _scadenza():
            if self._stop.is_set() or self._barge_in.is_set():
                break
            try:
                seg = tts.audio.get(timeout=0.3)
            except _q.Empty:
                if chunks and callable(occupato) and not occupato():
                    break          # sintesi conclusa e coda vuota: si parte
                continue
            if seg.generation != tts.generation:
                continue  # segmenti di una risposta gia' abortita (barge-in)
            chunks.append(seg.samples)
            campioni += len(seg.samples)
            rate = seg.sample_rate
            last = _time.monotonic()
            # Arrivato tutto il previsto: si chiude SUBITO invece di stare
            # secondi ad ascoltare il silenzio — erano 4s fissi per battuta,
            # una decina per turno. Soglia alta (95%) perche' una stima
            # ottimista taglierebbe la coda di una frase.
            if attesa_s > 0 and campioni / max(rate, 1) >= attesa_s * 0.95:
                break
        if not chunks:
            return None, rate
        ottenuto = campioni / max(rate, 1)
        if attesa_s > 0 and ottenuto < attesa_s * 0.8:
            # Non e' fatale — meglio una battuta corta che un turno bloccato —
            # ma va detto: e' il sintomo di una GPU che non sta dietro.
            log.warning("TTS: battuta incompleta, %.1fs di audio contro %.1fs "
                        "attesi da %d caratteri (%.1f car/s: %s)",
                        ottenuto, attesa_s, attesi_caratteri,
                        chunk_policy.caratteri_al_secondo(),
                        "misurati" if chunk_policy.stato()["campioni_cps"]
                        else "di ripiego")
        return _np.concatenate(chunks), rate

    def _play_local(self, pcm, rate) -> None:
        """Fallback: riproduce `pcm` sulle casse locali riusando l'HfpAudioIO
        gia' aperto (come `StreamingSession._uplink`). Usato finche' il worker
        avatar non e' pronto sulla persona."""
        audio = self._audio_io
        if audio is None:
            return
        out = pcm if rate == self.audio_rate else resample(pcm, rate, self.audio_rate)
        block = audio.block_frames
        for i in range(0, len(out), block):
            if self._stop.is_set() or self._barge_in.is_set():
                break
            try:
                audio.write(out[i:i + block])
            except Exception as e:  # noqa: BLE001 - stream chiuso o errore device
                log.warning("avatar-fallback: scrittura casse fallita: %s", e)
                return

    @staticmethod
    def _rifila_coda(pcm, rate: int, margine_s: float = 0.12):
        """Toglie il silenzio in CODA alla battuta sintetizzata.

        XTTS chiude ogni clip con 0,65-0,72 s di silenzio (misurato sulle
        clip vere). Il worker riproduce una battuta per volta, quindi quel
        silenzio diventa una PAUSA fra una frase e la successiva: con tre
        battute per turno sono due secondi di vuoto che sembrano un
        tentennamento dell'avatar. Si lascia `margine_s` di respiro, cosi'
        la coda della parola non viene tagliata e la giuntura resta naturale.
        """
        import numpy as _np
        if pcm is None or len(pcm) == 0:
            return pcm
        picco = float(_np.abs(pcm).max())
        if picco <= 0:
            return pcm
        forte = _np.where(_np.abs(pcm) > picco * 0.02)[0]
        if len(forte) == 0:
            return pcm
        fine = int(forte[-1]) + int(margine_s * rate)
        return pcm[:fine] if fine < len(pcm) else pcm

    def _wav_da_pcm(self, pcm, rate: int) -> str:
        """Scrive il PCM in un wav temporaneo e ne restituisce il percorso.

        Il worker legge il file in modo ASINCRONO, quindi non si cancella
        quello corrente: si potano solo i vecchi (>5 min)."""
        import glob as _glob
        import os as _os
        import tempfile as _tempfile
        import time as _time
        import wave as _wave

        import numpy as _np

        tmp = _tempfile.gettempdir()
        cutoff = _time.time() - 300
        for old in _glob.glob(_os.path.join(tmp, "avatar_say_*.wav")):
            try:
                if _os.path.getmtime(old) < cutoff:
                    _os.remove(old)
            except OSError:
                pass
        fd, wav_path = _tempfile.mkstemp(suffix=".wav", prefix="avatar_say_")
        _os.close(fd)
        with _wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((_np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        return wav_path

    def _stream_sentence(self, tts, sentence: str) -> None:
        """Sintetizza UNA frase e la manda subito al worker, che la accoda.

        È il pezzo che toglie l'attesa: prima si sintetizzava l'intera risposta
        e solo allora l'avatar apriva bocca — con un TTS a 0,9x realtime, quattro
        secondi di silenzio per quattro di parlato. Così il labiale parte dopo
        la PRIMA frase, e le successive si sintetizzano mentre lui parla.

        Il jitter fra una frase e l'altra lo assorbe il worker, che ha già un
        prebuffer di frame e tiene l'ultimo fotogramma quando la generazione
        arranca (`hold`), invece di far scattare il video.
        """
        if not sentence.strip() or self._stop.is_set():
            return
        # Accumulo: la PRIMA battuta parte appena c'è una frase (l'attesa
        # iniziale è ciò che si voleva togliere), le successive si accorpano
        # fino a `_MIN_BATTUTA_CHARS`. Due motivi:
        #  - i motori autoregressivi allucinano sugli input corti ("I'm all
        #    yours." da solo produceva farfugliato al posto della frase);
        #  - una battuta troppo breve finisce prima che la successiva sia
        #    sintetizzata — XTTS va a 0,86x realtime, cioè più piano di quanto
        #    l'avatar parli — e fra le due si sente la pausa.
        coda = getattr(self, "_coda_frasi", "")
        coda = (coda + " " + sentence.strip()).strip() if coda else sentence.strip()
        if getattr(self, "_battuta_inviata", False) and len(coda) < _MIN_BATTUTA_CHARS:
            self._coda_frasi = coda
            return
        self._coda_frasi = ""
        self._battuta_inviata = True
        self._invia_battuta(tts, coda)

    def _invia_battuta(self, tts, testo: str) -> None:
        """Sintetizza `testo` e lo accoda al worker (o lo suona sulle casse
        se il worker non è ancora pronto sulla persona)."""
        if not testo.strip() or self._stop.is_set():
            return
        self._log_tts_sentence(testo)
        self._barge_in.clear()
        tts.send_text(testo)
        tts.flush()
        pcm, rate = self._collect_pcm(tts, len(testo))
        # Via il silenzio finale del motore: e' la pausa che si sente fra
        # una battuta e l'altra (il worker le riproduce una per volta).
        pcm = self._rifila_coda(pcm, rate)
        if pcm is None or self._stop.is_set():
            return
        self._tts_active.set()
        if self.avatar is None or not self.avatar.is_ready(self.avatar_persona):
            self._play_local(pcm, rate)     # worker non pronto: casse locali
            return
        try:
            self.avatar.speak_wav(self._wav_da_pcm(pcm, rate))
        except Exception as e:  # noqa: BLE001 - il turno non deve morire qui
            log.warning("Avatar: invio della battuta al worker fallito: %s", e)

    def _finish_streamed_tts(self, tts, stt=None) -> None:
        """Manda l'ultimo pezzo accumulato e attende che il worker abbia
        pronunciato TUTTE le battute accodate.

        Il worker segnala `idle` a fine battuta: fra una frase e l'altra ci
        passa, quindi non basta il primo `idle` — si aspetta che resti fermo.
        """
        import time as _time
        # Coda residua: l'ultima frase di un turno e' quasi sempre sotto la
        # soglia di accorpamento e resterebbe muta.
        resto = getattr(self, "_coda_frasi", "")
        if resto:
            self._coda_frasi = ""
            self._invia_battuta(tts, resto)
        self._battuta_inviata = False      # il turno dopo riparte "reattivo"
        if self.avatar is None:
            super()._finish_streamed_tts(tts, stt)
            return
        fermo_da = None
        deadline = _time.monotonic() + 300.0
        while _time.monotonic() < deadline:
            if self._stop.is_set() or self._barge_in.is_set():
                break
            parlato = self.avatar.wait_until_spoken(0.2)
            if parlato:
                # Battuta conclusa: se ne resta un'altra in coda il worker
                # ricomincia subito, quindi si concede un momento di grazia.
                fermo_da = fermo_da or _time.monotonic()
                if _time.monotonic() - fermo_da > 1.2:
                    break
            else:
                fermo_da = None
        self._tts_active.clear()

    def _deliver_response(self, tts, full: str, stt=None) -> None:
        if not full:
            return
        import glob as _glob
        import os as _os
        import tempfile as _tempfile
        import time as _time
        import wave as _wave

        import numpy as _np

        self._last_response = full
        # Testo EFFETTIVAMENTE pronunciato dall'avatar. Le sessioni avatar non
        # passano da `_stream_sentence` (accumulano e mandano un WAV unico),
        # quindi senza questo nei log non restava traccia di cosa fosse stato
        # detto: una frase spezzata male o una parola fuori contesto non erano
        # diagnosticabili a posteriori.
        log.info("Avatar dice <- %r", full[:400])
        # Nuovo turno: azzera il flag di barge-in (nel percorso base lo fa
        # `_uplink`, qui no-op) per non bloccare la raccolta/riproduzione.
        self._barge_in.clear()
        try:
            tts.send_text(full)
            tts.flush()
            pcm, rate = self._collect_pcm(tts, len(full))
            if pcm is None or self._stop.is_set():
                return
            # Inizio riproduzione: overlay "speaking" e (in half-duplex) mic muto
            # finche' non e' finita.
            self._tts_active.set()
            if self.avatar is None or not self.avatar.is_ready(self.avatar_persona):
                # Worker non pronto (prima preparazione): casse locali.
                self._play_local(pcm, rate)
                return
            # Pota i vecchi avatar_say_*.wav (>5 min): il worker li legge in modo
            # asincrono, quindi non si cancella quello corrente.
            tmp = _tempfile.gettempdir()
            cutoff = _time.time() - 300
            for old in _glob.glob(_os.path.join(tmp, "avatar_say_*.wav")):
                try:
                    if _os.path.getmtime(old) < cutoff:
                        _os.remove(old)
                except OSError:
                    pass
            fd, wav_path = _tempfile.mkstemp(suffix=".wav", prefix="avatar_say_")
            _os.close(fd)
            with _wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes((_np.clip(pcm, -1, 1) * 32767)
                              .astype("<i2").tobytes())
            self.avatar.speak_wav(wav_path)
            # Attende la fine della riproduzione (ack idle/interrupted del
            # worker); timeout di sicurezza = durata del wav + margine. L'attesa
            # e' interrompibile: in full-duplex un nuovo enunciato in coda e' un
            # barge-in -> tronca l'avatar (via _interrupt_tts override).
            deadline = _time.monotonic() + len(pcm) / float(rate or 24000) + 5.0
            while _time.monotonic() < deadline:
                if self._stop.is_set() or self.avatar.wait_until_spoken(0.1):
                    break
                if not self.half_duplex and stt is not None \
                        and not stt.utterances.empty():
                    log.info("Barge-in durante l'avatar: interrompo la risposta")
                    self._interrupt_tts(tts)
                    break
        finally:
            self._tts_active.clear()


class PushToTalkManager:
    """Ascolta la hotkey globale e apre/chiude la sessione push-to-talk."""

    def __init__(self, config: Config, brains, overlay=None,
                 copilot=None, avatar=None) -> None:
        self.cfg = config
        # brains: il `ModeBrain` di base; ogni sessione ne prende una copia
        # (`fork`) nel modo scelto dalla hotkey, dal selettore o dalla config.
        self.brains = brains
        self._brain = None      # il cervello della sessione in corso
        self.overlay = overlay  # overlay del desktop (o None se UI non disponibile)
        self.copilot = copilot  # CopilotController (o None) -> Shift+F4
        # AvatarClient CONDIVISO (uno solo per processo, vincolo VRAM): abilita
        # le hotkey Ctrl+Shift+F1/F2 con avatar. None se avatar disabilitato.
        self._avatar = avatar
        # True mentre e' attiva una sessione con avatar (per session_state).
        self._avatar_active = False
        self._session: PushToTalkSession | None = None
        # Chiusura della sessione del cervello.
        self._session_end = None
        # Mutex logico dell'avvio: il preload (ensure_models_loaded, fino a 180s)
        # avviene SENZA tenere `_lock`, cosi' hotkey ed endpoint /session/* non
        # si congelano se Handy e' lento; `_starting` impedisce avvii concorrenti.
        self._starting = False
        # Ultimo toggle da hotkey (monotonic): per il debounce anti auto-repeat.
        self._last_toggle = 0.0
        self._lock = threading.Lock()
        self._listener: keyboard.GlobalHotKeys | None = None
        # Modalita' della sessione corrente: "half" | "full" | None.
        self._mode: str | None = None

    def start(self) -> None:
        """Registra le hotkey globali (non bloccante).

        - Shift+F1 / Shift+F2: cervello HARNESS (l'agente di TaDaMoo), half-duplex
          (casse: mic muto durante risposta e TTS) e full-duplex (cuffie: ascolta
          sempre, barge-in possibile).
        - Ctrl+Shift+F1 / F2: harness con l'avatar video (solo con un avatar).
        - Ctrl+Shift+F4 / F5: cervello DIRETTO (il modello caricato, senza
          strumenti), half e full-duplex; con l'avatar se c'e'.
        """
        if not self.cfg.push_to_talk_enabled:
            # Il flag serve a evitare conflitti di registrazione (es. hotkey gia'
            # posseduta da un altro programma): rispettalo qui, non solo lato API.
            log.info("Push-to-talk disabilitato (push_to_talk_enabled=false)")
            return
        from .engines import in_sospensione
        if in_sospensione():
            # Stato su disco: dopo un riavvio le hotkey restano spente finche'
            # l'utente non preme Riprendi (che richiama start()).
            log.info("Push-to-talk NON attivato: i componenti sono sospesi")
            return
        if self._listener is not None:
            return                      # gia' attivo (es. Riprendi senza Sospendi)
        hk_f1 = self._to_pynput(self.cfg.push_to_talk_hotkey)
        hk_f2 = self._to_pynput(self.cfg.push_to_talk_hotkey_fullduplex)

        def _ctrl_down() -> bool:
            # pynput NON fa match esatto dei modificatori: con Ctrl+Shift+F1
            # scattano SIA <ctrl>+<shift>+<f1> SIA <shift>+<f1> (verificato:
            # partiva la sessione solo-voce al posto dell'avatar, e prima del
            # debounce le due callback facevano start+stop istantaneo). Le
            # hotkey SENZA Ctrl si ignorano se Ctrl e' fisicamente premuto.
            import ctypes
            VK_CONTROL = 0x11
            return bool(ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)

        hotkeys = {
            hk_f1: lambda: None if _ctrl_down() else self._toggle(
                "harness", half_duplex=True, mode="half"),
            hk_f2: lambda: None if _ctrl_down() else self._toggle(
                "harness", half_duplex=False, mode="full"),
        }
        # Ctrl+Shift+F1/F2: stessa conversazione harness MA con avatar video.
        # Solo se un AvatarClient e' disponibile (avatar_enabled=true).
        if self._avatar is not None:
            hk_av = self._to_pynput(self.cfg.push_to_talk_hotkey_avatar)
            hk_avf = self._to_pynput(self.cfg.push_to_talk_hotkey_avatar_full)
            hotkeys[hk_av] = lambda: self._toggle("harness", half_duplex=True,
                                                  mode="half", avatar=True)
            hotkeys[hk_avf] = lambda: self._toggle("harness", half_duplex=False,
                                                   mode="full", avatar=True)
        # Ctrl+Shift+F4/F5: voce DIRETTA, stesso avatar delle F1/F2. Contengono
        # Ctrl, quindi NIENTE guard _ctrl_down.
        hk_d = self._to_pynput(self.cfg.push_to_talk_hotkey_direct)
        hk_df = self._to_pynput(self.cfg.push_to_talk_hotkey_direct_full)
        _use_av = self._avatar is not None  # degrada a sola-voce se avatar off
        hotkeys[hk_d] = lambda: self._toggle("direct", half_duplex=True,
                                             mode="half", avatar=_use_av)
        hotkeys[hk_df] = lambda: self._toggle("direct", half_duplex=False,
                                              mode="full", avatar=_use_av)
        # Shift+F4: copilota da conversazione (apre/chiude la finestra). GUARD:
        # pynput fa scattare Shift+F4 anche con Ctrl+Shift+F4 (diretto) -> ignora
        # quando Ctrl e' fisicamente premuto, altrimenti F4 aprirebbe anche il copilota.
        if self.copilot is not None:
            hotkeys[self._to_pynput(self.cfg.push_to_talk_hotkey_copilot)] =                 lambda: None if _ctrl_down() else self._toggle_copilot()
        try:
            self._listener = keyboard.GlobalHotKeys(hotkeys)
            self._listener.start()
        except Exception as e:  # noqa: BLE001 - hotkey non valida o backend assente
            log.error("Push-to-talk non avviato: %s", e)
            return
        log.info("Push-to-talk attivo: %s / %s (harness, casse / cuffie), "
                 "%s / %s (diretto, casse / cuffie)%s%s",
                 self.cfg.push_to_talk_hotkey, self.cfg.push_to_talk_hotkey_fullduplex,
                 self.cfg.push_to_talk_hotkey_direct, self.cfg.push_to_talk_hotkey_direct_full,
                 (f", {self.cfg.push_to_talk_hotkey_avatar}/"
                  f"{self.cfg.push_to_talk_hotkey_avatar_full} (harness con avatar)"
                  if self._avatar is not None else ""),
                 f", {self.cfg.push_to_talk_hotkey_copilot} (copilota)" if self.copilot else "")

    @property
    def hotkeys_attive(self) -> bool:
        return self._listener is not None

    def sospendi(self) -> bool:
        """Sospendi: toglie TUTTE le hotkey (sessioni, avatar, chat avatar,
        copilota), chiude la sessione e il copilota. True se erano attive.
        Il root Tk dell'overlay resta (lo usa la dettatura per l'icona del
        microfono): senza sessioni e senza copilota non mostra nulla."""
        erano = self._listener is not None
        self.stop()
        return erano

    def riprendi(self) -> None:
        """Riprendi: ri-registra le hotkey (no-op se push_to_talk_enabled=false)."""
        self.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._stop_session()
        if self.copilot is not None:
            try:
                self.copilot._deactivate()
            except Exception as e:  # noqa: BLE001
                log.warning("Copilota: stop fallito: %s", e)

    def _toggle_copilot(self) -> None:
        if self.copilot is not None:
            self.copilot.toggle()

    # --- API programmatica (control server / GUI) -------------------------

    def start_session(self, mode: str, avatar: bool = False, brain: str = "") -> tuple[bool, str]:
        """Avvia una sessione come le hotkey, ma IDEMPOTENTE (niente toggle:
        se una sessione e' gia' attiva NON la chiude — evita che una chiamata
        API spenga la sessione appena aperta dall'utente con la hotkey).

        mode: "half" (casse) o "full" (cuffie).
        brain: "harness" o "direct"; vuoto = il modo scelto nelle impostazioni.
        avatar: True -> mostra l'avatar video.
        Ritorna (ok, dettaglio): dettaglio = mode oppure il motivo del rifiuto.
        """
        from .engines import in_sospensione
        if in_sospensione():
            return False, "suspended"
        if mode not in ("half", "full"):
            return False, f"modalita' sconosciuta: {mode}"
        if brain and brain not in ("harness", "direct"):
            return False, f"cervello sconosciuto: {brain}"
        if avatar and self._avatar is None:
            return False, "avatar_unavailable"
        with self._lock:
            if self._session is not None:
                return False, "session_active"
            if self._starting:
                return False, "starting"
            self._starting = True
        try:
            ok = self._start_session_reserved(
                brain, half_duplex=(mode != "full"), mode=mode, avatar=avatar)
        finally:
            with self._lock:
                self._starting = False
        # modelli non pronti o device audio non disponibili
        return (True, mode) if ok else (False, "start_failed")

    def stop_session(self) -> bool:
        """Chiude la sessione attiva (se c'e'). Ritorna True se c'era."""
        with self._lock:
            had = self._session is not None
            self._stop_session_locked()
        return had

    def session_state(self) -> dict:
        """Stato per la GUI: {active, mode, phase, avatar, brain}. La fase replica la
        logica dell'overlay (speaking > reasoning > listening)."""
        with self._lock:
            s = self._session
            if s is None:
                return {"active": False, "mode": None, "phase": "idle",
                        "avatar": False, "brain": None}
            return {"active": True, "mode": self._mode, "phase": s.phase,
                    "avatar": self._avatar_active, "language": s.language,
                    "brain": self._brain.mode if self._brain is not None else None}

    def current_language(self) -> str:
        """Lingua della sessione in corso ("" se non ce n'è nessuna)."""
        with self._lock:
            return self._session.language if self._session is not None else ""

    def set_language(self, code: str) -> bool:
        """Cambia la lingua della sessione in corso. False se non c'è sessione
        o la lingua non è supportata."""
        with self._lock:
            s = self._session
        return bool(s is not None and s.request_language(code))

    # --- gestione sessione -----------------------------------------------

    def _toggle(self, brain: str, half_duplex: bool, mode: str,
                avatar: bool = False) -> None:
        # Lock BREVE per decidere start/stop; il preload lento avviene fuori.
        with self._lock:
            # Debounce: l'auto-repeat della tastiera (tasto tenuto premuto) fa
            # scattare la hotkey piu' volte; la pressione ripetuta trovava la
            # sessione appena creata e la CHIUDEVA subito (start+stop nello
            # stesso secondo). Un toggle entro 1s dall'ultimo viene ignorato.
            now = time.monotonic()
            if now - self._last_toggle < 1.0:
                return
            self._last_toggle = now
            if self._session is not None:
                self._stop_session_locked()
                return
            if self._starting:
                return  # avvio gia' in corso: ignora la seconda pressione
            self._starting = True
        try:
            self._start_session_reserved(brain, half_duplex, mode=mode,
                                         avatar=avatar)
        finally:
            with self._lock:
                self._starting = False

    def _start_session_reserved(self, brain: str, half_duplex: bool,
                                mode: str | None = None,
                                avatar: bool = False) -> bool:
        """Prepara e avvia la sessione. Va chiamato con `_starting=True` gia'
        impostato e SENZA tenere `_lock`: il preload (ensure_models_loaded, fino
        a 180s) non deve bloccare le altre hotkey/endpoint. Ritorna True se
        avviata."""
        from .engines import in_sospensione
        if in_sospensione():
            log.info("Push-to-talk: sessione rifiutata, i componenti sono sospesi")
            return False
        in_idx = self.cfg.ptt_input_device   # None = device di default dell'OS
        out_idx = self.cfg.ptt_output_device
        # Assicura che i modelli TTS (e STT) siano caricati in Handy: la
        # `StreamingSession` apre subito il WebSocket /tts/stream, che fallisce
        # con TimeoutError se Kokoro/Piper non e' in VRAM -> la sessione
        # abortirebbe senza fare nulla.
        if not ensure_models_loaded(self.cfg):
            log.error("Push-to-talk: caricamento modelli fallito (Handy non pronto?)")
            return False
        try:
            rate = negotiate_sample_rate(in_idx, out_idx, self.cfg.sample_rate)
        except Exception as e:  # noqa: BLE001 - device di default non disponibili
            log.error("Push-to-talk: device audio non disponibili: %s", e)
            return False
        # Avatar: usa la persona di `avatar_persona` e la sua voce (dal manifest).
        # Se la persona non e' configurata, degrada a sessione normale (warning).
        use_avatar = bool(avatar and self._avatar is not None)
        persona = self.cfg.avatar_persona if use_avatar else ""
        if use_avatar and not persona:
            log.warning("Push-to-talk avatar richiesto ma avatar_persona vuota: "
                        "avvio senza avatar")
            use_avatar = False
        voice = self.cfg.default_voice
        lang = self.cfg.default_lang
        if use_avatar:
            # Anche il MOTORE, non solo il nome della voce: le voci clonate
            # esistono con lo stesso nome su Chatterbox, XTTS e CosyVoice, e
            # senza dirlo la persona veniva fatta parlare da un altro motore.
            voce_persona = self._avatar.voice_for(persona)
            voice = voce_persona.get("voice") or self.cfg.default_voice
            voice_engine = (voce_persona.get("engine") or "").strip()
            # Avvio del worker in background (la prima preparazione di un clip
            # puo' richiedere minuti); se e' gia' caldo sulla persona, riuso.
            if not self._avatar.is_ready(persona):
                self._avatar.start_async(persona)

        # Il cervello di QUESTA sessione: le due copie condividono i cervelli e il
        # registro degli scambi, il modo e' proprio (lo cambia un comando vocale).
        session_brain = self.brains.fork(brain)
        session_end = getattr(session_brain, "end", None)
        # Inizio sessione: contesto del cervello nuovo e personalita' dalle impostazioni.
        try:
            session_brain.begin_session(None)
        except Exception as e:  # noqa: BLE001 - non bloccare l'avvio
            log.warning("begin_session fallito: %s", e)
        if use_avatar:
            session = AvatarPushToTalkSession(
                self.cfg, session_brain, in_idx, out_idx, rate,
                voice, lang, half_duplex=half_duplex,
                voice_engine=voice_engine,
                avatar=self._avatar, avatar_persona=persona,
            )
        else:
            session = PushToTalkSession(
                self.cfg, session_brain, in_idx, out_idx, rate,
                voice, lang, half_duplex=half_duplex,
            )
        with self._lock:
            self._session = session
            self._brain = session_brain
            self._session_end = session_end
            self._avatar_active = use_avatar
            self._mode = mode or ("half" if half_duplex else "full")
            # Overlay a 3 stati: ascolto (blu) -> reasoning (ambra, a
            # registrazione finita mentre il cervello elabora) -> parla (viola,
            # mentre il TTS riproduce).
            if self.overlay is not None:
                self.overlay.set_state_fn(lambda s=session: s.phase)
        threading.Thread(
            target=self._run_session, args=(session,),
            name="ptt-session", daemon=True,
        ).start()
        log.info("Push-to-talk: sessione avviata (%s, cervello %s%s)",
                 "half-duplex" if half_duplex else "full-duplex",
                 session_brain.mode,
                 f", avatar={persona}" if use_avatar else "")
        return True

    def _run_session(self, session: PushToTalkSession) -> None:
        try:
            session.run()
        finally:
            with self._lock:
                if self._session is session:
                    self._session = None
                    self._mode = None
                    self._brain = None
                    self._avatar_active = False
                    if self.overlay is not None:
                        self.overlay.set_state_fn(lambda: "hidden")

    def _stop_session(self) -> None:
        with self._lock:
            self._stop_session_locked()

    def _stop_session_locked(self) -> None:
        if self._session is not None:
            log.info("Push-to-talk: sessione chiusa")
            self._session.stop()
            self._session = None
            self._mode = None
            self._brain = None
            self._avatar_active = False
            if self.overlay is not None:
                self.overlay.set_state_fn(lambda: "hidden")
            if self._session_end is not None:
                try:
                    self._session_end()
                except Exception as e:  # noqa: BLE001 - cervello non raggiungibile
                    log.warning("Push-to-talk: chiusura del cervello fallita: %s", e)
                self._session_end = None

    @staticmethod
    def _to_pynput(hotkey: str) -> str:
        """Converte "shift+f1" nel formato di pynput "<shift>+<f1>"."""
        special = {
            "ctrl", "alt", "shift", "cmd", "space", "tab", "esc",
            "enter", "up", "down", "left", "right",
        }
        # tasti funzione f1..f24 (pynput li vuole come <f1>, <f2>, ...)
        special |= {f"f{i}" for i in range(1, 25)}
        parts = []
        for raw in hotkey.lower().split("+"):
            key = raw.strip()
            parts.append(f"<{key}>" if key in special else key)
        return "+".join(parts)
