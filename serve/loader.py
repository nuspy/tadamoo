"""The Loader: the engine settings a load uses, the memory they need next to what this PC has free now, and the speed
they should give.

Memory follows the engine's own sizing (src/program/generate.cpp, src/core/layer.cpp, src/core/mtp.cpp,
src/core/expert_source.cpp): the KV and indexer bytes per token of each --kv format, the fixed state of a session,
the dense arena from the model file, the drafter, the expert cache that takes the VRAM left over, and on the host the
resident copy of the experts that are not in that cache. What the engine cannot know before it runs (CUDA's own
context, the server's heap) is an estimate, named as one. Windows charges VRAM allocations to the commit, so the
commit a load needs is its committed host memory plus the VRAM it allocates; file-backed maps (the PLE table with
--ple-io ram) take RAM but no commit.

Rules of this PC's owner: at least 15% of the physical RAM and 15% of the commit limit stay free, and the page file
never grows: a plan that breaks one of them is refused.

Speed is predicted from a calibration measured on this model (or the seed figures until there is one), scaled by a
simple model that is documented on predict_speed()."""
from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

GiB, MiB = 1 << 30, 1 << 20
FREE_SHARE = 0.15                       # the share of RAM and of the commit limit that stays free
CONTEXT_UI_MAX = 262144                 # the longest context the Loader offers

# KV + indexer state per token of one session: 12 attention layers x (K/V cell + fp32 pooled indexer key 128 B +
# page table 1 B) + RoPE table and cell scores (layer.cpp qsa_state_bytes, session.cpp session_bytes)
KV_TOKEN_BYTES = {"fp16": 26384, "int8": 14480, "q4_0": 8720, "k8v4": 11600}
KV_CELL_BYTES = {"fp16": 2048, "int8": 1056, "q4_0": 576, "k8v4": 816}    # one layer's K/V of one token
SESSION_FIXED = 126 * MiB               # GDN recurrence and conv history (36 layers) and the scratch of one session
VERIFY_WINDOWS = 96 * MiB               # kWindowMib
LATE_DEVICE = 96 * MiB                  # late allocations the engine keeps back in serve mode
CUDA_CONTEXT = 600 * MiB                # estimate: CUDA context, cuBLAS and kernels (the engine sees less free VRAM)
VISION_SIDECAR = int(2.2 * GiB)         # --vision on: the encoder process on the main GPU
MIN_CACHE_SLOTS = 256                   # below this the prompt reads cannot borrow slots (k + 128 <= slots)
RESIDENT_SPARE_BLOBS = 96               # spare places of the resident copy
PROMPT_CHECKPOINTS = 6 * 118 * MiB      # --prompt-cache 6 checkpoints of the recurrent state
PLE_ROW_CACHE = 1048576 * 90            # --ple-io direct: the row cache
HOST_OTHER = 2 * GiB                    # estimate: the server, the engine heap and CUDA's host runtime
MTP_EXPERT_BLOB = 1382400

# what the engine does without the key (generate.cpp option defaults)
ENGINE_DEFAULTS = {"engine.max-context": 4096, "engine.kv": "fp16", "engine.prefill": None, "engine.spec": 0,
                   "engine.spec-min-p": 0, "engine.expert-cache": 0, "engine.resident-experts": False,
                   "engine.vram-reserve-mib": 700, "engine.ple-io": "direct", "engine.pool-workers": None,
                   "server.parallel": 1, "server.slot-full-context": False, "server.vision": "off", "server.port": 7232}

# "Valori sicuri" are computed from this PC and the model (safe()); SAFE is what that gives on one example PC (an RTX
# 5090 with 32 GiB, 128 GiB of RAM, a processor with 8 performance and 16 efficiency cores, the IQ3_XXS file with its
# drafter and vision projector), kept as a documented example and for the tests
SAFE = {"engine.max-context": 262144, "engine.kv": "q4_0", "engine.prefill": "auto", "engine.spec": 7,
        "engine.spec-min-p": 0.5, "engine.expert-cache": "auto", "engine.resident-experts": True,
        "engine.vram-reserve-mib": 6144, "engine.ple-io": "direct", "engine.pool-workers": 15,
        "server.parallel": 1, "server.slot-full-context": False, "server.vision": "dynamic"}

# seed speeds until this model is calibrated; SEED["note"] names their source
SEED = {"source": "seed", "decode_spec0": 184.0, "decode_spec7": 320.0, "hit_rate": 0.984, "cache_share": 0.37,
        "prefill": 892.0, "prefill_chunk": 512, "accept": None, "visible_share": None,
        "note": "README: IQ3_XXS, RTX 5090 @400 W, 184 tok/s spec 0, 320 tok/s spec 7; Strata on this PC: "
                "188 tok/s (spec 4, reasoning off, 98.4% expert-cache hits), prefill 892 tok/s at 512-token chunks"}
