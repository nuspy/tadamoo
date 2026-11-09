"""The enhancer in the avatar client: the worker is started with the size and rate the plan asks for, its frames reach the subscribers through the
enhancer, and a change of settings tells the next worker. The worker is a stand-in process; no GPU, no model, no network."""
import json
import socket
import struct
import sys
import time
import urllib.request

import pytest

from callbridge import avatar_client, models_store as ms
from callbridge.avatar_client import AvatarClient, AvatarFrames, Frame
from callbridge.avatar_store import AvatarStore
from callbridge.enhance import capability as cap
from callbridge.enhance.settings import EnhanceSettings

from avatar_fakes import make_config, put_weights

REPORT = cap.report(cap.GpuInfo("nvidia", "NVIDIA GeForce RTX 4090", 8.9, "581.42"), platform="win32", importable=lambda n: True)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    orig = socket.socket.connect

    def only_loopback(self, address, *a, **k):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return orig(self, address, *a, **k)
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", only_loopback)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network access attempted")))
    monkeypatch.setattr(ms, "_service_env", {})
    models = tmp_path / "models"
    put_weights(models)
    monkeypatch.setenv(ms.ROOT_ENV, str(models))
    log = tmp_path / "worker.jsonl"
    monkeypatch.setenv("FAKE_AVATAR_LOG", str(log))
    cfg = make_config(tmp_path, avatar_fps=24, avatar_output_height=32)
    client = AvatarClient(cfg)
    folder = AvatarStore(cfg.avatars_dir).create("ada")
    (folder / "clips" / "walk.mp4").write_bytes(b"x")
    yield client, cfg, log
    client.stop()


def started(log):
    return next(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if '"started"' in line)


def wait_for(cond, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_with_nothing_on_the_worker_gets_the_configured_rate_and_no_height(rig):
    client, cfg, log = rig
    assert client.ensure_worker()
    note = started(log)
    assert note["fps"] == "24" and note["height"] is None
    assert client.enhancer.status()["worker"] == {"running": True, "height": 0, "fps": 24}


def test_with_a_stage_on_the_worker_is_started_smaller_and_slower(rig):
    client, cfg, log = rig
    plan = client.configure_enhance(EnhanceSettings(upscale="basic", scale=2.0, interpolate="basic", factor=2), REPORT)
    assert (plan.worker_height, plan.worker_fps, plan.out_height, plan.out_fps) == (16, 12, 32, 24)
    assert client.ensure_worker()
    note = started(log)
    assert (note["height"], note["fps"]) == ("16", "12")
    assert client.enhancer.status()["worker"] == {"running": True, "height": 16, "fps": 12} and not client.enhancer.status()["restart_needed"]


def test_the_subscribers_get_the_restored_size_from_the_workers_small_frames(rig):
    client, cfg, log = rig
    client.configure_enhance(EnhanceSettings(upscale="basic", scale=2.0), REPORT)
    got = []
    client.frames.subscribe(got.append)
    assert client.ensure_worker()
    assert wait_for(lambda: len(got) >= 3)
    assert {(f.width, f.height) for f in got} == {(32, 32)}                        # the stand-in worker renders 16x16
    assert client.enhancer.status()["timing"]["frames_in"] >= 3


def test_changing_the_settings_while_the_worker_runs_says_a_restart_is_needed(rig):
    client, cfg, log = rig
    assert client.ensure_worker()
    client.configure_enhance(EnhanceSettings(interpolate="basic", factor=2), REPORT)
    assert client.enhancer.status()["restart_needed"] is True
    client.stop()
    assert client.enhancer.status()["worker"]["running"] is False


def test_a_frame_server_without_an_enhancer_publishes_as_before():
    server = avatar_client._FrameServer(AvatarFrames())
    got = []
    server.frames.subscribe(got.append)
    with socket.create_connection(("127.0.0.1", server.port)) as s:
        s.sendall(struct.pack("<4sII", b"AVI4", 2, 2) + b"\0" * 6)
    assert wait_for(lambda: len(got) == 1)
    server.close()


def test_a_frame_server_hands_frames_to_the_intake_it_was_given():
    taken = []
    server = avatar_client._FrameServer(AvatarFrames(), taken.append)
    with socket.create_connection(("127.0.0.1", server.port)) as s:
        s.sendall(struct.pack("<4sII", b"AVI4", 2, 2) + b"\0" * 6)
    assert wait_for(lambda: len(taken) == 1) and isinstance(taken[0], Frame)
    server.close()
