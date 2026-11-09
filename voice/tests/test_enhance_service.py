"""The acceleration in the voice service: GET /avatar/enhance (capabilities with reasons, settings, plan, measured cost), voice.json read by the
settings bridge, and the worker started with the size and rate the settings ask for. Same stand-in worker and fake GPU as the avatar tests."""
import json

import pytest

from callbridge.enhance import capability as cap
from callbridge.enhance.settings import EnhanceSettings

from test_avatar_service import AUTH, TOKEN, _isolated, rig, mark_installed  # noqa: F401 (fixtures)

GPU = cap.GpuInfo("nvidia", "NVIDIA GeForce RTX 4090", 8.9, "581.42", 24564, 1500)


@pytest.fixture
def fake_gpu(rig):
    reads = []

    def reader(index):
        reads.append(index)
        return GPU
    rig.state.avatar.enhance_gpu = reader
    rig.reads = reads
    return rig


def started(rig):
    log = rig.tmp / "worker.jsonl"
    return next(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if '"started"' in line)


def test_the_route_needs_the_token(rig):
    assert rig.client.get("/avatar/enhance").status_code == 401


def test_by_default_everything_is_off_and_the_gpu_is_not_even_read_until_the_page_asks(fake_gpu):
    rig = fake_gpu
    assert rig.get("/avatar/state").status_code == 200 and rig.get("/avatar/output").status_code == 200
    assert rig.reads == []
    body = rig.get("/avatar/enhance").get_json()
    assert rig.reads == [0]
    assert body["settings"] == EnhanceSettings().as_dict() and body["status"]["plan"]["active"] is False
    assert body["capabilities"]["gpu"]["name"] == "NVIDIA GeForce RTX 4090"
    assert body["capabilities"]["upscale"]["basic"]["available"] is True
    assert body["status"]["timing"]["frames_in"] == 0 and "not neural" in body["basic"]


def test_the_report_names_what_is_missing_for_the_nvidia_provider(fake_gpu):
    rig = fake_gpu
    entry = rig.get("/avatar/enhance").get_json()["capabilities"]["upscale"]["rtx_video"]
    assert entry["available"] is False and entry["code"] in ("needs_sdk", "not_windows") and entry["reason"]
    rig.write_settings(avatar={"enhance": {"upscale": "rtx_video", "scale": 2, "rtx_video_path": "C:\\sdk\\rtxvideo.dll"}})
    body = rig.get("/avatar/enhance").get_json()
    assert body["settings"]["rtx_video_path"] == "C:\\sdk\\rtxvideo.dll"
    assert body["status"]["upscale"]["running"] is False and body["status"]["upscale"]["reason"]
    assert body["status"]["plan"]["worker_height"] == 0                    # the worker is not asked to render less for a provider that cannot run


def test_a_damaged_enhance_object_in_the_file_means_everything_off(fake_gpu):
    rig = fake_gpu
    rig.write_settings(avatar={"enhance": {"upscale": "magic", "scale": "big", "interpolate": 7, "factor": 99}})
    assert rig.state.config.avatar_enhance == EnhanceSettings().as_dict()
    rig.write_settings(avatar={"enhance": "on"})
    assert rig.get("/avatar/enhance").get_json()["status"]["plan"]["active"] is False


def test_the_settings_reach_the_config_and_the_plan(fake_gpu):
    rig = fake_gpu
    rig.write_settings(avatar={"enhance": {"upscale": "basic", "scale": 2, "interpolate": "basic", "factor": 2}})
    body = rig.get("/avatar/enhance").get_json()
    assert rig.state.config.avatar_enhance["upscale"] == "basic" and rig.state.settings.avatar.enhance.interpolate == "basic"
    assert body["status"]["plan"] == {"active": True, "worker_height": 240, "worker_fps": 13, "out_height": 480, "out_fps": 25, "out_per_in": 2}
    assert body["status"]["upscale"]["running"] is True and body["status"]["interpolate"]["running"] is True


def test_loading_the_function_starts_the_worker_at_the_reduced_size_and_rate(fake_gpu):
    rig = fake_gpu
    rig.write_settings(avatar={"enhance": {"upscale": "basic", "scale": 3, "interpolate": "basic", "factor": 3}})
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"
    note = started(rig)
    assert (note["height"], note["fps"]) == ("160", "9")
    status = rig.get("/avatar/enhance").get_json()["status"]
    assert status["worker"] == {"running": True, "height": 160, "fps": 9} and status["restart_needed"] is False


def test_changing_the_settings_with_the_worker_running_asks_for_a_restart_and_does_not_touch_it(fake_gpu):
    rig = fake_gpu
    assert rig.apply().get_json()["report"]["avatar"]["state"] == "loaded"
    assert started(rig)["height"] is None
    rig.write_settings(avatar={"enhance": {"upscale": "basic", "scale": 2}})
    status = rig.get("/avatar/enhance").get_json()["status"]
    assert status["restart_needed"] is True and status["worker"]["height"] == 0
    assert rig.app.config["CB_AVATAR"]._proc.poll() is None
