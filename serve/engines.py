"""The inference engines the Loader can drive, which hardware each runs on, and which Models tabs a PC shows.

  ENGINES        the registry: id, label, binary dir, supported hardware families, model families, capability flags
  TABS           the Models sub-tabs backed by an engine (the first is the existing Qwen Loader)
  visible_engines(hw), visible_tabs(hw)   what this PC shows (hw = hardware.machine()); a newer NVIDIA architecture
                 also shows the tabs of the older ones, an AMD / Intel / Apple / unified-memory tab only its own hardware
  listing(hw)    the body of GET /api/loader/engines
  memory_pools(hw) / memory_pool(want, hw)   the memory pool modes: "split" (VRAM and RAM tiers) always, "single" (one
                 pool shared by CPU and GPU) only on a machine known to have unified memory
  FAMILIES       the model families Strata reads: its binary, API model name, capability flags and Loader controls
  controls(id, family)  the Loader controls an engine (and model family) supports; the page shows only those
  check_values(id, values, family)   a Strata Loader request's values, each checked
  install(id, ...)  copy an engine from a local folder or archive (serve/engine_install.py); a release download is
                 not available yet

A capability flag is True (the Loader control applies), False, or None (the engine does not report it yet)."""
from __future__ import annotations

import os

from serve import ROOT, hardware

NVIDIA_NEWER_SEES = {          # a card also shows the tabs of these architectures
    "nvidia_blackwell": ("nvidia_blackwell", "nvidia_ada", "nvidia_ampere", "nvidia_turing"),
    "nvidia_ada": ("nvidia_ada", "nvidia_ampere", "nvidia_turing"),
    "nvidia_ampere": ("nvidia_ampere", "nvidia_turing"),
    "nvidia_turing": ("nvidia_turing",),
}
CAPABILITIES = ("reserve_slider", "context_kv", "speed_spec", "expert_cache", "vision", "profiles", "calibrate")
POOL_SPLIT, POOL_SINGLE = "split", "single"

ENGINES: dict[str, dict] = {
    "basalt": {
        "id": "basalt", "label": "Basalt", "dir": "", "binary": "basalt",
        "families": ("nvidia_blackwell",), "sm_majors": hardware.BLACKWELL_MAJORS,
        "models": ("qwen4exp",),
        "capabilities": dict.fromkeys(CAPABILITIES, True),
    },
    "strata": {
        "id": "strata", "label": "Strata", "dir": "strata", "binary": "strata",
        "families": ("nvidia_turing", "nvidia_ampere", "nvidia_ada", "nvidia_blackwell", "amd", "intel", "apple"),
        "sm": ("7.5", "8.6", "8.9", "12.0"),
        "models": ("qwen4exp", "qwen35moe", "deepseek41"),
        "capabilities": {},          # filled below from FAMILIES
    },
}

# Strata's model families: the binary that reads them, the name its API answers to, what the binary supports (checked
# against the flags of the shipped binaries) and the Loader controls that map to a flag of that binary. Pictures are
# set in the model's own run config (the projector and its reader), so there is no Loader control for them.
FAMILIES: dict[str, dict] = {
    "qwen4exp": {"exe": "strata", "model_name": "qwen3.8-flash-next",
                 "capabilities": {"reserve_slider": True, "context_kv": True, "speed_spec": True, "expert_cache": True,
                                  "vision": True, "profiles": True, "calibrate": False},
                 "controls": ("max_context", "kv", "spec", "expert_cache", "vram_reserve", "parallel")},
    "qwen35moe": {"exe": "strata-q36", "model_name": "qwen3.6-35b-a3b",
                  "capabilities": {"reserve_slider": False, "context_kv": True, "speed_spec": True,
                                   "expert_cache": False, "vision": True, "profiles": True, "calibrate": False},
                  "controls": ("max_context", "mtp", "parallel")},
    "deepseek41": {"exe": "strata-dsv41", "model_name": "deepseek-v4.1-flash",
                   "capabilities": {"reserve_slider": True, "context_kv": True, "speed_spec": False,
                                    "expert_cache": True, "vision": True, "profiles": True, "calibrate": False},
                   "controls": ("max_context", "expert_cache", "vram_reserve", "parallel")},
}
ENGINES["strata"]["capabilities"] = {c: any(f["capabilities"][c] for f in FAMILIES.values()) for c in CAPABILITIES}
TADAMOO_CONTROLS = ("max_context", "kv", "spec", "expert_cache", "vram_reserve", "parallel")

