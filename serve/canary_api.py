"""The exfiltration test (the Openweight Canary add-on) as the page sees it: the routes under /api/canary.

The page writes a form; this module turns it into a canary config, starts and stops the test through the add-on's launcher,
keeps the security monitor on while the test runs, and reads back the findings, the report and the reviews. Everything the
test writes lives in the user data folder, so a test outlives the page and the server (it is found again by its pid file)."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from serve import addons, netwatch_api, userdata

KEY_ENV = "TADAMOO_CANARY_KEY"                      # the server's own key, passed by name so the config on disk holds no secret
WATCH_S = 5.0                                      # how often a running test is looked at
STOP_WAIT_S = 30.0                                 # how long a stop waits for the current session before it ends the process
EXIT_WAIT_S = 15.0
REPORT_MAX = 400_000

DEFAULTS: Dict[str, Any] = {
    "runtime": "basalt", "model": "", "base_url": "", "api_key": "", "vendor": "auto",
    "duration_minutes": None, "review_every": 10, "keep_running": False, "harness": "",
    "reviewer": {"enabled": False, "kind": "openai", "base_url": "", "api_key": "", "model": ""},
}


class CanaryError(ValueError):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class Context:
    """What the server knows about itself: its port, its key and the model it has loaded."""

    def __init__(self, port: int = 0, api_key: str = "", model: str = ""):
        self.port, self.api_key, self.model = int(port or 0), api_key or "", model or ""


_LOCK = threading.RLock()
_LAUNCHER: Any = None                              # the add-on's launch module; a test puts a fake here
_REVIEW: Dict[str, Any] = {"state": "idle", "error": "", "n": 0}
_WATCHER: Optional[threading.Thread] = None
_HARNESS_CACHE: Dict[str, List[Dict[str, str]]] = {}       # asked once per add-on folder: it is a process start
_HARNESSES: Optional[Callable[[], List[Dict[str, str]]]] = None   # a test puts a fake list source here


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="canary-task").start()


def _dir() -> Path:
    p = userdata.path("canary")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _state_dir() -> Path:
    return _dir() / "state"


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---- the add-on ----
def present() -> bool:
    return addons.canary_path() is not None


def lib(name: str) -> Any:
    """A module of the add-on's package, imported when first needed."""
    root = addons.canary_path()
    if root is None:
        raise CanaryError("The Openweight Canary add-on is not installed.", "no_addon")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    loaded = sys.modules.get("canary")
    if loaded is not None and Path(getattr(loaded, "__file__", "") or "").resolve().parent.parent != Path(root).resolve():
        for key in [k for k in sys.modules if k == "canary" or k.startswith("canary.")]:
            del sys.modules[key]                       # imported from another copy: follow the folder chosen now
        sys.path.remove(str(root))
        sys.path.insert(0, str(root))
    return importlib.import_module("canary." + name)


def launcher() -> Any:
    return _LAUNCHER if _LAUNCHER is not None else lib("launch")


def _catalog() -> Any:
    return lib("vendors").load_catalog(_dir() / "vendors.local.json")


def harnesses() -> List[Dict[str, str]]:
    """The simulated harness styles the add-on offers (`python -m canary harnesses`); empty when it has no such command."""
    root = addons.canary_path()
    if root is None:
        return []
    if _HARNESSES is not None:
        return _HARNESSES()
    if str(root) in _HARNESS_CACHE:
        return _HARNESS_CACHE[str(root)]
    _HARNESS_CACHE[str(root)] = out = _ask_harnesses(root)
    return out