SPEC_COST = 0.05          # extra cost of each drafted token in a verify step, as a share of a plain step
MISS_COST = 30.0          # a decode step's slowdown per unit of expert-cache misses
PREFILL_HALF = 768        # the chunk at which prefill runs at half its large-chunk rate

# a demo model for the mock engine: the sizes of the IQ3_XXS file
DEMO_FACTS = {"name": "demo model (mock engine)", "path": None, "experts_bytes": 53477376000, "n_layers": 48,
              "n_expert": 512, "max_blob": 2176000, "dense_bytes": 1538035200, "head_bytes": 437043200,
              "has_mtp": True, "mtp_bytes": 116099072 + 707788800, "n_vocab": 248320, "token_embd_bytes": 273152000,
              "ple_bytes": 28800138240, "mmproj_bytes": 904004352, "profile_slots": 24576, "has_profile": True}


# ------------------------------------------------------------------------------------------------ the model file
def model_facts(path: str | None) -> dict:
    """The sizes a plan needs, read from the .basalt file's sections and meta (DEMO_FACTS without a file)."""
    if not path:
        return dict(DEMO_FACTS)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from model_file import ModelFile
    mf = ModelFile(path)
    meta = json.loads(mf.read("meta"))
    size = lambda name: mf.section(name)[1] if name in mf.sections else 0   # noqa: E731
    tensors = meta.get("tensors") if isinstance(meta.get("tensors"), dict) else {}
    tb = lambda name: int((tensors.get(name) or {}).get("bytes") or 0)       # noqa: E731
    blobs, n_expert = [], 0
    if "experts.layout" in mf.sections:
        text = mf.read("experts.layout").decode("utf-8", "replace")
        m = re.search(r"n_expert (\d+)", text)
        n_expert = int(m.group(1)) if m else 0
        for line in text.splitlines():
            f = line.split()
            if len(f) == 5 and not line.startswith("#") and f[4].isdigit():
                blobs.append(int(f[4]))
    head = tensors.get("output.weight") or {}
    shape = head.get("shape") or [0, 0]
    profile = meta.get("profile") if isinstance(meta.get("profile"), dict) else {}
    return {"name": meta.get("name") or Path(path).stem, "path": str(path), "experts_bytes": size("experts"),
            "n_layers": len(blobs), "n_expert": n_expert, "max_blob": max(blobs) if blobs else 0,
            "dense_bytes": size("dense"), "head_bytes": tb("output.weight"),
            "has_mtp": "mtp/dense" in mf.sections, "mtp_bytes": size("mtp/dense") + size("mtp/experts"),
            "n_vocab": int(shape[-1]) if shape else 0, "token_embd_bytes": tb("token_embd.weight"),
            "ple_bytes": tb("per_layer_token_embd.weight"), "mmproj_bytes": size("vision/mmproj"),
            "profile_slots": int(profile.get("slots") or 0), "has_profile": "profile" in mf.sections,
            "meta_hash": mf.sections["meta"][2].hex()[:16]}


# ------------------------------------------------------------------------------------------------ this PC now
def _windows_memory() -> dict | None:
    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return {"ram_total": m.ullTotalPhys, "ram_available": m.ullAvailPhys, "commit_limit": m.ullTotalPageFile,
            "commit_used": m.ullTotalPageFile - m.ullAvailPageFile}


def _linux_memory() -> dict | None:
    try:
        info = {k: int(v.split()[0]) * 1024 for k, v in (line.split(":", 1) for line in open("/proc/meminfo"))}
    except (OSError, ValueError):
        return None
    return {"ram_total": info.get("MemTotal"), "ram_available": info.get("MemAvailable"),
            "commit_limit": info.get("CommitLimit"), "commit_used": info.get("Committed_AS")}


def system_now(gpu_index: int = 0) -> dict:
    """RAM, commit and VRAM of this PC now, in bytes; a value that cannot be read is None."""
    fake = os.environ.get("BASALT_FAKE_SYSTEM")
    if fake and os.environ.get("BASALT_FAKE_SYSTEM_OK") == "1":       # the mock server's demo readings
        return {k: int(v) for k, v in json.loads(fake).items()}
    mem = (_windows_memory() if os.name == "nt" else _linux_memory()) or {}
    out = {"ram_total": mem.get("ram_total"), "ram_available": mem.get("ram_available"),
           "commit_limit": mem.get("commit_limit"), "commit_used": mem.get("commit_used"),
           "vram_total": None, "vram_free": None}
    try:
        from serve.util.telemetry import _Nvml
        nv = _Nvml(int(gpu_index or 0))
        if nv.ok():
            m = nv.Mem()
            if nv.lib.nvmlDeviceGetMemoryInfo(nv.dev, ctypes.byref(m)) == 0:
                out["vram_total"], out["vram_free"] = int(m.total), int(m.free)
    except Exception:  # noqa: BLE001 - an unreadable GPU leaves None
        pass
    return out


