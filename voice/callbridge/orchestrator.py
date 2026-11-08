"""Orchestratore del call-bridge.

`CallSession` e' la sessione di una telefonata attiva (estende
`StreamingSession` aggiungendo il watcher che chiude la sessione quando la
chiamata torna IDLE). `CallBridge` gestisce il lifecycle — prepare modelli,
compone/risponde, avvia la sessione — ed e' il punto d'ingresso usato sia
dalla CLI sia dal server di controllo.
"""

from __future__ import annotations

import logging
import threading
import time

from .audio_hfp import find_hfp_devices, negotiate_sample_rate
from .config import Config
from .handy_client import HandyControlClient, HandyError
from .lang_map import voice_lang_for_number
from .phone_client import PhoneControlClient, PhoneError
from .streaming import StreamingSession

log = logging.getLogger("callbridge.orchestrator")


class CallSession(StreamingSession):
    """Sessione di una telefonata: `StreamingSession` + watcher di stato che
    chiude la sessione quando la chiamata torna IDLE."""

    def __init__(
        self,
        config: Config,
        brain,
        in_idx: int,
        out_idx: int,
        hfp_rate: int,
        voice: str,
        lang: str,
        phone: PhoneControlClient,
    ) -> None:
        super().__init__(config, brain, in_idx, out_idx, hfp_rate, voice, lang)
        self.phone = phone

    def _extra_threads(self) -> list[threading.Thread]:
        return [
            threading.Thread(
                target=self._state_watcher, name="state-watcher", daemon=True
            )
        ]

    def _state_watcher(self) -> None:
        """Chiude la sessione quando la chiamata torna IDLE."""
        while not self._stop.is_set():
            time.sleep(self.cfg.poll_status_interval_s)
            try:
                if self.phone.status().get("state") == "IDLE":
                    log.info("Chiamata terminata dall'altro capo (IDLE)")
                    self._stop.set()
                    return
            except PhoneError:
                pass  # transitorio: riprova al prossimo giro