def _ask_harnesses(root: Path) -> List[Dict[str, str]]:
    try:
        env = dict(os.environ, PYTHONPATH=str(root) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
        done = subprocess.run([sys.executable, "-m", "canary", "harnesses"], capture_output=True, text=True, timeout=20, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            return []
        return _parse_harnesses(done.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


def _parse_harnesses(text: str) -> List[Dict[str, str]]:
    """JSON (a list of ids or of {id, name, description}) or one id per line."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("harnesses", [])
    except ValueError:
        data = [ln.split()[0] for ln in text.splitlines() if ln.strip()]
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            out.append({"id": item, "name": item, "description": ""})
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append({"id": item["id"], "name": str(item.get("name") or item["id"]), "description": str(item.get("description") or "")})
    return out


# ---- settings ----
def settings() -> Dict[str, Any]:
    saved = _read(_dir() / "settings.json", {})
    out = json.loads(json.dumps(DEFAULTS))
    if isinstance(saved, dict):
        rev = saved.get("reviewer")
        out.update({k: v for k, v in saved.items() if k in out and k != "reviewer"})
        if isinstance(rev, dict):
            out["reviewer"].update({k: v for k, v in rev.items() if k in out["reviewer"]})
    return out


def _text(v: Any, name: str, limit: int = 300) -> str:
    if not isinstance(v, str):
        raise CanaryError(f"{name} is text.")
    return v.strip()[:limit]


def _url(v: Any, name: str) -> str:
    v = _text(v, name, 500)
    if v and not v.lower().startswith(("http://", "https://")):
        raise CanaryError(f"{name} starts with http:// or https://.")
    return v


def check_settings(patch: Any) -> Dict[str, Any]:
    if not isinstance(patch, dict):
        raise CanaryError("Send the settings as an object.")
    cur = settings()
    unknown = [k for k in patch if k not in DEFAULTS]
    if unknown:
        raise CanaryError("Unknown settings: " + ", ".join(sorted(unknown)))
    for k in ("runtime", "model", "api_key", "vendor", "harness"):
        if k in patch:
            cur[k] = _text(patch[k], k)
    if "base_url" in patch:
        cur["base_url"] = _url(patch["base_url"], "base_url")
    if not cur["runtime"]:
        raise CanaryError("Choose the engine the test runs against.")
    if "duration_minutes" in patch:
        d = patch["duration_minutes"]
        if d in (None, "", 0):
            cur["duration_minutes"] = None
        elif isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
            raise CanaryError("duration_minutes is a number above 0, or empty for no limit.")
        else:
            cur["duration_minutes"] = d
    if "review_every" in patch:
        v = patch["review_every"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise CanaryError("review_every is a whole number, 1 or more.")
        cur["review_every"] = v
    if "keep_running" in patch:
        cur["keep_running"] = bool(patch["keep_running"])
    if "reviewer" in patch:
        r = patch["reviewer"]
        if not isinstance(r, dict):
            raise CanaryError("reviewer is an object.")
        rev = cur["reviewer"]
        rev["enabled"] = bool(r.get("enabled", rev["enabled"]))
        if "kind" in r:
            if r["kind"] not in ("openai", "anthropic"):
                raise CanaryError('reviewer.kind is "openai" or "anthropic".')
            rev["kind"] = r["kind"]
        if "base_url" in r:
            rev["base_url"] = _url(r["base_url"], "reviewer.base_url")
        for k in ("api_key", "model"):
            if k in r:
                rev[k] = _text(r[k], "reviewer." + k)
    return cur


def save_settings(patch: Any) -> Dict[str, Any]:
    with _LOCK:
        cur = check_settings(patch)
        _write(_dir() / "settings.json", cur)
        return cur


# ---- the config the test runs with ----
def effective(s: Dict[str, Any], ctx: Context) -> Dict[str, Any]:
    """The model and endpoint the test will use: what the form says, else TaDaMoo's own."""
    ours = s["runtime"] == "basalt"
    model = s["model"] or (ctx.model if ours else "")
    base = s["base_url"] or (f"http://127.0.0.1:{ctx.port}/v1" if ours and ctx.port else "")
    return {"model": model, "base_url": base, "ours": ours}


OWNED_KEYS = ("target", "monitor", "network_watch", "state_dir", "runtime", "vendor", "vendors_file")     # decided here, never by the page


def overlay_path() -> Path:
    return _dir() / "page_settings.json"


def _deep(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: _deep(base.get(k), v) for k, v in over.items()}}
    return over


def with_page_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The settings saved in the Canary tab (what to look for, people, story, fake computer, alerts) laid over the config built from the form."""
    saved = _read(overlay_path(), {})
    for k, v in (saved.items() if isinstance(saved, dict) else ()):
        if k in OWNED_KEYS or (k == "reviewer" and not isinstance(v, dict)):
            continue
        cfg[k] = _deep(cfg.get(k), v)
    return cfg


def gui_app(ctx: Context) -> Any:
    """The Canary page with this server's start and stop (which keep the security monitor on), its own config and its own state."""
    server = lib("gui.server")
    return server.App(_dir() / "canary.json", state_dir=_state_dir(), prefix="/canary", check_host=False, overlay=overlay_path(),
                      start=lambda: start(ctx), stop=stop)


def gui_response(method: str, raw_path: str, headers: Any, body: bytes, ctx: Context) -> Tuple[int, bytes, str, Dict[str, str]]:
    if not present():
        raise CanaryError("The Openweight Canary add-on is not installed.", "no_addon")
    return lib("gui.server").handle(gui_app(ctx), method, raw_path, headers, body)


def build_config(s: Dict[str, Any], ctx: Context, state_dir: Path) -> Dict[str, Any]:
    eff = effective(s, ctx)
    if not eff["model"]:
        raise CanaryError("No model: load one in TaDaMoo, or type the model name the endpoint serves.", "no_model")
    cat = _catalog()
    if s["runtime"] not in cat.runtimes:
        raise CanaryError(f"Unknown engine '{s['runtime']}'.")
    vendor = None if s["vendor"] in ("", "auto") else s["vendor"]
    if vendor and vendor not in cat.vendors:
        raise CanaryError(f"Unknown vendor '{vendor}'.")
    cfg = lib("presets").build_config(cat, s["runtime"], eff["model"], vendor, eff["base_url"] or None)
    cfg.pop("_reviewer_example", None)
    if eff["ours"]:
        if not eff["base_url"]:
            raise CanaryError("TaDaMoo's own address is not known yet; try again in a moment.")
        key = f"env:{KEY_ENV}" if ctx.api_key else ""
        cfg["target"]["api_key"] = key
        cfg["monitor"] = {"base_url": f"http://127.0.0.1:{ctx.port}", "api_key": key}
        cfg["network_watch"] = None
    else:
        cfg["target"]["api_key"] = s["api_key"]
    if s["harness"]:
        cfg["harness"] = s["harness"]                          # written only when chosen: the add-on rejects keys it does not know
    cfg["state_dir"] = str(state_dir)
    cfg["vendors_file"] = str(_dir() / "vendors.local.json")
    minutes = s["duration_minutes"]
    cfg["duration"] = f"{int(round(minutes * 60))}" if minutes else "until-stopped"
    cfg["review_every"] = s["review_every"]
    rev = s["reviewer"]
    if rev["enabled"]:
        if not rev["base_url"] or not rev["model"]:
            raise CanaryError("The reviewer needs an address and a model name.")
        cfg["reviewer"] = {"kind": rev["kind"], "base_url": rev["base_url"], "api_key": rev["api_key"], "model": rev["model"], "extra_headers": {}}
    else:
        cfg["reviewer"] = None
    cfg = with_page_settings(cfg)                              # the reviewer may be the model under test: its reviews say so
    if not cfg.get("harnesses"):
        cfg["harnesses"] = ["hermes", "cline", "aider"]          # the test always runs the model through a harness, with its tools
    return cfg


def _write_config(s: Dict[str, Any], ctx: Context) -> Path:
    cfg = build_config(s, ctx, _state_dir())
    path = _dir() / "canary.json"
    _write(path, cfg)
    if ctx.api_key:
        os.environ[KEY_ENV] = ctx.api_key
    try:
        lib("config").load_config(path)
    except Exception as exc:                                   # the add-on's own check, with its own wording
        raise CanaryError(str(exc)) from exc
    return path


# ---- the test: start, look, stop ----
def _gui() -> Dict[str, Any]:
    return _read(_dir() / "run.json", {})


def _set_gui(**kw: Any) -> Dict[str, Any]:
    with _LOCK:
        g = _gui()
        g.update(kw)
        _write(_dir() / "run.json", g)
        return g


def _pid() -> Optional[int]:
    try:
        return launcher().running_pid(_state_dir())
    except CanaryError:
        return None


def start(ctx: Context) -> Dict[str, Any]:
    with _LOCK:
        if _pid():
            raise CanaryError("A test is already running.", "running")
        path = _write_config(settings(), ctx)
        nw = netwatch_api.get()
        turned_on = False
        if not nw.store.settings["enabled"]:
            nw.update_settings({"enabled": True})
            turned_on = True
        try:
            info = launcher().start_background(path, python=sys.executable)
        except Exception as exc:
            if turned_on:
                nw.update_settings({"enabled": False})
            raise CanaryError(f"The test did not start: {exc}", "start_failed") from exc
        _set_gui(started=time.time(), finished=None, netwatch_by_canary=turned_on, keep_running=bool(settings()["keep_running"]),
                 pid=info.get("pid"))
        _watch()
        return {"started": True, "pid": info.get("pid"), "netwatch_turned_on": turned_on, "state_dir": info.get("state_dir")}


def _watch() -> None:
    global _WATCHER
    if _WATCHER is not None and _WATCHER.is_alive():
        return

    def loop() -> None:
        while _pid():
            time.sleep(WATCH_S)
        sync()

    _WATCHER = threading.Thread(target=loop, daemon=True, name="canary-watch")
    _WATCHER.start()


def sync() -> None:
    """The test is over (it reached its time, it was stopped, or it died): what the page started with it is switched off."""
    with _LOCK:
        g = _gui()
        if not g.get("started") or g.get("finished") or _pid():
            return
        if g.get("netwatch_by_canary"):
            try:
                netwatch_api.get().update_settings({"enabled": False})
            except Exception:                                  # the monitor never keeps the test's end from being recorded
                pass
        _set_gui(finished=time.time(), netwatch_by_canary=False)


def stop() -> Dict[str, Any]:
    with _LOCK:
        if not _pid():
            sync()
            return {"stopping": False, "result": "not-running"}

    def run() -> None:
        try:
            launcher().stop(_state_dir(), timeout=STOP_WAIT_S)
        finally:
            sync()

    _spawn(run)
    return {"stopping": True}


def shutdown() -> None:
    """The server ends: a test that is not set to keep running ends with it, so no process is left behind."""
    if not _pid() or settings()["keep_running"]:
        return
    try:
        launcher().stop(_state_dir(), timeout=EXIT_WAIT_S)
    except Exception:
        pass
    sync()


def reattach() -> None:
    """After a restart of the server: a test that is still running is watched again, one that ended is closed."""
    try:
        if _pid():
            _watch()
        else:
            sync()
    except Exception:
        pass


# ---- reading ----
def _findings() -> List[Dict[str, Any]]:
    path = _state_dir() / "findings.jsonl"
    return lib("findings").read_findings(path) if path.exists() else []


def counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"high": 0, "medium": 0, "low": 0, "info": 0}
    for r in rows:
        out[r.get("severity") if r.get("severity") in out else "info"] += 1
    return out


def status(ctx: Context) -> Dict[str, Any]:
    sync()
    s = settings()
    out: Dict[str, Any] = {"addon": present(), "settings": s, "review": dict(_REVIEW), "port": ctx.port}
    if not out["addon"]:
        return out
    pid = _pid()
    st = launcher().read_status(_state_dir()) or {}
    g = _gui()
    rows = _findings()
    state = "idle"
    if pid:
        state = "stopping" if st.get("stop_requested") else st.get("state") or "running"
    elif g.get("started"):
        state = "stopped"
    nw = netwatch_api.get()
    cat = _catalog()
    out.update({
        "running": bool(pid), "pid": pid, "state": state, "started": g.get("started"), "finished": g.get("finished"),
        "sessions": st.get("sessions_this_run", 0), "monitor": st.get("monitor", ""), "findings": counts(rows), "findings_total": len(rows),
        "netwatch": {"enabled": bool(nw.store.settings["enabled"]), "by_canary": bool(g.get("netwatch_by_canary"))},
        "effective": effective(s, ctx),
        "runtimes": [{"id": r["id"], "name": r["name"]} for r in cat.runtimes.values()],
        "vendors": [{"id": v["id"], "name": v["name"]} for v in cat.vendors.values()],
        "harnesses": harnesses(),
        "duration_ends": (g["started"] + s["duration_minutes"] * 60) if g.get("started") and pid and s.get("duration_minutes") else None,
    })
    return out


def findings(query: Dict[str, str]) -> Dict[str, Any]:
    rows = _findings()
    reg = _gui().get("registered", {})
    sev = query.get("severity", "")
    try:
        since, limit = int(float(query.get("since", 0))), int(float(query.get("limit", 300)))
    except ValueError:
        raise CanaryError("since and limit are numbers.") from None
    shown = [dict(r, registered=reg.get(str(r.get("n")))) for r in rows if r.get("n", 0) > since and (not sev or r.get("severity") == sev)]
    return {"findings": shown[-limit:][::-1], "total": len(rows), "counts": counts(rows)}


def register(n: str) -> Dict[str, Any]:
    """A finding is copied to the security monitor's register as a manual report."""
    row = next((r for r in _findings() if str(r.get("n")) == str(n)), None)
    if row is None:
        raise CanaryError("No such finding.", "gone")
    ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    pick = lambda *keys: next((str(ev[k]) for k in keys if ev.get(k) not in (None, "")), "")
    tags = [t for t in (ev.get("tags") or []) if isinstance(t, str)]
    note = f"Canary finding #{row.get('n')} ({row.get('severity')}) {row.get('kind')}: " + json.dumps(ev, ensure_ascii=False, default=str)[:1500]
    made = netwatch_api.get().add_manual({"process": pick("process") or "canary", "proto": pick("proto") or "tcp", "remote_ip": pick("remote_ip", "ip"),
                                          "host": pick("host") or str(row.get("kind") or ""), "remote_port": pick("remote_port", "port") or 0,
                                          "tags": tags, "note": note})
    with _LOCK:
        reg = dict(_gui().get("registered", {}))
        reg[str(row.get("n"))] = made.get("id")
        _set_gui(registered=reg)
    return made


def verify() -> Dict[str, Any]:
    path = _state_dir() / "findings.jsonl"
    if not path.exists():
        return {"ok": True, "count": 0, "head": "", "message": "No findings yet."}
    r = lib("findings").verify_chain(path)
    return {"ok": r.ok, "count": r.count, "head": r.head, "message": r.message}


def report(generate: bool) -> Dict[str, Any]:
    folder = _state_dir()
    if generate:
        try:
            lib("report").write_report(folder)
        except Exception as exc:
            raise CanaryError(f"The report could not be written: {exc}") from exc
    md, js = folder / "report.md", folder / "report.json"
    if not md.is_file():
        return {"exists": False}
    text = md.read_text(encoding="utf-8", errors="replace")
    return {"exists": True, "markdown": text[:REPORT_MAX], "truncated": len(text) > REPORT_MAX, "generated": md.stat().st_mtime,
            "summary": _read(js, {}).get("counts") if js.is_file() else None}


def reviews() -> Dict[str, Any]:
    out = []
    for r in lib("reviewer").read_reviews(_state_dir()) if present() else []:
        out.append({"n": r.get("n"), "time": r.get("time"), "label": r.get("label"), "model": r.get("reviewer_model"),
                    "assessment": r.get("assessment"), "error": (r.get("reply") or {}).get("error")})
    return {"reviews": out[::-1], "state": dict(_REVIEW)}


def review(ctx: Context) -> Dict[str, Any]:
    """Asks the reviewer model for an opinion on the evidence so far. The model answers in the background; the page reads the result."""
    with _LOCK:
        if _REVIEW["state"] == "running":
            raise CanaryError("A review is already running.", "running")
        s = settings()
        if not s["reviewer"]["enabled"]:
            raise CanaryError("Turn on the reviewer in the form first and give it a model that is not the one under test.", "no_reviewer")
        path = _write_config(s, ctx)
        cfg = lib("config").load_config(path)
        rv = lib("reviewer").make_reviewer(cfg)
        _REVIEW.update(state="running", error="")

    def run() -> None:
        try:
            rec = rv.review(label="manual")
            err = (rec.get("reply") or {}).get("error") or ""
            _REVIEW.update(state="error" if err else "done", error=str(err), n=rec.get("n", 0))
        except Exception as exc:
            _REVIEW.update(state="error", error=str(exc))

    _spawn(run)
    return {"state": "running"}


def log_tail(lines: int = 80) -> Dict[str, Any]:
    p = _state_dir() / "canary.log"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"log": ""}
    return {"log": "\n".join(text.splitlines()[-lines:])}


