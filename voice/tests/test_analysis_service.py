"""/analysis/* inside the voice service: the installed STT model, optional diarization, a summary that only the direct brain sees, one job at a time.

ffmpeg, the STT, the diarizer and the model are stand-ins; no model, GPU, audio device or network is used."""
import io
import json
import struct
import threading
import time
import types
import wave
from pathlib import Path

import pytest

from callbridge import analysis as analysis_mod
from callbridge import api_analysis
from callbridge import models_store as ms
from voice import service

TOKEN = "t0ken-for-tests"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))
    return buf.getvalue()


class Brains:
    """Stands in for make_brain(): the direct brain records what it is asked; the harness brain must never be touched."""

    def __init__(self):
        self.asked = []
        self.direct = types.SimpleNamespace(ask_stream=self._ask)
        self.harness = types.SimpleNamespace(ask_stream=self._forbidden, respond_stream=self._forbidden)

    def _ask(self, text, source="remote"):
        self.asked.append(text)
        if "SEGMENTO" in text:
            yield json.dumps({"partial_summary": "p", "candidate_highlights": [], "candidate_agreements": [], "candidate_tasks": []})
        else:
            yield json.dumps({"summary": "A short talk.", "highlights": [{"title": "Point", "summary": "s"}], "agreements": [],
                              "tasks": [{"assignee": "Speaker 1", "task": "send it"}]})

    def _forbidden(self, *a, **k):
        raise AssertionError("an agent with tools saw a transcript")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "data" / "voice.json"
    settings.parent.mkdir()
    models = tmp_path / "models"
    settings.write_text(json.dumps({"models_dir": str(models)}), encoding="utf-8")
    app, state = service.build_app(settings, TOKEN)
    brains = Brains()
    gate = threading.Event()
    gate.set()
    calls = []

    def ffmpeg(cfg, args, timeout=0):
        seg = Path(args[-1]).parent
        seg.mkdir(parents=True, exist_ok=True)
        (seg / "seg0000.wav").write_bytes(_wav(2.0))

    def block(cfg, wav, words):
        gate.wait(10)
        calls.append(words)
        return {"text": "Hello there.", "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "Hello there.",
                                                        "words": [{"start_s": 0.0, "end_s": 1.0, "text": " Hello there."}]}]}

    monkeypatch.setattr(api_analysis, "_current", None)
    monkeypatch.setattr(analysis_mod, "_brain", None)
    monkeypatch.setattr(analysis_mod, "make_brain", lambda cfg: brains)
    monkeypatch.setattr(api_analysis.shutil, "which", lambda name: name)
    monkeypatch.setattr(api_analysis, "_run_ffmpeg", ffmpeg)
    monkeypatch.setattr(api_analysis, "_transcribe_block", block)
    monkeypatch.setattr(api_analysis.diarize, "available", lambda cfg: (False, "the diarization models are not installed"))
    yield types.SimpleNamespace(client=app.test_client(), root=settings.parent / "voice" / "analyses", models=models, brains=brains, gate=gate,
                                calls=calls, state=state)
    gate.set()
    for thread in threading.enumerate():                              # a job still running would reach into the next test's stand-ins
        if thread.name == "analysis-job":
            thread.join(10)


def _install_stt(env):
    folder = env.models / "stt" / "whisper-small"
    folder.mkdir(parents=True)
    (folder / "small.pt").write_bytes(b"x")


def _start(env, **form):
    data = {"file": (io.BytesIO(_wav()), "talk.wav"), **form}
    return env.client.post("/analysis/start", data=data, headers=AUTH, content_type="multipart/form-data")


def _wait(env, job, states=("done", "error")):
    end = time.time() + 15
    while time.time() < end:
        body = env.client.get(f"/analysis/{job}", headers=AUTH).get_json()
        if body["state"] in states:
            return body
        time.sleep(0.02)
    raise AssertionError("the job did not finish")


def test_without_the_stt_model_the_start_is_a_409_with_the_reason_and_nothing_is_kept(env):
    r = _start(env)
    body = r.get_json()
    assert r.status_code == 409
    assert body["code"] == "model_not_installed" and body["model"] == "whisper-small"
    assert body["error"] == "Whisper small is not installed: install it from Settings > Voice."
    assert not env.root.exists()
    assert env.calls == []


