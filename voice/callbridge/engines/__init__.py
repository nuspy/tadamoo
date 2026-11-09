"""Motori STT/TTS nativi in-process (integrazione di Handy).

Factory che, in base a `cfg.stt_backend`/`cfg.tts_backend`, restituiscono un
client nativo (in-process) o il client WebSocket verso Handy. I motori nativi
sono singleton condivisi (il modello si carica una sola volta).

Interfaccia dei client identica a `HandySttClient`/`HandyTtsClient`, cosi'
`StreamingSession` e gli altri consumatori non cambiano.
"""

from __future__ import annotations

import logging
import threading
import time

from .kokoro_tts import KokoroTtsEngine, NativeTtsClient
from .stt_shared import SharedSttEngine
from .tts_router import TtsRouter
from .whisper_stt import NativeSttClient, precisione_effettiva

log = logging.getLogger("callbridge.engines")

_lock = threading.Lock()
_stt_engine: SharedSttEngine | None = None
_tts_engine: TtsRouter | None = None


# "Sospendi": lo stato (file sentinella su disco) vive in `callbridge/sospensione.py`;
# qui solo il rimando per chi lo importava da `engines`.
from ..sospensione import (MSG_SOSPESO, SOSPENSIONE_FILE, ComponentiSospesi,  # noqa: E402,F401
                           in_sospensione, richiedi_attivi, riprendi, sospendi)


def _get_stt_engine(cfg) -> SharedSttEngine:
    """L'UNICO STT: il processo whisper condiviso da dettatura, sessioni voce,
    push-to-talk e /stt/stream (vedi `stt_shared`). Il bridge non carica piu'
    un whisper suo."""
    global _stt_engine
    with _lock:
        if _stt_engine is None:
            _stt_engine = SharedSttEngine(cfg)
        return _stt_engine


_controllo_stt: threading.Thread | None = None


def avvia_controllo_stt(cfg, intervallo: float | None = None) -> bool:
    """Avvia (una volta) il controllo periodico dell'STT: vivo e in VRAM.
    Vale anche da sospeso. True se avviato ora."""
    global _controllo_stt
    from .stt_shared import INTERVALLO_CONTROLLO_S
    pausa = INTERVALLO_CONTROLLO_S if intervallo is None else intervallo
    with _lock:
        if _controllo_stt is not None and _controllo_stt.is_alive():
            return False

        def _giri() -> None:
            while True:
                time.sleep(pausa)
                try:
                    _get_stt_engine(cfg).controlla()
                except Exception:  # noqa: BLE001 - il controllo non deve morire
                    log.exception("controllo STT fallito")

        _controllo_stt = threading.Thread(target=_giri, name="stt-controllo",
                                          daemon=True)
        _controllo_stt.start()
        return True


def chiudi_stt() -> None:
    """Solo all'uscita del bridge: non lascia il processo STT orfano."""
    if _stt_engine is not None:
        _stt_engine.chiudi()


def _get_tts_engine(cfg) -> TtsRouter:
    global _tts_engine
    with _lock:
        if _tts_engine is None:
            # Router: Kokoro per le voci builtin, Chatterbox per le clonate.
            # Stessa interfaccia di KokoroTtsEngine (model_dir/load/synth/close).
            _tts_engine = TtsRouter(cfg)
        return _tts_engine


def aggiorna_tts(cfg, motori: "set[str] | None" = None, scarica: bool = False) -> None:
    """Applica al router TTS gia' creato le scelte nuove della config (motore,
    device, memoria, cartelle dei modelli); senza router non c'e' niente da fare,
    perche' quando nascera' leggera la config. Non carica nessun modello.

    `motori`: ids dei motori cambiati (None = tutti). `scarica`: chiudi prima gli
    helper (serve quando cambia la GPU, che gli helper ricevono all'avvio)."""
    router = _tts_engine
    if router is None:
        return
    if scarica:
        router.scarica_tutti()
    router.usa_cartelle(cfg)
    router.reconfigure(cfg, solo=motori)
    scelto = (getattr(cfg, "tts_engine", "") or "kokoro").strip().lower()
    if scelto != router.attivo:
        router.attiva(scelto, precarica=False)


