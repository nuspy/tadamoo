#!/usr/bin/env python3
"""Helper TTS XTTS v2 (Coqui) — voice cloning zero-shot, 17 lingue.

Protocollo stdio (con "stream": true l'audio esce a blocchi mentre il GPT
genera, chiuso da [0xFFFFFFFF][0]) identico agli altri helper del bridge:
  stdin:  {"text","voice","lang","speed"}\\n  |  {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]  (count==0 = errore)
  stderr: log

`voice` e' il nome (senza estensione) di un riferimento in
$KOKORO_MODEL_DIR/cloned_voices/. I latents del riferimento sono in cache:
cambiare voce costa solo la prima frase.

Ambiente: venv `tts-venvs/xtts` (coqui-tts + torch cu128, transformers<5 —
con transformers 5.x manca `isin_mps_friendly` e il modello non carica).
`speed` e' supportato nativamente da XTTS.
"""
from __future__ import annotations

import os as _os

# Prima di qualunque import di torch: la memoria che torch libera torna subito
# al sistema invece di restare committata (vedi call-bridge/callbridge/memoria.py).
_os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import json
import os
import struct
import sys

# Le librerie stampano su stdout, che qui trasporta audio binario: si dirotta
# tutto su stderr PRIMA di qualunque import pesante.
_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr
# Il pacchetto callbridge (funzioni di memoria, solo stdlib) sta due cartelle sopra.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def log(*a) -> None:
    print("[xtts_helper]", *a, file=sys.stderr, flush=True)


CLONE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
# XTTS non conosce queste lingue: si ripiega sull'inglese avvisando, invece di
# far esplodere la sintesi a meta' sessione.
LINGUE = {"en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs",
          "ar", "zh-cn", "ja", "hu", "ko", "hi"}

_modello = None
_latents: dict = {}
_speakers: dict | None = None   # speaker di serie, caricati una volta
_dir_modello = ""


def _usa_soundfile() -> None:
    """torchaudio.load -> soundfile.

    Da torch 2.9 torchaudio delega a torchcodec, che su Windows pretende le DLL
    di FFmpeg 4-7 sul PATH e qui non si carica ("Could not load libtorchcodec"):
    XTTS lo usa per leggere il wav di riferimento, quindi ogni sintesi falliva.
    soundfile legge i wav senza dipendenze native aggiuntive.
    """
    import soundfile as sf
    import torch
    import torchaudio

    def load(uri, *a, **k):
        dati, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(dati.T.copy()), sr

    torchaudio.load = load


def _riferimento(voce: str) -> str | None:
    base = os.environ.get("KOKORO_MODEL_DIR", "")
    d = os.path.join(base, "cloned_voices")
    for ext in CLONE_EXTS:
        p = os.path.join(d, voce + ext)
        if os.path.isfile(p):
            return p
    return None


def _rilascia() -> None:
    """Restituisce subito al sistema i residui del caricamento (CPU e cache CUDA)."""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _carica_diretto(cfg, dir_modello: str, device: str):
    """Costruisce XTTS direttamente sul device e vi carica il checkpoint mappato
    in memoria (la libreria lo leggerebbe tutto in RAM con map_location=cpu)."""
    import torch
    from TTS.tts.models import xtts as _xtts
    from TTS.tts.models.xtts import Xtts

    try:
        from trainer.io import _WEIGHTS_ONLY as solo_pesi
    except ImportError:
        solo_pesi = True
    originale = _xtts.load_fsspec

    def _mmap(percorso, map_location=None, **kw):
        if os.path.isfile(str(percorso)):
            return torch.load(str(percorso), map_location="cpu", mmap=True,
                              weights_only=solo_pesi)
        return originale(percorso, map_location=map_location, **kw)

    _xtts.load_fsspec = _mmap
    try:
        with torch.device(device):
            m = Xtts.init_from_config(cfg)
            m.load_checkpoint(cfg, checkpoint_dir=dir_modello, eval=True)
    finally:
        _xtts.load_fsspec = originale
    m.to(device)        # eventuali code rimaste su CPU (buffer caricati a parte)
    return m


