"""TTS nativo in-process: Kokoro (kokoro-onnx) + client a interfaccia HandyTtsClient.

Sostituisce il WebSocket `/tts/stream` di Handy sintetizzando in-process con
`kokoro-onnx`. Il `NativeTtsClient` espone la STESSA interfaccia di
`HandyTtsClient` (start/configure/send_text/flush/end/close/bump_generation,
coda `audio` di `TtsSegment`), cosi' `StreamingSession` non cambia.

Le voci CLONATE (Chatterbox/ZONOS2) restano fuori scope di questo modulo: il
dispatcher engine<->voce e gli helper subprocess arrivano nella sotto-fase
"Avatar + voci selezionate".
"""

from __future__ import annotations

import glob
import logging
import os
import queue
import re
import threading
import time

import numpy as np

from .. import models_store
from ..tts_client import TtsSegment

log = logging.getLogger("callbridge.engines.tts")

# Split in frasi per iniziare a produrre audio prima di aver sintetizzato tutto.
_SENT_SPLIT = re.compile(r"(.+?(?:[.!?…]+|\n+|$))", re.DOTALL)


def _default_kokoro_dir() -> str:
    """Cartella modelli Kokoro di default: quella della cartella dei modelli vocali."""
    return models_store.folder("tts", "kokoro")


def _richiedi_file(model_dir: str, precision: str) -> str:
    """Il .onnx da aprire, o `ModelNotInstalled` se Kokoro non e' nella cartella."""
    onnx = _pick_onnx(model_dir, precision) if model_dir else None
    if not onnx or not os.path.isfile(os.path.join(model_dir, "voices-v1.0.bin")):
        raise models_store.ModelNotInstalled("kokoro", "tts", model_dir or "no models folder")
    return onnx


def available_precisions(model_dir: str) -> dict[str, str]:
    """Precisioni Kokoro presenti su disco -> {precision: path}. Le varianti sono
    kokoro-v1.0.onnx (fp32), kokoro-v1.0.fp16.onnx (fp16), *.int8.onnx (int8)."""
    out: dict[str, str] = {}
    for p in glob.glob(os.path.join(model_dir, "kokoro*.onnx")):
        low = os.path.basename(p).lower()
        if "fp16" in low:
            out["fp16"] = p
        elif "int8" in low or "quant" in low:
            out["int8"] = p
        else:
            out["fp32"] = p
    return out


def _pick_onnx(model_dir: str, precision: str) -> str | None:
    """File .onnx per la precisione richiesta; ripiega su fp16/fp32/qualsiasi."""
    avail = available_precisions(model_dir)
    if not avail:
        return None
    order = [precision, "fp16", "fp32", "int8"]
    for p in order:
        if p in avail:
            return avail[p]
    return next(iter(avail.values()))


def _split_sentences(text: str) -> list[str]:
    parts = [m.group(1).strip() for m in _SENT_SPLIT.finditer(text)]
    parts = [p for p in parts if p]
    return parts or [text.strip()]


# Pause richieste dopo la punteggiatura, in secondi. Kokoro ne produce di sue,
# ma brevi e irregolari: qui si impone un respiro costante fra i periodi e fra
# le proposizioni, che nel parlato dell'avatar è ciò che si sente come naturale.
_PAUSA_FRASE = ".!?…"
_PAUSA_INCISO = ",;:"
# Sotto questa lunghezza (senza punteggiatura) uno spezzone non si manda da
# solo al motore: si accorpa al precedente. Vedi `_units_with_pauses`.
_MIN_UNIT_CHARS = 24


