"""The Loader's endpoints: what the page reads (GET /api/loader), the live plan of the values being edited
(POST /api/loader/plan) and the load itself (POST /api/loader/load): checked as the server would load the file,
checked against the memory free now, written to the --config file (FILE.bak kept) and applied by restarting the
engine inside this server."""
from __future__ import annotations

import os

from serve import loader, settings
from serve.config import PARALLEL_MAX
from serve.engine.vision import VISION_MODES

FIELD_NAMES = [f"{s}.{k}" for s, k in settings.LOADER_FIELDS]


def sup_of(svc):
    """The service's supervisor; a mock one for a service built without (tests, embedding)."""
    sup = getattr(svc, "supervisor", None)
    if sup is None:
        from serve.supervisor import Supervisor
        sup = svc.supervisor = Supervisor(svc, [], mock=not hasattr(svc.engine, "restart"))
    return sup


class Refused(Exception):
    """A load that cannot go ahead: the HTTP status and the body."""

    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", {}).get("message", ""))
        self.status, self.body = status, body


def _config(svc) -> str | None:
    return (getattr(svc, "settings_info", None) or {}).get("config")


def file_values(svc) -> dict:
    path = _config(svc)
    if not path:
        return {}
    try:
        return settings.values_of(settings.read_file(path), settings.LOADER_FIELDS)
    except (OSError, ValueError):
        return {}


def _holds(svc, sup):
    """What the running engine gives back when a load stops it: its process memory, and the VRAM its own settings
    planned (the GPU cannot say per process under WDDM)."""
    holds = loader.engine_holds(svc.engine)
    vram = 0
    if holds["ram"] or holds["commit"]:
        running = (getattr(svc, "settings_info", None) or {}).get("running") or {}
        now = loader.system_now(getattr(svc, "gpu_index", 0))
        total, free = now.get("vram_total") or 0, now.get("vram_free") or 0
        reserve = int(loader.effective(running)["engine.vram-reserve-mib"]) * loader.MiB
        vram = max(0, min(total - free, total - reserve))  # the GPU's use, at most what the engine could take
    return holds, vram


def _same(a, b) -> bool:
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def facts_of(svc, model: str | None) -> dict:
    """The sizes a plan uses: the running model's, or another .basalt file's (from its header)."""
    sup = sup_of(svc)
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    if not model or _same(model, running) or _same(model, sup.facts.get("path")):
        return sup.facts
    from serve import models
    try:
        return models.facts_for(models.check_basalt(model))
    except (OSError, KeyError) as e:
        raise ValueError(f"model: {model}: {e}") from None
    except ValueError as e:
        raise ValueError(f"model: {e}") from None


def evaluate(svc, values: dict, lang: str = "en", model: str | None = None) -> dict:
    """The plan and speed of `values` ({"engine.kv": "q4_0", ...}; None = the engine's default) against now, for the
    running model or `model` (a .basalt file)."""
    sup = sup_of(svc)
    facts = facts_of(svc, model)
    holds, holds_vram = _holds(svc, sup)
    system = loader.system_now(getattr(svc, "gpu_index", 0))
    p = loader.plan(values, facts, system, holds, holds_vram)
    p["model"] = {k: v for k, v in facts.items() if k != "path"} | {"path": facts.get("path")}
    from serve import hardware
    p["gpu"] = hardware.gpu(getattr(svc, "gpu_index", 0))
    p["warnings"] = loader.reserve_warnings(values, p["gpu"])
    cal = loader.best_calibration(facts, values)
    p["speed"] = loader.predict_speed(p, cal)
    p["problems"] = [{**x, "text": loader.problem_text(x, "en"), "text_it": loader.problem_text(x, "it")}
                     for x in p["problems"]]
    p["system"] = system
    return p


def clean(values) -> dict:
    """The form's values for the Loader's fields; ValueError for anything else."""
    if not isinstance(values, dict):
        raise ValueError('send {"values": {"engine.kv": "q4_0", ...}}')
    out = {}
    for name, value in values.items():
        if name not in FIELD_NAMES:
            raise ValueError(f"{name}: not a field the Loader sets")
        section, key = name.split(".", 1)
        out[name] = None if value is None or value == "" else settings.check_value(section, key, value)
    return out


