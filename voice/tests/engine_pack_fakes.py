"""Stand-ins for the engine-pack tests: a models folder with the pack files in place (empty), an avatar folder with a photo, a fake process that says what a
runner would say, and the frames file a runner writes. No GPU, no model, no upstream code, no network."""
import threading
from pathlib import Path

from callbridge import models_store
from callbridge.avatar_client import HEADER, MAGIC

from voice.avatar_engines import pack


def install(base: Path, model_ids) -> None:
    """Every probe file of each catalog id, as an empty file, in the folder where the catalog puts it."""
    for model_id in model_ids:
        where = Path(models_store.folder("avatar", model_id, str(base)))
        for probe in models_store.MODELS[model_id][3]:
            target = where.joinpath(*probe.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")


def avatar(root: Path, name="ada", photo=True) -> Path:
    folder = root / "avatars" / name
    folder.mkdir(parents=True)
    if photo:
        (folder / "poster.jpg").write_bytes(b"jpg")
    return folder


def set_environment(monkeypatch, tmp_path: Path, python=True, ffmpeg=True) -> Path:
    """The models folder of the test, the engine Python and ffmpeg as the engine looks for them; returns the models folder."""
    base = tmp_path / "models"
    base.mkdir(exist_ok=True)
    monkeypatch.setenv(models_store.ROOT_ENV, str(base))
    exe = tmp_path / "engine-python.exe"
    exe.write_bytes(b"")
    if python:
        monkeypatch.setenv(pack.ENV_PYTHON, str(exe))
    else:
        monkeypatch.delenv(pack.ENV_PYTHON, raising=False)
    monkeypatch.setattr(pack.shutil, "which", lambda name: "ffmpeg" if ffmpeg else None)
    monkeypatch.setattr(pack, "kill_tree", lambda proc: proc.kill())          # a fake process has no tree to kill
    return base


def frames_file(path: Path, count: int, width=4, height=2) -> None:
    with open(path, "wb") as fh:
        for i in range(count):
            fh.write(HEADER.pack(MAGIC, width, height))
            fh.write(bytes([i]) * (width * height * 3 // 2))


class FakeProc:
    """What Popen returns, with canned lines. `gate` makes wait() block until it is set (a running process)."""

    pid = -1                                   # never a real process: tests that stop it also replace pack.kill_tree

    def __init__(self, out=(), err=(), code=0, gate: threading.Event | None = None) -> None:
        self.stdout, self.stderr, self.code, self.gate = list(out), list(err), code, gate
        self.killed = False

    def wait(self, timeout=None):
        if self.gate is not None:
            self.gate.wait(10)
        return self.code

    def poll(self):
        return None if self.gate is not None and not self.gate.is_set() and not self.killed else self.code

    def kill(self):
        self.killed = True
        if self.gate is not None:
            self.gate.set()


class Spawner:
    """A `spawner` for Job: records each (cmd, cwd, env), writes `frames` frames where `--frames-out` says, and returns a FakeProc with `out`."""

    def __init__(self, out=(), err=(), code=0, frames=0, gate=None) -> None:
        self.out, self.err, self.code, self.frames, self.gate = out, err, code, frames, gate
        self.calls: list[tuple] = []

    def __call__(self, cmd, cwd, env):
        self.calls.append((list(cmd), cwd, env))
        if "--frames-out" in cmd and self.frames:
            frames_file(Path(cmd[cmd.index("--frames-out") + 1]), self.frames)
        return FakeProc(self.out, self.err, self.code, self.gate)
