"""The training job manager: a plan with its refusals, estimate and checks; a start only from a confirmed token of an unchanged plan; one job at a time; progress from the
epochs; cancel and stop kill the whole process tree. Fake scripts stand for the upstream code; no GPU, model or network."""
import json
import time

import psutil
import pytest

from callbridge import avatar_assets, models_store
from callbridge.avatar_store import AvatarStore
from voice import avatar_training
from voice.avatar_engines import feathertalk, registry, ultralight
from voice.avatar_engines import personalised
from voice.avatar_engines.personalised import PYTHON_ENV
from voice.avatar_training import TrainingError, TrainingJobs

from avatar_fakes import video_file
from engine_fakes import PY, fake_commands, put_pack

CLIP = {"seconds": 240.0, "fps": 25.0, "width": 160, "height": 160}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(PYTHON_ENV, PY)
    monkeypatch.delenv("FAKE_HANG", raising=False)
    monkeypatch.setattr(models_store, "_service_env", {})
    monkeypatch.delenv(models_store.ROOT_ENV, raising=False)
    saved = {(e, n): registry.ENGINES[e].__dict__.get(n) for e in ("ultralight", "feathertalk") for n in ("convert_cmd", "audio_cmd", "which", "vendor")}
    yield
    for (e, n), v in saved.items():
        registry.ENGINES[e].__dict__.pop(n, None)
        if v is not None:
            registry.ENGINES[e].__dict__[n] = v


class Rig:
    def __init__(self, tmp_path, seconds=240.0, **kw):
        self.models = tmp_path / "models"
        self.store = AvatarStore(tmp_path / "user" / "voice" / "avatars")
        self.folder = self.store.create("ada", "Ada")
        video_file(self.folder / "clips" / "talk.mp4")
        for engine in ("ultralight", "feathertalk"):
            put_pack(self.models, engine)
            fake_commands(registry.ENGINES[engine])
        self.seconds = seconds
        self.jobs = TrainingJobs(self.store, models_base=lambda: str(self.models), probe=lambda path: {**CLIP, "seconds": self.seconds},
                                 free_vram=lambda gpu: 9000, which=lambda n: "/fake/" + n, **{"python": PY, **kw})

    def wait(self, state="running", timeout=30.0, **want):
        end = time.time() + timeout
        while time.time() < end:
            st = self.jobs.status()
            if (state is None or st["state"] != state) and all(st.get(k) == v for k, v in want.items()):
                return st
            if want and all(st.get(k) == v for k, v in want.items()):
                return st
            time.sleep(0.05)
        raise AssertionError(f"timeout: {self.jobs.status()}")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.jobs.shutdown()


# ---------------------------------------------------------------------------------------- the plan
def test_a_plan_says_the_video_the_estimate_the_vram_and_what_will_be_created(rig):
    p = rig.jobs.plan("ada", "ultralight")
    assert p["ok"] is True and p["problems"] == [] and p["video"] == {"clip": "talk.mp4", **CLIP} and p["options"] == {"asr": "wenet", "fps": 20, "epochs": 200, "batch": 16}
    est = p["estimate"]
    assert (est["basis"], est["frames"], est["epochs"], est["frame_epochs"]) == ("assumed", 4800, 200, 960000) and est["seconds"] == [2880, 19200]
    assert "not a promise" in est["note"]["en"] and "assumed" in est["note"]["en"] and "non una promessa" in est["note"]["it"]
    assert p["vram"]["mib"] is None and "Not measured" in p["vram"]["en"] and p["vram"]["free_mib"] == 9000
    assert [c["id"] for c in p["checks"]][0] == "idle" and all(c["ok"] for c in p["checks"]) and {"gpu", "ffmpeg", "vgg19", "landmarks", "python", "audio"} <= {c["id"] for c in p["checks"]}
    assert any("engines/ultralight/dataset" in c["en"] for c in p["creates"]) and len(p["token"]) == 64 and p["license"]["id"] == "Apache-2.0"
    assert rig.jobs.plan("ada", "feathertalk", epochs=10)["estimate"]["frames"] == 6000
    assert rig.jobs.plan("ada", "ultralight")["token"] == p["token"] != rig.jobs.plan("ada", "ultralight", epochs=5)["token"]