def _release_gpu_memory() -> None:
    """Rilascio IMMEDIATO della VRAM dopo che gli engine in-process hanno scartato
    i modelli (reconfigure). NECESSARIO: il caching allocator di torch (Whisper) e
    le sessioni onnxruntime (Kokoro/Piper) trattengono la VRAM finche' non si forza
    `gc.collect()` (esegue i finalizzatori che liberano le allocazioni CUDA) +
    `torch.cuda.empty_cache()` (ritorna i blocchi liberi al driver). Best-effort:
    Chatterbox/avatar liberano gia' da soli uccidendo il proprio subprocess."""
    import gc
    import sys
    gc.collect()
    # Se torch non e' ancora nel processo non c'e' nulla da restituire: importarlo
    # QUI costerebbe ~0,8 GiB di commit al bridge (misurato) solo per svuotare
    # una cache che non esiste. Idem se CUDA non e' mai stata inizializzata.
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_available() and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception as e:  # noqa: BLE001 - torch rotto: best-effort
        log.debug("release VRAM: torch non utilizzabile (%s)", e)


def catalogo_motori(cfg) -> list[dict]:
    """Elenco dei motori TTS (id, nome, disponibilita', modello e motivo...) per la GUI.

    Stessa fonte di `TtsRouter.catalogo()` (`catalog.elenca`), ma NON crea il router
    e non carica nulla: se il router esiste lo interroga, altrimenti legge i file su
    disco. Niente voci. Cosi' l'elenco si vede anche da sospeso, quando il server
    delle voci (:8720) e' spento."""
    from . import catalog
    from .tts_router import disponibile_senza_router
    router = _tts_engine
    if router is not None:
        def fn(s):
            return router.engine_available(s.id)
    else:
        def fn(s):
            return disponibile_senza_router(cfg, s)
    return catalog.elenca(cfg, router, disponibile_fn=fn, con_voci=False)


def engines_status(cfg) -> dict:
    """Dove girano DAVVERO STT e TTS, non dove dovrebbero.

    Serve alla GUI: i selettori mostrano l'intenzione (config), ma il motore in
    memoria può essere stato caricato con impostazioni diverse — si applicano al
    caricamento successivo — e il file .onnx può differire dalla precisione
    chiesta. Senza questo bisognava incrociare la GUI col log del bridge.
    """
    from .device import resolve_device
    out: dict = {}
    if _stt_engine is not None and hasattr(_stt_engine, "runtime_info"):
        out["stt"] = _stt_engine.runtime_info()
    else:
        atteso = resolve_device(cfg.stt_device, cfg.stt_gpu_memory)
        out["stt"] = {"configured": f"{atteso}/{cfg.stt_gpu_memory}",
                      "would_load_on": atteso, "loaded": False, "state": "stopped",
                      "running_on": None, "model": cfg.stt_model,
                      "precision": precisione_effettiva(atteso, cfg.stt_compute_type)}
    kok = getattr(_tts_engine, "_kokoro", None) if _tts_engine is not None else None
    if kok is not None and hasattr(kok, "runtime_info"):
        out["tts"] = kok.runtime_info()
    else:
        out["tts"] = {"configured": f"{cfg.tts_device}/{cfg.tts_gpu_memory}",
                      "would_load_on": resolve_device(cfg.tts_device, cfg.tts_gpu_memory),
                      "loaded": False, "running_on": None,
                      "file": None, "precision": getattr(cfg, "kokoro_precision", "fp16")}
    return out


