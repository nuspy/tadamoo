"""The avatar client with a fake worker process: offline start from the models folder, the missing-weight and missing-environment reasons,
loading an avatar, preparing a clip (progress, one at a time, cancel, failure), the frame subscription and speaking. No GPU, no model."""
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from callbridge import avatar_client, avatar_weights, models_store as ms
from callbridge.avatar_client import AvatarClient, AvatarFrames, Frame, PrepareBusy
from callbridge.avatar_store import AvatarStore, StoreError

from avatar_fakes import make_config, put_weights


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    orig = socket.socket.connect

    def only_loopback(self, address, *a, **k):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return orig(self, address, *a, **k)
        raise AssertionError("network access attempted")

    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", only_loopback)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", ms.ROOT_ENV, avatar_client.PYTHON_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ms, "_service_env", {})


class Rig:
    pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    models = tmp_path / "models"
    put_weights(models)
    monkeypatch.setenv(ms.ROOT_ENV, str(models))
    log = tmp_path / "worker.jsonl"
    monkeypatch.setenv("FAKE_AVATAR_LOG", str(log))
    cfg = make_config(tmp_path)
    r = Rig()
    r.cfg, r.client, r.store, r.log, r.models, r.monkeypatch, r.tmp = cfg, AvatarClient(cfg), AvatarStore(cfg.avatars_dir), log, models, monkeypatch, tmp_path
    folder = r.store.create("ada")
    (folder / "clips" / "walk.mp4").write_bytes(b"x")
    (folder / "clips" / "bad.mp4").write_bytes(b"x")
    yield r
    r.client.stop()


def worker_log(rig):
    return [json.loads(line) for line in rig.log.read_text(encoding="utf-8").splitlines()] if rig.log.exists() else []


def commands(rig):
    return [c["cmd"] for c in worker_log(rig) if "cmd" in c]


