"""Installing agent-desktop (https://github.com/vercel-labs/agent-desktop, Apache-2.0) into the tools folder (`<data>/tools/agent-desktop/`), from the project's own GitHub release.

agent-desktop comes with TaDaMoo (built from third_party/agent-desktop, tools/vendored/); this is how a newer release is taken, and it runs only when the person asks (the button in Wallet >
Computer control, or `python -m tadamoo.computer install`): nothing here starts by itself, and the copy it puts in the tools folder is taken before the included one. The release is read from
api.github.com, the archive for this system is downloaded from github.com/vercel-labs/agent-desktop/releases/download, its SHA-256 is checked against the one the release gives
(the asset's `digest`, else the release's checksums.txt), and only then is it unpacked. A release that gives no checksum is installed with that said. The archive may hold the program
and a helper beside it; they are kept together. The program's own state (`state/`) is not touched by an upgrade.
"""
from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.computer.backend import AGENT_DESKTOP_NAMES, ComputerError, find_agent_desktop

REPO = "vercel-labs/agent-desktop"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
MAX_BYTES = 80 * 1024 * 1024
RECORD = "install.json"


def target_triple(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """The release's name for this system, for example x86_64-pc-windows-msvc."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64" if machine in ("amd64", "x86_64", "x64") else ""
    if not arch:
        raise ComputerError(f"agent-desktop has no release for this processor ({machine})")
    if system.startswith("win"):
        return f"{arch}-pc-windows-msvc"
    if system.startswith("linux"):
        return f"{arch}-unknown-linux-gnu"
    if system == "darwin":
        return f"{arch}-apple-darwin"
    raise ComputerError(f"agent-desktop has no release for this system ({system})")


def pick_asset(release: Dict[str, Any], triple: str) -> Dict[str, Any]:
    """The command-line archive of the release for this system (not the C library's)."""
    tag = str(release.get("tag_name") or "")
    for asset in release.get("assets") or []:
        if asset.get("name") == f"agent-desktop-{tag}-{triple}.tar.gz":
            return asset
    raise ComputerError(f"release {tag or '?'} has no archive for {triple}")


def expected_hash(asset: Dict[str, Any], checksums: Optional[str]) -> str:
    """The SHA-256 the release gives for this asset ('' when it gives none)."""
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest[7:].lower()
    for line in (checksums or "").splitlines():
        m = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line.strip())
        if m and m.group(2) == asset.get("name"):
            return m.group(1).lower()
    return ""


def _fetch(url: str, opener: Callable[..., Any], limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TaDaMoo", "Accept": "application/octet-stream, application/json"})
    try:
        with opener(req, timeout=60) as r:
            data = r.read(limit + 1)
    except OSError as e:
        raise ComputerError(f"could not download {url}: {e}") from None
    if len(data) > limit:
        raise ComputerError(f"{url} is larger than {limit // (1024 * 1024)} MB: not downloaded")
    return data


def _unpack(archive: bytes, into: Path) -> List[str]:
    """Regular files only, each inside `into`; anything else in the archive is refused."""
    names: List[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            target = (into / member.name).resolve()
            if not member.isreg() or into.resolve() not in target.parents:
                raise ComputerError(f"the archive holds something that is not a plain file inside it ({member.name!r}): not installed")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            names.append(member.name)
    return names


def installed(tools_dir: Path) -> Dict[str, Any]:
    """What is installed in the tools folder: the program, and what the install recorded."""
    path, where = find_agent_desktop(Path(tools_dir))
    record: Dict[str, Any] = {}
    try:
        record = json.loads((Path(tools_dir) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"found": bool(path), "path": path, "where": where, "version": str(record.get("version") or ""), "sha256_verified": bool(record.get("sha256_verified")),
            "installed_at": record.get("installed_at")}


def program_status(command: List[str], home: Optional[Path] = None) -> Dict[str, Any]:
    """`agent-desktop version` and `status` of a program: what the page shows. {} with an `error` when it does not run."""
    from tadamoo.computer.agent_desktop import AdCli
    cli = AdCli(command, home, timeout=15)
    try:
        return {"version": cli.run(["version"], interruptible=False), "status": cli.run(["status"], interruptible=False)}
    except ComputerError as e:
        return {"error": str(e)}


def install(tools_dir: Path, tag: str = "", api_url: str = API_URL, opener: Callable[..., Any] = urllib.request.urlopen, allowed_prefix: str = DOWNLOAD_PREFIX,
            triple: Optional[str] = None) -> Dict[str, Any]:
    """Downloads and installs the release (the latest, or `tag`) for this system. Raises ComputerError saying what went wrong; nothing is left half installed."""
    tools_dir = Path(tools_dir)
    url = api_url if not tag else api_url.replace("/latest", f"/tags/{tag}")
    release = json.loads(_fetch(url, opener, 5 * 1024 * 1024).decode("utf-8"))
    asset = pick_asset(release, triple or target_triple())
    if not str(asset.get("browser_download_url") or "").startswith(allowed_prefix):
        raise ComputerError("the release names a download that is not on the project's GitHub releases: not installed")
    checksums = None
    sums = next((a for a in release.get("assets") or [] if a.get("name") == "checksums.txt"), None)
    if sums and not str(asset.get("digest") or "").startswith("sha256:"):
        checksums = _fetch(str(sums["browser_download_url"]), opener, 1024 * 1024).decode("utf-8", "replace")
    want = expected_hash(asset, checksums)
    data = _fetch(str(asset["browser_download_url"]), opener, MAX_BYTES)
    got = hashlib.sha256(data).hexdigest()
    if want and got != want:
        raise ComputerError(f"the download does not match the checksum the release gives (got {got[:12]}, wanted {want[:12]}): not installed")
    tools_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".new-", dir=tools_dir))
    try:
        names = _unpack(data, stage)
        exe = next((n for n in names if Path(n).name in AGENT_DESKTOP_NAMES), "")
        if not exe:
            raise ComputerError("the archive has no agent-desktop program: not installed")
        try:
            for old in json.loads((tools_dir / RECORD).read_text(encoding="utf-8")).get("files", []):
                (tools_dir / old).unlink(missing_ok=True)                 # the files of the version being replaced
        except (OSError, ValueError):
            pass
        for name in names:
            dest = tools_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(stage / name), str(dest))
            except PermissionError:
                raise ComputerError("agent-desktop is in use (a conversation is running it): close the conversations that control the computer, then install again") from None
            if sys.platform != "win32" and Path(name).name.startswith("agent-desktop"):
                dest.chmod(0o755)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    version = str(release.get("tag_name") or "").lstrip("v")
    (tools_dir / RECORD).write_text(json.dumps({"version": version, "asset": asset.get("name"), "sha256": got, "sha256_verified": bool(want), "files": names,
                                                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    return {"version": version, "path": str(tools_dir / exe), "sha256": got, "sha256_verified": bool(want),
            "note": "" if want else "the release gave no checksum, so the download could not be checked"}