# ---- the routes ----
def route_get(path: str, query: Dict[str, str], ctx: Context) -> Tuple[int, Dict[str, Any]]:
    if path == "/api/canary":
        return 200, status(ctx)
    if not present():
        raise CanaryError("The Openweight Canary add-on is not installed.", "no_addon")
    if path == "/api/canary/findings":
        return 200, findings(query)
    if path == "/api/canary/report":
        return 200, report(False)
    if path == "/api/canary/verify":
        return 200, verify()
    if path == "/api/canary/reviews":
        return 200, reviews()
    if path == "/api/canary/log":
        return 200, log_tail()
    return 404, {"error": {"type": "not_found", "message": "No such route."}}


def route_post(path: str, req: Dict[str, Any], ctx: Context) -> Tuple[int, Dict[str, Any]]:
    if path == "/api/canary/settings":
        save_settings(req)
        return 200, status(ctx)
    if not present():
        raise CanaryError("The Openweight Canary add-on is not installed.", "no_addon")
    if path == "/api/canary/start":
        if req:
            save_settings(req)
        return 200, start(ctx)
    if path == "/api/canary/stop":
        return 200, stop()
    if path == "/api/canary/report":
        return 200, report(True)
    if path == "/api/canary/review":
        return 200, review(ctx)
    parts = path.split("/")                                    # ['', 'api', 'canary', 'findings', n, 'register']
    if len(parts) == 6 and parts[3] == "findings" and parts[5] == "register":
        return 200, register(parts[4])
    return 404, {"error": {"type": "not_found", "message": "No such route."}}


def error_view(exc: CanaryError) -> Tuple[int, Dict[str, Any]]:
    status_code = 404 if exc.code == "gone" else 409 if exc.code == "running" else 400
    return status_code, {"error": {"type": "canary", "code": exc.code, "message": str(exc)}}
