"""The Loader's endpoints: what the page reads (GET /api/loader), the live plan of the values being edited
(POST /api/loader/plan) and the load itself (POST /api/loader/load): checked as the server would load the file,
checked against the memory free now, written to the --config file (FILE.bak kept) and applied by restarting the
engine inside this server."""
from __future__ import annotations

import os
import threading

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


def engine_holds(svc) -> tuple[dict, int]:
    """(_holds) what the running engine gives back when a load stops it, for any plan of a load (the profiles'
    summaries too)."""
    return _holds(svc, sup_of(svc))


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
           "status": sup.status(), "mock": sup.mock, "standby": bool(getattr(svc, "standby", False)),
           "relaunch": bool(getattr(svc, "relaunch_on_model_change", False))}
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
    out["reserve"] = {"default_mib": out["safe"]["engine.vram-reserve-mib"],
                      "warn_mib": loader.RESERVE_WARN_MIB, "vram_total": system.get("vram_total")}
    out["machine"] = loader.machine_settings()
    out["free_share"] = loader.free_share()
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
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    if model is None and not running and not sup.mock:
        raise Refused(409, {"error": {"type": "invalid_request_error", "code": "no_model",
                                      "message": "no model is chosen: pick one in the Loader's model list first"}})
    result = settings.save(path, values, settings.LOADER_FIELDS) if values else {"changed": [], "values": merged}
    if model is not None:
        from serve import models
        changed = settings.set_model(path, model, models.basalt_info(model, os.path.getsize(model)).get("name"))
        result["changed"] = result["changed"] + changed["changed"]
        if not _same(model, running) and getattr(svc, "relaunch_on_model_change", False):
            # the program that started this server starts it again with the model now in the config file
            print(f"[server] loader: {', '.join(result['changed']) or 'no change'} saved to {path}; the server "
                  "starts again with the new model", flush=True)
            threading.Timer(0.5, svc.exit_request.set).start()
            return {"status": "restarting", "changed": result["changed"], "values": result["values"], "plan": p,
                    "notes": ["model_restart"], "model": model}
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


# ------------------------------------------------------------------------------------------------ the wizard
def _wizard_facts(svc, model):
    if model:
        return facts_of(svc, model), model
    path = _config(svc)
    try:
        config_model = settings.model_of(path) if path else None
    except (OSError, ValueError):
        config_model = None
    return facts_of(svc, config_model), config_model


def wizard_info(svc, model: str | None = None) -> dict:
    """GET /api/wizard: this PC (step 1) and the proposal for `model` (else the config file's model)."""
    sup = sup_of(svc)
    facts, model = _wizard_facts(svc, model)
    holds, holds_vram = _holds(svc, sup)
    hw = loader.wizard_hardware(getattr(svc, "gpu_index", 0))
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    return {"hardware": hw, "proposal": loader.wizard_proposal(hw, facts, {}, holds, holds_vram), "model": model,
            "running_model": running, "standby": bool(getattr(svc, "standby", False)),
            "same_model": not model or _same(model, running) or sup.mock,
            "relaunch": bool(getattr(svc, "relaunch_on_model_change", False)), "status": sup.status()}


def wizard_plan(svc, req: dict) -> dict:
    """POST /api/wizard/plan: the values computed from the answers; nothing is written."""
    sup = sup_of(svc)
    facts, model = _wizard_facts(svc, req.get("model") or None)
    holds, holds_vram = _holds(svc, sup)
    hw = loader.wizard_hardware(getattr(svc, "gpu_index", 0))
    out = loader.wizard_proposal(hw, facts, req.get("answers") or {}, holds, holds_vram)
    out["model_path"] = model
    return out


def wizard_measure(svc, req: dict) -> dict:
    """POST /api/wizard/measure {"model", "values"}: measure the values with this server's model now; with another
    model, save it with the values and start the server again with it (when its owner can), then measure."""
    sup = sup_of(svc)
    path = _config(svc)
    if not path:
        raise Refused(409, {"error": {"type": "invalid_request_error", "code": "no_config",
                                      "message": "the server was started without --config: there is no file to load from"}})
    if sup.busy():
        raise Refused(409, {"error": {"type": "model_busy", "code": "busy",
                                      "message": "a load, a calibration or a measurement is already running"}})
    try:
        values = clean(req.get("values"))
        model = settings.check_model_value("model", req["model"]) if req.get("model") else None
    except ValueError as e:
        raise Refused(400, {"error": {"type": "invalid_request_error", "message": str(e)}}) from None
    now = loader.system_now(getattr(svc, "gpu_index", 0))
    limit, used = now.get("commit_limit"), now.get("commit_used")
    if limit and used is not None and used / limit >= loader.MEASURE_COMMIT_MAX:
        raise Refused(409, {"error": {"type": "insufficient_memory", "code": "commit_high",
                                      "message": f"the commit in use is {used / limit:.0%} of the limit: the "
                                                 f"measurement starts only below {loader.MEASURE_COMMIT_MAX:.0%}"}})
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    if model and not _same(model, running) and not sup.mock:
        if not getattr(svc, "relaunch_on_model_change", False):
            raise Refused(409, {"error": {"type": "invalid_request_error", "code": "load_first",
                                          "message": "the measurement runs with the model this server has: load the "
                                                     "model first, then measure"}})
        clean_values = {k: v for k, v in values.items() if v is not None}
        loader.save_machine_settings({"pending_measure": {"model": model, "values": clean_values,
                                                          "at": loader.now_iso()}})
        out = load(svc, {"values": clean_values, "model": model})
        return {**out, "measure": "after_restart"}
    if not sup.measure({k: v for k, v in values.items() if v is not None}):
        raise Refused(409, {"error": {"type": "model_busy", "code": "busy",
                                      "message": "a load, a calibration or a measurement is already running"}})
    return {"status": "measuring"}


def pending_measure(svc) -> bool:
    """At the start of a server the wizard restarted with another model: its measurement starts now (True)."""
    pm = loader.machine_settings().get("pending_measure")
    running = (getattr(svc, "settings_info", None) or {}).get("model")
    if not isinstance(pm, dict) or not _same(pm.get("model"), running):
        return False
    loader.save_machine_settings({"pending_measure": None})
    print("[server] the configuration wizard measures the values it chose for this model", flush=True)
    return sup_of(svc).measure(pm.get("values") or {})


def wizard_save(svc, req: dict) -> dict:
    """POST /api/wizard/save: this PC's answers and Machine Default (load profile with the model, inference preset)."""
    facts, model = _wizard_facts(svc, req.get("model") or None)
    values = clean(req.get("values"))
    port = ((getattr(svc, "settings_info", None) or {}).get("running") or {}).get("server.port")
    return loader.wizard_save(model, values, req.get("free_share", loader.free_share()),
                              int(req.get("vram_reserve_mib") or values.get("engine.vram-reserve-mib") or 0),
                              req.get("monitor"), facts.get("sampling") or {}, req.get("measured"), port)
