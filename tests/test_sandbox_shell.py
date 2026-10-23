import sys
import time
from pathlib import Path

import pytest

from harness.sandbox import ProcessSandbox, blocked_reason, command_words
from harness.secrets_broker import SecretsBroker
from harness.tools.shell import execute_shell_command

PY = f'"{sys.executable}"'


@pytest.mark.parametrize("command", [
    "reg query HKLM\\Software",
    "echo hi & reg add HKCU\\x",
    "cd . && schtasks /query",
    "cmd /c reg delete HKLM\\x",
    'powershell -NoProfile -Command "netsh advfirewall reset"',
    "dir | sc stop foo",
    "sudo reg add x",
    "C:\\Windows\\System32\\reg.exe query x",
])
def test_deny_list_sees_command_words_anywhere(command):
    assert blocked_reason(command) is not None


@pytest.mark.parametrize("command", [
    "python -c \"print('sc and reg are only words here')\"",
    "git commit -m 'fix reg handling'",
    "pytest -q tests/test_registry.py",
])
def test_deny_list_ignores_arguments_that_are_not_programs(command):
    assert blocked_reason(command) is None


def test_command_words_follow_wrappers():
    assert "reg" in command_words("cmd /c reg add x")
    assert command_words("echo a && echo b") == ["echo", "echo"]


def test_blocked_command_is_reported(tmp_path: Path):
    res = ProcessSandbox(tmp_path).run("echo x & netsh winsock reset")
    assert res["blocked"] and not res["success"]
    assert "blocked by sandbox" in res["stderr"]


def test_environment_is_scrubbed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_should_not_leak")
    res = ProcessSandbox(tmp_path).run(f"{PY} -c \"import os; print(sorted(os.environ))\"")
    assert res["success"], res
    assert "GITHUB_TOKEN" not in res["stdout"] and "AWS_SECRET_ACCESS_KEY" not in res["stdout"]


def test_timeout_kills_the_whole_process_tree(tmp_path: Path):
    marker = tmp_path / "alive.txt"
    child = ["import time", "time.sleep(3)", f"open(r'{marker}', 'w').write('child survived')"]
    parent = ["import subprocess, sys, time", "subprocess.Popen([sys.executable, 'child.py'])", "time.sleep(60)"]
    (tmp_path / "child.py").write_text("\n".join(child) + "\n", encoding="utf-8")
    (tmp_path / "parent.py").write_text("\n".join(parent) + "\n", encoding="utf-8")
    t0 = time.monotonic()
    res = ProcessSandbox(tmp_path).run(f"{PY} parent.py", timeout=1)
    assert time.monotonic() - t0 < 20
    assert res["timed_out"] and not res["success"]
    time.sleep(4)
    assert not marker.exists(), "the grandchild kept running after the timeout"


def test_output_is_capped_without_hanging(tmp_path: Path):
    res = ProcessSandbox(tmp_path, max_output_bytes=1000).run(f"{PY} -c \"print('x' * 200000)\"")
    assert res["success"] and res["truncated"]
    assert len(res["stdout"]) <= 1000


def test_stdin_is_closed(tmp_path: Path):
    res = ProcessSandbox(tmp_path).run(f"{PY} -c \"import sys; print(repr(sys.stdin.read()))\"", timeout=10)
    assert res["success"] and "''" in res["stdout"]


def test_timeout_is_clamped(tmp_path: Path):
    res = ProcessSandbox(tmp_path).run(f"{PY} -c \"print(1)\"", timeout=-5)
    assert res["success"]


def test_shell_tool_fills_secrets_at_the_boundary_and_masks_the_output(tmp_path: Path):
    broker = SecretsBroker({"TOKEN": "s3cr3t-value"})
    res = execute_shell_command(f"{PY} -c \"print('{{{{secret:TOKEN}}}}')\"", tmp_path, secrets=broker)
    assert res["success"], res
    assert "s3cr3t-value" not in res["stdout"]
    assert "[REDACTED:TOKEN]" in res["stdout"]
