"""The TTS engines (the parts that run in the service) read from the models folder, report a missing model plainly, and follow the choice
made in voice.json: the engine, the placement and the GPU. Helper processes are replaced by a fake `Popen`; no model, GPU or audio is used."""
import io
import json
import socket
import subprocess
import urllib.request
from types import SimpleNamespace

import pytest

from callbridge import engines, models_store as ms
from callbridge.config import Config
from callbridge.engines import catalog, tts_router
from callbridge.engines.chatterbox_tts import ChatterboxTtsEngine
from callbridge.engines.venv_tts import VenvTtsEngine
from voice import service, settings_bridge


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV, "HF_HUB_CACHE", "TTS_MODELS_DIR"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)        # test_memoria sets it to "" for the whole run
    monkeypatch.setattr(ms, "_service_env", {})
    monkeypatch.setattr(catalog, "_xtts_cache", None)
    monkeypatch.setattr(engines, "_tts_engine", None)


class FakeProc:
    launches: list = []

    def __init__(self, args, **kw):
        FakeProc.launches.append({"args": args, **kw})
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def popen(monkeypatch):
    """Helper processes of the TTS engines are faked; any other process (a thread of another test may start one) is left alone."""
    FakeProc.launches = []
    real = subprocess.Popen

    def start(args, **kw):
        if any(str(a).endswith("_tts_helper.py") for a in args):
            return FakeProc(args, **kw)
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "Popen", start)
    return FakeProc


def write_settings(path, models, tts=None):
    path.write_text(json.dumps({"models_dir": str(models), "services": {"tts": tts or {}}}), encoding="utf-8")


def configure(tmp_path, **tts):
    """A `Config` that has the settings of a voice.json applied; returns (config, models folder)."""
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    settings = tmp_path / "voice.json"
    write_settings(settings, models, tts)
    cfg = Config()
    cfg.tts_venvs_dir = str(tmp_path / "venvs")
    settings_bridge.apply(cfg, settings_bridge.load(settings))
    return cfg, models


def install(models, service_name, model_id, files=None):
    folder = models / service_name / model_id
    folder.mkdir(parents=True, exist_ok=True)
    files = files or {"config.json": 2}
    for name, size in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x" * size)
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": n, "size": s} for n, s in files.items()]}), encoding="utf-8")
    return folder


def make_venv(cfg, engine_id):
    py = catalog.venv_python(cfg, catalog.spec(engine_id))
    import os
    os.makedirs(os.path.dirname(py), exist_ok=True)
    open(py, "wb").close()


# --- a missing model stops the engine before a helper process starts --------------------------------

@pytest.mark.parametrize("engine_id,model", [("xtts", "xtts-v2"), ("cosyvoice", "cosyvoice-3"), ("qwen3tts", "qwen3tts-0.6b"),
                                              ("orpheus", "orpheus-3b"), ("dia", "dia-1.6b")])
def test_a_venv_engine_without_its_model_never_starts_a_helper(engine_id, model, tmp_path, popen):
    cfg, models = configure(tmp_path)
    make_venv(cfg, engine_id)
    eng = VenvTtsEngine(catalog.spec(engine_id), cfg)
    for call in (eng.load, lambda: eng.synth("hello", "v", "en", 1.0), lambda: list(eng.synth_stream("hello", "v", "en", 1.0))):
        with pytest.raises(ms.ModelNotInstalled) as e:
            call()
        assert str(e.value) == f"{ms.MODELS[model][2]} is not installed: install it from Settings > Voice."
    assert popen.launches == []


def test_chatterbox_without_its_model_never_starts_a_helper(tmp_path, popen):
    cfg, models = configure(tmp_path)
    py = tmp_path / "py.exe"
    py.write_bytes(b"")
    eng = ChatterboxTtsEngine(str(py), device="cuda")
    with pytest.raises(ms.ModelNotInstalled):
        eng.load()
    assert popen.launches == []


def test_qwen3tts_asks_for_the_size_that_is_chosen(tmp_path, popen):
    cfg, models = configure(tmp_path, selected="qwen3tts-1.7b")
    make_venv(cfg, "qwen3tts")
    install(models, "tts", "qwen3tts-0.6b")
    eng = VenvTtsEngine(catalog.spec("qwen3tts"), cfg)
    with pytest.raises(ms.ModelNotInstalled) as e:
        eng.load()
    assert "Qwen3-TTS 1.7B is not installed" in str(e.value)
    install(models, "tts", "qwen3tts-1.7b")
    eng.load()
    assert popen.launches[0]["env"]["QWEN3TTS_SIZE"] == "1.7B"


# --- the helper's environment ----------------------------------------------------------------------------

