"""The TTS routes (handy_compat) with a stand-in synthesiser: a missing model is a clear 409, never a stack trace, and the cloned voices
live in `<models folder>/tts/voices`. No model, GPU or audio is used."""
import asyncio
import base64
import os
import queue
import time
from types import SimpleNamespace

import pytest

from callbridge import models_store as ms

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from callbridge import handy_compat  # noqa: E402
from callbridge.engines.kokoro_tts import NativeTtsClient  # noqa: E402


class Router:
    """Stands in for the TTS router: it has no model installed."""
    model_dir = "."

    def __init__(self, error=None):
        self.error = error

    def synth(self, text, voice, lang, speed, engine=""):
        raise self.error or ms.ModelNotInstalled("xtts-v2", "tts")

    def attiva(self, engine_id, precarica=True):
        raise ms.ModelNotInstalled("xtts-v2", "tts")

    def load(self):
        pass

    def stato_motori(self):
        return {}


class Stt:
    _model = None

    def load(self):
        pass


@pytest.fixture
def models(tmp_path, monkeypatch):
    root = tmp_path / "models"
    monkeypatch.setenv(ms.ROOT_ENV, str(root))
    return root


def run(router, steps):
    cfg = SimpleNamespace(tts_engine="xtts", default_voice="x", default_lang="en", tts_speed=1.0, stt_model="small", stt_language="",
                          stt_vad_threshold=0.01, save=lambda: None)

    async def go():
        import unittest.mock as mock
        with mock.patch.object(handy_compat, "_get_tts_engine", lambda c: router), \
                mock.patch.object(handy_compat, "_get_stt_engine", lambda c: Stt()):
            app = handy_compat.build_app(cfg)
            async with TestClient(TestServer(app)) as cl:
                return await steps(cl)

    return asyncio.run(go())


def b64(data=b"RIFFdata"):
    return base64.b64encode(data).decode()


def test_tts_test_answers_409_with_the_plain_message(models):
    async def steps(cl):
        r = await cl.post("/tts/test", json={"text": "hello", "voice": "v", "engine": "xtts"})
        return r.status, await r.json()

    status, body = run(Router(), steps)
    assert status == 409
    assert body == {"status": "error", "code": "model_not_installed", "model": "xtts-v2",
                    "message": "XTTS v2 is not installed: install it from Settings > Voice."}


def test_other_synthesis_errors_keep_their_old_answer(models):
    async def steps(cl):
        r = await cl.post("/tts/test", json={"text": "hello"})
        return r.status, await r.json()

    status, body = run(Router(RuntimeError("boom")), steps)
    assert status == 200 and body == {"status": "error", "message": "boom"}


def test_loading_a_model_that_is_missing_answers_409(models):
    async def steps(cl):
        r = await cl.post("/models/load", json={"tts": True})
        return r.status, await r.json()

    status, body = run(Router(), steps)
    assert status == 409 and body["code"] == "model_not_installed" and "Settings > Voice" in body["message"]


def test_cloned_voices_are_kept_in_the_tts_voices_folder(models):
    async def steps(cl):
        assert (await (await cl.get("/voices/cloned")).json()) == {"voices": []}
        r = await cl.post("/voices/clone", json={"name": "anna", "format": "wav", "audio_b64": b64()})
        assert (await r.json()) == {"status": "ok", "voices": ["anna"]}
        await cl.post("/voices/clone", json={"name": "luca", "format": "mp3", "audio_b64": b64(b"ID3")})
        listed = (await (await cl.get("/voices/cloned")).json())["voices"]
        assert [(v["name"], v["format"]) for v in listed] == [("anna", "wav"), ("luca", "mp3")]
        r = await cl.delete("/voices/cloned/anna")
        assert (await r.json()) == {"status": "ok", "voices": ["luca"]}
        r = await cl.delete("/voices/cloned/anna")
        return await r.json()

    again = run(Router(), steps)
    assert again["status"] == "error"
    voices = models / "tts" / "voices"
    assert sorted(os.listdir(voices)) == ["luca.mp3"]
    assert (voices / "luca.mp3").read_bytes() == b"ID3"


def test_a_clone_name_cannot_leave_the_voices_folder(models):
    async def steps(cl):
        out = []
        for name, fmt in (("../escape", "wav"), ("a/b", "wav"), ("..\\x", "wav"), (".hidden", "wav"), ("ok", "exe"), ("ok", "wav/../../x")):
            r = await cl.post("/voices/clone", json={"name": name, "format": fmt, "audio_b64": b64()})
            out.append(r.status)
        return out

    assert run(Router(), steps) == [400] * 6
    assert not (models / "tts").exists() or os.listdir(models / "tts") == []
    assert not (models / "escape.wav").exists()


def test_cloning_without_a_models_folder_says_so(monkeypatch):
    monkeypatch.delenv(ms.ROOT_ENV, raising=False)

    async def steps(cl):
        r = await cl.post("/voices/clone", json={"name": "anna", "format": "wav", "audio_b64": b64()})
        return r.status, (await (await cl.get("/voices/cloned")).json())

    status, listed = run(Router(), steps)
    assert status == 409 and listed == {"voices": []}


def test_a_streaming_client_reports_a_missing_model():
    client = NativeTtsClient(Router(), SimpleNamespace(tts_normalize_text=False, tts_streaming=False))
    client.start()
    try:
        client.configure("anna", "en", 1.0, "xtts")
        client.send_text("Hello there.")
        client.flush()
        deadline = time.monotonic() + 5
        message = None
        while time.monotonic() < deadline and message is None:
            try:
                message = client.errors.get(timeout=0.1)
            except queue.Empty:
                pass
    finally:
        client.close()
    assert message == "XTTS v2 is not installed: install it from Settings > Voice."
