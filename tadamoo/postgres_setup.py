"""PostgreSQL for the knowledge archives' database: finding one already on this PC, offering the choices, and setting up a separate copy of our own.

`detect` only looks. `plan` turns what was found into the choices shown: use the one that exists, make a separate installation (its own folder, data and port), or leave
it (the archives keep using SQLite). `install_release` unpacks the official PostgreSQL binaries TaDaMoo ships (checked against the SHA-256 pinned here; never someone
else's build) into a folder, keeping only the server, its libraries and the headers a native extension needs, never pgAdmin or StackBuilder (together over 800 MB of a
GUI this never opens). `pgvector` is not part of this: there is no official Windows build of it, so ours is compiled from its own source with the same compiler that
builds the engine, at package time (compile_and_package.ps1), never a stranger's binary with full access to the database.

`PostgresService` makes the data directory the first time (`initdb`, a password of its own, local connections only) then starts and stops the server with `pg_ctl`,
PostgreSQL's own tool for that (a plain kill leaves the data directory needing WAL recovery on the next start; still safe, but `pg_ctl stop` asks it to finish first).
`provision` writes the connection settings and the password into the vault."""
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

EXE = "postgres.exe" if os.name == "nt" else "postgres"
RELEASE = {                                                   # the official EDB Windows x64 zip (no installer); pgAdmin and StackBuilder are in it but never unpacked
    "version": "18.7",
    "url": "https://get.enterprisedb.com/postgresql/postgresql-18.7-1-windows-x64-binaries.zip",
    "sha256": "ead0bbc27fb678fa24dcf86b738981323042abd073c744103bf7f3aed23f9a33",
    "bytes": 389515503,
}
KEEP_MEMBERS = ("bin/", "lib/", "share/", "include/", "server_license.txt")     # of the zip's pgsql/ folder: never pgAdmin, StackBuilder or doc
WELL_KNOWN_PORTS = (5432,)                    # where an existing PostgreSQL usually listens
OWN_PORTS = range(5500, 5600)                 # ours never takes the port of an existing one; 8080 is never used
FORBIDDEN_PORTS = (8080,)
LOCAL_DATABASE = "tadamoo_rag"
LOCAL_USER = "tadamoo"


def _candidates(env: Dict[str, str]) -> List[Path]:
    local = env.get("LOCALAPPDATA", "")
    found = [Path(p) / EXE for p in env.get("PATH", "").split(os.pathsep) if p]
    for base in (env.get("ProgramFiles", ""), env.get("ProgramFiles(x86)", "")):
        if not base:
            continue
        try:
            found += [d / "bin" / EXE for d in (Path(base) / "PostgreSQL").iterdir() if d.is_dir()]
        except OSError:
            pass
    for base in ("/usr/lib/postgresql", "/usr/local/bin", "/opt/postgresql"):
        if os.path.isdir(base):
            try:
                found += [d / "bin" / "postgres" for d in Path(base).iterdir() if d.is_dir()] + [Path(base) / "postgres"]
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
    """What is on this PC: `programs` (a `postgres` that can be run, from the PATH or Program Files\\PostgreSQL\\*), `broken` (a link that points nowhere), and
    `listening` (addresses where something answers on the usual PostgreSQL port)."""
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
        options.append({"id": "use_existing", "needs": ["host", "port", "database", "user", "password"],
                        "text": "Use the PostgreSQL that is already here. You give its address and a login; TaDaMoo makes only its own database (or schema) and never touches the rest."})
    options.append({"id": "install_separate", "needs": ["program"],
                    "text": "Install a separate PostgreSQL for TaDaMoo: its own folder, its own data and its own port, started and stopped by TaDaMoo. The one that exists is left alone."
                    if exists else "Install PostgreSQL for TaDaMoo: its own folder, data and port, started and stopped by TaDaMoo."})
    options.append({"id": "skip", "needs": [], "text": "Not now. The archives keep their database in SQLite and PostgreSQL can be set up later."})
    return {"found": found, "exists": exists, "options": options, "recommended": None if exists else "install_separate"}


def free_port(taken: Sequence[int] = (), listening: Callable[[str, int], bool] = _listening) -> int:
    """A port for our own copy: the first one of 5500-5599 that nothing listens on, never 8080 or the usual PostgreSQL port."""
    for p in OWN_PORTS:
        if p not in taken and p not in FORBIDDEN_PORTS and p not in WELL_KNOWN_PORTS and not listening("127.0.0.1", p):
            return p
    raise RuntimeError("no free port between 5500 and 5599")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def install_release(folder: Path, release: Optional[Dict[str, Any]] = None, fetch: Optional[Callable[[str, Path], None]] = None) -> Path:
    """Downloads the release TaDaMoo ships (`RELEASE`, or the one given), checks its SHA-256, and unpacks the server, its libraries and the headers (never pgAdmin or
    StackBuilder) into `folder`. Called only after the person said yes. Returns the path of `bin/postgres.exe` (or `bin/postgres`)."""
    release = release or RELEASE
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "download.zip"
    (fetch or (lambda u, p: urllib.request.urlretrieve(u, p)))(release["url"], archive)
    try:
        if sha256_of(archive) != release["sha256"].lower():
            raise ValueError("the downloaded file is not the one expected (its SHA-256 differs): it was removed")
        with zipfile.ZipFile(archive) as z:
            kept = [m for m in z.namelist() if any(m[len("pgsql/"):].startswith(k) for k in KEEP_MEMBERS) and not m.endswith("/")]
            if not kept:
                raise ValueError("the archive holds none of the parts expected (bin, lib, share, include)")
            for member in kept:
                rel = member[len("pgsql/"):]
                dest = folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    finally:
        archive.unlink(missing_ok=True)
    target = folder / "bin" / EXE
    if not target.is_file():
        raise ValueError("the archive holds no PostgreSQL server program")
    if os.name != "nt":
        for p in (folder / "bin").glob("*"):
            if p.is_file():
                p.chmod(0o755)
    return target


