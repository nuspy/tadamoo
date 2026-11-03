"""The audit hook of programs the agent runs: it refuses the protected places and nothing else."""
import os
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "harness" / "sandbox" / "guard"


def run_guarded(tmp_path, code):
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "secret.txt").write_text("s")
    env = {**os.environ, "TADAMOO_GUARD": str(protected), "PYTHONPATH": str(GUARD)}
    return subprocess.run([sys.executable, "-c", "import tadamoo_guard; tadamoo_guard.install()\n" + code.replace("PROTECTED", repr(str(protected)))],
                          capture_output=True, text=True, env=env, timeout=60)


def test_a_protected_file_is_refused(tmp_path):
    out = run_guarded(tmp_path, "try:\n    open(PROTECTED + '/secret.txt').read()\nexcept PermissionError:\n    print('refused')")
    assert out.stdout.strip() == "refused", out.stderr


def test_ctypes_can_load_a_library(tmp_path):
    out = run_guarded(tmp_path, "import ctypes, ctypes.util\nlib = ctypes.CDLL(None) if hasattr(ctypes, 'CDLL') and __import__('os').name != 'nt' else ctypes.WinDLL('kernel32')\nprint('loaded')")
    assert out.stdout.strip() == "loaded", out.stderr
