"""ER-NeRF through the training job manager of voice/avatar_training.py: the same plan, confirm, progress and cancel as the other personalised engines, with the
steps of voice/avatar_engines/ernerf.py. Fake processes stand for the upstream scripts; no GPU, model or network."""
import threading
import time

import pytest

from callbridge.avatar_store import AvatarStore

from avatar_fakes import video_file
from voice.avatar_engines import registry
from voice.avatar_engines.ernerf import ErNerfEngine
from voice.avatar_training import TrainingError, TrainingJobs
from voice.tests import engine_pack_fakes as fakes
from voice.tests.test_avatar_engine_ernerf import install_training, mark_modules, train_files

CLIP = {"seconds": 240.0, "fps": 25.0, "width": 160, "height": 160}


class Rig:
    def __init__(self, tmp_path, monkeypatch, spawner):
        self.base = fakes.set_environment(monkeypatch, tmp_path)
        install_training(self.base)
        self.store = AvatarStore(tmp_path / "user" / "voice" / "avatars")
        self.folder = self.store.create("ada", "Ada")
        video_file(self.folder / "clips" / "talk.mp4")
        self.engine = ErNerfEngine(registry.info_of("er_nerf"), spawner)
        monkeypatch.setitem(registry.ENGINES, "er_nerf", self.engine)
        self.jobs = TrainingJobs(self.store, models_base=lambda: str(self.base), python=str(tmp_path / "engine-python.exe"), probe=lambda path: CLIP,
                                 free_vram=lambda gpu: 9000, which=lambda n: "/fake/" + n)

    def wait(self, want, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.jobs.status()["state"] == want:
                return self.jobs.status()
            time.sleep(0.02)
        raise AssertionError(self.jobs.status())


class Steps(fakes.Spawner):
    """What the real steps leave behind: the stage outputs trained.json is made from."""

    def __init__(self, folder, **kw):
        super().__init__(**kw)
        self.folder = folder

    def __call__(self, cmd, cwd, env):
        if "--stage" in cmd:
            train_files(self.folder, checkpoint=cmd[cmd.index("--stage") + 1] == "torso")
        return super().__call__(cmd, cwd, env)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    spawner = Steps(None, out=['{"event": "done", "check": true, "modules": {"pytorch3d": "0.7"}}'])
    r = Rig(tmp_path, monkeypatch, spawner)
    spawner.folder = r.folder
    yield r
    r.jobs.shutdown()


def test_the_plan_names_the_four_steps_and_what_will_be_made_and_takes_no_epochs(rig):
    plan = rig.jobs.plan("ada", "er_nerf")
    assert plan["engine"] == "er_nerf" and [s["id"] for s in plan["stages"]] == ["check", "prepare", "head", "torso"]
    assert plan["ok"] is True                                               # the build check is the first step, not a precondition
    assert any("engines/er_nerf/trained.json" in c["en"] for c in plan["creates"]) and "hours" in plan["note"]["en"] and plan["video"]["clip"] == "talk.mp4"
    with pytest.raises(TrainingError) as err:
        rig.jobs.plan("ada", "er_nerf", epochs=10)
    assert err.value.code == "options"
    with pytest.raises(TrainingError):
        rig.jobs.plan("ada", "er_nerf", asr="wenet")


def test_nothing_starts_without_the_confirmation_of_a_plan_that_was_looked_at(rig):
    token = rig.jobs.plan("ada", "er_nerf")["token"]
    with pytest.raises(TrainingError) as err:
        rig.jobs.start(token, False)
    assert err.value.code == "confirm" and rig.jobs.status()["state"] == "idle"
    with pytest.raises(TrainingError) as err:
        rig.jobs.start("0" * 64, True)
    assert err.value.code == "unknown_plan"


def test_a_confirmed_training_runs_to_the_end_with_progress_and_leaves_trained_json(rig):
    token = rig.jobs.plan("ada", "er_nerf")["token"]
    started = rig.jobs.start(token, True)
    assert started["state"] == "running" and started["engine"] == "er_nerf"
    done = rig.wait("done")
    assert done["percent"] == 100.0 and done["message"] == "trained"
    assert (rig.folder / "engines" / "er_nerf" / "trained.json").is_file()
    assert registry.get("er_nerf").status_of(rig.folder, str(rig.base))["available"] is True


def test_cancel_stops_the_running_step_and_the_job_ends_cancelled(tmp_path, monkeypatch):
    gate = threading.Event()
    spawner = fakes.Spawner(gate=gate)
    r = Rig(tmp_path, monkeypatch, spawner)
    try:
        mark_modules(r.base)
        r.jobs.start(r.jobs.plan("ada", "er_nerf")["token"], True)
        deadline = time.time() + 10
        while not spawner.calls and time.time() < deadline:
            time.sleep(0.02)
        assert r.jobs.cancel() == {"cancelling": True}
        end = r.wait("cancelled")
        assert "cancelled" in end["message"] and not (r.folder / "engines" / "er_nerf" / "trained.json").exists()
    finally:
        r.jobs.shutdown()