def engine_holds(engine) -> dict:
    """What the running engine process holds and gives back when it stops: RAM (working set) and commit (private
    bytes); zeros when it is not running or cannot be read."""
    proc = getattr(engine, "proc", None)
    alive = getattr(engine, "alive", None)
    if proc is None or (callable(alive) and not alive()):
        return {"ram": 0, "commit": 0}
    try:
        import psutil
        mi = psutil.Process(proc.pid).memory_info()
        return {"ram": int(mi.rss), "commit": int(getattr(mi, "private", 0) or getattr(mi, "vms", 0))}
    except Exception:  # noqa: BLE001 - psutil missing or the process gone
        return {"ram": 0, "commit": 0}


# ------------------------------------------------------------------------------------------------ the values
def effective(values: dict) -> dict:
    """The values with the engine's defaults filled in for the absent ones."""
    out = dict(ENGINE_DEFAULTS)
    out.update({k: v for k, v in (values or {}).items() if k in ENGINE_DEFAULTS and v is not None})
    return out


def slot_context(v: dict) -> int:
    """The context each slot holds: the total divided among the slots unless slot-full-context."""
    par, ctx = max(1, int(v["server.parallel"])), int(v["engine.max-context"])
    if par == 1 or v["server.slot-full-context"]:
        return ctx
    return max(256, ctx // par)


def prefill_chunk(v: dict, resident: bool | None = None) -> int:
    """The chunk the prompt is read in: auto has an 8192 ceiling, auto:N has N (reads are cut at 12288)."""
    p, ctx = v["engine.prefill"], slot_context(v)
    if p is None:
        return min(2048, ctx)
    if isinstance(p, int):
        return max(1, min(p, ctx))
    p = str(p)
    if p == "auto":
        return min(8192, ctx)
    if p.startswith("auto:"):
        return min(int(p[5:]), 12288, ctx)
    return min(2048, ctx)


# ------------------------------------------------------------------------------------------------ the plan
def _item(key, nbytes, estimate=False):
    return {"key": key, "bytes": int(max(0, nbytes)), "estimate": estimate}


def plan(values: dict, facts: dict, system: dict, holds: dict | None = None, holds_vram: int = 0) -> dict:
    """The memory a load with these values needs, next to what is free (plus what the running engine gives back
    when it stops: `holds` from engine_holds(), `holds_vram` its VRAM). Bytes throughout."""
    v = effective(values)
    holds = holds or {"ram": 0, "commit": 0}
    par = max(1, int(v["server.parallel"]))
    ctx = slot_context(v)
    kvf = v["engine.kv"] if v["engine.kv"] in KV_TOKEN_BYTES else "fp16"
    spec = int(v["engine.spec"] or 0)
    mode = v["server.vision"]
    # ---- VRAM
    dense = facts["dense_bytes"] + facts["head_bytes"]
    kv = par * (KV_TOKEN_BYTES[kvf] * ctx + SESSION_FIXED)
    drafter = 0
    if facts.get("has_mtp") and spec >= 2:
        cell = KV_CELL_BYTES["int8" if kvf == "k8v4" else kvf] + 128
        drafter = facts["mtp_bytes"] + par * cell * ctx + spec * facts.get("n_vocab", 0) * 4
    vision_vram = VISION_SIDECAR if mode == "on" else (ctx + 64) * 12 if mode == "dynamic" else 0
    buffers = VERIFY_WINDOWS + LATE_DEVICE
    if not facts.get("has_profile"):                    # without a profile the prompt buffers are not borrowed
        buffers += (160 + prefill_chunk(v) * 680 // 1024) * MiB
    fixed = dense + kv + drafter + vision_vram + buffers + CUDA_CONTEXT
    reserve = int(v["engine.vram-reserve-mib"]) * MiB
    vram_free = system.get("vram_free")
    vram_avail = None if vram_free is None else vram_free + holds_vram
    blob = facts["max_blob"] or 1
    total_slots = facts["profile_slots"] or (facts["n_layers"] * facts["n_expert"])
    ec = v["engine.expert-cache"]
    if ec in ("auto", 0, "0", None):
        room = (vram_avail - fixed - reserve) if vram_avail is not None else 0
        slots = max(0, min(total_slots, room // blob))
    else:
        slots = min(total_slots, int(ec))
    cache = slots * blob
    vram_need = fixed + cache
    # ---- host RAM (committed unless file-backed)
    if v["engine.resident-experts"]:
        arena = max(0, facts["experts_bytes"] - cache) + RESIDENT_SPARE_BLOBS * blob
    else:
        arena = facts["experts_bytes"] + blob
    host = [_item("experts", arena), _item("token_embd", facts["token_embd_bytes"]),
            _item("checkpoints", PROMPT_CHECKPOINTS * par)]
    if mode == "dynamic":
        host.append(_item("vision", facts["mmproj_bytes"]))
    ple_ram = facts["ple_bytes"] if v["engine.ple-io"] == "ram" else 0
    host.append(_item("ple", ple_ram if ple_ram else PLE_ROW_CACHE))
    host.append(_item("other", HOST_OTHER, True))
    ram_need = sum(h["bytes"] for h in host)
    commit_need = ram_need - ple_ram + vram_need        # the PLE map is file-backed; VRAM is charged to the commit
    # ---- available and the rules
    total, avail = system.get("ram_total"), system.get("ram_available")
    limit, used = system.get("commit_limit"), system.get("commit_used")
    ram_avail = None if avail is None else avail + holds["ram"]
    commit_avail = None if limit is None or used is None else limit - used + holds["commit"]
    ram_keep = int(FREE_SHARE * total) if total else 0
    commit_keep = int(FREE_SHARE * limit) if limit else 0
    problems = []
    if vram_avail is not None:
        if ec in ("auto", 0, "0", None):
            if slots < MIN_CACHE_SLOTS:
                problems.append({"code": "vram", "need": fixed + MIN_CACHE_SLOTS * blob + reserve, "have": vram_avail})
        elif vram_need + reserve > vram_avail:
            problems.append({"code": "vram", "need": vram_need + reserve, "have": vram_avail})
    if ram_avail is not None and ram_need > ram_avail - ram_keep:
        problems.append({"code": "ple_ram" if ple_ram and ram_need - ple_ram <= ram_avail - ram_keep else "ram",
                         "need": ram_need, "have": ram_avail, "keep": ram_keep})
    if commit_avail is not None and commit_need > commit_avail - commit_keep:
        problems.append({"code": "commit", "need": commit_need, "have": commit_avail, "keep": commit_keep})
    return {
        "values": v, "slot_context": ctx, "parallel": par,
        "vram": {"items": [_item("dense", dense), _item("drafter", drafter), _item("kv", kv),
                           _item("vision", vision_vram), _item("buffers", buffers),
                           _item("cuda", CUDA_CONTEXT, True), _item("experts", cache)],
                 "need": vram_need, "reserve": reserve, "available": vram_avail, "free_now": vram_free,
                 "total": system.get("vram_total"), "expert_slots": slots, "expert_slots_total": total_slots},
        "ram": {"items": host, "need": ram_need, "available": ram_avail, "free_now": avail, "total": total,
                "keep": ram_keep},
        "commit": {"need": commit_need, "available": commit_avail, "free_now": None if limit is None or used is None
                   else limit - used, "limit": limit, "used": used, "keep": commit_keep,
                   "ram_part": ram_need - ple_ram, "vram_part": vram_need},
        "engine_holds": {"ram": holds["ram"], "commit": holds["commit"], "vram": holds_vram},
        "unknown": [k for k, x in (("vram", vram_avail), ("ram", ram_avail), ("commit", commit_avail)) if x is None],
        "fits": not problems, "problems": problems,
        "kv_bytes_per_token": KV_TOKEN_BYTES[kvf] * par,
    }


def problem_text(p: dict, lang: str = "en") -> str:
    """A refusal in words, with the figures."""
    g = lambda n: f"{n / GiB:.1f}".replace(".", "," if lang == "it" else ".")   # noqa: E731
    if p["code"] == "vram":
        return (f"Non entra in VRAM: servono {g(p['need'])} GiB (riserva compresa), ne sono liberi {g(p['have'])}. "
                "Riduci il contesto, usa una cache KV più piccola (q4_0), abbassa la riserva o spegni il drafter."
                if lang == "it" else
                f"Does not fit the VRAM: {g(p['need'])} GiB needed (reserve included), {g(p['have'])} GiB free. "
                "Lower the context, use a smaller KV cache (q4_0), lower the reserve or turn the drafter off.")
    if p["code"] == "ple_ram":
        return (f"La tabella PLE in RAM non ci sta: servono {g(p['need'])} GiB di RAM, disponibili {g(p['have'])} "
                f"e {g(p['keep'])} devono restare liberi (15%). Usa PLE direct."
                if lang == "it" else
                f"The PLE table does not fit in RAM: {g(p['need'])} GiB needed, {g(p['have'])} GiB available and "
                f"{g(p['keep'])} GiB must stay free (15%). Use PLE direct.")
    if p["code"] == "ram":
        return (f"Manca RAM: servono {g(p['need'])} GiB, disponibili {g(p['have'])} e {g(p['keep'])} devono restare "
                "liberi (15%). Chiudi altri programmi pesanti, oppure usa meno contesto o meno slot."
                if lang == "it" else
                f"Not enough RAM: {g(p['need'])} GiB needed, {g(p['have'])} GiB available and {g(p['keep'])} GiB must "
                "stay free (15%). Close other heavy programs, or use less context or fewer slots.")
    return (f"Manca memoria impegnabile (commit): servono {g(p['need'])} GiB, ne restano {g(p['have'])} sotto il "
            f"limite e {g(p['keep'])} devono restare liberi (15%). Il file di paging non deve crescere: chiudi altri "
            "programmi (la VRAM usata conta nel commit)."
            if lang == "it" else
            f"Not enough commit: {g(p['need'])} GiB needed, {g(p['have'])} GiB left under the limit and "
            f"{g(p['keep'])} GiB must stay free (15%). The page file must not grow: close other programs (the VRAM "
            "in use counts toward the commit).")


CONTEXT_STEPS = (262144, 131072, 65536, 32768, 16384, 8192, 4096)
RESERVE_SHARE = 3 / 16                  # of the card's VRAM left free by the safe values (6 GiB of 32 GiB)
RESERVE_MIN_MIB, RESERVE_MAX_MIB = 1024, 8192
RESERVE_WARN_MIB = 1024                 # below this the desktop and the driver may run short of VRAM


def safe_reserve_mib(vram_total: int | None) -> int:
    """The VRAM the safe values leave free: 3/16 of the card, 1 to 8 GiB, in steps of 256 MiB (6 GiB when unknown)."""
    if not vram_total:
        return 6144
    mib = int(vram_total / MiB * RESERVE_SHARE) // 256 * 256
    return max(RESERVE_MIN_MIB, min(RESERVE_MAX_MIB, mib))


def safe(facts: dict, system: dict, holds: dict | None = None, holds_vram: int = 0, cpu: dict | None = None,
         why: dict | None = None) -> dict:
    """The safe values for this PC and this model, computed with the same rules as every plan: 15% of the RAM and of
    the commit limit stay free; the VRAM reserve is sized to the card (safe_reserve_mib); the KV cache is the
    smallest (q4_0) and the context the longest that fits; the drafter is on when the file has one; the experts are
    resident in RAM and the PLE table in RAM when they fit; the CPU workers follow the cores (hardware.pool_workers).
    `why`, when given, is filled with each value's reason {key: (English, Italian)}."""
    from serve import hardware
    g = lambda n: f"{n / GiB:.1f}"                                               # noqa: E731
    gi = lambda n: g(n).replace(".", ",")                                       # noqa: E731
    reasons = {} if why is None else why
    reserve = safe_reserve_mib(system.get("vram_total"))
    workers, w_en, w_it = hardware.pool_workers(cpu)
    mtp = bool(facts.get("has_mtp"))
    vision = "dynamic" if facts.get("mmproj_bytes") else "off"
    out = {"engine.max-context": CONTEXT_STEPS[-1], "engine.kv": "q4_0", "engine.prefill": "auto",
           "engine.spec": 7 if mtp else 0, "engine.spec-min-p": 0.5 if mtp else None, "engine.expert-cache": "auto",
           "engine.resident-experts": True, "engine.vram-reserve-mib": reserve, "engine.ple-io": "direct",
           "engine.pool-workers": workers, "server.parallel": 1, "server.slot-full-context": False,
           "server.vision": vision}
    fitted = False
    for resident in (True, False):
        for ctx in CONTEXT_STEPS:
            trial = {**out, "engine.max-context": ctx, "engine.resident-experts": resident}
            if plan(trial, facts, system, holds, holds_vram)["fits"]:
                out, fitted = trial, True
                break
        if fitted:
            break
    if facts.get("ple_bytes"):
        with_ram = plan({**out, "engine.ple-io": "ram"}, facts, system, holds, holds_vram)
        if with_ram["fits"] and "ram" not in with_ram["unknown"]:
            out["engine.ple-io"] = "ram"
    ctx = out["engine.max-context"]
    kv = KV_TOKEN_BYTES["q4_0"] * ctx
    total = system.get("vram_total")
    reasons.update({
        "engine.max-context": (
            f"{ctx:,} tokens: the longest context that fits this PC with the rest of these values "
            f"(KV cache {g(kv)} GiB in q4_0)." if ctx < CONTEXT_STEPS[0] else
            f"The model's full context; with q4_0 its KV cache is {g(kv)} GiB.",
            f"{ctx:,} token: il contesto più lungo che ci sta su questo PC con gli altri valori (cache KV "
            f"{gi(kv)} GiB in q4_0).".replace(",", ".", 1) if ctx < CONTEXT_STEPS[0] else
            f"Il contesto intero del modello; con q4_0 la cache KV è di {gi(kv)} GiB."),
    })
    if not fitted:
        reasons["engine.max-context"] = (
            f"{ctx:,} tokens, the shortest offered: with what is free now no context fits; close other programs "
            "(the plan says what is missing).",
            f"{ctx:,} token, il più corto: con quello che è libero ora non ci sta nessun contesto; chiudi altri "
            "programmi (la previsione dice cosa manca).".replace(",", ".", 1))
    reasons.update({
        "engine.kv": ("q4_0: the smallest KV cache, more VRAM left for experts.",
                      "q4_0: la cache KV più piccola, più VRAM per gli esperti."),
        "engine.prefill": ("auto: the prompt is read in large chunks that borrow expert-cache slots.",
                           "auto: il prompt si legge a blocchi grandi che prendono in prestito posti della cache esperti."),
        "engine.spec": (("7: the drafter proposes up to 6 tokens per step; the usual best speed." if mtp else
                         "0: this model file has no MTP drafter."),
                        ("7: il drafter propone fino a 6 token per passo; di solito la velocità migliore." if mtp else
                         "0: questo file non ha il drafter MTP.")),
        "engine.spec-min-p": (("0.5: drafting stops when the drafter is unsure, so fewer wasted checks." if mtp else
                               "Not used without a drafter."),
                              ("0.5: il drafter si ferma quando è incerto, meno verifiche sprecate." if mtp else
                               "Non serve senza drafter.")),
        "engine.expert-cache": ("auto: every MiB of VRAM left after the reserve holds experts.",
                                "auto: tutta la VRAM che resta oltre la riserva contiene esperti."),
        "engine.resident-experts": (
            ("on: the experts outside the GPU cache are copied to RAM once, not read from disk."
             if out["engine.resident-experts"] else
             "off: a resident copy of the experts would not leave 15% of the RAM and of the commit free; they are "
             "read from the file."),
            ("attivo: gli esperti fuori dalla cache GPU sono copiati in RAM una volta, non letti dal disco."
             if out["engine.resident-experts"] else
             "spento: una copia residente degli esperti non lascerebbe liberi il 15% della RAM e del commit; si "
             "leggono dal file.")),
        "engine.vram-reserve-mib": (
            f"{g(reserve * MiB)} GiB stay free on the GPU ({'3/16 of its ' + g(total) + ' GiB' if total else 'the GPU size is unknown'}"
            ", 1 to 8 GiB) for the desktop, the browser and other programs.",
            f"{gi(reserve * MiB)} GiB restano liberi sulla GPU ({'3/16 dei suoi ' + gi(total) + ' GiB' if total else 'dimensione della GPU sconosciuta'}"
            ", da 1 a 8 GiB) per il desktop, il browser e gli altri programmi."),
        "engine.ple-io": (("ram: the PLE table fits in RAM with 15% of RAM and of the commit free." if
                           out["engine.ple-io"] == "ram" else
                           "direct: rows are read from disk (the table in RAM would not leave 15% free)." if
                           facts.get("ple_bytes") else "direct: this model has no PLE table."),
                          ("ram: la tabella PLE ci sta in RAM lasciando liberi il 15% della RAM e del commit." if
                           out["engine.ple-io"] == "ram" else
                           "direct: le righe si leggono dal disco (in RAM non resterebbe libero il 15%)." if
                           facts.get("ple_bytes") else "direct: questo modello non ha la tabella PLE.")),
        "engine.pool-workers": (w_en, w_it),
        "server.parallel": ("1: one request at a time gets the whole context and the full speed.",
                            "1: una richiesta alla volta ha tutto il contesto e tutta la velocità."),
        "server.slot-full-context": ("off: with one slot it changes nothing.", "spento: con un solo slot non cambia nulla."),
        "server.vision": (("dynamic: images are read inside the engine, no VRAM kept for them." if vision == "dynamic"
                           else "off: this model file has no vision projector."),
                          ("dynamic: le immagini si leggono dentro il motore, nessuna VRAM fissa." if vision == "dynamic"
                           else "off: questo file non ha il proiettore visivo.")),
    })
    return out


def reserve_warnings(values: dict, gpu: dict | None) -> list[dict]:
    """A VRAM reserve too small for the desktop and the driver (more when other programs use the GPU)."""
    v = effective(values)
    if v["engine.expert-cache"] not in ("auto", 0, "0", None):
        return []
    reserve = int(v["engine.vram-reserve-mib"])
    others = [p for p in (gpu or {}).get("processes") or []
              if (p.get("name") or "").lower() not in ("basalt.exe", "basalt", "python.exe", "python")]
    want = RESERVE_WARN_MIB + (1024 if others else 0)
    if reserve >= want:
        return []
    names = ", ".join(sorted({p.get("name") or str(p.get("pid")) for p in others}))
    g = lambda n: f"{n / 1024:.1f}"                                              # noqa: E731
    return [{"code": "reserve_low", "reserve_mib": reserve, "want_mib": want, "processes": others,
             "text": f"Only {g(reserve)} GB of VRAM stay free: the desktop and the driver need about {g(want)} GB"
                     + (f" (other programs on the GPU: {names})" if others else "") + ". Raise it to avoid stutters "
                     "or an engine that cannot allocate.",
             "text_it": f"Restano liberi solo {g(reserve).replace('.', ',')} GB di VRAM: desktop e driver ne vogliono "
                        f"circa {g(want).replace('.', ',')}"
                        + (f" (altri programmi sulla GPU: {names})" if others else "")
                        + ". Alzala per evitare scatti o un motore che non riesce ad allocare."}]


# ------------------------------------------------------------------------------------------------ speed
SPEED_KEYS = ("engine.kv", "engine.max-context", "engine.prefill", "engine.spec", "engine.spec-min-p",
              "engine.expert-cache", "engine.resident-experts", "engine.vram-reserve-mib", "engine.ple-io",
              "engine.pool-workers", "server.parallel", "server.slot-full-context", "server.vision")


def fingerprint(values: dict) -> str:
    v = effective(values)
    return hashlib.sha256(json.dumps({k: v.get(k) for k in SPEED_KEYS}, sort_keys=True).encode()).hexdigest()[:16]


def _drafts(spec: int) -> int:
    """Tokens the drafter proposes per verify step (a window of 0 or 1 is no speculation)."""
    return max(0, int(spec) - 1) if int(spec) >= 2 else 0


def spec_factor(spec: int, accept: float) -> float:
    """Tokens per unit of time against plain decoding: E(accepted + 1) / step cost, with each draft accepted with
    probability `accept` while the ones before it were (geometric), and a step costing 1 + SPEC_COST per draft."""
    d = _drafts(spec)
    if d == 0:
        return 1.0
    a = min(0.99, max(0.01, accept))
    return (1 - a ** (d + 1)) / (1 - a) / (1 + SPEC_COST * d)


def accept_for(ratio: float, spec: int) -> float:
    """The per-draft acceptance that gives the measured speedup `ratio` at window `spec` (bisection)."""
    lo, hi = 0.01, 0.99
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if spec_factor(spec, mid) < ratio else (lo, mid)
    return (lo + hi) / 2


def hit_rate(cache_share: float, ref_share: float, ref_hit: float) -> float:
    """The expert-cache hit rate for a cache holding `cache_share` of the experts: h = share^alpha, alpha fitted to
    the reference point. Routing is skewed, so a small cache already catches most lookups."""
    if cache_share <= 0:
        return 0.0
    if cache_share >= 1:
        return 1.0
    ref_share = min(0.999, max(0.001, ref_share))
    alpha = math.log(max(1e-6, min(0.999999, ref_hit))) / math.log(ref_share)
    return cache_share ** alpha


def prefill_gain(chunk: int) -> float:
    return chunk / (chunk + PREFILL_HALF)


def predict_speed(the_plan: dict, calibration: dict | None = None) -> dict:
    """Predicted tok/s for a plan. The model, all from one reference point (a calibration of this model, else SEED):

      decode   = base(ref) x (1 + MISS_COST (1 - h_ref)) / (1 + MISS_COST (1 - h)) x spec_factor(spec, a) / spec_factor(spec_ref, a)
      h        = expert-cache hit rate from the share of the experts the plan's cache holds (hit_rate())
      a        = the drafts' acceptance, measured (calibration) or fitted to the seed's spec 0 / spec 7 speeds
      reasoning: the same decode rate; the visible share of the tokens is the calibration's (unknown before)
      prefill  = prefill(ref) x prefill_gain(chunk) / prefill_gain(chunk_ref)

    Parallel slots share the GPU: the figures are for one request alone."""
    v = the_plan["values"]
    share = the_plan["vram"]["expert_slots"] / max(1, the_plan["vram"]["expert_slots_total"])
    c = calibration or {}
    ref = c if c.get("decode_off") else None
    if ref:
        ref_hit = ref.get("hit_rate") or SEED["hit_rate"]
        ref_share = ref.get("cache_share") or SEED["cache_share"]
        ref_spec = int(ref.get("spec") or 0)
        accept = ref.get("accept") or accept_for(SEED["decode_spec7"] / SEED["decode_spec0"], 7)
        base = ref["decode_off"] / spec_factor(ref_spec, accept)
        pre_ref, chunk_ref = ref.get("prefill") or SEED["prefill"], ref.get("prefill_chunk") or SEED["prefill_chunk"]
        source = {"kind": "calibration", "at": ref.get("at"), "exact": ref.get("fingerprint") == fingerprint(v)}
    else:
        ref_hit, ref_share = SEED["hit_rate"], SEED["cache_share"]
        accept = accept_for(SEED["decode_spec7"] / SEED["decode_spec0"], 7)
        base = SEED["decode_spec0"]
        pre_ref, chunk_ref = SEED["prefill"], SEED["prefill_chunk"]
        source = {"kind": "seed", "at": None, "exact": False, "note": SEED["note"]}
    h = hit_rate(share, ref_share, ref_hit)
    miss = (1 + MISS_COST * (1 - ref_hit)) / (1 + MISS_COST * (1 - h))
    spec = int(v["engine.spec"] or 0)
    decode = base * miss * spec_factor(spec, accept)
    if ref and source["exact"]:
        decode = ref["decode_off"]                     # measured with these very settings
    chunk = prefill_chunk(v)
    prefill = pre_ref * prefill_gain(chunk) / prefill_gain(chunk_ref)
    if ref and source["exact"] and ref.get("prefill"):
        prefill = ref["prefill"]
    vis = c.get("visible_share") if ref else None
    on_total = (ref.get("decode_on") / ref["decode_off"] * decode) if ref and ref.get("decode_on") else decode
    return {"decode": round(decode, 1), "decode_reasoning": round(on_total, 1),
            "decode_reasoning_visible": round(on_total * vis, 1) if vis else None, "visible_share": vis,
            "prefill": round(prefill, 0), "prefill_chunk": chunk, "hit_rate": round(h, 4), "accept": round(accept, 3),
            "spec": spec, "source": source}


# ------------------------------------------------------------------------------------------------ calibration file
def calibration_path(facts: dict) -> Path:
    from serve import userdata
    return userdata.calibration_file(facts.get("meta_hash") or "mock")


def read_calibrations(facts: dict) -> dict:
    try:
        data = json.loads(calibration_path(facts).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("runs"), dict) else {"runs": {}}
    except (OSError, ValueError):
        return {"runs": {}}


def best_calibration(facts: dict, values: dict) -> dict | None:
    """The calibration of exactly these settings, else this model's newest one, else None."""
    runs = read_calibrations(facts)["runs"]
    fp = fingerprint(values)
    if fp in runs:
        return runs[fp]
    return max(runs.values(), key=lambda r: r.get("at") or "", default=None)


def save_calibration(facts: dict, record: dict) -> Path:
    path = calibration_path(facts)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_calibrations(facts)
    data["model"] = facts.get("name")
    data["runs"][record["fingerprint"]] = record
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="calibration.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def main(argv=None) -> int:
    """python -m serve.loader check --config FILE: the plan of the config file's settings against what is free now,
    in Italian lines and a last JSON line; exit 0 when it fits, 3 when it does not, 4 when the GPU cannot run the
    engine (not Blackwell), 2 for a bad file."""
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from serve import settings
    ap = argparse.ArgumentParser(prog="python -m serve.loader")
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--gpu", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        raw = settings.read_file(a.config)
        values = settings.values_of(raw, settings.LOADER_FIELDS)
        model = values.get("server.model")
        if model and not os.path.isabs(model):
            model = str(Path(a.config).resolve().parent / model)
        facts = model_facts(model)
    except (OSError, ValueError, KeyError) as e:
        print(f"configurazione non leggibile: {e}", file=sys.stderr)
        return 2
    values = {k: v for k, v in values.items() if k != "server.model"}
    p = plan(values, facts, system_now(a.gpu))
    s = predict_speed(p)
    g = lambda n: "—" if n is None else f"{n / GiB:.1f}".replace(".", ",")   # noqa: E731
    dots = lambda n: f"{n:,}".replace(",", ".")                                # noqa: E731
    v, r, c = p["vram"], p["ram"], p["commit"]
    from serve import hardware
    gpu = hardware.gpu(a.gpu)
    if gpu.get("name"):
        cc = ".".join(str(x) for x in gpu["cc"]) if gpu.get("cc") else "?"
        print(f"  GPU:    {gpu['name']} (compute capability {cc})")
    cache = next((i["bytes"] for i in v["items"] if i["key"] == "experts"), 0)
    share = v["expert_slots"] / max(1, v["expert_slots_total"]) * 100
    print(f"  VRAM:   servono {g(v['need'] + v['reserve'])} GiB (riserva compresa), liberi {g(v['available'])} GiB; "
          f"{g(v['reserve'])} GiB lasciati liberi, {g(cache)} GiB per gli esperti: {dots(v['expert_slots'])} di "
          f"{dots(v['expert_slots_total'])} esperti in VRAM ({share:.0f}%)")
    print(f"  RAM:    servono {g(r['need'])} GiB, disponibili {g(r['available'])} GiB, {g(r['keep'])} restano liberi (15%)")
    print(f"  commit: servono {g(c['need'])} GiB (RAM {g(c['ram_part'])} + VRAM {g(c['vram_part'])}), liberi "
          f"{g(c['available'])} GiB, {g(c['keep'])} restano liberi (15%)")
    hit = f"{s['hit_rate'] * 100:.1f}".replace(".", ",")
    print(f"  velocità stimata: {s['decode']:.0f} tok/s in risposta, {s['prefill']:.0f} tok/s in lettura, {hit}% colpi "
          f"nella cache esperti ({'calibrata' if s['source']['kind'] == 'calibration' else 'stima iniziale'})")
    for x in reserve_warnings(values, gpu):
        print("  Attenzione: " + x["text_it"])
    for x in p["problems"]:
        print("  " + problem_text(x, "it"))
    warn = hardware.gpu_warning(gpu, "it")
    unsupported = gpu.get("blackwell") is False
    if warn:
        print(("  GPU NON SUPPORTATA: " if unsupported else "  Attenzione: ") + warn)
    print(json.dumps({"fits": p["fits"], "problems": [x["code"] for x in p["problems"]],
                      "port": p["values"].get("server.port"), "gpu_supported": not unsupported,
                      "expert_slots": v["expert_slots"], "expert_slots_total": v["expert_slots_total"],
                      "decode": s["decode"], "prefill": s["prefill"], "hit_rate": s["hit_rate"],
                      "commit_need": c["need"], "reserve_mib": int(v["reserve"] // MiB)}))
    if unsupported:
        return 4
    return 0 if p["fits"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
