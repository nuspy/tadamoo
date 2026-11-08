"""Client verso il server lato agente (Hermes).

Implementa la spec descritta in `skill/SKILL.md`: notifica chiamate in
arrivo/uscita, manda gli enunciati trascritti e legge la risposta come
stream SSE (token-per-token), notifica la fine della chiamata.

Hermes (o qualsiasi altro agente) e' atteso come **server** su `hermes_base_url`
con gli endpoint:
  POST /incoming-call   {session_id, number, voice, lang}    -> {"status":"ready"}
  POST /outgoing-call   {session_id, number, voice, lang}    -> {"status":"ready"}
  POST /utterance       {session_id, text, direction}        -> text/event-stream
  POST /call-ended      {session_id}                         -> {"status":"ok"}
  GET  /health                                               -> {"status":"ok"}

Se l'agente non e' raggiungibile, gli errori vengono loggati ma non sollevati:
l'orchestrator continua a gestire la chiamata (sara' senza risposte) — questo
preserva l'audio se Hermes va giu' a meta' chiamata.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import requests

from .config import Config

log = logging.getLogger("callbridge.hermes")


class HermesError(RuntimeError):
    """Errore di comunicazione col server lato agente."""


class HermesClient:
    """HTTP + SSE verso Hermes (server delle chiamate)."""

    def __init__(self, config: Config, timeout: float = 10.0,
                 read_timeout: float = 120.0) -> None:
        self._base = config.hermes_base_url.rstrip("/")
        self._timeout = timeout
        # Read-timeout PER-CHUNK sull'SSE: se Hermes si blocca a meta' stream
        # senza chiudere, senza questo il generatore (e il thread "cervello" che
        # lo consuma) resterebbe appeso per sempre. Un'assenza di byte per
        # `read_timeout` secondi viene trattata come fine turno.
        self._read_timeout = read_timeout

    # --- notifiche -------------------------------------------------------

    def notify_incoming(
        self, session_id: str, number: str, voice: str, lang: str
    ) -> bool:
        return self._notify("/incoming-call", session_id, number, voice, lang)

    def notify_outgoing(
        self, session_id: str, number: str, voice: str, lang: str
    ) -> bool:
        return self._notify("/outgoing-call", session_id, number, voice, lang)

    def _notify(
        self, path: str, session_id: str, number: str, voice: str, lang: str
    ) -> bool:
        payload = {
            "session_id": session_id,
            "number": number,
            "voice": voice,
            "lang": lang,
        }
        try:
            resp = requests.post(f"{self._base}{path}", json=payload, timeout=self._timeout)
            resp.raise_for_status()
            log.info("Hermes %s (session=%s, lang=%s)", path, session_id, lang)
            return True
        except requests.RequestException as e:
            log.warning("Hermes %s fallito (%s): proseguo comunque", path, e)
            return False

    # --- conversazione (streaming SSE) ----------------------------------

    def respond(
        self, session_id: str, text: str, direction: str
    ) -> Iterator[str | dict]:
        """POSTa `/utterance` e yielda i chunk di risposta dall'SSE.

        Yielda:
          - str: chunk di testo (parola/token) da inviare al TTS
          - dict: metadati speciali, es. {"voice_change": {"voice": ..., "lang": ...}}
            L'orchestratore chiama tts.configure() con questi parametri.

        Termina sull'evento ``{"done": true}``, sull'errore, o se la connessione
        cade. Non solleva — eventuali errori vengono loggati.
        """
        url = f"{self._base}/utterance"
        body = {"session_id": session_id, "text": text, "direction": direction}
        # connect timeout + read-timeout per-chunk (non a tempo aperto): un
        # blocco di Hermes a meta' stream chiude il turno invece di appendere.
        try:
            with requests.post(
                url, json=body, stream=True,
                timeout=(self._timeout, self._read_timeout)
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        # Evento SSE non-data (es. `event:`, commento `:`): ignora.
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("Hermes SSE: JSON non valido: %s", data)
                        continue
                    if payload.get("done"):
                        return
                    if "error" in payload:
                        log.error("Hermes ha riportato un errore: %s", payload["error"])
                        return
                    # Metadati speciali (voice_change, ecc.) — yielda come dict
                    if "voice_change" in payload:
                        yield payload
                        continue
                    chunk = payload.get("chunk")
                    if chunk:
                        yield chunk
        except requests.RequestException as e:
            log.warning("Hermes /utterance fallito: %s", e)

    def call_ended(self, session_id: str) -> bool:
        try:
            resp = requests.post(
                f"{self._base}/call-ended",
                json={"session_id": session_id},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            log.warning("Hermes /call-ended fallito (%s)", e)
            return False


class HermesSession:
    """Sessione legata a un `session_id`: espone `.respond(text)` come iterator.

    Questo e' l'oggetto passato a `StreamingSession` come `hermes`: il loop
    `_brain` chiama `.respond(text)` e ottiene i chunk di risposta. La
    `direction` e il `session_id` viaggiano automaticamente sotto.
    """

    def __init__(self, client: HermesClient, session_id: str, direction: str) -> None:
        self._client = client
        self._session_id = session_id
        self._direction = direction

    @property
    def session_id(self) -> str:
        return self._session_id

    def respond(self, text: str) -> Iterator[str | dict]:
        yield from self._client.respond(self._session_id, text, self._direction)

    def end(self) -> None:
        self._client.call_ended(self._session_id)
