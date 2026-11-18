"""The embeddings/Docling Python environment: find a suitable Python already on this PC, build a venv for it, install torch (CUDA 13), transformers and
docling into it, never download an interpreter."""
from pathlib import Path

import pytest

from tadamoo.rag import embed_env as ee


class FakeRun:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def test_find_python_keeps_only_3_10_through_3_14_and_skips_what_does_not_answer(monkeypatch):
    def fake_run(argv, timeout, env=None):
        version = {"old": "3 9", "new": "3 14", "toonew": "3 15", "mid": "3 12"}.get(Path(argv[0]).name)
        if version is None:
            raise OSError("no such interpreter")
        return FakeRun(0, version)
    monkeypatch.setattr(ee, "_run", fake_run)
    assert ee.find_python(["old", "broken", "toonew", "mid"]) == "mid"
    assert ee.find_python(["old", "toonew"]) == ""


def test_install_builds_the_venv_pins_torchs_cuda13_index_then_transformers_and_docling(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "find_python", lambda: "py312")
    calls = []

    def fake_run(argv, timeout, env=None):
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        return FakeRun(0)
    rt = ee.Runtime(tmp_path / "rt", runner=fake_run)
    assert not rt.installed()
    said = []
    rt.install(say=said.append)
    assert rt.installed() and any("installed" in s for s in said)
    assert calls[0][1:3] == ["-m", "venv"]
    torch_call = next(c for c in calls if ee.TORCH in c)
    assert "--index-url" in torch_call and torch_call[torch_call.index("--index-url") + 1] == ee.TORCH_INDEX
    pkgs_call = calls[-1]
    assert all(p in pkgs_call for p in ee.PACKAGES)


def test_install_without_a_suitable_python_refuses_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "find_python", lambda: "")
    rt = ee.Runtime(tmp_path / "rt", runner=lambda *a, **k: FakeRun(0))
    with pytest.raises(ee.EmbedEnvError, match="3.10"):
        rt.install()
    assert not rt.installed()


def test_install_removes_the_half_made_venv_when_torch_fails_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
            return FakeRun(0)
        if ee.TORCH in argv:
            return FakeRun(1, err="no matching distribution")
        return FakeRun(0)
    rt = ee.Runtime(tmp_path / "rt", runner=fake_run)
    with pytest.raises(ee.EmbedEnvError, match="no matching distribution"):
        rt.install()
    assert not rt.installed()


def test_install_removes_the_venv_when_transformers_or_docling_fail_after_torch_succeeded(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
            return FakeRun(0)
        if any(p in argv for p in ee.PACKAGES):
            return FakeRun(1, err="docling needs a newer pip")
        return FakeRun(0)
    rt = ee.Runtime(tmp_path / "rt", runner=fake_run)
    with pytest.raises(ee.EmbedEnvError, match="docling needs a newer pip"):
        rt.install()
    assert not rt.installed()


def test_probe_reads_cuda_or_cpu_from_the_venvs_own_torch_and_is_empty_without_one(tmp_path):
    rt = ee.Runtime(tmp_path / "rt", runner=lambda *a, **k: FakeRun(0))
    assert rt.probe() == ""
    (rt.venv / "Scripts").mkdir(parents=True)
    (rt.venv / "Scripts" / "python.exe").write_bytes(b"")
    rt2 = ee.Runtime(tmp_path / "rt", runner=lambda argv, timeout, env=None: FakeRun(0, "cuda"))
    assert rt2.probe() == "cuda"


def test_remove_deletes_the_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        return FakeRun(0)
    rt = ee.Runtime(tmp_path / "rt", runner=fake_run)
    rt.install()
    assert rt.installed()
    rt.remove()
    assert not rt.installed()