def _carica():
    global _modello
    if _modello is not None:
        return _modello
    import torch
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    _usa_soundfile()
    device = os.environ.get("TTS_DEVICE", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        log("CUDA non disponibile: si ripiega su CPU")
        device = "cpu"
    root = os.environ.get("TTS_MODELS_DIR", "")
    dir_modello = os.path.join(root, "xtts_v2")
    if not os.path.isdir(dir_modello):
        # Prima esecuzione: scarica i pesi (~1,8 GB) una tantum.
        from huggingface_hub import snapshot_download
        log("scarico coqui/XTTS-v2 ...")
        snapshot_download("coqui/XTTS-v2", local_dir=dir_modello)
    cfg = XttsConfig()
    cfg.load_json(os.path.join(dir_modello, "config.json"))
    m = None
    if device == "cuda" and os.environ.get("XTTS_DIRETTO", "1") != "0":
        # Il modello nasce gia' in VRAM e i pesi vanno dal file (mmap) alla GPU
        # un tensore alla volta: niente modello fp32 da ~2 GB in RAM piu' la
        # copia del checkpoint (stesso scopo di callbridge/memoria.py).
        # XTTS_DIRETTO=0 torna al percorso classico.
        try:
            m = _carica_diretto(cfg, dir_modello, device)
        except Exception as e:  # noqa: BLE001 - si ripiega sul percorso classico
            log(f"caricamento diretto in VRAM non riuscito "
                f"({type(e).__name__}: {e}): uso il percorso classico")
            m = None
            _rilascia()
    if m is None:
        m = Xtts.init_from_config(cfg)
        m.load_checkpoint(cfg, checkpoint_dir=dir_modello, eval=True)
        m.to(device)
    if device == "cpu":
        # Il modello sta in RAM per scelta: un unico buffer contiguo costa
        # ~1/3 di commit in meno dei centinaia di tensori sparsi.
        try:
            from callbridge.memoria import consolida_cpu
            log(f"pesi CPU consolidati: {consolida_cpu(m) / 2**20:.0f} MiB")
        except Exception as e:  # noqa: BLE001 - ottimizzazione, non necessaria
            log(f"consolidamento non applicato: {type(e).__name__}: {e}")
    _rilascia()
    # Mezza precisione: dimezza la VRAM residente e accelera le matmul sui
    # tensor core. Solo su CUDA — su CPU fp16 e' emulato e va PIU' PIANO.
    #
    # Si dimezza il solo GPT, non tutto il modello: il vocoder HiFiGAN che
    # produce la forma d'onda e' la parte dove fp16 puo' dare artefatti udibili
    # (satura sui picchi), e vale poca memoria. Il GPT autoregressivo e'
    # l'ottanta per cento dei pesi ed e' la parte lenta, quindi e' li' che il
    # guadagno sta tutto.
    #
    # XTTS_FP16=0 torna a piena precisione senza toccare il codice, se
    # all'ascolto la voce dovesse peggiorare.
    # Default SPENTO. Provato il 15/09: dimezzare i pesi del GPT non basta,
    # perche' i latenti di condizionamento e l'embedding dello speaker restano
    # in fp32 e la inferenza muore sul disallineamento di tipo — con l'avatar
    # che non diceva piu' NIENTE. La conversione dei latenti c'e' qui sotto,
    # ma la combinazione va provata all'ascolto prima di accenderla di default:
    # su XTTS il vocoder in mezza precisione puo' saturare sui picchi.
    #
    # XTTS_FP16=1 per provarla.
    if device == "cuda" and os.environ.get("XTTS_FP16", "0") == "1":
        try:
            m.gpt = m.gpt.half()
            log("GPT in mezza precisione (sperimentale)")
        except Exception as e:  # noqa: BLE001 - meglio fp32 che un helper muto
            log(f"mezza precisione non applicata: {type(e).__name__}: {e}")
    _modello = m
    global _dir_modello
    _dir_modello = dir_modello
    log(f"modello caricato su {device}")
    return _modello


def _lat(m, rif: str):
    if rif not in _latents:
        _latents[rif] = m.get_conditioning_latents(audio_path=[rif])
    return _latents[rif]


def speakers_integrati(dir_modello: str) -> dict:
    """`nome -> (gpt_cond_latent, speaker_embedding)` degli speaker DI SERIE.

    XTTS v2 ne porta 58 dentro `speakers_xtts.pth`, ognuno multilingua (la
    stessa voce parla tutte e 17 le lingue conservando il timbro). Sono utili
    senza registrare campioni, e restano l'unica scelta sensata quando serve
    cambiare lingua a metà conversazione.
    """
    global _speakers
    if _speakers is not None:
        return _speakers
    import torch
    p = os.path.join(dir_modello, "speakers_xtts.pth")
    if not os.path.isfile(p):
        _speakers = {}
        return _speakers
    try:
        _speakers = torch.load(p, map_location="cpu", weights_only=False) or {}
        log(f"{len(_speakers)} speaker integrati disponibili")
    except Exception as e:  # noqa: BLE001 - senza, restano solo le clonate
        log(f"speaker integrati non caricati: {type(e).__name__}: {e}")
        _speakers = {}
    return _speakers


# Token audio al secondo del GPT di XTTS: sample rate 22050 / stride 1024.
# Il modello ne ammette al massimo 605 (~28 s di parlato).
_TOKEN_AL_SECONDO = 22050 / 1024
_MAX_TOKEN = 605
# Caratteri al secondo del parlato. MISURATO su questo motore e questa voce,
# non stimato: 66 caratteri in 3,70 s e 79 in 4,70 s, cioe' 17,3-17,8. Il 12
# di prima era una stima prudente, e la prudenza qui si paga: il tetto sotto
# e' proporzionale a questo numero, quindi sottostimarlo lo allarga del 46% —
# ed e' proprio nello spazio in piu' che ci sta il farfugliato finale.
_CHAR_AL_SECONDO = 17.5


def _limite_generazione(testo: str) -> dict:
    """`hf_generate_kwargs` con un tetto ai token audio, proporzionato al testo.

    Su input BREVI il GPT di XTTS spesso non emette il token di fine e tira
    avanti a generare: ne esce un farfugliato che sembra la lettura della
    punteggiatura. Misurato su questa macchina: "Ciao." ha prodotto 5,3 s di
    audio contro gli ~1,1 s del parlato reale, e un "." isolato quasi 4 s.
    Il fenomeno è aleatorio — la stessa frase a volte esce pulita — quindi non
    basta accorpare le frasi corte a monte: qui si mette il tetto.

    Il margine è generoso (2x la durata stimata più un secondo): tagliare una
    frase legittima sarebbe peggio del difetto che si sta correggendo.
    """
    secondi = len(testo) / _CHAR_AL_SECONDO
    tetto = int((secondi * 2.0 + 1.0) * _TOKEN_AL_SECONDO)
    # `hf_generate_kwargs` nella firma è **kwargs: i parametri vanno passati
    # espansi, non dentro un dizionario con quel nome.
    return {"max_new_tokens": max(40, min(tetto, _MAX_TOKEN))}


def _lingua(lang: str) -> str:
    lg = (lang or "it").lower().replace("_", "-")
    if lg.startswith("zh"):
        lg = "zh-cn"
    lg = lg.split("-")[0] if lg != "zh-cn" else lg
    if lg not in LINGUE:
        log(f"lingua '{lang}' non supportata da XTTS: uso 'en'")
        return "en"
    return lg


def _scrivi(samples, sr: int) -> None:
    import numpy as np
    a = np.asarray(samples, dtype="<f4").ravel()
    _BINARY_OUT.write(struct.pack("<II", a.size, int(sr)))
    _BINARY_OUT.write(a.tobytes())
    _BINARY_OUT.flush()


def _errore() -> None:
    _BINARY_OUT.write(struct.pack("<II", 0, 0))
    _BINARY_OUT.flush()


# Streaming (richiesta con "stream": true): N blocchi audio mentre il GPT
# genera, poi [0xFFFFFFFF][0]. 20 token audio per blocco ~ 0,9 s di parlato:
# il primo arriva dopo ~20 passi del GPT invece che a frase finita.
_FINE_STREAM = 0xFFFFFFFF
_TOKEN_PER_BLOCCO = 20


def _fine_stream() -> None:
    _BINARY_OUT.write(struct.pack("<II", _FINE_STREAM, 0))
    _BINARY_OUT.flush()


def main() -> int:
    for riga in sys.stdin:
        riga = riga.strip()
        if not riga:
            continue
        try:
            req = json.loads(riga)
        except json.JSONDecodeError:
            log("richiesta non JSON, ignorata")
            _errore()
            continue
        if req.get("cmd") == "quit":
            return 0
        if req.get("cmd") == "speakers":
            # Elenco degli speaker di serie: su stderr, dove sta il testo.
            try:
                _carica()
                log("SPEAKERS " + json.dumps(sorted(speakers_integrati(_dir_modello))))
            except Exception as e:  # noqa: BLE001
                log(f"elenco speaker fallito: {e}")
            _errore()          # il protocollo binario vuole comunque una risposta
            continue
        testo = (req.get("text") or "").strip()
        if not testo:
            _errore()
            continue
        try:
            import torch
            m = _carica()
            voce = (req.get("voice") or "").strip()
            rif = _riferimento(voce)
            if rif is not None:
                gpt_lat, spk_emb = _lat(m, rif)
            else:
                # Nessun campione con quel nome: puo' essere uno dei 58 speaker
                # di serie, che portano gia' i propri latenti.
                sp = speakers_integrati(_dir_modello).get(voce)
                if sp is None:
                    log(f"voce '{voce}' sconosciuta (ne' clonata ne' di serie)")
                    _errore()
                    continue
                gpt_lat = sp["gpt_cond_latent"]
                spk_emb = sp["speaker_embedding"]
            # I latenti arrivano in fp32 dalla cache: se il GPT e' stato
            # dimezzato vanno convertiti, altrimenti l'inferenza muore sul
            # disallineamento di tipo (visto dal vivo: sintesi fallita a ogni
            # frase e avatar muto).
            _dt = next(m.gpt.parameters()).dtype
            if gpt_lat is not None and gpt_lat.dtype != _dt:
                gpt_lat = gpt_lat.to(_dt)
            if spk_emb is not None and spk_emb.dtype != _dt:
                spk_emb = spk_emb.to(_dt)
            if req.get("stream"):
                n = 0
                with torch.inference_mode():
                    for pezzo in m.inference_stream(
                            testo, _lingua(req.get("lang")), gpt_lat, spk_emb,
                            stream_chunk_size=_TOKEN_PER_BLOCCO,
                            speed=float(req.get("speed") or 1.0),
                            **_limite_generazione(testo)):
                        a = pezzo.float().cpu().numpy() if hasattr(pezzo, "cpu") else pezzo
                        n += a.size
                        _scrivi(a, 24000)
                log(f"sintesi (streaming): {n/24000:.2f}s di audio per "
                    f"{len(testo)} char (voce {voce!r}) -> {testo[:60]!r}")
                _fine_stream()
                continue
            with torch.inference_mode():
                out = m.inference(
                    text=testo, language=_lingua(req.get("lang")),
                    gpt_cond_latent=gpt_lat, speaker_embedding=spk_emb,
                    speed=float(req.get("speed") or 1.0),
                    **_limite_generazione(testo))
            import numpy as _np
            _n = _np.asarray(out["wav"]).size
            log(f"sintesi: {_n/24000:.2f}s di audio per {len(testo)} char "
                f"(voce {voce!r}, speed {req.get('speed')}) -> {testo[:60]!r}")
            _scrivi(out["wav"], 24000)
        except Exception as e:  # noqa: BLE001
            log(f"sintesi fallita: {type(e).__name__}: {e}")
            _errore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