class CallBridge:
    """Orchestratore: lifecycle delle chiamate + sessioni di streaming.

    `brain` e' il cervello (vedi brains.py): `respond_stream(text, source)`.
    """

    def __init__(self, config: Config, brain) -> None:
        self.cfg = config
        self.brain = brain
        # True mentre il watcher delle chiamate in arrivo e' fermo per la
        # sospensione (lo mostra /components/status).
        self.watcher_sospeso = False
        self.phone = PhoneControlClient(config)
        self.handy = HandyControlClient(config)
        self._session: CallSession | None = None
        self._session_busy = False  # slot prenotato durante il setup (guarda _session_lock)
        self._session_lock = threading.Lock()

    # --- comandi (usati da CLI e da control_server) ----------------------

    def prepare(self, stt_model: str | None = None, tts: bool = True) -> dict:
        """Carica i modelli in VRAM in vista di una chiamata."""
        return self.handy.load_models(stt_model=stt_model, tts=tts)

    def start_outgoing_call(
        self,
        number: str,
        voice: str | None = None,
        lang: str | None = None,
        stt_model: str | None = None,
        brain=None,
    ) -> threading.Thread:
        """Avvia una chiamata in uscita. Non bloccante: ritorna il thread driver.

        `brain` scavalca `self.brain` per QUESTA chiamata (es. l'ExternalBrain
        delle chiamate pilotate via REST)."""
        voice = voice or self.cfg.default_voice
        lang = lang or self.cfg.default_lang
        if stt_model is None:
            stt_model = self.cfg.default_stt_model

        t = threading.Thread(
            target=self._drive_outgoing_call,
            args=(number, voice, lang, stt_model, brain),
            name="call-driver",
            daemon=True,
        )
        t.start()
        return t

    def start_incoming_answer(self, ring_timeout: float = 30.0) -> threading.Thread:
        """Attende UNA chiamata in arrivo, la gestisce, poi termina (one-shot)."""

        def _drive() -> None:
            if self._wait_for_incoming(ring_timeout):
                self._handle_incoming_call()
            else:
                log.info("Nessuna chiamata in arrivo entro il timeout")

        t = threading.Thread(target=_drive, name="answer-driver", daemon=True)
        t.start()
        return t

    def start_incoming_watcher(self) -> threading.Thread:
        """Vigila in continuazione e gestisce ogni chiamata in arrivo."""

        def _watch() -> None:
            from .sospensione import in_sospensione
            log.info("Watcher chiamate in arrivo avviato")
            while True:
                if in_sospensione():
                    # Sospeso (stato su disco): non interroga il telefono e non
                    # risponde finche' l'utente non preme Riprendi.
                    self.watcher_sospeso = True
                    time.sleep(2.0)
                    continue
                self.watcher_sospeso = False
                if self._wait_for_incoming(timeout=2.0):
                    self._handle_incoming_call()

        t = threading.Thread(target=_watch, name="incoming-watcher", daemon=True)
        t.start()
        return t

    def hangup(self) -> dict:
        """Termina la chiamata corrente e ferma la sessione."""
        with self._session_lock:
            session = self._session
        if session is not None:
            session.stop()
        try:
            self.phone.hangup()
            return {"status": "ok"}
        except PhoneError as e:
            return {"status": "error", "message": str(e)}

    def status(self) -> dict:
        """Stato: sessione attiva + stato telefono + modelli Handy."""
        with self._session_lock:
            active = self._session is not None
        out: dict = {"session_active": active}
        try:
            out["phone"] = self.phone.status()
        except PhoneError as e:
            out["phone"] = {"error": str(e)}
        try:
            out["handy"] = self.handy.models_status()
        except HandyError as e:
            out["handy"] = {"error": str(e)}
        return out

    # --- driver interni ---------------------------------------------------

    def _drive_outgoing_call(
        self, number: str, voice: str, lang: str, stt_model: str | None,
        brain=None,
    ) -> None:
        try:
            # 1. COMPONI SUBITO: il caricamento dei motori (decine di secondi) non
            # deve ritardare la composizione, altrimenti il chiamato non squilla
            # per un minuto e riaggancia. La chiamata parte, i modelli si scaldano
            # mentre suona.
            try:
                self.phone.call(number)
            except PhoneError as e:
                log.error("chiamata fallita: %s", e)
                return
            log.info("Chiamata OUTGOING a %s: composta, scaldo i motori mentre squilla", number)

            # 2. carica i motori voce IN PARALLELO (mentre il telefono squilla)
            prep: dict = {"ok": False, "err": None}

            def _carica_motori() -> None:
                try:
                    r = self.handy.load_models(stt_model=stt_model, tts=True)
                    prep["ok"] = (r.get("status") != "error")
                    prep["err"] = r.get("message")
                except Exception as e:  # noqa: BLE001
                    prep["err"] = str(e)
            tload = threading.Thread(target=_carica_motori, name="call-prepare", daemon=True)
            tload.start()

            # 3. attendi che diventi attiva (l'utente risponde)
            if not self._wait_for_state("OFFHOOK", timeout=60):
                log.warning("La chiamata non e' diventata attiva entro il timeout")
                self._safe_hangup()
                return
            log.info("Chiamata OUTGOING attiva (OFFHOOK)")

            # 4. assicura i motori pronti (il caricamento parallelo deve finire)
            tload.join(timeout=120)
            if not prep["ok"]:
                log.error("motori voce non pronti: %s", prep.get("err"))
                self._safe_hangup()
                return

            # 5. risolvi i device audio HFP ORA: il canale SCO è attivo solo a
            # chiamata in corso, e gli endpoint WDM-KS del telefono compaiono/si
            # aprono solo adesso — risolverli prima (a riposo) fallisce.
            try:
                in_idx, out_idx, rate = self._resolve_devices()
            except RuntimeError as e:
                log.error("device HFP non risolti: %s", e)
                self._safe_hangup()
                return

            # 6. esegui la sessione di streaming
            self._run_session(in_idx, out_idx, rate, voice, lang,
                              direction="OUTGOING", number=number, brain=brain)
        finally:
            # un brain esterno resterebbe in attesa per sempre su un early
            # return (prepare/device/chiamata falliti): va avvisato comunque
            _ended = getattr(brain, "call_ended", None)
            if callable(_ended):
                _ended()

    def _handle_incoming_call(self) -> None:
        """Flusso completo per una chiamata in arrivo: prefisso -> lingua,
        carica i modelli, accetta, avvia la sessione."""
        with self._session_lock:
            if self._session is not None:
                log.warning("Chiamata in arrivo ignorata: sessione gia' attiva")
                return

        # 1. numero del chiamante -> lingua/voce dal prefisso internazionale
        try:
            number = self.phone.status().get("number", "")
        except PhoneError as e:
            log.error("status() fallito: %s", e)
            return
        lang, voice = voice_lang_for_number(
            number, self.cfg.default_lang, self.cfg.default_voice
        )
        log.info("Chiamata in arrivo da %s -> lingua=%s voce=%s",
                 number or "?", lang, voice)

        # 2. carica i modelli in VRAM
        try:
            result = self.handy.load_models(tts=True)
            if result.get("status") == "error":
                log.error("prepare fallito: %s", result.get("message"))
                return
        except HandyError as e:
            log.error("prepare fallito: %s", e)
            return

        # 3. risolvi i device audio HFP
        try:
            in_idx, out_idx, rate = self._resolve_devices()
        except RuntimeError as e:
            log.error("device HFP non risolti: %s", e)
            return

        # 4. accetta la chiamata
        try:
            self.phone.answer()
        except PhoneError as e:
            log.error("answer fallito: %s", e)
            return
        if not self._wait_for_state("OFFHOOK", timeout=15):
            log.warning("La chiamata non e' diventata attiva dopo answer()")
            self._safe_hangup()
            return

        # 5. esegui la sessione di streaming
        self._run_session(in_idx, out_idx, rate, voice, lang,
                          direction="INCOMING", number=number)

    def _wait_for_incoming(self, timeout: float | None) -> bool:
        """Attende RINGING + direction INCOMING. `timeout=None` = indefinito."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is None or time.monotonic() < deadline:
            try:
                st = self.phone.status()
            except PhoneError:
                time.sleep(self.cfg.poll_status_interval_s)
                continue
            if st.get("state") == "RINGING" and st.get("direction") == "INCOMING":
                return True
            time.sleep(self.cfg.poll_status_interval_s)
        return False

    def _run_session(
        self,
        in_idx: int,
        out_idx: int,
        rate: int,
        voice: str,
        lang: str,
        *,
        direction: str,
        number: str = "",
        brain=None,
    ) -> None:
        """Avvia una sessione di streaming.

        `brain` scavalca `self.brain` per questa sessione (chiamate esterne).
        """
        # Prenota lo slot sessione ATOMICAMENTE prima di qualunque lavoro: una
        # outgoing (dal control server) e una incoming (dal watcher) concorrenti
        # passerebbero entrambe il check "sessione attiva" fatto altrove e la
        # seconda sovrascriverebbe `_session`, rendendo la prima non piu'
        # raggiungibile da hangup()/status().
        with self._session_lock:
            if self._session is not None or self._session_busy:
                log.warning("Sessione gia' attiva/in avvio: abbandono la nuova (%s)",
                            direction)
                return
            self._session_busy = True

        session: CallSession | None = None
        try:
            brain_for_session = brain if brain is not None else self.brain

            session = CallSession(
                self.cfg, brain_for_session, in_idx, out_idx, rate, voice, lang,
                self.phone)
            with self._session_lock:
                self._session = session
            session.run()
        except Exception as e:  # noqa: BLE001
            # Senza questo guard un'eccezione (es. device HFP sparito) lascerebbe
            # la telefonata attiva senza bridge e senza riaggancio.
            log.error("Sessione %s fallita: %s", direction, e)
            self._safe_hangup()
        finally:
            with self._session_lock:
                if self._session is session:
                    self._session = None
                self._session_busy = False

    def _resolve_devices(self) -> tuple[int, int, int]:
        in_idx = self.cfg.hfp_input_device
        out_idx = self.cfg.hfp_output_device
        if in_idx is None or out_idx is None:
            # Il nome BT arriva dal telefono stesso (GET /identify): lega
            # l'endpoint audio al telefono che sta davvero gestendo la chiamata.
            auto_in, auto_out = find_hfp_devices(self.phone.bt_name)
            in_idx = auto_in if in_idx is None else in_idx
            out_idx = auto_out if out_idx is None else out_idx
        rate = negotiate_sample_rate(in_idx, out_idx, self.cfg.sample_rate)
        return in_idx, out_idx, rate

    def _wait_for_state(self, target: str, timeout: float) -> bool:
        """Polla /status finche' lo stato == `target`. False su timeout, o su
        IDLE ma solo DOPO che la chiamata e' partita.

        Subito dopo `phone.call()` lo stato e' ancora IDLE per un istante (il
        TelecomManager non l'ha ancora portato a DIALING): trattare quell'IDLE
        iniziale come "caduta" faceva abortire la chiamata nello stesso secondo.
        Si aspetta di vedere DIALING/RINGING/OFFHOOK prima di considerare IDLE
        una chiusura."""
        deadline = time.monotonic() + timeout
        partita = False
        while time.monotonic() < deadline:
            try:
                state = self.phone.status().get("state")
            except PhoneError as e:
                log.error("status() fallito: %s", e)
                state = None
            if state == target:
                return True
            if state in ("DIALING", "RINGING", "OFFHOOK"):
                partita = True
            if target == "OFFHOOK" and state == "IDLE" and partita:
                return False
            time.sleep(self.cfg.poll_status_interval_s)
        return False

    def _safe_hangup(self) -> None:
        try:
            self.phone.hangup()
        except PhoneError as e:
            log.error("hangup() fallito: %s", e)