def test_the_helper_environment_names_the_models_folder_and_nothing_else(tmp_path, popen, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")                      # a value from outside must not survive
    cfg, models = configure(tmp_path, selected="xtts-v2")
    make_venv(cfg, "xtts")
    install(models, "tts", "xtts-v2")
    eng = VenvTtsEngine(catalog.spec("xtts"), cfg)
    eng.load()
    env = popen.launches[0]["env"]
    assert env[ms.ROOT_ENV] == str(models)
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == env["HF_DATASETS_OFFLINE"] == "1"
    assert env["TTS_VENVS_DIR"] == str(tmp_path / "venvs")
    for gone in ("HF_HUB_CACHE", "TTS_MODELS_DIR", "KOKORO_MODEL_DIR"):
        assert not env.get(gone), gone


def test_the_chosen_gpu_and_placement_reach_the_helper(tmp_path, popen):
    cfg, models = configure(tmp_path, selected="xtts-v2", device="cuda:1", memory="vram")
    make_venv(cfg, "xtts")
    install(models, "tts", "xtts-v2")
    VenvTtsEngine(catalog.spec("xtts"), cfg).load()
    env = popen.launches[0]["env"]
    assert env["CUDA_VISIBLE_DEVICES"] == "1" and env["TTS_DEVICE"] == "cuda"


@pytest.mark.parametrize("tts", [{"device": "cpu", "memory": "ram"}, {"device": "cuda:1", "memory": "ram"}, {"device": "auto", "memory": "ram"}])
def test_ram_or_cpu_runs_the_helper_on_the_cpu_without_a_gpu_choice(tts, tmp_path, popen):
    cfg, models = configure(tmp_path, selected="orpheus-3b", **tts)
    make_venv(cfg, "orpheus")
    install(models, "tts", "orpheus-3b")
    VenvTtsEngine(catalog.spec("orpheus"), cfg).load()
    env = popen.launches[0]["env"]
    assert env["TTS_DEVICE"] == "cpu" and "CUDA_VISIBLE_DEVICES" not in env


def test_kokoro_helper_gets_the_chosen_gpu_too(tmp_path, popen):
    from callbridge.engines.kokoro_tts import KokoroTtsEngine
    cfg, models = configure(tmp_path, selected="kokoro", device="cuda:2", memory="vram")
    install(models, "tts", "kokoro", {"kokoro-v1.0.fp16.onnx": 3, "voices-v1.0.bin": 3})
    eng = KokoroTtsEngine(cfg.kokoro_model_dir, cfg.tts_device, cfg.tts_gpu_memory)
    eng.load()
    env = popen.launches[0]["env"]
    assert env["CUDA_VISIBLE_DEVICES"] == "2" and env["KOKORO_DEVICE"] == "cuda"
    assert env["KOKORO_MODEL_DIR"] == str(models / "tts" / "kokoro")


# --- the catalog and the router -----------------------------------------------------------------------------

def test_the_engine_catalog_says_which_model_each_engine_needs(tmp_path):
    cfg, models = configure(tmp_path)
    ids = {s.id: catalog.model_id(cfg, s) for s in catalog.SPECS}
    assert ids == {"kokoro": "kokoro", "piper": "piper", "chatterbox": "chatterbox", "xtts": "xtts-v2", "cosyvoice": "cosyvoice-3",
                   "qwen3tts": "qwen3tts-0.6b", "orpheus": "orpheus-3b", "dia": "dia-1.6b"}
    assert all(i in ms.MODELS and ms.MODELS[i][0] == "tts" for i in ids.values())
    cfg.qwen3tts_size = "1.7B"
    assert catalog.model_id(cfg, catalog.spec("qwen3tts")) == "qwen3tts-1.7b"


def test_an_engine_is_available_only_with_its_model(tmp_path):
    cfg, models = configure(tmp_path)
    make_venv(cfg, "xtts")
    xtts, kokoro = catalog.spec("xtts"), catalog.spec("kokoro")
    assert catalog.disponibile(cfg, xtts) is False and catalog.disponibile(cfg, kokoro) is False
    install(models, "tts", "xtts-v2")
    install(models, "tts", "kokoro", {"kokoro-v1.0.fp16.onnx": 3, "voices-v1.0.bin": 3})
    assert catalog.disponibile(cfg, xtts) is True and catalog.disponibile(cfg, kokoro) is True
    rows = {m["id"]: m for m in catalog.elenca(cfg, con_voci=False)}
    assert rows["xtts"]["is_downloaded"] and rows["xtts"]["reason"] == ""
    assert rows["dia"]["reason"] == "Dia 1.6B is not installed: install it from Settings > Voice."
    assert "ggml" not in rows["qwen3tts"]


def test_the_router_does_not_fall_back_when_a_model_is_missing(tmp_path, popen):
    cfg, models = configure(tmp_path)
    make_venv(cfg, "xtts")
    router = tts_router.TtsRouter(cfg)
    with pytest.raises(ms.ModelNotInstalled) as e:
        router.synth("hello", "anything", "en", 1.0, engine="xtts")
    assert "XTTS v2 is not installed" in str(e.value)
    with pytest.raises(ms.ModelNotInstalled) as e:
        router.synth("hello", "ryan", "en", 1.0)                       # a preset voice of Qwen3-TTS
    assert "Qwen3-TTS 0.6B is not installed" in str(e.value)
    assert popen.launches == []


def test_activating_an_engine_whose_model_is_missing_says_so(tmp_path):
    cfg, models = configure(tmp_path)
    router = tts_router.TtsRouter(cfg)
    info = router.attiva("dia")
    assert info["warming"] is False and info["message"] == "Dia 1.6B is not installed: install it from Settings > Voice."
    assert router.stato_motori()["dia"]["state"] == "error"


def test_the_selected_engine_is_the_active_one_and_speaks_without_a_voice(tmp_path, popen):
    cfg, models = configure(tmp_path, selected="xtts-v2")
    router = tts_router.TtsRouter(cfg)
    assert cfg.tts_engine == "xtts" and router.attivo == "xtts"
    with pytest.raises(ms.ModelNotInstalled) as e:
        router.synth("hello", "", "en", 1.0)                          # no voice and no engine: the chosen engine
    assert "XTTS v2" in str(e.value)


def test_a_new_choice_in_voice_json_reaches_a_router_that_exists(tmp_path):
    models = tmp_path / "models"
    settings = tmp_path / "voice.json"
    write_settings(settings, models, {"selected": "kokoro"})
    app, state = service.build_app(settings, "token")
    router = tts_router.TtsRouter(state.config)
    engines._tts_engine = router
    venv_engine = router._get_venv_engine("orpheus")
    assert router.attivo == "kokoro" and venv_engine.device == "cuda"

    write_settings(settings, models, {"selected": "orpheus-3b", "device": "auto", "memory": "vram"})
    state.refresh()
    assert router.attivo == "orpheus" and state.config.tts_engine == "orpheus"
    assert router._venv_engines["orpheus"] is venv_engine and venv_engine.device == "cuda"

    write_settings(settings, models, {"selected": "orpheus-3b", "device": "auto", "memory": "ram"})
    state.refresh()
    assert router._venv_engines["orpheus"] is venv_engine and venv_engine.device == "cpu"      # kept, told to restart with the new placement

    write_settings(settings, models, {"selected": "orpheus-3b", "device": "cuda:1", "memory": "vram"})
    state.refresh()
    assert ms.helper_env("tts")["CUDA_VISIBLE_DEVICES"] == "1"
    assert "orpheus" not in router._venv_engines                                              # the GPU is given at start: its helper was closed


def test_a_new_models_folder_reaches_a_router_that_exists(tmp_path):
    settings = tmp_path / "voice.json"
    write_settings(settings, tmp_path / "one", {})
    app, state = service.build_app(settings, "token")
    router = tts_router.TtsRouter(state.config)
    engines._tts_engine = router
    assert router._kokoro.model_dir == str(tmp_path / "one" / "tts" / "kokoro")
    write_settings(settings, tmp_path / "two", {})
    state.refresh()
    assert router._kokoro.model_dir == str(tmp_path / "two" / "tts" / "kokoro")
    assert router._piper.model_dir == str(tmp_path / "two" / "tts" / "piper")
    assert ms.voices_dir() == str(tmp_path / "two" / "tts" / "voices")


def test_the_gpu_snapshot_tells_what_changed(tmp_path):
    cfg, models = configure(tmp_path)
    before = settings_bridge.tts_snapshot(cfg)
    settings = tmp_path / "voice.json"
    write_settings(settings, models, {"selected": "dia-1.6b", "device": "cpu", "memory": "ram"})
    settings_bridge.apply(cfg, settings_bridge.load(settings))
    changed, gpu = settings_bridge.tts_changes(before, settings_bridge.tts_snapshot(cfg))
    assert changed == {"dia"} and gpu is False


# --- the status of the service ----------------------------------------------------------------------------------

def test_status_reports_the_energy_detector_as_degraded_when_silero_is_missing(tmp_path):
    settings = tmp_path / "voice.json"
    models = tmp_path / "models"
    write_settings(settings, models, {})
    app, state = service.build_app(settings, "token")
    vad = state.status()["services"]["vad"]
    assert vad["installed"] is False and vad["degraded"] is True
    assert vad["degraded_reason"] == ("Silero VAD v4 is not installed: install it from Settings > Voice. "
                                      "Speech detection uses the energy detector instead.")
    install(models, "vad", "silero-vad-v4", {"silero_vad_v4.onnx": 4})
    vad = state.status()["services"]["vad"]
    assert vad["installed"] is True and "degraded" not in vad


def test_status_names_the_missing_tts_model(tmp_path):
    settings = tmp_path / "voice.json"
    write_settings(settings, tmp_path / "models", {"selected": "xtts-v2"})
    app, state = service.build_app(settings, "token")
    tts = state.status()["services"]["tts"]
    assert tts["installed"] is False and tts["reason"] == "XTTS v2 is not installed: install it from Settings > Voice."