def wait_for(cond, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_worker_starts_offline_from_the_models_folder_with_the_code_on_its_path(rig):
    assert rig.client.start("ada")
    first = worker_log(rig)[0]
    assert first["models"] == str(rig.models / "avatar" / "musetalk-v15")
    assert first["code"] == str(avatar_client.CODE_DIR) and first["pythonpath"].split(os.pathsep)[0] == str(avatar_client.CODE_DIR)
    assert first["hf_offline"] == "1" and first["tf_offline"] == "1"
    assert first["cwd"] == str(avatar_client.CODE_DIR)
    assert Path(first["state"]) == Path(rig.cfg.avatars_dir).parent / "avatar-state"
    assert {"cmd": "persona", "dir": str(rig.store.path("ada"))} in commands(rig)
    assert rig.client.is_ready("ada") and rig.client._active_persona == "ada"


def test_a_missing_weight_keeps_the_worker_from_starting_and_names_the_file(rig, monkeypatch):
    (rig.models / "avatar" / "musetalk-v15" / "face-parse-bisent" / "79999_iter.pth").unlink()
    started = []
    monkeypatch.setattr(avatar_client.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert not rig.client.start("ada")
    assert "face-parse-bisent/79999_iter.pth" in rig.client.last_error and "missing" in rig.client.last_error
    assert not rig.client.ensure_worker() and not started
    assert avatar_weights.missing(str(rig.models)) == ["face-parse-bisent/79999_iter.pth"]


def test_every_listed_weight_is_checked_and_the_reason_says_how_many(rig):
    for rel in ("s3fd/s3fd-619a316812.pth", "dwpose/dw-ll_ucoco_384.pth"):
        (rig.models / "avatar" / "musetalk-v15" / Path(*rel.split("/"))).unlink()
    text = avatar_weights.reason(str(rig.models))
    assert "2 weight file" in text and "s3fd/s3fd-619a316812.pth" in text and "dwpose/dw-ll_ucoco_384.pth" in text
    assert avatar_weights.missing("") == list(avatar_weights.REQUIRED)               # no models folder: all of them


def test_without_the_environment_the_reason_names_the_variable(rig):
    rig.cfg.avatar_python = ""
    assert not rig.client.start("ada") and avatar_client.PYTHON_ENV in rig.client.last_error
    rig.cfg.avatar_python = str(rig.tmp / "no" / "python.exe")
    assert not rig.client.start("ada") and "not there" in rig.client.last_error
    rig.cfg.avatar_python, rig.cfg.avatar_worker = sys.executable, str(rig.tmp / "none.py")
    assert not rig.client.start("ada") and "worker is missing" in rig.client.last_error


def test_the_variable_names_the_python_when_the_config_names_none(rig, monkeypatch):
    rig.cfg.avatar_python = ""
    monkeypatch.setenv(avatar_client.PYTHON_ENV, sys.executable)
    assert rig.client.start("ada")


def test_an_avatar_that_does_not_exist_or_has_no_clip_does_not_start(rig):
    assert not rig.client.start("nobody") and "no avatar" in rig.client.last_error
    rig.store.create("photo")
    assert not rig.client.start("photo") and "no video clip" in rig.client.last_error
    assert not worker_log(rig)                                                       # the worker was never started for either


def test_the_worker_is_offline_and_refuses_every_download(rig):
    assert rig.client.start("ada")
    env = rig.client._worker_env()
    assert env["HF_HUB_OFFLINE"] == "1" and env["HF_DATASETS_OFFLINE"] == "1" and env["MUSETALK_MODELS"].endswith(os.path.join("avatar", "musetalk-v15"))
    source = (Path(avatar_client.__file__).resolve().parents[1] / "avatar" / "avatar_worker.py").read_text(encoding="utf-8")
    assert "torch.hub.load_state_dict_from_url = _no_download" in source and "torch.utils.model_zoo.load_url = _no_download" in source
    assert "pyvirtualcam" not in source and "imshow" not in source                   # no camera and no window in the worker


def test_the_gpu_the_person_chose_is_the_only_one_the_worker_sees(rig):
    rig.cfg.avatar_gpu_index, rig.cfg.avatar_fps = 1, 16
    ms.set_service_env("avatar", {"CUDA_VISIBLE_DEVICES": "0"})
    env = rig.client._worker_env()
    assert env["CUDA_VISIBLE_DEVICES"] == "1" and env["AVATAR_FPS"] == "16"


def test_preparing_a_clip_reports_progress_and_records_it(rig):
    rig.monkeypatch.setenv("FAKE_AVATAR_STEP_S", "0.1")
    assert rig.client.ensure_worker()
    job = rig.client.prepare("ada", "walk.mp4")
    assert job["state"] == "running" and job["avatar"] == "ada"
    assert wait_for(lambda: (rig.client.prepare_state() or {}).get("progress"))
    assert rig.client.prepare_state()["progress"]["phase"] == "latents"
    assert wait_for(lambda: rig.client.prepare_state()["state"] == "done")
    assert rig.store.prepared("ada")["walk.mp4"]["frames"] == 8
    assert rig.store.info("ada")["prepared"] == ["walk.mp4"]


def test_one_preparation_at_a_time_and_it_can_be_cancelled(rig):
    rig.monkeypatch.setenv("FAKE_AVATAR_STEP_S", "0.3")
    rig.monkeypatch.setenv("FAKE_AVATAR_PREP_STEPS", "20")
    assert rig.client.ensure_worker()
    rig.client.prepare("ada", "walk.mp4")
    with pytest.raises(PrepareBusy):
        rig.client.prepare("ada", "bad.mp4")
    assert rig.client.cancel_prepare()
    assert wait_for(lambda: rig.client.prepare_state()["state"] == "cancelled")
    assert rig.store.prepared("ada") == {}
    assert not rig.client.cancel_prepare()                                           # nothing running any more
    rig.client.prepare("ada", "walk.mp4")                                            # a new one can start


def test_a_failed_preparation_keeps_the_message_and_the_worker(rig):
    assert rig.client.ensure_worker()
    rig.client.prepare("ada", "bad.mp4")
    assert wait_for(lambda: rig.client.prepare_state()["state"] == "failed")
    assert rig.client.prepare_state()["error"] == "no face found" and rig.store.prepared("ada") == {}
    assert rig.client._proc.poll() is None


def test_preparing_needs_the_avatar_loaded_and_a_clip_that_exists(rig):
    with pytest.raises(RuntimeError, match="not loaded"):
        rig.client.prepare("ada", "walk.mp4")
    assert rig.client.ensure_worker()
    for name, clip in (("nobody", "walk.mp4"), ("ada", "other.mp4"), ("ada", "../walk.mp4"), ("../ada", "walk.mp4")):
        with pytest.raises(StoreError):
            rig.client.prepare(name, clip)


def test_frames_go_to_subscribers_and_the_worker_is_told_to_stop_when_none_is_left(rig):
    assert rig.client.ensure_worker()
    got = []
    off = rig.client.frames.subscribe(got.append)
    assert wait_for(lambda: len(got) >= 3)
    assert isinstance(got[0], Frame) and (got[0].width, got[0].height) == (16, 16) and len(got[0].i420) == 16 * 16 * 3 // 2
    assert got[1].bgr().shape == (16, 16, 3) and got[1].jpeg()[:2] == b"\xff\xd8"
    off()
    time.sleep(0.3)
    seen = len(got)
    time.sleep(0.3)
    assert len(got) == seen                                                          # nothing more after the last subscriber left
    ons = [c["on"] for c in commands(rig) if c["cmd"] == "frames"]
    assert ons == [True, False]


def test_a_subscriber_that_arrives_before_the_worker_gets_frames_when_it_starts(rig):
    got = []
    rig.client.frames.subscribe(got.append)
    assert rig.client.ensure_worker()
    assert wait_for(lambda: len(got) >= 1)


def test_a_subscriber_that_raises_does_not_stop_the_others():
    frames, good = AvatarFrames(), []
    frames.subscribe(lambda f: 1 / 0)
    frames.subscribe(good.append)
    frames.publish(Frame(2, 2, b"\0" * 6))
    assert len(good) == 1 and frames.count == 2


def test_the_frame_server_refuses_a_bad_header():
    server = avatar_client._FrameServer(AvatarFrames())
    got = []
    server.frames.subscribe(got.append)
    with socket.create_connection(("127.0.0.1", server.port)) as s:
        s.sendall(struct.pack("<4sII", b"XXXX", 16, 16) + b"\0" * 384)
    with socket.create_connection(("127.0.0.1", server.port)) as s:
        s.sendall(struct.pack("<4sII", b"AVI4", 15, 16) + b"\0" * 400)                # odd side
    with socket.create_connection(("127.0.0.1", server.port)) as s:
        s.sendall(struct.pack("<4sII", b"AVI4", 16, 16) + b"\0" * 384)
    assert wait_for(lambda: len(got) == 1)
    server.close()


def test_speaking_interrupting_and_the_events(rig):
    assert rig.client.start("ada")
    rig.client.speak_wav("x.wav", mute=True)
    assert rig.client.wait_until_playing(5) and rig.client.preparazione_s == pytest.approx(0.12)
    assert rig.client.wait_until_spoken(5)
    rig.client.interrupt(svuota=True)
    assert wait_for(lambda: {"cmd": "interrupt", "svuota": True} in commands(rig))
    assert {"cmd": "speak", "wav": "x.wav", "mute": True} in commands(rig)


def test_stop_ends_the_worker_and_fails_a_running_job(rig):
    rig.monkeypatch.setenv("FAKE_AVATAR_PREP_STEPS", "40")
    rig.monkeypatch.setenv("FAKE_AVATAR_STEP_S", "0.3")
    assert rig.client.ensure_worker()
    proc = rig.client._proc
    rig.client.prepare("ada", "walk.mp4")
    rig.client.stop()
    assert proc.poll() is not None and rig.client.prepare_state()["state"] == "failed"
    assert not rig.client.is_ready()


def test_the_voice_the_wizard_made_is_read_from_the_store(rig):
    rig.store.write_metadata("ada", voice={"engine": "xtts", "voice": "ada"})
    assert rig.client.voice_for("ada") == {"engine": "xtts", "voice": "ada"} and rig.client.voice_for("nobody") == {}
    assert rig.client.personas() == ["ada"]