def info(svc, local: bool, lang: str = "en") -> dict:
    sup = sup_of(svc)
    path = _config(svc)
    si = getattr(svc, "settings_info", None) or {}
    values = file_values(svc)
    try:
        config_model = settings.model_of(path) if path else None
    except (OSError, ValueError):
        config_model = None
    out = {"config": path, "model": {k: v for k, v in sup.facts.items() if k != "path"},
           "model_path": si.get("model"), "config_model": config_model,
           "values": values, "running": si.get("running") or {},
           "from_command_line": [n for n in si.get("cli") or [] if n in FIELD_NAMES], "fields": FIELD_NAMES,
           "defaults": loader.ENGINE_DEFAULTS, "editable": False, "reason": "",
           "limits": {"parallel_max": PARALLEL_MAX, "spec_max": settings.SPEC_MAX, "kv": list(settings.KV_MODES),
                      "vision": list(VISION_MODES), "ple_io": list(settings.PLE_IO_MODES),
                      "context_max": loader.CONTEXT_UI_MAX, "prefill_ceilings": list(settings.PREFILL_CEILINGS),
                      "pool_workers_max": settings.POOL_WORKERS_MAX, "cpu_threads": os.cpu_count()},
           "status": sup.status(), "mock": sup.mock}
    holds, holds_vram = _holds(svc, sup)
    system = loader.system_now(getattr(svc, "gpu_index", 0))
    try:
        out["plan"] = evaluate(svc, values, lang, config_model)
        facts = facts_of(svc, config_model)
    except ValueError:
        out["plan"] = evaluate(svc, values, lang)
        facts = sup.facts
    why = {}
    out["safe"] = loader.safe(facts, system, holds, holds_vram, why=why)
    out["safe_why"] = {k: {"en": en, "it": it} for k, (en, it) in why.items()}
    from serve import hardware
    out["gpu"] = out["plan"]["gpu"]
    out["gpu_warning"] = {"it": hardware.gpu_warning(out["gpu"], "it"), "en": hardware.gpu_warning(out["gpu"], "en")}
    out["reserve"] = {"default_mib": loader.safe_reserve_mib(system.get("vram_total")),
                      "warn_mib": loader.RESERVE_WARN_MIB, "vram_total": system.get("vram_total")}
    runs = loader.read_calibrations(sup.facts)["runs"]
    out["calibrations"] = sorted(({"at": r.get("at"), "fingerprint": fp, "decode_off": r.get("decode_off"),
                                   "decode_on": r.get("decode_on"), "prefill": r.get("prefill"), "mock": r.get("mock")}
                                  for fp, r in runs.items()), key=lambda r: r["at"] or "", reverse=True)
    if not path:
        out["reason"] = "no_config"
    elif not local:
        out["reason"] = "not_local"
    elif not os.access(path, os.W_OK):
        out["reason"] = "read_only"
    else:
        out["editable"] = True
    return out


def load(svc, req: dict) -> dict:
    """Check, write and apply; Refused with the status and body otherwise."""
    sup = sup_of(svc)
    path = _config(svc)
    if not path:
        raise Refused(409, {"error": {"type": "invalid_request_error", "code": "no_config",
                                      "message": "the server was started without --config: there is no file to load from"}})
    if sup.busy():
        raise Refused(409, {"error": {"type": "model_busy", "code": "busy",
                                      "message": "a load or a calibration is already running"}})
    model = req.get("model") if isinstance(req, dict) else None
    try:
        values = clean(req.get("values") if isinstance(req, dict) else None)
        settings.preview(path, values, settings.LOADER_FIELDS) if values else None
        if model is not None:
            model = settings.check_model_value("model", model)
    except ValueError as e:
        raise Refused(400, {"error": {"type": "invalid_request_error", "message": str(e)}}) from None
    merged = {**file_values(svc), **values}
    merged = {k: v for k, v in merged.items() if v is not None and k in FIELD_NAMES}
    p = evaluate(svc, merged, model=model)
    if not p["fits"]:
        raise Refused(422, {"error": {"type": "insufficient_memory", "code": "does_not_fit",
                                      "message": " ".join(x["text"] for x in p["problems"])},
                            "problems": p["problems"], "plan": p})
    result = settings.save(path, values, settings.LOADER_FIELDS) if values else {"changed": [], "values": merged}
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    if model is not None:
        from serve import models
        changed = settings.set_model(path, model, models.basalt_info(model, os.path.getsize(model)).get("name"))
        result["changed"] = result["changed"] + changed["changed"]
        if not _same(model, running):
            # the tokenizer, the chat template and the API name come from the model file when the server starts:
            # a different model is loaded by starting the server again, not by restarting the engine inside it
            print(f"[server] loader: {', '.join(result['changed']) or 'no change'} saved to {path}; the new model "
                  "loads when the server starts again", flush=True)
            return {"status": "saved", "changed": result["changed"], "values": result["values"], "plan": p,
                    "notes": ["model_on_restart"], "model": model}
    print(f"[server] loader: {', '.join(result['changed']) or 'no change'} saved to {path}; reloading the engine",
          flush=True)
    if not sup.reload():
        raise Refused(409, {"error": {"type": "model_busy", "code": "busy",
                                      "message": "a load or a calibration is already running"}})
    notes = []
    port = merged.get("server.port")
    running_port = ((getattr(svc, "settings_info", None) or {}).get("running") or {}).get("server.port")
    if port is not None and running_port is not None and int(port) != int(running_port):
        notes.append("port_on_restart")                 # the HTTP server keeps its port until it starts again
    return {"status": "reloading", "changed": result["changed"], "values": result["values"], "plan": p,
            "notes": notes}
