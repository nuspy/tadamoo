"""Client WebSocket per lo streaming TTS di Handy (`WS /tts/stream`).

Invia il testo di risposta di Hermes a pezzi (man mano che viene generato) e
riceve l'audio sintetizzato frase per frase. Pensato per l'uso in un
orchestratore a thread: il testo si invia dal thread "cervello", i segmenti
audio si consumano dal thread che scrive l'uplink HFP.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass

import numpy as np

from websocket import WebSocketApp

from . import chunk_policy
from .audio_clean import rifila_artefatto_coda

log = logging.getLogger("callbridge.tts")


@dataclass
class TtsSegment:
    """Un blocco di audio sintetizzato (tipicamente una frase)."""

    samples: np.ndarray  # float32 mono
    sample_rate: int
    generation: int = 0  # turno TTS di appartenenza (per scartare l'audio stale al barge-in)
    #: La frase che questo audio pronuncia. Senza, chi consegna l'audio sa
    #: quanti secondi ha emesso ma non QUALE testo e' stato davvero detto — e
    #: sono due cose diverse appena una risposta viene interrotta o riscritta.
    #: Vuota per i motori che non la dichiarano: e' un di piu', non un requisito.
    #: In streaming la porta solo il PRIMO pezzo della frase: il testo conta
    #: come detto appena se ne sente l'inizio, e non va ripetuto.
    testo: str = ""
    #: Frase di appartenenza (0 = non numerata). In streaming una frase arriva
    #: in piu' segmenti con lo stesso numero: chi taglia "a fine frase" deve
    #: lasciar finire tutti i suoi pezzi, non solo quello in riproduzione.
    frase: int = 0
    #: False sui pezzi intermedi di una frase in streaming; True sull'ultimo e
    #: sui segmenti di frase intera (il comportamento di sempre).
    ultimo: bool = True


class HandyTtsClient:
    """Connessione a Handy `/tts/stream`.

    Uso tipico:
        tts = HandyTtsClient("ws://127.0.0.1:8720/tts/stream")
        tts.start()
        tts.configure(voice="if_sara", lang="it")
        tts.send_text("Ciao, ")      # man mano che Hermes genera il testo
        tts.send_text("come stai?")
        tts.flush()                  # forza la sintesi del testo bufferizzato
        seg = tts.audio.get()        # TtsSegment, dal thread dell'uplink
    """

    def __init__(self, ws_url: str, connect_timeout: float = 5.0) -> None:
        self._url = ws_url
        self._connect_timeout = connect_timeout
        self._ws: WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        # Segmenti audio sintetizzati, consumati dal thread dell'uplink.
        self.audio: queue.Queue[TtsSegment] = queue.Queue()
        # Sample rate dichiarato dall'ultimo "audio_start" (precede il binario).
        self._pending_rate = 24000
        #: Testo dichiarato dall'ultimo "audio_start", come il rate.
        self._pending_testo = ""
        # Generazione TTS corrente: bump_generation() la incrementa al barge-in,
        # cosi' l'uplink scarta i segmenti ancora in sintesi per il testo abortito.
        self.generation = 0
        # Generazione del testo ATTUALMENTE in sintesi dall'altra parte, fissata
        # alla flush(). E' con questa che si marcano i segmenti in arrivo: fra
        # la partenza del testo e il ritorno dell'audio puo' esserci stato un
        # barge-in, e `generation` a quel punto e' gia' un'altra.
        self._gen_in_volo = 0

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
            target=self._ws.run_forever, name="tts-ws", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self._connect_timeout):
            # Non lasciare orfani il WebSocketApp e il thread run_forever se la
            # connessione riesce dopo lo scadere del timeout.
            self.close()
            raise TimeoutError(
                f"Handy TTS WS non pronto entro {self._connect_timeout}s ({self._url})"
            )

    def configure(self, voice: str = "if_sara", lang: str = "it",
                  speed: float = 1.0, engine: str = "") -> None:
        """Imposta voce, lingua, velocita' e MOTORE di sintesi.

        Senza `engine` il router deduce il motore dal nome della voce, e le voci
        che non gli appartengono finiscono a quello sbagliato: uno speaker XTTS
        come "Chandra MacFarland" veniva mandato a Kokoro, che rispondeva
        "Voice not found" — l'avatar restava muto pur avendo la voce giusta
        configurata sulla persona.
        """
        self._send_json({"type": "config", "voice": voice, "lang": lang,
                         "speed": speed, "engine": engine})

    def send_text(self, chunk: str) -> None:
        """Invia un pezzo di testo da sintetizzare (puo' essere parziale)."""
        if chunk:
            # Testo e audio si contano QUI, dove passano tutti i percorsi: dal
            # loro rapporto la politica dei chunk ricava quanti caratteri vale
            # un secondo di parlato, che e' cio' che lega la formula (in
            # secondi) alla soglia del sanitizer (in caratteri).
            chunk_policy.osserva_testo(len(chunk))
            self._send_json({"type": "text", "chunk": chunk})

    def flush(self) -> None:
        """Forza la sintesi del testo ancora bufferizzato lato Handy."""
        # Si fissa QUI la generazione a cui appartiene l'audio che tornera':
        # e' il momento in cui il testo parte. Marcarlo alla ricezione (com'era)
        # e' sbagliato di principio, perche' fra la partenza e il ritorno puo'
        # esserci stato un barge-in.
        self._gen_in_volo = self.generation
        self._send_json({"type": "flush"})

    def bump_generation(self) -> int:
        """Apre una nuova generazione TTS (al barge-in): i segmenti gia' in
        sintesi per il testo abortito porteranno la generazione precedente e
        l'uplink li scartera'. Ritorna la nuova generazione."""
        self.generation += 1
        return self.generation

    def abort(self) -> int:
        """Interrompe la sintesi: butta il testo non ancora sintetizzato (di
        la' dal socket) e l'audio gia' pronto ma non ancora consumato (di qua).

        `bump_generation` da solo non basta: marca il futuro, ma il testo gia'
        affidato al motore continua a diventare audio e la coda locale resta
        piena. Serve dirlo anche all'altro capo, dove il worker controlla la
        generazione sia quando sfila un job sia a ogni frase.
        """
        gen = self.bump_generation()
        self._send_json({"type": "abort"})
        while True:
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break
        return gen

    def end(self) -> None:
        """Sintetizza l'ultimo testo e chiude la sessione TTS."""
        self._send_json({"type": "end"})

    def close(self) -> None:
        self._closed.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 - chiusura best-effort
                pass

    def _send_json(self, obj: dict) -> None:
        if self._ws is None or self._closed.is_set():
            return
        try:
            self._ws.send(json.dumps(obj))  # frame di testo
        except Exception as e:  # noqa: BLE001 - la connessione puo' cadere
            log.warning("invio '%s' fallito: %s", obj.get("type"), e)

    # --- callback di WebSocketApp ----------------------------------------

    def _on_open(self, _ws: WebSocketApp) -> None:
        log.info("TTS WS connesso a %s", self._url)

    def _on_message(self, _ws: WebSocketApp, message: str | bytes) -> None:
        # I frame binari sono PCM f32 LE mono della frase appena sintetizzata.
        if isinstance(message, (bytes, bytearray)):
            samples = np.frombuffer(message, dtype="<f4").copy()
            # Qui e non nei chiamanti: un frame e' una sintesi COMPLETA, e
            # ripulirla alla nascita vale per tutti i percorsi (telefono, PTT
            # del PC, copilota, telefonate) senza doverli ricordare uno per uno.
            samples = rifila_artefatto_coda(samples, self._pending_rate)
            if self._pending_rate:
                chunk_policy.osserva_audio(len(samples) / self._pending_rate)
            # `_gen_in_volo`, non `generation`: sono diverse proprio nel caso
            # che conta. Dopo un barge-in la generazione corrente e' gia' salita,
            # e marcare qui con quella faceva passare per NUOVI i segmenti
            # sintetizzati per il testo appena abbandonato — che e' esattamente
            # cio' che il meccanismo doveva impedire. Il difetto era silenzioso:
            # l'audio vecchio riaffiorava all'inizio del turno successivo.
            self.audio.put(TtsSegment(samples=samples,
                                      sample_rate=self._pending_rate,
                                      generation=self._gen_in_volo,
                                      testo=self._pending_testo))
            self._pending_testo = ""
            return

        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            log.warning("TTS WS: JSON non valido: %s", message)
            return

        mtype = msg.get("type")
        if mtype == "ready":
            self._ready.set()
        elif mtype == "audio_start":
            # Precede il frame binario: memorizza il sample rate dichiarato.
            self._pending_rate = int(msg.get("sample_rate", 24000))
            self._pending_testo = msg.get("testo") or ""
        elif mtype == "audio_end":
            pass  # il binario corrispondente e' gia' stato accodato
        elif mtype == "error":
            log.error("TTS WS: errore dal server: %s", msg.get("message"))
        else:
            log.debug("TTS WS: messaggio non gestito: %s", msg)

    def _on_error(self, _ws: WebSocketApp, error: object) -> None:
        log.error("TTS WS: errore di connessione: %s", error)

    def _on_close(self, _ws: WebSocketApp, status_code: object, reason: object) -> None:
        self._closed.set()
        log.info("TTS WS chiuso (code=%s reason=%s)", status_code, reason)
