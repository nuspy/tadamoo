"""The training routes of the voice service: they need the token, the plan carries its refusals, a start needs the confirmed token, the status and cancel work, and an
avatar that is being trained cannot be deleted. The job manager is the real one with fake scripts; the rig is the avatar service test's."""
import time

import pytest

from callbridge import models_store
from voice.avatar_engines import personalised, registry
from voice.avatar_engines.personalised import PYTHON_ENV
from voice.avatar_training import TrainingJobs
from voice.tests.test_avatar_service import _isolated, rig  # noqa: F401 (fixtures)

from avatar_fakes import video_file
from engine_fakes import PY, fake_commands, put_pack

CLIP = {"seconds": 240.0, "fps": 25.0, "width": 160, "height": 160}


@pytest.fixture
def trained_rig(rig, monkeypatch):
    monkeypatch.setenv(PYTHON_ENV, PY)
    monkeypatch.delenv("FAKE_HANG", raising=False)
    monkeypatch.setattr(models_store, "_service_env", {})
    saved = {n: registry.ENGINES["feathertalk"].__dict__.get(n) for n in ("convert_cmd", "audio_cmd", "which", "vendor")}
    put_pack(rig.models, "feathertalk")
    fake_commands(registry.ENGINES["feathertalk"])
    video_file(rig.store.path("ada") / "clips" / "walk.mp4")
    jobs = TrainingJobs(rig.store, models_base=lambda: str(rig.models), python=PY, probe=lambda p: CLIP, free_vram=lambda g: 8000, which=lambda n: "/fake/" + n)
    rig.app.config["CB_AVATAR_TRAINING"] = jobs
    yield rig
    jobs.shutdown()
    for n, v in saved.items():
        registry.ENGINES["feathertalk"].__dict__.pop(n, None)
        if v is not None:
            registry.ENGINES["feathertalk"].__dict__[n] = v


def test_the_routes_need_the_token(rig):
    for method, path in (("post", "/avatar/train/plan"), ("post", "/avatar/train/start"), ("get", "/avatar/train/status"), ("post", "/avatar/train/cancel")):
        assert getattr(rig.client, method)(path).status_code == 401, path


def test_a_plan_is_answered_with_its_token_and_a_bad_request_is_a_plain_error(trained_rig):
    r = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 3})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and len(body["token"]) == 64 and body["options"]["epochs"] == 3
    assert trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "musetalk_15"}).status_code == 400
    assert trained_rig.post("/avatar/train/plan", {"avatar": "nobody", "engine": "feathertalk"}).status_code == 404
    bad = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 0})
    assert bad.status_code == 400 and bad.get_json()["code"] == "options"


def test_a_start_without_confirm_is_refused_and_returns_the_plan(trained_rig):
    token = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 3}).get_json()["token"]
    r = trained_rig.post("/avatar/train/start", {"token": token})
    assert r.status_code == 409 and r.get_json()["code"] == "confirm" and r.get_json()["plan"]["token"] == token
    assert trained_rig.get("/avatar/train/status").get_json()["state"] == "idle"
    assert trained_rig.post("/avatar/train/start", {"token": "0" * 64, "confirm": True}).get_json()["code"] == "unknown_plan"


def test_a_confirmed_plan_runs_and_the_avatar_cannot_be_deleted_meanwhile(trained_rig, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    token = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 50}).get_json()["token"]
    r = trained_rig.post("/avatar/train/start", {"token": token, "confirm": True})
    assert r.status_code == 200 and r.get_json()["state"] == "running"
    assert trained_rig.wait(lambda: trained_rig.get("/avatar/train/status").get_json()["epoch"] == 1)
    gone = trained_rig.client.delete("/avatar/ada", headers={"Authorization": "Bearer t0ken-for-tests"})
    assert gone.status_code == 409 and "being trained" in gone.get_json()["error"]
    again = trained_rig.post("/avatar/train/start", {"token": token, "confirm": True})
    assert again.status_code == 409 and again.get_json()["code"] == "busy"
    assert trained_rig.post("/avatar/train/cancel").get_json() == {"cancelling": True}
    assert trained_rig.wait(lambda: trained_rig.get("/avatar/train/status").get_json()["state"] == "cancelled")
    time.sleep(0.1)
    assert trained_rig.client.delete("/avatar/ada", headers={"Authorization": "Bearer t0ken-for-tests"}).status_code == 200


def test_the_engines_route_shows_the_training_state_of_the_avatar(trained_rig):
    token = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 2}).get_json()["token"]
    trained_rig.post("/avatar/train/start", {"token": token, "confirm": True})
    assert trained_rig.wait(lambda: trained_rig.get("/avatar/train/status").get_json()["state"] == "done")
    engines = {e["id"]: e for e in trained_rig.get("/avatar/engines").get_json()["engines"]}
    assert engines["feathertalk"]["status"]["trained"] is True and "install `feathertalk-weights`" not in engines["feathertalk"]["status"]["reason"]


def test_the_set_up_routes_answer_the_panel(trained_rig):
    r = trained_rig.post("/avatar/train/setup", {"engine": "feathertalk", "avatar": "ada"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and body["trained"] is None and body["python"]["source"] == "given" and {i["id"] for i in body["items"]} >= {"audio", "landmarks", "python"}
    assert trained_rig.post("/avatar/train/setup", {"engine": "musetalk_15"}).status_code == 400
    assert trained_rig.post("/avatar/train/setup", {"engine": "feathertalk", "avatar": "nobody"}).status_code == 404
    bad = trained_rig.post("/avatar/train/python", {"path": "relative"})
    assert bad.status_code == 400 and bad.get_json()["code"] == "python"
    assert trained_rig.post("/avatar/train/python", {"path": PY}).get_json()["path"] == PY
    assert trained_rig.post("/avatar/train/export", {"avatar": "ada", "engine": "feathertalk"}).get_json()["code"] == "export_failed"
    token = trained_rig.post("/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk", "epochs": 2}).get_json()["token"]
    trained_rig.post("/avatar/train/start", {"token": token, "confirm": True})
    assert trained_rig.wait(lambda: trained_rig.get("/avatar/train/status").get_json()["state"] == "done")
    assert trained_rig.post("/avatar/train/setup", {"engine": "feathertalk", "avatar": "ada"}).get_json()["trained"]["exported"] is True
    assert trained_rig.post("/avatar/train/export", {"avatar": "ada", "engine": "feathertalk"}).get_json()["files"] > 4
