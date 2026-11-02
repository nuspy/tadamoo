"""What a benchmark needs on this PC, checked before anything starts. Every check is `{id, ok, detail, fix}` with a fix in plain words. Programs, disk,
network and the GPU lock are reached through injectable parts so the checks can be tried without them."""
from __future__ import annotations

import shutil
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .importer import Runner, default_runner
from .registry import BenchSpec

GB = 1024 ** 3
PROBE_TIMEOUT_S = 30


def _online(host: str) -> bool:
    try:
        socket.create_connection((host, 443), timeout=4).close()
        return True
    except OSError:
        return False


@dataclass
class Probes:
    """The parts a check reads the machine through."""
    runner: Runner = default_runner
    which: Callable[[str], Optional[str]] = shutil.which
    disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free
    gpu_busy: Optional[Callable[[], str]] = None            # the reason another GPU session is active, or "" when none is
    online: Callable[[str], bool] = _online
    python: Tuple[int, ...] = field(default_factory=lambda: tuple(sys.version_info[:3]))
    platform: str = sys.platform


def check(id: str, ok: bool, detail: str, fix: str = "", required: bool = True) -> Dict[str, Any]:
    return {"id": id, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix, "required": required}


def _nearest_existing(path: Path) -> Path:
    p = Path(path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _wsl_has_v2(text: str) -> bool:
    for line in text.replace("\x00", "").splitlines():
        parts = line.replace("*", " ").split()
        if len(parts) >= 3 and parts[-1] == "2" and parts[0].upper() != "NAME":
            return True
    return False


def run_checks(spec: BenchSpec, base_dir: Path, probes: Optional[Probes] = None) -> List[Dict[str, Any]]:
    """All checks for one benchmark. No judge key is looked for: judges and simulators are always the local model."""
    pr = probes or Probes()
    out = [check("git", bool(pr.which("git")), "git found" if pr.which("git") else "git is not installed",
                 "Install Git for Windows (https://git-scm.com) and open the app again.")]
    if spec.source_kind == "hf_dataset":
        lfs = pr.runner(["git", "lfs", "version"], None, PROBE_TIMEOUT_S)
        out.append(check("git_lfs", lfs.returncode == 0, "git-lfs found" if lfs.returncode == 0 else "git-lfs is missing, so dataset files cannot be fetched",
                         "Install Git LFS (https://git-lfs.com), then run 'git lfs install' once."))
    for tool in spec.tools:
        found = pr.which(tool)
        out.append(check(tool, bool(found), f"{tool} found" if found else f"{tool} is not installed",
                         "Install uv (https://docs.astral.sh/uv) so the benchmark can set up its own Python environment." if tool == "uv" else f"Install {tool} and make sure it is on the PATH."))
    if spec.python_min:
        need = ".".join(map(str, spec.python_min))
        ok = pr.python[:len(spec.python_min)] >= spec.python_min or bool(pr.which("uv"))
        out.append(check("python", ok, f"Python {'.'.join(map(str, pr.python))} here, {need} needed" + (" (uv can fetch it)" if ok and pr.python[:len(spec.python_min)] < spec.python_min else ""),
                         f"Install Python {need} or install uv, which downloads it by itself."))
    if spec.needs.docker:
        client = pr.which("docker")
        out.append(check("docker_client", bool(client), "Docker client found" if client else "the docker command is not installed",
                         "Install Docker Desktop (https://www.docker.com/products/docker-desktop) and restart the app."))
        if client:
            info = pr.runner(["docker", "info"], None, PROBE_TIMEOUT_S)
            out.append(check("docker_daemon", info.returncode == 0, "Docker engine reachable" if info.returncode == 0 else "the Docker engine does not answer",
                             "Start Docker Desktop and wait until it says Engine running."))
    if spec.needs.wsl2:
        if pr.platform.startswith("win"):
            wsl = pr.runner(["wsl", "-l", "-v"], None, PROBE_TIMEOUT_S)
            ok = wsl.returncode == 0 and _wsl_has_v2(wsl.stdout)
            out.append(check("wsl2", ok, "a WSL2 distribution is installed" if ok else "no WSL2 distribution was found",
                             "Open a terminal as administrator, run 'wsl --install', restart the PC and finish the Linux user setup. 'wsl -l -v' must list one distribution with VERSION 2."))
        else:
            out.append(check("wsl2", True, "not needed on this system"))
    need_gb = spec.disk_gb
    try:
        free = pr.disk_free(_nearest_existing(base_dir))
        out.append(check("disk", free >= need_gb * GB, f"{free / GB:.1f} GB free, {need_gb:g} GB needed",
                         f"Free at least {need_gb:g} GB on the drive that holds {base_dir}, or choose another data folder."))
    except OSError as exc:
        out.append(check("disk", False, f"free space could not be read ({exc})", "Check that the data folder's drive is connected."))
    if spec.needs.internet:
        host = urlparse(spec.source).hostname or "github.com"
        up = pr.online(host)
        out.append(check("internet", up, f"{host} reachable" if up else f"{host} cannot be reached", "Connect to the internet; the benchmark and its data are fetched from there."))
    out.append(gpu_check(pr.gpu_busy))
    return out


def gpu_check(gpu_busy: Optional[Callable[[], str]]) -> Dict[str, Any]:
    """Not ready while another session holds the GPU. A lock source that fails counts as busy: the state is unknown."""
    if gpu_busy is None:
        return check("gpu_free", True, "no other GPU session is registered")
    try:
        reason = gpu_busy()
    except Exception as exc:
        return check("gpu_free", False, f"the GPU lock could not be read ({type(exc).__name__})", "Check the other session's flag file, then try again.")
    return check("gpu_free", not reason, "the GPU is free" if not reason else f"another GPU session is active: {reason}",
                 "Wait until the other GPU session has finished, then try again. Only one GPU process runs at a time.")


def ready(checks: List[Dict[str, Any]]) -> bool:
    return all(c["ok"] for c in checks if c.get("required", True))
