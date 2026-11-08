"""The voice service in-process: standby, token, status, models only from the models folder and never fetched (the network is blocked)."""
import json
import os
import socket
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest

from callbridge import models_store as ms
from voice import service

REPO = Path(__file__).resolve().parents[2]
TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture(autouse=True)
def _no_network_no_leaks(monkeypatch):
    """Any attempt to reach the network fails the test; the process environment the service sets is restored afterwards."""
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(ms, "_service_env", {})


@pytest.fixture
def env(tmp_path):
    settings = tmp_path / "voice.json"
    models = tmp_path / "models"
    settings.write_text(json.dumps({"models_dir": str(models)}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    return types.SimpleNamespace(app=app, state=state, client=app.test_client(), settings=settings, models=models)


def _status(env):
    r = env.client.get("/status", headers=AUTH)
    assert r.status_code == 200
    return r.get_json()


def _install(models, service_name, model_id, files):
    folder = models / service_name / model_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in files.items():
        (folder / name).write_bytes(b"x" * size)
    return folder


def test_health_is_open_and_the_other_routes_need_the_token(env):
    assert env.client.get("/health").get_json() == {"status": "ok"}
    assert env.client.get("/status").status_code == 401
    assert env.client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert env.client.get("/brain/state").status_code == 401
    assert env.client.get("/status", headers=AUTH).status_code == 200


def test_standby_with_nothing_installed(env):
    st = _status(env)
    assert st["state"] == "standby" and st["pid"] == os.getpid() and st["models_dir"] == str(env.models)
    assert sorted(st["services"]) == sorted(["stt", "tts", "avatar", "diarization", "vad", "embedding"])
    for name, s in st["services"].items():
        assert s["installed"] is False, name
        assert s["reason"]
    stt = st["services"]["stt"]
    assert (stt["model"], stt["engine"], stt["device"], stt["memory"]) == ("whisper-small", "whisper", "auto", "vram")
    assert stt["reason"] == "Whisper small is not installed: install it from Settings > Voice."
    assert st["services"]["embedding"]["reason"] == "this service has no model in the catalog yet"
    assert st["offline"] is True
    assert not env.models.exists()                      # status creates nothing


def test_building_the_app_opens_no_hardware_and_imports_no_engine(tmp_path):
    """In a fresh interpreter: no audio library, no torch, no onnxruntime, no engine after the app is built and asked for its status."""
    code = ("import sys, json; from voice import service; app, st = service.build_app(sys.argv[1], 't'); "
            "app.test_client().get('/status', headers={'Authorization': 'Bearer t'}); "
            "print(json.dumps([m for m in ('sounddevice', 'torch', 'onnxruntime', 'whisper', 'kokoro_onnx', 'piper', 'sherpa_onnx', 'soundcard') if m in sys.modules]))")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path / "voice.json")], cwd=str(REPO), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def test_the_installed_marker_flips_installed(env):
    folder = _install(env.models, "stt", "whisper-small", {"small.pt": 10})
    assert _status(env)["services"]["stt"]["installed"] is True       # a hand-placed model: its probe file
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": "small.pt", "size": 10}]}), encoding="utf-8")
    assert _status(env)["services"]["stt"]["installed"] is True
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": "small.pt", "size": 11}]}), encoding="utf-8")
    s = _status(env)["services"]["stt"]
    assert s["installed"] is False and "not installed" in s["reason"]                    # a size that no longer matches
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": "small.pt", "size": 10}, {"name": "gone.bin", "size": 1}]}), encoding="utf-8")
    assert _status(env)["services"]["stt"]["installed"] is False                         # a file of the marker is missing
    (folder / ms.MARKER).write_text(json.dumps({"files": [{"name": "small.pt", "size": 10}]}), encoding="utf-8")
    assert _status(env)["services"]["stt"]["installed"] is True
    (folder / "small.pt").unlink()
    assert _status(env)["services"]["stt"]["installed"] is False


def test_a_marker_of_another_model_does_not_count(env):
    _install(env.models, "stt", "whisper-base", {"base.pt": 5})
    assert _status(env)["services"]["stt"]["installed"] is False       # the choice is small
    env.settings.write_text(json.dumps({"models_dir": str(env.models), "services": {"stt": {"selected": "whisper-base"}}}), encoding="utf-8")
    assert _status(env)["services"]["stt"]["installed"] is True


def test_settings_are_read_again_when_the_file_changes(env):
    assert _status(env)["services"]["tts"]["model"] == "kokoro"
    other = env.models.parent / "other"
    env.settings.write_text(json.dumps({"models_dir": str(other), "services": {"tts": {"selected": "piper", "device": "cuda:1", "memory": "vram"}}}),
                            encoding="utf-8")
    st = _status(env)
    assert st["models_dir"] == str(other)
    assert (st["services"]["tts"]["model"], st["services"]["tts"]["device"]) == ("piper", "cuda:1")
    assert env.state.config.tts_engine == "piper" and env.state.config.piper_device == "cuda"
    env.settings.write_text("{ damaged", encoding="utf-8")
    assert _status(env)["services"]["tts"]["model"] == "kokoro"                          # a damaged file: the defaults, no crash
    env.settings.unlink()
    assert _status(env)["services"]["tts"]["model"] == "kokoro"


