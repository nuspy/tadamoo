"""Client HTTP per l'app Android di controllo chiamate.

Mappa 1:1 la REST API esposta da `ControlService` (vedi android-controller/).

L'indirizzo del telefono non e' fisso: cambia a ogni riconnessione al Wi-Fi.
Il client parte da quello in configurazione ma, se non risponde, cerca il
telefono da solo (vedi `phone_discovery`) e riprova la richiesta: chi lo usa
non deve accorgersi di niente.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .config import Config
from .phone_discovery import PhoneEndpoint, discover

log = logging.getLogger("callbridge.phone")

# Pausa tra una discovery fallita e la successiva, per le richieste PASSIVE
# (stato/salute interrogati in loop). Raddoppia a ogni fallimento fino al tetto.
# Senza, col telefono spento il bridge rifaceva la cascata completa (adb, mDNS,
# broadcast, scansione della /24 con 254 thread per rete) a ogni giro
# dell'attesa chiamate: 25.584 volte in una settimana, e quel carico di thread
# Python si mangiava il GIL del processo rallentando tutto il resto, la
# dettatura per prima.
_BACKOFF_MIN_S = 30.0
_BACKOFF_MAX_S = 600.0


class PhoneError(RuntimeError):
    """Errore di comunicazione o di azione col telefono."""


class PhoneUnreachable(PhoneError):
    """Il telefono non risponde all'indirizzo corrente (candidato a re-discovery)."""


class PhoneControlClient:
    """Telecomando del telefono gateway: componi / rispondi / riaggancia / stato."""

    def __init__(self, config: Config, timeout: float = 10.0) -> None:
        self._cfg = config
        self._base = config.base_url()
        self._timeout = timeout
        self._headers = {"Authorization": f"Bearer {config.auth_token}"}
        self._endpoint: PhoneEndpoint | None = None
        self._lock = threading.Lock()
        self._fallite = 0                 # discovery fallite di fila
        self._prossima_discovery = 0.0    # monotonic: prima di allora, niente cascata

    # --- identita' del telefono -------------------------------------------

    @property
    def endpoint(self) -> PhoneEndpoint | None:
        """L'ultimo telefono individuato, se la discovery ha gia' girato."""
        return self._endpoint

    @property
    def bt_name(self) -> str:
        """Nome Bluetooth del telefono, chiave per trovare il suo endpoint HFP.

        E' il pezzo che rende il sistema indipendente dal modello: su Windows
        gli endpoint del vivavoce si chiamano "<nome BT> Hands-Free HF Audio".
        Un valore in configurazione, se c'e', ha la precedenza (permette di
        forzare la mano quando il telefono non sa dirlo).
        """
        forzato = getattr(self._cfg, "phone_bt_name", "")
        if forzato:
            return forzato
        return self._endpoint.bt_name if self._endpoint else ""

    def locate(self, force: bool = False) -> PhoneEndpoint | None:
        """Cerca il telefono sulla rete e ne adotta l'indirizzo.

        Con `force` la cache viene ignorata: serve quando l'indirizzo noto ha
        appena smesso di rispondere ed e' proprio quello da non riprovare.
        """
        with self._lock:
            ep = discover(
                token=self._cfg.auth_token,
                adb_path=getattr(self._cfg, "adb_exe", ""),
                want_id=getattr(self._cfg, "phone_id", ""),
                use_cache=not force,
            )
            if ep:
                self._endpoint = ep
                self._base = ep.base_url()
                self._fallite, self._prossima_discovery = 0, 0.0
                log.info("telefono agganciato: %s", ep.describe())
            else:
                self._fallite += 1
                pausa = min(_BACKOFF_MAX_S,
                            _BACKOFF_MIN_S * 2 ** (self._fallite - 1))
                self._prossima_discovery = time.monotonic() + pausa
                log.info("telefono non trovato: prossima ricerca automatica tra "
                         "%.0fs (prima se si chiama)", pausa)
            return ep

    # --- trasporto ---------------------------------------------------------

    def _get(self, path: str, passivo: bool = False) -> dict:
        return self._request("GET", path, passivo=passivo)

    def _post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict | None = None,
                 riprova: bool = True, passivo: bool = False) -> dict:
        """`passivo`: richiesta da polling (stato, salute). Se il telefono non
        risponde, la discovery parte solo fuori dalla pausa di backoff; le
        azioni dell'utente (chiama/rispondi/riaggancia) la fanno sempre."""
        url = f"{self._base}{path}"
        try:
            resp = requests.request(
                method, url, json=body, headers=self._headers, timeout=self._timeout
            )
        except requests.RequestException as e:
            # Irraggiungibile: puo' solo essere cambiato indirizzo. Lo cerchiamo
            # e riproviamo una volta sola, per non entrare in un ciclo quando il
            # telefono e' davvero spento.
            cerca = riprova and getattr(self._cfg, "phone_auto_discover", True)
            if cerca and passivo and time.monotonic() < self._prossima_discovery:
                cerca = False
            if cerca:
                log.info("%s non risponde, cerco il telefono sulla rete...", self._base)
                if self.locate(force=True):
                    return self._request(method, path, body, riprova=False)
            raise PhoneUnreachable(
                f"{method} {path}: connessione fallita ({e})"
            ) from e

        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        if not resp.ok:
            raise PhoneError(f"{method} {path}: {resp.status_code} {data}")
        return data

    # --- API pubblica -----------------------------------------------------

    def health(self) -> bool:
        """True se il servizio sul telefono risponde."""
        try:
            return self._get("/health", passivo=True).get("status") == "ok"
        except PhoneError:
            return False

    def identify(self) -> dict:
        """Carta d'identita' del telefono: modello, nome Bluetooth, id."""
        return self._get("/identify")

    def status(self) -> dict:
        """Stato chiamata: {'state': 'IDLE|RINGING|OFFHOOK', 'number': ..., 'since': ...}."""
        return self._get("/status", passivo=True)

    def call(self, number: str) -> dict:
        """Avvia una chiamata cellulare verso `number`."""
        log.info("call -> %s", number)
        return self._post("/call", {"number": number})

    def answer(self) -> dict:
        """Risponde alla chiamata in arrivo."""
        log.info("answer")
        return self._post("/answer")

    def hangup(self) -> dict:
        """Termina la chiamata corrente."""
        log.info("hangup")
        return self._post("/hangup")
