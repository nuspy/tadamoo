"""Selezione automatica del trasporto per le chiamate.

Due trasporti implementati:
  - **gsm**     : modem USB SIM800C su seriale (preferito) — vedi gsm_call.py
  - **android** : telefono Android via Bluetooth-HFP + app di controllo (fallback)
                  — vedi orchestrator.CallBridge

Il router RILEVA cosa è disponibile e sceglie da solo: preferenza al chip GSM,
ripiego sul telefono Android. La scelta è ri-valutata ad ogni chiamata (un
dispositivo può essere collegato/scollegato a runtime). Politica configurabile
con `call_transport`: "auto" (default) | "gsm" | "android" | "off".

"Disponibile" non basta: deve essere **in grado di chiamare** (GSM = registrato
alla rete con SIM; Android = app di controllo raggiungibile).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .config import Config
from .phone_client import PhoneControlClient

log = logging.getLogger("callbridge.transport")

try:
    import serial
    from serial.tools import list_ports
    _HAVE_SERIAL = True
except Exception:  # noqa: BLE001
    _HAVE_SERIAL = False


@dataclass
class TransportInfo:
    name: str
    present: bool = False          # dispositivo/connessione rilevati
    can_call: bool = False         # davvero in grado di piazzare una chiamata
    detail: dict = field(default_factory=dict)


# --- rilevamento GSM --------------------------------------------------------

def _probe_modem(dev: str, baud: int) -> bool:
    """True se su `dev` c'è qualcosa che risponde 'OK' a un AT."""
    try:
        # write_timeout: senza, su una seriale Bluetooth col dispositivo assente
        # la write dell'AT resta appesa per sempre — e con lei la creazione del
        # control server (:8770 mai in ascolto, GUI che non legge ne' salva).
        s = serial.Serial(dev, baud, timeout=0.5, write_timeout=0.5)
    except Exception:  # noqa: BLE001 - porta occupata/inesistente
        return False
    try:
        s.reset_input_buffer()
        s.write(b"AT\r")
        time.sleep(0.3)
        return b"OK" in s.read(128)
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def find_gsm_port(preferred: str | None, baud: int) -> str | None:
    """Cerca la porta del modem: prima `preferred`, poi i CH340 (VID 1A86),
    poi le altre seriali non-Bluetooth. Ritorna la prima che risponde all'AT."""
    if not _HAVE_SERIAL:
        return None
    ports = list(list_ports.comports())
    names = {p.device for p in ports}
    ordered: list[str] = []
    if preferred and preferred in names:
        ordered.append(preferred)
    for p in ports:                      # CH340 prima
        if p.device not in ordered and (p.vid == 0x1A86 and p.pid == 0x7523):
            ordered.append(p.device)
    for p in ports:                      # poi le altre seriali (no Bluetooth)
        if p.device not in ordered and "Bluetooth" not in (p.description or ""):
            ordered.append(p.device)
    for dev in ordered:
        if _probe_modem(dev, baud):
            return dev
    return None


def detect_gsm(cfg: Config, existing=None) -> TransportInfo:
    """Rileva il modem GSM. Se `existing` (GsmModem già aperto) è dato, ne usa
    lo stato senza riaprire la seriale."""
    if existing is not None:
        try:
            st = existing.status()
            return TransportInfo("gsm", True, bool(st.get("registered")),
                                 {"port": cfg.gsm_port, **st})
        except Exception as e:  # noqa: BLE001
            return TransportInfo("gsm", False, False, {"error": str(e)})
    port = find_gsm_port(cfg.gsm_port, cfg.gsm_baud)
    if not port:
        return TransportInfo("gsm", False, False, {"reason": "nessun modem su seriale"})
    from .gsm_modem import GsmModem
    m = GsmModem(port, cfg.gsm_baud)
    try:
        m.open()
        st = m.status()
        return TransportInfo("gsm", True, bool(st.get("registered")),
                             {"port": port, **st})
    except Exception as e:  # noqa: BLE001
        return TransportInfo("gsm", True, False, {"port": port, "error": str(e)})
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001
            pass


def detect_android(cfg: Config) -> TransportInfo:
    """Rileva il telefono Android via raggiungibilità dell'app di controllo."""
    phone = PhoneControlClient(cfg)
    try:
        ok = bool(phone.health())
    except Exception:  # noqa: BLE001
        ok = False
    return TransportInfo("android", ok, ok, {"host": cfg.base_url()})


def select(cfg: Config, existing_gsm=None) -> tuple[str | None, dict[str, TransportInfo]]:
    """Rileva e sceglie il trasposto. Ritorna (selezionato|None, {info per nome})."""
    mode = (cfg.call_transport or "auto").lower()
    if mode == "off":
        return None, {}
    infos: dict[str, TransportInfo] = {}
    if mode in ("auto", "gsm"):
        infos["gsm"] = detect_gsm(cfg, existing_gsm)
    if mode in ("auto", "android"):
        infos["android"] = detect_android(cfg)

    if mode == "gsm":
        sel = "gsm" if infos["gsm"].can_call else None
    elif mode == "android":
        sel = "android" if infos["android"].can_call else None
    else:  # auto: preferenza GSM, fallback Android
        if infos.get("gsm") and infos["gsm"].can_call:
            sel = "gsm"
        elif infos.get("android") and infos["android"].can_call:
            sel = "android"
        else:
            sel = None
    return sel, infos


# --- router -----------------------------------------------------------------

