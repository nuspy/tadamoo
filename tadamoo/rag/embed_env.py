"""The Python environment for the knowledge archives' embeddings and Docling (embed_client.py, ingest/docling_worker.py): a separate virtual environment with
torch, transformers and docling, built once from a Python already on this PC. Set up on request (Settings > Knowledge, "Install the embeddings environment"),
never forced on every install: torch alone is a few GB, and someone who does not use the knowledge archives' semantic search does not need it.

Pinned: torch 2.9.1+cu130, from PyTorch's own index for CUDA 13 (download.pytorch.org/whl/cu130) — the build already measured and used on this GPU for other
work (docs/media/speed.md), checked live against the real index before it was pinned here. transformers and docling are not pinned: pure-Python packages
with no CUDA build of their own, and embed_worker.py already tolerates more than one transformers version."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

MIN_MINOR, MAX_MINOR = 10, 14             # torch 2.9.1+cu130 has Windows wheels for cp310 through cp314
TORCH = "torch==2.9.1+cu130"
TORCH_INDEX = "https://download.pytorch.org/whl/cu130"
PACKAGES = ("transformers", "docling")
PROBE_S = 20.0
INSTALL_S = 3600.0                        # torch alone is a few GB: a slow connection needs real time


class EmbedEnvError(RuntimeError):
    pass


def _run(argv: List[str], timeout: float, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    import os
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, env={**os.environ, **(env or {})}, **kwargs)


def _candidate_pythons() -> List[str]:
    """Every Python this PC's launcher (or PATH, elsewhere) knows of, newest first; version filtering happens in `find_python`."""
    if sys.platform == "win32":
        try:
            out = _run(["py", "-0p"], 15.0).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        return [m.group(1) for m in re.finditer(r"(\S.*?python(?:w)?\.exe)\s*$", out, re.M) if Path(m.group(1)).is_file()]
    found = [shutil.which(f"python3.{m}") for m in range(MAX_MINOR, MIN_MINOR - 1, -1)]
    return [f for f in found if f]


def find_python(candidates: Optional[List[str]] = None) -> str:
    """A Python 3.10-3.14 already on this PC, newest first; "" when there is none (we never download an interpreter)."""
    for exe in candidates if candidates is not None else _candidate_pythons():
        try:
            out = _run([exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"], PROBE_S).stdout.split()
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if len(out) == 2 and out[0] == "3" and MIN_MINOR <= int(out[1]) <= MAX_MINOR:
            return exe
    return ""


class Runtime:
    """One venv, under `base`, with torch (CUDA 13), transformers and docling installed; built from a Python already on this PC."""

    def __init__(self, base: Path, runner: Callable[..., Any] = _run) -> None:
        self.base = Path(base)
        self.venv = self.base / "venv"
        self._run = runner

    def _venv_python(self) -> Optional[Path]:
        for rel in ("Scripts/python.exe", "bin/python"):
            p = self.venv / rel
            if p.is_file():
                return p
        return None

    def installed(self) -> bool:
        return self._venv_python() is not None

    def info(self) -> Dict[str, Any]:
        py = find_python()
        return {"installed": self.installed(), "python_found": bool(py), "python": py}

    def install(self, say: Callable[[str], None] = lambda text: None) -> None:
        """Creates the venv (from a Python 3.10-3.14 on this PC) and installs torch, transformers and docling into it. Raises EmbedEnvError with the reason;
        a failed pip install removes the venv, so a later try starts clean."""
        py = find_python()
        if not py:
            raise EmbedEnvError("No Python 3.10, 3.11, 3.12, 3.13 or 3.14 was found on this PC. Install one from python.org, then try again.")
        self.base.mkdir(parents=True, exist_ok=True)
        say(f"Python found: {py}")
        done = self._run([py, "-m", "venv", str(self.venv)], 120.0)
        if done.returncode != 0:
            raise EmbedEnvError(f"Could not create the virtual environment: {(done.stderr or done.stdout).strip()[:400]}")
        say("Virtual environment created")
        pip_py = self._venv_python()
        if pip_py is None:
            raise EmbedEnvError("The virtual environment was created without a usable python")
        done = self._run([str(pip_py), "-m", "pip", "install", "--upgrade", "pip"], 180.0)
        say("pip ready" if done.returncode == 0 else "pip: using the venv's own version")
        say(f"installing {TORCH} from {TORCH_INDEX} (a few GB: this can take a while)")
        done = self._run([str(pip_py), "-m", "pip", "install", TORCH, "--index-url", TORCH_INDEX], INSTALL_S)
        if done.returncode != 0:
            shutil.rmtree(self.venv, ignore_errors=True)
            raise EmbedEnvError(f"pip install {TORCH} failed: {(done.stderr or done.stdout).strip()[-800:]}")
        say("torch installed")
        done = self._run([str(pip_py), "-m", "pip", "install", *PACKAGES], 900.0)
        if done.returncode != 0:
            shutil.rmtree(self.venv, ignore_errors=True)
            raise EmbedEnvError(f"pip install {' '.join(PACKAGES)} failed: {(done.stderr or done.stdout).strip()[-800:]}")
        say(f"{', '.join(PACKAGES)} installed")

    def remove(self) -> None:
        shutil.rmtree(self.venv, ignore_errors=True)

    def probe(self) -> str:
        """`cuda`, `cpu` or "" (torch missing or broken): what this venv's own torch reports, asked fresh each time."""
        py = self._venv_python()
        if py is None:
            return ""
        try:
            done = self._run([str(py), "-c", "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"], 120.0)
        except (OSError, subprocess.SubprocessError):
            return ""
        last = (done.stdout or "").strip().splitlines()[-1:] or [""]
        return last[0] if done.returncode == 0 and last[0] in ("cuda", "cpu") else ""


_SHARED: Optional[Runtime] = None


def shared() -> Runtime:
    """The one runtime of this process, in the tools folder of the user data."""
    global _SHARED
    if _SHARED is None:
        from serve.userdata import path as userdata_path
        _SHARED = Runtime(userdata_path("tools") / "embeddings")
    return _SHARED
