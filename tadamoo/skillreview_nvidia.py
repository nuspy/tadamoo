"""NVIDIA's SkillEvaluator (Apache-2.0, github.com/NVIDIA/SkillEvaluator) as a second, optional review of a skill before it is installed, next to our own
(`tadamoo/connectors/review.py`). The person chooses, in Settings > Connectors: our own review, NVIDIA's, or both.

It needs Python 3.12 or newer, which this program's own Python (3.10) is not: it gets its own small virtual environment, built once from a Python 3.12+
already on the PC (never downloaded), under a tools folder. Only its Tier 1 (static checks: schema, security scanners, licence, PII, quality; no network,
no LLM key, no Docker) is wired in by default. Tier 3 (a live agent run in a sandbox) is opt-in and needs Docker on this PC; left off, only Tier 1 (and
Tier 2 when an LLM key is configured) runs. Nothing here patches or forks NVIDIA's package: it runs as published, in its own Python."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PACKAGE = "skillevaluator"
MIN_MINOR, MAX_MINOR = 12, 14              # requires-python >=3.12,<3.15
PROBE_S = 20.0
RUN_S = 600.0


class SkillEvaluatorError(RuntimeError):
    pass


def _run(argv: List[str], timeout: float, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    import os
    kwargs = {"capture_output": True, "text": True, "timeout": timeout}
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
    """A Python 3.12-3.14 already on this PC, newest first; "" when there is none (we never download an interpreter)."""
    for exe in candidates if candidates is not None else _candidate_pythons():
        try:
            out = _run([exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"], PROBE_S).stdout.split()
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if len(out) == 2 and out[0] == "3" and MIN_MINOR <= int(out[1]) <= MAX_MINOR:
            return exe
    return ""


class Runtime:
    """One venv, under `base`, with `skillevaluator` installed in it; built from a Python 3.12+ found on this PC."""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.venv = self.base / "venv"

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

    def install(self, say=lambda text: None) -> None:
        """Creates the venv (from a Python 3.12+ on this PC) and installs the package into it. Raises SkillEvaluatorError with the reason."""
        py = find_python()
        if not py:
            raise SkillEvaluatorError("No Python 3.12, 3.13 or 3.14 was found on this PC. Install one from python.org, then try again; "
                                       "this program's own Python is not used to run NVIDIA's tool.")
        self.base.mkdir(parents=True, exist_ok=True)
        say(f"Python found: {py}")
        done = _run([py, "-m", "venv", str(self.venv)], 120.0)
        if done.returncode != 0:
            raise SkillEvaluatorError(f"Could not create the virtual environment: {(done.stderr or done.stdout).strip()[:400]}")
        say("Virtual environment created")
        pip_py = self._venv_python()
        if pip_py is None:
            raise SkillEvaluatorError("The virtual environment was created without a usable python.exe")
        done = _run([str(pip_py), "-m", "pip", "install", "--upgrade", "pip"], 180.0)
        say("pip ready" if done.returncode == 0 else "pip: using the venv's own version")
        done = _run([str(pip_py), "-m", "pip", "install", PACKAGE], 600.0)
        if done.returncode != 0:
            shutil.rmtree(self.venv, ignore_errors=True)
            raise SkillEvaluatorError(f"pip install {PACKAGE} failed: {(done.stderr or done.stdout).strip()[-800:]}")
        say(f"{PACKAGE} installed")

    def remove(self) -> None:
        shutil.rmtree(self.venv, ignore_errors=True)

    def version(self) -> str:
        py = self._venv_python()
        if py is None:
            return ""
        done = _run([str(py), "-m", "pip", "show", PACKAGE], 30.0)
        m = re.search(r"^Version:\s*(\S+)", done.stdout, re.M)
        return m.group(1) if m else ""

    # -- running it ---------------------------------------------------------------------------------------

    def quality_check(self, skill_dir: Path) -> Dict[str, Any]:
        """Tier 1 only: static checks (schema, security scanners, licence, PII, quality), no network, no LLM key, no Docker."""
        return self._run_report(["quality-check", str(skill_dir), "--min-score", "0"], "quality")

    def validate(self, skill_dir: Path, tiers: str) -> Dict[str, Any]:
        """`tiers`: "1" (static only), "1,2" (adds semantic deduplication), or "1,2,3" (adds a live agent run; needs Docker, checked by the caller)."""
        return self._run_report(["validate", str(skill_dir), "--tiers", tiers, "--min-score", "0"], "validate")

    def _run_report(self, args: List[str], basename_hint: str) -> Dict[str, Any]:
        py = self._venv_python()
        if py is None:
            raise SkillEvaluatorError("NVIDIA SkillEvaluator is not installed (Settings > Connectors)")
        with tempfile.TemporaryDirectory(prefix="skillevaluator-") as tmp:
            argv = [str(py), "-m", PACKAGE, *args, "--report", "json", "--output-dir", tmp]
            try:
                done = _run(argv, RUN_S)
            except subprocess.TimeoutExpired:
                raise SkillEvaluatorError(f"NVIDIA SkillEvaluator did not finish in {int(RUN_S)}s") from None
            reports = sorted(Path(tmp).glob("*.json"))
            if not reports:
                raise SkillEvaluatorError((done.stderr or done.stdout or "no report was written").strip()[-800:] or "no report was written")
            try:
                return json.loads(reports[-1].read_text(encoding="utf-8"))
            except ValueError as e:
                raise SkillEvaluatorError(f"the report could not be read: {e}") from None


_SHARED: Optional[Runtime] = None


def shared() -> Runtime:
    global _SHARED
    if _SHARED is None:
        from serve.userdata import path as userdata_path
        _SHARED = Runtime(userdata_path("tools") / "skillevaluator")
    return _SHARED
