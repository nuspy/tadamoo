"""FeatherTalk as an engine: what status_of says (code, FeatherHuBERT weights, Python, training), the command lines built exactly (README, `process.py`, `train.py`,
`inference.py`, `export_models.py`, `convert_mnn_models.sh` at the pinned commit), progress, the export for a phone with and without MNNConvert, the manifest entry.
The code pack is made of fake scripts; no GPU, model or network."""
import json
import os
from pathlib import Path

import pytest

from callbridge import avatar_assets, models_store
from voice.avatar_engines import feathertalk, registry
from voice.avatar_engines.personalised import MNN_ENV, PYTHON_ENV, PersonalisedEngine, Runner

from avatar_fakes import video_file
from engine_fakes import PY, fake_commands, put_pack

ENGINE = registry.ENGINES["feathertalk"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(PYTHON_ENV, PY)
    monkeypatch.delenv(MNN_ENV, raising=False)
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


def run_prepare(models, avatar, opts, **kw):
    events = []
    ENGINE.prepare(avatar, lambda phase, pct, **info: events.append((phase, pct, info)), clip=avatar / "clips" / "talk.mp4", options=opts, models_base=str(models), python=PY, **kw)
    return events


def test_the_registry_holds_the_engine_with_code_and_keeps_the_descriptor():
    assert isinstance(ENGINE, PersonalisedEngine) and ENGINE.info.native_resolution == 144 and ENGINE.info.license.id == "Apache-2.0"
    assert "Chinese" in " ".join(n["en"] for n in ENGINE.info.needs)


def test_status_says_what_is_missing_in_both_languages(models, avatar, monkeypatch):
    base = str(models)
    st = ENGINE.status_of(avatar, base)
    assert st["available"] is False and "has not been trained" in st["reason"] and "questo avatar non è stato addestrato" in st["reason_it"]
    put_pack(models, "feathertalk", weights=False)
    fake_commands(ENGINE)
    run_checks = {c.id: c.ok for c in ENGINE.preflight(base, ENGINE.options())}
    assert run_checks["audio"] is False and run_checks["landmarks"] is True and run_checks["python"] is True and run_checks["vgg19"] is True
    put_pack(models, "feathertalk")
    run_prepare(models, avatar, ENGINE.options(None, 2))
    assert ENGINE.status_of(avatar, base)["available"] is True
    (models / "avatar-engines" / "feathertalk" / "weights" / "feather_hubert.pth").unlink()
    st = ENGINE.status_of(avatar, base)
    assert st["available"] is False and "install `feathertalk-weights`" in st["reason"]
    monkeypatch.delenv(PYTHON_ENV)
    assert PYTHON_ENV in ENGINE.status_of(avatar, base)["reason"]


def test_the_options_have_one_audio_encoder_and_a_range_of_epochs():
    assert ENGINE.options() == {"asr": "feather_hubert", "fps": 25, "epochs": 200, "batch": 16}
    assert ENGINE.options("feather_hubert", 1)["epochs"] == 1
    for bad in (("wenet", None), (None, 0), (None, 500), (None, False)):
        with pytest.raises(ValueError):
            ENGINE.options(*bad)


def test_the_command_lines_are_those_of_the_upstream_readme_and_scripts():
    code, base = Path("/c"), "/m"
    w = Path("/m") / "avatar-engines" / "feathertalk" / "weights" / "feather_hubert.pth"
    ds, ck, video = Path("/a/dataset"), Path("/a/checkpoints"), Path("/a/dataset/train.mp4")
    o = ENGINE.options(None, 30)
    assert ENGINE.convert_cmd(Path("/a/c.mp4"), video, 25)[:6] == ["ffmpeg", "-y", "-i", str(Path("/a/c.mp4")), "-r", "25"]
    assert ENGINE.preprocess_cmd("py", code, base, video, o) == (["py", "data_utils/process.py", str(video), "--feather_hubert_checkpoint", str(w)], code)
    assert ENGINE.train_cmd("py", code, base, ds, ck, o, None) == (["py", "train.py", "--dataset_dir", str(ds), "--save_dir", str(ck), "--model", "feathertalk",
                                                                   "--epochs", "30", "--batchsize", "16"], code)
    assert ENGINE.train_cmd("py", code, base, ds, ck, o, ck / "last.pth")[0] == ["py", "train.py", "--dataset_dir", str(ds), "--save_dir", str(ck), "--resume",
                                                                                str(ck / "last.pth"), "--epochs", "30", "--batchsize", "16"]
    wav, work = Path("/t/s.wav"), Path("/t")
    assert ENGINE.features_cmd("py", code, base, wav, work, o) == (["py", "data_utils/feather_hubert/feather_hubert.py", "--wav", str(wav), "--checkpoint", str(w),
                                                                    "--out", str(work / "feat.npy")], code, work / "feat.npy")
    assert ENGINE.infer_cmd("py", code, ds, Path("/t/f.npy"), ck / "last.pth", Path("/t/o.mp4"), o) == (
        ["py", "inference.py", "--dataset", str(ds), "--audio_feat", str(Path("/t/f.npy")), "--checkpoint", str(ck / "last.pth"), "--save_path", str(Path("/t/o.mp4"))], code)
    out = Path("/a/export")
    assert ENGINE.export_cmds("py", code, base, ck / "last.pth", out, o) == [
        (["py", "FeatherTalk-CPP/tools/export_models.py", "--feather-checkpoint", str(w), "--visual-checkpoint", str(ck / "last.pth"), "--model", "auto",
          "--output-dir", str(out)], code)]


def test_with_mnnconvert_set_the_two_onnx_files_are_converted_with_the_flags_of_the_upstream_script(tmp_path, monkeypatch):
    converter = tmp_path / "MNNConvert"
    converter.write_text("x")
    monkeypatch.setenv(MNN_ENV, str(converter))
    out = Path("/a/export")
    cmds = ENGINE.export_cmds("py", Path("/c"), "/m", Path("/k.pth"), out, ENGINE.options())
    assert [c[0] for c in cmds[1:]] == [
        [str(converter), "-f", "ONNX", "--modelFile", str(out / "feather_hubert.onnx"), "--MNNModel", str(out / "feather_hubert.mnn"), "--fp16", "--bizCode", "FeatherTalk"],
        [str(converter), "-f", "ONNX", "--modelFile", str(out / "feathertalk_144.onnx"), "--MNNModel", str(out / "feathertalk_144_fp16.mnn"), "--fp16", "--bizCode", "FeatherTalk"]]
    assert [p.name for p in ENGINE.export_files("/m", out, {})] == ["feather_hubert.onnx", "feathertalk_144.onnx", "feather_hubert.mnn", "feathertalk_144_fp16.mnn"]
    assert ENGINE.device_input({})["model_file"] == "feathertalk_144_fp16.mnn"
    monkeypatch.setenv(MNN_ENV, str(tmp_path / "missing"))                                   # a path that is not a file counts as not set
    assert len(ENGINE.export_cmds("py", Path("/c"), "/m", Path("/k.pth"), out, ENGINE.options())) == 1


def test_a_training_reads_its_progress_from_the_epochs_and_leaves_the_files(models, avatar):
    put_pack(models, "feathertalk")
    fake_commands(ENGINE)
    events = run_prepare(models, avatar, ENGINE.options(None, 6))
    train = [e for e in events if e[0] == "train"]
    assert train[0][2] == {"epoch": 1, "epochs": 6} and train[-1][1] == 100.0 and sorted(e[1] for e in train) == [e[1] for e in train]
    folder = avatar / "engines" / "feathertalk"
    mark = json.loads((folder / "trained.json").read_text())
    assert mark["engine"] == "feathertalk" and mark["asr"] == "feather_hubert" and mark["fps"] == 25 and mark["epochs"] == 6 and mark["export_error"] == ""
    assert (folder / "dataset" / "aud_hu.npy").is_file() and (folder / "checkpoints" / "last.pth").is_file()
    assert [e[0] for e in events][-1] == "export"


def test_the_export_gives_onnx_the_fixed_interface_and_the_first_frames(models, avatar):
    put_pack(models, "feathertalk")
    fake_commands(ENGINE)
    run_prepare(models, avatar, ENGINE.options(None, 1))
    data = json.loads((avatar / "engines" / "feathertalk" / "export" / "export.json").read_text())
    assert data["input"] == {"audio_encoder": "feather_hubert", "fps": 25, "size": 144, "sample_rate": 16000, "audio_step_ms": 20, "audio_tokens": 40, "feature_dim": 1024,
                             "model_file": "feathertalk_144.onnx", "encoder_file": "feather_hubert.onnx",
                             "shapes": {"input": [1, 6, 144, 144], "audio": [1, 40, 1024], "waveform": [1, "samples"]}}
    assert {"engines/feathertalk/export/feather_hubert.onnx", "engines/feathertalk/export/feathertalk_144.onnx", "engines/feathertalk/export/frames/0.jpg",
            "engines/feathertalk/export/lms/0.lms"} <= set(data["files"])
    man = avatar_assets.manifest(avatar, "ada")
    assert man["engines"]["feathertalk"]["input"]["size"] == 144 and man["render_modes"][-1] == "feathertalk"


def test_render_gives_frames(models, avatar):
    put_pack(models, "feathertalk")
    fake_commands(ENGINE)
    run_prepare(models, avatar, ENGINE.options(None, 1))
    frames = list(ENGINE.render(b"\0\0" * 8000, 16000, 144, 25, avatar_dir=avatar, models_base=str(models), python=PY))
    assert len(frames) == 10 and all(f.height == 144 and f.width == 192 for f in frames)


def test_the_runner_gives_lines_ended_by_a_return_or_a_newline_and_reports_the_exit_code():
    lines = []
    code = Runner(lines.append).run([PY, "-c", "import sys; sys.stdout.write('a\\rb\\r\\nc\\n'); sys.stdout.write('tail'); sys.exit(4)"], ".", dict(os.environ))
    assert code == 4 and lines == ["a", "b", "c", "tail"]
