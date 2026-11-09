"""Only the selected engine is loaded: selecting B never starts A, switching stops A and unloads it, and listing or asking for the status of the engines starts
nothing. Fake processes only."""
import threading

import pytest

from voice.avatar_engines import pack, registry
from voice.avatar_engines.active import ActiveEngine
from voice.avatar_engines.sadtalker import SadTalkerEngine
from voice.avatar_engines.wav2lipv2 import Wav2LipV2Engine
from voice.tests import engine_pack_fakes as fakes

IDS_A = ("wav2lipv2-code", "wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks")
IDS_B = ("sadtalker-code", "sadtalker-weights", "sadtalker-gfpgan")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    base = fakes.set_environment(monkeypatch, tmp_path)
    fakes.install(base, IDS_A + IDS_B)
    folder = fakes.avatar(tmp_path)
    a_spawn, b_spawn = fakes.Spawner(out=['{"event": "done", "frames": 1}'], frames=1), fakes.Spawner(out=['{"event": "done", "frames": 1}'], frames=1)
    a, b = Wav2LipV2Engine(registry.info_of("wav2lip_v288"), a_spawn), SadTalkerEngine(registry.info_of("sadtalker"), b_spawn)
    return ActiveEngine({"wav2lip_v288": a, "sadtalker": b}), a, b, a_spawn, b_spawn, folder


def test_nothing_starts_until_an_engine_is_selected_and_asked_to_draw(rig):
    active, a, b, a_spawn, b_spawn, folder = rig
    registry.describe(folder, None)                                         # listing the engines and their status starts nothing
    a.status_of(folder), b.status_of(folder)
    assert a_spawn.calls == [] and b_spawn.calls == [] and active.current_id is None
    with pytest.raises(RuntimeError, match="no avatar engine"):
        active.render(b"", 16000, 288, 25)


def test_selecting_b_never_starts_a(rig):
    active, a, b, a_spawn, b_spawn, folder = rig
    active.use("sadtalker")
    active.prepare(folder, lambda phase, pct: None)
    list(active.render(b"\x00\x00" * 80, 16000, 256, 20))
    assert a_spawn.calls == [] and len(b_spawn.calls) == 2                  # the check and one render, all of B


def test_switching_stops_the_running_engine_and_unloads_it(rig, monkeypatch):
    active, a, b, a_spawn, b_spawn, folder = rig
    gate = threading.Event()
    a._spawn = fakes.Spawner(gate=gate)
    active.use("wav2lip_v288")
    outcome = []
    t = threading.Thread(target=lambda: outcome.append(_try(lambda: active.prepare(folder, lambda p, c: None))))
    t.start()
    while not a._spawn.calls:
        pass
    active.use("sadtalker")                                                  # the person switches while A is working
    t.join(5)
    assert outcome == ["stopped"] and a._avatar is None and active.current_id == "sadtalker"
    assert b_spawn.calls == []                                               # selecting B did not start B either: only a call does
    with pytest.raises(pack.EngineNotReady, match="choose an avatar"):
        a.render(b"", 16000, 288, 25)                                        # A forgot its avatar


def test_release_unloads_the_engine_in_use_and_selecting_the_same_one_again_changes_nothing(rig):
    active, a, b, a_spawn, b_spawn, folder = rig
    active.use("wav2lip_v288")
    active.prepare(folder, lambda p, c: None)
    assert a._avatar is not None
    assert active.use("wav2lip_v288") is a and a._avatar is not None
    active.release()
    assert a._avatar is None and active.current_id is None


def test_an_unknown_engine_is_refused_and_leaves_the_one_in_use_alone(rig):
    active, a, b, *_ = rig
    active.use("sadtalker")
    with pytest.raises(KeyError):
        active.use("nope")
    assert active.current_id == "sadtalker"


def _try(call):
    try:
        call()
    except pack.EngineFailed as e:
        return "stopped" if "stopped" in str(e) else str(e)
    return "ran"
