"""The voice functions (speech to text, speech synthesis, avatar): loaded on request, unloaded on request, with the VRAM each one took.

`GET /functions` says, per function, its state (unloaded | loading | loaded | error), its model and placement, the VRAM it was
estimated to need (the server sends the estimate; it comes from the catalog or from an earlier measurement) and the VRAM it was
measured to take. `POST /functions/apply` loads what is on and unloads what is off, one function at a time. The free VRAM is read
before and after each step (`free_vram_mib`); a reading that cannot be made is `null`, never a guess. A model that is not installed
leaves its function unloaded with the plain `ModelNotInstalled` reason and the others still load.

Loads go through callbridge's VRAM arbiter (engines/vram.py), which lets one load run at a time. Nothing here imports torch, onnxruntime
or an audio library until a function is actually loaded."""
from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
import sys
import threading

from callbridge import models_store as ms

from voice import settings_bridge

log = logging.getLogger("voice.functions")

FUNCTIONS = ("stt", "tts", "avatar")
STATES = ("unloaded", "loading", "loaded", "error")
_DEVICE_RE = re.compile(r"^(auto|cpu|cuda:[0-9]{1,2})$")


class Busy(RuntimeError):
    """Another apply is running."""


class Suspended(RuntimeError):
    """The components are suspended: nothing may be loaded."""


# ------------------------------------------------------------------------------------------------ free VRAM
def _nvml_free_mib(gpu: int) -> int | None:
    import pynvml                                             # noqa: PLC0415 (absent on a PC without the package)
    pynvml.nvmlInit()
    try:
        info = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(gpu))
        return int(info.free) // (1 << 20)
    finally:
        pynvml.nvmlShutdown()


def _smi_free_mib(gpu: int) -> int | None:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits", "-i", str(gpu)],
                         capture_output=True, text=True, timeout=5, check=True).stdout.strip().splitlines()
    return int(out[0].strip()) if out else None


def free_vram_mib(gpu: int = 0) -> int | None:
    """The free VRAM of GPU `gpu` in MiB: pynvml when it imports, else `nvidia-smi`; None when neither can say."""
    for read in (_nvml_free_mib, _smi_free_mib):
        try:
            value = read(gpu)
        except Exception:                                     # noqa: BLE001 (a missing library, a driver that refuses, no nvidia-smi)
            continue
        if value is not None:
            return value
    return None


# ------------------------------------------------------------------------------------------------ the backends
def _engines():
    """callbridge.engines when something has already imported it, else None: a function can only be loaded once it has been."""
    return sys.modules.get("callbridge.engines")


class SttBackend:
    """The shared STT helper process (callbridge/engines/stt_shared.py), with the Whisper size of the config."""

    def __init__(self, config):
        self.config = config

    def load(self, choice) -> None:
        from callbridge import engines
        eng = engines._get_stt_engine(self.config)
        eng.reconfigure(self.config)
        eng.load()
        if getattr(eng, "state", "") == "error":
            raise RuntimeError(getattr(eng, "error", None) or "the STT did not load")

    def unload(self) -> None:
        engines = _engines()
        if engines is not None and engines._stt_engine is not None:
            engines._stt_engine.chiudi()
            engines._release_gpu_memory()

    def loaded(self) -> bool:
        engines = _engines()
        eng = engines._stt_engine if engines is not None else None
        return bool(eng is not None and eng.loaded)


class TtsBackend:
    """The TTS router (callbridge/engines/tts_router.py): the chosen engine is made active and warmed up with a short sentence."""

    def __init__(self, config):
        self.config = config

    def load(self, choice) -> None:
        from callbridge import engines
        from callbridge.engines import catalog
        router = engines._get_tts_engine(self.config)
        spec = catalog.spec(choice.engine)
        if spec is None:
            raise RuntimeError(f"unknown TTS engine: {choice.engine}")
        if not catalog.disponibile(self.config, spec, router):
            raise RuntimeError(catalog.modello_mancante(self.config, spec) or "the engine is not installed (its environment or helper is missing)")
        router.attiva(choice.engine, precarica=False)         # makes it the active engine and unloads the others
        router._precarica(choice.engine)                      # loads the weights now: the first synthesis does it otherwise
        state = router.stato_motori().get(choice.engine) or {}
        if state.get("state") != "ready":
            raise RuntimeError(state.get("message") or "the engine did not load")

    def unload(self) -> None:
        engines = _engines()
        if engines is not None and engines._tts_engine is not None:
            engines._tts_engine.scarica_tutti()
            engines._release_gpu_memory()

    def loaded(self) -> bool:
        engines = _engines()
        router = engines._tts_engine if engines is not None else None
        return bool(router is not None and any(s.get("state") == "ready" for s in router.stato_motori().values()))


