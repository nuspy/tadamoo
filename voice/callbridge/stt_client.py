"""Client WebSocket per lo streaming STT di Handy (`WS /stt/stream`).

Invia PCM continuo (float32 mono 16 kHz) e riceve gli enunciati trascritti.
Pensato per l'uso in un orchestratore a thread: l'audio si invia dal thread
che legge il downlink HFP, gli enunciati si consumano da una coda.
"""

from __future__ import annotations

import json
import logging
import queue
import threading

import numpy as np
from websocket import ABNF, WebSocketApp

log = logging.getLogger("callbridge.stt")

# Handy /stt/stream lavora a 16 kHz: chi invia audio ricampiona a questo rate.
STT_RATE = 16000


class HandySttClient:
    """Connessione a Handy `/stt/stream`.

    Uso tipico:
        stt = HandySttClient("ws://127.0.0.1:8720/stt/stream")
        stt.start()                       # apre la connessione, attende "ready"
        stt.send_audio(pcm_f32_mono16k)   # dal thread del downlink
        text = stt.utterances.get()       # dal thread "cervello"
        stt.close()
    """

    def __init__(self, ws_url: str, connect_timeout: float = 5.0) -> None:
        self._url = ws_url
        self._connect_timeout = connect_timeout
        self._ws: WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        # Enunciati trascritti, consumati dal thread "cervello".
        self.utterances: queue.Queue[str] = queue.Queue()
        # Eventi di inizio voce: utili per il barge-in (Task #7).
        self.speech_events: queue.Queue[str] = queue.Queue()

    def start(self) -> None:
        """Apre la connessione e attende il messaggio "ready" dal server."""
        self._ws = WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever, name="stt-ws", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self._connect_timeout):
            # Non lasciare orfani il WebSocketApp e il thread run_forever se la
            # connessione riesce dopo lo scadere del timeout.
            self.close()
            raise TimeoutError(
                f"Handy STT WS non pronto entro {self._connect_timeout}s ({self._url})"
            )

    def send_audio(self, pcm: np.ndarray) -> None:
        """Invia un blocco di PCM (float32 mono 16 kHz) al motore STT."""
        if self._ws is None or self._closed.is_set():
            return
        data = np.ascontiguousarray(pcm, dtype="<f4").tobytes()
        try:
            self._ws.send(data, opcode=ABNF.OPCODE_BINARY)
        except Exception as e:  # noqa: BLE001 - la connessione puo' cadere
            log.warning("send_audio fallito: %s", e)

    def flush(self) -> None:
        """Chiude subito l'enunciato in corso, senza attendere il silenzio.

        Serve al walkie-talkie: al rilascio del pulsante l'utente ha finito di
        parlare e lo sa gia'. Aspettare che il VAD "scopra" il silenzio aggiunge
        ritardo, e se la coda di silenzio e' piu' corta di
        `stt_endpoint_silence_s` l'enunciato non si chiude affatto.
        """
        if self._ws is None or self._closed.is_set():
            return
        try:
            self._ws.send(json.dumps({"type": "flush"}), opcode=ABNF.OPCODE_TEXT)
        except Exception as e:  # noqa: BLE001 - la connessione puo' cadere
            log.warning("flush fallito: %s", e)

    def close(self) -> None:
        self._closed.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 - chiusura best-effort
                pass

    # --- callback di WebSocketApp ----------------------------------------

    def _on_open(self, _ws: WebSocketApp) -> None:
        log.info("STT WS connesso a %s", self._url)

    def _on_message(self, _ws: WebSocketApp, message: str | bytes) -> None:
        # Il server STT manda sempre JSON di testo.
        if isinstance(message, (bytes, bytearray)):
            log.warning("STT WS: ricevuto binario inatteso (%d byte)", len(message))
            return
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            log.warning("STT WS: JSON non valido: %s", message)
            return

        mtype = msg.get("type")
        if mtype == "ready":
            self._ready.set()
        elif mtype == "utterance":
            text = (msg.get("text") or "").strip()
            if text:
                log.info("STT -> %s", text)
                self.utterances.put(text)
        elif mtype == "speech_start":
            self.speech_events.put("speech_start")
        elif mtype == "error":
            log.error("STT WS: errore dal server: %s", msg.get("message"))
        else:
            log.debug("STT WS: messaggio non gestito: %s", msg)

    def _on_error(self, _ws: WebSocketApp, error: object) -> None:
        log.error("STT WS: errore di connessione: %s", error)

    def _on_close(self, _ws: WebSocketApp, status_code: object, reason: object) -> None:
        self._closed.set()
        log.info("STT WS chiuso (code=%s reason=%s)", status_code, reason)