class CallRouter:
    """Instrada chiamate in uscita/arrivo sul trasporto auto-selezionato."""

    def __init__(self, cfg: Config, bridge, brain_factory) -> None:
        self.cfg = cfg
        self.bridge = bridge                 # CallBridge (android)
        self.brain_factory = brain_factory   # () -> cervello per il GSM
        self._gsm_modem = None
        self._gsm_session = None
        self._selected: str | None = None
        self._infos: dict[str, TransportInfo] = {}
        self._lock = threading.Lock()

    def setup(self) -> None:
        """Rilevamento iniziale + apertura del modem se GSM è il preferito."""
        self.refresh()
        if self._selected == "gsm":
            try:
                self._ensure_gsm_open()
            except Exception as e:  # noqa: BLE001
                log.error("GSM: apertura modem fallita: %s", e)
        log.info("Trasporto chiamate selezionato: %s", self._selected or "nessuno")
        for name, info in self._infos.items():
            log.info("  %-7s presente=%s puo_chiamare=%s %s",
                     name, info.present, info.can_call,
                     info.detail.get("reason") or info.detail.get("operator") or "")

    def refresh(self) -> str | None:
        # Non sondare la seriale (select manda AT di probe) mentre una chiamata
        # GSM e' attiva: i comandi si interlaccerebbero con la sessione in corso.
        if self._gsm_busy():
            return self._selected
        sel, infos = select(self.cfg, self._gsm_modem)
        with self._lock:
            self._selected = sel
            self._infos = infos
        return sel

    # --- GSM lifecycle ----------------------------------------------------

    def _gsm_busy(self) -> bool:
        """True se c'e' una sessione GSM in corso (non ancora terminata)."""
        s = self._gsm_session
        return s is not None and not s.ended

    def _ensure_gsm_open(self):
        with self._lock:
            if self._gsm_modem is not None:
                return self._gsm_modem
            from .gsm_modem import GsmModem
            info = self._infos.get("gsm")
            port = (info.detail.get("port") if info else None) or self.cfg.gsm_port
            m = GsmModem(port, self.cfg.gsm_baud,
                         on_incoming_call=self._gsm_incoming,
                         on_call_ended=self._gsm_ended)
            m.open()
            self._gsm_modem = m
            log.info("GSM: modem aperto su %s (preferito)", port)
            return m

    def _gsm_incoming(self, number: str | None) -> None:
        log.info("GSM: chiamata in arrivo da %s", number or "sconosciuto")
        from .sospensione import in_sospensione
        if in_sospensione():
            log.info("GSM: chiamata in arrivo NON risposta, componenti sospesi")
            return
        if not self.cfg.gsm_auto_answer or self._gsm_modem is None:
            return
        from .gsm_call import GsmCallSession
        with self._lock:
            if self._gsm_busy():
                log.warning("GSM: chiamata in arrivo ignorata, sessione gia' attiva")
                return
            s = GsmCallSession(self.cfg, self._gsm_modem, self.brain_factory())
            self._gsm_session = s

        def _run() -> None:
            try:
                s.run_answer()
            finally:
                s.mark_ended()

        threading.Thread(target=_run, name="gsm-answer", daemon=True).start()

    def _gsm_ended(self) -> None:
        if self._gsm_session is not None:
            self._gsm_session.mark_ended()

    # --- API ---------------------------------------------------------------

    def place_call(self, number: str, voice=None, lang=None, greeting=None,
                   force: str | None = None, brain=None) -> dict:
        """Piazza una chiamata sul trasporto scelto (ri-rileva al momento).

        `brain` scavalca il cervello di default PER QUESTA chiamata (es.
        l'ExternalBrain delle chiamate pilotate via REST); se ha un
        `call_ended()`, viene avvisato a fine chiamata."""
        sel = force or self.refresh()
        if sel == "gsm":
            from .gsm_call import GsmCallSession
            if self._gsm_busy():
                return {"status": "busy", "transport": "gsm",
                        "message": "una chiamata GSM e' gia' in corso"}
            try:
                self._ensure_gsm_open()
            except Exception as e:  # noqa: BLE001
                return {"status": "error", "transport": "gsm", "message": str(e)}
            s = GsmCallSession(self.cfg, self._gsm_modem,
                               brain or self.brain_factory())
            with self._lock:
                if self._gsm_busy():
                    return {"status": "busy", "transport": "gsm",
                            "message": "una chiamata GSM e' gia' in corso"}
                self._gsm_session = s

            def _run() -> None:
                try:
                    s.run_outgoing(number, greeting=greeting or "Pronto?")
                finally:
                    s.mark_ended()
                    _ended = getattr(brain, "call_ended", None)
                    if callable(_ended):
                        _ended()

            threading.Thread(target=_run, name="gsm-call", daemon=True).start()
            return {"status": "calling", "transport": "gsm", "number": number}
        if sel == "android":
            self.bridge.start_outgoing_call(number, voice=voice, lang=lang,
                                            brain=brain)
            return {"status": "calling", "transport": "android", "number": number}
        return {"status": "error", "message": "nessun trasporto disponibile",
                "detail": self.status()}

    def hangup(self) -> dict:
        # riaggancia su entrambi, per sicurezza
        self._gsm_ended()
        if self._gsm_modem is not None:
            try:
                self._gsm_modem.hangup()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.bridge.hangup()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "ok"}

    def status(self) -> dict:
        with self._lock:
            return {
                "selected": self._selected,
                "mode": (self.cfg.call_transport or "auto"),
                "transports": {k: vars(v) for k, v in self._infos.items()},
            }
