"""The voice functions: the apply order, one load at a time, a failure that stays with its function, the VRAM measurement and its null path,
the offline rule, and the routes. Backends, the free-VRAM reading and the serialiser are stand-ins: no model is loaded, no GPU is read,
the network is blocked."""
import json
import socket
import sys
import threading
import types
import urllib.request
from contextlib import contextmanager

import pytest

from callbridge import models_store as ms
from voice import functions, service, settings_bridge

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


class Backend:
    """A function that loads and unloads on paper; every call goes to the shared log, and it holds `vram` MiB while loaded."""

    def __init__(self, name, log, vram=0, fail=None, pool=None):
        self.name, self.log, self.vram, self.fail, self.pool, self.on = name, log, vram, fail, pool, False

    def load(self, choice):
        self.log.append(("load", self.name, choice.model, choice.device, choice.memory))
        if self.fail:
            raise self.fail
        self.on = True
        if self.pool is not None:
            self.pool["free"] -= self.vram

    def unload(self):
        self.log.append(("unload", self.name))
        if self.on and self.pool is not None:
            self.pool["free"] += self.vram
        self.on = False

    def loaded(self):
        return self.on


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.models = tmp_path / "models"
        self.settings = tmp_path / "voice.json"
        self.settings.write_text(json.dumps({"models_dir": str(self.models)}), encoding="utf-8")
        self.app, self.state = service.build_app(self.settings, TOKEN)
        self.client = self.app.test_client()
        self.log: list = []
        self.pool = {"free": 20000}                      # the free VRAM of the fake GPU, MiB
        self.reads: list = []
        self.held = {"max": 0, "now": 0}

        def free(gpu=0):
            self.reads.append(gpu)
            return self.pool["free"]
        monkeypatch.setattr(functions, "free_vram_mib", free)
        self.backends = {"stt": Backend("stt", self.log, 700, pool=self.pool), "tts": Backend("tts", self.log, 1200, pool=self.pool),
                         "avatar": Backend("avatar", self.log, 7000, pool=self.pool)}
        rig = self

        @contextmanager
        def loading(name):
            rig.held["now"] += 1
            rig.held["max"] = max(rig.held["max"], rig.held["now"])
            rig.log.append(("enter", name))
            try:
                yield
            finally:
                rig.log.append(("exit", name))
                rig.held["now"] -= 1
        self.f = self.state.functions
        self.f.backends, self.f.loading = self.backends, loading

    def install(self, service_name, model_id, files=None):
        folder = self.models / service_name / model_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in files or ms.MODELS[model_id][3] or ("w.bin",):
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"x")
        if not ms.MODELS[model_id][3]:
            (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": "w.bin", "size": 1}]}), encoding="utf-8")

    def install_all(self):
        self.install("stt", "whisper-small")
        self.install("tts", "kokoro")
        self.install("avatar", "musetalk-v15")

    def write(self, **services):
        self.settings.write_text(json.dumps({"models_dir": str(self.models), "services": services}), encoding="utf-8")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


ALL_ON = {"stt": {"on": True}, "tts": {"on": True}, "avatar": {"on": True}}


def test_a_new_service_lists_the_three_functions_unloaded_with_the_free_vram(rig):
    snap = rig.f.snapshot()
    assert list(snap["functions"]) == ["stt", "tts", "avatar"] and snap["free_vram_mib"] == 20000
    for name, f in snap["functions"].items():
        assert f["state"] == "unloaded" and f["installed"] is False and f["reason"].endswith("install it from Settings > Voice.")
        assert f["measured_mib"] is None and f["estimated_mib"] is None and f["error"] is None
    assert snap["functions"]["stt"]["model"] == "whisper-small" and snap["functions"]["avatar"]["engine"] == "musetalk"


def test_apply_loads_in_order_one_at_a_time_through_the_serialiser(rig):
    rig.install_all()
    out = rig.f.apply(ALL_ON)
    loads = [e for e in rig.log if e[0] == "load"]
    assert [e[1] for e in loads] == ["stt", "tts", "avatar"]
    assert rig.held["max"] == 1                                       # never two loads inside the serialiser
    seq = [e for e in rig.log if e[0] in ("enter", "load", "exit")]
    assert seq == [("enter", "stt"), ("load", "stt", "whisper-small", "auto", "vram"), ("exit", "stt"), ("enter", "tts"),
                   ("load", "tts", "kokoro", "auto", "vram"), ("exit", "tts"), ("enter", "avatar"),
                   ("load", "avatar", "musetalk-v15", "auto", "vram"), ("exit", "avatar")]
    r = out["report"]
    assert {n: r[n]["state"] for n in r} == {"stt": "loaded", "tts": "loaded", "avatar": "loaded"}
    assert (r["stt"]["before_free_mib"], r["stt"]["after_free_mib"], r["stt"]["measured_mib"]) == (20000, 19300, 700)
    assert (r["tts"]["measured_mib"], r["avatar"]["measured_mib"]) == (1200, 7000)
    assert out["free_vram_mib"] == 20000 - 700 - 1200 - 7000
    snap = rig.f.snapshot()["functions"]
    assert snap["avatar"]["state"] == "loaded" and snap["avatar"]["measured_mib"] == 7000


def test_what_is_off_leaves_before_anything_enters(rig):
    rig.install_all()
    rig.f.apply(ALL_ON)
    rig.log.clear()
    out = rig.f.apply({"stt": {"on": True}, "tts": {"on": False}, "avatar": {"on": False}})
    assert [e for e in rig.log if e[0] in ("load", "unload")] == [("unload", "tts"), ("unload", "avatar")]
    r = out["report"]
    assert r["tts"]["action"] == "unload" and r["tts"]["measured_mib"] == 1200 and r["tts"]["state"] == "unloaded"
    assert r["avatar"]["measured_mib"] == 7000 and r["stt"]["action"] == "keep"


def test_a_changed_model_or_placement_unloads_then_loads_again(rig):
    rig.install_all()
    rig.install("stt", "whisper-tiny")
    rig.f.apply({"stt": {"on": True}})
    rig.log.clear()
    rig.f.apply({"stt": {"on": True, "model": "whisper-tiny", "device": "cuda:1", "memory": "vram"}})
    assert [e[:2] for e in rig.log if e[0] in ("load", "unload")] == [("unload", "stt"), ("load", "stt")]
    assert ("load", "stt", "whisper-tiny", "cuda:1", "vram") in rig.log
    assert rig.state.config.stt_model == "tiny"                       # the request reached the config the helper reads
    assert 1 in rig.reads                                             # the free VRAM of the chosen GPU was read


def test_a_function_that_fails_stays_with_its_own_error(rig):
    rig.install_all()
    rig.backends["tts"].fail = RuntimeError("the helper died")
    out = rig.f.apply(ALL_ON)["report"]
    assert out["tts"]["state"] == "error" and out["tts"]["error"] == "the helper died" and out["tts"]["measured_mib"] is None
    assert out["stt"]["state"] == "loaded" and out["avatar"]["state"] == "loaded"
    assert [e[1] for e in rig.log if e[0] == "load"] == ["stt", "tts", "avatar"]        # the next one still loaded
    snap = rig.f.snapshot()["functions"]
    assert snap["tts"]["state"] == "error" and snap["tts"]["error"] == "the helper died"
    rig.backends["tts"].fail = None
    assert rig.f.apply({"tts": {"on": True}})["report"]["tts"]["state"] == "loaded"      # a later apply tries again


def test_a_failing_unload_is_reported_and_the_others_go_on(rig):
    rig.install_all()
    rig.f.apply(ALL_ON)

    def boom():
        raise OSError("still releasing")
    rig.backends["tts"].unload = boom
    out = rig.f.apply({"tts": {"on": False}, "avatar": {"on": False}})["report"]
    assert out["tts"]["error"] == "still releasing" and out["tts"]["state"] == "loaded"       # it is still in memory
    assert out["avatar"]["state"] == "unloaded" and out["avatar"]["error"] is None


def test_the_measurement_is_null_when_it_cannot_be_made(rig, monkeypatch):
    rig.install_all()
    monkeypatch.setattr(functions, "free_vram_mib", lambda gpu=0: None)
    out = rig.f.apply(ALL_ON)
    for name, r in out["report"].items():
        assert r["before_free_mib"] is None and r["after_free_mib"] is None and r["measured_mib"] is None and r["state"] == "loaded", name
    assert out["free_vram_mib"] is None and rig.f.snapshot()["free_vram_mib"] is None
    assert rig.f.snapshot()["functions"]["stt"]["measured_mib"] is None


def test_the_measurement_is_null_for_the_cpu_and_for_a_reading_that_went_up(rig):
    rig.install_all()
    rig.backends["stt"].vram = 500
    out = rig.f.apply({"stt": {"on": True, "device": "cpu", "memory": "ram"}})["report"]["stt"]
    assert out["measured_mib"] is None and out["state"] == "loaded"                  # on the CPU: no VRAM is taken, none is claimed
    rig.backends["tts"].vram = -300                                                  # another program freed memory meanwhile
    assert rig.f.apply({"tts": {"on": True}})["report"]["tts"]["measured_mib"] is None


def test_the_estimate_sent_by_the_server_is_kept_beside_the_measurement(rig):
    rig.install_all()
    rig.f.apply({"stt": {"on": True, "estimated_mib": 900}})
    f = rig.f.snapshot()["functions"]["stt"]
    assert (f["estimated_mib"], f["measured_mib"]) == (900, 700)


def test_a_missing_model_leaves_that_function_unloaded_with_the_plain_reason_and_the_others_load(rig):
    rig.install("stt", "whisper-small")
    rig.install("avatar", "musetalk-v15")
    out = rig.f.apply(ALL_ON)["report"]
    assert out["tts"]["state"] == "unloaded" and out["tts"]["error"] == "Kokoro is not installed: install it from Settings > Voice."
    assert out["tts"]["measured_mib"] is None
    assert out["stt"]["state"] == "loaded" and out["avatar"]["state"] == "loaded"
    assert ("load", "tts") not in [e[:2] for e in rig.log]                           # nothing was attempted, nothing fetched
    assert rig.f.snapshot()["functions"]["tts"]["error"] == out["tts"]["error"]


def test_apply_refuses_what_it_does_not_understand(rig):
    for bad in (None, {}, [], {"nope": {"on": True}}, {"stt": {"on": "yes"}}, {"stt": "on"}, {"stt": {"on": True, "model": "kokoro"}},
                {"stt": {"on": True, "device": "tpu"}}, {"stt": {"on": True, "device": "cpu", "memory": "vram"}},
                {"stt": {"on": True, "estimated_mib": -1}}, {"stt": {"on": True, "estimated_mib": True}}):
        with pytest.raises(ValueError):
            rig.f.apply(bad)
    assert rig.log == []


def test_a_second_apply_while_one_runs_is_refused(rig):
    rig.install_all()
    inside, release = threading.Event(), threading.Event()
    real = rig.backends["stt"].load

    def slow(choice):
        inside.set()
        release.wait(5)
        real(choice)
    rig.backends["stt"].load = slow
    t = threading.Thread(target=rig.f.apply, args=({"stt": {"on": True}},))
    t.start()
    assert inside.wait(5)
    r = rig.client.post("/functions/apply", headers=AUTH, json={"functions": {"tts": {"on": True}}})
    assert r.status_code == 409 and r.get_json()["code"] == "busy"
    release.set()
    t.join(5)
    assert [e for e in rig.log if e[0] == "load"] == [("load", "stt", "whisper-small", "auto", "vram")]


def test_nothing_is_loaded_while_the_components_are_suspended(rig):
    rig.install_all()
    from callbridge import sospensione
    sospensione.SOSPENSIONE_FILE.write_text("1")                      # the conftest points this at a temporary file
    r = rig.client.post("/functions/apply", headers=AUTH, json={"functions": ALL_ON})
    assert r.status_code == 409 and r.get_json()["code"] == "suspended" and rig.log == []
    assert rig.client.post("/functions/apply", headers=AUTH, json={"functions": {"tts": {"on": False}}}).status_code == 200


def test_the_routes_need_the_token_and_speak_json(rig):
    rig.install_all()
    assert rig.client.get("/functions").status_code == 401
    assert rig.client.post("/functions/apply", json={"functions": ALL_ON}).status_code == 401
    g = rig.client.get("/functions", headers=AUTH)
    assert g.status_code == 200 and sorted(g.get_json()["functions"]) == ["avatar", "stt", "tts"]
    r = rig.client.post("/functions/apply", headers=AUTH, json={"functions": ALL_ON})
    assert r.status_code == 200 and r.get_json()["report"]["avatar"]["state"] == "loaded"
    assert rig.client.post("/functions/apply", headers=AUTH, json={"functions": {"x": {"on": True}}}).status_code == 400
    assert rig.client.post("/functions/apply", headers=AUTH, data="not json").status_code == 400


def test_enabled_defaults_to_true_for_an_installed_stt_only(rig):
    s = settings_bridge.load(rig.settings)
    on = lambda s: [n for n, c in s.services.items() if settings_bridge.is_enabled(c, s.models_dir)]       # noqa: E731
    assert on(s) == []                                                # nothing installed: everything off
    rig.install_all()
    assert on(s) == ["stt"]                                           # the same settings, read after the install
    rig.write(stt={"enabled": False}, tts={"enabled": True}, avatar={"enabled": "yes"})
    s = settings_bridge.load(rig.settings)
    assert (s.services["stt"].enabled, s.services["tts"].enabled, s.services["avatar"].enabled) == (False, True, None)
    assert on(s) == ["tts"]
    assert rig.f.snapshot()["functions"]["tts"]["enabled"] is True


def test_the_stt_loads_at_start_when_it_is_enabled_and_installed_and_nothing_else_does(rig):
    assert rig.f.autostart() is None and rig.log == []                # not installed: nothing
    rig.install_all()
    out = rig.f.autostart()
    assert out["report"]["stt"]["state"] == "loaded"
    assert [e[:2] for e in rig.log if e[0] == "load"] == [("load", "stt")]
    rig.log.clear()
    rig.write(stt={"enabled": False})
    assert rig.f.autostart() is None and rig.log == []                # switched off in the settings


def test_the_serialiser_is_callbridges_vram_arbiter(rig):
    from callbridge.engines import vram
    ctx = functions._serialise("stt")
    assert type(ctx).__name__ == type(vram.caricamento("x")).__name__
    with ctx:
        pass


def test_free_vram_reads_pynvml_then_nvidia_smi_then_says_none(monkeypatch):
    class Mem:
        free = 5 * (1 << 20) * 1000

    nv = types.SimpleNamespace(nvmlInit=lambda: None, nvmlShutdown=lambda: None, nvmlDeviceGetHandleByIndex=lambda i: ("h", i),
                               nvmlDeviceGetMemoryInfo=lambda h: Mem)
    monkeypatch.setitem(sys.modules, "pynvml", nv)
    assert functions.free_vram_mib(1) == 5000
    monkeypatch.setitem(sys.modules, "pynvml", None)                  # not importable
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout="12345\n")
    monkeypatch.setattr(functions.subprocess, "run", run)
    assert functions.free_vram_mib(2) == 12345
    assert calls[0][:3] == ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"] and calls[0][-2:] == ["-i", "2"]

    def missing(cmd, **kw):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(functions.subprocess, "run", missing)
    assert functions.free_vram_mib() is None
    monkeypatch.setattr(functions.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(stdout=""))
    assert functions.free_vram_mib() is None


def test_asking_for_the_functions_imports_no_heavy_library(tmp_path):
    import subprocess
    from pathlib import Path
    code = ("import sys, json; from voice import service, functions; functions.free_vram_mib = lambda gpu=0: None; "
            "app, st = service.build_app(sys.argv[1], 't'); app.test_client().get('/functions', headers={'Authorization': 'Bearer t'}); "
            "print(json.dumps([m for m in ('torch', 'onnxruntime', 'pynvml', 'sounddevice', 'whisper') if m in sys.modules]))")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path / "voice.json")], cwd=str(Path(__file__).resolve().parents[2]),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
