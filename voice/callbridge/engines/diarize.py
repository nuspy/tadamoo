"""Diarizzazione: chi parla quando ("Speaker 1", "Speaker 2", ...).

TRASCRIVI-POI-DIARIZZA, non il contrario. Whisper trascrive l'audio intero e
restituisce i tempi (segmenti, meglio ancora parole); questo modulo, in
parallelo, produce le fasce `[start, end, speaker]`; `assign_speakers()` incrocia
le due cose per sovrapposizione temporale.

Tagliare l'audio per timbro PRIMA dello STT — l'idea intuitiva — rende molto
peggio: whisper ragiona su una finestra da 30 s e su spezzoni da 1-2 s allucina,
ripete e sbaglia lingua sui back-channel ("si'", "ok", "mhm"); paga comunque
l'encoder sui 30 s pieni a ogni chiamata (400 turni = 400 inferenze invece di
12); e i confini di turno cadono in mezzo alle parole, distruggendo proprio le
sovrapposizioni in cui due persone parlano insieme.

Modelli (ONNX, in `models/diarization/`): pyannote-segmentation-3.0 dice "quante
voci e quali sono attive adesso" su finestre da 10 s, NeMo TitaNet ne estrae
l'impronta vocale, il clustering di sherpa-onnx raggruppa le impronte in speaker.
Nessun token HuggingFace, nessuna licenza da accettare, nessun torch: gira
sull'onnxruntime che sherpa-onnx si porta dietro.

VINCOLO che governa il resto della pipeline: il clustering e' PER-FILE. Diarizzare
i blocchi da 300 s uno per uno darebbe uno "Speaker 1" diverso in ogni blocco, per
questo si diarizza l'audio INTERO in una passata sola.
"""

from __future__ import annotations

import logging
import os
import threading
import wave

import numpy as np

from .. import models_store

log = logging.getLogger("callbridge.diarize")

SAMPLE_RATE = 16000        # i modelli lavorano a 16 kHz mono
_MERGE_GAP_S = 1.0         # gap sotto il quale due turni dello stesso speaker si fondono
_MIN_TURN_S = 0.8          # sotto questa durata un cambio di speaker isolato non e' un turno

