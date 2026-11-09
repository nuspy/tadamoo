"""ER-NeRF as an engine: status_of (pack, environment, weights, checked CUDA modules, trained checkpoint), the training plan and its stages, the progress of
a training run on fakes, what trained.json holds, and the render command from it. No GPU, no upstream code, no network."""
import json
from pathlib import Path

import pytest

from voice.avatar_engines import ernerf as mod, pack, registry
from voice.avatar_engines.ernerf import ErNerfEngine
from voice.tests import engine_pack_fakes as fakes

RENDER = ("ernerf-code", "ernerf-audio-encoder")
TRAIN = ("ernerf-bfm", "ernerf-face-parsing")
CHECKPOINT = "torso/checkpoints/ngp_ep0007.pth"
POSE = "data/transforms_train.json"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return fakes.set_environment(monkeypatch, tmp_path), tmp_path


def engine(spawner=None):
    return ErNerfEngine(registry.info_of("er_nerf"), *([spawner] if spawner else []))


def mark_modules(base):
    root = pack.engine_dir("ernerf", str(base))
    root.mkdir(parents=True, exist_ok=True)
    (root / mod.MODULES_MARK).write_text("{}", encoding="utf-8")


def train_files(folder: Path, checkpoint=True):
    work = folder / "engines" / "er_nerf"
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "data" / "transforms_train.json").write_text("{}", encoding="utf-8")
    (work / "torso" / "checkpoints").mkdir(parents=True, exist_ok=True)
    if checkpoint:
        (work / "torso" / "checkpoints" / "ngp_ep0007.pth").write_bytes(b"x")
    return work


def trained(folder: Path, **extra):
    work = train_files(folder)
    (work / "trained.json").write_text(json.dumps({"checkpoint": CHECKPOINT, "pose": POSE, **extra}), encoding="utf-8")
    return work


def clip_in(tmp: Path) -> Path:
    clip = tmp / "clip.mp4"
    clip.write_bytes(b"video")
    return clip


def test_the_registry_builds_this_engine_and_says_it_is_fragile_and_personalised():
    info = registry.info_of("er_nerf")
    assert isinstance(registry.get("er_nerf"), ErNerfEngine) and info.kind == "personalized" and info.training.needed and info.runs_on == ("pc",)
    assert "fragile" in " ".join(n["en"] for n in info.needs).lower() and "PyTorch3D" in " ".join(n["en"] for n in info.needs)


def test_status_matrix_from_nothing_to_available(rig):
    base, tmp = rig
    folder = fakes.avatar(tmp, photo=False)
    e = engine()
    first = e.status_of(folder, str(base))["reason"]
    for part in ("ER-NeRF code", "audio encoder", "CUDA modules", "not been trained"):
        assert part in first, part
    fakes.install(base, RENDER)
    reason = e.status_of(folder, str(base))["reason"]
    assert "ER-NeRF code" not in reason and "CUDA modules" in reason and "not been trained" in reason
    mark_modules(base)
    assert "CUDA modules" not in e.status_of(folder, str(base))["reason"]
    work = trained(folder)
    assert e.status_of(folder, str(base)) == {"available": True, "reason": "", "reason_it": ""}          # no photo needed: it draws from the trained model
    (work / "torso" / "checkpoints" / "ngp_ep0007.pth").unlink()
    assert "missing" in e.status_of(folder, str(base))["reason"]


def test_the_italian_reason_is_there_too(rig):
    base, tmp = rig
    reason_it = engine().status_of(fakes.avatar(tmp), str(base))["reason_it"]
    assert "non è stato addestrato" in reason_it and "moduli CUDA" in reason_it


def test_the_render_command_uses_what_the_training_recorded(rig):
    base, tmp = rig
    folder = fakes.avatar(tmp)
    work = trained(folder, asr_model="ave")
    root, py = base / "avatar-engines" / "ernerf", str(tmp / "engine-python.exe")
    assert engine().render_command(str(base), tmp / "o", tmp / "a.wav", 450, 25, folder) == [
        py, str(pack.RUNNERS / "ernerf_run.py"), "--pack", str(root / "code"), "--checkpoint", str(work / CHECKPOINT), "--pose", str(work / POSE),
        "--asr-model", "ave", "--audio", str(tmp / "a.wav"), "--frames-out", str(tmp / "o"), "--height", "450", "--fps", "25"]
    assert engine().check_command(str(base)) == [py, str(pack.RUNNERS / "ernerf_run.py"), "--pack", str(root / "code"), "--engine-dir", str(root), "--check"]