def reset_engines(cfg, tts: bool = True, stt: bool = False,
                  motori_tts: "set[str] | None" = None) -> None:
    """Riconfigura IN PLACE gli engine col device/memoria/precisione correnti di
    `cfg` (cambiati dalla GUI). In place (non si sostituisce l'oggetto) perche'
    il compat server ne cattura il riferimento all'avvio.

    TTS: tocca SOLO i motori in `motori_tts` (quelli il cui campo e' cambiato;
    None = tutti). Un motore toccato viene scaricato; se era l'attivo ed era in
    memoria viene rimesso subito col nuovo valore, altrimenti non si carica. Gli
    altri motori non vengono toccati.
    STT: l'unico STT e' sempre attivo, quindi si RICARICA SUBITO (in background)
    sul nuovo modello/device/precisione; non viene mai scaricato a riposo."""
    riscalda = ""
    with _lock:
        if tts and _tts_engine is not None:
            # Se il motore attivo era in memoria va rimesso li' col NUOVO device:
            # altrimenti cambiare device dalla GUI lo lasciava scarico e il
            # prossimo turno di voce pagava il caricamento completo.
            attivo = getattr(_tts_engine, "attivo", "")
            toccato = motori_tts is None or attivo in motori_tts
            if attivo and toccato and _tts_engine._caricato(attivo):
                riscalda = attivo
            _tts_engine.reconfigure(cfg, motori_tts)
            log.info("TTS riconfigurato (%s): ricarica al prossimo uso%s",
                     "tutti i motori" if motori_tts is None
                     else ", ".join(sorted(motori_tts)),
                     f", subito '{riscalda}'" if riscalda else "")
        if stt and _stt_engine is not None:
            _stt_engine.reconfigure(cfg)
            log.info("STT riconfigurato: ricarica immediata in background")
        if tts or stt:
            _release_gpu_memory()
    if riscalda:
        # Fuori dal lock: `attiva` avvia il warm-up in un thread proprio.
        try:
            _tts_engine.attiva(riscalda)
        except Exception as e:  # noqa: BLE001
            log.warning("riscaldamento di '%s' dopo il reset fallito: %s",
                        riscalda, e)


def make_stt_client(cfg, follow: bool = False):
    """Client STT: nativo (in-process) o Handy (WS), secondo `cfg.stt_backend`.

    `follow`: the utterances come with the language the recognizer heard (callbridge/language_follow.py), so the language is detected, not
    hinted, and `take_language_info()` gives it."""
    if getattr(cfg, "stt_backend", "handy") == "native":
        return NativeSttClient(
            _get_stt_engine(cfg), lang=(None if follow else cfg.stt_language or None),
            report_language=follow,
            allowed_languages=tuple(getattr(cfg, "language_enabled", ()) or ()),
            vad_threshold=cfg.stt_vad_threshold,
            endpoint_silence_s=cfg.stt_endpoint_silence_s,
            min_utterance_s=cfg.stt_min_utterance_s,
            vad_model=cfg.stt_vad_model, speech_prob=cfg.stt_vad_speech_prob,
            chunk_silence_s=getattr(cfg, 'stt_chunk_silence_s', 0.0),
            chunk_min_s=getattr(cfg, 'stt_chunk_min_s', 2.0))
    from ..stt_client import HandySttClient
    return HandySttClient(cfg.handy_stt_url())


def make_tts_client(cfg):
    """Client TTS: nativo (in-process) o Handy (WS), secondo `cfg.tts_backend`."""
    if getattr(cfg, "tts_backend", "handy") == "native":
        return NativeTtsClient(_get_tts_engine(cfg), cfg)
    from ..tts_client import HandyTtsClient
    return HandyTtsClient(cfg.handy_tts_url())


def warm_up(cfg) -> bool:
    """Scalda in anticipo i motori nativi selezionati. True se ok."""
    ok = True
    if getattr(cfg, "stt_backend", "handy") == "native":
        try:
            _get_stt_engine(cfg).load()
        except Exception as e:  # noqa: BLE001
            log.error("warm-up STT nativo fallito: %s", e)
            ok = False
    if getattr(cfg, "tts_backend", "handy") == "native":
        try:
            eng = _get_tts_engine(cfg)
            # Comanda l'AVATAR ATTIVO: il motore da scaldare e' quello della
            # voce della sua identita'. `tts_engine` in config resta solo come
            # ripiego quando non c'e' un avatar. Senza questo, al riavvio si
            # scaldava il motore vecchio (2,3 GB di XTTS con l'avatar ormai su
            # Kokoro) e il primo turno pagava lo swap: ~60 s misurati.
            attivo = ""
            if hasattr(eng, "voce_avatar_attivo"):
                attivo = (eng.voce_avatar_attivo().get("engine") or "").strip().lower()
            if not attivo:
                attivo = (getattr(cfg, "tts_engine", "") or "kokoro").strip().lower()
            # Si scalda il motore SCELTO, non per forza Kokoro: se la selezione
            # persistita e' XTTS, scaldare Kokoro non serviva a nulla e il primo
            # turno pagava comunque il caricamento di XTTS.
            if attivo and attivo != "kokoro" and hasattr(eng, "attiva"):
                eng.attiva(attivo)
            else:
                eng.load()
        except Exception as e:  # noqa: BLE001
            log.error("warm-up TTS nativo fallito: %s", e)
            ok = False
    return ok
