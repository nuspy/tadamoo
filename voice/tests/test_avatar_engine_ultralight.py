"""Ultralight-Digital-Human as an engine: what status_of says, the command lines built exactly (those of the upstream README and scripts), the progress read from
the training output, the files it leaves, the export for a phone and the manifest entry. The code pack is made of fake scripts; no GPU, model or network."""
import json
import sys
from pathlib import Path

import pytest

from callbridge import avatar_assets, models_store
from voice.avatar_engines import registry, ultralight
from voice.avatar_engines.personalised import EXPORT_FRAMES, PYTHON_ENV, Cancelled, EngineError, PersonalisedEngine, Runner

from avatar_fakes import video_file
from engine_fakes import PY, fake_commands, put_pack

ENGINE = registry.ENGINES["ultralight"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ultralight, "HAS_WAIT3", True)
    monkeypatch.setenv(PYTHON_ENV, PY)
    monkeypatch.setattr(models_store, "_service_env", {})
    monkeypatch.delenv(models_store.ROOT_ENV, raising=False)


@pytest.fixture(autouse=True)
def _restore():                                                       # fake_commands replaces methods on the one engine of the registry
    saved = {n: ENGINE.__dict__.get(n) for n in ("convert_cmd", "audio_cmd", "which", "vendor")}
    yield
    for n, v in saved.items():
        ENGINE.__dict__.pop(n, None)
        if v is not None:
            ENGINE.__dict__[n] = v


@pytest.fixture
def models(tmp_path):
    return tmp_path / "models"


@pytest.fixture
def avatar(tmp_path):
    folder = tmp_path / "avatars" / "ada"
    (folder / "clips").mkdir(parents=True)
    video_file(folder / "clips" / "talk.mp4")
    return folder


def trained(avatar, asr="wenet"):
    folder = avatar / "engines" / "ultralight"
    (folder / "checkpoints").mkdir(parents=True)
    (folder / "checkpoints" / "last.pth").write_text("epoch=1")
    (folder / "trained.json").write_text(json.dumps({"engine": "ultralight", "asr": asr, "fps": ultralight.FPS[asr], "epochs": 2}))


def reason(status):
    return status["reason"]


# ---------------------------------------------------------------------------------------- the descriptor and the registry
def test_the_registry_holds_the_engine_with_code_and_the_other_descriptor_facts_are_kept():
    assert isinstance(ENGINE, PersonalisedEngine) and ENGINE.info.id == "ultralight" and ENGINE.info.kind == "personalized"
    assert ENGINE.info.training.video_minutes == (3, 5) and ENGINE.info.license.id == "Apache-2.0"
    assert [e["status"]["available"] for e in registry.describe(None) if e["id"] == "ultralight"] == [False]


# ---------------------------------------------------------------------------------------- status_of
def test_status_says_exactly_what_is_missing(models, avatar, monkeypatch):
    base = str(models)
    st = ENGINE.status_of(None, base)
    assert st["available"] is False and "no avatar is chosen" in reason(st)
    st = ENGINE.status_of(avatar, base)
    assert st["trained"] is False and "has not been trained" in reason(st) and "-code" not in reason(st)
    put_pack(models, "ultralight")
    st = ENGINE.status_of(avatar, base)
    assert st["python"] is True and reason(st) == "this avatar has not been trained for this engine"
    trained(avatar)
    st = ENGINE.status_of(avatar, base)
    assert st == {**st, "available": True, "reason": "", "trained": True, "checkpoint": "checkpoints/last.pth"}
    monkeypatch.delenv(PYTHON_ENV)
    assert reason(ENGINE.status_of(avatar, base)) == f"the Python of the engines is not set: name it in the Set up panel or in {PYTHON_ENV}"
    monkeypatch.setenv(PYTHON_ENV, str(models / "no-python.exe"))
    assert "is not there" in reason(ENGINE.status_of(avatar, base))


def test_a_trained_avatar_with_the_wenet_encoder_missing_says_so_and_hubert_asks_for_its_cache(models, avatar):
    put_pack(models, "ultralight", wenet=False)
    trained(avatar, "wenet")
    st = ENGINE.status_of(avatar, str(models))
    assert st["available"] is False and "encoder.onnx" in reason(st) and "Google Drive" in reason(st)
    trained_dir = avatar / "engines" / "ultralight"
    mark = json.loads((trained_dir / "trained.json").read_text())
    (trained_dir / "trained.json").write_text(json.dumps({**mark, "asr": "hubert", "fps": 25}))
    assert "facebook/hubert-large-ls960-ft" in reason(ENGINE.status_of(avatar, str(models)))
    put_pack(models, "ultralight", wenet=False, hubert=True)
    assert ENGINE.status_of(avatar, str(models))["available"] is True