def _units_with_pauses(text: str, pausa_frase: float,
                       pausa_inciso: float) -> list[tuple[str, float]]:
    """[(testo, pausa_dopo)] spezzando su fine frase e su inciso.

    Lo spezzone conserva il proprio segno di punteggiatura, così l'intonazione
    resta quella giusta; il silenzio viene aggiunto dopo, in coda all'audio.
    """
    unita: list[tuple[str, float]] = []
    corrente = ""
    for i, ch in enumerate(text):
        corrente += ch
        if ch in _PAUSA_FRASE:
            # Sospensione ("..." o "…"): NON è fine frase, è un respiro dentro
            # la frase. Spezzare qui produceva sintesi isolate — "qualcosa di
            # più..." + "succoso" — con la seconda pronunciata fuori contesto.
            # Resta nel testo: la sospensione la rende il modello, e il taglio
            # avviene solo alla punteggiatura vera che segue.
            if ch == "…":
                continue
            if ch == "." and (
                (i + 1 < len(text) and text[i + 1] == ".") or
                (i > 0 and text[i - 1] == ".")):
                continue
            unita.append((corrente.strip(), pausa_frase))
            corrente = ""
        elif ch in _PAUSA_INCISO:
            unita.append((corrente.strip(), pausa_inciso))
            corrente = ""
    if corrente.strip():
        unita.append((corrente.strip(), 0.0))
    # Unisci gli spezzoni vuoti o di sola punteggiatura al precedente, e con
    # loro quelli TROPPO CORTI: i modelli autoregressivi (XTTS, CosyVoice)
    # allucinano sugli input di poche sillabe — continuano a generare oltre il
    # testo e ne esce farfugliato. Misurato su XTTS: "Ciao." produce 7,5s di
    # audio contro 1,1s di "Ciao", e un "." da solo quasi 4s. Spezzare su ogni
    # virgola creava proprio quei frammenti ("Ciao,", "ora.").
    pulite: list[tuple[str, float]] = []
    for testo, pausa in unita:
        troppo_corto = len(testo.strip(" .,;:!?…")) < _MIN_UNIT_CHARS
        if pulite and (not any(c.isalnum() for c in testo) or troppo_corto):
            prec, _ = pulite[-1]
            sep = "" if testo[:1] in ".,;:!?…" else " "
            pulite[-1] = (prec + sep + testo, pausa)
        else:
            pulite.append((testo, pausa))
    # Un'unica unità rimasta corta (risposta brevissima: "Sì.") non si può
    # accorpare a nulla: si manda com'è, meglio un artefatto che il silenzio.
    return pulite


_avviso_gpu_mostrato = False
_avviso_lock = threading.Lock()


def _avvisa_gpu_non_disponibile(errore: str) -> None:
    """Finestra di errore Windows quando il TTS su GPU non funziona.

    NON si ripiega su CPU: sarebbe un degrado silenzioso di ~10x che si nota
    solo dai tempi. Meglio dirlo subito e lasciare che si risolva. Il dialogo
    parte in un thread a sé: è modale per l'utente, non per la sessione vocale.
    Mostrato UNA volta per processo, altrimenti ogni frase aprirebbe una finestra.
    """
    global _avviso_gpu_mostrato
    with _avviso_lock:
        if _avviso_gpu_mostrato:
            return
        _avviso_gpu_mostrato = True

    dettaglio = errore.split(";")[0].strip()[:300]
    testo = (
        "Il motore vocale Kokoro non riesce a usare la GPU e la sintesi è "
        "fallita.\n\n"
        f"Errore: {dettaglio}\n\n"
        "Causa tipica: torch (Whisper) ha già caricato le proprie cuDNN nel "
        "processo e onnxruntime non può usare le sue.\n\n"
        "Come risolvere:\n"
        "• Motori voce → Elaborazione motori → TTS Kokoro: passa a \"CPU\" "
        "(più lento ma funziona), oppure\n"
        "• fai girare Kokoro in un processo separato (vedi note del progetto).\n\n"
        "Finché l'errore persiste l'avatar NON parla."
    )

    def _box() -> None:
        try:
            import ctypes
            # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(
                None, testo, "callAPIcall — TTS su GPU non disponibile",
                0x10 | 0x10000 | 0x40000)
        except Exception as e:  # noqa: BLE001 - niente GUI (servizio/headless)
            log.error("Avviso GPU non mostrabile (%s). Errore TTS: %s", e, dettaglio)

    threading.Thread(target=_box, name="avviso-gpu-tts", daemon=True).start()
    log.error("TTS su GPU NON disponibile: %s — nessun fallback su CPU "
              "(scelta esplicita): l'avatar resta muto finché non si risolve.",
              dettaglio)


def _helper_path() -> str:
    return os.path.join(os.path.dirname(__file__), "kokoro_tts_helper.py")


