"""The vendored Linly-Talker code of Wav2Lip v2, SadTalker and ER-NeRF: every file is the one the table of its VENDORED.md names (SHA-256 of the blob of the pinned commit,
unedited), nothing else is there, no weight or media is, the notes say the licence is to confirm, the scripts parse and their local imports resolve inside the folder,
the runners call what the vendored scripts define, and the run folder is filled from the vendored files. Nothing is run: no model, no GPU, no network."""
import ast
import hashlib
import re
from pathlib import Path

import pytest

from callbridge import models_store

from voice.avatar_engines import pack, registry
from voice.avatar_engines.pack import NOTES, VENDOR, EngineNotReady
from voice.tests import engine_pack_fakes as fakes

COMMIT = "dc831b3eea1202728c45042dd92de73ade8c3abd"
ORIGIN = "https://github.com/Kedreamix/Linly-Talker"
LICENSE_SHA = "cbbbe5f1b5da77831c4b62279a8609607b85d886daab2dd5d16f75b9de192eca"          # Linly-Talker's MIT LICENSE
NAMES = ("wav2lipv2", "sadtalker", "ernerf")
BINARY = (".onnx", ".pth", ".pt", ".tar", ".npy", ".bin", ".safetensors", ".mnn", ".mp4", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".wav", ".so", ".pyd", ".dll")
SIZE_LIMIT = {"wav2lipv2": 120_000, "sadtalker": 300_000, "ernerf": 520_000}
# entry scripts: (engine, script, the folders `import x` is looked up in: the run folder and what the script puts on sys.path)
ENTRIES = {"wav2lipv2": [("TFG/Wav2Lipv2.py", ("",))], "sadtalker": [("TFG/SadTalker.py", ("",))],
           "ernerf": [("TFG/NeRFTalk.py", ("", "NeRF")), ("NeRF/data_utils/process.py", ("", "NeRF"))]}


def table(name):
    text = (VENDOR / name / "VENDORED.md").read_text(encoding="utf-8")
    return text, {m.group(1): (m.group(2), int(m.group(3))) for m in re.finditer(r"^\| (\S+) \| ([0-9a-f]{64}) \| (\d+) \|$", text, re.M)}


def on_disk(name):
    return {p.relative_to(VENDOR / name).as_posix() for p in (VENDOR / name).rglob("*") if p.is_file() and "__pycache__" not in p.parts}