@pytest.mark.parametrize("seconds, code", [(90.0, "too_short"), (119.0, "too_short"), (601.0, "too_long"), (1200.0, "too_long")])
def test_a_video_under_two_minutes_or_over_ten_is_refused_with_the_plain_reason(tmp_path, seconds, code):
    jobs = Rig(tmp_path, seconds).jobs
    p = jobs.plan("ada", "feathertalk")
    assert p["ok"] is False and [x["code"] for x in p["problems"]] == [code]
    assert f"{seconds:.0f} s long" in p["problems"][0]["en"] and ("at least 2 minutes" in p["problems"][0]["en"] or "at most 10 minutes" in p["problems"][0]["en"])
    assert "Il video dura" in p["problems"][0]["it"]
    with pytest.raises(TrainingError) as e:
        jobs.start(p["token"], True)
    assert (e.value.status, e.value.code) == (422, "not_possible")


def test_the_edges_of_the_range_are_accepted(tmp_path):
    for seconds in (120.0, 600.0):
        assert Rig(tmp_path / str(seconds), seconds).jobs.plan("ada", "feathertalk")["problems"] == []


def test_an_avatar_without_a_readable_video_is_refused(tmp_path):
    rig = Rig(tmp_path)
    (rig.folder / "clips" / "talk.mp4").unlink()
    assert [x["code"] for x in rig.jobs.plan("ada", "feathertalk")["problems"]] == ["no_video"]
    rig2 = Rig(tmp_path / "b")
    rig2.jobs.probe = lambda path: (_ for _ in ()).throw(ValueError("bad"))
    assert [x["code"] for x in rig2.jobs.plan("ada", "feathertalk")["problems"]] == ["no_video"]


def test_the_longest_clip_is_the_one_used(tmp_path):
    rig = Rig(tmp_path)
    video_file(rig.folder / "clips" / "long.mp4", frames=40)
    rig.jobs.probe = lambda path: {**CLIP, "seconds": 300.0 if path.name == "long.mp4" else 200.0}
    assert rig.jobs.plan("ada", "feathertalk")["video"]["clip"] == "long.mp4"


def test_the_checks_name_what_is_missing_and_make_the_plan_not_ok(rig, monkeypatch):
    monkeypatch.delenv(PYTHON_ENV)
    rig.jobs.python = None
    rig.jobs.free_vram = lambda gpu: None
    (rig.models / "avatar-engines" / "torch-home" / "hub" / "checkpoints" / "vgg19-dcbb9e9d.pth").unlink()
    p = rig.jobs.plan("ada", "feathertalk")
    bad = {c["id"]: c for c in p["checks"] if not c["ok"]}
    assert p["ok"] is False and set(bad) == {"python", "vgg19", "gpu"} and "cannot be read" in bad["gpu"]["en"] and PYTHON_ENV in bad["python"]["en"]


def test_a_bad_engine_avatar_or_option_is_an_error_not_a_plan(rig):
    for args, status, code in ((("ada", "visemes"), 400, "engine"), (("ada", None), 400, "engine"), (("nobody", "ultralight"), 404, "not_found"),
                               (("../x", "ultralight"), 404, "not_found"), (("ada", "ultralight", 0), 400, "options"), (("ada", "ultralight", 5, "mp3"), 400, "options")):
        with pytest.raises(TrainingError) as e:
            rig.jobs.plan(*args)
        assert (e.value.status, e.value.code) == (status, code), args


# ---------------------------------------------------------------------------------------- confirm, token
def test_nothing_starts_without_confirm_true_on_a_token_that_was_planned(rig):
    p = rig.jobs.plan("ada", "feathertalk", epochs=2)
    for confirm in (None, False, "yes", 1):
        with pytest.raises(TrainingError) as e:
            rig.jobs.start(p["token"], confirm)
        assert (e.value.status, e.value.code) == (409, "confirm") and e.value.extra["plan"]["token"] == p["token"]
    for token in (None, "", "0" * 64, 5):
        with pytest.raises(TrainingError) as e:
            rig.jobs.start(token, True)
        assert (e.value.status, e.value.code) == (409, "unknown_plan")
    assert rig.jobs.status()["state"] == "idle" and not (rig.folder / "engines").exists()


