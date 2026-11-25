"""The order of loading and the VRAM of the voice functions (Models > Voice services, the "Voice functions" dialog).

A function (stt, tts, avatar) is wanted on or off, with a model and a placement: the values of voice.json. The voice service holds what is
loaded now. This module compares the two, says what loads and unloads and whether it fits, and does it:

  adding, engine running   the model is unloaded, the service applies the change (it measures the VRAM of each step), and the model is
                           loaded again through the Loader's reload, which plans in what is left and keeps the Loader's reserve
  removing                 the service unloads the function now and the model is NOT touched; the VRAM stays free until
                           `reload_llm` is confirmed, and that reload then uses all the free VRAM minus the reserve
  the Loader's Load        functions that are on and not loaded load first, so the Loader reads the free VRAM they left; a function that loads
                           on request (the avatar, by default) is left out and shows as `on_request`
  on request               the avatar loads when asked for (serve/voice_avatar_ensure.py): the broker lends the VRAM, the model is NOT touched
                           (`apply_lent`), and an idle avatar is unloaded the same way (`unload_lent`)
  adding, engine lends     with the engine running with --vram-elastic, a function added in Models > Voice services borrows its VRAM from the
                           expert cache (serve/voice_lend.py) and the model is NOT unloaded; the share is given back when the function unloads
  any start of the engine  `ensure_first`: the functions that are on, load at the Load and are not loaded load first, and the start waits for them

Every change runs as a job on a thread and holds the supervisor's job slot, so no reload, calibration or other apply runs meanwhile. A
different model (the server ends and its owner starts it again) never reaches the service: the functions stay loaded. Measured VRAM is
kept in voice-measured.json and replaces the catalog's size as the estimate."""
from __future__ import annotations

import json
import os
import threading
import time
import types

from serve import loader, loader_api, models, userdata, voice_catalog, voice_lend, voice_service, voice_settings

FUNCTIONS = ("stt", "tts", "avatar")
MEASURED_FILE = "voice-measured.json"
APPLY_TIMEOUT_S = 3600.0                 # a first load of a large model from a slow disk
MIB = 1 << 20
COMMIT_FACTOR = 2                        # the commit a function takes when none was measured, as a multiple of its VRAM (rough rule: the GPU
                                         # allocations are charged to the commit in full and the process's host copies come on top)
FIRST_WAIT_S = 600.0                     # most that a start of the engine waits for a function that is loading
_lock = threading.Lock()
_job: dict = {"phase": "idle"}
_first = threading.Lock()                # one `ensure_first` at a time
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


