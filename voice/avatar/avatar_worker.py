"""Worker dell'avatar video (runtime MuseTalk) — processo dedicato.

Gira nel venv `musetalk-venv` con CWD = cartella del repo MuseTalk (importa i
suoi moduli e trova i pesi in ./models). L'orchestratore (callbridge) lo
avvia via `callbridge/avatar_client.py` e gli parla a righe JSON su stdin:

  {"cmd": "persona", "dir": "<path persona>", "clip": "<path clip opzionale>"}
      carica la persona: sceglie un clip (a caso se non indicato), fa la
      PREPARAZIONE MuseTalk (landmark/latenti/maschere — cache su disco, la
      prima volta richiede qualche minuto) e parte il loop di idle.
  {"cmd": "speak", "wav": "<path wav>"}
      sintetizza il labiale sul loop per l'audio dato e lo riproduce
      (video sul sink + audio sul device configurato), poi torna in idle.
  {"cmd": "quit"}

Eventi su stdout (righe JSON): {"event": "ready"|"persona_ready"|"speaking"|
"idle"|"error", ...}.

Sink video: webcam virtuale OBS via pyvirtualcam se disponibile (selezionabile
come camera in Meet/Teams/Zoom), altrimenti finestra di anteprima OpenCV.
Audio: device `AVATAR_AUDIO_DEVICE` (indice sounddevice; per parlare DENTRO la
call usare un cavo virtuale impostato come microfono dell'app di chiamata).

Streaming frame-by-frame (v2): la generazione procede a blocchi (BATCH_SIZE
frame) in un thread produttore; la riproduzione parte appena il PRIMO blocco
e' pronto (~86 ms, vedi BATCH_SIZE) e consuma i frame a 25 fps ancorata all'orologio
dell'audio. Se la generazione resta momentaneamente indietro si tiene a
schermo l'ultimo frame e si riallinea scartando i ritardatari (il sync col
parlato vince sulla completezza). La battuta e' INTERROMPIBILE col comando
{"cmd": "interrupt"}: audio fermato, coda svuotata, ritorno all'idle.


"""
from __future__ import annotations

import os as _os

# Prima di qualunque import di torch: la memoria che torch libera torna subito
# al sistema invece di restare committata (vedi call-bridge/callbridge/memoria.py).
_os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import copy
import glob
import json
import os
import pickle
import queue
import random
import shutil
import struct
import sys
import threading
import time

import cv2
import numpy as np

# --- Anteprima locale (finestra "callAPIcall — Avatar") ---------------------
# Con AVATAR_PREVIEW=1 la finestra si apre SUBITO all'avvio del worker (qui,
# PRIMA degli import pesanti torch/musetalk che costano ~1 min) con una
# schermata di caricamento; quando i frame veri arrivano, VideoSink li spinge
# nella coda. Un SOLO thread possiede la GUI cv2 (imshow/waitKey).
_PREVIEW_TITLE = "callAPIcall - Avatar"
_preview_q: "queue.Queue" = queue.Queue(maxsize=4)
_preview_started = False


def _preview_status_frame(text: str) -> "np.ndarray":
    frame = np.zeros((512, 512, 3), dtype=np.uint8)
    cv2.putText(frame, "callAPIcall Avatar", (70, 230),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (210, 210, 210), 2, cv2.LINE_AA)
    cv2.putText(frame, text, (70, 290),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (120, 200, 255), 2, cv2.LINE_AA)
    return frame


def _preview_push(frame) -> None:
    try:
        _preview_q.put_nowait(frame)
    except queue.Full:
        pass  # la riproduzione vince: un frame perso in anteprima non conta


def _preview_status(text: str) -> None:
    if _preview_started:
        _preview_push(_preview_status_frame(text))


# Visibilita' della finestra: True alla nascita del worker; il comando stdin
# {"cmd": "preview", "show": bool} la cambia (sessione chiusa -> finestra via,
# worker resta caldo per la sessione successiva).
_preview_show = threading.Event()
_preview_show.set()


def _preview_loop() -> None:
    frame = _preview_status_frame("caricamento modelli (~1 min)...")
    shown = False
    topmost = False
    while True:
        try:
            frame = _preview_q.get(timeout=0.05)
        except queue.Empty:
            pass
        if not _preview_show.is_set():
            if shown:
                try:
                    cv2.destroyWindow(_PREVIEW_TITLE)
                    cv2.waitKey(1)
                except Exception:
                    pass
                shown = False
                topmost = False
            time.sleep(0.1)
            continue
        cv2.imshow(_PREVIEW_TITLE, frame)
        shown = True
        if not topmost:
            # Il worker parte da un processo in background: senza TOPMOST la
            # finestra nasce DIETRO alle app aperte e non si vede mai.
            try:
                cv2.setWindowProperty(_PREVIEW_TITLE, cv2.WND_PROP_TOPMOST, 1)
            except Exception:
                pass
            topmost = True
        cv2.waitKey(1)


def _preview_start() -> None:
    global _preview_started
    if _preview_started:
        return
    _preview_started = True
    threading.Thread(target=_preview_loop, name="preview", daemon=True).start()


if os.environ.get("AVATAR_PREVIEW") == "1":
    _preview_start()

import torch

# torch >= 2.6 ha weights_only=True di default in torch.load: i checkpoint di
# MuseTalk (unet/dwpose/bisenet) sono in formati legacy e verrebbero rifiutati.
# Sono pesi locali scaricati dai repo ufficiali (fidati): ripristiniamo il
# comportamento pre-2.6 SOLO in questo worker.
_torch_load = torch.load


