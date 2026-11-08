"""STT nativo in-process: whisper (torch) + client a interfaccia HandySttClient.

Sostituisce il WebSocket `/stt/stream` di Handy con una trascrizione locale:
`openai-whisper` su GPU (cuda) o CPU, con endpointing VAD a energia (silero
arrivera' in Fase 2). Il `NativeSttClient` espone la STESSA interfaccia di
`HandySttClient` (start/send_audio/close, code `utterances`/`speech_events`),
cosi' `StreamingSession` non cambia.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time

import numpy as np

from ..memoria import carica_whisper, rilascia
from ..stt_client import STT_RATE

log = logging.getLogger("callbridge.engines.stt")

# lang del bridge (Kokoro-style) -> codice lingua whisper.
_WHISPER_LANG = {
    "it": "it", "en": "en", "en-us": "en", "en-gb": "en", "fr": "fr",
    "fr-fr": "fr", "es": "es", "pt": "pt", "pt-br": "pt", "ja": "ja",
    "cmn": "zh", "zh": "zh", "hi": "hi",
}


def _compute_vuole_half(compute_type) -> bool:
    """True se `stt_compute_type` chiede i pesi in mezza precisione."""
    v = str(compute_type or "").lower()
    if "float32" in v or "fp32" in v:
        return False
    return ("16" in v) or v in ("auto", "")


def precisione_effettiva(device: str, compute_type) -> str:
    """Precisione che whisper USERA' davvero, "float16" o "float32".

    Le sole due che hanno un percorso vero: su CPU i pesi sono sempre fp32, e
    "int8" non ha nessun percorso (non e' mai stato implementato: finisce in
    fp32). Un valore come "int8_float16" (di CTranslate2, mai usato) conta come
    float16 perche' contiene "16"."""
    return ("float16" if (device == "cuda" and _compute_vuole_half(compute_type))
            else "float32")


