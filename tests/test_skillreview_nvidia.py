"""NVIDIA SkillEvaluator as a second, optional review: its own Python 3.12+ venv (never our own 3.10), install, and running Tier 1/validate against a
stand-in of the real CLI (no real pip, no real NVIDIA package: nothing here reaches the network)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tadamoo import skillreview_nvidia as sr


def test_find_python_keeps_only_3_12_through_3_14(monkeypatch):
    def fake_run(argv, timeout, env=None):
        exe = argv[0]
        version = {"py312": "3 12", "py310": "3 10", "py315": "3 15", "broken": None}[Path(exe).name]
        if version is None:
            raise OSError("no such interpreter")
        return type("R", (), {"stdout": version})()
    monkeypatch.setattr(sr, "_run", fake_run)
    assert sr.find_python(["py310", "py315", "broken", "py312"]) == "py312"
    assert sr.find_python(["py310", "py315"]) == ""


def test_install_builds_the_venv_from_a_found_python(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(sr, "_run", fake_run)
    rt = sr.Runtime(tmp_path / "rt")
    assert not rt.installed()
    said = []
    rt.install(say=said.append)
    assert rt.installed() and any("installed" in s for s in said)
    assert calls[0][0] == "py312" and calls[0][1:3] == ["-m", "venv"]
    assert any(sr.PACKAGE in a for a in calls[-1])


def test_install_without_a_python_312_refuses_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "find_python", lambda: "")
    rt = sr.Runtime(tmp_path / "rt")
    with pytest.raises(sr.SkillEvaluatorError, match="3.12"):
        rt.install()
    assert not rt.installed()


def test_install_removes_the_half_made_venv_when_pip_install_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "install" in argv and sr.PACKAGE in argv:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no matching distribution"})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(sr, "_run", fake_run)
    rt = sr.Runtime(tmp_path / "rt")
    with pytest.raises(sr.SkillEvaluatorError, match="no matching distribution"):
        rt.install()
    assert not rt.installed()


def test_quality_check_reads_the_json_report_the_cli_writes(tmp_path, monkeypatch):
    rt = sr.Runtime(tmp_path / "rt")
    (rt.venv / "Scripts").mkdir(parents=True)
    (rt.venv / "Scripts" / "python.exe").write_bytes(b"")
    seen = {}

    def fake_run(argv, timeout, env=None):
        seen["argv"] = argv
        out_dir = Path(argv[argv.index("--output-dir") + 1])
        (out_dir / "quality.json").write_text(json.dumps({"score": 91, "grade": "A", "findings": []}), encoding="utf-8")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(sr, "_run", fake_run)
    got = rt.quality_check(tmp_path / "a-skill")
    assert got == {"score": 91, "grade": "A", "findings": []}
    assert "quality-check" in seen["argv"] and "--report" in seen["argv"] and str(tmp_path / "a-skill") in seen["argv"]


def test_validate_passes_the_chosen_tiers(tmp_path, monkeypatch):
    rt = sr.Runtime(tmp_path / "rt")
    (rt.venv / "Scripts").mkdir(parents=True)
    (rt.venv / "Scripts" / "python.exe").write_bytes(b"")
    seen = {}

    def fake_run(argv, timeout, env=None):
        seen["argv"] = argv
        out_dir = Path(argv[argv.index("--output-dir") + 1])
        (out_dir / "validate.json").write_text(json.dumps({"score": 70, "tiers": [1, 2, 3]}), encoding="utf-8")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(sr, "_run", fake_run)
    got = rt.validate(tmp_path / "a-skill", "1,2,3")
    assert got["tiers"] == [1, 2, 3] and seen["argv"][seen["argv"].index("--tiers") + 1] == "1,2,3"


def test_a_run_with_no_report_and_no_stderr_still_raises_clearly(tmp_path, monkeypatch):
    rt = sr.Runtime(tmp_path / "rt")
    (rt.venv / "Scripts").mkdir(parents=True)
    (rt.venv / "Scripts" / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sr, "_run", lambda argv, timeout, env=None: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    with pytest.raises(sr.SkillEvaluatorError, match="no report"):
        rt.quality_check(tmp_path / "a-skill")


def test_running_without_the_venv_refuses(tmp_path):
    rt = sr.Runtime(tmp_path / "rt")
    with pytest.raises(sr.SkillEvaluatorError, match="not installed"):
        rt.quality_check(tmp_path / "a-skill")


def test_remove_deletes_the_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "find_python", lambda: "py312")

    def fake_run(argv, timeout, env=None):
        if argv[1:3] == ["-m", "venv"]:
            (tmp_path / "rt" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (tmp_path / "rt" / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr(sr, "_run", fake_run)
    rt = sr.Runtime(tmp_path / "rt")
    rt.install()
    assert rt.installed()
    rt.remove()
    assert not rt.installed()
