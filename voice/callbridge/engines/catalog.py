"""Catalogo dei motori TTS: una sola fonte di verita' per bridge e GUI.

Prima ogni pezzo sapeva i motori a modo suo: `/tts/models` aveva una lista
cablata, il router indovinava il motore dal NOME della voce, e la GUI rifaceva
il catalogo Kokoro in TypeScript. Aggiungere un motore voleva dire toccare
quattro punti e dimenticarne almeno uno.

Qui ogni motore e' una `EngineSpec` che dichiara:
  - come si sintetizza (in-process oppure helper in un venv separato);
  - da dove vengono le sue voci (`voices_from`);
  - quali lingue copre.

`elenca(cfg)` restituisce il catalogo con la disponibilita' REALE su disco, ed
e' quello che alimentano sia `GET /tts/models` sia `GET /tts/voices`.

Perche' i venv separati: XTTS, CosyVoice, Dia e Orpheus hanno stack torch/CUDA
incompatibili tra loro e col processo del bridge (che usa onnxruntime). Girano
quindi come subprocess helper, esattamente come gia' faceva Chatterbox.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .. import models_store

log = logging.getLogger("callbridge.engines.catalog")

# Radice dei venv dei motori pesanti (uno per motore, creati a mano).
_VENV_ROOT_DEFAULT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "tts-venvs"))


@dataclass(frozen=True)
class EngineSpec:
    id: str                      # id stabile usato da GUI e config
    name: str                    # etichetta mostrata all'utente
    engine: str                  # famiglia (== id, tranne alias storici)
    # "builtin"  voci fisse dichiarate qui sotto
    # "cloned"   le voci clonate in <models folder>/tts/voices
    # "piper"    i modelli .onnx presenti nella cartella Piper
    voices_from: str
    langs: tuple[str, ...]       # ISO 639-1; () = nessun vincolo dichiarato
    # Voci fisse (nome, lingua) per voices_from == "builtin".
    voices: tuple[tuple[str, str], ...] = ()
    venv: str = ""               # nome della cartella venv sotto _VENV_ROOT
    helper: str = ""             # helper accanto a questo modulo
    note: str = ""
    realtime: float = 0.0        # velocita' misurata (x realtime), 0 = ignota
    extra_env: dict = field(default_factory=dict)
    # Variabili d'ambiente dell'helper prese dalla config: {ENV: campo_cfg}.
    # Cambiarne una dalla GUI riavvia l'helper (es. il modello di Qwen3-TTS).
    cfg_env: dict = field(default_factory=dict)
    # L'helper sa consegnare l'audio A PEZZI mentre genera ("stream": true nel
    # protocollo stdio). Vedi `VenvTtsEngine.synth_stream`.
    streaming: bool = False
    # Id del modello nel catalogo vocale (models_store.MODELS); `{size}` e' la
    # taglia scelta in config. I pesi si leggono solo da <models folder>/tts/<id>/.
    model: str = ""


# Catalogo Kokoro: 9 lingue, fisse nel modello.
_KOKORO_VOICES = (
    ("if_sara", "it"), ("im_nicola", "it"),
    ("af_heart", "en"), ("am_michael", "en"),
    ("bf_emma", "en"), ("bm_george", "en"),
    ("ff_siwis", "fr"),
    ("ef_dora", "es"), ("em_alex", "es"),
    ("pf_dora", "pt"), ("pm_alex", "pt"),
    ("jf_alpha", "ja"), ("jm_kumo", "ja"),
    ("zf_xiaoxiao", "zh"), ("zm_yunxi", "zh"),
    ("hf_alpha", "hi"), ("hm_omega", "hi"),
)

# Qwen3-TTS: le 9 voci del modello CustomVoice, con la loro lingua madre (le
# usa in tutte le 10 lingue del modello, italiano compreso).
_QWEN3_PRESET = (
    ("vivian", "zh"), ("serena", "zh"), ("uncle_fu", "zh"), ("dylan", "zh"),
    ("eric", "zh"), ("ryan", "en"), ("aiden", "en"), ("ono_anna", "ja"),
    ("sohee", "ko"),
)

# Orpheus: 8 voci fisse, solo inglese.
_ORPHEUS_VOICES = tuple(
    (v, "en") for v in ("tara", "leah", "jess", "mia", "zoe", "leo", "dan", "zac"))


SPECS: tuple[EngineSpec, ...] = (
    EngineSpec(
        id="kokoro", name="Kokoro", engine="kokoro",
        voices_from="builtin", voices=_KOKORO_VOICES,
        langs=("it", "en", "fr", "es", "pt", "ja", "zh", "hi"),
        model="kokoro", note="in-process, il piu' veloce", realtime=6.9),
    EngineSpec(
        id="piper", name="Piper", engine="piper", voices_from="piper",
        langs=(), model="piper",
        note="un modello .onnx per voce, copre le lingue assenti "
             "da Kokoro (es. ungherese)", realtime=5.0),
    EngineSpec(
        id="chatterbox", name="Chatterbox", engine="chatterbox",
        voices_from="cloned",
        langs=("it", "en", "fr", "es", "ru", "de", "pt"),
        venv="", helper="chatterbox_tts_helper.py", model="chatterbox",
        note="voice cloning, qualita' alta ma lento", realtime=0.6),
    EngineSpec(
        id="xtts", name="XTTS v2", engine="xtts", voices_from="cloned",
        langs=("it", "en", "fr", "es", "ru", "de", "pt", "pl", "tr", "nl",
               "cs", "ar", "zh", "ja", "ko", "hu", "hi"),
        venv="xtts", helper="xtts_tts_helper.py", model="xtts-v2",
        note="Coqui, 17 lingue incluso l'ungherese, cloning zero-shot",
        realtime=3.3, streaming=True),
    EngineSpec(
        id="cosyvoice", name="CosyVoice 3", engine="cosyvoice",
        voices_from="cloned",
        langs=("it", "en", "fr", "es", "ru", "de", "ja", "ko", "zh"),
        venv="cosyvoice", helper="cosyvoice_tts_helper.py",
        model="cosyvoice-3", note="miglior resa dichiarata sull'italiano; niente ungherese",
        realtime=0.8, streaming=True),
    EngineSpec(
        id="qwen3tts", name="Qwen3-TTS", engine="qwen3tts",
        voices_from="cloned",
        langs=("it", "en", "fr", "de", "es", "pt", "ru", "ja", "ko", "zh"),
        # Venv condiviso con Dia: stesso torch cu128; transformers fissato a
        # 5.15.1, che va bene a entrambi (verificato).
        venv="dia", helper="qwen3tts_tts_helper.py",
        model="qwen3tts-{size}",
        note="Alibaba Qwen: cloning zero-shot + 9 voci preimpostate, streaming "
             "(primo audio ~0,25 s), modelli 0.6B / 1.7B",
        realtime=3.7, streaming=True,
        cfg_env={"QWEN3TTS_SIZE": "qwen3tts_size"}),
    EngineSpec(
        id="orpheus", name="Orpheus 3B", engine="orpheus",
        voices_from="builtin", voices=_ORPHEUS_VOICES, langs=("en",),
        venv="orpheus", helper="orpheus_tts_helper.py", model="orpheus-3b",
        note="tag di espressivita' (<laugh>, <sigh>...) — solo inglese",
        realtime=1.0),
    EngineSpec(
        id="dia", name="Dia 1.6B", engine="dia", voices_from="cloned",
        langs=("en",),
        venv="dia", helper="dia_tts_helper.py", model="dia-1.6b",
        note="dialogo con marcatori non verbali — solo inglese",
        realtime=0.5),
)

_BY_ID = {s.id: s for s in SPECS}


def spec(engine_id: str) -> EngineSpec | None:
    return _BY_ID.get((engine_id or "").strip().lower())


def model_id(cfg, s: EngineSpec) -> str:
    """Id del modello vocale che il motore carica ("" se non ne ha uno)."""
    size = (getattr(cfg, "qwen3tts_size", "") or "0.6B").strip().lower()
    return s.model.format(size=size)


def modello_mancante(cfg, s: EngineSpec) -> str:
    """Perche' il modello del motore non e' nella cartella dei modelli, a parole
    semplici; "" se c'e'."""
    mid = model_id(cfg, s)
    return models_store.reason(mid) if mid else ""


def venv_root(cfg) -> str:
    return (getattr(cfg, "tts_venvs_dir", "") or "").strip() or _VENV_ROOT_DEFAULT


def venv_python(cfg, s: EngineSpec) -> str:
    """Python del venv del motore ("" se il motore non ne usa uno)."""
    if not s.venv:
        return ""
    return os.path.join(venv_root(cfg), s.venv, "Scripts", "python.exe")


def helper_path(s: EngineSpec) -> str:
    return os.path.join(os.path.dirname(__file__), s.helper) if s.helper else ""


def disponibile(cfg, s: EngineSpec, router=None) -> bool:
    """Disponibilita' REALE: il motore puo' sintetizzare adesso?

    Piper lo sa il router (ha le sue voci in una cartella); per gli altri serve il
    modello nella cartella dei modelli e, per i motori pesanti, il venv e
    l'helper su disco. Chatterbox e' un caso a parte: il suo venv non sta sotto
    tts-venvs/ ma dove l'aveva creato Handy, quindi lo decide il router.
    """
    if s.id == "piper":
        return bool(router is not None and router.piper_available)
    if modello_mancante(cfg, s):
        return False
    if s.id == "kokoro":
        return True
    if s.id == "chatterbox":
        return bool(router is not None and router.clone_available)
    py = venv_python(cfg, s)
    return bool(py and os.path.isfile(py) and os.path.isfile(helper_path(s)))


def voci(cfg, s: EngineSpec, router=None) -> list[dict]:
    """Voci offerte dal motore, nella forma {voice, lang, language}."""
    if s.voices_from == "builtin":
        return [{"voice": v, "lang": lg, "language": lg} for v, lg in s.voices]
    if s.voices_from == "piper":
        nomi = router.piper_voices() if router is not None else []
        # Il nome del modello Piper porta la lingua: hu_HU-anna-medium -> hu
        return [{"voice": n, "lang": n.split("_")[0] if "_" in n else "",
                 "language": n} for n in nomi]
    if s.voices_from == "cloned":
        nomi = _voci_clonate(router)
        # Le voci clonate non hanno una lingua propria: il motore le rende in
        # tutte quelle che dichiara di supportare.
        out = [{"voice": n, "lang": "", "language": "clonata"} for n in nomi]
        # XTTS porta anche 58 speaker DI SERIE, ognuno multilingua: sono
        # utilizzabili senza registrare campioni e non perdono il timbro
        # cambiando lingua. Restavano invisibili perché il catalogo guardava
        # solo la cartella dei cloni.
        if s.id == "xtts":
            out += [{"voice": n, "lang": "", "language": "integrata"}
                    for n in _xtts_speakers(router)]
        # Chatterbox ha UNA voce di serie, che l'helper usa quando il nome non
        # corrisponde a nessun campione. Era raggiungibile solo per sbaglio:
        # qui diventa una scelta, utile a chi non ha clonato niente.
        if s.id == "chatterbox":
            out.append({"voice": "default", "lang": "", "language": "integrata"})
        # Qwen3-TTS: le voci del modello CustomVoice accanto ai cloni (che usano
        # il modello Base): l'helper sceglie il modello dalla voce.
        if s.id == "qwen3tts":
            out += [{"voice": v, "lang": lg, "language": "preimpostata"}
                    for v, lg in _QWEN3_PRESET]
        return out
    return []


_xtts_cache: list[str] | None = None


def _xtts_speakers(router) -> list[str]:
    """Nomi dei 58 speaker di serie di XTTS, letti da `speakers_xtts.pth` nella
    cartella del modello.

    Si legge il file direttamente invece di avviare l'helper: caricare XTTS
    per un elenco costerebbe gigabyte e decine di secondi. Memoizzato: il file
    non cambia mai."""
    global _xtts_cache
    if _xtts_cache is not None:
        return _xtts_cache
    _xtts_cache = []
    where = models_store.folder("tts", "xtts-v2")
    p = os.path.join(where, "speakers_xtts.pth") if where else ""
    if not p or not os.path.isfile(p):
        return _xtts_cache
    try:
        import torch
        dati = torch.load(p, map_location="cpu", weights_only=False) or {}
        _xtts_cache = sorted(str(k) for k in dati)
        log.info("XTTS: %d speaker integrati nel catalogo", len(_xtts_cache))
    except Exception as e:  # noqa: BLE001 - restano le sole voci clonate
        log.warning("XTTS: speaker integrati non letti: %s", e)
    return _xtts_cache


def _voci_clonate(router) -> list[str]:
    if router is None:
        return []
    d = models_store.voices_dir()
    if not d or not os.path.isdir(d):
        return []
    esti = (".wav", ".mp3", ".flac", ".ogg")
    nomi = {os.path.splitext(f)[0] for f in os.listdir(d)
            if f.lower().endswith(esti)}
    return sorted(nomi)


def elenca(cfg, router=None, disponibile_fn=None, con_voci: bool = True) -> list[dict]:
    """Catalogo completo per la GUI: un elemento per motore, con voci e stato.

    `disponibile_fn(spec) -> bool` sostituisce il calcolo della disponibilita'
    (serve a chi non ha un router: vedi `engines.catalogo_motori`); `con_voci=False`
    salta le voci (XTTS leggerebbe i suoi speaker con torch)."""
    out = []
    for s in SPECS:
        ok = disponibile_fn(s) if disponibile_fn is not None else disponibile(cfg, s, router)
        out.append({
            "id": s.id,
            "name": s.name,
            "engine": s.engine,
            "is_downloaded": ok,
            # `is_cloning` resta per compatibilita' con la GUI storica: vuol
            # dire "motore a voci clonate" (prima significava "in download").
            "is_cloning": s.voices_from == "cloned",
            "voices_from": s.voices_from,
            "langs": list(s.langs),
            "note": s.note,
            "realtime": s.realtime,
            "streaming": s.streaming,
            "voices": voci(cfg, s, router) if (ok and con_voci) else [],
            "model": model_id(cfg, s),
            "reason": "" if ok else (modello_mancante(cfg, s) or "the Python environment of this engine is missing"),
        })
    return out
