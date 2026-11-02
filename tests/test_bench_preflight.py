"""Preflight checks (tadamoo/bench/preflight.py) with every probe replaced. CPU only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import preflight
from tadamoo.bench.importer import Result
from tadamoo.bench.registry import Needs
from bench_support import make_spec, ready_probes

DOCKER = make_spec("x", needs=Needs(docker=True, wsl2=True, internet=True), tools=("uv",), python_min=(3, 11), disk_gb=5)


def by_id(checks):
    return {c["id"]: c for c in checks}


def runner_with(**answers):
    def run(args, cwd=None, timeout=None, env=None):
        key = " ".join(args[:2])
        return answers.get(key, Result(0, "ok", ""))
    return run


WSL_OK = Result(0, "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n", "")


def test_a_ready_machine_is_ready(tmp_path):
    checks = preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"wsl -l": WSL_OK})))
    assert preflight.ready(checks) and all(c["ok"] for c in checks)
    assert {"git", "uv", "python", "docker_client", "docker_daemon", "wsl2", "disk", "internet", "gpu_free"} <= set(by_id(checks))
    assert all(c["fix"] == "" for c in checks)


def test_a_stopped_docker_says_how_to_fix_it(tmp_path):
    checks = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"docker info": Result(1, "", "error"), "wsl -l": WSL_OK}))))
    assert not checks["docker_daemon"]["ok"] and "Start Docker Desktop and wait until it says Engine running" in checks["docker_daemon"]["fix"]
    assert not preflight.ready(list(checks.values()))


def test_no_docker_client(tmp_path):
    checks = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(which=lambda n: None if n == "docker" else "/bin/" + n, runner=runner_with(**{"wsl -l": WSL_OK}))))
    assert not checks["docker_client"]["ok"] and "docker_daemon" not in checks


def test_wsl_output_in_utf16_and_without_version_two(tmp_path):
    utf16 = Result(0, "  N\x00A\x00M\x00E\x00 \x00 \x00V\x00E\x00R\x00S\x00I\x00O\x00N\x00\n\x00*\x00 \x00U\x00b\x00u\x00n\x00t\x00u\x00 \x00R\x00u\x00n\x00n\x00i\x00n\x00g\x00 \x002\x00\n\x00", "")
    assert by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"wsl -l": utf16}))))["wsl2"]["ok"]
    v1 = Result(0, "  NAME STATE VERSION\n* Ubuntu Running 1\n", "")
    c = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"wsl -l": v1}))))["wsl2"]
    assert not c["ok"] and "wsl --install" in c["fix"]
    none = Result(1, "", "")
    assert not by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"wsl -l": none}))))["wsl2"]["ok"]


def test_wsl_is_not_needed_off_windows(tmp_path):
    c = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(platform="linux", runner=runner_with())))
    assert c["wsl2"]["ok"] and "not needed" in c["wsl2"]["detail"]


def test_disk_is_compared_with_the_need(tmp_path):
    c = by_id(preflight.run_checks(DOCKER, tmp_path / "not" / "made" / "yet", ready_probes(disk_free=lambda p: 1 * preflight.GB, runner=runner_with(**{"wsl -l": WSL_OK}))))["disk"]
    assert not c["ok"] and "1.0 GB free, 5 GB needed" in c["detail"]


def test_python_is_enough_through_uv_or_directly(tmp_path):
    old = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(python=(3, 10, 0), runner=runner_with(**{"wsl -l": WSL_OK}))))["python"]
    assert old["ok"] and "uv can fetch it" in old["detail"]
    no_uv = by_id(preflight.run_checks(DOCKER, tmp_path, ready_probes(python=(3, 10, 0), which=lambda n: None if n == "uv" else "/bin/" + n,
                                                                      runner=runner_with(**{"wsl -l": WSL_OK}))))["python"]
    assert not no_uv["ok"]


def test_no_judge_key_is_looked_for_or_read(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear")
    checks = preflight.run_checks(DOCKER, tmp_path, ready_probes(runner=runner_with(**{"wsl -l": WSL_OK})))
    assert not [c for c in checks if "judge" in c["id"] or "key" in c["id"]]
    assert "sk-must-not-appear" not in repr(checks)


def test_another_gpu_session_blocks_ready(tmp_path):
    checks = preflight.run_checks(DOCKER, tmp_path, ready_probes(gpu_busy=lambda: "the night session holds the GPU", runner=runner_with(**{"wsl -l": WSL_OK})))
    gpu = by_id(checks)["gpu_free"]
    assert not gpu["ok"] and "night session" in gpu["detail"] and gpu["fix"]
    assert not preflight.ready(checks)


def test_a_broken_gpu_lock_reading_counts_as_busy(tmp_path):
    def boom():
        raise OSError("flag file unreadable")
    assert not preflight.gpu_check(boom)["ok"]
    assert preflight.gpu_check(lambda: "")["ok"]


def test_a_source_on_hugging_face_needs_git_lfs(tmp_path):
    spec = make_spec("x", source_kind="hf_dataset")
    c = by_id(preflight.run_checks(spec, tmp_path, ready_probes(runner=runner_with(**{"git lfs": Result(1, "", "")}))))
    assert not c["git_lfs"]["ok"] and "Git LFS" in c["git_lfs"]["fix"]
