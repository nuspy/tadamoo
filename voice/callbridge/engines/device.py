"""Risoluzione device (CPU/GPU) + bootstrap CUDA per gli engine onnxruntime.

Due selettori per ogni engine, come Kokoro:
  - device: "cpu" | "cuda"
  - memory: "dedicated" | "shared"   (solo se device=cuda)

`resolve_device()` mappa la coppia sul device EFFETTIVO. Nota onesta: su GPU
discreta con CUDA lo "shared VRAM" non e' un target di allocazione ottenibile
(vedi discussione), quindi "shared" esegue su **CPU** = zero VRAM dedicata (utile
quando altri sistemi, es. LM Studio, occupano la VRAM). L'utente sceglie:
  cuda + dedicated -> GPU (VRAM dedicata)
  cuda + shared    -> CPU (RAM, 0 VRAM dedicata)
  cpu + *          -> CPU

`ensure_ort_cuda()` carica le DLL CUDA 12 (dai wheel nvidia-*-cu12) necessarie
all'EP CUDA di onnxruntime: senza, `onnxruntime_providers_cuda.dll` non trova
cublasLt64_12.dll ecc. e ricade su CPU in silenzio.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("callbridge.engines.device")

_preloaded = False
_preload_lock = threading.Lock()


def resolve_device(device: str, memory: str = "") -> str:
    """(device) -> "cuda" | "cpu".

    `memory` è ACCETTATO E IGNORATO. Prima valeva "shared" o "dedicated", ma
    "shared" non era affatto una gestione della memoria: significava
    semplicemente "esegui su CPU", con un nome che lo nascondeva. Serviva come
    ripiego quando la GPU non partiva (torch occupava le cuDNN e onnxruntime
    ripiegava in silenzio); da quando i motori girano in subprocess quel
    conflitto non c'è più, e l'unico effetto rimasto era rallentare di ~10x chi
    la sceglieva credendo di ottimizzare la VRAM. Chi vuole la CPU la chiede
    con `device`, che è dove ci si aspetta di trovarla.

    Il parametro resta nella firma per non rompere i chiamatori e le config
    esistenti, dove `*_gpu_memory` può ancora comparire.
    """
    return "cuda" if (device or "cpu").strip().lower() == "cuda" else "cpu"


def _add_nvidia_dll_dirs() -> list[str]:
    """Mette le cartelle `bin` dei wheel NVIDIA nel PATH del processo.

    `ort.preload_dlls()` carica `cudnn64_9.dll`, ma cuDNN risolve le PROPRIE
    sotto-librerie (`cudnn_engines_*`) con LoadLibrary(nome), che guarda solo il
    PATH: senza questo si otteneva
        Could not locate cudnn_engines_tensor_ir64_9.dll
        CUDNN_STATUS_SUBLIBRARY_LOADING_FAILED
    e onnxruntime ripiegava **in silenzio** sulla CPU — con il log che
    continuava a dire "su CUDA" perché riporta il device richiesto, non quello
    ottenuto. Risultato: TTS lentissimo senza una ragione visibile.
    """
    import os
    import site
    aggiunte: list[str] = []

    def _aggiungi(d: str) -> None:
        if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            aggiunte.append(d)

    basi = list(site.getsitepackages())
    try:
        basi.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    # 1) I wheel NVIDIA per ULTIMI nella priorità: contengono la cuDNN completa
    #    (10 DLL, inclusa cudnn_engines_tensor_ir che a torch manca).
    for base in basi:
        nvidia = os.path.join(base, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pacchetto in os.listdir(nvidia):
            _aggiungi(os.path.join(nvidia, pacchetto, "bin"))
    # 2) `torch/lib` in TESTA: torch è schizzinoso sulle proprie cuDNN e se
    #    trova quelle dei wheel NVIDIA muore con
    #    "WinError 127: procedura non trovata" -> STT su CPU. Mettendolo per
    #    primo, torch usa le sue e onnxruntime pesca dai wheel NVIDIA solo la
    #    sotto-libreria che a torch manca. Vanno bene entrambi.
    for base in basi:
        _aggiungi(os.path.join(base, "torch", "lib"))
    return aggiunte


def ensure_ort_cuda() -> bool:
    """Carica una volta le DLL CUDA per l'EP CUDA di onnxruntime. Idempotente."""
    global _preloaded
    with _preload_lock:
        if _preloaded:
            return True
        try:
            aggiunte = _add_nvidia_dll_dirs()
            if aggiunte:
                log.info("onnxruntime: %d cartelle DLL NVIDIA aggiunte al PATH",
                         len(aggiunte))
            import onnxruntime as ort
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()          # carica CUDA/cuDNN dai wheel nvidia
                log.info("onnxruntime: DLL CUDA precaricate (EP CUDA disponibile)")
            _preloaded = True
            return True
        except Exception as e:  # noqa: BLE001 - GPU non disponibile: si usa CPU
            log.warning("onnxruntime: preload DLL CUDA fallito (%s): uso CPU", e)
            return False


def ort_providers(device: str) -> list[str]:
    """Lista provider onnxruntime per il device effettivo."""
    if device == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]
