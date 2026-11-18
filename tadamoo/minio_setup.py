"""MinIO for the images of the knowledge archives: finding one that is already on this PC, offering the choices, and setting up a separate copy of our own.

`detect` only looks. `plan` turns what was found into the choices the person is shown: use the one that exists, make a separate installation (its own folder, data and port),
or leave it. Nothing is downloaded or started here without a call that the person's answer led to: `install_from` copies a program the person points at (or one downloaded from a
release the caller gives, checked against its SHA-256), `MinioService` starts and stops our own copy, and `provision` writes the settings of the archives and the secret key into the vault."""
from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

NAMES = ("minio.exe", "silo.exe") if os.name == "nt" else ("minio", "silo")          # the community fork of MinIO is called Silo; either runs the same S3 service
EXE = NAMES[0]
RELEASE = {                                                  # the release shipped with TaDaMoo for Windows x64 (github.com/pgsty/silo, community fork of MinIO, AGPL-3.0)
    "tag": "RELEASE.2026-11-12T00-00-00Z",
    "url": "https://github.com/pgsty/silo/releases/download/RELEASE.2026-11-12T00-00-00Z/silo_20261112000000.0.0_windows_amd64.zip",
    "sha256": "4064756dcea753b21480f985bde045e111e4b68a593a5ad0e4e932625574e8e0",
    "bytes": 37717959,
}
WELL_KNOWN_PORTS = (9000, 9001)               # where an existing MinIO is usually listening
OWN_PORTS = range(9400, 9500)                 # ours never takes the ports of an existing one; 8080 is never used
FORBIDDEN_PORTS = (8080,)
LOCAL_BUCKET = "tadamoo-images"