def _read_exact(pipe, n: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = pipe.read(n - got)
        if not b:
            break                      # EOF: subprocess morto
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


class KokoroTtsEngine:
    """Modello Kokoro caricato una sola volta (condiviso tra le sessioni).

    Su **CPU** gira in-process. Su **GPU** gira in un SUBPROCESS
    (`kokoro_tts_helper.py`): nel processo del bridge c'e' torch, che occupa le
    cuDNN e impedisce a onnxruntime di usare le proprie -> la sintesi ripiegava
    su CPU (~0,5x realtime) o falliva del tutto. Isolato, va sulla GPU davvero:
    misurato ~17x realtime contro ~0,5x. Stesso trucco gia' usato per Chatterbox.
    """

    def __init__(self, model_dir: str = "", device: str = "cpu",
                 memory: str = "dedicated", precision: str = "fp16") -> None:
        self.model_dir = model_dir or _default_kokoro_dir()
        self.device = device
        self.memory = memory
        self.precision = precision
        self._k = None
        self._proc = None                       # helper subprocess (solo GPU)
        self._loaded_on: str | None = None      # device del caricamento in corso
        self._degradato = False                 # GPU fallita -> gia. passato a CPU
        self._loaded_file: str | None = None    # .onnx effettivamente aperto
        self._lock = threading.Lock()

    # --- helper subprocess (solo GPU) ------------------------------------

    def _avvia_helper_locked(self) -> None:
        """Avvia (o riavvia) il subprocess dell'helper Kokoro su GPU."""
        if self._proc is not None and self._proc.poll() is None:
            return
        import subprocess
        import sys
        helper = _helper_path()
        if not os.path.isfile(helper):
            raise FileNotFoundError(f"Helper Kokoro non trovato: {helper}")
        _richiedi_file(self.model_dir, self.precision)
        env = models_store.helper_env("tts")
        env["KOKORO_MODEL_DIR"] = self.model_dir
        env["KOKORO_PRECISION"] = self.precision
        env["KOKORO_DEVICE"] = "cuda"
        env.setdefault("PYTHONUNBUFFERED", "1")
        kw: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        t0 = time.perf_counter()
        # `sys.executable`: stesso interprete del bridge (ha kokoro-onnx e
        # onnxruntime-gpu) ma processo NUOVO, quindi senza torch di mezzo.
        self._proc = subprocess.Popen(
            [sys.executable, "-u", helper],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, env=env, **kw)
        threading.Thread(target=self._drena_stderr, daemon=True,
                         name="kokoro-helper-stderr").start()
        self._loaded_on = "cuda"
        self._loaded_file = os.path.basename(_pick_onnx(self.model_dir, self.precision) or "")
        log.info("TTS nativo pronto: Kokoro %s su CUDA (subprocess, %.1fs; il "
                 "modello si carica alla prima sintesi)",
                 self._loaded_file, time.perf_counter() - t0)

    def _drena_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            riga = raw.decode("utf-8", "replace").rstrip()
            if not riga:
                continue
            # Le righe che contano (provider effettivi, errori) vanno a INFO.
            if "provider=" in riga or "ATTENZIONE" in riga or "fallita" in riga:
                log.info("kokoro-helper: %s", riga)
            else:
                log.debug("kokoro-helper: %s", riga)

    def _chiudi_helper_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.write(b'{"cmd":"quit"}\n')
                proc.stdin.flush()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def load(self) -> None:
        from .device import resolve_device
        if resolve_device(self.device, self.memory) == "cuda":
            with self._lock:
                self._avvia_helper_locked()
            return
        with self._lock:
            if self._proc is not None:      # si torna su CPU: via l'helper
                self._chiudi_helper_locked()
            if self._k is not None:
                return
            from .device import ensure_ort_cuda, resolve_device  # noqa: F811
            from kokoro_onnx import Kokoro
            onnx = _richiedi_file(self.model_dir, self.precision)
            voices = os.path.join(self.model_dir, "voices-v1.0.bin")
            # kokoro-onnx sceglie il provider dall'env ONNX_PROVIDER.
            eff = resolve_device(self.device, self.memory)
            if eff == "cuda":
                ensure_ort_cuda()
                os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
            else:
                os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"
            t0 = time.perf_counter()
            self._k = Kokoro(onnx, voices)
            # Con COSA e' stato caricato davvero: le impostazioni possono essere
            # cambiate dopo (si applicano al prossimo caricamento) e il file puo'
            # differire dalla precisione chiesta (_pick_onnx ripiega). La GUI
            # mostra questo, non l'intenzione.
            self._loaded_on = eff
            self._loaded_file = os.path.basename(onnx)
            log.info("TTS nativo pronto: Kokoro %s su %s (%.1fs)",
                     os.path.basename(onnx), eff.upper(),
                     time.perf_counter() - t0)

    def close(self) -> None:
        """Scarta il modello: chiude l'helper GPU e libera quello in-process.
        Da chiamare quando la GUI cambia device/precisione."""
        with self._lock:
            self._chiudi_helper_locked()
            self._k = None

    def _providers_attivi(self) -> list[str]:
        """Execution provider REALI della sessione onnxruntime.

        Non basta sapere quale device si è chiesto: se le DLL cuDNN non si
        risolvono, onnxruntime ripiega **in silenzio** su CPU e continuare a
        dire "CUDA" nasconde un rallentamento di 10x. Questa è l'unica fonte
        attendibile."""
        for attr in ("sess", "session", "_sess"):
            s = getattr(self._k, attr, None)
            if s is not None and hasattr(s, "get_providers"):
                try:
                    return list(s.get_providers())
                except Exception:  # noqa: BLE001
                    break
        return []

    def runtime_info(self) -> dict:
        """Stato REALE del motore, per la GUI (niente da incrociare col log)."""
        from .device import resolve_device
        vivo_helper = self._proc is not None and self._proc.poll() is None
        if vivo_helper:
            # Su GPU gira nel subprocess: i provider li verifica l'helper, che
            # avvisa su stderr se è dovuto ripiegare (riga "provider=").
            return {
                "configured": f"{self.device}/{self.memory}",
                "would_load_on": "cuda",
                "loaded": True,
                "running_on": "cuda",
                "providers": ["CUDAExecutionProvider (subprocess)"],
                "file": self._loaded_file,
                "precision": self.precision,
            }
        prov = self._providers_attivi() if self._k is not None else []
        # Il primo provider è quello che esegue davvero.
        reale = None
        if prov:
            reale = "cuda" if prov[0].startswith("CUDA") else "cpu"
        elif self._k is not None:
            reale = self._loaded_on
        return {
            "configured": f"{self.device}/{self.memory}",
            "would_load_on": resolve_device(self.device, self.memory),
            "loaded": self._k is not None,
            "running_on": reale,
            "providers": prov,
            "file": self._loaded_file if self._k is not None else None,
            "precision": self.precision,
        }

    def _synth_helper(self, text: str, voice: str, lang: str, speed: float):
        """Sintesi nel subprocess GPU. Serializzata: una pipe sola."""
        import json as _json
        import struct as _struct
        with self._lock:
            self._avvia_helper_locked()
            proc = self._proc
            assert proc is not None and proc.stdin and proc.stdout
            req = _json.dumps({"text": text, "voice": voice or "if_sara",
                               "lang": lang or "it", "speed": float(speed)},
                              ensure_ascii=False) + "\n"
            try:
                proc.stdin.write(req.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._chiudi_helper_locked()
                raise RuntimeError(f"helper Kokoro non scrivibile: {e}") from e
            header = _read_exact(proc.stdout, 8)
            if len(header) < 8:
                self._chiudi_helper_locked()
                raise RuntimeError("helper Kokoro: EOF (processo morto)")
            count, sr = _struct.unpack("<II", header)
            if count == 0:
                raise RuntimeError("sintesi Kokoro fallita (vedi log helper)")
            data = _read_exact(proc.stdout, count * 4)
            if len(data) < count * 4:
                self._chiudi_helper_locked()
                raise RuntimeError("helper Kokoro: audio troncato (EOF)")
            return np.frombuffer(data, dtype="<f4").astype(np.float32), int(sr)

    def synth(self, text: str, voice: str, lang: str, speed: float):
        from .device import resolve_device
        from .kokoro_lang import kokoro_lang
        lang = kokoro_lang(lang)
        if resolve_device(self.device, self.memory) == "cuda":
            return self._synth_helper(text, voice, lang, speed)
        self.load()
        try:
            samples, sr = self._k.create(text, voice=voice, speed=float(speed), lang=lang)
        except Exception as e:  # noqa: BLE001 - vedi sotto
            # CUDA rotta a RUNTIME (tipico: CUDNN_STATUS_..._SUBLIBRARY_UNAVAILABLE
            # perché torch ha già preso le cuDNN del processo). NON si ripiega su
            # CPU: un degrado silenzioso nasconde il guasto e lo si scopre solo
            # dai tempi. Si avvisa a schermo e si propaga l'errore, così il
            # problema è visibile e risolvibile.
            msg = str(e)
            if "CUDNN" in msg.upper() or "CUDA" in msg.upper():
                _avvisa_gpu_non_disponibile(msg)
            raise
        return np.asarray(samples, dtype=np.float32), int(sr)


class NativeTtsClient:
    """Adattatore a interfaccia `HandyTtsClient` sul motore Kokoro locale.

    Bufferizza il testo (`send_text`) e lo sintetizza su `flush`/`end` in un
    thread dedicato, frase per frase, accodando `TtsSegment` in `audio`. Il
    barge-in (`bump_generation`) scarta i job/segmenti della generazione vecchia.
    """

    def __init__(self, engine: KokoroTtsEngine, cfg=None) -> None:
        self._engine = engine
        self._cfg = cfg
        self.audio: queue.Queue[TtsSegment] = queue.Queue()
        # Messaggi per chi ascolta quando un modello manca (ModelNotInstalled).
        self.errors: queue.Queue[str] = queue.Queue()
        self.generation = 0
        self._voice = "if_sara"
        self._lang = "it"
        self._speed = 1.0
        self._engine_id = ""    # motore imposto per la voce corrente ("" = deduci)
        # Respiro imposto dopo la punteggiatura. Letto dalla config a ogni job
        # (vedi _worker), così il valore cambiato dalla GUI vale subito.
        self._pausa_frase = float(getattr(cfg, "tts_pause_sentence_s", 0.5)) if cfg else 0.5
        self._pausa_inciso = float(getattr(cfg, "tts_pause_clause_s", 0.25)) if cfg else 0.25
        self._buf: list[str] = []
        self._jobs: queue.Queue[tuple] = queue.Queue()
        # True mentre il worker sta sintetizzando: permette ai consumatori di
        # sapere QUANDO il turno è finito invece di dedurlo dal silenzio. Senza
        # questo `_collect_pcm` (avatar) aspettava 4 s fissi dopo l'ultimo
        # segmento: con la sintesi a 0,8 s erano 4 s di attesa a vuoto per ogni
        # battuta.
        self._working = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Streaming: numero progressivo delle frasi (i pezzi di una frase lo
        # condividono) e stima di fino a quando chi ascolta ha gia' audio da
        # sentire, per decidere se una frase va consegnata a pezzi.
        self._frase = 0
        self._frase_in_corso = 0
        self._finisci_frase = 0       # frase da completare nonostante un abort
        self._audio_fino_a = 0.0      # monotonic: fine dell'audio gia' consegnato

    def start(self) -> None:
        self._engine.load()
        self._thread = threading.Thread(target=self._worker, name="native-tts",
                                        daemon=True)
        self._thread.start()

    def configure(self, voice: str = "if_sara", lang: str = "it",
                  speed: float = 1.0, engine: str = "") -> None:
        """`engine` = motore da usare per QUESTA voce ("" = lo deduce il router).

        Serve perché le voci clonate esistono su PIÙ motori con lo stesso nome
        (`Pluto` sta su Chatterbox, XTTS e CosyVoice): senza dirlo, la persona
        che ha scelto XTTS si sentiva rispondere da un altro motore."""
        self._voice, self._lang, self._speed = voice, lang, float(speed)
        self._engine_id = (engine or "").strip()

    def send_text(self, chunk: str) -> None:
        if chunk:
            self._buf.append(chunk)

    def flush(self) -> None:
        text = "".join(self._buf).strip()
        self._buf = []
        if text:
            self._jobs.put((text, self.generation, self._voice, self._lang,
                            self._speed, self._engine_id))

    def end(self) -> None:
        self.flush()

    def bump_generation(self) -> int:
        self.generation += 1
        return self.generation

    def abort(self, finisci_frase: bool = False) -> int:
        """Butta tutto il lavoro in corso e in coda. Ritorna la nuova generazione.

        `finisci_frase`: la frase in sintesi in questo momento si completa
        comunque (i suoi pezzi passano alla generazione nuova). Serve al taglio
        "morbido" (riscrittura), che lascia finire la frase in corso: con lo
        streaming una frase arriva a pezzi, e tagliare fra un pezzo e l'altro la
        spezzerebbe a meta' parola.

        Serve quando l'utente interrompe: senza, il testo gia' affidato alla
        sintesi continua a diventare audio e riaffiora nel turno successivo —
        l'avatar riprende a dire una frase che nessuno aspetta piu'. Il solo
        `bump_generation` non basta: i job restano in coda e i segmenti gia'
        prodotti restano in `audio`.

        Quello che NON si puo' fermare e' la frase in sintesi proprio adesso:
        il controllo di generazione dentro il ciclo (`_worker`) la taglia alla
        frase successiva, quindi il residuo massimo e' una frase — lavoro di
        GPU sprecato, non audio consegnato.
        """
        frase = self._frase_in_corso if finisci_frase else 0
        self._finisci_frase = frase
        gen = self.bump_generation()
        tenuti: list[TtsSegment] = []
        for coda in (self._jobs, self.audio):
            while True:
                try:
                    el = coda.get_nowait()
                except queue.Empty:
                    break
                # Pezzi gia' pronti della frase da completare: restano, con la
                # generazione nuova.
                if (frase and coda is self.audio
                        and getattr(el, "frase", 0) == frase):
                    el.generation = gen
                    tenuti.append(el)
        for el in tenuti:
            self.audio.put(el)
        self._buf = []
        return gen

    def busy(self) -> bool:
        """True se c'è ancora testo da sintetizzare per il turno in corso.

        Serve ai consumatori che accumulano l'intera risposta (avatar, copilota)
        per sapere quando fermarsi: senza, l'unico criterio era attendere N
        secondi di silenzio, che con la sintesi veloce è tutta attesa sprecata.
        """
        return self._working or not self._jobs.empty()

    def close(self) -> None:
        self._stop.set()

    # --- worker ----------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                text, gen, voice, lang, speed, engine = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if gen != self.generation:
                continue  # barge-in: job stantio
            self._working = True
            try:
                if self._cfg is not None:   # rileggi: la GUI può averle cambiate
                    self._pausa_frase = float(getattr(self._cfg, "tts_pause_sentence_s", 0.5))
                    self._pausa_inciso = float(getattr(self._cfg, "tts_pause_clause_s", 0.25))
                # Numeri, unita' e sigle in forma pronunciabile: senza questo il
                # motore legge "15 km" come "quindici kappa emme" e "1250" cifra
                # per cifra. Solo per l'italiano: le regole di lettura sono sue.
                if (lang or "").lower().startswith("it") and getattr(
                        self._cfg, "tts_normalize_text", True):
                    from ..text_normalize import normalizza_per_tts
                    testo_orig = text
                    text = normalizza_per_tts(text)
                    if text != testo_orig:
                        log.debug("TTS normalizzato: %r -> %r",
                                  testo_orig[:60], text[:60])
                for sent, pausa in _units_with_pauses(text, self._pausa_frase,
                                                      self._pausa_inciso):
                    if self._stop.is_set() or gen != self.generation:
                        break
                    self._frase += 1
                    self._frase_in_corso = self._frase
                    if self._va_streammata(voice, engine):
                        if self._sintetizza_a_pezzi(sent, pausa, gen, voice, lang,
                                                    speed, engine) != gen:
                            # Interruzione morbida a meta': la frase e' stata
                            # completata, il resto del vecchio testo no.
                            break
                        continue
                    try:
                        # `engine` esplicito quando la persona ne ha scelto uno:
                        # il router altrimenti lo deduce dal NOME della voce, e
                        # le voci clonate hanno lo stesso nome su piu' motori.
                        samples, sr = (self._engine.synth(sent, voice, lang, speed, engine)
                                       if engine else
                                       self._engine.synth(sent, voice, lang, speed))
                    except Exception as e:  # noqa: BLE001 - sintesi fallita
                        log.error("TTS nativo: sintesi fallita per %r: %s", sent[:60], e)
                        self._segnala(e)
                        continue
                    if pausa > 0:
                        # Il silenzio va in coda allo STESSO segmento: un segmento a
                        # parte verrebbe trattato come un altro pezzo di parlato
                        # (e il barge-in potrebbe troncarlo a metà).
                        quiete = np.zeros(int(sr * pausa), dtype=samples.dtype)
                        samples = np.concatenate([samples, quiete])
                    self._consegna(TtsSegment(samples=samples, sample_rate=sr,
                                              generation=gen, testo=sent,
                                              frase=self._frase))
            finally:
                self._working = False
                self._frase_in_corso = 0

    # --- streaming ---------------------------------------------------------

    def _segnala(self, e: Exception) -> None:
        """Un modello mancante si dice a chi ascolta; gli altri errori restano a log."""
        if isinstance(e, models_store.ModelNotInstalled):
            self.errors.put(str(e))

    def _consegna(self, seg: TtsSegment) -> None:
        """Accoda un segmento e aggiorna la stima dell'audio gia' pronto per chi
        ascolta (si assume che la riproduzione parta appena arriva)."""
        dur = len(seg.samples) / float(seg.sample_rate or 24000)
        ora = time.monotonic()
        self._audio_fino_a = max(ora, self._audio_fino_a) + dur
        self.audio.put(seg)

    def _va_streammata(self, voice: str, engine: str) -> bool:
        """Questa frase va consegnata a pezzi?

        Si', se il motore lo sa fare E chi ascolta sta per restare senza audio:
        e' il caso della prima frase di ogni risposta e di una sintesi rimasta
        indietro. Con abbastanza audio gia' in coda la frase si genera intera,
        come sempre: spezzettarla non accorcerebbe nessuna attesa.
        """
        cfg = self._cfg
        if cfg is None or not getattr(cfg, "tts_streaming", True):
            return False
        puo = getattr(self._engine, "puo_streammare", None)
        if not callable(puo):
            return False
        margine = self._audio_fino_a - time.monotonic()
        if margine >= float(getattr(cfg, "tts_stream_margin_s", 2.0)):
            return False
        try:
            return bool(puo(voice, engine))
        except Exception:  # noqa: BLE001 - nel dubbio, frase intera
            return False

    def _sintetizza_a_pezzi(self, sent: str, pausa: float, gen: int, voice: str,
                            lang: str, speed: float, engine: str) -> int:
        """Consegna `sent` a pezzi mentre il motore la genera.

        Ritorna la generazione a cui sono stati attribuiti i pezzi: cambia se
        un `abort(finisci_frase=True)` arriva a meta' (la frase si completa
        sulla generazione nuova)."""
        frase = self._frase
        primo = True
        sr = 24000
        t0 = time.perf_counter()
        stream = self._engine.synth_stream(sent, voice, lang, speed, engine)
        try:
            for samples, sr in stream:
                if self._stop.is_set():
                    break
                if gen != self.generation:
                    if self._finisci_frase == frase:
                        gen = self.generation       # si completa la frase
                    else:
                        break                       # interruzione: basta cosi'
                if primo:
                    log.info("TTS streaming: primo audio in %.0f ms (%r)",
                             (time.perf_counter() - t0) * 1000, sent[:40])
                self._consegna(TtsSegment(samples=samples, sample_rate=sr,
                                          generation=gen,
                                          testo=sent if primo else "",
                                          frase=frase, ultimo=False))
                primo = False
        except Exception as e:  # noqa: BLE001 - sintesi fallita
            log.error("TTS nativo: streaming fallito per %r: %s", sent[:60], e)
            self._segnala(e)
        finally:
            stream.close()          # svuota la pipe dell'helper se ci si ferma prima
            if self._finisci_frase == frase:
                self._finisci_frase = 0
        # Chiusura della frase: la pausa (o un istante di silenzio) come ultimo
        # pezzo, cosi' i consumatori sanno che la frase e' completa.
        if not primo and not self._stop.is_set() and gen == self.generation:
            quiete = np.zeros(max(1, int(sr * max(pausa, 0.0))), dtype=np.float32)
            self._consegna(TtsSegment(samples=quiete, sample_rate=sr,
                                      generation=gen, testo="", frase=frase,
                                      ultimo=True))
        return gen