def test_render_hands_over_frames_of_a_trained_avatar(rig):
    base, tmp = rig
    fakes.install(base, RENDER)
    mark_modules(base)
    folder = fakes.avatar(tmp)
    trained(folder)
    spawner = fakes.Spawner(out=['{"event": "done", "frames": 2}'], frames=2)
    got = list(engine(spawner).render(b"\x00\x00" * 80, 16000, 450, 25, avatar_dir=folder))
    assert len(got) == 2 and "--checkpoint" in spawner.calls[0][0] and "--asr-model" in spawner.calls[0][0]


def test_the_plan_lists_the_steps_and_what_is_missing_and_starts_nothing(rig):
    base, tmp = rig
    folder = fakes.avatar(tmp)
    spawner = fakes.Spawner()
    plan = engine(spawner).training_plan(folder, clip_in(tmp), None, str(base))
    assert spawner.calls == [] and plan["ready"] is False and plan["engine"] == "er_nerf"
    assert [s["id"] for s in plan["stages"]] == ["check", "prepare", "head", "torso"]
    bad = " ".join(c["en"] for c in plan["checks"] if not c["ok"])
    for part in ("3DMM", "face parsing", "ER-NeRF code"):
        assert part in bad, part
    assert "hours" in plan["note"]["en"] and plan["options"] == {"iters_head": mod.ITERS, "iters_torso": mod.ITERS}
    assert not (folder / "engines").exists()                                   # a plan writes nothing


