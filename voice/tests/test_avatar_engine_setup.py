"""The routes behind the Set up panel: the items an engine needs, the Python of the engine environment kept from the page, and the explicit check that loads the engine
once. The rig is the avatar service test's; the engine's process is a fake. No GPU, model or network."""
import sys
import time

import pytest

from callbridge import models_store

from voice.avatar_engines import pack, registry
from voice.tests import engine_pack_fakes as fakes
from voice.tests.test_avatar_engine_ernerf import RENDER
from voice.tests.test_avatar_service import _isolated, rig  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def _engine_env(_isolated, rig, monkeypatch):
    monkeypatch.setenv(models_store.ROOT_ENV, str(rig.models))
    monkeypatch.delenv(pack.ENV_PYTHON, raising=False)
    monkeypatch.setattr(pack.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(pack, "kill_tree", lambda proc: proc.kill())


def items(body):
    return {c["id"]: c for c in body["checks"]}


def test_the_routes_need_the_token_and_an_engine_that_has_a_setup(rig):
    assert rig.client.get("/avatar/engines/er_nerf/setup").status_code == 401
    assert rig.get("/avatar/engines/musetalk_15/setup").status_code == 404
    assert rig.post("/avatar/engines/visemes/check").status_code == 404


def test_er_nerf_lists_every_prerequisite_item_by_item(rig):
    body = rig.get("/avatar/engines/er_nerf/setup").get_json()
    got = items(body)
    for key in ("code", "python", "ffmpeg", "ernerf-audio-encoder", "ernerf-bfm", "ernerf-face-parsing", "module:pytorch3d", "module:freqencoder",
                "module:gridencoder", "module:raymarching", "module:shencoder"):
        assert key in got, key
    assert got["python"]["ok"] is False and got["code"]["ok"] is True and "vendor/ernerf" in got["code"]["en"] and "model" not in got["code"] and got["ernerf-bfm"]["scope"] == "training"
    assert got["ffmpeg"]["ok"] is True and body["ready"] is False and body["python"] == {"path": "", "source": "none"}
    assert body["check"] == {"state": "idle"}


def test_the_photo_engines_list_their_files_and_the_check(rig):
    got = items(rig.get("/avatar/engines/wav2lip_v288/setup").get_json())
    assert {"code", "python", "ffmpeg", "wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks", "check"} <= set(got)
    assert items(rig.get("/avatar/engines/sadtalker/setup").get_json())["sadtalker-gfpgan"]["ok"] is False


def test_the_python_is_kept_checked_and_forgotten(rig, tmp_path):
    assert rig.post("/avatar/engines/python", {"path": "relative/python"}).status_code == 400
    assert rig.post("/avatar/engines/python", {"path": str(tmp_path / "nothing.exe")}).status_code == 400
    assert rig.post("/avatar/engines/python", {}).status_code == 400
    ok = rig.post("/avatar/engines/python", {"path": sys.executable})
    assert ok.status_code == 200 and ok.get_json()["python"] == {"path": sys.executable, "source": "stored"}
    assert items(rig.get("/avatar/engines/er_nerf/setup").get_json())["python"]["ok"] is True
    assert pack.python() == sys.executable
    assert rig.post("/avatar/engines/python", {"path": ""}).get_json()["python"]["source"] == "none"


def test_the_variable_wins_over_the_stored_path(rig, monkeypatch):
    rig.post("/avatar/engines/python", {"path": sys.executable})
    monkeypatch.setenv(pack.ENV_PYTHON, "/elsewhere/python")
    assert pack.python() == "/elsewhere/python" and pack.python_source() == "env"


def test_a_check_with_something_missing_is_refused_and_starts_nothing(rig, monkeypatch):
    spawner = fakes.Spawner()
    monkeypatch.setattr(registry.get("sadtalker"), "_spawn", spawner)
    r = rig.post("/avatar/engines/sadtalker/check")
    assert r.status_code == 409 and r.get_json()["code"] == "not_ready" and "TADAMOO_AVATAR_ENGINES_PYTHON" in r.get_json()["error"]
    assert spawner.calls == []


def test_the_check_loads_the_engine_once_and_the_panel_then_shows_it_passed(rig, monkeypatch):
    fakes.install(rig.models, ("sadtalker-weights", "sadtalker-gfpgan"))
    rig.post("/avatar/engines/python", {"path": sys.executable})
    spawner = fakes.Spawner(out=['{"event": "progress", "phase": "loading", "percent": 5}', '{"event": "done", "check": true}'])
    monkeypatch.setattr(registry.get("sadtalker"), "_spawn", spawner)
    started = rig.post("/avatar/engines/sadtalker/check")
    assert started.status_code == 200 and started.get_json()["check"]["state"] in ("running", "done")
    assert rig.wait(lambda: rig.get("/avatar/engines/sadtalker/setup").get_json()["check"]["state"] == "done")
    body = rig.get("/avatar/engines/sadtalker/setup").get_json()
    assert items(body)["check"]["ok"] is True and body["ready"] is True and len(spawner.calls) == 1 and spawner.calls[0][0][-1] == "--check"
    assert (rig.models / "avatar-engines" / "sadtalker" / "check.json").is_file()


def test_a_check_that_fails_says_why_in_the_state(rig, monkeypatch):
    fakes.install(rig.models, ("sadtalker-weights", "sadtalker-gfpgan"))
    rig.post("/avatar/engines/python", {"path": sys.executable})
    monkeypatch.setattr(registry.get("sadtalker"), "_spawn", fakes.Spawner(out=['{"event": "error", "message": "ImportError: facexlib"}'], code=1))
    rig.post("/avatar/engines/sadtalker/check")
    assert rig.wait(lambda: rig.get("/avatar/engines/sadtalker/setup").get_json()["check"]["state"] == "failed")
    body = rig.get("/avatar/engines/sadtalker/setup").get_json()
    assert "facexlib" in body["check"]["error"] and items(body)["check"]["ok"] is False


def test_er_nerf_modules_show_what_the_check_found(rig, monkeypatch):
    fakes.install(rig.models, RENDER)
    rig.post("/avatar/engines/python", {"path": sys.executable})
    done = '{"event": "done", "check": true, "modules": {"pytorch3d": "0.7.5", "gridencoder": "ok"}}'
    monkeypatch.setattr(registry.get("er_nerf"), "_spawn", fakes.Spawner(out=[done]))
    rig.post("/avatar/engines/er_nerf/check")
    assert rig.wait(lambda: rig.get("/avatar/engines/er_nerf/setup").get_json()["check"]["state"] == "done")
    got = items(rig.get("/avatar/engines/er_nerf/setup").get_json())
    assert got["module:pytorch3d"]["ok"] and "0.7.5" in got["module:pytorch3d"]["en"] and got["module:gridencoder"]["ok"]
    assert got["module:freqencoder"]["ok"] is False and "press Check" in got["module:freqencoder"]["en"]


def test_a_second_check_while_one_runs_is_refused(rig, monkeypatch):
    import threading
    fakes.install(rig.models, RENDER)
    rig.post("/avatar/engines/python", {"path": sys.executable})
    gate = threading.Event()
    spawner = fakes.Spawner(gate=gate)
    monkeypatch.setattr(registry.get("er_nerf"), "_spawn", spawner)
    assert rig.post("/avatar/engines/er_nerf/check").status_code == 200
    assert rig.wait(lambda: spawner.calls)
    assert rig.post("/avatar/engines/er_nerf/check").status_code == 409
    gate.set()
    time.sleep(0.1)
