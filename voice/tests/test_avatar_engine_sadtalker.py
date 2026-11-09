"""SadTalker as an engine: status_of, the exact command lines (256 px, 20 fps), the frames it hands over. Fakes only."""
from pathlib import Path

import pytest

from callbridge import models_store

from voice.avatar_engines import pack, registry
from voice.avatar_engines.sadtalker import SadTalkerEngine
from voice.tests import engine_pack_fakes as fakes

IDS = ("sadtalker-weights", "sadtalker-gfpgan")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return fakes.set_environment(monkeypatch, tmp_path), tmp_path


def engine(spawner=None):
    return SadTalkerEngine(registry.info_of("sadtalker"), *([spawner] if spawner else []))


def test_the_registry_builds_this_engine_for_sadtalker_and_it_is_pc_only_at_256():
    assert isinstance(registry.get("sadtalker"), SadTalkerEngine)
    info = registry.info_of("sadtalker")
    assert info.runs_on == ("pc",) and info.native_resolution == 256 and not info.license.verify and info.default_resolution == 256


def test_status_matrix(rig, tmp_path):
    base, tmp = rig
    folder = fakes.avatar(tmp)
    st = engine().status_of(folder, str(base))
    assert not st["available"] and all(models_store.MODELS[i][2] in st["reason"] for i in IDS)
    for missing in IDS:
        fakes.install(base, [i for i in IDS if i != missing])
        st = engine().status_of(folder, str(base))
        assert not st["available"] and models_store.MODELS[missing][2] in st["reason"]
        fakes.install(base, [missing])                                       # in place from now on: the next round names the next one
        break
    fakes.install(base, IDS)
    assert engine().status_of(folder, str(base))["available"] is True
    assert not engine().status_of(fakes.avatar(tmp, "bo", photo=False), str(base))["available"]


def test_the_weights_are_where_the_code_looks_for_them(rig):
    base, _ = rig
    root = base / "avatar-engines" / "sadtalker"
    assert Path(models_store.folder("avatar", "sadtalker-weights", str(base))) == root / "checkpoints"
    assert Path(models_store.folder("avatar", "sadtalker-gfpgan", str(base))) == root / "code" / "gfpgan" / "weights"   # the code reads ./gfpgan/weights


def test_the_command_lines_are_exactly_these(rig):
    base, tmp = rig
    root, runner, py = base / "avatar-engines" / "sadtalker", str(pack.RUNNERS / "sadtalker_run.py"), str(tmp / "engine-python.exe")
    folder = fakes.avatar(tmp)
    assert engine().check_command(str(base)) == [py, runner, "--pack", str(root / "code"), "--engine-dir", str(root), "--check"]
    assert engine().render_command(str(base), tmp / "o", tmp / "a.wav", 256, 20, folder) == [
        py, runner, "--pack", str(root / "code"), "--engine-dir", str(root), "--image", str(folder / "poster.jpg"), "--audio", str(tmp / "a.wav"),
        "--frames-out", str(tmp / "o"), "--height", "256", "--fps", "20"]


def test_render_uses_its_own_frame_rate_when_none_is_given_and_hands_over_the_frames(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    spawner = fakes.Spawner(out=['{"event": "done", "frames": 2}'], frames=2)
    got = list(engine(spawner).render(b"\x00\x00" * 80, 16000, 256, 0, avatar_dir=fakes.avatar(tmp)))
    assert len(got) == 2
    cmd = spawner.calls[0][0]
    assert cmd[cmd.index("--fps") + 1] == "20"


def test_stop_ends_the_running_process_and_a_failure_is_reported(rig):
    base, tmp = rig
    fakes.install(base, IDS)
    import threading
    gate = threading.Event()
    spawner = fakes.Spawner(gate=gate)
    e = engine(spawner)
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(_attempt(e, tmp)))
    t.start()
    while not spawner.calls:
        pass
    e.stop()
    t.join(5)
    assert outcome == ["was stopped"]


def _attempt(e, tmp):
    try:
        e.prepare(fakes.avatar(tmp, "cy"), lambda phase, pct: None)
    except pack.EngineFailed as err:
        return "was stopped" if "stopped" in str(err) else str(err)
    return "ran"