@pytest.mark.parametrize("name", NAMES)
def test_every_file_is_the_one_the_table_names_and_nothing_else_is_there(name):
    text, rows = table(name)
    assert rows and f"- Commit: {COMMIT}" in text and f"- Origin: {ORIGIN}" in text and "- Patches: none." in text and "## Patches" not in text
    assert on_disk(name) - set(NOTES) == set(rows)
    for rel, (sha, size) in rows.items():
        data = (VENDOR / name / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha and len(data) == size, rel
    total = sum(size for _, size in rows.values())
    assert total < SIZE_LIMIT[name] and f"- Size: {total} bytes in {len(rows)} files." in text


@pytest.mark.parametrize("name", NAMES)
def test_the_licence_is_linly_talkers_mit_file_and_the_note_says_it_is_to_confirm(name):
    data = (VENDOR / name / "LICENSE").read_bytes()
    assert hashlib.sha256(data).hexdigest() == LICENSE_SHA and data.startswith(b"MIT License")
    note = (VENDOR / name / "LICENSE-NOTE.md").read_text(encoding="utf-8")
    assert "licence to confirm" in note and "not legal advice" in note and "nothing about it is shown in the GUI" in note


def test_the_notes_name_what_is_known_of_each_licence_plainly():
    notes = {n: (VENDOR / n / "LICENSE-NOTE.md").read_text(encoding="utf-8") for n in NAMES}
    assert "non-commercial" in notes["wav2lipv2"] and "AGPL-3.0" in notes["wav2lipv2"] and "ultralytics" in notes["wav2lipv2"]
    assert "Apache 2.0" in notes["sadtalker"] and "VOCA" in notes["ernerf"] and "MIT" in notes["ernerf"]


@pytest.mark.parametrize("name", NAMES)
def test_no_weight_or_media_is_vendored_and_no_ultralytics_code_is(name):
    for rel in on_disk(name):
        assert not rel.lower().endswith(BINARY), rel
        assert "ultralytics" not in rel.split("/"), rel
    names = {p.name for p in (VENDOR / name).rglob("*") if p.is_file()}
    assert not {"wav2lipv2.pth", "yolov8n-face.pt", "model.pth", "audio_visual_encoder.pth", "79999_iter.pth"} & names


@pytest.mark.parametrize("name", NAMES)
def test_every_script_parses_and_none_needs_a_name_only_linux_has(name):
    posix_only = re.compile(r"\bfrom os import [^\n]*\b(wait3|wait4|fork|killpg|getpgid|setsid)\b|\bos\.(wait3|wait4|fork|killpg|getpgid|setsid)\b|\bimport (fcntl|termios|pwd|grp)\b")
    for rel in on_disk(name):
        if rel.endswith(".py"):
            source = (VENDOR / name / rel).read_text(encoding="utf-8", errors="replace")
            ast.parse(source, rel)
            assert not posix_only.search(source), rel


def test_only_the_wav2lip_v2_wrapper_imports_ultralytics():
    found = [(n, rel) for n in NAMES for rel in on_disk(n) if rel.endswith(".py") and "ultralytics" in (VENDOR / n / rel).read_text(encoding="utf-8", errors="replace")]
    assert found == [("wav2lipv2", "TFG/Wav2Lipv2.py")]


# ---- the local imports resolve inside the folder
def _resolve(root: Path, module: str, bases) -> list[str]:
    out = []
    for base in bases:
        stem = (base + "/" if base else "") + module.replace(".", "/")
        if (root / (stem + ".py")).is_file():
            out.append(stem + ".py")
        elif (root / stem).is_dir():
            out.append(stem + "/__init__.py" if (root / stem / "__init__.py").is_file() else "namespace:" + stem)
    return out


def _local_imports(root: Path, rel: str, bases) -> set[str]:
    tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"), rel)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.update(_resolve(root, a.name, bases))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = Path(rel).parent
                for _ in range(node.level - 1):
                    up = up.parent
                module, where = ".".join(filter(None, [*up.parts, node.module or ""])), ("",)
            else:
                module, where = node.module or "", bases
            found.update(_resolve(root, module, where))
            for a in node.names:
                found.update(_resolve(root, module + "." + a.name, where))
    return found


@pytest.mark.parametrize("name", NAMES)
def test_the_local_imports_of_the_entry_scripts_resolve_inside_the_folder(name):
    root, have = VENDOR / name, on_disk(name)
    seen, todo = set(), [rel for rel, _ in ENTRIES[name]]
    bases = dict(ENTRIES[name])
    while todo:
        rel = todo.pop()
        if rel in seen or rel.startswith("namespace:"):
            continue
        seen.add(rel)
        for found in _local_imports(root, rel, bases.get(rel, next(iter(bases.values())))):
            if not found.startswith("namespace:"):
                assert found in have, f"{rel} imports {found}, which is not vendored"
                todo.append(found)
    assert seen >= {rel for rel, _ in ENTRIES[name]} and len(seen) > 3


# ---- what the runners call exists in the vendored scripts, with these arguments
def _defs(name: str, rel: str) -> dict:
    tree = ast.parse((VENDOR / name / rel).read_text(encoding="utf-8", errors="replace"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    out[f"{node.name}.{item.name}"] = [a.arg for a in item.args.args]
        elif isinstance(node, ast.FunctionDef):
            out.setdefault(node.name, [a.arg for a in node.args.args])
    return out


def test_the_wav2lip_v2_runner_calls_what_the_vendored_script_defines():
    defs = _defs("wav2lipv2", "TFG/Wav2Lipv2.py")
    assert {"checkpoint_path", "pretrained_model_dir"} <= set(defs["Wav2Lipv2.__init__"])
    assert {"video_path", "audio_path", "batch_size", "enhance", "outfile", "fps"} <= set(defs["Wav2Lipv2.run"])
    runner = (pack.RUNNERS / "wav2lipv2_run.py").read_text(encoding="utf-8")
    assert "checkpoint_path=" in runner and "pretrained_model_dir=" in runner and "model.run(image, wav, batch_size=BATCH, enhance=False, outfile=out, fps=args.fps)" in runner


def test_the_wav2lip_v2_script_reads_its_models_where_the_catalog_puts_them():
    source = (VENDOR / "wav2lipv2" / "TFG" / "Wav2Lipv2.py").read_text(encoding="utf-8")
    assert "{pretrained_model_dir}/yolov8n-face/yolov8n-face.pt" in source and "{pretrained_model_dir}/wflw/hrnet18_256x256_p1/" in source
    assert models_store.FOLDERS["wav2lipv2-yolo-face"] == "avatar-engines/wav2lipv2/models/yolov8n-face"
    assert models_store.FOLDERS["wav2lipv2-landmarks"] == "avatar-engines/wav2lipv2/models/wflw/hrnet18_256x256_p1"
    assert "os.path.join(args.engine_dir, \"models\")" in (pack.RUNNERS / "wav2lipv2_run.py").read_text(encoding="utf-8")


def test_the_sadtalker_runner_calls_what_the_vendored_script_defines():
    defs = _defs("sadtalker", "TFG/SadTalker.py")
    assert {"checkpoint_path", "config_path"} <= set(defs["SadTalker.__init__"])
    assert {"source_image", "driven_audio", "preprocess", "still_mode", "use_enhancer", "batch_size", "size", "fps", "result_dir"} <= set(defs["SadTalker.test2"])
    init = (VENDOR / "sadtalker" / "src" / "utils" / "init_path.py").read_text(encoding="utf-8")
    assert "SadTalker_V0.0.2_" in init and "mapping_00229-model.pth.tar" in init
    assert "root_path = 'gfpgan/weights'" in (VENDOR / "sadtalker" / "src" / "face3d" / "extract_kp_videos_safe.py").read_text(encoding="utf-8", errors="replace")
    assert models_store.FOLDERS["sadtalker-gfpgan"] == "avatar-engines/sadtalker/code/gfpgan/weights"                   # the run folder, which is the working directory


def test_the_ernerf_runners_use_names_the_vendored_code_defines():
    wrapper = _defs("ernerf", "TFG/NeRFTalk.py")
    assert {"NeRFTalk.init_model", "NeRFTalk.predict"} <= set(wrapper) and wrapper["NeRFTalk.init_model"][1:] == ["ckpt_path", "pose"]
    assert "class Options" in (VENDOR / "ernerf" / "TFG" / "NeRFTalk.py").read_text(encoding="utf-8")
    train = (pack.RUNNERS / "ernerf_train.py").read_text(encoding="utf-8")
    for module, name in (("network", "NeRFNetwork"), ("provider", "NeRFDataset"), ("utils", "LPIPSMeter"), ("utils", "PSNRMeter"), ("utils", "Trainer")):
        source = (VENDOR / "ernerf" / "NeRF" / "nerf_triplane" / f"{module}.py").read_text(encoding="utf-8", errors="replace")
        assert re.search(rf"^class {name}\b", source, re.M), (module, name)
        assert name in train


def test_the_ernerf_data_preparation_and_the_cuda_modules_are_there_to_build():
    have = on_disk("ernerf")
    assert {"NeRF/data_utils/process.py", "NeRF/data_utils/face_tracking/face_tracker.py", "NeRF/data_utils/face_parsing/test.py"} <= have
    for module in ("freqencoder", "gridencoder", "raymarching", "shencoder"):
        assert {f"NeRF/{module}/setup.py", f"NeRF/{module}/backend.py"} <= have and any(r.startswith(f"NeRF/{module}/src/") and r.endswith(".cu") for r in have)


# ---- the run folder
def test_stage_code_fills_the_run_folder_from_the_vendored_files_without_the_notes(tmp_path):
    for name in NAMES:
        run = pack.stage_code(name, str(tmp_path))
        assert run == tmp_path / "avatar-engines" / name / "code"
        staged = {p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()}
        assert staged == on_disk(name) - set(NOTES)
        for rel in staged:
            assert (run / rel).read_bytes() == (VENDOR / name / rel).read_bytes()


def test_stage_code_copies_only_what_differs_and_repairs_a_file_that_was_changed(tmp_path):
    run = pack.stage_code("wav2lipv2", str(tmp_path))
    target = run / "TFG" / "Wav2Lipv2.py"
    mtimes = {p: p.stat().st_mtime_ns for p in run.rglob("*") if p.is_file()}
    pack.stage_code("wav2lipv2", str(tmp_path))
    assert {p: p.stat().st_mtime_ns for p in run.rglob("*") if p.is_file()} == mtimes
    target.write_bytes(b"x")
    pack.stage_code("wav2lipv2", str(tmp_path))
    assert target.read_bytes() == (VENDOR / "wav2lipv2" / "TFG" / "Wav2Lipv2.py").read_bytes()


def test_stage_code_keeps_the_weights_that_live_in_the_run_folder(tmp_path):
    weights = tmp_path / "avatar-engines" / "sadtalker" / "code" / "gfpgan" / "weights"
    weights.mkdir(parents=True)
    (weights / "alignment_WFLW_4HG.pth").write_bytes(b"w")
    pack.stage_code("sadtalker", str(tmp_path))
    assert (weights / "alignment_WFLW_4HG.pth").read_bytes() == b"w" and (tmp_path / "avatar-engines" / "sadtalker" / "code" / "TFG" / "SadTalker.py").is_file()


def test_stage_code_says_plainly_when_the_vendored_code_or_the_models_folder_is_missing(tmp_path):
    with pytest.raises(EngineNotReady, match="missing from this installation"):
        pack.stage_code("sadtalker", str(tmp_path), vendor=tmp_path / "empty")
    with pytest.raises(EngineNotReady, match="no models folder"):
        pack.stage_code("sadtalker", "")


# ---- the engines run from the vendored code
@pytest.fixture
def rig(tmp_path, monkeypatch):
    return fakes.set_environment(monkeypatch, tmp_path), tmp_path


@pytest.mark.parametrize("engine_id,key,weights", [("wav2lip_v288", "wav2lipv2", ("wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks")),
                                                    ("sadtalker", "sadtalker", ("sadtalker-weights", "sadtalker-gfpgan")), ("er_nerf", "ernerf", ("ernerf-audio-encoder",))])
def test_an_engine_needs_no_code_to_be_installed_only_its_weights(engine_id, key, weights):
    engine = registry.get(engine_id)
    assert engine.key == key and engine.models == weights and not any(m.endswith("-code") for m in engine.models)


@pytest.mark.parametrize("engine_id,weights", [("wav2lip_v288", ("wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks")), ("sadtalker", ("sadtalker-weights", "sadtalker-gfpgan"))])
def test_a_render_runs_in_the_run_folder_filled_from_the_vendored_code(rig, engine_id, weights):
    base, tmp = rig
    fakes.install(base, weights)
    spawner = fakes.Spawner(out=['{"event": "done", "frames": 1, "width": 4, "height": 2}'], frames=1)
    engine = type(registry.get(engine_id))(registry.info_of(engine_id), spawner)
    folder = fakes.avatar(tmp)
    assert list(engine.render(b"\x00\x00" * 160, 16000, 288, 25, avatar_dir=folder))
    cmd, cwd, env = spawner.calls[0]
    assert Path(cwd) == base / "avatar-engines" / engine.key / "code" and cmd[cmd.index("--pack") + 1] == str(cwd)
    assert (Path(cwd) / "TFG").is_dir() and not (Path(cwd) / "VENDORED.md").exists()
    assert env["PYTHONPATH"].split(pack.os.pathsep)[0] == str(cwd)


def test_status_reports_missing_vendored_code_in_both_languages(rig, monkeypatch):
    base, tmp = rig
    fakes.install(base, ("sadtalker-weights", "sadtalker-gfpgan"))
    monkeypatch.setattr(pack, "VENDOR", tmp / "empty")
    st = registry.get("sadtalker").status_of(fakes.avatar(tmp), str(base))
    assert not st["available"] and "voice/avatar_engines/vendor/sadtalker" in st["reason"] and "manca da questa installazione" in st["reason_it"]


def test_the_check_and_a_training_stage_use_the_staged_folder(rig):
    base, tmp = rig
    fakes.install(base, ("ernerf-audio-encoder", "ernerf-bfm", "ernerf-face-parsing"))
    spawner = fakes.Spawner(out=['{"event": "done", "check": true, "modules": {"pytorch3d": "0.7"}}'])
    engine = type(registry.get("er_nerf"))(registry.info_of("er_nerf"), spawner)
    engine.check_environment(base=str(base))
    assert Path(spawner.calls[0][1]) == base / "avatar-engines" / "ernerf" / "code"
    assert (base / "avatar-engines" / "ernerf" / "code" / "NeRF" / "raymarching" / "src" / "raymarching.cu").is_file()


def test_the_setup_panel_lists_the_vendored_code_as_an_item(rig):
    base, _ = rig
    got = {c["id"]: c for c in registry.get("wav2lip_v288").setup_checks(str(base))}
    assert got["code"]["ok"] is True and "vendor/wav2lipv2" in got["code"]["en"] and "model" not in got["code"] and "wav2lipv2-code" not in got
