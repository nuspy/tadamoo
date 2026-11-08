"""Memoria del processo: allocatore, rilascio e caricamento dei pesi senza copie.

Perche' esiste (misurato il 17/10/2026 sui processi vivi e con prove isolate in
sola CPU, nessun servizio toccato):

1. **La memoria liberata non tornava al sistema.** torch su Windows usa
   mimalloc, che dopo un `free` tiene i blocchi per un ritardo di "purge" e li
   restituisce solo quando il processo torna ad allocare. Un processo fermo
   (il caso dei servizi a riposo) non alloca piu': i blocchi restavano
   *committati* per sempre. Prova: whisper medium su CPU, dopo `del model` +
   `gc.collect()` il commit restava a 5,9-7,4 GiB; con `MIMALLOC_PURGE_DELAY=0`
   scende a 1,6 GiB (la base di `import torch`) nello stesso istante. Nessun
   costo misurabile in velocita' (3 trascrizioni whisper small su CPU: 2,45 s
   contro 2,4-2,7 s). Va impostata PRIMA di importare torch: la leggono
   all'avvio del processo.

2. **Il caricamento passava da copie complete in RAM.** `whisper.load_model`
   costruisce il modello in fp32 sulla CPU (3 GiB per `medium`, che con
   l'overhead dell'allocatore diventano 4,5 GiB di commit) e solo dopo lo manda
   in GPU; MuseTalk fa lo stesso con l'UNet (3,4 GB fp32 + 3,4 GB di state
   dict). Qui i pesi vanno dal file (mmap) alla destinazione un tensore alla
   volta: in GPU senza passare dalla RAM, in CPU in un unico buffer
   contiguo (2,85 GiB di commit invece di 4,5 per il medesimo modello).

Limite noto (misurato da Opus su WDDM, RTX 5090): ogni GiB allocato in VRAM
addebita +1 GiB al commit del processo (non e' RAM fisica) e il solo contesto
CUDA costa ~2,45 GiB di commit per processo. Il caricamento diretto toglie le
copie in RAM, NON questo addebito: l'unico rimedio e' liberare la VRAM
(`del` + `empty_cache`) o far uscire il processo.

Niente di questo importa torch al caricamento del modulo: e' importabile dal
processo principale senza pagarne il costo.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import sys

log = logging.getLogger("callbridge.memoria")


def configura_allocatore() -> None:
    """Fa restituire subito al sistema la memoria che torch libera.

    Idempotente; non sovrascrive un valore gia' scelto dall'utente. Da chiamare
    prima di `import torch` (la legge mimalloc all'inizializzazione)."""
    os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")


def memoria_processo() -> dict:
    """Commit (Private Bytes) e working set del processo corrente, in GiB.

    Solo Windows (e' il contatore che pesa sulla "commit" di sistema); altrove
    restituisce un dict vuoto."""
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        from ctypes import wintypes as w

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                        ("PrivateUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(_PMC), w.DWORD]
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(pmc), pmc.cb):
            return {}
        gib = 1024 ** 3
        return {"commit_gib": round(pmc.PrivateUsage / gib, 2),
                "ram_gib": round(pmc.WorkingSetSize / gib, 2)}
    except Exception:  # noqa: BLE001 - e' solo diagnostica
        return {}


def rilascia(cuda: bool = True) -> dict:
    """Restituisce al sistema tutta la memoria non piu' referenziata.

    `gc.collect()` esegue i finalizzatori (che liberano tensori e sessioni);
    `empty_cache()` ritorna i blocchi liberi del caching allocator CUDA al
    driver. Non inizializza CUDA se non e' gia' in uso. Restituisce il commit
    prima/dopo, utile da mostrare nella GUI."""
    prima = memoria_processo()
    gc.collect()
    gc.collect()
    if cuda and "torch" in sys.modules:
        try:
            import torch
            if torch.cuda.is_available() and torch.cuda.is_initialized():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception as e:  # noqa: BLE001 - best-effort
            log.debug("rilascia: torch.cuda non disponibile (%s)", e)
    dopo = memoria_processo()
    return {"prima": prima, "dopo": dopo}


# ---------------------------------------------------------------------------
#  Costruzione di un modello senza inizializzarlo
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def senza_init():
    """Spegne le inizializzazioni dei pesi di `torch.nn.init` (random, che poi
    `load_state_dict` sovrascrive comunque) e consente di costruire sul device
    "meta", cioe' senza allocare nulla."""
    import torch
    init = torch.nn.init
    originali = {n: getattr(init, n) for n in dir(init)
                 if n.endswith("_") and not n.startswith("_")
                 and callable(getattr(init, n))}
    for n in originali:
        setattr(init, n, lambda t, *a, **k: t)
    try:
        yield
    finally:
        for n, f in originali.items():
            setattr(init, n, f)


def consolida_cpu(modello) -> int:
    """Porta i pesi float di un modello CPU in UN solo buffer contiguo.

    Un modello fatto di centinaia di tensori da 4-16 MiB costa all'allocatore
    circa il 60% in piu' di commit del dovuto (misurato: whisper medium, 2,85
    GiB di pesi, 4,5 GiB di commit); in un blocco unico il commit e' esatto.
    Serve ai modelli che per scelta stanno in RAM (STT/TTS su CPU), caricati da
    librerie che non permettono di scegliere come allocare. Ritorna i byte
    consolidati. Non tocca i tensori fuori dalla CPU."""
    import torch
    voci = []
    for mod in modello.modules():
        for nome, p in list(mod._parameters.items()):
            if (p is not None and p.device.type == "cpu"
                    and p.dtype.is_floating_point):
                voci.append(("p", mod, nome, p))
        for nome, b in list(mod._buffers.items()):
            if (b is not None and b.device.type == "cpu"
                    and b.dtype.is_floating_point and not b.is_sparse):
                voci.append(("b", mod, nome, b))
    per_tipo: dict = {}
    for v in voci:
        per_tipo.setdefault(v[3].dtype, []).append(v)
    totale = 0
    for dtype, lista in per_tipo.items():
        n_el = sum(v[3].numel() for v in lista)
        piatto = torch.empty(n_el, dtype=dtype)
        off = 0
        for genere, mod, nome, t in lista:
            vista = piatto[off:off + t.numel()].view(t.shape)
            vista.copy_(t.data)
            off += t.numel()
            if genere == "p":
                t.data = vista
            else:
                mod._buffers[nome] = vista
        totale += n_el * piatto.element_size()
    gc.collect()
    return totale


# ---------------------------------------------------------------------------
#  whisper: dal checkpoint alla destinazione, un tensore alla volta
# ---------------------------------------------------------------------------
def _percorso_whisper(nome: str) -> tuple[str, bytes | None]:
    """(file del checkpoint, alignment_heads) come fa `whisper.load_model`, ma dalla cartella
    dei modelli e senza scaricare: `ModelNotInstalled` se la taglia non c'e'."""
    import whisper
    from . import models_store
    if nome in whisper._MODELS:
        return models_store.whisper_checkpoint(nome), whisper._ALIGNMENT_HEADS[nome]
    if os.path.isfile(nome):
        return nome, None
    raise RuntimeError(f"modello whisper non trovato: {nome}")


def carica_whisper(nome: str, device: str, half: bool):
    """Equivalente di `whisper.load_model(nome, device)` (+ `.half()` se `half`)
    senza la copia completa in RAM.

    - **cuda**: il modello nasce vuoto (meta) e ogni tensore va dal file mmap
      direttamente in VRAM, gia' nel dtype finale. Picco di RAM: un tensore.
    - **cpu**: i pesi finiscono in un unico buffer contiguo fp32 (o fp16 se
      `half`). Niente copia del checkpoint, niente modello inizializzato a
      caso poi sovrascritto.

    Qualunque imprevisto (versione di whisper diversa, formato del file) ripiega
    sul caricamento originale: meglio lento che muto."""
    import torch
    import whisper
    from . import models_store
    try:
        return _carica_whisper_diretto(nome, device, half)
    except models_store.ModelNotInstalled:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("whisper: caricamento diretto non riuscito (%s: %s), "
                    "uso whisper.load_model", type(e).__name__, e)
        gc.collect()
        # dal file locale: con un nome `load_model` scaricherebbe
        m = whisper.load_model(_percorso_whisper(nome)[0], device=device)
        if half and device != "cpu":
            m = m.half()
        return m


def _carica_whisper_diretto(nome: str, device: str, half: bool):
    import numpy as np
    import torch
    from whisper.model import ModelDimensions, Whisper

    percorso, teste = _percorso_whisper(nome)
    ck = torch.load(percorso, map_location="cpu", mmap=True, weights_only=True)
    dims = ModelDimensions(**ck["dims"])
    sd = ck["model_state_dict"]
    dtype = torch.float16 if half else torch.float32

    # Whisper registra `alignment_heads` con `.to_sparse()`, non supportato sul
    # device meta: durante la costruzione lo si lascia passare com'e'.
    to_sparse = torch.Tensor.to_sparse
    torch.Tensor.to_sparse = lambda self, *a, **k: (
        self if self.is_meta else to_sparse(self, *a, **k))
    try:
        with senza_init(), torch.device("meta"):
            modello = Whisper(dims)
    finally:
        torch.Tensor.to_sparse = to_sparse

    nuovi = {}
    if device == "cpu":
        n_el = sum(v.numel() for v in sd.values() if v.is_floating_point())
        piatto = torch.empty(n_el, dtype=dtype)
        off = 0
        for k, v in sd.items():
            if v.is_floating_point():
                vista = piatto[off:off + v.numel()].view(v.shape)
                vista.copy_(v)
                off += v.numel()
                nuovi[k] = vista
            else:
                nuovi[k] = v.clone()
    else:
        for k, v in sd.items():
            nuovi[k] = v.to(device=device,
                            dtype=dtype if v.is_floating_point() else v.dtype)
    modello.load_state_dict(nuovi, assign=True)
    del ck, sd, nuovi

    # Buffer non persistenti, assenti dallo state dict: si ricostruiscono.
    mask = torch.empty(dims.n_text_ctx, dims.n_text_ctx).fill_(-np.inf).triu_(1)
    modello.decoder.register_buffer("mask", mask.to(device=device, dtype=dtype),
                                    persistent=False)
    if teste is not None:
        modello.set_alignment_heads(teste)
    else:
        modello.register_buffer(
            "alignment_heads",
            torch.zeros(dims.n_text_layer, dims.n_text_head, dtype=torch.bool).to_sparse(),
            persistent=False)
    if device != "cpu":
        modello.register_buffer("alignment_heads",
                                modello.alignment_heads.to(device), persistent=False)
    meta = [n for n, t in list(modello.named_parameters())
            + list(modello.named_buffers()) if t.is_meta]
    if meta:
        raise RuntimeError(f"tensori rimasti su meta: {meta[:3]}")
    return modello