def _load_trusted(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    return _torch_load(*args, **kwargs)


torch.load = _load_trusted

# Leve di prestazione, gratuite e senza effetti sulla qualita' (misurate su
# RTX 5090, batch UNet+VAE ripetuto 12 volte, GPU libera):
#   BATCH_SIZE   2       4       6       8      12      16
#   prima     64,9ms  71,2ms  84,5ms  95,9ms 121,2ms 144,1ms
#   dopo      51,3ms  66,5ms  77,4ms  86,0ms 110,8ms 136,4ms
#   guadagno   -21%    -6,5%   -8,4%  -10,4%   -8,5%   -5,3%
# VRAM identica (1807 MB di pesi in fp16, invariata).
# - cudnn.benchmark: l'UNet gira SEMPRE sulla stessa shape (BATCH_SIZE fisso),
#   quindi l'autotune cuDNN sceglie l'algoritmo una volta e lo riusa. E' il caso
#   in cui benchmark=True conviene sempre; con shape variabili farebbe il
#   contrario (riautotune a ogni shape nuova).
# - TF32: matmul/conv in tensor-core a 19 bit di mantissa. Su pesi fp16 la
#   perdita e' sotto il rumore di quantizzazione del modello.
if torch.cuda.is_available():
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

# Fotogrammi al secondo generati. E' la leva piu' diretta sul carico GPU
# dell'avatar: il lavoro di UNet/VAE e' proporzionale al numero di fotogrammi,
# quindi scendere da 25 a 16 toglie il 36% del calcolo. Per una testa che parla
# 16 fps restano fluidi — il cinema ne usa 24 e i primi film muti 16.
#
# L'allineamento audio-labiale non si rompe: `get_whisper_chunk` riceve `fps` e
# ricampiona le feature di conseguenza, quindi ogni fotogramma copre la fetta
# di audio giusta. E il ritmo della riproduzione lo detta `frame_period` qui
# sotto, ancorato all'orologio: meno fotogrammi al secondo, non un video piu'
# lento. La traccia WebRTC del gateway non deve combaciare — pubblica a
# cadenza sua l'ultimo fotogramma disponibile.
FPS = int(os.environ.get("AVATAR_FPS") or 25)
# Altezza dei frame generati. 480p e' la risoluzione con cui l'avatar viaggia
# verso l'app: generare gia' a questa misura evita di fondere e trasportare
# fotogrammi a piena risoluzione per poi ridurli. 0 = nessun ridimensionamento.
OUTPUT_HEIGHT = int(os.environ.get("AVATAR_OUTPUT_HEIGHT", "480") or 0)
EXTRA_MARGIN = 10
PARSING_MODE = "jaw"
# Frame generati per passata sulla GPU. Governa la LATENZA della prima battuta:
# il consumatore non parte finche' il primo blocco non e' completo, quindi il
# ritardo e' il tempo di calcolo di UN batch, non BATCH_SIZE/FPS.
# Misure (RTX 5090, con le leve qui sopra attive):
#   BATCH_SIZE    2       4       6       8      12
#   latenza    54,9ms  72,8ms  76,8ms  89,7ms 114,9ms
#   picco GPU   266MB   522MB   778MB  1035MB  1547MB   (oltre i pesi)
#   throughput  36fps   55fps   78fps   89fps  104fps   (servono 25: FPS)
# 4 e' il punto giusto: PICCO DIMEZZATO rispetto a 8 (522 contro 1035 MB
# oltre i pesi) e latenza pure piu' bassa, tenendo comunque 2,2x il tempo
# reale di margine. Il picco conta piu' del throughput: e' la memoria che il
# worker chiede mentre elabora, ed e' quella che spinge il driver a sfrattare
# in shared una fetta del modello LLM (con crollo da 114 a 10 token/s,
# misurato). Sotto 4 il margine sul tempo reale si assottiglia troppo.
BATCH_SIZE = 4


def _limita_altezza(frame, altezza: int):
    """Riduce il frame a `altezza` px mantenendo le proporzioni (mai ingrandisce).

    La larghezza viene portata a un numero PARI: i codec video (H.264 in
    particolare, quello di WebRTC) rifiutano dimensioni dispari."""
    if not altezza:
        return frame
    h, w = frame.shape[:2]
    if h <= altezza:
        return frame
    nw = int(round(w * altezza / h))
    nw += nw % 2
    return cv2.resize(frame, (nw, altezza), interpolation=cv2.INTER_AREA)


MUSETALK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(MUSETALK_DIR, "MuseTalk"))
os.chdir(os.path.join(MUSETALK_DIR, "MuseTalk"))

# DWPose/scipy importati SUBITO, prima di transformers/Engine: importarli
# lazy dentro prepare_clip deadlocka il loader DLL di Windows (scipy
# _fitpack resta appeso in create_module col processo gia' pieno di thread
# — diagnosticato con py-spy). Costa ~1 min in piu' all'avvio del worker,
# ma la preparazione diventa affidabile.
from musetalk.utils.preprocessing import get_landmark_and_bbox     # noqa: E402

from musetalk.utils.audio_processor import AudioProcessor          # noqa: E402
from musetalk.utils.blending import (                              # noqa: E402
    get_image_blending, get_image_prepare_material)
from musetalk.utils.face_parsing import FaceParsing                # noqa: E402
from musetalk.utils.utils import datagen, load_all_model           # noqa: E402


def log(*a) -> None:
    print("[avatar_worker]", *a, file=sys.stderr, flush=True)


def emit(event: str, **kw) -> None:
    print(json.dumps({"event": event, **kw}), flush=True)


# ---------------------------------------------------------------------------
#  Progress + ETA della preparazione clip
# ---------------------------------------------------------------------------
STATS_PATH = os.path.join(MUSETALK_DIR, "cache", "prepare-stats.json")


class PrepProgress:
    """Emette eventi {"event":"progress", phase, done, total, percent, eta_s}
    (throttled a 2/s) e mantiene le statistiche di velocita' per fase
    (sec/frame, EMA) in prepare-stats.json: l'ETA usa la velocita' LIVE della
    fase corrente e le statistiche storiche per le fasi future."""

    PHASES = ("extract", "landmark", "latents", "masks")

    def __init__(self) -> None:
        self.stats: dict = {}
        try:
            with open(STATS_PATH, encoding="utf-8") as f:
                self.stats = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.stats = {}
        self.totals: dict[str, int] = {}
        self.phase: str | None = None
        self.done = 0
        self.total = 1
        self._t0 = 0.0
        self._last_emit = 0.0

    def set_totals(self, n_frames: int) -> None:
        n = max(1, n_frames)
        self.totals = {"extract": n, "landmark": n,
                       "latents": n, "masks": 2 * n}

    def begin(self, phase: str, total: int) -> None:
        self.phase = phase
        self.total = max(1, total)
        self.totals[phase] = self.total
        self.done = 0
        self._t0 = time.monotonic()
        self._last_emit = 0.0
        self._emit(force=True)

    def step(self, done: int) -> None:
        self.done = done
        self._emit()

    def end(self) -> None:
        if self.phase is None:
            return
        elapsed = time.monotonic() - self._t0
        if self.done > 0 and elapsed > 0:
            spf = elapsed / self.done
            cur = self.stats.get(self.phase) or {}
            old = cur.get("spf")
            cur["spf"] = spf if old is None else round(0.7 * old + 0.3 * spf, 5)
            cur["n"] = int(cur.get("n", 0)) + 1
            self.stats[self.phase] = cur
            try:
                os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
                with open(STATS_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.stats, f, indent=2)
            except OSError as e:  # noqa: BLE001
                log(f"stats di preparazione non salvate: {e}")
        self.done = self.total
        self._emit(force=True)
        self.phase = None

    def _eta_s(self) -> float | None:
        if self.phase is None:
            return None
        elapsed = time.monotonic() - self._t0
        spf_live = (elapsed / self.done) if self.done > 3 else None
        spf_cur = spf_live or (self.stats.get(self.phase) or {}).get("spf")
        if spf_cur is None:
            return None
        eta = (self.total - self.done) * spf_cur
        after = False
        for ph in self.PHASES:
            if ph == self.phase:
                after = True
                continue
            if not after:
                continue
            spf = (self.stats.get(ph) or {}).get("spf")
            tot = self.totals.get(ph)
            if spf is not None and tot:
                eta += spf * tot
        return eta

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.5:
            return
        self._last_emit = now
        eta = self._eta_s()
        emit("progress", phase=self.phase, done=self.done, total=self.total,
             percent=round(100.0 * self.done / max(1, self.total), 1),
             eta_s=round(eta, 1) if eta is not None else None)