class WhisperSttEngine:
    """Modello whisper caricato una sola volta (condiviso tra le sessioni)."""

    def __init__(self, model: str = "small", device: str = "cuda",
                 gpu_memory: str = "shared", compute_type: str = "auto",
                 single_thread: bool = True) -> None:
        self.model_name = model
        self.gpu_memory = gpu_memory
        self.compute_type = compute_type
        self.device = self._resolve_device(device, gpu_memory)
        self._model = None
        self._loaded_on: str | None = None       # device del modello caricato
        self._loaded_model: str | None = None
        self._loaded_half: bool | None = None    # precisione REALE dei pesi vivi
        self._lock = threading.Lock()
        # Il modello whisper NON e' thread-safe: due `transcribe` in
        # parallelo (il chunk del parlato in background e la coda del turno)
        # corrompono lo stato dell'attenzione e falliscono con
        # "Expected key.size(1) == value.size(1)". Misurato: succede al primo
        # turno con il chunking acceso. Questo lock le mette in fila.
        self._infer_lock = threading.Lock()
        # Inferenza in UN SOLO thread (vedi `_nel_thread_unico`). False =
        # comportamento storico, ogni chiamante trascrive nel proprio thread.
        self.single_thread = single_thread
        self._infer_thread: threading.Thread | None = None
        self._avvio_lock = threading.Lock()
        self._lavori: "queue.Queue[tuple]" = queue.Queue()

    @staticmethod
    def _resolve_device(device: str, memory: str = "dedicated") -> str:
        """(device, memory) -> cuda|cpu. cuda+dedicated=GPU; cuda+shared=CPU
        (shared VRAM reale non ottenibile con torch: shared = CPU, 0 VRAM
        dedicata). CPU anche se CUDA non e' disponibile (distribuibilita')."""
        from .device import resolve_device
        if resolve_device(device, memory) == "cpu":
            return "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            log.warning("STT: CUDA non disponibile -> uso CPU")
        except Exception as e:  # noqa: BLE001 - torch assente/rotto
            log.warning("STT: torch/CUDA non caricabile (%s) -> uso CPU", e)
        return "cpu"

    def reconfigure(self, cfg) -> None:
        """Applica device/memoria/modello correnti e scarta il modello caricato
        (reload pigro). Stesso oggetto (il compat server ne cattura il riferimento)."""
        self.model_name = getattr(cfg, "stt_model", self.model_name)
        self.gpu_memory = getattr(cfg, "stt_gpu_memory", self.gpu_memory)
        self.compute_type = getattr(cfg, "stt_compute_type", self.compute_type)
        self.device = self._resolve_device(
            getattr(cfg, "stt_device", "cuda"), self.gpu_memory)
        self._model = None

    def _vuole_half(self) -> bool:
        """True se i pesi vanno tenuti in mezza precisione.

        Legge `stt_compute_type`, che finora era un campo MORTO: conteneva
        valori di CTranslate2 (es. "int8_float16") perche' pensato per un
        backend faster-whisper mai arrivato, e openai-whisper non lo guardava
        nemmeno. Ora almeno la parte float16 ha effetto. "float32"/"fp32"
        tengono la precisione piena.
        """
        return _compute_vuole_half(getattr(self, "compute_type", ""))

    @property
    def precision(self) -> str:
        """Precisione REALE: "float16" o "float32". Del modello vivo se c'e';
        altrimenti quella che il prossimo caricamento userebbe. Sono le sole due
        che hanno un percorso vero: su CPU i pesi sono sempre fp32, e "int8" non
        ha nessun percorso (finisce in fp32)."""
        if self._model is not None and self._loaded_half is not None:
            return "float16" if self._loaded_half else "float32"
        return "float16" if (self.device == "cuda" and self._vuole_half()) else "float32"

    def load(self) -> None:
        from .vram import caricamento
        with self._lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            # Serializzato con gli altri caricamenti: whisper e un motore TTS
            # che si caricano insieme sommano i picchi e finiscono in shared.
            with caricamento(f"stt:{self.model_name}"):
                # Mezza precisione: `whisper.load_model` carica in fp32 e il
                # flag fp16=True di transcribe() riguarda solo il CALCOLO, non i
                # pesi. Portarli a half dimezza la VRAM residente (misurato su
                # medium: 2915 -> 1468 MB) ed e' anche piu' veloce (2,1 -> 2,6x
                # realtime), con trascrizioni identiche.
                # Il caricamento passa da `carica_whisper`: i pesi vanno dal
                # file alla destinazione (VRAM, o un unico buffer in RAM) senza
                # la copia fp32 completa che `whisper.load_model` teneva in RAM
                # (medium su CPU: 7,75 -> 4,5 GiB di commit, risultati
                # identici bit a bit; vedi memoria.py).
                half = self.device == "cuda" and self._vuole_half()
                self._model = carica_whisper(self.model_name, self.device, half)
                # I LayerNorm restano in fp32: quello di whisper fa
                # `super().forward(x.float())`, e con pesi half va in
                # RuntimeError "expected scalar type Float but found Half".
                if half:
                    import torch as _t
                    for _m in self._model.modules():
                        if isinstance(_m, _t.nn.LayerNorm):
                            _m.float()
            rilascia(cuda=False)     # niente empty_cache: vedi nota in transcribe_detailed
            self._loaded_on = self.device        # device REALE del modello vivo
            self._loaded_model = self.model_name
            self._loaded_half = bool(half)
            log.info("STT nativo pronto: whisper '%s' su %s (%.1fs)",
                     self.model_name, self.device, time.perf_counter() - t0)

    def runtime_info(self) -> dict:
        """Stato REALE del motore, per la GUI (niente da incrociare col log)."""
        return {
            "configured": f"cuda/{self.gpu_memory}" if self.device == "cuda"
                          else f"cpu/{self.gpu_memory}",
            "would_load_on": self.device,
            "loaded": self._model is not None,
            "running_on": self._loaded_on if self._model is not None else None,
            "model": self._loaded_model if self._model is not None else self.model_name,
            "precision": self.precision,
        }

    # --- un solo thread per tutta l'inferenza ----------------------------
    def _servi_lavori(self) -> None:
        while True:
            fn, esito = self._lavori.get()
            try:
                esito["val"] = fn()
            except BaseException as e:  # noqa: BLE001 - rilanciata al chiamante
                esito["err"] = e
            finally:
                esito["fatto"].set()

    def _nel_thread_unico(self, fn):
        """Esegue `fn` sempre nello STESSO thread e ne restituisce il risultato.

        Whisper e' PyTorch, e PyTorch tiene le cache dei piani di esecuzione
        cuDNN in variabili THREAD_LOCAL: un thread nuovo le ricostruisce da
        zero. Sull'avatar questo costava 1,4 s per battuta (vedi la classe
        `Produttore` in avatar/avatar_worker.py), e qui la situazione era la
        stessa: `_trascrivi_parziale` lanciava un thread usa-e-getta per OGNI
        pezzo di parlato, quindi ogni micro-pausa della dettatura ripagava la
        costruzione dei piani dell'encoder.

        Non cambia la semantica: le trascrizioni erano gia' in fila una dietro
        l'altra (il modello non e' thread-safe, `_infer_lock`); cambia solo che
        la fila la fa una coda invece di N thread bloccati sul lucchetto.
        """
        if not self.single_thread:
            return fn()
        with self._avvio_lock:
            if self._infer_thread is None or not self._infer_thread.is_alive():
                self._infer_thread = threading.Thread(
                    target=self._servi_lavori, name="stt-infer", daemon=True)
                self._infer_thread.start()
        if threading.current_thread() is self._infer_thread:
            return fn()          # gia' dentro: niente attese su se stessi
        esito: dict = {"fatto": threading.Event()}
        self._lavori.put((fn, esito))
        esito["fatto"].wait()
        if "err" in esito:
            raise esito["err"]
        return esito["val"]

    def transcribe(self, audio_f32_16k: np.ndarray, lang: str | None = None) -> str:
        return self.transcribe_detailed(audio_f32_16k, lang)["text"]

    def transcribe_detailed(self, audio_f32_16k: np.ndarray,
                            lang: str | None = None,
                            word_timestamps: bool = False) -> dict:
        """Trascrizione CON i tempi: `{text, segments: [{start_s, end_s, text,
        words?}]}`.

        whisper li produce comunque, ma `transcribe()` restituiva il solo testo e
        li buttava via. Servono alla diarizzazione: senza sapere QUANDO e' stata
        detta una frase non la si puo' attribuire a una voce. `word_timestamps`
        aggiunge i tempi per parola (costa ~20-30% in piu' di inferenza) e
        permette di spezzare un segmento la' dove uno interrompe l'altro, invece
        di attribuirlo tutto a chi ha parlato di piu'."""
        self.load()
        wl = _WHISPER_LANG.get((lang or "").lower()) if lang else None
        # Il flag di calcolo segue i pesi REALI: con pesi fp32 su GPU, fp16=True
        # calcolava comunque in mezza precisione (whisper converte i pesi a ogni
        # operazione), quindi "float32" non aveva l'effetto che prometteva.
        fp16 = self._loaded_on == "cuda" and bool(self._loaded_half)
        def _inferisci():
            return self._model.transcribe(
                np.ascontiguousarray(audio_f32_16k, dtype=np.float32),
                language=wl, fp16=fp16, word_timestamps=word_timestamps,
            )

        with self._infer_lock:
            r = self._nel_thread_unico(_inferisci)
            # Il workspace della trascrizione (~3,5 GB su medium, contro
            # 1,5 GB di pesi) resta prenotato dal caching allocator anche a
            # microfono fermo. Si restituisce QUI, DENTRO il lucchetto: una
            # prima versione stava fuori, e col chunking del parlato un
            # secondo thread poteva allocare durante la liberazione ->
            # "CUDA error: unknown error" a ogni trascrizione, voce e
            # dettatura mute. Dentro il lock nessun'altra inferenza whisper
            # e' in corso per costruzione.
            if self.device == "cuda":
                import torch as _t
                _t.cuda.empty_cache()
        # RIMOSSO un `torch.cuda.empty_cache()` che stava QUI, fuori dal
        # lock: con il chunking del parlato un secondo thread trascrive in
        # parallelo, e liberare la cache mentre l'altro alloca corrompeva il
        # contesto -> "CUDA error: unknown error" a OGNI trascrizione, voce e
        # dettatura completamente mute. Il guadagno era marginale; se si
        # rivuole, va dentro `_infer_lock`.
        segments = []
        for s in (r.get("segments") or []):
            testo = (s.get("text") or "").strip()
            if not testo:
                continue
            seg = {"start_s": float(s.get("start") or 0.0),
                   "end_s": float(s.get("end") or 0.0), "text": testo}
            if word_timestamps:
                seg["words"] = [
                    {"start_s": float(w.get("start") or 0.0),
                     "end_s": float(w.get("end") or 0.0),
                     "text": str(w.get("word") or "")}
                    for w in (s.get("words") or [])
                    if str(w.get("word") or "").strip()
                ]
            segments.append(seg)
        return {"text": (r.get("text") or "").strip(), "segments": segments}


