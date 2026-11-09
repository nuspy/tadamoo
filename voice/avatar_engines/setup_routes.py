"""The routes behind the Set up panel of an engine card (Settings > Voice): what the engine needs on this PC item by item, the explicit check that loads it once,
and the Python of the engine environment.

    GET  /avatar/engines/<engine>/setup    {engine, name, python: {path, source}, checks: [{id, ok, en, it, model?, scope?}], check: {state, phase, percent, error}, ready}
    POST /avatar/engines/<engine>/check    load the engine once in its environment (a thread; the state is in `check`); 409 while another engine process runs
    POST /avatar/engines/python {path}     keep the Python of the engine environment ("" forgets it); the variable TADAMOO_AVATAR_ENGINES_PYTHON wins when set

Installing the files is the server's download flow (plan, confirm, SHA-256), which the panel calls for the `model` of an item; training an ER-NeRF is
`/avatar/train/*`. Nothing here starts an engine except the check the person asks for."""
from __future__ import annotations

import threading
import time

from flask import jsonify, request

from . import pack, registry


def _err(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def register(app) -> None:
    jobs: dict[str, dict] = {}
    lock = threading.Lock()

    def engine_of(engine_id):
        found = registry.get(engine_id)
        return found if isinstance(found, pack.PackEngine) else None

    def view(engine) -> dict:
        checks = engine.setup_checks()
        state = jobs.get(engine.ID) or {"state": "idle"}
        return {"engine": engine.ID, "name": engine.info.name, "python": {"path": pack.python(), "source": pack.python_source()}, "checks": checks,
                "check": {k: state.get(k) for k in ("state", "phase", "percent", "error") if k in state},
                "ready": all(c["ok"] for c in checks if c.get("scope") != "training" and c["id"] != "check")}

    @app.get("/avatar/engines/<engine_id>/setup")
    def engine_setup(engine_id):
        engine = engine_of(engine_id)
        if engine is None:
            return _err("this engine has no setup here", "not_found", 404)
        return jsonify(view(engine))

    @app.post("/avatar/engines/<engine_id>/check")
    def engine_check(engine_id):
        engine = engine_of(engine_id)
        if engine is None:
            return _err("this engine has no setup here", "not_found", 404)
        with lock:
            if any(j.get("state") == "running" for j in jobs.values()):
                return _err("a check is already running", "busy", 409)
            problems = pack.environment_problems() + pack.not_installed(engine.models)
            if problems:
                return _err("; ".join(p[0] for p in problems), "not_ready", 409)
            jobs[engine.ID] = state = {"state": "running", "phase": "loading", "percent": 0.0, "started": time.time()}

        def progress(phase: str, percent: float) -> None:
            state.update(phase=phase, percent=percent)

        def run() -> None:
            try:
                engine.check_environment(progress)
                state.update(state="done", percent=100.0, error="")
            except (pack.EngineFailed, pack.EngineNotReady, pack.EngineBusy) as e:
                state.update(state="failed", error=str(e))
            except Exception as e:                                           # noqa: BLE001 (a thread that dies unseen would leave the panel waiting)
                state.update(state="failed", error=f"{type(e).__name__}: {e}")

        threading.Thread(target=run, name="avatar-engine-check", daemon=True).start()
        return jsonify(view(engine))

    @app.post("/avatar/engines/python")
    def engines_python():
        body = request.get_json(silent=True)
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str):
            return _err("send the path of the Python, or an empty text to forget it", "bad_request", 400)
        try:
            pack.set_python(path)
        except ValueError as e:
            return _err(str(e), "bad_request", 400)
        return jsonify({"python": {"path": pack.python(), "source": pack.python_source()}})