def _write_measured(data: dict) -> None:
    path = _measured_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_measured(model: str, device: str, memory: str, mib: int) -> None:
    data = measured()
    data[model] = {"mib": int(mib), "device": device, "memory": memory, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _write_measured(data)


def record_commit(model: str, device: str, memory: str, mib: int) -> None:
    """The commit a load took, kept with the VRAM just measured for the same placement (nothing is kept without it)."""
    data = measured()
    saved = data.get(model)
    if isinstance(saved, dict) and saved.get("device") == device and saved.get("memory") == memory and mib > 0:
        saved["commit_mib"] = int(mib)
        _write_measured(data)


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


def commit_estimate_mib(model: str, device: str, memory: str) -> tuple[int | None, str]:
    """(MiB of the Windows commit the model is expected to take, where the figure comes from: measured | rule | cpu | unknown). The rule is
    the VRAM estimate times COMMIT_FACTOR."""
    if device == "cpu" or memory == "ram":
        return 0, "cpu"
    saved = measured().get(model)
    if isinstance(saved, dict) and saved.get("device") == device and saved.get("memory") == memory and isinstance(saved.get("commit_mib"), int):
        return saved["commit_mib"], "measured"
    est, _source = estimate_mib(model, device, memory)
    return (None, "unknown") if est is None else (est * COMMIT_FACTOR, "rule")


# ------------------------------------------------------------------------------------------------ what is wanted and what is there
def desired_from_settings() -> dict:
    return {f: {"on": voice_settings.enabled(f), "model": voice_settings.selected(f), **voice_settings.placement(f), "load": voice_settings.load_mode(f)}
            for f in FUNCTIONS}


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
        want = {**out[f], **{k: req[k] for k in ("on", "model", "device", "memory", "load") if k in req}}
        if not isinstance(want["on"], bool):
            raise voice_settings.VoiceSettingsError("enabled", "on is true or false.", "on è true o false.")
        entry = voice_catalog.find(want["model"])
        if entry is None or entry["service"] != f:
            raise voice_settings.VoiceSettingsError("model", "This model is not in the catalog of that service.", "Questo modello non è nel catalogo di quel servizio.")
        voice_settings.check_placement(want["device"], want["memory"])
        if want["load"] not in voice_settings.LOAD_MODES or (want["load"] == "on_request" and f not in voice_settings.ON_REQUEST_SERVICES):
            raise voice_settings.VoiceSettingsError("load", "load is at_load, or on_request for the avatar.", "load è at_load, o on_request per l'avatar.")
        out[f] = want
    return out


def check_only(only):
    """The one function a change is for (None: all of them)."""
    if only is not None and only not in FUNCTIONS:
        raise voice_settings.VoiceSettingsError("service", "Unknown voice function.", "Funzione vocale sconosciuta.")
    return only


def only_one(want: dict, live: dict, only: str | None) -> dict:
    """`want` with every function but `only` as the service has it now, so that changing one function touches no other."""
    if not only:
        return want
    out = dict(want)
    for f in FUNCTIONS:
        if f == only:
            continue
        now = live["functions"].get(f) or {}
        if now.get("state") in ("loaded", "loading"):
            with_ = now.get("loaded_with") or {}
            out[f] = {"on": True, **{k: with_.get(k, want[f][k]) for k in ("model", "device", "memory")}}
        else:
            out[f] = {**want[f], "on": False}
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


def _item(f: str, want: dict, live: dict, root: str, ask: bool = False) -> dict:
    """`ask`: this function is being loaded on purpose now, so a function that loads on request loads."""
    entry = voice_catalog.find(want["model"])
    loaded = live.get("state") in ("loaded", "loading")
    with_ = live.get("loaded_with") or {"model": want["model"], "device": want["device"], "memory": want["memory"]}
    same = (with_.get("model"), with_.get("device"), with_.get("memory")) == (want["model"], want["device"], want["memory"])
    installed = voice_catalog.installed(entry, root)
    est, source = estimate_mib(want["model"], want["device"], want["memory"])
    commit, commit_source = commit_estimate_mib(want["model"], want["device"], want["memory"])
    out = {"id": f, "model": want["model"], "name": entry["name"], "engine": entry["engine"], "device": want["device"], "memory": want["memory"],
           "load": want.get("load", "at_load"), "state": live.get("state", "unloaded"), "installed": installed, "estimated_mib": est, "estimate_source": source,
           "commit_mib": commit, "commit_source": commit_source, "measured_mib": live.get("measured_mib"), "recover_mib": 0, "error": live.get("error")}
    if want["on"]:
        out["action"] = "keep" if loaded and same else ("reload" if loaded else "load")
        if not installed:
            out["action"], out["reason"] = "missing", live.get("reason") or f"{entry['name']} is not installed: install it in Models > Voice services."
        elif out["action"] == "load" and want.get("load") == "on_request" and not ask:
            out["action"] = "on_request"
    else:
        out["action"] = "unload" if loaded else "off"
    if out["action"] in ("unload", "reload"):
        old = with_.get("model", want["model"])
        out["recover_mib"] = live.get("measured_mib") if live.get("measured_mib") is not None else estimate_mib(old, with_.get("device", want["device"]),
                                                                                                           with_.get("memory", want["memory"]))[0]
    return out


def pending_commit(svc) -> int:
    """Bytes of the Windows commit that the functions which load before the model will take; 0 when there is nothing to load or the service is down."""
    try:
        live = live_state()
        want = desired_from_settings()
        root = voice_settings.models_dir()
        items = [_item(f, want[f], live["functions"].get(f) or {}, root) for f in FUNCTIONS]
    except Exception:  # noqa: BLE001 - no voice service, or a setting that cannot be read: nothing is counted
        return 0
    return sum(i["commit_mib"] or 0 for i in items if i["action"] in ("load", "reload")) * MIB


def commit_check(svc, need_mib: int, with_model: bool) -> dict:
    """Whether the commit has room for `need_mib` more by the Loader's own rule (the share kept free stays free); `with_model`: for the model
    too, which loads after the functions. {"ok": True | False | None (the commit cannot be read), "reason", "reason_it"}."""
    out = {"ok": True, "reason": "", "reason_it": ""}
    if need_mib <= 0:
        return out
    problem = None
    try:
        if with_model:
            p = loader_api.evaluate(svc, loader_api.file_values(svc), voice_commit=need_mib * MIB)
            out["ok"] = None if "commit" in p["unknown"] else True
            problem = next((x for x in p["problems"] if x["code"] == "commit"), None)
        else:
            now = loader.system_now(_gpu(svc))
            limit, used = now.get("commit_limit"), now.get("commit_used")
            if limit is None or used is None:
                out["ok"] = None
            else:
                share = loader.free_share()
                keep, have = loader.keep_bytes(limit, share), limit - used
                if need_mib * MIB > have - keep:
                    problem = {"code": "commit", "need": need_mib * MIB, "have": have, "keep": keep, "share": share}
    except (ValueError, OSError, KeyError):
        out["ok"] = None
    if problem is not None:
        out.update(ok=False, reason=loader.problem_text(problem, "en"), reason_it=loader.problem_text(problem, "it"))
    return out


def plan(svc, desired=None, live=None, only=None, lent=False) -> dict:
    """What an apply would do: the items, the VRAM needed and recovered, the free VRAM now, whether the model must be loaded again and
    whether the additions fit. `fits` is None when a figure is missing. `commit_ok` is the same for the Windows commit, with the functions'
    estimate (`commit_mib` per item) added to what the model needs when it loads again; False comes with `commit_reason` and
    `commit_reason_it`. With `only`, the other functions stay as they are loaded now, and
    that function loads even when it loads on request. `lent`: the broker lent the VRAM, so the model stays as it is and the additions fit."""
    want = normalise(desired)
    live = live or live_state()
    want = only_one(want, live, check_only(only))
    root = voice_settings.models_dir()
    items = [_item(f, want[f], live["functions"].get(f) or {}, root, ask=f == only) for f in FUNCTIONS]
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
    commit_need = sum(i["commit_mib"] or 0 for i in adds)
    commit = commit_check(svc, commit_need, bool(llm and vram and not lent))
    for i in adds:
        i["commit_ok"] = commit["ok"]
    return {"commit_need_mib": commit_need, "commit_ok": commit["ok"], "commit_reason": commit["reason"], "commit_reason_it": commit["reason_it"], "items": items, "changes": any(i["action"] in ("load", "reload", "unload") for i in items), "free_mib": v["free_mib"],
            "total_mib": v["total_mib"], "reserve_mib": v["reserve_mib"], "llm_mib": v["llm_mib"], "need_mib": need, "recover_mib": recover,
            "recover_known": recover_known, "unknown": unknown, "llm_running": llm, "llm_reload": bool(llm and vram and not lent),
            "left_for_llm_mib": left, "fits": True if lent else None if left is None or unknown else left >= 0,
            "lend_possible": bool(llm and vram and not lent and can_lend(svc)),
            "reload_suggested": bool(llm and gone and not vram and not lent), "lent": lent}


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
    wanted_now = [r for r in vram_rows if r["on"] and r["action"] != "on_request"]
    need = sum(r["estimated_mib"] or 0 for r in wanted_now)
    unknown = [r["id"] for r in wanted_now if r["estimated_mib"] is None]
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
    """What the service is asked for: a function that waits for its request stays off, so the service does not load it."""
    est = {i["id"]: i["estimated_mib"] for i in items}
    waiting = {i["id"] for i in items if i["action"] == "on_request"}
    return {f: {**want[f], "estimated_mib": est.get(f), **({"on": False} if f in waiting else {})} for f in FUNCTIONS}


def _commit_used(svc) -> int | None:
    return loader.system_now(_gpu(svc)).get("commit_used")


def _apply_now(svc, want: dict, p: dict) -> dict:
    """POST /functions/apply for the plan `p` and keep what it measured: the VRAM of each function loaded and, when only one function loaded
    into VRAM, the commit that rose. The service's answer; Refused when it cannot be had."""
    before = _commit_used(svc)
    try:
        code, body = voice_service.call("POST", "/functions/apply", {"functions": _wanted(want, p["items"])}, APPLY_TIMEOUT_S)
    except voice_service.ServiceDown as e:
        raise _refuse(503, "voice_service_down", str(e)) from None
    if code != 200 or not body or "report" not in body:
        raise _refuse(502, "apply_failed", (body or {}).get("error") or f"the voice service answered {code}")
    done = []
    for i in p["items"]:
        r = body["report"].get(i["id"]) or {}
        if i["action"] in ("load", "reload") and r.get("state") == "loaded" and r.get("measured_mib") is not None and i["memory"] == "vram":
            record_measured(i["model"], i["device"], i["memory"], r["measured_mib"])
            done.append(i)
    after = _commit_used(svc)
    if len(done) == 1 and before is not None and after is not None and after > before:
        record_commit(done[0]["model"], done[0]["device"], done[0]["memory"], (after - before) // MIB)
    return body


def _persist(want: dict, only: str | None = None) -> None:
    for f in (only,) if only else FUNCTIONS:
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


def _give_back(svc, ids) -> None:
    """The share of the lent VRAM of these functions ends (nothing when none was lent)."""
    lent = getattr(svc, "voice_lend", None)
    if lent is not None and ids:
        lent.release(*ids)


def _run(svc, sup, want, p, then, borrowed=()) -> None:
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
        body = _apply_now(svc, want, p)
        _set(report=body["report"], free_after_mib=body.get("free_vram_mib"))
        _set(problems=[f"{k}: {r['error']}" for k, r in body["report"].items() if r.get("error")])
        lent = getattr(svc, "voice_lend", None)
        removed = [i for i in p["items"] if i["action"] == "unload"]
        held = [i for i in removed if lent is not None and lent.holds(i["id"])]       # their VRAM goes back to the expert cache by itself
        if removed and p["llm_running"] and not p["llm_reload"] and then is None and not p.get("lent") and len(held) < len(removed):
            rest = [i for i in removed if i not in held]
            recovered = sum((body["report"].get(i["id"]) or {}).get("measured_mib") or i["recover_mib"] or 0 for i in rest)
            with _lock:
                _pending = {"recovered_mib": recovered, "removed": [i["id"] for i in rest], "at": time.time()}
        _give_back(svc, [i["id"] for i in held])
        _give_back(svc, [f for f in borrowed if (body["report"].get(f) or {}).get("state") != "loaded"])      # a function that did not load keeps no share
    except models.Refused as e:
        error = _message(e)
        _set(error_code=e.body["error"].get("code"))
        _give_back(svc, borrowed)
    except Exception as e:                                       # noqa: BLE001 (reported on the page)
        error = str(e) or e.__class__.__name__
        _give_back(svc, borrowed)
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


def start(svc, want: dict, p: dict, then=None, wait: bool = False, only: str | None = None, persist: bool = True, borrowed=()) -> dict:
    """Take the supervisor's slot, write voice.json (unless `persist` is off) and run the job; Refused when a job is running."""
    sup = loader_api.sup_of(svc)
    with _lock:
        if _job.get("phase") in ("unloading_llm", "applying", "reloading_llm", "loading_llm"):
            raise _refuse(409, "busy", "the voice functions are being changed already")
    if not sup.hold("voice", "voice"):
        raise _refuse(409, "busy", "a load or a calibration is already running")
    if persist:
        _persist(want, only)
    with _lock:
        _job.clear()
        _job.update(phase="starting", started=time.time(), finished=None, error=None, report=None,
                    llm_reload=p["llm_reload"], plan={k: p[k] for k in ("need_mib", "recover_mib", "free_mib", "left_for_llm_mib")})
    t = threading.Thread(target=_run, args=(svc, sup, want, p, then, tuple(borrowed)), name="voice-apply", daemon=True)
    t.start()
    if wait:
        t.join()
    with _lock:
        return dict(_job)


def apply(svc, req: dict, wait: bool = False) -> dict:
    """POST /api/voice/functions/apply {"desired", "only": "stt"|"tts"|"avatar" (optional), "confirm": true}: the job's state, or {"status": "nothing"} when nothing changes."""
    if not isinstance(req, dict) or req.get("confirm") is not True:
        raise _refuse(400, "confirm_required", 'send "confirm": true: this loads and unloads models')
    only = check_only(req.get("only"))
    live = live_state()
    want = only_one(normalise(req.get("desired")), live, only)
    p = plan(svc, want, live)
    if not p["changes"]:
        _persist(want, only)
        return {"status": "nothing", "plan": p}
    force = bool(req.get("force"))
    borrowed, refused, lend_commit = (), None, None
    if p["llm_reload"] and can_lend(svc):                        # the model keeps running: the functions borrow their VRAM from its expert cache
        lent_plan = plan(svc, want, live, lent=True)
        if lent_plan["commit_ok"] is not False:
            sizes = {i["id"]: i["estimated_mib"] or voice_lend.FALLBACK_MIB for i in lent_plan["items"]
                     if i["action"] in ("load", "reload") and i["memory"] == "vram" and i["device"] != "cpu"}
            got = voice_lend.of(svc).acquire(sizes)
            if got["ok"]:
                p, borrowed = lent_plan, tuple(sizes)
            else:
                refused = got["reason"]                          # the model is unloaded and loaded again, as without lending
        else:
            lend_commit = lent_plan                              # the commit has no room even for the function alone
    try:
        if p["fits"] is False and not force:
            raise models.Refused(422, {"error": {"type": "insufficient_memory", "code": "does_not_fit",
                                                 "message": "the functions do not fit in the VRAM that is free once the Loader's reserve is kept"}, "plan": p})
        if p["commit_ok"] is False and not force:
            # when the engine could lend, the rule that stopped the lend is the reason the person can act on, not the model reload's figures
            shown = lend_commit or p
            message, message_it = shown["commit_reason"], shown["commit_reason_it"]
            if lend_commit:
                message = "The engine could lend the VRAM, but the Windows commit has no room for the function: " + message
                message_it = "Il motore potrebbe prestare la VRAM, ma il commit di Windows non ha posto per la funzione: " + message_it
            raise models.Refused(422, {"error": {"type": "insufficient_memory", "code": "not_enough_commit", "message": message,
                                                 "message_it": message_it, **({"lend": "commit"} if lend_commit else {})}, "plan": p})
        job = start(svc, want, p, wait=wait, only=only, borrowed=borrowed)
    except BaseException:
        _give_back(svc, borrowed)
        raise
    return {"status": "started", "job": job, "plan": p, "lent": {"used": bool(borrowed), "refused": refused}}


def can_lend(svc) -> bool:
    """Whether the running engine can shrink its expert cache to lend VRAM (it was started with --vram-elastic)."""
    running = (getattr(svc, "settings_info", None) or {}).get("running") or {}
    try:
        return bool(loader.effective(running)["engine.vram-elastic"])
    except (KeyError, ValueError, TypeError):
        return False


def apply_lent(svc, only: str, wait: bool = True) -> dict:
    """Load one function into VRAM the broker lent: the model is not unloaded and the plan's fit check is not made (the lease checked the free
    VRAM). The job, or {"status": "nothing"}."""
    only = check_only(only)
    live = live_state()
    want = only_one(normalise(None), live, only)
    p = plan(svc, want, live, only=only, lent=True)
    if not p["changes"]:
        return {"status": "nothing", "plan": p}
    return {"status": "started", "job": start(svc, want, p, wait=wait, only=only), "plan": p}


def unload_lent(svc, only: str, wait: bool = True) -> dict:
    """Unload one function that was loaded on request, leaving the choices of voice.json (it is still wanted) and the model as they are."""
    only = check_only(only)
    live = live_state()
    want = only_one(normalise(None), live, only)
    want[only] = {**want[only], "on": False}
    p = plan(svc, want, live, lent=True)
    if not p["changes"]:
        return {"status": "nothing", "plan": p}
    return {"status": "started", "job": start(svc, want, p, wait=wait, only=only, persist=False), "plan": p}


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


class Booting:
    """The part of a service that `ensure_first` reads, for a server whose engine starts before its service exists."""
    standby, loading, engine, vision, gpu_index = False, True, None, None, 0

    def __init__(self, settings_info: dict, facts: dict) -> None:
        self.settings_info = settings_info
        self.supervisor = types.SimpleNamespace(mock=False, facts=facts)

    def loaded(self) -> bool:
        return False


def ensure_first(svc, sleep=time.sleep) -> None:
    """Every start of the model's engine calls this before it: the functions that are on, load with the Load and are not loaded load now, and
    the start waits for them, so that the model sizes itself in what is left. Nothing to load, or no voice service, returns at once. It never
    raises: a function that fails, or a commit that cannot hold both, is one line in the log and the model starts all the same."""
    try:
        with _first:
            _ensure_first(svc, sleep)
    except Exception as e:  # noqa: BLE001 - the model's start must not fail because of the voice functions
        print(f"[server] warning: the voice functions were not loaded before the model ({_message(e)})", flush=True)


def _ensure_first(svc, sleep) -> None:
    try:
        live = live_state()
    except models.Refused:
        return
    deadline = time.monotonic() + FIRST_WAIT_S
    while any(f.get("state") == "loading" for f in live["functions"].values()) and time.monotonic() < deadline:
        sleep(1.0)                                               # another job is loading one: the model starts after it
        live = live_state()
    want = desired_from_settings()
    p = plan(svc, want, live)
    todo = [i for i in p["items"] if i["action"] in ("load", "reload") and i["state"] != "error"]     # a function that failed is not tried again by itself
    if not todo:
        return
    need = sum(i["commit_mib"] or 0 for i in todo)
    check = commit_check(svc, need, True)
    if check["ok"] is False:
        print(f"[server] warning: the voice functions are not loaded before the model: {check['reason']}", flush=True)
        return
    print(f"[server] voice functions first: {', '.join(i['id'] for i in todo)}", flush=True)
    body = _apply_now(svc, want, p)
    for f, r in body["report"].items():
        if r.get("error"):
            print(f"[server] warning: voice function {f} did not load: {r['error']}", flush=True)


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