def _candidates(env: Dict[str, str]) -> List[Path]:
    local = env.get("LOCALAPPDATA", "")
    found = [Path(p) / n for p in env.get("PATH", "").split(os.pathsep) if p for n in NAMES]
    found += [Path(local) / "Microsoft" / "WinGet" / "Links" / n for n in NAMES] if local else []
    for base in (env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", ""), "/usr/local/bin", "/opt/minio", "/usr/bin"):
        if base:
            found += [Path(base) / d / n for n in NAMES for d in ("MinIO", "Silo", "")]
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        try:
            found += [d / n for d in packages.iterdir() if d.name.lower().startswith(("minio", "silo")) for n in NAMES]
        except OSError:
            pass
    return found


def _listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect(env: Optional[Dict[str, str]] = None, ports: Sequence[int] = WELL_KNOWN_PORTS, listening: Callable[[str, int], bool] = _listening) -> Dict[str, Any]:
    """What is on this PC: `programs` (a MinIO that can be run, from the PATH, WinGet, Program Files...), `broken` (a link or entry that points at a file that is not there),
    and `listening` (addresses where something answers on the usual MinIO ports)."""
    env = dict(os.environ if env is None else env)
    programs, broken, seen = [], [], set()
    for c in _candidates(env):
        try:
            if c.is_symlink() and not c.exists():
                broken.append({"path": str(c), "points_to": os.readlink(c)})
                continue
            if not c.is_file():
                continue
            real = str(c.resolve())
            if real.lower() in seen:
                continue
            seen.add(real.lower())
            programs.append({"path": real, "size": c.stat().st_size})
        except OSError:
            continue
    return {"programs": programs, "broken": broken, "listening": [f"127.0.0.1:{p}" for p in ports if listening("127.0.0.1", p)]}


def plan(found: Dict[str, Any]) -> Dict[str, Any]:
    """The choices to show, in words a person can follow. Where something exists, no choice is marked as the right one: the person decides."""
    options = []
    exists = bool(found["programs"] or found["listening"])
    if exists:
        options.append({"id": "use_existing", "needs": ["endpoint", "access_key", "secret_key"],
                        "text": "Use the MinIO that is already here. You give its address and keys; TaDaMoo adds only its own bucket and never touches the others."})
    options.append({"id": "install_separate", "needs": ["program"],
                    "text": "Install a separate MinIO for TaDaMoo: its own folder, its own data and its own port, started and stopped by TaDaMoo. The one that exists is left alone."
                    if exists else "Install MinIO for TaDaMoo: its own folder, data and port, started and stopped by TaDaMoo."})
    options.append({"id": "skip", "needs": [], "text": "Not now. The images are kept as files in a folder and MinIO can be set up later."})
    return {"found": found, "exists": exists, "options": options, "recommended": None if exists else "install_separate"}


def free_port(taken: Sequence[int] = (), listening: Callable[[str, int], bool] = _listening) -> int:
    """A port for our own copy: the first one of 9400-9499 that nothing listens on, never 8080 or a usual MinIO port."""
    for p in OWN_PORTS:
        if p not in taken and p not in FORBIDDEN_PORTS and p not in WELL_KNOWN_PORTS and not listening("127.0.0.1", p):
            return p
    raise RuntimeError("no free port between 9400 and 9499")


def bundled_program() -> Optional[Path]:
    """MinIO, bundled with this copy of TaDaMoo (the hash of `RELEASE` was already checked by whoever made the package), when there is one."""
    from serve import addons
    found = addons.addons_dir()
    if found is None:
        return None
    p = found / "minio" / EXE
    return p if p.is_file() else None


def install_release(folder: Path, release: Optional[Dict[str, Any]] = None, fetch: Optional[Callable[[str, Path], None]] = None) -> Path:
    """Downloads the release TaDaMoo ships (`RELEASE`, or the one given), checks its SHA-256 and unpacks the service program into `folder`. Called only after the person said yes."""
    release = release or RELEASE
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "download.zip"
    (fetch or (lambda u, p: urllib.request.urlretrieve(u, p)))(release["url"], archive)
    try:
        if sha256_of(archive) != release["sha256"].lower():
            raise ValueError("the downloaded file is not the one expected (its SHA-256 differs): it was removed")
        with zipfile.ZipFile(archive) as z:
            member = next((m for m in z.namelist() if Path(m).name.lower() in NAMES), None)
            if member is None:
                raise ValueError("the archive holds no MinIO program")
            target = folder / Path(member).name
            with z.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    finally:
        archive.unlink(missing_ok=True)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install_from(folder: Path, program: Optional[Path] = None, url: str = "", sha256: str = "", fetch: Optional[Callable[[str, Path], None]] = None) -> Path:
    """Puts a MinIO program into `folder` (our own copy). `program` is a file the person pointed at, copied as it is; `url` with `sha256` is a release the caller names, downloaded
    and refused if its hash differs. One of the two is needed: no address is built in here."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / EXE
    if program is not None:
        if not Path(program).is_file():
            raise FileNotFoundError(f"there is no such program: {program}")
        shutil.copy2(program, target)
    elif url:
        if len(sha256) != 64:
            raise ValueError("a download needs the SHA-256 of the file, to check it")
        part = target.with_name(target.name + ".part")
        (fetch or (lambda u, p: urllib.request.urlretrieve(u, p)))(url, part)
        if sha256_of(part) != sha256.lower():
            part.unlink(missing_ok=True)
            raise ValueError("the downloaded file is not the one expected (its SHA-256 differs): it was removed")
        os.replace(part, target)
    else:
        raise ValueError("give the program to copy, or the address and the SHA-256 of a release")
    if os.name != "nt":
        target.chmod(0o755)
    return target


class MinioService:
    """Our own MinIO process: started on 127.0.0.1 with its own data folder and keys, stopped by us. `launcher(command, env)` starts it and returns an object with `poll()` and
    `terminate()`; the default starts a real process."""

    def __init__(self, program: Path, data: Path, port: int, access_key: str, secret_key: str, launcher: Optional[Callable[[List[str], Dict[str, str]], Any]] = None) -> None:
        if port in FORBIDDEN_PORTS:
            raise ValueError("that port is never used")
        self.program, self.data, self.port, self._access, self._secret = Path(program), Path(data), port, access_key, secret_key
        self._launcher = launcher or (lambda cmd, env: subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self._proc: Any = None

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.port}"

    def command(self) -> List[str]:
        return [str(self.program), "server", str(self.data), "--address", self.endpoint, "--console-address", f"127.0.0.1:{self.port + 1}"]

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> str:
        if self.running():
            return self.endpoint
        if not self.program.is_file():
            raise FileNotFoundError(f"MinIO is not installed at {self.program}")
        self.data.mkdir(parents=True, exist_ok=True)
        self._proc = self._launcher(self.command(), {**os.environ, "MINIO_ROOT_USER": self._access, "MINIO_ROOT_PASSWORD": self._secret})
        return self.endpoint

    def stop(self) -> None:
        if self.running():
            self._proc.terminate()
        self._proc = None


def new_keys() -> Dict[str, str]:
    """An access key and a secret key for our own copy, made here and never shown in a file: the secret goes to the vault."""
    return {"access_key": "tadamoo-" + secrets.token_hex(4), "secret_key": secrets.token_urlsafe(24)}


def provision(store: Any, put_secret: Callable[[str, str], None], endpoint: str, access_key: str, secret_key: str, bucket: str = LOCAL_BUCKET, vault_entry: str = "images-s3") -> Dict[str, Any]:
    """Points the knowledge archives at a MinIO: the secret key goes to the vault entry (`put_secret(entry, secret)`), the rest to the knowledge settings, which then keep the images
    in the service. Used for our own copy and for an existing one the person gave the keys of."""
    put_secret(vault_entry, secret_key)
    return store.settings.write({"images_store": "s3", "images_s3_endpoint": endpoint, "images_s3_bucket": bucket, "images_s3_access_key": access_key, "images_s3_vault": vault_entry})