class AvatarBackend:
    """The avatar worker (callbridge/avatar_client.py): started, the persona prepared when one is set, the window hidden."""

    def __init__(self, config, app=None):
        self.config, self.app = config, app

    def client(self):
        from callbridge.avatar_client import AvatarClient
        if self.app is None:
            raise RuntimeError("no app to hold the avatar client")
        client = self.app.config.get("CB_AVATAR")
        if client is None:
            client = self.app.config["CB_AVATAR"] = AvatarClient(self.config)
        return client

    def load(self, choice) -> None:
        avatar = self.client()
        persona = getattr(self.config, "avatar_persona", "")
        if persona:
            ok = avatar.start(persona)
            if ok:
                avatar.set_preview(False)
        else:
            with avatar._lock:
                ok = avatar._ensure_worker()
        if not ok:
            raise RuntimeError(avatar.last_error or "the avatar worker did not start")

    def unload(self) -> None:
        if self.app is not None and self.app.config.get("CB_AVATAR") is not None:
            self.app.config["CB_AVATAR"].stop()

    def loaded(self) -> bool:
        avatar = self.app.config.get("CB_AVATAR") if self.app is not None else None
        proc = getattr(avatar, "_proc", None)
        return bool(proc is not None and proc.poll() is None)


def _serialise(name: str):
    """callbridge's VRAM arbiter: one load at a time, the wait and the time held logged."""
    from callbridge.engines import vram
    return vram.caricamento(name)