def test_a_plan_that_changed_since_it_was_shown_is_refused_with_the_new_plan(rig):
    p = rig.jobs.plan("ada", "feathertalk", epochs=2)
    video_file(rig.folder / "clips" / "talk.mp4", frames=33)
    with pytest.raises(TrainingError) as e:
        rig.jobs.start(p["token"], True)
    assert (e.value.status, e.value.code) == (409, "plan_changed") and e.value.extra["plan"]["token"] != p["token"]
    assert rig.jobs.status()["state"] == "idle"


# ---------------------------------------------------------------------------------------- a job
def test_a_confirmed_plan_runs_to_the_end_with_progress_and_the_next_estimate_is_measured(rig):
    p = rig.jobs.plan("ada", "feathertalk", epochs=6)
    st = rig.jobs.start(p["token"], True)
    assert st["state"] == "running" and st["avatar"] == "ada" and st["engine"] == "feathertalk"
    done = rig.wait()
    assert done["state"] == "done" and done["percent"] == 100.0 and done["error"] == "" and done["message"] == "trained and exported" and done["epoch"] == 6 and done["epochs"] == 6
    assert done["log"] and any(line.startswith("Epoch 6/6") for line in done["log"])
    folder = rig.folder / "engines" / "feathertalk"
    assert json.loads((folder / "trained.json").read_text())["epochs"] == 6 and (folder / "checkpoints" / "last.pth").is_file()
    assert "Epoch 6/6" in (folder / "log_tail.txt").read_text()
    assert registry.ENGINES["feathertalk"].status_of(rig.folder, str(rig.models))["available"] is True
    assert "feathertalk" in avatar_assets.manifest(rig.folder, "ada")["engines"]
    timing = json.loads((rig.folder.parent.parent / avatar_training.TIMING_FILE).read_text())["feathertalk"]
    assert timing["s_per_frame_epoch"] > 0 and timing["epochs"] == 6
    again = rig.jobs.plan("ada", "feathertalk", epochs=10)
    assert again["estimate"]["basis"] == "measured" and again["estimate"]["seconds"][0] == again["estimate"]["seconds"][1]
    assert (again["resume_epoch"], again["estimate"]["epochs"]) == (6, 4)                    # it continues after the 6 epochs that were saved
    fresh = rig.jobs.plan("ada", "feathertalk", epochs=10, fresh=True)
    assert (fresh["resume_epoch"], fresh["estimate"]["epochs"]) == (0, 10) and fresh["token"] != again["token"]


def test_the_progress_follows_the_phases_in_order(tmp_path):
    rig = Rig(tmp_path)
    seen = []
    original = rig.jobs._progress

    def spy(phase, percent, **info):
        seen.append((phase, round(rig.jobs.status()["percent"], 1)))
        original(phase, percent, **info)
    rig.jobs._progress = spy
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=3)["token"], True)
    rig.wait()
    phases = [p for p, _ in seen]
    assert phases == sorted(phases, key=["convert", "preprocess", "train", "export"].index)
    overall = [rig.jobs.status()["percent"]]
    assert overall == [100.0] and seen[0][0] == "convert"


def test_one_job_at_a_time_and_cancel_ends_the_process_and_its_children(rig, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    p = rig.jobs.plan("ada", "feathertalk", epochs=50)
    rig.jobs.start(p["token"], True)
    st = rig.wait(state=None, epoch=1)
    assert st["state"] == "running" and st["percent"] >= 12
    assert rig.jobs.busy() and rig.jobs.busy_with("ada") and not rig.jobs.busy_with("other")
    again = rig.jobs.plan("ada", "ultralight", epochs=5)
    assert again["ok"] is False and [c["id"] for c in again["checks"] if not c["ok"]] == ["idle"]
    with pytest.raises(TrainingError) as e:
        rig.jobs.start(p["token"], True)
    assert (e.value.status, e.value.code) == (409, "busy")
    child = int((rig.folder / "engines" / "feathertalk" / "child.pid").read_text())
    assert psutil.pid_exists(child)
    assert rig.jobs.cancel() == {"cancelling": True}
    done = rig.wait()
    assert done["state"] == "cancelled" and "continues from them" in done["message"] and not rig.jobs.busy()
    for _ in range(50):
        if not psutil.pid_exists(child):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(child)
    assert rig.jobs.cancel() == {"cancelling": False}                                      # nothing runs: nothing to cancel
    assert rig.jobs.status()["engine"] == "feathertalk"                                    # the last job stays visible until the next


def test_stopping_the_service_ends_a_running_job_and_its_children(rig, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=50)["token"], True)
    rig.wait(state=None, epoch=1)
    child = int((rig.folder / "engines" / "feathertalk" / "child.pid").read_text())
    rig.jobs.shutdown()
    assert not rig.jobs.busy() and rig.jobs.status()["state"] == "cancelled"
    for _ in range(50):
        if not psutil.pid_exists(child):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(child)