# ---------------------------------------------------------------------------
#  Uscita verso il telefono: i frame escono dal worker su una socket locale,
#  il gateway li impacchetta in WebRTC. Qui NON si parla di rete: il worker
#  gira in un venv suo (MuseTalk) e tenerlo libero da aiortc evita di
#  duplicare quella dipendenza in due ambienti.
# ---------------------------------------------------------------------------
#: Intestazione dei fotogrammi: deve combaciare con `_MAGIC`/`_INTESTAZIONE`
#: in call-bridge/callbridge/avatar_webrtc.py. Il magic fa fallire subito e con
#: un messaggio chiaro l'accoppiata di un worker e un gateway disallineati.
_MAGIC = b"AVI4"
_INTESTAZIONE = struct.Struct("<4sII")


class _RemoteSink:
    """Frame verso il gateway, su socket TCP locale. Nessun accodamento: se il
    consumatore è lento si scarta, perché un fotogramma vecchio in una
    conversazione dal vivo non serve a nessuno."""

    def __init__(self) -> None:
        self._sock = None
        self._lock = threading.Lock()
        self.attivo = False
        self._porta = int(os.environ.get("AVATAR_STREAM_PORT", "0") or 0)
        self._shape = None

    def collega(self, porta: int = 0) -> None:
        """Apre la socket verso il gateway (chiamata quando l'app chiede video).

        La porta arriva nel comando: il gateway la sceglie a runtime (bind su 0)
        e il worker non puo' saperla dall'ambiente, essendo nato prima."""
        if porta:
            self._porta = int(porta)
        if not self._porta:
            log("stream video: nessuna porta indicata")
            return
        import socket
        try:
            s = socket.create_connection(("127.0.0.1", self._porta), timeout=3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._sock = s
                self.attivo = True
            log(f"stream video verso il gateway attivo (porta {self._porta})")
        except OSError as e:
            log(f"stream video non collegato: {e}")

    def stacca(self) -> None:
        with self._lock:
            self.attivo = False
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        log("stream video verso il gateway chiuso")

    def push(self, frame_bgr) -> None:
        with self._lock:
            s = self._sock
            if s is None:
                return
        try:
            h, w = frame_bgr.shape[:2]
            # I420 esige lati pari (i piani U/V stanno a meta' risoluzione):
            # si taglia un pixel invece di rifiutare il fotogramma.
            w, h = w - (w % 2), h - (h % 2)
            if (h, w) != frame_bgr.shape[:2]:
                frame_bgr = frame_bgr[:h, :w]
            if self._shape != (h, w):
                self._shape = (h, w)
                log(f"stream video: {w}x{h} I420")
            # Si manda I420, non BGR. Due motivi, entrambi misurati: pesa la
            # meta' sulla socket (1,5 byte/pixel invece di 3) ed e' gia' il
            # formato dell'encoder H.264, quindi al gateway sparisce una
            # conversione che costava piu' della codifica stessa. La
            # compressione vera resta a valle in WebRTC: comprimere due volte
            # sarebbe sprecato.
            yuv = cv2.cvtColor(np.ascontiguousarray(frame_bgr),
                               cv2.COLOR_BGR2YUV_I420)
            s.sendall(_INTESTAZIONE.pack(_MAGIC, w, h))
            s.sendall(yuv.tobytes())
        except OSError:
            self.stacca()


_remote_sink = _RemoteSink()


# ---------------------------------------------------------------------------
#  Sink video: pyvirtualcam (OBS) -> fallback finestra di anteprima
# ---------------------------------------------------------------------------
class VideoSink:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self._cam = None
        # AVATAR_PREVIEW=1 (dal client, config avatar_preview_window): finestra
        # di anteprima locale SEMPRE, anche con la webcam virtuale attiva.
        # La GUI cv2 vive nel thread anteprima (vedi _preview_loop in testa al
        # modulo, avviato PRIMA degli import pesanti): qui si spingono i frame.
        self._preview = os.environ.get("AVATAR_PREVIEW") == "1"
        try:
            import pyvirtualcam
            self._cam = pyvirtualcam.Camera(width=width, height=height,
                                            fps=FPS, print_fps=False)
            log(f"webcam virtuale attiva: {self._cam.device}")
        except Exception as e:  # noqa: BLE001 - driver OBS assente
            log(f"webcam virtuale non disponibile ({e}): uso la finestra di anteprima")
            self._preview = True
        if self._preview:
            _preview_start()

    def send(self, frame_bgr: np.ndarray) -> None:
        # Con il telefono collegato il video va SOLO a lui: webcam virtuale e
        # finestra restano spente. È una richiesta esplicita — se l'avatar sta
        # parlando in una videochiamata sul telefono, sul PC non deve comparire.
        if _remote_sink.attivo:
            _remote_sink.push(frame_bgr)
            return
        if self._cam is not None:
            self._cam.send(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        if self._preview:
            _preview_push(frame_bgr)

    def close(self) -> None:
        if self._cam is not None:
            self._cam.close()
        # La finestra di anteprima resta al thread dedicato (si chiude col
        # processo): distruggerla da qui violerebbe il single-thread cv2.


# ---------------------------------------------------------------------------
#  Materiale per-clip (preparazione MuseTalk, con cache su disco)
# ---------------------------------------------------------------------------
class ClipMaterial:
    """Frame del loop + coordinate + latenti VAE + maschere di blending."""

    def __init__(self, frames, coords, latents, masks, mask_boxes) -> None:
        self.frames = frames
        self.coords = coords
        self.latents = latents
        self.masks = masks
        self.mask_boxes = mask_boxes

    def __len__(self) -> int:
        return len(self.frames)


class Engine:
    def __init__(self) -> None:
        _t = time.perf_counter

        def _lap(msg, t0):
            log(f"[timing] {msg}: {time.perf_counter() - t0:.2f}s")
            return time.perf_counter()

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        log(f"carico i modelli MuseTalk su {self.device}...")
        t = _t()
        # Primo tocco della GPU: inizializza il contesto CUDA (spesso vari secondi).
        if self.device.type == "cuda":
            torch.zeros(1, device=self.device)
            torch.cuda.synchronize()
            t = _lap("init contesto CUDA", t)
        self.vae, self.unet, self.pe = load_all_model(
            unet_model_path="./models/musetalkV15/unet.pth",
            vae_type="sd-vae",
            unet_config="./models/musetalkV15/musetalk.json",
            device=self.device,
            # UNet direttamente in VRAM in fp16, dal file mappato: niente copia
            # fp32 da 3,4 GB in RAM (vedi musetalk/models/unet.py).
            dtype=torch.float16 if self.device.type == "cuda" else None,
        )
        t = _lap("load_all_model (unet+vae+pe da disco)", t)
        self.pe = self.pe.half().to(self.device)
        self.vae.vae = self.vae.vae.half().to(self.device)
        self.unet.model = self.unet.model.half().to(self.device)
        if self.device.type == "cuda":
            torch.cuda.synchronize()
        t = _lap("half() + to(device) (unet+vae+pe)", t)
        self.timesteps = torch.tensor([0], device=self.device)
        self.weight_dtype = self.unet.model.dtype

        from transformers import WhisperModel
        self.audio_processor = AudioProcessor(feature_extractor_path="./models/whisper")
        t = _lap("AudioProcessor (whisper feature extractor)", t)
        self.whisper = (WhisperModel.from_pretrained("./models/whisper")
                        .to(device=self.device, dtype=self.weight_dtype).eval())
        self.whisper.requires_grad_(False)
        if self.device.type == "cuda":
            torch.cuda.synchronize()
        t = _lap("WhisperModel.from_pretrained + to(device)", t)
        self.fp = FaceParsing(left_cheek_width=90, right_cheek_width=90)
        t = _lap("FaceParsing init", t)
        # Fine caricamento: i residui CPU (checkpoint, state dict) tornano al
        # sistema subito, non al prossimo giro dell'allocatore.
        import gc
        gc.collect()
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        log("modelli pronti")

    # --- preparazione di un clip (una tantum, cache su disco) -------------
    def _modelli_prep(self, su_gpu: bool) -> None:
        """Sposta fra GPU e CPU i modelli che servono SOLO a preparare un clip.

        DWPose (389 MB su disco) e FaceParsing (96 MB) entrano in VRAM comunque:
        il primo perche' `musetalk.utils.preprocessing` lo carica a livello di
        MODULO (riga `model = init_model(...)`, quindi al solo import), il
        secondo perche' l'Engine lo istanzia nel costruttore. Ma servono
        soltanto in `prepare_clip`: durante il parlato girano solo pe/unet/vae
        e whisper. Con il clip in cache — il caso NORMALE — restavano in
        memoria senza fare nulla. Qui si parcheggiano su CPU e si riportano su
        GPU solo per la preparazione.

        Non si scaricano e basta perche' l'import deve restare dov'e' (in cima
        al modulo: importarlo tardi deadlocka il loader DLL di Windows, vedi
        la nota sugli import).
        """
        dev = self.device if su_gpu else torch.device("cpu")
        try:
            from musetalk.utils import preprocessing as _prep
            if getattr(_prep, "model", None) is not None:
                _prep.model.to(dev)
        except Exception as e:  # noqa: BLE001
            log(f"[vram] dwpose non spostabile: {e}")
        try:
            if getattr(self, "fp", None) is not None and getattr(self.fp, "net", None) is not None:
                self.fp.net.to(dev)
        except Exception as e:  # noqa: BLE001
            log(f"[vram] face-parsing non spostabile: {e}")
        if not su_gpu and self.device.type == "cuda":
            torch.cuda.empty_cache()

    def prepare_clip(self, clip_path: str, cache_dir: str) -> ClipMaterial:
        latents_p = os.path.join(cache_dir, "latents.pt")
        coords_p = os.path.join(cache_dir, "coords.pkl")
        masks_p = os.path.join(cache_dir, "mask_boxes.pkl")
        frames_d = os.path.join(cache_dir, "frames")
        masks_d = os.path.join(cache_dir, "masks")

        if os.path.isfile(latents_p):
            log(f"cache trovata: {cache_dir}")
            # Clip gia' pronto: i modelli di preparazione non servono.
            self._modelli_prep(su_gpu=False)
            frames = self._read_dir(frames_d)
            masks = self._read_dir(masks_d)
            with open(coords_p, "rb") as f:
                coords = pickle.load(f)
            with open(masks_p, "rb") as f:
                mask_boxes = pickle.load(f)
            latents = torch.load(latents_p)
            return ClipMaterial(frames, coords, latents, masks, mask_boxes)

        log(f"preparazione clip {clip_path} (prima volta: alcuni minuti)...")
        # Serve preparare davvero: i modelli tornano su GPU.
        self._modelli_prep(su_gpu=True)
        shutil.rmtree(cache_dir, ignore_errors=True)
        os.makedirs(frames_d, exist_ok=True)
        os.makedirs(masks_d, exist_ok=True)
        prog = PrepProgress()

        tmp_frames = os.path.join(cache_dir, "src_frames")
        os.makedirs(tmp_frames, exist_ok=True)
        cap = cv2.VideoCapture(clip_path)
        # Totale stimato dal contenitore (puo' essere impreciso: corretto a
        # fine estrazione col conteggio reale).
        total_est = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        prog.set_totals(total_est)
        prog.begin("extract", total_est)
        n = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            # Ridimensionamento QUI, una volta sola: da qui in poi landmark,
            # blending e trasporto lavorano tutti sulla risoluzione ridotta.
            # Ridurre a valle costerebbe un resize per ogni fotogramma, 25 volte
            # al secondo, su frame che nel frattempo sono stati fusi a piena
            # risoluzione. La bocca la genera comunque un crop 256x256, quindi
            # scendere a 480p non toglie dettaglio dove conta.
            frame = _limita_altezza(frame, OUTPUT_HEIGHT)
            cv2.imwrite(f"{tmp_frames}/{n:08d}.png", frame)
            n += 1
            prog.step(n)
        cap.release()
        if n == 0:
            raise RuntimeError(f"clip illeggibile: {clip_path}")
        prog.total = n
        prog.set_totals(n)
        prog.end()

        # Landmark a blocchi: get_landmark_and_bbox elabora frame per frame
        # (nessuna dipendenza cross-frame), quindi il chunking e' equivalente
        # e permette progress reale + meno RAM (niente clip intero in memoria).
        img_list = sorted(glob.glob(os.path.join(tmp_frames, "*.png")))
        prog.begin("landmark", len(img_list))
        coord_list, frame_list = [], []
        chunk = 25
        for off in range(0, len(img_list), chunk):
            cl, fl = get_landmark_and_bbox(img_list[off:off + chunk], 0)
            coord_list += cl
            frame_list += fl
            prog.step(min(off + chunk, len(img_list)))
        prog.end()

        prog.begin("latents", len(coord_list))
        latents_list = []
        placeholder = (0.0, 0.0, 0.0, 0.0)
        for i, (bbox, frame) in enumerate(zip(coord_list, frame_list)):
            prog.step(i + 1)
            if bbox == placeholder:
                continue
            x1, y1, x2, y2 = bbox
            y2 = min(y2 + EXTRA_MARGIN, frame.shape[0])
            coord_list[i] = [x1, y1, x2, y2]
            crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256),
                              interpolation=cv2.INTER_LANCZOS4)
            latents_list.append(self.vae.get_latents_for_unet(crop))
        prog.end()

        # Ciclo avanti+indietro (loop senza scatto al riavvolgimento).
        frames = frame_list + frame_list[::-1]
        coords = coord_list + coord_list[::-1]
        latents = latents_list + latents_list[::-1]

        prog.begin("masks", len(frames))
        masks, mask_boxes = [], []
        for i, frame in enumerate(frames):
            cv2.imwrite(f"{frames_d}/{i:08d}.png", frame)
            mask, crop_box = get_image_prepare_material(
                frame, coords[i], fp=self.fp, mode=PARSING_MODE)
            cv2.imwrite(f"{masks_d}/{i:08d}.png", mask)
            masks.append(mask)
            mask_boxes.append(crop_box)
            prog.step(i + 1)
        prog.end()

        with open(coords_p, "wb") as f:
            pickle.dump(coords, f)
        with open(masks_p, "wb") as f:
            pickle.dump(mask_boxes, f)
        torch.save(latents, latents_p)
        shutil.rmtree(tmp_frames, ignore_errors=True)
        self._modelli_prep(su_gpu=False)   # finita: via dalla VRAM
        log(f"preparazione completata: {len(frames)} frame nel ciclo")
        return ClipMaterial(frames, coords, latents, masks, mask_boxes)

    @staticmethod
    def _read_dir(d: str) -> list:
        files = sorted(glob.glob(os.path.join(d, "*.png")),
                       key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
        return [cv2.imread(f) for f in files]

    # --- sintesi del parlato (wav -> frame con labiale, in streaming) ------
    @torch.inference_mode()
    def lipsync_stream(self, mat: ClipMaterial, wav_path: str,
                       start_idx: int):
        """Generatore: produce i frame con labiale UNO ALLA VOLTA, a blocchi
        di BATCH_SIZE sulla GPU. Il consumatore puo' iniziare a riprodurre
        appena arriva il primo blocco: la latenza e' il TEMPO DI CALCOLO di
        quel blocco (~86 ms a BATCH_SIZE=8 su RTX 5090), non BATCH_SIZE/FPS."""
        _first = not getattr(Engine, "_infer_warmed", False)
        _ta = time.perf_counter()
        feats, librosa_len = self.audio_processor.get_audio_feature(
            wav_path, weight_dtype=self.weight_dtype)
        chunks = self.audio_processor.get_whisper_chunk(
            feats, self.device, self.weight_dtype, self.whisper, librosa_len,
            fps=FPS, audio_padding_length_left=2, audio_padding_length_right=2)
        # Sempre, non solo la prima volta: questo e' il preambolo fisso che
        # l'utente sente come attesa prima che l'avatar apra bocca, e senza
        # misurarlo a ogni battuta non si sa se ottimizzare qui o altrove.
        _t_feat = time.perf_counter() - _ta
        log(f"[timing] feature audio+whisper {_t_feat:.2f}s"
            + (" (prima sintesi)" if _first else ""))

        # I latenti partono dal punto corrente del loop (continuita' visiva).
        cyc = len(mat)
        lat_cycle = [mat.latents[(start_idx + i) % cyc] for i in range(len(chunks))]
        produced = 0
        # Tempi di TUTTI i batch, non solo del primo. Serve a distinguere due
        # spiegazioni che dall'esterno si somigliano: se il primo batch costa
        # 1,5s e i successivi 70ms e' un avviamento (kernel, allocazioni,
        # cache fredda) e si attacca quello; se costano tutti 1,5s e'
        # throughput, e allora o la GPU e' contesa o il modello non e' dove
        # crediamo. Senza questa riga si vedeva solo il primo e si tirava a
        # indovinare.
        _tempi_batch: list[float] = []
        # Profilo dei PRIMI batch, se richiesto. Il primo costa venti volte i
        # successivi e nessuna ipotesi esterna ha retto (clock, empty_cache,
        # thread, dati): serve vedere DENTRO, kernel per kernel. Si attiva con
        # `avatar_profile` in configurazione, una volta sola per processo, e
        # scrive un riepilogo nel log piu' una traccia per Chrome/Perfetto.
        _prof = None
        # `_infer_warmed` e' False solo durante il riscaldamento: quello si
        # salta, perche' il suo generatore non viene esaurito e paga comunque
        # la compilazione dei kernel — due motivi per cui non dice nulla.
        if (os.environ.get("AVATAR_PROFILE") == "1"
                and getattr(Engine, "_infer_warmed", False)
                and not getattr(Engine, "_profilato", False)):
            Engine._profilato = True
            from torch.profiler import ProfilerActivity, profile
            _prof = profile(
                activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
                record_shapes=True, with_stack=False, profile_memory=False)
            _prof.__enter__()
            log("[profilo] attivo sui primi batch")
        gen = datagen(chunks, lat_cycle, BATCH_SIZE)
        for whisper_batch, latent_batch in gen:
            _tb = time.perf_counter()
            # Il PRIMO batch di ogni battuta costa ~10 volte i successivi e
            # non si sapeva perche'. Qui lo si spacchetta nelle sue tre fasi,
            # sincronizzando fra l'una e l'altra: senza sync i tempi sarebbero
            # fittizi, perche' CUDA accoda e torna subito, e tutto il lavoro
            # finirebbe attribuito all'ultima riga che aspetta davvero.
            _dett = produced == 0 and self.device.type == "cuda"
            audio_feat = self.pe(whisper_batch.to(self.device))
            if _dett:
                torch.cuda.synchronize(); _t_pe = time.perf_counter()
            latent_batch = latent_batch.to(device=self.device,
                                           dtype=self.unet.model.dtype)
            pred = self.unet.model(latent_batch, self.timesteps,
                                   encoder_hidden_states=audio_feat).sample
            if _dett:
                torch.cuda.synchronize(); _t_unet = time.perf_counter()
            pred = pred.to(device=self.device, dtype=self.vae.vae.dtype)
            recon = self.vae.decode_latents(pred)
            if _dett:
                torch.cuda.synchronize()
                log(f"[timing] dettaglio primo batch: pe {(_t_pe-_tb)*1000:.0f} ms, "
                    f"unet {(_t_unet-_t_pe)*1000:.0f} ms, "
                    f"vae {(time.perf_counter()-_t_unet)*1000:.0f} ms")
            if self.device.type == "cuda":
                # Gratuito: `decode_latents` chiude con `.cpu()`, che e' gia'
                # una copia bloccante e quindi ha gia' sincronizzato. Questa
                # riga rende la misura esplicita invece che dipendente da un
                # dettaglio di un altro modulo.
                torch.cuda.synchronize()
            _tempi_batch.append(time.perf_counter() - _tb)
            if _prof is not None and len(_tempi_batch) >= 3:
                # Si chiude QUI e non dopo il ciclo: il generatore puo' non
                # venire esaurito (il consumatore esce quando ha i fotogrammi
                # che gli servono) e allora __exit__ non arriverebbe mai.
                # Succedeva, e il riepilogo non compariva affatto.
                _prof.__exit__(None, None, None)
                try:
                    log("[profilo] primi 3 batch: "
                        + ", ".join(f"{x*1000:.0f} ms" for x in _tempi_batch))
                    nl = chr(10)
                    log("[profilo] per tempo CUDA:" + nl
                        + _prof.key_averages().table(
                            sort_by="self_cuda_time_total", row_limit=14))
                    log("[profilo] per tempo CPU:" + nl
                        + _prof.key_averages().table(
                            sort_by="self_cpu_time_total", row_limit=14))
                    fuori = os.path.join(os.environ.get("TEMP", "."),
                                         "avatar_profilo.json")
                    _prof.export_chrome_trace(fuori)
                    log(f"[profilo] traccia in {fuori}")
                except Exception as e:  # noqa: BLE001
                    log(f"[profilo] riepilogo fallito: {type(e).__name__}: {e}")
                _prof = None
            if produced == 0:
                log(f"[timing] primo batch UNet+VAE {_tempi_batch[0]:.2f}s"
                    + (" (compilazione kernel cuDNN)" if _first else ""))
                Engine._infer_warmed = True
                _first = False
            for res in recon:
                idx = (start_idx + produced) % cyc
                bbox = mat.coords[idx]
                ori = copy.deepcopy(mat.frames[idx])
                x1, y1, x2, y2 = bbox
                try:
                    res = cv2.resize(res.astype(np.uint8), (x2 - x1, y2 - y1))
                except Exception:  # noqa: BLE001 - bbox degenere
                    produced += 1
                    yield ori
                    continue
                produced += 1
                yield get_image_blending(ori, res, bbox, mat.masks[idx],
                                         mat.mask_boxes[idx])
        if len(_tempi_batch) > 1:
            resto = _tempi_batch[1:]
            log(f"[timing] batch: {len(_tempi_batch)} totali, primo "
                f"{_tempi_batch[0]*1000:.0f} ms, successivi "
                f"{sum(resto)/len(resto)*1000:.0f} ms in media "
                f"(max {max(resto)*1000:.0f})")

    def warmup_infer(self, mat: ClipMaterial) -> None:
        """Esegue UNA inferenza fittizia (audio silenzioso) per compilare i
        kernel cuDNN di UNet/VAE e scaldare Whisper: cosi' la PRIMA battuta reale
        non paga gli ~8s di prima esecuzione (misurati: ~3.7s feature+whisper,
        ~4.2s compilazione kernel). One-time per processo. I frame prodotti si
        scartano (non passano dal sink -> niente output sulla webcam virtuale)."""
        import tempfile
        import wave
        sr = 16000
        silent = np.zeros(int(sr * 1.0), dtype="<i2")  # 1s di silenzio
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(silent.tobytes())
            t0 = time.perf_counter()
            for _ in self.lipsync_stream(mat, path, 0):
                break  # il primo frame implica gia' il primo batch = compilazione
            log(f"[timing] warmup inferenza completato in {time.perf_counter()-t0:.2f}s")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
#  Loop principale: idle + speak, comandi da stdin
# ---------------------------------------------------------------------------
def play_wav_async(wav_path: str) -> threading.Thread:
    import soundfile as sf
    import sounddevice as sd
    data, sr = sf.read(wav_path, dtype="float32")
    dev = os.environ.get("AVATAR_AUDIO_DEVICE")
    dev = int(dev) if dev not in (None, "",) else None

    def _run() -> None:
        try:
            sd.play(data, sr, device=dev)
            sd.wait()
        except Exception as e:  # noqa: BLE001
            log(f"riproduzione audio fallita: {e}")
    t = threading.Thread(target=_run, daemon=True)
    return t


class Produttore:
    """UN SOLO thread per tutta l'inferenza dell'avatar, per sempre.

    Non e' una raffinatezza di stile: PyTorch tiene le cache dei piani di
    esecuzione cuDNN in variabili THREAD_LOCAL (i piani non sono thread-safe
    fra engine diversi), quindi con `cudnn.benchmark = True` un thread appena
    nato riparte da cache VUOTA e riautotuna da zero tutte le convoluzioni di
    UNet+VAE. Misurato col profiler sui primi tre batch di una battuta:

        aten::cudnn_convolution   954 ms di CPU propria   (402 chiamate)
        cudaDeviceSynchronize     250 ms                  (546 chiamate!)
        cudaFree / cudaMalloc      72 ms
        --------------------------------------------------------------
        CPU totale 1,55 s   contro   CUDA totale 0,28 s

    Cioe': del "primo batch da 1450 ms" (contro i 70 ms dei successivi) la GPU
    ne usava 70; il resto era la CPU che provava e cronometrava algoritmi di
    convoluzione, con 546 sincronizzazioni. Prima ogni battuta nasceva in un
    thread suo e quindi ripagava l'autotune INTERO, ogni volta. Ecco perche' il
    riscaldamento all'avvio non serviva a niente (girava in un altro thread) e
    perche' lo stesso codice fuori dal worker costava 65 ms: nel test l'inferenza
    stava sempre nello stesso thread.

    Riusando questo thread l'autotune si paga una volta sola, al riscaldamento.
    """

    def __init__(self) -> None:
        self._lavori: "queue.Queue[tuple]" = queue.Queue()
        threading.Thread(target=self._loop, name="lipsync-gen",
                         daemon=True).start()

    def _loop(self) -> None:
        while True:
            fn, fine = self._lavori.get()
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log(f"[produttore] lavoro fallito: {type(e).__name__}: {e}")
            finally:
                fine.set()

    def esegui(self, fn) -> threading.Event:
        """Accoda un lavoro; l'Event restituito si chiude quando ha finito."""
        fine = threading.Event()
        self._lavori.put((fn, fine))
        return fine


def main() -> int:
    engine = Engine()
    emit("ready")
    _preview_status("modelli pronti - attendo la persona...")

    mat: ClipMaterial | None = None
    sink: VideoSink | None = None
    idx = 0
    speak_q: "queue.Queue[str]" = queue.Queue()
    stop = threading.Event()
    # Interruzione della battuta in corso: gestita DIRETTAMENTE dal reader
    # (non passa dalla coda comandi, che durante lo speak non viene letta).
    interrupt = threading.Event()
    produttore = Produttore()
    # Battuta precedente da abbandonare: il produttore e' unico e sequenziale,
    # quindi se una generazione interrotta restasse appesa su frame_q.put()
    # bloccherebbe TUTTE le battute successive. Si segnala l'abbandono prima di
    # accodare la nuova.
    abbandona: "threading.Event | None" = None

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                emit("error", message="JSON non valido")
                continue
            if cmd.get("cmd") == "interrupt":
                interrupt.set()
                if cmd.get("svuota"):
                    # Senza questo, `interrupt` tronca SOLO la battuta in corso:
                    # il ciclo principale sfila subito la successiva da speak_q e
                    # fa `interrupt.clear()`, cosi' l'avatar riprende ad animare
                    # proprio la risposta che si voleva abbandonare. Chi
                    # interrompe una conversazione vuole fermare tutto il
                    # discorso, non una frase sola.
                    buttate = 0
                    while True:
                        try:
                            speak_q.get_nowait()
                            buttate += 1
                        except queue.Empty:
                            break
                    if buttate:
                        log(f"[interrupt] scartate {buttate} battute in coda")
                continue
            if cmd.get("cmd") == "preview":
                # Gestito DIRETTAMENTE dal reader (come interrupt): durante lo
                # speak la coda comandi non viene letta, ma la finestra deve
                # chiudersi/riaprirsi subito.
                if cmd.get("show", True):
                    _preview_show.set()
                else:
                    _preview_show.clear()
                continue
            speak_q.put(json.dumps(cmd))
            if cmd.get("cmd") == "quit":
                break
        stop.set()

    threading.Thread(target=reader, daemon=True).start()

    frame_period = 1.0 / FPS
    next_t = time.monotonic()
    while not stop.is_set():
        # comandi pendenti
        try:
            raw = speak_q.get_nowait()
            cmd = json.loads(raw)
            what = cmd.get("cmd")
            if what == "quit":
                break
            if what == "stream":
                # L'app ha chiesto (o chiuso) il video: da qui in poi i frame
                # vanno al telefono e non alla webcam virtuale.
                if cmd.get("on"):
                    _remote_sink.collega(int(cmd.get("port") or 0))
                else:
                    _remote_sink.stacca()
                emit("stream", on=_remote_sink.attivo)
                continue
            if what == "persona":
                pdir = cmd["dir"]
                clip = cmd.get("clip")
                if not clip:
                    man = json.load(open(os.path.join(pdir, "manifest.json"),
                                         encoding="utf-8"))
                    clips = [os.path.join(pdir, c) for c in man.get("clips", [])]
                    if not clips:
                        emit("error", message=f"nessun clip in {pdir}")
                        continue
                    clip = random.choice(clips)
                # L'altezza fa parte dell'identita' della cache: il materiale
                # preparato a piena risoluzione non e' riusabile per il 480p
                # (frame, maschere e coordinate sono tutti in scala).
                cache = os.path.join(
                    MUSETALK_DIR, "cache",
                    f"{os.path.basename(pdir)}__{os.path.splitext(os.path.basename(clip))[0]}"
                    + (f"__h{OUTPUT_HEIGHT}" if OUTPUT_HEIGHT else ""))
                os.makedirs(cache, exist_ok=True)
                _preview_status("preparazione persona (la prima volta richiede minuti)...")
                try:
                    mat = engine.prepare_clip(clip, cache)
                except Exception as e:  # noqa: BLE001
                    emit("error", message=f"preparazione fallita: {e}")
                    mat = None
                    continue
                h, w = mat.frames[0].shape[:2]
                if sink is not None:
                    sink.close()
                sink = VideoSink(w, h)
                idx = 0
                # Warmup inferenza (una volta per processo): compila i kernel
                # cuDNN e scalda Whisper ORA (al preload, in background) cosi' la
                # prima battuta reale non paga gli ~8s di prima esecuzione.
                if not getattr(Engine, "_infer_warmed", False):
                    _preview_status("scaldo i kernel GPU (una tantum)...")
                    # DENTRO il produttore, non qui: e' quel thread che
                    # deve trovarsi le cache cuDNN calde (vedi Produttore).
                    # Eseguito nel thread sbagliato, il riscaldamento non
                    # serviva a nulla e ogni battuta ripagava 1400 ms.
                    try:
                        produttore.esegui(
                            lambda m=mat: engine.warmup_infer(m)).wait(180)
                    except Exception as e:  # noqa: BLE001 - warmup non fatale
                        log(f"warmup inferenza fallito (non fatale): {e}")
                emit("persona_ready", clip=os.path.basename(clip),
                     frames=len(mat))
            elif what == "speak":
                if mat is None or sink is None:
                    emit("error", message="nessuna persona caricata")
                    continue
                wav = cmd["wav"]
                emit("speaking", wav=os.path.basename(wav))
                interrupt.clear()

                # --- produttore: genera i frame a blocchi in un thread ----
                frame_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=FPS * 8)
                gen_done = threading.Event()
                gen_err: list[Exception | None] = [None]

                if abbandona is not None:
                    abbandona.set()      # sveglia l'eventuale battuta appesa
                abbandona = mia = threading.Event()

                def _produce(mat=mat, wav=wav, start=idx, mia=mia) -> None:
                    try:
                        for f in engine.lipsync_stream(mat, wav, start):
                            # `put` con scadenza, non bloccante a vita: se il
                            # consumatore ha smesso di leggere (interruzione) il
                            # produttore deve uscire, altrimenti tiene occupato
                            # l'unico thread di inferenza.
                            while True:
                                if (interrupt.is_set() or stop.is_set()
                                        or mia.is_set()):
                                    return
                                try:
                                    frame_q.put(f, timeout=0.2)
                                    break
                                except queue.Full:
                                    continue
                    except Exception as e:  # noqa: BLE001
                        gen_err[0] = e
                    finally:
                        gen_done.set()

                produttore.esegui(_produce)

                # --- prebuffer: il primo blocco basta per partire ---------
                _t_batt = time.perf_counter()
                prebuf: list[np.ndarray] = []
                while (len(prebuf) < BATCH_SIZE
                       and not interrupt.is_set()
                       and not (gen_done.is_set() and frame_q.empty())):
                    try:
                        prebuf.append(frame_q.get(timeout=2))
                    except queue.Empty:
                        continue  # setup audio/whisper ancora in corso
                if interrupt.is_set():
                    # Interrotto prima ancora di partire: niente errore.
                    while not frame_q.empty():
                        try:
                            frame_q.get_nowait()
                        except queue.Empty:
                            break
                    emit("interrupted", frames=0)
                    emit("idle")
                    continue
                if not prebuf:
                    emit("error", message=f"lipsync fallito: {gen_err[0]}")
                    continue

                # --- consumatore: 25 fps ancorati all'avvio dell'audio ----
                # `mute`: labiale senza suono locale. Serve quando la voce esce
                # da un'altra parte (il telefono, via gateway): sentirla anche
                # dalle casse del PC significherebbe sentirla due volte e
                # sfasata. I fotogrammi restano ancorati al tempo, non
                # all'audio, quindi la temporizzazione non cambia.
                audio_t = None if cmd.get("mute") else play_wav_async(wav)
                if audio_t is not None:
                    audio_t.start()
                # "speaking" viene emesso APPENA ARRIVA il comando, prima di
                # generare i fotogrammi: chi lo aspettasse per sincronizzarsi
                # partirebbe con l'anticipo della generazione. "playing" invece
                # segna l'istante esatto in cui il video comincia a scorrere, ed
                # e' quello a cui va agganciata la voce che esce altrove.
                # Questo e' l'istante che il gateway attende prima di mandare
                # la voce: il totale qui sotto e' il ritardo percepito fra "la
                # LLM ha finito" e "l'avatar apre bocca".
                _preparazione = time.perf_counter() - _t_batt
                log(f"[timing] battuta pronta in {_preparazione:.2f}s "
                    f"(prebuffer {BATCH_SIZE} frame)")
                # `preparazione_s` viaggia fino al gateway perche' e' il SOLO
                # numero che misura il preambolo vero del labiale. Il gateway
                # finora cronometrava la propria POST, che include l'attesa in
                # `speak_q` dietro le battute gia' accodate: misurato, 0,10-0,17 s
                # di preparazione contro POST da 2 s quando la coda non e' vuota.
                emit("playing", wav=os.path.basename(wav),
                     preparazione_s=round(_preparazione, 3))
                t0 = time.monotonic()
                k = 0          # istante video (frame teorico dall'inizio audio)
                fetched = 0    # frame realmente estratti dal produttore
                last = prebuf[0]
                ripetuti = 0
                interrupted = False
                while True:
                    if stop.is_set() or interrupt.is_set():
                        interrupted = interrupt.is_set()
                        break
                    # Frame per l'istante k: scarta gli eventuali ritardatari
                    # (riallineamento dopo un buco di generazione).
                    got = None
                    while fetched <= k:
                        if prebuf:
                            cand = prebuf.pop(0)
                        else:
                            left = (t0 + (k + 1) * frame_period) - time.monotonic()
                            try:
                                cand = frame_q.get(timeout=max(0.0, left))
                            except queue.Empty:
                                break
                        fetched += 1
                        got = cand
                    if got is not None:
                        last = got
                    elif gen_done.is_set() and frame_q.empty():
                        break  # battuta completata
                    else:
                        # Buco di generazione: si ripete l'ultimo fotogramma.
                        # Contarli e' l'unica misura OGGETTIVA della fluidita':
                        # un labiale "a scatti" per pochi fotogrammi al secondo
                        # e' una scelta, uno che si blocca perche' la GPU non
                        # sta dietro e' un difetto, e a occhio si somigliano.
                        ripetuti += 1
                    sink.send(last)
                    idx = (idx + 1) % len(mat)
                    k += 1
                    delay = (t0 + k * frame_period) - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)

                if k:
                    quota = 100.0 * ripetuti / k
                    log(f"[timing] battuta resa: {k} fotogrammi, {ripetuti} "
                        f"ripetuti ({quota:.0f}%)"
                        + ("  <- la generazione non sta dietro" if quota > 10
                           else ""))
                if interrupted:
                    try:
                        import sounddevice as sd
                        sd.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    while not frame_q.empty():
                        try:
                            frame_q.get_nowait()
                        except queue.Empty:
                            break
                    log(f"battuta interrotta dopo {k} frame")
                    emit("interrupted", frames=k)
                elif audio_t is not None:
                    audio_t.join(timeout=5)
                next_t = time.monotonic()
                # Battuta finita: si restituiscono al driver le attivazioni
                # trattenute dal caching allocator di torch. Durante la
                # generazione il worker oscilla di ~1,9 GB (misurato), e quel
                # picco resta prenotato anche a bocca chiusa: e' cio' che
                # spinge il driver a sfrattare in shared una fetta del modello
                # LLM, facendone crollare la velocita'. Costa qualche ms alla
                # prima allocazione della battuta successiva, che e' dentro il
                # prebuffer e non si sente.
                # AVATAR_EMPTY_CACHE=0 per NON restituire la cache: serve a
                # capire se il costo del primo batch nasce qui.
                if (torch.cuda.is_available()
                        and os.environ.get("AVATAR_EMPTY_CACHE", "1") != "0"):
                    torch.cuda.empty_cache()
                emit("idle")
        except queue.Empty:
            pass

        # idle: cicla i frame del template
        if mat is not None and sink is not None:
            sink.send(mat.frames[idx])
            idx = (idx + 1) % len(mat)
        next_t += frame_period
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()

    if sink is not None:
        sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
