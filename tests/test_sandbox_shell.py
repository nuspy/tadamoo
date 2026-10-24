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


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are a Windows feature")


@windows_only
def test_a_command_cannot_take_more_memory_than_its_cap(tmp_path: Path):
    marker = tmp_path / "allocated.txt"
    (tmp_path / "hog.py").write_text(f"x = bytearray(600 * 1024 * 1024)\nx[::4096] = b'1' * len(x[::4096])\nopen(r'{marker}', 'w').write('ok')\n", encoding="utf-8")
    capped = ProcessSandbox(tmp_path, max_memory_bytes=200 << 20).run(f"{PY} hog.py", timeout=60)
    assert not capped["success"] and not marker.exists()
    free = ProcessSandbox(tmp_path, max_memory_bytes=2 << 30).run(f"{PY} hog.py", timeout=60)
    assert free["success"] and marker.exists()


@windows_only
def test_a_command_cannot_start_more_processes_than_its_cap(tmp_path: Path):
    (tmp_path / "fork.py").write_text(
        "import subprocess, sys\n"
        "kids = []\n"
        "try:\n"
        "    for _ in range(12):\n"
        "        kids.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']))\n"
        "    print('started', len(kids))\n"
        "except OSError:\n"
        "    print('refused', len(kids))\n"
        "finally:\n"
        "    [k.kill() for k in kids]\n", encoding="utf-8")
    res = ProcessSandbox(tmp_path, max_processes=5).run(f"{PY} fork.py", timeout=60)
    assert "refused" in res["stdout"], res
    assert "started 12" in ProcessSandbox(tmp_path, max_processes=64).run(f"{PY} fork.py", timeout=60)["stdout"]


@windows_only
def test_a_process_left_running_dies_with_the_command(tmp_path: Path):
    marker = tmp_path / "late.txt"
    (tmp_path / "late.py").write_text(f"import time\ntime.sleep(3)\nopen(r'{marker}', 'w').write('left behind')\n", encoding="utf-8")
    (tmp_path / "leave.py").write_text("import subprocess, sys\nsubprocess.Popen([sys.executable, 'late.py'])\nprint('parent done')\n", encoding="utf-8")
    res = ProcessSandbox(tmp_path).run(f"{PY} leave.py", timeout=30)
    assert res["success"] and "parent done" in res["stdout"]
    time.sleep(4.5)
    assert not marker.exists(), "a background process outlived its command"


@windows_only
def test_the_command_runs_nothing_before_it_is_in_the_job(tmp_path: Path):
    # created suspended and put in the job first: a command that forks at once is still counted against the cap
    (tmp_path / "burst.py").write_text(
        "import subprocess, sys\n"
        "n = 0\n"
        "for _ in range(8):\n"
        "    try:\n"
        "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); n += 1\n"
        "    except OSError:\n"
        "        break\n"
        "print('forks', n)\n", encoding="utf-8")
    res = ProcessSandbox(tmp_path, max_processes=3).run(f"{PY} burst.py", timeout=30)
    forks = int(res["stdout"].split("forks")[1].split()[0])
    assert forks <= 2, res                          # the shell and the interpreter already use the first slots