def test_the_plan_is_ready_when_everything_is_in_place_and_skips_the_check_once_passed(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    folder = fakes.avatar(tmp)
    e = engine()
    plan = e.training_plan(folder, clip_in(tmp), {"iters_head": 5000, "iters_torso": 6000}, str(base))
    assert plan["ready"] and plan["options"] == {"iters_head": 5000, "iters_torso": 6000}
    mark_modules(base)
    assert [s["id"] for s in e.training_plan(folder, clip_in(tmp), None, str(base))["stages"]] == ["prepare", "head", "torso"]
    no_clip = e.training_plan(folder, None, None, str(base))
    assert not no_clip["ready"] and "no video" in " ".join(c["en"] for c in no_clip["checks"] if not c["ok"])


def test_a_data_path_with_a_space_is_refused_because_the_upstream_does_not_quote_it(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    folder = fakes.avatar(tmp / "a b")
    checks = engine().training_checks(folder, clip_in(tmp), str(base), {})
    assert [c for c in checks if c["id"] == "path"][0]["ok"] is False


@pytest.mark.parametrize("bad", [{"iters_head": 0}, {"iters_torso": "9"}, {"iters_head": True}, {"iters_torso": 10 ** 9}])
def test_options_are_checked(bad):
    with pytest.raises(ValueError):
        engine().training_options(bad)


def test_the_stages_have_these_commands(rig):
    base, tmp = rig
    folder = fakes.avatar(tmp)
    root = base / "avatar-engines" / "ernerf"
    work, py = folder / "engines" / "er_nerf", str(tmp / "engine-python.exe")
    stages = engine().training_stages(folder, clip_in(tmp), {"iters_head": 11, "iters_torso": 22}, str(base))
    assert (work / "data" / "video.mp4").read_bytes() == b"video"
    check, prepare, head, torso = stages
    assert check.argv == tuple(engine().check_command(str(base))) and check.cwd == root / "code"
    assert prepare.argv == (py, "data_utils/process.py", str(work / "data" / "video.mp4"), "--task", "-1", "--asr", "deepspeech")
    assert prepare.cwd == root / "code" / "NeRF" and prepare.needs_done is False
    train = (py, str(pack.RUNNERS / "ernerf_train.py"), "--pack", str(root / "code"), "--data", str(work / "data"), "--asr-model", "deepspeech")
    assert head.argv == train + ("--stage", "head", "--workspace", str(work / "head"), "--iters", "11")
    assert torso.argv == train + ("--stage", "torso", "--workspace", str(work / "torso"), "--head-workspace", str(work / "head"), "--iters", "22")
    assert [s.weight for s in stages] == [2, 10, 40, 48]


def test_a_training_runs_the_steps_in_order_reports_progress_and_writes_trained_json(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    folder = fakes.avatar(tmp)
    seen = []

    class Steps(fakes.Spawner):
        def __call__(self, cmd, cwd, env):
            if "--stage" in cmd:                                               # what the real stage leaves behind
                train_files(folder, checkpoint=cmd[cmd.index("--stage") + 1] == "torso")
            return super().__call__(cmd, cwd, env)
    spawner = Steps(out=['{"event": "progress", "phase": "x", "percent": 50}', '{"event": "done", "check": true, "modules": {"gridencoder": "ok"}}'])
    mark = engine(spawner).prepare(folder, lambda phase, pct: seen.append((phase, pct)), clip=clip_in(tmp), options={"iters_head": 1000, "iters_torso": 2000},
                                   models_base=str(base))
    scripts = [Path(c[0][1]).name for c in spawner.calls]
    assert scripts == ["ernerf_run.py", "process.py", "ernerf_train.py", "ernerf_train.py"]
    assert [p for p, _ in seen][0] == "check" and seen[-1] == ("done", 100.0)
    percents = [pct for _, pct in seen]
    assert percents == sorted(percents) and percents[0] == 0.0 and percents[-1] == 100.0
    assert mark["checkpoint"] == CHECKPOINT and mark["pose"] == POSE and mark["asr_model"] == "deepspeech"
    assert json.loads((folder / "engines" / "er_nerf" / "trained.json").read_text(encoding="utf-8"))["iters"] == {"iters_head": 1000, "iters_torso": 2000}
    marker = pack.engine_dir("ernerf", str(base)) / mod.MODULES_MARK                     # the check passed once and is not repeated
    assert json.loads(marker.read_text(encoding="utf-8"))["modules"] == {"gridencoder": "ok"}


def test_a_failed_step_stops_the_training_and_names_it(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    mark_modules(base)
    spawner = fakes.Spawner(out=['{"event": "error", "message": "ImportError: pytorch3d"}'], code=1)
    with pytest.raises(pack.EngineFailed, match="pytorch3d"):
        engine(spawner).prepare(fakes.avatar(tmp), lambda p, c: None, clip=clip_in(tmp), models_base=str(base))
    assert len(spawner.calls) == 1                                              # nothing ran after the first failure


def test_a_training_that_leaves_no_checkpoint_is_not_called_trained(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    mark_modules(base)
    folder = fakes.avatar(tmp)
    with pytest.raises(pack.EngineFailed, match="without a torso checkpoint"):
        engine(fakes.Spawner(out=['{"event": "done"}'])).prepare(folder, lambda p, c: None, clip=clip_in(tmp), models_base=str(base))
    assert not (folder / "engines" / "er_nerf" / "trained.json").exists()


def test_prepare_refuses_without_starting_when_the_checks_fail(rig):
    base, tmp = rig
    spawner = fakes.Spawner()
    with pytest.raises(pack.EngineNotReady, match="no video"):
        engine(spawner).prepare(fakes.avatar(tmp), lambda p, c: None, clip=None, models_base=str(base))
    assert spawner.calls == []


def test_stop_between_steps_ends_the_training(rig):
    base, tmp = rig
    fakes.install(base, RENDER + TRAIN)
    mark_modules(base)
    e = engine()

    class Stopping(fakes.Spawner):
        def __call__(self, cmd, cwd, env):
            e.stop()                                                            # the person cancels while the first step runs
            return super().__call__(cmd, cwd, env)
    spawner = Stopping(out=['{"event": "done"}'])
    e._spawn = spawner
    with pytest.raises(pack.EngineFailed, match="stopped"):
        e.prepare(fakes.avatar(tmp), lambda p, c: None, clip=clip_in(tmp), models_base=str(base))
    assert len(spawner.calls) == 1