TABS: list[dict] = [
    {"id": "qwen", "engine": "basalt", "label": "Qwen Loader", "builtin": True, "shows_on": ("nvidia_blackwell",)},
    {"id": "rtx3040", "engine": "strata", "label": "RTX 30/40", "builtin": False,
     "shows_on": ("nvidia_turing", "nvidia_ampere", "nvidia_ada")},
    {"id": "mac", "engine": "strata", "label": "Mac", "builtin": False, "shows_on": ("apple",)},
    {"id": "amd", "engine": "strata", "label": "AMD", "builtin": False, "shows_on": ("amd",)},
    {"id": "intel", "engine": "strata", "label": "Intel", "builtin": False, "shows_on": ("intel",)},
    {"id": "unified", "engine": "strata", "label": "Unified memory", "builtin": False,
     "shows_on": ("unified_memory",)},
]


def installed(engine_id: str) -> bool:
    """The engine's binary is in its bin/ folder."""
    e = ENGINES[engine_id]
    exe = e["binary"] + (".exe" if os.name == "nt" else "")
    return (ROOT / "bin" / e["dir"] / exe).is_file()


def compatible(engine_id: str, hw: dict) -> tuple[bool, str | None, str | None]:
    """(ok, reason code, reason text) of the engine on this PC; the reason is None when it runs here."""
    e = ENGINES[engine_id]
    gpus = [g for g in hw.get("gpus") or [] if g.get("family")]
    if not gpus:
        return False, "no_gpu", "no supported GPU was detected"
    mine = [g for g in gpus if g["family"] in e["families"]]
    if not mine:
        return False, "family", f"{e['label']} does not run on this GPU family"
    if "sm" in e and not any(g["family"] not in hardware.NVIDIA_FAMILIES or g.get("sm") in e["sm"] for g in mine):
        return False, "sm", f"{e['label']} is built for compute capability {', '.join(e['sm'])}"
    if "sm_majors" in e and not any(g.get("cc") and int(g["cc"][0]) in e["sm_majors"] for g in mine):
        return False, "sm", f"{e['label']} is built for compute capability {', '.join(map(str, e['sm_majors']))}.x"
    return True, None, None


def seen_families(hw: dict) -> set[str]:
    """The families whose tabs this PC shows: its own, and the older NVIDIA architectures below its NVIDIA ones."""
    out: set[str] = set()
    for f in hw.get("families") or []:
        out.update(NVIDIA_NEWER_SEES.get(f, (f,)))
    return out


def tab_visible(tab: dict, hw: dict) -> bool:
    return bool(tab["builtin"]) or bool(seen_families(hw) & set(tab["shows_on"]))


def visible_tabs(hw: dict, show_all: bool = False) -> list[dict]:
    """The Models sub-tabs for this PC: id, engine, label, builtin, installed, compatible, reason, visible. The
    built-in Qwen Loader is always listed; the others only where the hardware has their family, or all of them
    (marked incompatible where they do not fit) with `show_all`."""
    out = []
    for t in TABS:
        seen = tab_visible(t, hw)
        if not (seen or show_all):
            continue
        ok, code, text = compatible(t["engine"], hw)
        if ok and not seen:
            ok, code, text = False, "hardware", "this PC has no hardware for this tab"
        out.append({"id": t["id"], "engine": t["engine"], "label": t["label"], "builtin": t["builtin"],
                    "shows_on": list(t["shows_on"]), "installed": installed(t["engine"]), "compatible": ok,
                    "reason_code": code, "reason": text})
    return out


def visible_engines(hw: dict, show_all: bool = False) -> list[str]:
    """The ids of the engines that have a tab on this PC."""
    ids = {t["engine"] for t in visible_tabs(hw, show_all)}
    return [i for i in ENGINES if i in ids]


def memory_pools(hw: dict) -> list[str]:
    """The memory pool modes this PC may use: "split" always, "single" only with unified memory (known, not guessed)."""
    return [POOL_SPLIT, POOL_SINGLE] if hw.get("unified_memory") is True else [POOL_SPLIT]


