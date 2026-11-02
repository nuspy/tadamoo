"""The outside-program runner: a program that does not start is stopped and reported."""
import sys

from tadamoo.bench.adapters import proc


def test_a_program_with_no_sign_of_life_is_killed_after_the_time_given_and_one_that_shows_it_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, "POLL_S", 0.05)
    dumped = []
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    out = proc.long_runner(sleeper, cwd=tmp_path, timeout=20, stall_after=0.3, alive=lambda: False, on_stall=dumped.append)
    assert out.returncode == proc.STALLED and len(dumped) == 1 and isinstance(dumped[0], int)
    quick = [sys.executable, "-c", "import time; time.sleep(0.6)"]
    out = proc.long_runner(quick, cwd=tmp_path, timeout=20, stall_after=0.2, alive=lambda: True, on_stall=dumped.append)
    assert out.returncode == 0 and len(dumped) == 1


def test_the_stack_dump_never_raises_and_names_the_program(tmp_path):
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        proc.dump_stacks(p.pid, tmp_path / "d.txt")
        assert "python" in (tmp_path / "d.txt").read_text(encoding="utf-8").lower()
        proc.dump_stacks(99999999, tmp_path / "e.txt")
        assert (tmp_path / "e.txt").read_text(encoding="utf-8").startswith("no dump")
    finally:
        p.kill()