def install_from(folder: Path, exe: Path) -> Path:
    """Copies an existing PostgreSQL installation's `bin`, `lib`, `share` and `include` into `folder` (our own copy), from the installation that `exe` (its
    `postgres.exe`, found by `detect()`) belongs to. Used when the person points at one already on this PC instead of downloading TaDaMoo's own."""
    src_root = Path(exe).parent.parent
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("bin", "lib", "share", "include"):
        src = src_root / name
        if src.is_dir():
            shutil.copytree(src, folder / name, dirs_exist_ok=True)
    target = folder / "bin" / EXE
    if not target.is_file():
        raise FileNotFoundError(f"there is no {EXE} under {src_root}")
    return target


def new_password() -> str:
    """A password for our own copy's superuser, made here and never shown in a file: the secret goes to the vault."""
    return secrets.token_urlsafe(24)


class PostgresService:
    """Our own PostgreSQL: its own data directory (made with `initdb` the first time), started on 127.0.0.1 with its own port, stopped with `pg_ctl`. `runner(argv, **kw)`
    runs a tool of `bin_dir` and returns something with `.returncode` and `.stdout`/`.stderr`; the default runs a real one with `subprocess.run`."""

    def __init__(self, bin_dir: Path, data: Path, port: int, password: str, user: str = LOCAL_USER, runner: Callable[..., Any] = subprocess.run) -> None:
        if port in FORBIDDEN_PORTS:
            raise ValueError("that port is never used")
        self.bin_dir, self.data, self.port, self.user, self._password = Path(bin_dir), Path(data), port, user, password
        self._runner = runner

    @property
    def endpoint(self) -> str:
        return f"127.0.0.1:{self.port}"

    def _tool(self, name: str) -> str:
        exe = name + (".exe" if os.name == "nt" else "")
        return str(self.bin_dir / exe)

    def _run(self, argv: List[str], timeout: float) -> Any:
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return self._runner(argv, capture_output=True, text=True, timeout=timeout, **kw)

    def initialized(self) -> bool:
        return (self.data / "PG_VERSION").is_file()

    def init_db(self) -> None:
        """Makes the data directory: UTF-8, no locale dependency, the superuser `user` with the password given to this instance (never written to a file once this
        returns), local connections over TCP only and scram-sha-256 authentication."""
        self.data.mkdir(parents=True, exist_ok=True)
        pwfile = self.data.with_name(self.data.name + ".pwtmp")
        pwfile.write_text(self._password, encoding="utf-8")
        try:
            run = self._run([self._tool("initdb"), "-D", str(self.data), "-U", self.user, "--pwfile", str(pwfile), "-A", "scram-sha-256", "-E", "UTF8", "--no-locale"], 120.0)
            if run.returncode != 0:
                raise RuntimeError("initdb: " + ((run.stderr or run.stdout or "").strip()[-400:]))
        finally:
            pwfile.unlink(missing_ok=True)
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as f:
            f.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\n")
        with (self.data / "pg_hba.conf").open("a", encoding="utf-8") as f:
            f.write("\nhost all all 127.0.0.1/32 scram-sha-256\n")

    def running(self) -> bool:
        if not self.initialized():
            return False
        return self._run([self._tool("pg_ctl"), "status", "-D", str(self.data)], 15.0).returncode == 0

    def start(self) -> str:
        if not self.initialized():
            self.init_db()
        if self.running():
            return self.endpoint
        run = self._run([self._tool("pg_ctl"), "start", "-D", str(self.data), "-w", "-t", "30", "-l", str(self.data / "server.log")], 40.0)
        if run.returncode != 0:
            raise RuntimeError("pg_ctl start: " + ((run.stderr or run.stdout or "").strip()[-400:]))
        return self.endpoint

    def stop(self) -> None:
        if not self.initialized():
            return
        try:
            self._run([self._tool("pg_ctl"), "stop", "-D", str(self.data), "-m", "fast", "-w", "-t", "20"], 25.0)
        except Exception:                                      # already gone, or refuses to answer: not this call's problem
            pass

# Pointing a project's archives at this server is not this module's job: a project's database is configured through tadamoo.rag.backends (clean_config,
# Router.set_config, store_password), the same way for any PostgreSQL, ours or one the person already had. This module only gets a server running.
