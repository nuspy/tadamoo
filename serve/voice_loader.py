"""The order of loading and the VRAM of the voice functions (Models > Voice services, the "Voice functions" dialog).

A function (stt, tts, avatar) is wanted on or off, with a model and a placement: the values of voice.json. The voice service holds what is
loaded now. This module compares the two, says what loads and unloads and whether it fits, and does it:

  adding, engine running   the model is unloaded, the service applies the change (it measures the VRAM of each step), and the model is
                           loaded again through the Loader's reload, which plans in what is left and keeps the Loader's reserve
  removing                 the service unloads the function now and the model is NOT touched; the VRAM stays free until
                           `reload_llm` is confirmed, and that reload then uses all the free VRAM minus the reserve
  the Loader's Load        functions that are on and not loaded load first, so the Loader reads the free VRAM they left

Every change runs as a job on a thread and holds the supervisor's job slot, so no reload, calibration or other apply runs meanwhile. A
different model (the server ends and its owner starts it again) never reaches the service: the functions stay loaded. Measured VRAM is
kept in voice-measured.json and replaces the catalog's size as the estimate."""
from __future__ import annotations

import json
import os
import threading
import time

from serve import loader, loader_api, models, userdata, voice_catalog, voice_service, voice_settings

FUNCTIONS = ("stt", "tts", "avatar")
MEASURED_FILE = "voice-measured.json"
APPLY_TIMEOUT_S = 3600.0                 # a first load of a large model from a slow disk
MIB = 1 << 20
_lock = threading.Lock()
_job: dict = {"phase": "idle"}
_pending: dict | None = None             # functions were removed and the model was left as it is: {"recovered_mib", "removed", "at"}


def _refuse(status: int, code: str, message: str) -> models.Refused:
    return models.Refused(status, {"error": {"type": "voice_loader", "code": code, "message": message}})


# ------------------------------------------------------------------------------------------------ what was measured
def _measured_file():
    return userdata.path(MEASURED_FILE)


