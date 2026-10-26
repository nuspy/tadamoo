"""What was measured about each model, kept between runs, and the figures the Model converter page compares.

A model is known by its sections' hashes (`fingerprints`): the whole file for what speed and memory depend on (the profile and the drafter
layer included), the weights alone for what quality depends on. A result is stored with the settings it was measured under, so it is
offered for reuse only to a request with the same settings; a result under other settings stays and is listed. Published figures for
the official packs are kept apart and never mixed with measured ones.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from serve import userdata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

GiB = 1 << 30
SCHEMA = 1
TEST_VERSION = 1                          # changes when a test's prompts or scoring change: older results are not reused
STEPS = ("memory", "speed", "image", "quality")
RELAX_DEFAULT = (0.0, 0.5, 0.3, 0.2)      # 0 = exact sampling
KEEP = 5                                  # results kept for each step and model

# the settings a result depends on, by step
ENGINE_KEYS = ("max-context", "kv", "spec", "spec-min-p", "prefill", "resident-experts", "vram-reserve-mib", "ple-io", "pool-workers", "parallel")
VISION_KEYS = ("vision", "vision-hold", "vision-projector")
STEP_KEYS = {"memory": ENGINE_KEYS + VISION_KEYS, "speed": ENGINE_KEYS + ("relax", "reps", "power"), "image": VISION_KEYS + ("max-context",),
             "quality": ("kv", "n", "max-context")}

_lock = threading.RLock()


# ------------------------------------------------------------------------------------------------ identity
def fingerprints(path: str) -> Optional[Dict[str, Any]]:
    """{"fp", "weights_fp", "size_bytes", "name"} of a .basalt file from the hashes in its section table; None when it cannot be read."""
    from model_file import ModelFile
    try:
        mf = ModelFile(str(path))
    except (OSError, ValueError):
        return None
    dig = lambda n: mf.sections[n][2].hex() if n in mf.sections else ""      # noqa: E731
    weights = hashlib.sha256("|".join(dig(n) for n in ("dense", "experts")).encode()).hexdigest()[:16]
    whole = hashlib.sha256("|".join([weights] + [dig(n) for n in ("meta", "profile", "mtp/dense", "mtp/experts", "vision/mmproj")]).encode()).hexdigest()[:16]
    return {"fp": whole, "weights_fp": weights, "size_bytes": os.path.getsize(path), "name": Path(path).stem}


def step_key(step: str, settings: Dict[str, Any]) -> str:
    """The key a step's result is stored and found under: the settings it depends on, and the version of the test."""
    s = dict(settings)
    if "relax" in s:
        s["relax"] = [round(float(x), 3) for x in s["relax"]]
    if "power" in s and s["power"] is not None:
        s["power"] = round(float(s["power"]) / 50) * 50           # the power limit within 50 W is the same condition
    keep = {k: s.get(k) for k in STEP_KEYS[step]}
    return hashlib.sha256(json.dumps([TEST_VERSION, step, keep], sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ the store
def store_dir() -> Path:
    p = userdata.path("bench")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file(fp: str) -> Path:
    return store_dir() / f"{fp}.json"


def read(fp: str) -> Dict[str, Any]:
    try:
        data = json.loads(_file(fp).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}
    except (OSError, ValueError):
        return {}


def _write(fp: str, data: Dict[str, Any]) -> None:
    path = _file(fp)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bench-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def record(ident: Dict[str, Any], model: Dict[str, Any], step: str, key: str, settings: Dict[str, Any], data: Dict[str, Any],
           power_w: Optional[float] = None, at: Optional[float] = None) -> None:
    """Keeps one step's result for a model (`ident` from fingerprints); the last KEEP per step stay."""
    if step not in STEPS:
        raise ValueError(f"no such test: {step}")
    with _lock:
        entry = read(ident["fp"]) or {"schema": SCHEMA, "fp": ident["fp"], "weights_fp": ident["weights_fp"], "results": {}}
        entry["model"] = {k: model.get(k) for k in ("name", "path", "quant", "size_bytes", "experts_bytes", "mtp", "vision") if k in model}
        rows = entry["results"].setdefault(step, [])
        rows[:] = [r for r in rows if r.get("key") != key]
        rows.append({"key": key, "at": at if at is not None else time.time(), "power_w": power_w, "settings": settings, "data": data})
        rows.sort(key=lambda r: r["at"], reverse=True)
        del rows[KEEP:]
        _write(ident["fp"], entry)


def all_entries() -> List[Dict[str, Any]]:
    out = []
    for p in store_dir().glob("*.json"):
        e = read(p.stem)
        if e:
            out.append(e)
    return out


def find(ident: Dict[str, Any], step: str, key: str) -> Optional[Dict[str, Any]]:
    """The stored result of `step` under exactly these settings for this file; the quality test is shared by every file with the same weights."""
    with _lock:
        if step == "quality":
            hits = [r for e in all_entries() if e.get("weights_fp") == ident["weights_fp"] for r in e["results"].get(step, []) if r["key"] == key]
        else:
            hits = [r for r in read(ident["fp"]).get("results", {}).get(step, []) if r["key"] == key]
    return max(hits, key=lambda r: r["at"]) if hits else None


def availability(ident: Dict[str, Any], settings: Dict[str, Any], steps=STEPS) -> Dict[str, Optional[Dict[str, Any]]]:
    """For each step, the result that would be reused ({"at", "power_w"}) or None: what the consent dialog shows."""
    out = {}
    for step in steps:
        hit = find(ident, step, step_key(step, settings))
        out[step] = {"at": hit["at"], "power_w": hit.get("power_w")} if hit else None
    return out


# ------------------------------------------------------------------------------------------------ the figures
def _latest(entry: Dict[str, Any], step: str) -> Optional[Dict[str, Any]]:
    rows = entry.get("results", {}).get(step) or []
    return rows[0] if rows else None


def _mean(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 1) if values else None


def figures(entry: Dict[str, Any], others: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The comparable numbers of one stored model: {"metric id": value or None}. `others`: entries with the same weights, searched
    for a quality result this file has none of."""
    out: Dict[str, Any] = {}
    model = entry.get("model") or {}
    if model.get("size_bytes"):
        out["size_gib"] = round(model["size_bytes"] / GiB, 1)
    if model.get("experts_bytes"):
        out["experts_gib"] = round(model["experts_bytes"] / GiB, 1)
    mem = _latest(entry, "memory")
    if mem:
        d = mem["data"]
        rows = {r.get("tag"): r for r in d.get("mem", [])}
        before, loaded = rows.get("before"), rows.get("loaded")
        if before and loaded:
            out["ram_gib"] = round(loaded["ram_used_gib"] - before["ram_used_gib"], 1)
            out["commit_gib"] = round(loaded["commit_used_gib"] - before["commit_used_gib"], 1)
            out["vram_gib"] = round((loaded["vram_used_mib"] - before["vram_used_mib"]) / 1024, 1)
        if d.get("load_seconds") is not None:
            out["load_s"] = d["load_seconds"]
    spd = _latest(entry, "speed")
    if spd:
        rows = spd["data"].get("rows", [])
        by: Dict[float, List[float]] = {}
        for r in rows:
            by.setdefault(float(r.get("relax") or 0), []).append(r.get("decode_tok_s"))
        exact = _mean(by.get(0.0, []))
        out["decode"] = exact
        for p, name in ((0.5, "decode_r05"), (0.3, "decode_r03"), (0.2, "decode_r02")):
            out[name] = _mean(by.get(p, []))
        gains = [v for v in (out.get("decode_r05"), out.get("decode_r03"), out.get("decode_r02")) if v and exact]
        out["relax_gain_pct"] = round((max(gains) / exact - 1) * 100, 1) if gains and exact else None
        out["ttft_s"] = _mean([r.get("ttft_s") for r in rows if not r.get("relax")])
        if spd["data"].get("prefill_tok_s") is not None:
            out["prefill"] = spd["data"]["prefill_tok_s"]
        if spd["data"].get("decode_off") is not None:
            out["decode_off"] = spd["data"]["decode_off"]
        out["power_w"] = spd.get("power_w")
    img = _latest(entry, "image")
    if img:
        tries = img["data"].get("tries", [])
        out["image_first_s"] = tries[0]["seconds"] if tries else None
        out["image_ok"] = bool(tries and tries[0].get("text"))
    q = _latest(entry, "quality")
    if q is None and others:
        for o in others:
            if o.get("weights_fp") == entry.get("weights_fp"):
                q = _latest(o, "quality") or q
    if q and q["data"].get("gsm8k"):
        g = q["data"]["gsm8k"]
        out["gsm8k_pct"] = round(100 * g["acc"], 1)
        out["gsm8k_n"] = g.get("n")
    return out


# the metrics the page lists, in groups; `better` says which way is good (the radar turns every axis so that outward is good)
METRICS = [
    {"id": "size_gib", "group": "weight", "unit": "GiB", "better": "lower"},
    {"id": "ram_gib", "group": "weight", "unit": "GiB", "better": "lower"},
    {"id": "commit_gib", "group": "weight", "unit": "GiB", "better": "lower"},
    {"id": "vram_gib", "group": "weight", "unit": "GiB", "better": "lower"},
    {"id": "load_s", "group": "weight", "unit": "s", "better": "lower"},
    {"id": "decode", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "decode_off", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "decode_r05", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "decode_r03", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "decode_r02", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "relax_gain_pct", "group": "speed", "unit": "%", "better": "higher"},
    {"id": "prefill", "group": "speed", "unit": "tok/s", "better": "higher"},
    {"id": "ttft_s", "group": "speed", "unit": "s", "better": "lower"},
    {"id": "image_first_s", "group": "speed", "unit": "s", "better": "lower"},
    {"id": "gsm8k_pct", "group": "quality", "unit": "%", "better": "higher"},
    {"id": "ppl", "group": "quality", "unit": "", "better": "lower"},
    {"id": "kld", "group": "quality", "unit": "", "better": "lower"},
    {"id": "top1_pct", "group": "quality", "unit": "%", "better": "higher"},
]

# Published figures of the official packs (README, `basalt-bench --quality` against a Q8_0 reference; speed from docs/precision.md at 400 W).
# They are shown as references, with their source, and are never mixed into a measured number.
PUBLISHED_SOURCE = "Basalt README and docs/precision.md"
PUBLISHED = [
    {"id": "pub-iq1_m-coder", "name": "GSQ-RCO Coder IQ1_M", "quant": "IQ1_M", "size_gib": 57.4, "experts_gib": 23.4, "kld": 1.03, "top1_pct": 69.0},
    {"id": "pub-q2_0", "name": "GSQ-RCO Q2_0", "quant": "Q2_0", "size_gib": 64.9, "experts_gib": 31.6, "ppl": 2.971, "kld": 0.486, "top1_pct": 80.6},
    {"id": "pub-gsq-iq3_xxs", "name": "GSQ-RCO IQ3_XXS", "quant": "IQ3_XXS", "size_gib": 74.4, "experts_gib": 40.0, "ppl": 2.535, "kld": 0.277,
     "top1_pct": 86.8, "decode_off": 183.9, "decode": 319.8, "power_w": 400},
    {"id": "pub-ud-q2_k_xl", "name": "UD-Q2_K_XL", "quant": "Q2_K_XL", "size_gib": 76.5, "experts_gib": 42.9, "ppl": 2.755, "kld": 0.323, "top1_pct": 84.3},
    {"id": "pub-ud-iq3_xxs", "name": "UD-IQ3_XXS", "quant": "IQ3_XXS", "size_gib": 79.4, "experts_gib": 45.3, "ppl": 2.564, "kld": 0.244, "top1_pct": 87.0},
    {"id": "pub-gsq-iq3_s", "name": "GSQ-RCO IQ3_S", "quant": "IQ3_S", "size_gib": 81.0, "experts_gib": 46.8, "ppl": 2.387, "kld": 0.177, "top1_pct": 89.4,
     "decode_off": 145.1, "decode": 239.5, "power_w": 400},
    {"id": "pub-ud-q3_k_xl", "name": "UD-Q3_K_XL", "quant": "Q3_K_XL", "size_gib": 86.8, "experts_gib": 52.0, "ppl": 2.457, "kld": 0.160, "top1_pct": 89.3},
    {"id": "pub-ud-q4_k_xl", "name": "UD-Q4_K_XL", "quant": "Q4_K_XL", "size_gib": 106.7, "experts_gib": 71.7, "ppl": 2.321, "kld": 0.068, "top1_pct": 93.0},
    {"id": "pub-q8_0", "name": "Q8_0 (the reference)", "quant": "Q8_0", "size_gib": 178.3, "experts_gib": 119.5, "ppl": 2.282, "kld": 0.0, "top1_pct": 100.0},
]


def published() -> List[Dict[str, Any]]:
    return [{"id": p["id"], "kind": "published", "name": p["name"], "quant": p["quant"], "source": PUBLISHED_SOURCE,
             "figures": {k: v for k, v in p.items() if k not in ("id", "name", "quant")}, "tests": {}} for p in PUBLISHED]


def overview(local: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The page's list: the local .basalt files (items of models.scan) with what was measured for each, then the published references."""
    entries = all_entries()
    models = []
    for m in local:
        ident = fingerprints(m["path"])
        if ident is None:
            continue
        entry = read(ident["fp"]) or {"results": {}, "weights_fp": ident["weights_fp"]}
        entry = {**entry, "model": {**(entry.get("model") or {}), "name": m.get("name"), "path": m.get("path"), "quant": m.get("quant"),
                                    "size_bytes": m.get("size_bytes"), "experts_bytes": m.get("experts_bytes")}}
        tests = {s: {"at": r["at"], "power_w": r.get("power_w")} for s in STEPS if (r := _latest(entry, s))}
        models.append({"id": ident["fp"], "kind": "local", "name": m.get("name"), "quant": m.get("quant"), "path": m.get("path"),
                       "fp": ident["fp"], "weights_fp": ident["weights_fp"], "figures": figures(entry, entries), "tests": tests})
    return {"models": models + published(), "metrics": METRICS}


# ------------------------------------------------------------------------------------------------ what a run measured
def settings_of(config: Dict[str, Any], **extra) -> Dict[str, Any]:
    """The flat settings of a run from its config file's object ({"server": {...}, "engine": {...}}), plus the test's own (relax, reps, power, n)."""
    flat = {**(config.get("engine") or {}), **{k: v for k, v in (config.get("server") or {}).items() if k in ("parallel", "vision", "vision-hold", "vision-projector")}}
    flat.setdefault("vision-hold", "keep")
    flat.setdefault("vision-projector", "auto")
    flat.update(extra)
    return flat


def record_run(model_path: str, model: Dict[str, Any], run: Dict[str, Any]) -> List[str]:
    """Stores what one run (the dict benchrun writes) measured, each step under its own key; the steps stored."""
    ident = fingerprints(model_path)
    if ident is None:
        raise ValueError(f"{model_path}: not a model file this PC can read")
    config = run.get("config") or {}
    power = run.get("power_limit_w")
    at = run.get("at") or time.time()
    stored = []
    base = settings_of(config)
    if run.get("mem"):
        record(ident, model, "memory", step_key("memory", base), base, {"mem": run["mem"], "load_seconds": run.get("load_seconds"), "ready": run.get("ready")}, power, at)
        stored.append("memory")
    if run.get("speed"):
        s = settings_of(config, relax=sorted({float(r.get("relax") or 0) for r in run["speed"]}), reps=1 + max(int(r.get("rep", 0)) for r in run["speed"]), power=power)
        record(ident, model, "speed", step_key("speed", s), s, {"rows": run["speed"], **{k: run[k] for k in ("prefill_tok_s", "decode_off") if k in run}}, power, at)
        stored.append("speed")
    if run.get("image"):
        record(ident, model, "image", step_key("image", base), base, {"tries": run["image"]}, power, at)
        stored.append("image")
    if run.get("gsm8k"):
        s = settings_of(config, n=run["gsm8k"]["n"])
        record(ident, model, "quality", step_key("quality", s), s, {"gsm8k": run["gsm8k"]}, power, at)
        stored.append("quality")
    return stored
