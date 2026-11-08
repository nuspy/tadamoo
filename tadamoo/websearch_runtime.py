"""The programs the local web search servers need, installed without Docker and without administrator rights, for the system the harness runs on.

Everything lives in one folder of the user data (`websearch/`): a Node.js runtime (the PC's own when it is recent enough, else the official archive for this
system, checked against the SHA-256 list that nodejs.org publishes) and, for each MCP server, an npm package installed with its install scripts switched off.
A server is a child process started the first time a search or a page read needs it and stopped after a quiet period and when the harness ends; the person
never starts or stops it. Nothing here changes the PATH, the registry or any setting of the PC."""
from __future__ import annotations

import atexit
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

NODE_INDEX = "https://nodejs.org/dist/index.json"
NODE_BASE = "https://nodejs.org/dist/"
MIN_NODE = 18
IDLE_SECONDS = 300.0
DOWNLOAD_SECONDS = 120.0
NPM_SECONDS = 900.0
MAX_ARCHIVE = 400 << 20
# (system, architecture) -> (name inside the file name, archive type) of the official Node.js builds
NODE_BUILDS: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("windows", "x64"): ("win-x64", "zip"), ("windows", "arm64"): ("win-arm64", "zip"),
    ("linux", "x64"): ("linux-x64", "tar.xz"), ("linux", "arm64"): ("linux-arm64", "tar.xz"),
    ("macos", "x64"): ("darwin-x64", "tar.gz"), ("macos", "arm64"): ("darwin-arm64", "tar.gz"),
}
# browsers the Playwright server can drive, by system: (channel it is told, how to tell it is installed)
BROWSERS: Dict[str, List[Tuple[str, List[str]]]] = {
    "windows": [("msedge", [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]),
                ("chrome", [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"])],
    "macos": [("chrome", ["/Applications/Google Chrome.app"]), ("msedge", ["/Applications/Microsoft Edge.app"])],
    "linux": [("chrome", ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome"]),
              ("msedge", ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"])],
}

Say = Callable[[str], None]


class RuntimeProblem(RuntimeError):
    """What stopped an install or a launch, in words for the person."""


def system() -> Dict[str, Any]:
    """The system the harness runs on, as the installers tell it apart: windows, linux or macos, and x64 or arm64."""
    name = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "x64" if machine in ("amd64", "x86_64", "x64") else "arm64" if machine in ("arm64", "aarch64") else machine
    note = ""
    supported = (name, arch) in NODE_BUILDS
    if not supported:
        note = f"There is no official Node.js build for {name} on {arch}."
    elif name == "linux" and glob.glob("/lib/ld-musl-*") + glob.glob("/usr/lib/ld-musl-*"):
        supported, note = False, "This Linux uses musl (for example Alpine): the official Node.js builds need glibc. Install Node.js with the system's package manager."
    return {"os": name, "arch": arch, "supported": supported, "note": note}


def _run(argv: List[str], timeout: float = 60.0, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def _fetch(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo/1"}), timeout=timeout) as resp:
        return resp.read(8_000_000)


def _download(url: str, target: Path, say: Say, timeout: float = DOWNLOAD_SECONDS) -> str:
    """The file at `url` written to `target`; returns its SHA-256. Refuses an archive larger than MAX_ARCHIVE."""
    digest, done, last = hashlib.sha256(), 0, 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo/1"}), timeout=timeout) as resp, open(target, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_ARCHIVE:
                raise RuntimeProblem("the download is larger than expected: stopped")
            digest.update(chunk)
            out.write(chunk)
            if done - last >= 10 << 20:
                last = done
                say(f"downloaded {done >> 20} MB")
    return digest.hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def extract(archive: Path, dest: Path) -> None:
    """Unpacks a zip or a tar archive under `dest`; a member that would land outside it (or a link that points outside it) stops the whole unpack."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not _inside(dest, dest / info.filename):
                    raise RuntimeProblem(f"the archive holds a path outside its folder: {info.filename}")
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for member in t.getmembers():
            target = dest / member.name
            if not _inside(dest, target) or member.isdev() or (member.issym() and not _inside(dest, (target.parent / member.linkname))) or (member.islnk() and not _inside(dest, dest / member.linkname)):
                raise RuntimeProblem(f"the archive holds a path outside its folder or a device: {member.name}")
        try:
            t.extractall(dest, filter="data")
        except TypeError:                                    # an older Python has no filter; the members were checked above
            t.extractall(dest)


def _node_paths(root: Path, os_name: str) -> Dict[str, Any]:
    if os_name == "windows":
        return {"node": root / "node.exe", "npm_cli": root / "node_modules" / "npm" / "bin" / "npm-cli.js", "bin": root}
    return {"node": root / "bin" / "node", "npm_cli": root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js", "bin": root / "bin"}


class Runtime:
    """Node.js for the MCP servers: the PC's own when it is MIN_NODE or newer, else the copy this class installs under `base/node`."""

    def __init__(self, base: Path, info: Optional[Dict[str, Any]] = None, run: Callable[..., Any] = _run, fetch: Callable[[str], bytes] = _fetch,
                 download: Callable[..., str] = _download, which: Callable[[str], Optional[str]] = shutil.which) -> None:
        self.base, self.info = Path(base), info or system()
        self._run, self._fetch, self._download, self._which = run, fetch, download, which

    def _version(self, node: str) -> Optional[Tuple[int, ...]]:
        try:
            out = self._run([node, "--version"], 10.0).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", out)
        return tuple(int(x) for x in found.groups()) if found else None

    def find(self) -> Optional[Dict[str, Any]]:
        """{node, npm (argv), bin, version, source} of a usable Node.js, or None."""
        system_node = self._which("node")
        if system_node:
            version = self._version(system_node)
            npm = self._which("npm")
            if version and version[0] >= MIN_NODE and npm:
                return {"node": system_node, "npm": [npm], "bin": str(Path(system_node).parent), "version": ".".join(map(str, version)), "source": "system"}
        try:
            current = json.loads((self.base / "node" / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        paths = _node_paths(self.base / "node" / str(current.get("dir", "")), self.info["os"])
        if not paths["node"].is_file() or not paths["npm_cli"].is_file():
            return None
        return {"node": str(paths["node"]), "npm": [str(paths["node"]), str(paths["npm_cli"])], "bin": str(paths["bin"]), "version": str(current.get("version", "")), "source": "private"}

    def env(self, found: Dict[str, Any]) -> Dict[str, str]:
        return {"PATH": found["bin"] + os.pathsep + os.environ.get("PATH", "")}

    # -- the steps of an install, as (label, detail, function) so a plan can be shown before anything runs

    def steps(self) -> List[Tuple[str, str, Callable[[Say], None]]]:
        if self.find():
            return []
        key = (self.info["os"], self.info["arch"])
        if not self.info["supported"] or key not in NODE_BUILDS:
            return [("Node.js cannot be installed here", self.info["note"] or "unsupported system", self._unsupported)]
        plat, ext = NODE_BUILDS[key]
        state: Dict[str, Any] = {}

        def pick(say: Say) -> None:
            index = json.loads(self._fetch(NODE_INDEX).decode("utf-8"))
            lts = next((r for r in index if r.get("lts")), None)
            if not lts:
                raise RuntimeProblem("nodejs.org lists no long-term release")
            state["version"] = str(lts["version"])
            state["file"] = f"node-{state['version']}-{plat}.{ext}"
            say(f"the current long-term release is {state['version']}")

        def get(say: Say) -> None:
            url = NODE_BASE + f"{state['version']}/"
            wanted = None
            for line in self._fetch(url + "SHASUMS256.txt").decode("utf-8").splitlines():
                digest, _, name = line.strip().partition("  ")
                if name == state["file"]:
                    wanted = digest.lower()
            if not wanted:
                raise RuntimeProblem(f"nodejs.org has no checksum for {state['file']}")
            target = self.base / "node" / "download" / state["file"]
            got = self._download(url + state["file"], target, say)
            if got != wanted:
                target.unlink(missing_ok=True)
                raise RuntimeProblem("the download does not match the checksum nodejs.org publishes: it was removed")
            say("the checksum matches")
            state["archive"] = target

        def unpack(say: Say) -> None:
            folder = self.base / "node" / f"{state['version']}-{plat}"
            if folder.exists():
                shutil.rmtree(folder)
            staging = self.base / "node" / "unpack"
            if staging.exists():
                shutil.rmtree(staging)
            extract(state["archive"], staging)
            inner = staging / f"node-{state['version']}-{plat}"
            if not inner.is_dir():
                raise RuntimeProblem("the archive has an unexpected layout")
            inner.rename(folder)
            shutil.rmtree(staging, ignore_errors=True)
            state["archive"].unlink(missing_ok=True)
            (self.base / "node" / "current.json").write_text(json.dumps({"version": state["version"], "dir": folder.name}), encoding="utf-8")
            say(f"installed under {folder}")

        def check(say: Say) -> None:
            found = self.find()
            if not found:
                raise RuntimeProblem("the installed Node.js does not start")
            say(f"Node.js {found['version']} answers")

        return [("Find the current Node.js release", f"GET {NODE_INDEX}", pick),
                ("Download Node.js for this system and check its checksum", f"{NODE_BASE}<version>/node-<version>-{plat}.{ext}", get),
                ("Unpack it into the harness folder", str(self.base / "node"), unpack),
                ("Check that it starts", "node --version", check)]

    @staticmethod
    def _unsupported(say: Say) -> None:
        raise RuntimeProblem("Node.js cannot be installed here")

    def remove_private(self) -> None:
        shutil.rmtree(self.base / "node", ignore_errors=True)


# -- npm packages and the servers they hold --------------------------------------------------------------------------


class Packages:
    """MCP servers installed with npm under `base/packages/<name>`, install scripts off, and the version that was installed (never "latest" at launch)."""

    def __init__(self, base: Path, runtime: Runtime, run: Callable[..., Any] = _run) -> None:
        self.base, self.runtime, self._run = Path(base), runtime, run

    def folder(self, package: str) -> Path:
        return self.base / "packages" / re.sub(r"[^A-Za-z0-9._-]+", "_", package)

    def _manifest(self, package: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads((self.folder(package) / "node_modules" / package / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def installed(self, package: str) -> str:
        """The installed version of a package, or empty."""
        manifest = self._manifest(package)
        return str((manifest or {}).get("version") or "")

    def entry(self, package: str) -> Optional[Path]:
        """The script that starts the server of an installed package (its `bin`)."""
        manifest = self._manifest(package)
        if not manifest:
            return None
        bin_field = manifest.get("bin")
        rel = bin_field if isinstance(bin_field, str) else next(iter(bin_field.values()), None) if isinstance(bin_field, dict) else manifest.get("main")
        path = (self.folder(package) / "node_modules" / package / str(rel)) if rel else None
        return path if path and path.is_file() else None

    def command(self, package: str, *args: str) -> Tuple[List[str], Dict[str, str]]:
        """(argv, environment) that start the server of an installed package; RuntimeProblem when it or Node.js is missing."""
        found, entry = self.runtime.find(), self.entry(package)
        if not found:
            raise RuntimeProblem("Node.js is not installed: install it from Settings > Web search")
        if entry is None:
            raise RuntimeProblem(f"{package} is not installed: install it from Settings > Web search")
        return [found["node"], str(entry), *args], self.runtime.env(found)

    def steps(self, package: str) -> List[Tuple[str, str, Callable[[Say], None]]]:
        folder = self.folder(package)

        def install(say: Say) -> None:
            found = self.runtime.find()
            if not found:
                raise RuntimeProblem("Node.js is not available")
            folder.mkdir(parents=True, exist_ok=True)
            argv = [*found["npm"], "install", "--prefix", str(folder), "--no-audit", "--no-fund", "--ignore-scripts", package]
            say("$ npm install --ignore-scripts " + package)
            done = self._run(argv, NPM_SECONDS, {**{k: v for k, v in os.environ.items()}, **self.runtime.env(found)})
            for line in ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-6:]:
                say(line)
            if done.returncode != 0:
                raise RuntimeProblem(f"npm could not install {package} (exit {done.returncode})")

        def check(say: Say) -> None:
            version = self.installed(package)
            if not version or self.entry(package) is None:
                raise RuntimeProblem(f"{package} was installed but has no program to start")
            say(f"{package} {version} is installed")

        return [(f"Install the {package} server with npm, install scripts off", f"npm install --prefix {folder} --ignore-scripts {package}", install),
                ("Check that it can be started", f"{package} {self.installed(package) or '<version>'}", check)]

    def remove(self, package: str) -> None:
        shutil.rmtree(self.folder(package), ignore_errors=True)


def browser_channel(os_name: str, exists: Callable[[str], bool] = os.path.exists) -> str:
    """The browser the Playwright server is told to drive on this system: an installed Edge or Chrome, else its own Chromium (which it must download)."""
    for channel, paths in BROWSERS.get(os_name, []):
        if any(exists(p) for p in paths):
            return channel
    return "chromium"


# -- a server that starts when it is needed and stops when it is not ---------------------------------------------------


_servers: List["ManagedServer"] = []
_servers_lock = threading.Lock()


class ManagedServer:
    """One MCP server as a child process: started by the first call, stopped after `idle` quiet seconds and when the harness exits."""

    def __init__(self, name: str, launch: Callable[[], Tuple[List[str], Dict[str, str]]], idle: float = IDLE_SECONDS,
                 make_client: Optional[Callable[[List[str], Dict[str, str]], Any]] = None) -> None:
        self.name, self._launch, self.idle = name, launch, idle
        self._make = make_client or self._default_client
        self._client: Any = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.RLock()
        with _servers_lock:
            _servers.append(self)

    @staticmethod
    def _default_client(argv: List[str], env: Dict[str, str]) -> Any:
        from tadamoo.mcp_client import MCPClient
        client = MCPClient(argv, env=env)
        client.start()
        client.initialize()
        return client

    @property
    def running(self) -> bool:
        return self._client is not None

    def call(self, tool: str, arguments: Dict[str, Any], timeout: float = 60.0) -> str:
        with self._lock:
            try:
                if self._client is None:
                    argv, env = self._launch()
                    self._client = self._make(argv, env)
                text = self._client.call_tool(tool, arguments, timeout=timeout)
            except RuntimeProblem:
                raise
            except Exception:
                self.stop()                                  # a server that failed once is started afresh on the next call
                raise
            self._arm()
            return text

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def stop_all() -> None:
    with _servers_lock:
        servers = list(_servers)
    for server in servers:
        server.stop()


atexit.register(stop_all)
