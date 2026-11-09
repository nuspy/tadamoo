"""Wav2Lip v2 as an engine: what status_of says is missing, the exact command lines, the progress of the runner, the frames it hands over.
Fakes only: no GPU, no upstream code, no model, no network."""
from pathlib import Path

import pytest

from callbridge import models_store

from voice.avatar_engines import pack, registry
from voice.avatar_engines.wav2lipv2 import Wav2LipV2Engine
from voice.tests import engine_pack_fakes as fakes

IDS = ("wav2lipv2-code", "wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    base = fakes.set_environment(monkeypatch, tmp_path)
    return base, tmp_path


def engine(spawner=None):
    return Wav2LipV2Engine(registry.info_of("wav2lip_v288"), *([spawner] if spawner else []))


def test_the_registry_builds_this_engine_for_wav2lip_v288():
    assert isinstance(registry.get("wav2lip_v288"), Wav2LipV2Engine)
    info = registry.info_of("wav2lip_v288")
    assert info.native_resolution == 288 and info.runs_on == ("pc", "web") and info.license.verify and "AGPL" in info.license.note["en"]


def test_status_names_everything_that_is_missing_in_both_languages(tmp_path, monkeypatch):
    base = fakes.set_environment(monkeypatch, tmp_path, python=False, ffmpeg=False)
    folder = fakes.avatar(tmp_path, photo=False)
    st = engine().status_of(folder, str(base))
    assert st["available"] is False
    for part in ("TADAMOO_AVATAR_ENGINES_PYTHON", "ffmpeg", "Wav2Lip v2 code", "288 px weights", "YOLOv8n-face", "WFLW", "no photo"):
        assert part in st["reason"], part
    assert "non è impostato" in st["reason_it"] and "non è installato" in st["reason_it"] and "nessuna foto" not in st["reason_it"]
    assert "foto" in st["reason_it"]


def test_no_avatar_chosen_is_said(rig):
    base, _ = rig
    assert "no avatar is chosen" in engine().status_of(None, str(base))["reason"]


@pytest.mark.parametrize("skip", IDS)
def test_one_missing_piece_makes_it_unavailable_and_is_the_only_one_named(rig, skip):
    base, tmp = rig
    fakes.install(base, [i for i in IDS if i != skip])
    st = engine().status_of(fakes.avatar(tmp), str(base))
    assert st["available"] is False and models_store.MODELS[skip][2] in st["reason"]
    assert all(models_store.MODELS[i][2] not in st["reason"] for i in IDS if i != skip)


def test_everything_in_place_is_available(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    assert engine().status_of(fakes.avatar(tmp), str(base)) == {"available": True, "reason": "", "reason_it": ""}
    assert registry.describe(fakes.avatar(tmp, "bo"), str(base))[2]["status"]["available"] is True          # the registry asks the engine


def test_the_files_are_where_the_catalog_puts_them(rig):
    base, _ = rig
    root = base / "avatar-engines" / "wav2lipv2"
    assert pack.code_dir("wav2lipv2", str(base)) == root / "code" and pack.engine_dir("wav2lipv2", str(base)) == root
    assert Path(models_store.folder("avatar", "wav2lipv2-weights", str(base))) == root / "checkpoints"
    assert Path(models_store.folder("avatar", "wav2lipv2-yolo-face", str(base))) == root / "models" / "yolov8n-face"
    assert Path(models_store.folder("avatar", "wav2lipv2-landmarks", str(base))) == root / "models" / "wflw" / "hrnet18_256x256_p1"


def test_the_command_lines_are_exactly_these(rig):
    base, tmp = rig
    root, runner, py = base / "avatar-engines" / "wav2lipv2", str(pack.RUNNERS / "wav2lipv2_run.py"), str(tmp / "engine-python.exe")
    e = engine()
    assert e.check_command(str(base)) == [py, runner, "--pack", str(root / "code"), "--engine-dir", str(root), "--check"]
    folder = fakes.avatar(tmp)
    cmd = e.render_command(str(base), tmp / "o.i420", tmp / "a.wav", 288, 25, folder)
    assert cmd == [py, runner, "--pack", str(root / "code"), "--engine-dir", str(root), "--image", str(folder / "poster.jpg"), "--audio", str(tmp / "a.wav"),
                   "--frames-out", str(tmp / "o.i420"), "--height", "288", "--fps", "25"]


def test_the_process_is_offline_with_the_pack_as_cwd_and_the_engine_python_first_on_the_path(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    spawner = fakes.Spawner(out=['{"event": "done", "frames": 1}'], frames=1)
    e = engine(spawner)
    e.prepare(fakes.avatar(tmp), lambda phase, pct: None)
    cmd, cwd, env = spawner.calls[0]
    assert Path(cwd) == base / "avatar-engines" / "wav2lipv2" / "code"
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["PATH"].split(pack.os.pathsep)[0] == str(tmp) and env["PYTHONPATH"].split(pack.os.pathsep)[0] == str(cwd)


def test_progress_of_the_runner_and_of_a_tqdm_bar_reach_the_caller_and_the_check_marks_the_avatar(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    seen = []
    spawner = fakes.Spawner(out=['{"event": "progress", "phase": "loading", "percent": 0}', "noise", '{"event": "done", "check": true}'],
                            err=[" 40%|####      | 4/10 [00:01<00:02]"])
    e = engine(spawner)
    e.prepare(fakes.avatar(tmp), lambda phase, pct: seen.append((phase, pct)))
    assert ("loading", 0.0) in seen and ("loading", 40.0) in seen and seen[-1] == ("ready", 100.0)
    assert spawner.calls[0][0][-1] == "--check"


def test_a_runner_that_fails_says_why(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    spawner = fakes.Spawner(out=['{"event": "error", "message": "RuntimeError: CUDA out of memory"}'], code=1)
    with pytest.raises(pack.EngineFailed, match="CUDA out of memory"):
        engine(spawner).prepare(fakes.avatar(tmp), lambda phase, pct: None)
    silent = fakes.Spawner(out=["Traceback (most recent call last):", "ImportError: no module named ultralytics"], code=1)
    with pytest.raises(pack.EngineFailed, match="ultralytics"):
        engine(silent).prepare(fakes.avatar(tmp, "bo"), lambda phase, pct: None)


def test_prepare_refuses_when_something_is_missing_and_starts_nothing(rig):
    base, tmp = rig
    spawner = fakes.Spawner()
    with pytest.raises(pack.EngineNotReady, match="not installed"):
        engine(spawner).prepare(fakes.avatar(tmp), lambda phase, pct: None)
    assert spawner.calls == []


def test_render_hands_the_frames_of_the_runner_to_a_sink_at_the_rate_asked(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    folder = fakes.avatar(tmp)
    spawner = fakes.Spawner(out=['{"event": "done", "frames": 3, "width": 4, "height": 2}'], frames=3)
    e = engine(spawner)
    e.prepare(folder, lambda phase, pct: None)
    frames = e.render(b"\x00\x00" * 160, 16000, 288, 25)
    got, naps = [], []
    sent = pack.feed_frames(frames, got.append, 25, naps.append)
    assert sent == 3 and [(f.width, f.height) for f in got] == [(4, 2)] * 3 and naps == [0.04] * 3
    assert len(got[0].i420) == 12 and got[1].i420[0] == 1
    cmd = spawner.calls[-1][0]
    assert cmd[cmd.index("--height") + 1] == "288" and cmd[cmd.index("--fps") + 1] == "25" and "--image" in cmd


def test_render_needs_an_avatar_and_a_complete_pack(rig):
    base, tmp = rig
    with pytest.raises(pack.EngineNotReady, match="choose an avatar"):
        engine().render(b"", 16000, 288, 25)
    with pytest.raises(pack.EngineNotReady, match="not installed"):
        engine().render(b"", 16000, 288, 25, avatar_dir=fakes.avatar(tmp))


def test_a_cut_frames_file_is_an_error_not_a_short_clip(tmp_path):
    path = tmp_path / "f.i420"
    fakes.frames_file(path, 2)
    path.write_bytes(path.read_bytes()[:-3])
    with pytest.raises(pack.EngineFailed, match="cut short"):
        list(pack.read_frames(path))