def test_a_checkpoint_without_the_trained_mark_does_not_count(models, avatar):
    put_pack(models, "ultralight")
    trained(avatar)
    (avatar / "engines" / "ultralight" / "trained.json").unlink()
    assert ENGINE.status_of(avatar, str(models))["trained"] is False


def test_the_preflight_names_ffmpeg_the_vgg_file_and_the_platform(models, monkeypatch):
    put_pack(models, "ultralight", vgg=False)
    opts = ENGINE.options()
    checks = {c.id: c for c in ENGINE.preflight(str(models), opts, which=lambda n: None)}
    assert not checks["ffmpeg"].ok and not checks["vgg19"].ok and "vgg19-dcbb9e9d.pth" in checks["vgg19"].en and checks["platform"].ok
    monkeypatch.setattr(ultralight, "HAS_WAIT3", False)
    bad = {c.id: c for c in ENGINE.preflight(str(models), opts, which=lambda n: "x")}["platform"]
    assert not bad.ok and "os.wait3" in bad.en


# ---------------------------------------------------------------------------------------- the command lines
def test_the_options_are_checked_and_follow_the_encoder():
    assert ENGINE.options() == {"asr": "wenet", "fps": 20, "epochs": 200, "batch": 16}
    assert ENGINE.options("hubert", 50) == {"asr": "hubert", "fps": 25, "epochs": 50, "batch": 16}
    for bad in (("mp3", None), ("wenet", 0), ("wenet", 401), ("wenet", True), ("wenet", "5")):
        with pytest.raises(ValueError):
            ENGINE.options(*bad)