def measured() -> dict:
    try:
        data = json.loads(_measured_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_measured(model: str, device: str, memory: str, mib: int) -> None:
    data = measured()
    data[model] = {"mib": int(mib), "device": device, "memory": memory, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    path = _measured_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def estimate_mib(model: str, device: str, memory: str) -> tuple[int | None, str]:
    """(MiB of VRAM the model is expected to take, where the figure comes from: measured | catalog | cpu | unknown)."""
    if device == "cpu" or memory == "ram":
        return 0, "cpu"
    saved = measured().get(model)
    if isinstance(saved, dict) and saved.get("device") == device and saved.get("memory") == memory and isinstance(saved.get("mib"), int):
        return saved["mib"], "measured"
    entry = voice_catalog.find(model)
    if entry is not None and entry.get("size"):
        return -(-int(entry["size"]) // MIB), "catalog"
    return None, "unknown"


# ------------------------------------------------------------------------------------------------ what is wanted and what is there
def desired_from_settings() -> dict:
    return {f: {"on": voice_settings.enabled(f), "model": voice_settings.selected(f), **voice_settings.placement(f)} for f in FUNCTIONS}


def normalise(desired) -> dict:
    """The wanted state of every function, checked; a function left out keeps the value of voice.json."""
    out = desired_from_settings()
    if desired is None:
        return out
    if not isinstance(desired, dict):
        raise _refuse(400, "bad_request", 'send {"desired": {"stt": {"on": true, "model": "...", "device": "auto", "memory": "vram"}, ...}}')
    for f, req in desired.items():
        if f not in FUNCTIONS or not isinstance(req, dict):
            raise voice_settings.VoiceSettingsError("service", "Unknown voice function.", "Funzione vocale sconosciuta.")
        want = {**out[f], **{k: req[k] for k in ("on", "model", "device", "memory") if k in req}}
        if not isinstance(want["on"], bool):
            raise voice_settings.VoiceSettingsError("enabled", "on is true or false.", "on è true o false.")
        entry = voice_catalog.find(want["model"])
        if entry is None or entry["service"] != f:
            raise voice_settings.VoiceSettingsError("model", "This model is not in the catalog of that service.", "Questo modello non è nel catalogo di quel servizio.")
        voice_settings.check_placement(want["device"], want["memory"])
        out[f] = want
    return out


def live_state() -> dict:
    """The service's own view (GET /functions); a refusal naming `voice_service_down` when it cannot be reached."""
    try:
        code, body = voice_service.call("GET", "/functions")
    except voice_service.ServiceDown as e:
        raise _refuse(503, "voice_service_down", str(e)) from None
    if code != 200 or not body or not isinstance(body.get("functions"), dict):
        raise _refuse(503, "voice_service_down", f"the voice service answered {code} to its functions request")
    return body


def _gpu(svc) -> int:
    return int(getattr(svc, "gpu_index", 0) or 0)


def _reserve_mib(svc) -> int:
    return int(loader.effective(loader_api.file_values(svc))["engine.vram-reserve-mib"])


def _llm_running(svc) -> bool:
    return bool(svc.loaded()) and not svc.loading


def vram_now(svc) -> dict:
    """The Loader's own reading of the GPU, in MiB (None when it cannot be read), the reserve it keeps and what the running model holds."""
    now = loader.system_now(_gpu(svc))
    free, total = now.get("vram_free"), now.get("vram_total")
    held = loader_api.engine_holds(svc)[1] // MIB if _llm_running(svc) else 0
    return {"free_mib": free // MIB if free is not None else None, "total_mib": total // MIB if total is not None else None,
            "reserve_mib": _reserve_mib(svc), "llm_mib": held}


def _item(f: str, want: dict, live: dict, root: str) -> dict:
    entry = voice_catalog.find(want["model"])
    loaded = live.get("state") in ("loaded", "loading")
    with_ = live.get("loaded_with") or {"model": want["model"], "device": want["device"], "memory": want["memory"]}
    same = (with_.get("model"), with_.get("device"), with_.get("memory")) == (want["model"], want["device"], want["memory"])
    installed = voice_catalog.installed(entry, root)
    est, source = estimate_mib(want["model"], want["device"], want["memory"])
    out = {"id": f, "model": want["model"], "name": entry["name"], "engine": entry["engine"], "device": want["device"], "memory": want["memory"],
           "state": live.get("state", "unloaded"), "installed": installed, "estimated_mib": est, "estimate_source": source,
           "measured_mib": live.get("measured_mib"), "recover_mib": 0, "error": live.get("error")}
    if want["on"]:
        out["action"] = "keep" if loaded and same else ("reload" if loaded else "load")
        if not installed:
            out["action"], out["reason"] = "missing", live.get("reason") or f"{entry['name']} is not installed: install it from Settings > Voice."
    else:
        out["action"] = "unload" if loaded else "off"
    if out["action"] in ("unload", "reload"):
        old = with_.get("model", want["model"])
        out["recover_mib"] = live.get("measured_mib") if live.get("measured_mib") is not None else estimate_mib(old, with_.get("device", want["device"]),
                                                                                                           with_.get("memory", want["memory"]))[0]
    return out


def plan(svc, desired=None, live=None) -> dict:
    """What an apply would do: the items, the VRAM needed and recovered, the free VRAM now, whether the model must be loaded again and
    whether the additions fit. `fits` is None when a figure is missing."""
    want = normalise(desired)
    live = live or live_state()
    root = voice_settings.models_dir()
    items = [_item(f, want[f], live["functions"].get(f) or {}, root) for f in FUNCTIONS]
    adds = [i for i in items if i["action"] in ("load", "reload")]
    gone = [i for i in items if i["action"] in ("unload", "reload")]
    vram = [i for i in adds if i["memory"] == "vram" and i["device"] != "cpu"]
    unknown = [i["id"] for i in vram if i["estimated_mib"] is None]
    v = vram_now(svc)
    need = sum(i["estimated_mib"] or 0 for i in vram)
    recover = sum(i["recover_mib"] or 0 for i in gone)
    recover_known = all(i["recover_mib"] is not None for i in gone)
    llm = _llm_running(svc)
    left = None if v["free_mib"] is None else v["free_mib"] + v["llm_mib"] + recover - need - v["reserve_mib"]
    return {"items": items, "changes": any(i["action"] in ("load", "reload", "unload") for i in items), "free_mib": v["free_mib"],
            "total_mib": v["total_mib"], "reserve_mib": v["reserve_mib"], "llm_mib": v["llm_mib"], "need_mib": need, "recover_mib": recover,
            "recover_known": recover_known, "unknown": unknown, "llm_running": llm, "llm_reload": bool(llm and vram),
            "left_for_llm_mib": left, "fits": None if left is None or unknown else left >= 0,
            "reload_suggested": bool(llm and gone and not vram)}


# ------------------------------------------------------------------------------------------------ what the page reads
def info(svc) -> dict:
    """GET /api/voice/functions: the wanted state of voice.json, the service's live state when it answers, the Loader's free VRAM, the job."""
    want = desired_from_settings()
    try:
        live = live_state()
        running, reason = True, ""
    except models.Refused as e:
        live, running, reason = {"functions": {}}, False, e.body["error"]["message"]
    p = plan(svc, want, live) if running else None
    v = vram_now(svc)
    rows = []
    root = voice_settings.models_dir()
    for f in FUNCTIONS:
        item = next((i for i in p["items"] if i["id"] == f), None) if p else _item(f, want[f], {}, root)
        rows.append({**item, "on": want[f]["on"], "live": live["functions"].get(f)})
    vram_rows = [r for r in rows if r["memory"] == "vram" and r["device"] != "cpu"]
    need = sum(r["estimated_mib"] or 0 for r in vram_rows if r["on"])
    unknown = [r["id"] for r in vram_rows if r["on"] and r["estimated_mib"] is None]
    held = sum((r["measured_mib"] if r["measured_mib"] is not None else r["estimated_mib"]) or 0 for r in vram_rows if r["state"] == "loaded")
    room = None if v["free_mib"] is None else v["free_mib"] + v["llm_mib"] + held - v["reserve_mib"]      # all the VRAM the model and the functions share
    with _lock:
        job, pend = dict(_job), dict(_pending) if _pending else None
    return {"service": {"running": running, "reason": reason}, "functions": rows, "vram": v, "set_need_mib": need, "set_unknown": unknown,
            "left_for_llm_mib": None if room is None or unknown else max(0, room - need), "llm_running": _llm_running(svc),
            "pending_reload": pend, "job": job}


# ------------------------------------------------------------------------------------------------ doing it
def _set(**kw) -> None:
    with _lock:
        _job.update(kw)


def _wanted(want: dict, items: list) -> dict:
    est = {i["id"]: i["estimated_mib"] for i in items}
    return {f: {**want[f], "estimated_mib": est.get(f)} for f in FUNCTIONS}


def _persist(want: dict) -> None:
    for f in FUNCTIONS:
        voice_settings.select(f, want[f]["model"])
        voice_settings.set_placement(f, want[f]["device"], want[f]["memory"])
        voice_settings.set_enabled(f, want[f]["on"])


def llm_reloaded() -> None:
    """The model loads again: the VRAM that was left free is used, nothing is pending any more."""
    global _pending
    with _lock:
        _pending = None


def _message(e: Exception) -> str:
    body = getattr(e, "body", None)
    return (body.get("error", {}).get("message") if isinstance(body, dict) else None) or str(e) or e.__class__.__name__


def _run(svc, sup, want, p, then) -> None:
    global _pending
    unloaded, was_standby, error = False, bool(getattr(svc, "standby", False)), None
    try:
        if p["llm_reload"]:
            _set(phase="unloading_llm")
            result = svc.unload()
            if result == "busy":
                raise _refuse(409, "busy", "a request is running: wait for it to finish, then apply again")
            if result == "unloaded":
                unloaded, svc.standby = True, True              # nothing loads it again while the functions load
        _set(phase="applying")
        try:
            code, body = voice_service.call("POST", "/functions/apply", {"functions": _wanted(want, p["items"])}, APPLY_TIMEOUT_S)
        except voice_service.ServiceDown as e:
            raise _refuse(503, "voice_service_down", str(e)) from None
        if code != 200 or not body or "report" not in body:
            raise _refuse(502, "apply_failed", (body or {}).get("error") or f"the voice service answered {code}")
        _set(report=body["report"], free_after_mib=body.get("free_vram_mib"))
        for i in p["items"]:
            r = body["report"].get(i["id"]) or {}
            if i["action"] in ("load", "reload") and r.get("state") == "loaded" and r.get("measured_mib") is not None and i["memory"] == "vram":
                record_measured(i["model"], i["device"], i["memory"], r["measured_mib"])
        _set(problems=[f"{k}: {r['error']}" for k, r in body["report"].items() if r.get("error")])
        removed = [i for i in p["items"] if i["action"] == "unload"]
        if removed and p["llm_running"] and not p["llm_reload"] and then is None:
            recovered = sum((body["report"].get(i["id"]) or {}).get("measured_mib") or i["recover_mib"] or 0 for i in removed)
            with _lock:
                _pending = {"recovered_mib": recovered, "removed": [i["id"] for i in removed], "at": time.time()}
    except models.Refused as e:
        error = _message(e)
        _set(error_code=e.body["error"].get("code"))
    except Exception as e:                                       # noqa: BLE001 (reported on the page)
        error = str(e) or e.__class__.__name__
    finally:
        if then is not None:
            _set(phase="loading_llm")
            try:
                then()
            except Exception as e:                               # noqa: BLE001 (the load's own refusal, shown with the job)
                error = error or _message(e)
            if sup.status().get("phase") == "voice":             # the Load did not start a reload: the slot is free again
                sup.release(None)
        elif unloaded:
            _set(phase="reloading_llm")
            llm_reloaded()
            if not sup.reload(held=True):
                svc.standby = was_standby
                sup.release(None)
                error = error or "the model could not be loaded again: a job is running"
        else:
            sup.release(None)
        _set(phase="failed" if error else "done", error=error, finished=time.time())


def start(svc, want: dict, p: dict, then=None, wait: bool = False) -> dict:
    """Take the supervisor's slot, write voice.json and run the job; Refused when a job is running."""
    sup = loader_api.sup_of(svc)
    with _lock:
        if _job.get("phase") in ("unloading_llm", "applying", "reloading_llm", "loading_llm"):
            raise _refuse(409, "busy", "the voice functions are being changed already")
    if not sup.hold("voice", "voice"):
        raise _refuse(409, "busy", "a load or a calibration is already running")
    _persist(want)
    with _lock:
        _job.clear()
        _job.update(phase="starting", started=time.time(), finished=None, error=None, report=None,
                    llm_reload=p["llm_reload"], plan={k: p[k] for k in ("need_mib", "recover_mib", "free_mib", "left_for_llm_mib")})
    t = threading.Thread(target=_run, args=(svc, sup, want, p, then), name="voice-apply", daemon=True)
    t.start()
    if wait:
        t.join()
    with _lock:
        return dict(_job)


def apply(svc, req: dict, wait: bool = False) -> dict:
    """POST /api/voice/functions/apply {"desired", "confirm": true}: the job's state, or {"status": "nothing"} when nothing changes."""
    if not isinstance(req, dict) or req.get("confirm") is not True:
        raise _refuse(400, "confirm_required", 'send "confirm": true: this loads and unloads models')
    want = normalise(req.get("desired"))
    p = plan(svc, want)
    if not p["changes"]:
        _persist(want)
        return {"status": "nothing", "plan": p}
    if p["fits"] is False and not req.get("force"):
        raise models.Refused(422, {"error": {"type": "insufficient_memory", "code": "does_not_fit",
                                             "message": "the functions do not fit in the VRAM that is free once the Loader's reserve is kept"}, "plan": p})
    job = start(svc, want, p, wait=wait)
    return {"status": "started", "job": job, "plan": p}


def before_load(svc, req: dict, wait: bool = False) -> dict | None:
    """The Loader's Load: functions that are on and not loaded load first. None when there is nothing to do or the service is not there
    (the Load goes on as it is); else the job, which ends by running the Load again with `voice_done`."""
    try:
        want = desired_from_settings()
        p = plan(svc, want)
    except models.Refused:
        return None
    if not any(i["action"] in ("load", "reload") for i in p["items"]):
        return None
    job = start(svc, want, p, then=lambda: loader_api.load(svc, req, voice_done=True), wait=wait)
    return {"status": "voice_first", "notes": ["voice_first"], "job": job, "plan": p}


def reload_llm(svc, req: dict) -> dict:
    """POST /api/voice/functions/reload-llm {"confirm": true}: the model loads again with all the free VRAM minus the Loader's reserve."""
    if not isinstance(req, dict) or req.get("confirm") is not True:
        raise _refuse(400, "confirm_required", 'send "confirm": true: the model is unloaded and loaded again')
    sup = loader_api.sup_of(svc)
    if not loader_api._config(svc):
        raise _refuse(409, "no_config", "the server was started without --config: there is no file to load from")
    if not (getattr(svc, "settings_info", None) or {}).get("model") and not sup.mock:
        raise _refuse(409, "no_model", "no model is chosen: pick one in the Loader's model list first")
    with _lock:
        running = _job.get("phase") in ("unloading_llm", "applying", "reloading_llm", "loading_llm")
    if running or not sup.reload():
        raise _refuse(409, "busy", "a load or a calibration is already running")
    llm_reloaded()
    return {"status": "reloading"}