def memory_pool(want: str | None, hw: dict) -> str:
    """The memory pool mode to use for the request `want` (None: "split"); ValueError when "single" is asked on a PC
    without known unified memory or the mode is unknown."""
    want = want or POOL_SPLIT
    if want not in (POOL_SPLIT, POOL_SINGLE):
        raise ValueError(f"unknown memory pool mode {want!r}")
    if want not in memory_pools(hw):
        raise ValueError("the single memory pool is offered only on machines with unified memory")
    return want


def listing(hw: dict | None = None, show_all: bool = False, host: dict | None = None) -> dict:
    """GET /api/loader/engines: every engine with whether it is installed, runs here and is running, the tabs to
    show, the PC's adapters and families, and the memory pool modes. `host`: serve/engine_host.py's status."""
    from serve import engine_install
    hw = hardware.machine() if hw is None else hw
    host = host or {"engine": None, "phase": "idle"}
    shown = set(visible_engines(hw, show_all))
    sources = engine_install.sources()
    engines = []
    for e in ENGINES.values():
        ok, code, text = compatible(e["id"], hw)
        have = installed(e["id"])
        running = host["engine"] == e["id"]
        # Basalt is running when no other engine is: its own supervisor owns that state
        state = "not_installed" if not have else (host["phase"] if running else "stopped")
        item = {"id": e["id"], "label": e["label"], "installed": have, "compatible": ok,
                "reason_code": code, "reason": text, "families": list(e["families"]),
                "models": list(e["models"]), "capabilities": dict(e["capabilities"]),
                "controls": list(controls(e["id"])), "visible": e["id"] in shown, "state": state,
                "active": running, "source": sources.get(e["id"]), "install": engine_install.job(e["id"])}
        if e["id"] == "strata":
            item["model_families"] = {k: {"capabilities": dict(f["capabilities"]), "controls": list(f["controls"]),
                                          "model_name": f["model_name"]} for k, f in FAMILIES.items()}
            item["error"] = host.get("error") if running else None
        engines.append(item)
    return {"engines": engines, "tabs": visible_tabs(hw, show_all),
            "hardware": {k: hw.get(k) for k in ("gpus", "families", "unified_memory", "os", "arch")},
            "memory_pools": memory_pools(hw), "memory_pool": POOL_SPLIT, "active": host["engine"]}


def controls(engine_id: str, family: str | None = None) -> tuple[str, ...]:
    """The Loader controls this engine supports: Basalt's all; Strata's those of the model family (all families'
    when none is given)."""
    if engine_id == "basalt":
        return TADAMOO_CONTROLS
    if family:
        return FAMILIES[family]["controls"]
    return tuple(dict.fromkeys(c for f in FAMILIES.values() for c in f["controls"]))


def family_capabilities(family: str) -> dict:
    return dict(FAMILIES[family]["capabilities"])


def check_values(engine_id: str, values, family: str | None = None) -> dict:
    """A Strata request's Loader values: only controls the engine supports (ValueError names the others), each a
    whole number, a choice or text as the control needs."""
    if engine_id != "strata":
        raise ValueError(f"{engine_id}: its values are the Loader's own fields")
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("values: an object of Loader controls")
    allowed = controls(engine_id, family)
    out: dict = {}
    for name, value in values.items():
        if name not in allowed and name != "tokenizer":
            raise ValueError(f"{name}: not a control {ENGINES[engine_id]['label']} supports"
                             + (f" for the {family} family" if family else ""))
        if value is None or value == "":
            continue
        if name == "kv":
            if value not in KV_CHOICES:
                raise ValueError(f"kv: one of {', '.join(KV_CHOICES)}")
        elif name == "tokenizer":
            if not isinstance(value, str):
                raise ValueError("tokenizer: a folder path")
        elif name == "expert_cache":
            if value is False or value == "off":
                value = 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("expert_cache: a whole number of expert slots (0 = off)")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0 or (name == "max_context" and value < 1):
            raise ValueError(f"{name}: a whole number")
        out[name] = value
    return out


KV_CHOICES = ("fp16", "int8", "q4_0", "k8v4")


def install(engine_id: str, source: str | None = None, remember: bool = False) -> tuple[int, dict]:
    """(HTTP status, body) of an install request: see serve/engine_install.py."""
    from serve import engine_install
    return engine_install.start(engine_id, source, remember)
