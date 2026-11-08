"""Client per il canale di controllo di Handy (`/models/*`).

L'orchestrator usa questo client per chiedere a Handy di caricare/scaricare i
modelli STT/TTS in VRAM e per interrogare le voci/lingue disponibili.
Lo streaming audio vero e proprio passa invece dai WebSocket (vedi
`stt_client.py` / `tts_client.py`).
"""

from __future__ import annotations

import logging

import requests

from .config import Config

log = logging.getLogger("callbridge.handy")


def _scalda_solo_stt(config: Config) -> bool:
    """Durante la sospensione: carica (se manca) il solo STT unico."""
    if getattr(config, "stt_backend", "handy") != "native":
        return True
    try:
        from .engines import _get_stt_engine
        _get_stt_engine(config).load()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("warm-up STT (sospeso) fallito: %s", e)
        return False


class HandyError(RuntimeError):
    """Errore di comunicazione col canale di controllo di Handy."""


def ensure_models_loaded(
    config: Config,
    stt_model: str | None = None,
    tts: bool = True,
    timeout: float = 180.0,
    in_process: bool = True,
    keepalive: bool = False,
) -> bool:
    """Best-effort: chiede a Handy di caricare (o tenere caldi) i modelli.

    Ritorna False se Handy non risponde o rifiuta; sta al chiamante decidere
    se e' fatale (avvio sessione PTT) o ignorabile (keep-alive periodico).

    Backend nativo: scalda i motori in-process invece di Handy. Se ENTRAMBI
    (STT e TTS) sono nativi, Handy non serve e si ritorna subito.

    `keepalive=True` per ogni giro AUTOMATICO (keep-alive periodico, warm-up
    all'avvio del bridge) E per ogni uso: durante una sospensione ("Sospendi"
    dalla GUI, che sopravvive ai riavvii) nessuno carica o riprende nulla, tranne
    lo STT, che resta sempre caldo (e' quello della dettatura). Solo «Riprendi»
    toglie la sospensione, PRIMA di chiamare questa funzione. Un uso non
    automatico mentre e' sospeso ritorna False (i chiamanti lo riportano).

    `in_process=False` per chi i motori NON li usa in casa ma li raggiunge via
    WebSocket sul bridge (il voice gateway): scaldarli localmente ne caricava una
    seconda copia in VRAM — un helper XTTS da ~2,5 GB piu' un contesto CUDA
    separato — che poi non veniva usata da nessuno. In quel caso si chiede il
    preload al bridge per HTTP, che e' l'unico che li possiede davvero.
    """
    native_stt = getattr(config, "stt_backend", "handy") == "native"
    native_tts = getattr(config, "tts_backend", "handy") == "native"
    if (native_stt or native_tts) and in_process:
        try:
            from .engines import in_sospensione, warm_up
            if in_sospensione():
                # Niente TTS, niente avatar. Lo STT si': la dettatura e' sempre
                # calda. Per un uso non automatico e' un rifiuto: False.
                solo_stt = _scalda_solo_stt(config)
                return solo_stt if keepalive else False
            native_ok = warm_up(config)
        except Exception as e:  # noqa: BLE001
            log.error("warm-up motori nativi fallito: %s", e)
            native_ok = False
        if native_stt and native_tts:
            return native_ok
        # backend misto: scalda anche Handy per la parte non nativa (sotto).
    try:
        resp = requests.post(
            f"{config.handy_base_url()}/models/load",
            json={"stt_model": stt_model, "tts": tts},
            timeout=timeout,
        )
        if not resp.ok:
            return False
        # Handy risponde HTTP 200 anche quando il warm-up del modello FALLISCE
        # (body {"status":"error", "message": ...}): il WS TTS poi rifiuta la
        # connessione ("motore Kokoro non caricato") e la sessione muore subito
        # dopo l'avvio. Controlla lo status nel body, non solo il codice HTTP.
        try:
            data = resp.json()
        except ValueError:
            return True  # nessun body JSON: fidati del 200
        if isinstance(data, dict) and str(data.get("status", "")).lower() == "error":
            log.warning("ensure_models_loaded: Handy ha risposto errore: %s",
                        data.get("message"))
            return False
        return True
    except requests.RequestException as e:
        log.debug("ensure_models_loaded: %s", e)
        return False


class HandyControlClient:
    """Client HTTP per gli endpoint `/models/*` di Handy."""

    def __init__(self, config: Config, timeout: float = 60.0) -> None:
        # Timeout generoso: il caricamento di un modello in VRAM puo' richiedere
        # diversi secondi (Whisper) o ~1-2s (spawn dell'helper Kokoro).
        self._base = config.handy_base_url()
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        try:
            resp = requests.request(method, url, json=body, timeout=self._timeout)
        except requests.RequestException as e:
            raise HandyError(f"{method} {path}: connessione a Handy fallita ({e})") from e
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if not resp.ok:
            raise HandyError(f"{method} {path}: {resp.status_code} {data}")
        return data

    def models_available(self) -> dict:
        """Voci/lingue TTS disponibili + stato del modello STT."""
        return self._request("GET", "/models/available")

    def load_models(self, stt_model: str | None = None, tts: bool = True) -> dict:
        """Carica i modelli in VRAM. Ritorna {'status': 'ready'|'error', ...}."""
        log.info("Handy load: stt_model=%s tts=%s", stt_model, tts)
        return self._request("POST", "/models/load", {"stt_model": stt_model, "tts": tts})

    def unload_models(self, component: str = "all") -> dict:
        """Scarica dalla VRAM 'stt' | 'tts' | 'all'."""
        log.info("Handy unload: %s", component)
        return self._request("POST", "/models/unload", {"component": component})

    def models_status(self) -> dict:
        """Cosa e' caricato in VRAM e da quanto e' inattivo."""
        return self._request("GET", "/models/status")
