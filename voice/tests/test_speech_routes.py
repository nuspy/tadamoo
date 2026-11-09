"""The speech routes of callAPIcall's old Handy server, now served by the voice service: a model that is not installed is a 409 with its
reason, everything else answers in the shape callAPIcall's clients know. The engines are stand-ins; no model, GPU or audio device is used."""
import base64
import io
import json
import struct
import types
import wave

import numpy as np
import pytest

from callbridge import engines
from callbridge import models_store as ms
from voice import service

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _wav(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 100) * int(16000 * seconds))
    return buf.getvalue()


class Stt:
    loaded = True
    state = "running"

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, lang=None):
        self.calls.append(("text", len(audio), lang))
        return "hello"

    def transcribe_detailed(self, audio, lang=None, words=False, detect=False, allowed=None):
        self.calls.append(("detailed", len(audio), lang, words))
        out = {"text": "hello", "segments": [{"start_s": 0, "end_s": 0.5, "text": "hello"}]}
        if detect:
            out["language"], out["language_prob"] = "en", 0.99876
        return out

    def runtime_info(self):
        return {"model": "small", "running_on": "cpu", "state": "running"}


class Tts:
    model_dir = "."

    def __init__(self, error=None):
        self.error, self.asked = error, []

    def synth(self, text, voice, lang, speed, engine=""):
        if self.error:
            raise self.error
        self.asked.append((text, voice, lang, engine))
        return np.zeros(2400, dtype=np.float32), 24000

    def catalogo(self):
        return [{"id": "kokoro", "name": "Kokoro", "is_downloaded": True}]

    def voci_di(self, engine_id):
        return [{"voice": "if_sara", "lang": "it"}] if engine_id == "kokoro" else []

    def stato_motori(self):
        return {"kokoro": {"state": "ready", "message": ""}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "voice.json"
    models = tmp_path / "models"
    settings.write_text(json.dumps({"models_dir": str(models)}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    stt, tts = Stt(), Tts()
    monkeypatch.setattr(engines, "_get_stt_engine", lambda cfg: stt)
    monkeypatch.setattr(engines, "_get_tts_engine", lambda cfg: tts)
    return types.SimpleNamespace(client=app.test_client(), models=models, stt=stt, tts=tts)


def _install_stt(env):
    folder = env.models / "stt" / "whisper-small"
    folder.mkdir(parents=True)
    (folder / "small.pt").write_bytes(b"x")


def _post_audio(env, query=""):
    return env.client.post("/transcribe" + query, data={"file": (io.BytesIO(_wav()), "a.wav")}, headers=AUTH, content_type="multipart/form-data")


def test_transcribe_without_the_model_is_a_409_with_the_reason(env):
    r = _post_audio(env)
    assert r.status_code == 409
    assert r.get_json() == {"status": "error", "code": "model_not_installed", "model": "whisper-small",
                            "message": "Whisper small is not installed: install it from Settings > Voice."}
    assert env.stt.calls == []


def test_transcribe_answers_with_the_text_or_with_the_timed_segments(env):
    _install_stt(env)
    assert _post_audio(env, "?lang=it").get_json() == {"text": "hello"}
    assert env.stt.calls == [("text", 8000, "it")]
    body = _post_audio(env, "?segments=1&words=1").get_json()
    assert body["segments"][0]["text"] == "hello" and env.stt.calls[-1] == ("detailed", 8000, None, True)
    assert (body["language"], body["language_probability"]) == ("en", 0.9988) and "language_prob" not in body          # asked nothing: it says what it heard
    hinted = _post_audio(env, "?segments=1&lang=it").get_json()
    assert "language" not in hinted and "language_probability" not in hinted                                       # a language was given: nothing to report
    assert _post_audio(env, "").get_json() == {"text": "hello"}                                                    # the plain answer is as before


def test_transcribe_needs_a_file_and_refuses_what_is_not_audio(env):
    _install_stt(env)
    assert env.client.post("/transcribe", data={}, headers=AUTH, content_type="multipart/form-data").status_code == 400
    r = env.client.post("/transcribe", data={"file": (io.BytesIO(b"not audio"), "a.wav")}, headers=AUTH, content_type="multipart/form-data")
    assert r.status_code == 400 and "cannot be read" in r.get_json()["message"]


def test_tts_test_returns_a_wav_and_the_engine_it_was_asked_for(env):
    r = env.client.post("/tts/test", json={"text": "Ciao", "voice": "if_sara", "lang": "it", "engine": "kokoro-voices"}, headers=AUTH)
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "ok" and body["engine"] == "kokoro" and body["duration_ms"] == 100
    assert base64.b64decode(body["audio_b64"])[:4] == b"RIFF"
    assert env.tts.asked == [("Ciao", "if_sara", "it", "kokoro")]
    assert env.client.post("/tts/test", json={"text": " "}, headers=AUTH).status_code == 400


def test_tts_test_with_a_missing_model_is_a_409_and_other_errors_keep_their_text(env):
    env.tts.error = ms.ModelNotInstalled("xtts-v2", "tts")
    r = env.client.post("/tts/test", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["code"] == "model_not_installed" and r.get_json()["model"] == "xtts-v2"
    env.tts.error = RuntimeError("the helper died")
    r = env.client.post("/tts/test", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 500 and r.get_json()["message"] == "the helper died"


def test_the_catalogue_voices_and_status_routes(env):
    models = env.client.get("/tts/models", headers=AUTH).get_json()
    assert models["models"][0]["id"] == "kokoro" and models["selected"] == "kokoro"
    assert env.client.get("/tts/voices?model=kokoro", headers=AUTH).get_json() == {"model": "kokoro", "voices": [{"voice": "if_sara", "lang": "it"}]}
    status = env.client.get("/models/status", headers=AUTH).get_json()
    assert status["tts"]["loaded"] is True and status["stt"]["loaded"] is True
    available = env.client.get("/models/available", headers=AUTH).get_json()
    assert available["tts"]["engine"] == "kokoro" and available["tts"]["voices"]


def test_cloned_voices_live_in_the_models_folder(env):
    assert env.client.get("/voices/cloned", headers=AUTH).get_json() == {"voices": []}
    sample = base64.b64encode(_wav()).decode()
    r = env.client.post("/voices/clone", json={"name": "ada", "format": "wav", "audio_b64": sample}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["voices"] == ["ada"]
    assert (env.models / "tts" / "voices" / "ada.wav").is_file()
    assert env.client.post("/voices/clone", json={"name": "../x", "audio_b64": sample}, headers=AUTH).status_code == 400
    assert env.client.post("/voices/clone", json={"name": "ada", "format": "exe", "audio_b64": sample}, headers=AUTH).status_code == 400
    assert env.client.delete("/voices/cloned/ada", headers=AUTH).get_json()["voices"] == []
    assert env.client.delete("/voices/cloned/ada", headers=AUTH).status_code == 404


def test_the_old_handy_proxies_are_gone_from_the_control_routes(env):
    for method, path in (("post", "/models/load"), ("post", "/models/unload"), ("post", "/tts/model"), ("post", "/tts/voice")):
        assert getattr(env.client, method)(path, json={}, headers=AUTH).status_code == 404


def test_transcribe_with_the_real_shared_engine_over_the_stand_in_helper_reports_the_language(env, monkeypatch):
    """The real SharedSttEngine and its process protocol (a stand-in helper process): the HTTP answer carries what the engine heard."""
    import os
    import sys
    from callbridge.engines import stt_shared
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_stt_helper.py")
    monkeypatch.setattr(stt_shared, "_comando_helper", lambda: [sys.executable, "-u", helper])
    monkeypatch.setenv("FAKE_STT_LANG", "fr")
    engine = stt_shared.SharedSttEngine(types.SimpleNamespace(stt_model="small", stt_device="cpu", stt_gpu_memory="shared", stt_compute_type="auto", stt_language=""))
    monkeypatch.setattr(engines, "_get_stt_engine", lambda cfg: engine)
    _install_stt(env)
    try:
        body = _post_audio(env, "?segments=1").get_json()
        assert (body["language"], body["language_probability"]) == ("fr", 0.93) and body["segments"]
        assert "language" not in _post_audio(env, "?segments=1&lang=it").get_json()
    finally:
        engine.chiudi()


def test_the_handy_compat_transcribe_route_reports_the_language_too(tmp_path, monkeypatch):
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer
    from callbridge import handy_compat
    stt = Stt()
    monkeypatch.setattr(handy_compat, "_get_stt_engine", lambda cfg: stt)
    monkeypatch.setattr(handy_compat, "_get_tts_engine", lambda cfg: Tts())

    async def post(query):
        from aiohttp import FormData
        from callbridge.config import Config
        async with TestClient(TestServer(handy_compat.build_app(Config()))) as client:
            form = FormData()
            form.add_field("file", _wav(), filename="a.wav", content_type="audio/wav")
            r = await client.post("/transcribe" + query, data=form)
            return await r.json()

    body = asyncio.run(post("?segments=1"))
    assert (body["language"], body["language_probability"]) == ("en", 0.9988) and body["segments"] and "language_prob" not in body
    assert "language" not in asyncio.run(post("?segments=1&lang=it"))
    assert asyncio.run(post("")) == {"text": "hello"}