def test_a_failing_training_ends_failed_with_the_reason_and_the_log(rig):
    (rig.models.parent / "fake-vendor" / "feathertalk" / "train.py").write_text("print('boom'); import sys; sys.exit(2)")
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=3)["token"], True)
    done = rig.wait()
    assert done["state"] == "failed" and "train failed (exit code 2)" in done["error"] and "boom" in done["log"]
    assert "boom" in (rig.folder / "engines" / "feathertalk" / "log_tail.txt").read_text() and not (rig.folder / "engines" / "feathertalk" / "trained.json").exists()
    assert rig.jobs.busy() is False


def test_a_cancelled_training_continues_from_its_checkpoints(rig, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=50)["token"], True)
    rig.wait(state=None, epoch=1)
    rig.jobs.cancel()
    rig.wait()
    monkeypatch.delenv("FAKE_HANG")
    assert rig.jobs.plan("ada", "feathertalk", epochs=4)["resume_epoch"] == 0                # nothing was saved yet: from the start
    folder = rig.folder / "engines" / "feathertalk"
    (folder / "checkpoints").mkdir(parents=True, exist_ok=True)
    (folder / "checkpoints" / "9.pth").write_text("epoch=9")
    (folder / "checkpoints" / "last.pth").write_text("epoch=9")
    assert (folder / "dataset" / "ready.json").is_file()
    p = rig.jobs.plan("ada", "feathertalk", epochs=14)
    assert (p["resume_epoch"], p["estimate"]["epochs"]) == (10, 4)


# ---------------------------------------------------------------------------------------- the Set up panel
def test_setup_lists_each_prerequisite_with_the_entry_that_fixes_it_and_where_a_file_goes(tmp_path):
    rig = Rig(tmp_path, python=None)
    (rig.models / "avatar-engines" / "landmarks" / "scrfd_2.5g_kps.onnx").unlink()
    (rig.models / "avatar-engines" / "feathertalk" / "weights" / "feather_hubert.pth").unlink()
    (rig.models / "avatar-engines" / "torch-home" / "hub" / "checkpoints" / "vgg19-dcbb9e9d.pth").unlink()
    s = rig.jobs.setup("feathertalk")
    items = {i["id"]: i for i in s["items"]}
    assert s["ok"] is False and s["environment"] is None and s["trained"] is None
    assert items["landmarks"]["install"] == "engine-landmarks" and items["audio"]["install"] == "feathertalk-weights" and "install" not in items["vgg19"]
    assert items["audio"]["where"].endswith("weights") and items["vgg19"]["where"].endswith("checkpoints") and items["python"]["ok"] and items["ffmpeg"]["ok"]
    assert s["python"] == {"path": PY, "source": "env", "env": PYTHON_ENV}
    assert "platform" not in {i["id"] for i in rig.jobs.setup("ultralight")["items"]}
    for want in ("wenet", "hubert"):
        assert rig.jobs.setup("ultralight", want)["options"]["asr"] == want
    audio = lambda asr: {i["id"]: i for i in rig.jobs.setup("ultralight", asr)["items"]}["audio"]
    assert audio("wenet")["ok"] and not audio("hubert")["ok"] and audio("hubert")["where"].endswith("snapshots")
    with pytest.raises(TrainingError) as e:
        rig.jobs.setup("ultralight", "mp3")
    assert e.value.status == 400