_VAD_WIN = 512  # campioni per finestra silero @ 16 kHz (~32 ms)
# PREROLL: finestre tenute da parte PRIMA che il VAD si convinca che e'
# voce. Senza, l'attacco della frase si perde — il VAD ha bisogno di
# `start_need` finestre per decidere, e quelle finiscono nel cestino:
# "ti ho chiesto di farmi una domanda" arrivava come "di farmi una
# domanda". 10 finestre = ~320 ms, abbastanza per una sillaba d'attacco
# senza trascinarsi dentro il rumore che precede.
_VAD_PREROLL = 10

# Energia minima (RMS) perche' un segmento valga la pena di essere
# trascritto. Sotto, e' silenzio o fruscio: whisper non lo lascia vuoto,
# INVENTA — "Si.", "Grazie.", "Sottotitoli e revisione a cura di..." sono
# le sue frasi-fantasma tipiche. Quelle finivano in conversazione come se
# le avesse dette l'utente, e bastavano a far scattare il barge-in
# troncando la risposta. Misurato sui turni veri: voce piena rms ~0,11,
# silenzio/rumore rms 0,005-0,012.
# Regolabile senza toccare il codice: il livello dipende dal microfono, e
# quello di un auricolare bluetooth arriva piu' basso di uno da tavolo.
_MIN_RMS = float(os.environ.get("CALLBRIDGE_STT_MIN_RMS") or 0.012)