def test_a_recording_is_transcribed_summarised_and_kept_in_the_user_data_folder(env):
    _install_stt(env)
    r = _start(env, speakers="2")
    assert r.status_code == 202
    job = r.get_json()["id"]
    done = _wait(env, job)
    assert done["state"] == "done" and done["analysis"]["summary"] == "A short talk."
    assert (env.root / job / "transcript.json").is_file() and (env.root / job / "summary.md").is_file()
    transcript = env.client.get(f"/analysis/{job}/transcript", headers=AUTH).get_json()
    assert [s["text"] for s in transcript["segments"]] == ["Hello there."]
    assert "speaker" not in transcript["segments"][0]                 # no diarization models: no labels, and the analysis still ran
    assert env.calls == [False]                                       # word times are asked for only when the speakers will be told apart
    listed = env.client.get("/analysis/list", headers=AUTH).get_json()["analyses"]
    assert [a["id"] for a in listed] == [job]
    assert env.client.delete(f"/analysis/{job}", headers=AUTH).status_code == 200
    assert not (env.root / job).exists()


def test_with_diarization_models_the_lines_get_speakers_and_they_can_be_named(env, monkeypatch):
    _install_stt(env)
    monkeypatch.setattr(api_analysis.diarize, "available", lambda cfg: (True, ""))
    seen = {}

    class Diarizer:
        def diarize(self, audio, progress=None, num_speakers=0):
            seen["samples"], seen["speakers"] = len(audio), num_speakers
            return [{"start_s": 0.0, "end_s": 2.0, "speaker": "Speaker 1"}]

    monkeypatch.setattr(api_analysis.diarize, "get_diarizer", lambda cfg: Diarizer())
    job = _start(env, speakers="3").get_json()["id"]
    done = _wait(env, job)
    assert done["state"] == "done" and done["speakers_found"] == 1
    assert env.calls == [True] and seen == {"samples": 32000, "speakers": 3}
    first = env.client.get(f"/analysis/{job}/transcript", headers=AUTH).get_json()["segments"][0]
    assert first["speaker"] == "Speaker 1"
    named = env.client.post(f"/analysis/{job}/speakers", json={"names": {"Speaker 1": "Ada"}}, headers=AUTH)
    assert named.get_json()["speaker_names"] == {"Speaker 1": "Ada"}
    assert env.client.get(f"/analysis/{job}/transcript", headers=AUTH).get_json()["segments"][0]["speaker"] == "Ada"


def test_a_broken_diarizer_does_not_fail_the_analysis(env, monkeypatch):
    _install_stt(env)
    monkeypatch.setattr(api_analysis.diarize, "available", lambda cfg: (True, ""))
    monkeypatch.setattr(api_analysis.diarize, "get_diarizer", lambda cfg: (_ for _ in ()).throw(RuntimeError("no onnx")))
    done = _wait(env, _start(env).get_json()["id"])
    assert done["state"] == "done" and "analysis" in done


def test_only_the_direct_brain_reads_the_transcript(env):
    _install_stt(env)
    done = _wait(env, _start(env).get_json()["id"])
    assert done["state"] == "done"
    assert env.brains.asked and any("Hello there." in text for text in env.brains.asked)      # the harness brain would have raised


def test_analyze_zero_keeps_the_text_away_from_every_brain(env):
    _install_stt(env)
    done = _wait(env, _start(env, analyze="0").get_json()["id"])
    assert done["state"] == "done" and "analysis" not in done
    assert env.brains.asked == []


def test_one_job_at_a_time(env):
    _install_stt(env)
    env.gate.clear()
    first = _start(env)
    assert first.status_code == 202
    second = _start(env)
    assert second.status_code == 409 and second.get_json()["code"] == "busy"
    env.gate.set()
    assert _wait(env, first.get_json()["id"])["state"] == "done"
    assert _start(env).status_code == 202


def test_a_model_that_goes_missing_during_the_job_ends_it_with_the_reason(env, monkeypatch):
    _install_stt(env)

    def gone(cfg, wav, words):
        raise ms.ModelNotInstalled("whisper-small", "stt")
    monkeypatch.setattr(api_analysis, "_transcribe_block", gone)
    done = _wait(env, _start(env).get_json()["id"])
    assert done["state"] == "error" and "Whisper small is not installed" in done["error"]


def test_the_contract_the_harness_relies_on(env):
    """tadamoo/attachment_tools.py (transcribe_attachment) posts a file and polls /analysis/<id>: queued, then done with a transcript."""
    _install_stt(env)
    r = _start(env, speakers="0", analyze="0", title="talk.wav")
    assert r.status_code == 202 and set(r.get_json()) == {"id", "state"} and r.get_json()["state"] == "queued"
    job = r.get_json()["id"]
    done = _wait(env, job)
    assert done["state"] == "done" and done["title"] == "talk.wav"
    segments = env.client.get(f"/analysis/{job}/transcript", headers=AUTH).get_json()["segments"]
    assert all({"start_s", "end_s", "text"} <= set(s) for s in segments)
    assert env.client.get("/analysis/nope", headers=AUTH).status_code == 404