def test_the_python_is_chosen_in_the_panel_checked_for_existing_and_kept_over_the_variable(tmp_path, monkeypatch):
    rig = Rig(tmp_path, python=None)
    for bad in ("relative/python", str(tmp_path / "no-python.exe"), 5):
        with pytest.raises(TrainingError) as e:
            rig.jobs.set_python(bad)
        assert (e.value.status, e.value.code) == (400, "python") or bad == 5
    monkeypatch.delenv(PYTHON_ENV)
    assert rig.jobs.setup("feathertalk")["python"]["source"] == "none"
    assert {i["id"]: i for i in rig.jobs.setup("feathertalk")["items"]}["python"]["ok"] is False
    assert rig.jobs.set_python(PY) == {"path": PY, "effective": PY, "source": "settings"}
    assert {i["id"]: i for i in rig.jobs.setup("feathertalk")["items"]}["python"]["ok"] is True
    monkeypatch.setenv(PYTHON_ENV, "/elsewhere/python")
    assert rig.jobs.setup("feathertalk")["python"] == {"path": "/elsewhere/python", "source": "env", "env": PYTHON_ENV}      # the variable wins when it is set, as for the other engines
    assert rig.jobs.set_python("")["effective"] == "/elsewhere/python"
    assert (rig.models / "avatar-engines" / "python.json").is_file()                                                     # the file every engine that runs upstream code reads


def test_the_probe_names_each_package_and_cuda_and_runs_only_when_asked(tmp_path):
    asked = []

    def probe(python, modules):
        asked.append((python, tuple(modules)))
        return {"torch": "2.1.0", "torchvision": "0.16", "cv2": "4.10", "numpy": "1.26", "onnx": None, "onnxruntime": "1.17", "librosa": "0.10", "soundfile": "0.12", "tqdm": "4", "yaml": "6",
                "cuda": False}
    rig = Rig(tmp_path, probe_env=probe)
    assert rig.jobs.setup("ultralight", "wenet")["environment"] is None and asked == []
    s = rig.jobs.setup("ultralight", "wenet", probe=True)
    env = {c["id"]: c for c in s["environment"]}
    assert asked == [(PY, ultralight.UltralightEngine.MODULES + ("yaml",))] and env["module-torch"]["ok"] and "2.1.0" in env["module-torch"]["en"]
    assert env["module-onnx"]["ok"] is False and "not installed" in env["module-onnx"]["en"] and env["cuda"]["ok"] is False and s["ok"] is False
    assert feathertalk.FeatherTalkEngine.MODULES == tuple(c["id"][7:] for c in rig.jobs.setup("feathertalk", probe=True)["environment"] if c["id"].startswith("module-"))
    rig.jobs.probe_env = lambda python, modules: {"error": "boom"}
    assert rig.jobs.setup("feathertalk", probe=True)["environment"][0]["id"] == "environment"
    rig.jobs.probe_env = lambda python, modules: {**{m: "1" for m in modules}, "cuda": True}
    assert rig.jobs.setup("feathertalk", probe=True)["ok"] is True


def test_setup_for_an_avatar_says_what_is_trained_and_export_makes_the_phone_files_again(rig):
    assert rig.jobs.setup("feathertalk", avatar="ada")["trained"] is None
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=2)["token"], True)
    rig.wait()
    t = rig.jobs.setup("feathertalk", avatar="ada")["trained"]
    assert t["epochs"] == 2 and t["exported"] is True and t["export_error"] == ""
    export = rig.folder / "engines" / "feathertalk" / "export"
    (export / "feathertalk_144.onnx").unlink()
    out = rig.jobs.export("ada", "feathertalk")
    assert out["files"] > 4 and (export / "feathertalk_144.onnx").is_file()
    with pytest.raises(TrainingError) as e:
        rig.jobs.export("ada", "ultralight")
    assert (e.value.status, e.value.code) == (422, "export_failed") and "not been trained" in str(e.value)
    with pytest.raises(TrainingError) as e:
        rig.jobs.setup("feathertalk", avatar="nobody")
    assert e.value.status == 404


def test_an_export_is_refused_while_a_training_runs(rig, monkeypatch):
    monkeypatch.setenv("FAKE_HANG", "1")
    rig.jobs.start(rig.jobs.plan("ada", "feathertalk", epochs=50)["token"], True)
    rig.wait(state=None, epoch=1)
    with pytest.raises(TrainingError) as e:
        rig.jobs.export("ada", "feathertalk")
    assert (e.value.status, e.value.code) == (409, "busy")