def test_the_command_lines_are_those_of_the_upstream_scripts(tmp_path):
    code, base = Path("/c"), "/m"
    ds, ck, video = Path("/a/dataset"), Path("/a/checkpoints"), Path("/a/dataset/train.mp4")
    w, o = ENGINE.options("wenet", 30), ENGINE.options("hubert", 30)
    assert ENGINE.convert_cmd(Path("/a/clip.mp4"), video, 20) == ["ffmpeg", "-y", "-i", str(Path("/a/clip.mp4")), "-r", "20", "-c:v", "libx264", "-crf", "14",
                                                                  "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)]
    assert ENGINE.preprocess_cmd("py", code, base, video, w) == (["py", "data_utils/process.py", str(video), "--asr", "wenet"], code)
    assert ENGINE.preprocess_cmd("py", code, base, video, o)[0][-2:] == ["--asr", "hubert"]
    assert ENGINE.train_cmd("py", code, base, ds, ck, w, None) == (["py", "train.py", "--dataset_dir", str(ds), "--save_dir", str(ck), "--asr", "wenet",
                                                                   "--epochs", "30", "--batchsize", "16"], code)
    assert ENGINE.train_cmd("py", code, base, ds, ck, w, ck / "last.pth")[0][-2:] == ["--resume", str(ck / "last.pth")]
    wav, work = Path("/t/speech16k.wav"), Path("/t")
    assert ENGINE.features_cmd("py", code, base, wav, work, w) == (["py", "wenet_infer.py", str(wav)], code / "data_utils", Path("/t/speech16k_wenet.npy"))
    assert ENGINE.features_cmd("py", code, base, wav, work, o) == (["py", "data_utils/hubert.py", "--wav", str(wav), "--out", str(work / "feat.npy")], code, work / "feat.npy")
    assert ENGINE.infer_cmd("py", code, ds, Path("/t/f.npy"), ck / "last.pth", Path("/t/o.mp4"), w) == (
        ["py", "inference.py", "--asr", "wenet", "--dataset", str(ds), "--audio_feat", str(Path("/t/f.npy")), "--save_path", str(Path("/t/o.mp4")),
         "--checkpoint", str(ck / "last.pth")], code)
    assert ENGINE.export_cmds("py", code, base, ck / "last.pth", Path("/a/export"), w) == [
        (["py", "pth2onnx.py", "--checkpoint", str(ck / "last.pth"), "--onnx_path", str(Path("/a/export/unet.onnx")), "--asr", "wenet"], code)]


# ---------------------------------------------------------------------------------------- training
def run_prepare(models, avatar, opts, **kw):
    events = []
    clip = avatar / "clips" / "talk.mp4"
    result = ENGINE.prepare(avatar, lambda phase, pct, **info: events.append((phase, round(pct, 1), info)), clip=clip, options=opts, models_base=str(models), python=PY, **kw)
    return result, events


def test_a_training_converts_prepares_trains_exports_and_writes_the_mark(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    result, events = run_prepare(models, avatar, ENGINE.options("wenet", 7))
    phases = [e[0] for e in events]
    assert phases[0] == "convert" and phases.index("preprocess") < phases.index("train") < phases.index("export") and phases[-1] == "export" and events[-1][1] == 100
    train = [e for e in events if e[0] == "train"]
    assert train[0][2] == {"epoch": 1, "epochs": 7} and train[-1][2] == {"epoch": 7, "epochs": 7} and train[-1][1] == 100.0
    assert [e[1] for e in train] == sorted(e[1] for e in train) and 7.1 in [e[1] for e in train]                      # epoch 1 at half: (0 + .5) / 7
    folder = avatar / "engines" / "ultralight"
    assert (folder / "checkpoints" / "last.pth").is_file() and (folder / "checkpoints" / "4.pth").is_file() and (folder / "checkpoints" / "6.pth").is_file()
    assert not (folder / "dataset" / "train.mp4").exists() and (folder / "dataset" / "full_body_img" / "0.jpg").is_file()
    mark = json.loads((folder / "trained.json").read_text())
    assert mark == {**mark, "engine": "ultralight", "asr": "wenet", "fps": 20, "epochs": 7, "frames": 12, "clip": "talk.mp4", "export_error": ""} and result == mark
    assert ENGINE.status_of(avatar, str(models))["available"] is True
    run = models / "avatar-engines" / "ultralight" / "run"                                    # the scripts ran from a copy with the weights in the places they read
    assert (run / "train.py").is_file() and (run / "data_utils" / "encoder.onnx").read_bytes() == b"enc" and (run / "data_utils" / "scrfd_2.5g_kps.onnx").read_bytes() == b"lm"
    assert (run / "data_utils" / "checkpoint_epoch_335.pth.tar").is_file()


def test_the_export_for_a_phone_lists_files_with_their_hashes_and_the_input_of_the_model(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    run_prepare(models, avatar, ENGINE.options("wenet", 2))
    export = avatar / "engines" / "ultralight" / "export"
    data = json.loads((export / "export.json").read_text())
    assert data["engine"] == "ultralight" and data["input"] == {"audio_encoder": "wenet", "fps": 20, "size": 160, "sample_rate": 16000, "model_file": "unet.onnx",
                                                                "shapes": {"input": [1, 6, 160, 160], "audio": [1, 128, 16, 32]}, "encoder_file": "encoder.onnx"}
    rels = set(data["files"])
    assert {"engines/ultralight/export/unet.onnx", "engines/ultralight/export/encoder.onnx", "engines/ultralight/export/frames/0.jpg", "engines/ultralight/export/lms/11.lms"} <= rels
    assert len([r for r in rels if "/frames/" in r]) == min(12, EXPORT_FRAMES)
    man = avatar_assets.manifest(avatar, "ada")
    assert man["engines"]["ultralight"]["input"] == data["input"] and "ultralight" in man["render_modes"]
    assert set(man["engines"]["ultralight"]["files"]) == rels and all(len(h) == 64 for h in man["engines"]["ultralight"]["files"].values())
    assert not any(k.startswith("engines/") for k in man["files"])                         # the zip of every device does not carry them
    files = ENGINE.export_for_device(avatar, "android", models_base=str(models), python=PY)
    assert export / "export.json" in files and all(f.is_file() for f in files)
    assert ENGINE.export_for_device(avatar, "pc", models_base=str(models)) == []
    with pytest.raises(ValueError):
        ENGINE.export_for_device(avatar, "esp32", models_base=str(models))


def test_a_missing_export_tool_does_not_lose_the_training(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    (models / "avatar-engines" / "ultralight" / "weights" / "encoder.onnx").unlink()       # status and export need it; the training does not
    opts = {**ENGINE.options("hubert", 2)}
    put_pack(models, "ultralight", wenet=False, hubert=True)
    result, _ = run_prepare(models, avatar, opts)
    assert result["export_error"] == "" and (avatar / "engines" / "ultralight" / "trained.json").is_file()
    assert "encoder_file" not in json.loads((avatar / "engines" / "ultralight" / "export" / "export.json").read_text())["input"]


def test_a_second_training_continues_from_the_saved_epoch_and_a_changed_clip_starts_again(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    run_prepare(models, avatar, ENGINE.options("wenet", 5))
    assert ENGINE.resume_epoch(avatar) == 5
    lines = []
    runner = Runner(lines.append)
    run_prepare(models, avatar, ENGINE.options("wenet", 8), runner=runner)
    assert [m for m in lines if m.startswith("Epoch")][0].startswith("Epoch 6/8")
    assert not (avatar / "engines" / "ultralight" / "dataset" / "train.mp4").exists()
    video_file(avatar / "clips" / "talk.mp4", frames=30)                                    # another clip: the dataset and checkpoints are made again
    lines.clear()
    run_prepare(models, avatar, ENGINE.options("wenet", 3), runner=Runner(lines.append))
    assert [m for m in lines if m.startswith("Epoch")][0].startswith("Epoch 1/3")


def test_a_failing_step_names_itself_and_a_dataset_without_landmarks_is_refused(models, avatar, monkeypatch):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    (models.parent / "fake-vendor" / "ultralight" / "train.py").write_text("import sys; sys.exit(3)")
    with pytest.raises(EngineError, match=r"train failed \(exit code 3\)"):
        run_prepare(models, avatar, ENGINE.options("wenet", 2))
    assert not (avatar / "engines" / "ultralight" / "trained.json").exists()
    put_pack(models, "ultralight")
    monkeypatch.setenv("FAKE_FRAMES", "0")
    with pytest.raises(EngineError, match="made 0 frames"):
        run_prepare(models, avatar, ENGINE.options("wenet", 2), fresh=True)


def test_a_preflight_that_fails_stops_before_anything_runs(models, avatar):
    put_pack(models, "ultralight", landmarks=False)
    fake_commands(ENGINE)
    with pytest.raises(EngineError, match="install `engine-landmarks`"):
        run_prepare(models, avatar, ENGINE.options("wenet", 2))
    assert not (avatar / "engines").exists()


def test_a_cancelled_runner_raises_cancelled_before_any_command(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    runner = Runner()
    runner.cancel()
    with pytest.raises(Cancelled):
        run_prepare(models, avatar, ENGINE.options("wenet", 2), runner=runner)


# ---------------------------------------------------------------------------------------- rendering
def test_render_runs_the_scripts_and_gives_frames_at_the_asked_height_and_rate(models, avatar):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    run_prepare(models, avatar, ENGINE.options("wenet", 2))
    frames = list(ENGINE.render(b"\0\0" * 16000, 16000, 96, 25, avatar_dir=avatar, models_base=str(models), python=PY))
    assert len(frames) == 12 and {(f.width, f.height) for f in frames} == {(128, 96)}               # 10 frames at 20 fps are 12 at 25
    assert frames[0].bgr().shape == (96, 128, 3)
    with pytest.raises(ValueError):
        list(ENGINE.render(b"", 16000, 96, 25))
    (avatar.parent / "fresh").mkdir()
    with pytest.raises(EngineError, match="has not been trained"):
        list(ENGINE.render(b"", 16000, 96, 25, avatar_dir=avatar.parent / "fresh", models_base=str(models)))


def test_the_staging_copies_what_changed_and_names_a_weight_that_is_missing(models):
    put_pack(models, "ultralight")
    fake_commands(ENGINE)
    run = ENGINE.stage(str(models), ENGINE.options("wenet"))
    before = (run / "train.py").stat().st_mtime_ns
    ENGINE.stage(str(models), ENGINE.options("wenet"))
    assert (run / "train.py").stat().st_mtime_ns == before
    (models.parent / "fake-vendor" / "ultralight" / "train.py").write_text("print('new version')")
    ENGINE.stage(str(models), ENGINE.options("wenet"))
    assert (run / "train.py").read_text() == "print('new version')"
    (models / "avatar-engines" / "landmarks" / "scrfd_2.5g_kps.onnx").unlink()
    with pytest.raises(EngineError, match="scrfd_2.5g_kps.onnx is missing"):
        ENGINE.stage(str(models), ENGINE.options("wenet"))