def test_the_config_gets_the_chosen_models_and_roots(env):
    cfg = env.state.config
    assert cfg.stt_model == "small" and cfg.tts_engine == "kokoro" and cfg.call_transport == "off"
    assert cfg.kokoro_model_dir == str(env.models / "tts" / "kokoro")
    assert cfg.control_auth_token == TOKEN


def test_offline_environment_is_set(env):
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ[ms.ROOT_ENV] == str(env.models)


# --- models are read only from the models folder, never fetched ---------------------------------

def test_model_not_installed_message(env):
    with pytest.raises(ms.ModelNotInstalled) as e:
        ms.require("whisper-medium", str(env.models))
    assert str(e.value) == "Whisper medium is not installed: install it from Settings > Voice."
    assert (e.value.model, e.value.service) == ("whisper-medium", "stt")
    with pytest.raises(ms.ModelNotInstalled):
        ms.whisper_checkpoint("tiny", str(env.models))
    with pytest.raises(ms.ModelNotInstalled):
        ms.whisper_checkpoint("small", "")                                                # no models folder at all


def test_whisper_checkpoint_comes_from_the_models_folder(env):
    _install(env.models, "stt", "whisper-small", {"small.pt": 3})
    assert ms.whisper_checkpoint("small") == str(env.models / "stt" / "whisper-small" / "small.pt")


def test_whisper_path_never_downloads(env, monkeypatch):
    from callbridge import memoria
    fake = types.ModuleType("whisper")
    fake._MODELS = {"small": "https://example.invalid/small.pt"}
    fake._ALIGNMENT_HEADS = {"small": b"heads"}
    fake._download = lambda *a, **k: pytest.fail("whisper tried to download")
    monkeypatch.setitem(sys.modules, "whisper", fake)
    with pytest.raises(ms.ModelNotInstalled):
        memoria._percorso_whisper("small")
    _install(env.models, "stt", "whisper-small", {"small.pt": 3})
    assert memoria._percorso_whisper("small") == (str(env.models / "stt" / "whisper-small" / "small.pt"), b"heads")


def test_kokoro_refuses_to_run_without_its_files(env):
    from callbridge.engines import kokoro_tts
    eng = kokoro_tts.KokoroTtsEngine(str(env.models / "tts" / "kokoro"), device="cpu")
    with pytest.raises(ms.ModelNotInstalled) as e:
        eng.load()
    assert "Kokoro is not installed" in str(e.value) and "Settings > Voice" in str(e.value)
    gpu = kokoro_tts.KokoroTtsEngine(str(env.models / "tts" / "kokoro"), device="cuda")
    with pytest.raises(ms.ModelNotInstalled):
        gpu.load()                                                                       # before any helper process is started
    assert gpu._proc is None


def test_piper_refuses_a_voice_that_is_not_there(env):
    from callbridge.engines.piper_tts import PiperTtsEngine
    with pytest.raises(ms.ModelNotInstalled) as e:
        PiperTtsEngine(str(env.models / "tts" / "piper"))._get_voice("it_IT-paola-medium")
    assert "Piper is not installed" in str(e.value)


def test_diarization_says_which_model_is_missing(env, monkeypatch):
    from callbridge.engines import diarize
    monkeypatch.setitem(sys.modules, "sherpa_onnx", types.ModuleType("sherpa_onnx"))
    cfg = env.state.config
    ok, why = diarize.available(cfg)
    assert ok is False and "sherpa-onnx diarization is not installed" in why
    with pytest.raises(ms.ModelNotInstalled):
        diarize.SpeakerDiarizer(cfg).load()


def test_silero_is_found_only_in_the_models_folder(env):
    from callbridge.engines.whisper_stt import _find_silero
    assert _find_silero("") is None
    folder = _install(env.models, "vad", "silero-vad-v4", {"silero_vad_v4.onnx": 4})
    assert _find_silero("") == str(folder / "silero_vad_v4.onnx")


def test_helpers_get_the_offline_environment(env, monkeypatch):
    from callbridge.engines import stt_shared
    seen = {}

    class FakeProc:
        pid = 1
        stderr = None

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen.update(kw)
        return FakeProc()

    monkeypatch.setattr(stt_shared.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stt_shared.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    eng = stt_shared.SharedSttEngine(env.state.config)
    eng._spawn_locked()
    e = seen["env"]
    assert e["HF_HUB_OFFLINE"] == "1" and e["TRANSFORMERS_OFFLINE"] == "1" and e[ms.ROOT_ENV] == str(env.models)
    assert e["DICT_STT_MODEL"] == "small"
