"""Only the engine in use is loaded: nothing starts when an engine is described, asked its status or selected; a render starts its scripts when it is called and leaves
nothing; selecting engine B never starts engine A; switching to B stops A's running work and its children. Fake scripts stand for the upstream code."""
import subprocess
import threading
import time

import psutil
import pytest

from callbridge import models_store
from voice.avatar_engines import personalised, registry, ultralight
from voice.avatar_engines.active import ActiveEngine
from voice.avatar_engines.personalised import PYTHON_ENV, Cancelled

from avatar_fakes import video_file
from engine_fakes import PY, fake_commands, put_pack

A, B = registry.ENGINES["ultralight"], registry.ENGINES["feathertalk"]
ATTRS = ("convert_cmd", "audio_cmd", "which", "vendor")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(PYTHON_ENV, PY)
    monkeypatch.delenv("FAKE_HANG_RENDER", raising=False)
    monkeypatch.setattr(ultralight, "HAS_WAIT3", True)
    monkeypatch.setattr(models_store, "_service_env", {})
    monkeypatch.delenv(models_store.ROOT_ENV, raising=False)
    saved = {(e.ID, n): e.__dict__.get(n) for e in (A, B) for n in ATTRS}
    yield
    for (eid, n), v in saved.items():
        engine = registry.ENGINES[eid]
        engine.__dict__.pop(n, None)
        if v is not None:
            engine.__dict__[n] = v
    for engine in (A, B):
        engine.unload()


@pytest.fixture
def trained(tmp_path):
    """One avatar trained (1 epoch, fakes) for both engines; returns (models, avatar)."""
    models = tmp_path / "models"
    folder = tmp_path / "avatars" / "ada"
    (folder / "clips").mkdir(parents=True)
    clip = video_file(folder / "clips" / "talk.mp4")
    for engine in (A, B):
        put_pack(models, engine.ID)
        fake_commands(engine)
        engine.prepare(folder, lambda *a, **k: None, clip=clip, options=engine.options(None, 1), models_base=str(models), python=PY)
    return models, folder


def render(engine, models, folder):
    return list(engine.render(b"\0\0" * 8000, 16000, 96, 25, avatar_dir=folder, models_base=str(models), python=PY))


def test_nothing_is_started_by_describing_asking_the_status_or_selecting(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a process was started"))
    registry.describe(None, str(tmp_path))
    active = ActiveEngine()
    for engine in (A, B):
        engine.status_of(tmp_path, str(tmp_path))
        assert active.use(engine.ID) is engine and not engine.loaded()
    active.release()
    with pytest.raises(KeyError):
        active.use("nothing")


def test_selecting_b_never_starts_a_and_a_render_leaves_nothing_loaded(trained, monkeypatch):
    models, folder = trained
    started = []
    real = subprocess.Popen

    def spy(argv, *a, **k):
        started.append(str(k.get("cwd")))
        return real(argv, *a, **k)
    monkeypatch.setattr(personalised.subprocess, "Popen", spy)
    active = ActiveEngine()
    engine = active.use("feathertalk")
    frames = render(engine, models, folder)
    assert len(frames) == 10 and started
    assert not any(str(models / "avatar-engines" / "ultralight") in cwd for cwd in started)       # no process of A, not even a probe
    assert not A.loaded() and not B.loaded()                                                       # and B left none behind
    started.clear()
    render(active.use("ultralight"), models, folder)                                              # switching: B is unloaded, nothing of it is running
    assert not B.loaded()
    assert started and not any(str(models / "avatar-engines" / "feathertalk") in cwd for cwd in started)


def test_switching_the_selection_stops_the_running_work_of_the_other_engine_and_its_children(trained, monkeypatch):
    models, folder = trained
    monkeypatch.setenv("FAKE_HANG_RENDER", "1")
    pidfile = models.parent / "render-child.pid"
    monkeypatch.setenv("FAKE_PID_FILE", str(pidfile))
    result = {}
    active = ActiveEngine()
    first = active.use("ultralight")

    def draw():
        try:
            render(first, models, folder)
        except Cancelled as e:
            result["cancelled"] = e
    t = threading.Thread(target=draw)
    t.start()
    for _ in range(100):
        if A.loaded() and pidfile.is_file() and pidfile.read_text():
            break
        time.sleep(0.1)
    assert A.loaded()
    child = int(pidfile.read_text())
    assert psutil.pid_exists(child)
    monkeypatch.delenv("FAKE_HANG_RENDER")
    engine = active.use("feathertalk")                                                              # the other engine becomes the one in use
    t.join(20)
    assert not t.is_alive() and "cancelled" in result and not A.loaded()
    for _ in range(50):
        if not psutil.pid_exists(child):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(child)
    assert len(render(engine, models, folder)) == 10 and not B.loaded()                             # B works, and starts only now


def test_stop_is_idempotent_and_an_export_is_also_stopped(trained):
    models, folder = trained
    A.stop()
    A.stop()
    assert not A.loaded()
    runner = personalised.Runner()
    active = ActiveEngine()
    active.use("ultralight")
    with A._tracked(runner):
        assert A.loaded()
        active.use("feathertalk")
        assert runner.cancelled.is_set()
    assert not A.loaded()