# ------------------------------------------------------------------------------------------------ the manager
class Functions:
    """The state of the functions and the apply step. `backends` maps a function to an object with load(choice), unload() and loaded();
    `loading` is the context manager that serialises a load (default: callbridge's VRAM arbiter)."""

    def __init__(self, state, backends: dict | None = None, loading=None, app=None):
        self.state = state
        cfg = state.config
        self.backends = backends if backends is not None else {"stt": SttBackend(cfg), "tts": TtsBackend(cfg), "avatar": AvatarBackend(cfg, app)}
        self.loading = loading or _serialise
        self._busy = threading.Lock()
        self._rec: dict[str, dict] = {n: {"state": "unloaded", "error": "", "estimated_mib": None, "measured_mib": None, "applied": None,
                                         "loading": False} for n in FUNCTIONS}

    # ---- reading
    def _gpu(self, choice) -> int:
        return choice.cuda_index or 0

    def _free(self, gpu: int) -> int | None:
        return free_vram_mib(gpu)                             # looked up here so that a test can replace it

    def _live(self, name: str) -> str:
        rec = self._rec[name]
        if rec["loading"]:
            return "loading"
        try:
            on = bool(self.backends[name].loaded())
        except Exception:                                     # noqa: BLE001 (a backend that cannot tell is not loaded)
            on = False
        if on:
            return "loaded"
        return "error" if rec["state"] == "error" else "unloaded"

    def snapshot(self) -> dict:
        self.state.refresh()
        settings = self.state.settings
        out = {}
        for name in FUNCTIONS:
            c, rec = settings.services[name], self._rec[name]
            installed = ms.is_installed(c.model, settings.models_dir)
            entry = {"state": self._live(name), "model": c.model, "name": ms.MODELS[c.model][2] if c.model in ms.MODELS else "", "engine": c.engine,
                     "device": c.device, "memory": c.memory, "enabled": settings_bridge.is_enabled(c, settings.models_dir), "installed": installed,
                     "estimated_mib": rec["estimated_mib"], "measured_mib": rec["measured_mib"], "error": rec["error"] or None}
            if not installed:
                entry["reason"] = ms.reason(c.model, settings.models_dir)
            out[name] = entry
        return {"functions": out, "free_vram_mib": self._free(0)}

    # ---- the request
    def _choices(self, wanted) -> dict:
        """The request checked and merged over the settings of voice.json: {function: (choice, on, estimated_mib)}."""
        if not isinstance(wanted, dict) or not wanted:
            raise ValueError("send {\"functions\": {\"stt\": {\"on\": true}, ...}}")
        self.state.refresh()
        base = self.state.settings
        services, out = dict(base.services), {}
        for name, req in wanted.items():
            if name not in FUNCTIONS:
                raise ValueError(f"{name}: not a voice function (stt, tts or avatar)")
            if not isinstance(req, dict) or not isinstance(req.get("on"), bool):
                raise ValueError(f"{name}: on is true or false")
            c = services[name]
            model, device, memory = req.get("model", c.model), req.get("device", c.device), req.get("memory", c.memory)
            if model not in ms.MODELS or ms.MODELS[model][0] != name:
                raise ValueError(f"{name}: {model!r} is not a model of this function")
            if not (isinstance(device, str) and _DEVICE_RE.match(device)) or memory not in settings_bridge.MEMORY or (device == "cpu" and memory == "vram"):
                raise ValueError(f"{name}: bad placement {device!r} / {memory!r}")
            est = req.get("estimated_mib")
            if est is not None and not (isinstance(est, int) and not isinstance(est, bool) and est >= 0):
                raise ValueError(f"{name}: estimated_mib is a whole number of MiB or null")
            services[name] = dataclasses.replace(c, model=model, device=device, memory=memory)
            out[name] = (services[name], req["on"], est)
        merged = dataclasses.replace(base, services=services)
        settings_bridge.apply(self.state.config, merged)
        return out

    def apply(self, wanted) -> dict:
        """Unload what is off (and what changes model or placement), then load what is on, one function at a time.
        Returns {"report": {function: {before_free_mib, after_free_mib, measured_mib, state, error, action}}}."""
        choices = self._choices(wanted)
        if any(on for _c, on, _e in choices.values()):
            from callbridge import sospensione
            if sospensione.in_sospensione():
                raise Suspended(sospensione.MSG_SOSPESO)
        if not self._busy.acquire(blocking=False):
            raise Busy("a load of the voice functions is already running")
        try:
            report: dict[str, dict] = {}
            root = self.state.settings.models_dir
            for name in FUNCTIONS:                            # what leaves first: its memory is free before anything enters
                if name in choices:
                    c, on, _est = choices[name]
                    if self._live(name) == "loaded" and (not on or self._rec[name]["applied"] != (c.model, c.device, c.memory)):
                        report[name] = self._unload(name, c)
            for name in FUNCTIONS:
                if name not in choices:
                    continue
                c, on, est = choices[name]
                if on:
                    if self._live(name) == "loaded":
                        report.setdefault(name, self._entry(name, "keep", None, None, None))
                    else:
                        report[name] = self._load(name, c, est, root)
                else:
                    report.setdefault(name, self._entry(name, "keep", None, None, None))
            return {"report": report, "free_vram_mib": self._free(0)}
        finally:
            self._busy.release()

    def _entry(self, name, action, before, after, measured, error="") -> dict:
        return {"action": action, "before_free_mib": before, "after_free_mib": after, "measured_mib": measured, "state": self._live(name),
                "error": error or None}

    @staticmethod
    def _delta(before, after, sign):
        """The VRAM a step took (sign -1: free memory fell) or gave back (sign +1); None when it cannot be told or came out negative."""
        if before is None or after is None:
            return None
        d = (after - before) * sign
        return d if d >= 0 else None

    def _unload(self, name, choice) -> dict:
        gpu = self._gpu(choice)
        before = self._free(gpu)
        error = ""
        try:
            self.backends[name].unload()
        except Exception as e:                                # noqa: BLE001 (reported, the others go on)
            error = str(e) or e.__class__.__name__
            log.warning("unloading %s failed: %s", name, error)
        after = self._free(gpu)
        rec = self._rec[name]
        rec.update(state="error" if error else "unloaded", error=error, applied=None, measured_mib=None)
        return self._entry(name, "unload", before, after, self._delta(before, after, +1), error)

    def _load(self, name, choice, estimated, root) -> dict:
        rec = self._rec[name]
        rec["estimated_mib"] = estimated
        if not ms.is_installed(choice.model, root):
            reason = ms.reason(choice.model, root)
            rec.update(state="unloaded", error=reason, measured_mib=None, applied=None)
            return self._entry(name, "load", None, None, None, reason)
        gpu = self._gpu(choice)
        error = ""
        with self.loading(name):
            rec.update(loading=True, error="")
            before = self._free(gpu)
            try:
                self.backends[name].load(choice)
            except Exception as e:                            # noqa: BLE001 (a missing model, a helper that died: this function only)
                error = str(e) or e.__class__.__name__
                log.warning("loading %s failed: %s", name, error)
            after = self._free(gpu)
            rec["loading"] = False
        # a function on the CPU or in RAM takes no VRAM: nothing to measure
        measured = None if choice.on_cpu else self._delta(before, after, -1)
        if error:
            measured = None
            rec.update(state="error", error=error, measured_mib=None, applied=None)
        else:
            rec.update(state="loaded", error="", measured_mib=measured, applied=(choice.model, choice.device, choice.memory))
        return self._entry(name, "load", before, after, measured, error)

    # ---- always on
    def autostart(self) -> dict | None:
        """STT and dictation are not gated by the VRAM plan: the STT loads when the service starts if it is enabled and its model is installed."""
        from callbridge import sospensione
        self.state.refresh()
        c = self.state.settings.services["stt"]
        root = self.state.settings.models_dir
        if sospensione.in_sospensione() or not settings_bridge.is_enabled(c, root) or not ms.is_installed(c.model, root):
            return None
        try:
            return self.apply({"stt": {"on": True}})
        except (Busy, Suspended, ValueError) as e:
            log.info("STT not loaded at start: %s", e)
            return None


# ------------------------------------------------------------------------------------------------ the routes
def register(app, manager: Functions) -> None:
    """GET /functions and POST /functions/apply on the control app; its before_request already demands the bearer token."""
    from flask import jsonify, request

    @app.get("/functions")
    def functions_get():
        return jsonify(manager.snapshot())

    @app.post("/functions/apply")
    def functions_apply():
        body = request.get_json(silent=True)
        try:
            return jsonify(manager.apply(body.get("functions") if isinstance(body, dict) else None))
        except ValueError as e:
            return jsonify({"error": str(e), "code": "bad_request"}), 400
        except Busy as e:
            return jsonify({"error": str(e), "code": "busy"}), 409
        except Suspended as e:
            return jsonify({"error": str(e), "code": "suspended"}), 409