# Nomi dei file cercati sotto la cartella dei modelli. Il segmentatore sta in una
# sottocartella perche' l'archivio ufficiale si scompatta cosi'.
_SEG_RELPATH = os.path.join("sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
# Ordine di preferenza. CAM++ per primo: su un'intervista reale di 5 minuti ha
# separato le due voci come TitaNet large (166 s / 108 s) impiegando la meta' del
# tempo (19x realtime contro 9x) e pesando 28 MB invece di 101.
_EMB_NAMES = (
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
    "nemo_en_titanet_large.onnx",
    "nemo_en_titanet_small.onnx",
    "wespeaker_en_voxceleb_CAM++.onnx",
)
# Un cluster con cosi' poco parlato non e' una persona: e' un "mhm" isolato o un
# rumore che il clustering non sa dove mettere. Si riassorbe nel turno vicino.
_MIN_SPEAKER_S = 5.0
_MIN_SPEAKER_FRAZIONE = 0.01


def models_dir(cfg) -> str:
    """Cartella dei modelli di diarizzazione (config, poi il default nel repo)."""
    esplicita = (getattr(cfg, "diarization_model_dir", "") or "").strip()
    if esplicita:
        return os.path.expandvars(esplicita)
    return models_store.folder("diarization", "sherpa-diarization")


def find_models(cfg) -> tuple[str, str] | None:
    """(segmentazione, embedding) o None se i modelli non ci sono."""
    root = models_dir(cfg)
    seg = os.path.join(root, _SEG_RELPATH)
    if not os.path.isfile(seg):
        return None
    for nome in _EMB_NAMES:
        emb = os.path.join(root, nome)
        if os.path.isfile(emb):
            return os.path.abspath(seg), os.path.abspath(emb)
    return None


def available(cfg) -> tuple[bool, str]:
    """(utilizzabile, perche' no). La diarizzazione e' un DI PIU': se manca il
    pacchetto o i modelli la trascrizione deve uscire lo stesso, senza speaker."""
    from ..sospensione import MSG_SOSPESO, in_sospensione
    if in_sospensione():
        return False, MSG_SOSPESO          # da sospeso il modello non si carica
    if not bool(getattr(cfg, "diarization_enabled", True)):
        return False, "diarizzazione disabilitata in configurazione"
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False, "pacchetto `sherpa-onnx` non installato"
    if find_models(cfg) is None:
        return False, models_store.reason("sherpa-diarization")
    return True, ""


class SpeakerDiarizer:
    """Diarizzatore offline. Caricamento pigro, un'analisi per volta.

    `num_speakers` > 0 fissa il numero di voci: quando lo si sa (un'intervista e'
    a due) e' di gran lunga il modo piu' affidabile. A 0 lo decide il clustering
    con `threshold`, e la soglia e' delicata: sulla stessa intervista CAM++ dava
    7 speaker a 0.8, i 2 giusti a 1.0 e un solo speaker a 1.2. Nota che la scala
    dipende dal modello di embedding — cambiandolo, la soglia va ritarata."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._sd = None
        self._parti: tuple | None = None    # (seg, emb, provider, threads) per set_config
        self._num_speakers = -1             # quello attualmente configurato
        self._lock = threading.Lock()

    def _config(self, num_speakers: int):
        import sherpa_onnx
        seg, emb, provider, threads = self._parti
        return sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=seg),
                provider=provider, num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=emb, provider=provider, num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers if num_speakers > 0 else -1,
                threshold=float(getattr(self.cfg, "diarization_threshold", 1.0))),
            min_duration_on=0.3,     # sotto i 300 ms non e' un turno, e' un respiro
            min_duration_off=0.5,    # pause piu' corte non spezzano il turno
        )

    def load(self) -> None:
        with self._lock:
            if self._sd is not None:
                return
            import sherpa_onnx
            from .device import ensure_ort_cuda, resolve_device
            trovati = find_models(self.cfg)
            if trovati is None:
                raise models_store.ModelNotInstalled(
                    "sherpa-diarization", "diarization", models_dir(self.cfg))
            seg, emb = trovati
            device = resolve_device(getattr(self.cfg, "diarization_device", "cpu"))
            if device == "cuda":
                # sherpa-onnx si porta il PROPRIO onnxruntime: l'EP CUDA c'e'
                # solo nei wheel `sherpa-onnx` build CUDA. Con quello di pip
                # (CPU) il provider viene ignorato e si finisce su CPU comunque
                # — che per 5 minuti di audio significa 16 secondi, non ore.
                ensure_ort_cuda()
            provider = "cuda" if device == "cuda" else "cpu"
            threads = max(1, int(getattr(self.cfg, "diarization_num_threads", 4)))
            self._parti = (seg, emb, provider, threads)
            self._num_speakers = max(0, int(
                getattr(self.cfg, "diarization_num_speakers", 0)))
            config = self._config(self._num_speakers)
            if not config.validate():
                raise RuntimeError("configurazione di diarizzazione non valida "
                                   f"(segmentazione: {seg}, embedding: {emb})")
            self._sd = sherpa_onnx.OfflineSpeakerDiarization(config)
            log.info("Diarizzazione pronta: pyannote-segmentation + %s su %s "
                     "(%s)", os.path.basename(emb), provider,
                     f"{self._num_speakers} speaker fissi" if self._num_speakers
                     else "numero di speaker automatico")

    def scarica(self) -> bool:
        """Toglie i modelli dalla memoria (si ricaricano alla prossima analisi).
        True se c'era qualcosa."""
        with self._lock:
            if self._sd is None:
                return False
            self._sd = None
            self._parti = None
            self._num_speakers = -1
        from ..memoria import rilascia
        rilascia(cuda=False)
        return True

    def diarize(self, samples: np.ndarray, progress=None,
                num_speakers: int = -1) -> list[dict]:
        """`samples` float32 mono a 16 kHz -> `[{start_s, end_s, speaker}]`.

        `num_speakers` >= 0 sovrascrive per questa chiamata il valore di config
        (0 = automatico): il numero di voci e' una proprieta' del singolo file,
        non dell'installazione. `progress(frazione)` con frazione 0..1 — un'ora
        di audio non e' istantanea e la GUI non deve restare ferma."""
        self.load()
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        if audio.size == 0:
            return []
        def _cb(processati: int, totali: int) -> int:
            if progress and totali > 0:
                try:
                    progress(min(1.0, processati / totali))
                except Exception:  # noqa: BLE001 - il progresso non deve fermare l'analisi
                    pass
            return 0

        # Riconfigurazione e analisi sotto lo STESSO lock: sono un'operazione
        # sola. Separandole, un secondo job potrebbe cambiare il numero di
        # speaker mentre questo sta processando.
        with self._lock:
            if num_speakers >= 0 and num_speakers != self._num_speakers:
                self._sd.set_config(self._config(num_speakers))
                self._num_speakers = num_speakers
                log.info("Diarizzazione: %s", f"{num_speakers} speaker fissi"
                         if num_speakers else "numero di speaker automatico")
            res = self._sd.process(audio, callback=_cb)
        turni = _pulisci([{"start_s": float(s.start), "end_s": float(s.end),
                           "speaker": int(s.speaker)}
                          for s in res.sort_by_start_time()])
        log.info("Diarizzazione: %d turni, %d speaker su %.0f s di audio",
                 len(turni), len({t["speaker"] for t in turni}),
                 audio.size / SAMPLE_RATE)
        return turni


_engine: SpeakerDiarizer | None = None
_engine_lock = threading.Lock()


def get_diarizer(cfg) -> SpeakerDiarizer:
    """Singleton: i modelli si caricano una volta sola per processo."""
    global _engine
    from ..sospensione import richiedi_attivi
    richiedi_attivi()
    with _engine_lock:
        if _engine is None:
            _engine = SpeakerDiarizer(cfg)
        return _engine


def label(idx: int) -> str:
    """Indice del cluster -> etichetta umana. Senza nomi: la diarizzazione sa
    distinguere le voci, non a chi appartengono."""
    return f"Speaker {idx + 1}"


def _pulisci(turni: list[dict]) -> list[dict]:
    """Riassorbe i cluster trascurabili e rinumera gli speaker per apparizione.

    Due difetti tipici del clustering automatico, entrambi visibili nel test
    reale: i back-channel ("mhm", "yes") finiscono in micro-cluster propri, e gli
    indici restano quelli interni (Speaker 8, Speaker 12, con buchi). Qui i
    cluster sotto `_MIN_SPEAKER_S` **e** sotto l'1% del parlato passano al
    vicino, poi si rinumera: Speaker 1 e' chi parla per primo.

    Il compromesso e' esplicito: in una riunione, chi dice solo "ciao" viene
    attribuito a qualcun altro. Chi ha davvero due parole da dire supera l'1%."""
    if not turni:
        return []
    totale = sum(t["end_s"] - t["start_s"] for t in turni)
    durate: dict[int, float] = {}
    for t in turni:
        durate[t["speaker"]] = durate.get(t["speaker"], 0.0) + t["end_s"] - t["start_s"]
    buoni = {s for s, d in durate.items()
             if d >= _MIN_SPEAKER_S or d >= _MIN_SPEAKER_FRAZIONE * totale}
    if not buoni:                      # tutti minuscoli: si tiene il piu' lungo
        buoni = {max(durate, key=durate.get)}
    if len(buoni) < len(durate):
        log.info("Diarizzazione: %d cluster trascurabili riassorbiti "
                 "(sotto %.0fs e sotto l'1%% del parlato)",
                 len(durate) - len(buoni), _MIN_SPEAKER_S)
    ripulito: list[dict] = []
    for t in turni:
        if t["speaker"] in buoni:
            ripulito.append(dict(t))
        elif ripulito:
            ripulito[-1]["end_s"] = max(ripulito[-1]["end_s"], t["end_s"])
        else:
            # micro-cluster in apertura: prende il primo speaker buono che segue
            seguente = next((x["speaker"] for x in turni if x["speaker"] in buoni),
                            t["speaker"])
            ripulito.append({**t, "speaker": seguente})
    ordine: dict[int, int] = {}
    for t in ripulito:
        ordine.setdefault(t["speaker"], len(ordine))
    return [{"start_s": t["start_s"], "end_s": t["end_s"],
             "speaker": label(ordine[t["speaker"]])} for t in ripulito]


def read_wavs(paths) -> np.ndarray:
    """Concatena dei wav PCM mono 16 kHz in un unico array float32.

    La pipeline di analisi ha gia' spezzato l'audio in blocchi per lo STT: si
    rileggono quelli invece di far riscrivere a ffmpeg un secondo file intero."""
    pezzi: list[np.ndarray] = []
    for p in paths:
        with wave.open(str(p), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 \
                    or w.getframerate() != SAMPLE_RATE:
                raise RuntimeError(
                    f"{os.path.basename(str(p))}: atteso PCM mono 16 bit a "
                    f"{SAMPLE_RATE} Hz, trovato {w.getnchannels()}ch/"
                    f"{8 * w.getsampwidth()}bit/{w.getframerate()}Hz")
            raw = w.readframes(w.getnframes())
        pezzi.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
    if not pezzi:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pezzi)


def _speaker_at(turni: list[dict], start: float, end: float) -> str:
    """Speaker della fascia che si sovrappone di piu' a [start, end].

    Nessuna sovrapposizione (parola caduta in una pausa, o musica) -> il turno
    piu' vicino: meglio attribuire che lasciare orfana una riga di dialogo."""
    if not turni:
        return ""
    migliore, quanto = "", 0.0
    for t in turni:
        ov = min(end, t["end_s"]) - max(start, t["start_s"])
        if ov > quanto:
            migliore, quanto = t["speaker"], ov
    if migliore:
        return migliore
    centro = (start + end) / 2.0
    vicino = min(turni, key=lambda t: min(abs(centro - t["start_s"]),
                                          abs(centro - t["end_s"])))
    return vicino["speaker"]


def assign_speakers(segments: list[dict], turni: list[dict]) -> list[dict]:
    """Incrocia i segmenti trascritti con le fasce di speaker.

    Con le PAROLE (`words`) il taglio e' fine: ogni parola prende lo speaker
    della fascia che la contiene e le parole consecutive dello stesso speaker
    tornano insieme in un turno — cosi' una frase in cui il primo interlocutore
    viene interrotto si spezza dove e' stata davvero interrotta. Senza parole si
    etichetta il segmento intero (whisper ne fa uno per frase: l'errore resta
    limitato ai turni che si accavallano).

    I turni contigui dello stesso speaker separati da meno di un secondo si
    fondono: sono la stessa battuta, spezzata da un respiro."""
    if not turni:
        return segments
    fuori: list[dict] = []
    for seg in segments:
        parole = [w for w in (seg.get("words") or [])
                  if w.get("start_s") is not None and (w.get("text") or "").strip()]
        if not parole:
            fuori.append({**{k: v for k, v in seg.items() if k != "words"},
                          "speaker": _speaker_at(turni, float(seg["start_s"]),
                                                 float(seg["end_s"]))})
            continue
        corrente: dict | None = None
        for w in parole:
            chi = _speaker_at(turni, float(w["start_s"]), float(w["end_s"]))
            if corrente is not None and corrente["speaker"] == chi:
                corrente["end_s"] = float(w["end_s"])
                corrente["text"] += w["text"]
            else:
                if corrente is not None:
                    fuori.append(corrente)
                corrente = {"start_s": float(w["start_s"]),
                            "end_s": float(w["end_s"]),
                            "text": w["text"].lstrip(), "speaker": chi}
        if corrente is not None:
            fuori.append(corrente)
    return _merge(_smussa(fuori))


def _smussa(segments: list[dict]) -> list[dict]:
    """Toglie i micro-turni incastrati tra due battute dello stesso speaker.

    Sul test reale una singola parola ("ami") finiva attribuita all'altro
    interlocutore in mezzo a una frase: e' quasi sempre un'eco, un "mhm" o un
    errore di allineamento di mezzo secondo, non un vero cambio di turno."""
    out = list(segments)
    for i in range(1, len(out) - 1):
        durata = float(out[i]["end_s"]) - float(out[i]["start_s"])
        if durata < _MIN_TURN_S and out[i - 1]["speaker"] == out[i + 1]["speaker"] \
                and out[i]["speaker"] != out[i - 1]["speaker"]:
            out[i] = {**out[i], "speaker": out[i - 1]["speaker"]}
    return out


def _merge(segments: list[dict]) -> list[dict]:
    """Fonde i segmenti adiacenti dello stesso speaker (gap < `_MERGE_GAP_S`)."""
    out: list[dict] = []
    for seg in segments:
        testo = (seg.get("text") or "").strip()
        if not testo:
            continue
        seg = {**seg, "text": testo}
        prec = out[-1] if out else None
        if prec is not None and prec.get("speaker") == seg.get("speaker") \
                and float(seg["start_s"]) - float(prec["end_s"]) < _MERGE_GAP_S:
            prec["text"] = (prec["text"] + " " + testo).strip()
            prec["end_s"] = float(seg["end_s"])
            continue
        out.append(seg)
    return out