def _find_silero(path_hint: str) -> str | None:
    """Percorso del modello silero VAD onnx: hint di config, poi Handy/resources."""
    import os
    cands = [path_hint] if path_hint else []
    cands += [
        # bundled nel repo (call-bridge/models/), accanto al pacchetto
        os.path.join(os.path.dirname(__file__), "..", "..", "models",
                     "silero_vad_v4.onnx"),
        os.path.expandvars(r"%LOCALAPPDATA%\callAPIcall\models\silero_vad_v4.onnx"),
        os.path.expandvars(r"%LOCALAPPDATA%\Handy\resources\models\silero_vad_v4.onnx"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


class SileroVad:
    """VAD silero (onnx): probabilita' di voce per finestra da 512 campioni @ 16k.

    Robusto ai microfoni a basso livello (a differenza dell'energia RMS): la voce
    continua NON viene scambiata per silenzio, quindi non taglia l'enunciato."""

    def __init__(self, onnx_path: str) -> None:
        import onnxruntime as ort
        self._sess = ort.InferenceSession(onnx_path,
                                          providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def prob(self, window: np.ndarray) -> float:
        x = np.ascontiguousarray(window, dtype=np.float32).reshape(1, -1)
        out, self._h, self._c = self._sess.run(None, {
            "input": x, "sr": np.array(16000, dtype=np.int64),
            "h": self._h, "c": self._c,
        })
        return float(np.asarray(out).reshape(-1)[0])


# Frasi che Whisper produce dal SILENZIO: vengono dai sottotitoli su cui e'
# stato addestrato e non le dice mai nessuno a un microfono. Confrontate in
# minuscolo su testo senza punteggiatura.
# Sentinella in coda all'audio: "chiudi l'enunciato adesso". Non e' un flag
# perche' l'ordine rispetto ai campioni gia' inviati deve restare garantito.
_FLUSH = object()

_ALLUCINAZIONI = (
    "sottotitoli e revisione a cura di",
    "sottotitoli creati dalla comunit",
    "amara.org",
    "sottotitoli a cura di",
    "subtitles by the amara.org community",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "ご視聴ありがとうございました",
    "字幕由",
)


def _e_allucinazione(testo: str) -> bool:
    """Vero se il testo e' una frase-fantasma tipica del silenzio."""
    t = "".join(c for c in testo.lower() if c.isalnum() or c in " .-").strip()
    return any(marca in t for marca in _ALLUCINAZIONI)


def _troppo_debole(audio: np.ndarray) -> bool:
    """True se il segmento non ha abbastanza energia per essere voce.

    Si guarda l'audio, non il testo: filtrare le allucinazioni per nome e'
    una rincorsa infinita (ogni lingua ha le sue) e rischia di buttare via
    un "Grazie" detto davvero. L'energia invece distingue alla radice il
    parlato dal silenzio.
    """
    if audio.size == 0:
        return True
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    if rms < _MIN_RMS:
        # Si registra lo scarto: senza, un enunciato appena sotto soglia
        # spariva senza lasciare traccia e dal di fuori era indistinguibile
        # da "non mi sente". Il valore dice quale dei due e': rms ~0 e' un
        # microfono muto, rms appena sotto 0,012 e' la soglia da alzare.
        log.info("STT: segmento scartato per energia (rms %.4f < %.3f, %.2fs)",
                 rms, _MIN_RMS, audio.size / STT_RATE)
        return True
    return False


def _dump_se_richiesto(audio: np.ndarray) -> None:
    """Salva su disco l'audio che sta per essere trascritto, se richiesto.

    Si accende creando `call-bridge/dump-turni.on`. Quando la trascrizione e'
    incomprensibile serve poter riascoltare e misurare *cosa* e' arrivato a
    whisper: senza, non si distingue un microfono che manda audio sporco da un
    motore che sbaglia.
    """
    import os
    import wave
    from pathlib import Path

    radice = Path(__file__).resolve().parents[2]
    if not os.environ.get("CB_DUMP_TURNS") and not (radice / "dump-turni.on").exists():
        return
    cartella = radice / "turni"
    cartella.mkdir(exist_ok=True)
    nome = cartella / f"stt-{time.strftime('%H%M%S')}-{len(audio) // 160:04d}cs.wav"
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(nome), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(STT_RATE)
        w.writeframes(pcm.tobytes())
    log.info("STT: audio del turno salvato in %s", nome.name)


class NativeSttClient:
    """Adattatore a interfaccia `HandySttClient` sul motore whisper locale.

    Accumula il PCM (float32 mono 16 kHz) inviato con `send_audio`, rileva la
    fine dell'enunciato col VAD SILERO (o energia in fallback) e mette il testo
    trascritto nella coda `utterances`. `speech_events` segnala l'inizio voce.
    """

    def __init__(self, engine: WhisperSttEngine, lang: str | None = None,
                 vad_threshold: float = 0.01, endpoint_silence_s: float = 0.8,
                 min_utterance_s: float = 0.3, vad_model: str = "",
                 speech_prob: float = 0.5,
                 chunk_silence_s: float = 0.0,
                 chunk_min_s: float = 2.0) -> None:
        self._engine = engine
        self._lang = lang
        self._thr = vad_threshold
        self._end_sil = endpoint_silence_s
        self._min_utt = min_utterance_s
        self._speech_prob = speech_prob
        # Chunking del parlato: a ogni MICRO-pausa (piu' corta di quella che
        # chiude il turno) il pezzo gia' detto parte in trascrizione mentre
        # l'utente continua. Quando poi scatta l'endpoint resta da trascrivere
        # solo la coda, non l'intero turno: su una frase lunga si risparmiano
        # secondi di attesa a bocca chiusa. 0 = disattivato (comportamento
        # storico: si trascrive tutto alla fine).
        self._chunk_sil = chunk_silence_s
        self._chunk_min = chunk_min_s
        self._parziali: list[str] = []      # testo dei chunk gia' trascritti
        self._parz_lock = threading.Lock()
        self._vad_path = _find_silero(vad_model)
        self.utterances: queue.Queue[str] = queue.Queue()
        self.speech_events: queue.Queue[str] = queue.Queue()
        self._in: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._engine.load()  # scalda il modello (prima volta ~10s)
        self._thread = threading.Thread(target=self._run, name="native-stt",
                                        daemon=True)
        self._thread.start()

    def send_audio(self, pcm: np.ndarray) -> None:
        if self._stop.is_set():
            return
        self._in.put(np.ascontiguousarray(pcm, dtype=np.float32))

    def flush(self) -> None:
        """Chiude subito l'enunciato in corso (pulsante walkie-talkie rilasciato).

        Passa dalla stessa coda dell'audio, non da un flag: cosi' l'ordine e'
        garantito e si trascrive tutto il parlato inviato prima del rilascio,
        senza correre contro il thread che consuma i campioni.
        """
        if self._stop.is_set():
            return
        self._in.put(_FLUSH)

    def close(self) -> None:
        self._stop.set()

    # --- worker ----------------------------------------------------------

    def _make_vad(self):
        if not self._vad_path:
            log.warning("STT: silero VAD non trovato -> endpointing a energia "
                        "(meno robusto). Metti silero_vad_v4.onnx nei modelli.")
            return None
        try:
            v = SileroVad(self._vad_path)
            log.info("STT: VAD silero attivo (%s)", self._vad_path)
            return v
        except Exception as e:  # noqa: BLE001
            log.warning("STT: silero non caricabile (%s) -> energia", e)
            return None

    def _run(self) -> None:
        vad = self._make_vad()
        buf = np.zeros(0, dtype=np.float32)
        speech: list[np.ndarray] = []   # solo le finestre marcate come voce
        tutto: list[np.ndarray] = []    # tutto il turno, per il flush di riserva
        in_speech = False
        speech_frames = 0
        preroll: list = []          # ultime finestre prima della voce
        silence_frames = 0
        start_need = 2                              # finestre di voce per iniziare
        end_need = max(1, int(self._end_sil / (_VAD_WIN / STT_RATE)))  # silenzio -> fine
        # Micro-pausa che chiude un CHUNK senza chiudere il turno.
        chunk_need = (max(1, int(self._chunk_sil / (_VAD_WIN / STT_RATE)))
                      if self._chunk_sil > 0 else 0)
        chunk_min_win = max(1, int(self._chunk_min * STT_RATE / _VAD_WIN))
        while not self._stop.is_set():
            try:
                chunk = self._in.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is _FLUSH:
                # Si trascrive cio' che il VAD ha marcato come voce; se non ha
                # marcato nulla (voce bassa, microfono lontano) si ripiega su
                # TUTTO l'audio del turno. In walkie-talkie il turno lo delimita
                # l'utente col pulsante: tornare a mani vuote perche' il VAD non
                # si e' convinto e' il difetto peggiore dei due.
                if buf.size:
                    speech.append(buf)
                    tutto.append(buf)
                    buf = np.zeros(0, dtype=np.float32)
                blocchi = speech or tutto
                if blocchi:
                    self._finalize(blocchi)
                    if vad is not None:
                        vad.reset()
                speech, tutto, in_speech = [], [], False
                preroll = []
                speech_frames = silence_frames = 0
                continue
            if chunk.size == 0:
                continue
            buf = np.concatenate([buf, chunk]) if buf.size else chunk
            while len(buf) >= _VAD_WIN:
                win = buf[:_VAD_WIN]
                buf = buf[_VAD_WIN:]
                tutto.append(win)
                if vad is not None:
                    is_speech = vad.prob(win) >= self._speech_prob
                else:
                    rms = float(np.sqrt(np.mean(np.square(win, dtype=np.float64))))
                    is_speech = rms >= self._thr
                if is_speech:
                    speech_frames += 1
                    silence_frames = 0
                    if not in_speech and speech_frames >= start_need:
                        # L'attacco sta nel preroll: si antepone, cosi' la
                        # prima sillaba entra nella trascrizione.
                        speech.extend(preroll)
                        preroll = []        # usato: non va riagganciato dopo
                        in_speech = True
                        self.speech_events.put("speech_start")
                    if in_speech:
                        speech.append(win)
                else:
                    # Fuori dal parlato si tiene una coda corta: e' quella che
                    # recupera l'attacco quando la voce comincia.
                    if not in_speech:
                        preroll.append(win)
                        if len(preroll) > _VAD_PREROLL:
                            preroll.pop(0)
                    if in_speech:
                        speech.append(win)  # trascina un po' di coda
                        silence_frames += 1
                        # Chunk: micro-pausa e abbastanza parlato accumulato
                        # -> si trascrive QUESTO pezzo in background e si
                        # riparte, restando dentro lo stesso turno.
                        if (chunk_need and silence_frames == chunk_need
                                and silence_frames < end_need
                                and len(speech) >= chunk_min_win):
                            self._trascrivi_parziale(speech)
                            speech = []
                            if vad is not None:
                                vad.reset()
                        if silence_frames >= end_need:
                            self._finalize(speech)
                            if vad is not None:
                                vad.reset()
                            speech, tutto, in_speech = [], [], False
                            preroll = []
                            speech_frames = silence_frames = 0
                    else:
                        speech_frames = 0
        if in_speech:
            self._finalize(speech)

    def _trascrivi_parziale(self, chunks: list[np.ndarray]) -> None:
        """Trascrive un pezzo del turno IN BACKGROUND, mentre l'utente parla.

        Il testo si accumula in `_parziali` e viene unito alla coda finale da
        `_finalize`. Se fallisce non si perde nulla di critico: il pezzo
        semplicemente non comparira' nel testo (meglio di un turno bloccato).
        """
        if not chunks:
            return
        audio = np.concatenate(chunks)
        if len(audio) / STT_RATE < self._min_utt or _troppo_debole(audio):
            return

        def _lavora():
            # Tempo di OGNI chunk: e' il percorso in cui il thread e' davvero
            # nuovo a ogni micro-pausa, quindi l'unico dove il thread unico
            # dell'engine puo' fare la differenza. Con `stt_single_thread`
            # acceso questi tempi devono essere piatti; se il primo di ogni
            # dettatura resta molto piu' alto, il costo sta altrove.
            _t0 = time.perf_counter()
            try:
                testo = self._engine.transcribe(audio, lang=self._lang)
            except Exception as e:  # noqa: BLE001
                log.warning('STT: chunk non trascritto: %s', e)
                return
            log.info('STT chunk: %.0f ms per %.1fs di audio',
                     (time.perf_counter() - _t0) * 1000, len(audio) / STT_RATE)
            testo = (testo or '').strip()
            if not testo or _e_allucinazione(testo):
                return
            with self._parz_lock:
                self._parziali.append(testo)
            log.debug('STT chunk -> %s', testo[:60])

        threading.Thread(target=_lavora, name='stt-chunk', daemon=True).start()

    def _raccogli_parziali(self) -> str:
        """Testo dei chunk gia' trascritti, svuotando l'accumulatore.

        Si attende qualche istante che gli ultimi thread finiscano: sono partiti
        mentre l'utente parlava, quindi nel caso normale hanno gia' concluso.
        """
        for _ in range(40):                      # max ~2 s
            if not [t for t in threading.enumerate() if t.name == "stt-chunk"]:
                break
            time.sleep(0.05)
        with self._parz_lock:
            testi, self._parziali = self._parziali, []
        return " ".join(testi).strip()

    def _finalize(self, chunks: list[np.ndarray]) -> None:
        prima = self._raccogli_parziali()
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        coda = ""
        if len(audio) / STT_RATE >= self._min_utt and not _troppo_debole(audio):
            _dump_se_richiesto(audio)
            try:
                coda = self._engine.transcribe(audio, lang=self._lang) or ""
            except Exception as e:  # noqa: BLE001 - errore trascrizione
                log.error("STT nativo: trascrizione fallita: %s", e)
                coda = ""
            if coda and _e_allucinazione(coda):
                log.info("STT: scartata coda-fantasma (%r)", coda[:60])
                coda = ""
        # I chunk gia' trascritti vanno DAVANTI: sono la parte detta prima.
        text = " ".join(p for p in (prima, coda.strip()) if p).strip()
        if not text:
            return
        if not prima and _e_allucinazione(text):
            # Meglio nessuna trascrizione che una frase inventata: passandola
            # all'agente si otterrebbe una risposta a qualcosa mai detto.
            log.info("STT: scartata frase-fantasma da silenzio (%r)", text[:60])
            return
        log.info("STT -> %s", text)
        self.utterances.put(text)
