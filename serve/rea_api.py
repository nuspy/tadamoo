"""Reverse engineering with REA (the rea-agents add-on): the switch in the Agent tab, the programs REA can use and their installers.

Turning it on registers REA's MCP server (the add-on's Node.js running `rea.mjs mcp`) and copies REA's skill into TaDaMoo's own
capability store, where both are born approved. Turning it off removes exactly those two things. `rea setup` is never run: it
rewrites the configuration of other coding agents. A program is installed only when the person presses its button, with the
command shown on the page; the command comes from the table below, never from the request. A program that is missing is installed in the order of what it needs, by the
first way that works (winget, a download checked against its SHA-256, a GitHub release, pip), each tried again when the network fails;
a program that exists only for Linux is not offered on Windows.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from serve import addons, rea_install, userdata

MCP_NAME = "rea"
MCP_ID = f"mcp:{MCP_NAME}"
SOURCE = "rea-addon"
SKILL_NAME = "reverse-engineer-anything"
MARKER = ".tadamoo-rea"
OUTPUT_KEEP = 6000
INSTALL_TIMEOUT = 1800
VERSION_TIMEOUT = 5

_install_lock = threading.Lock()
_state_lock = threading.RLock()


class ReaError(Exception):
    def __init__(self, message: str, code: str = "rea", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class Host:
    """What the module asks of the machine; tests replace it."""
    platform = sys.platform

    def environ(self) -> Dict[str, str]:
        return dict(os.environ)

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def glob(self, pattern: str) -> List[str]:
        return sorted(glob.glob(os.path.expandvars(pattern)))

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def run(self, argv: List[str], timeout: int) -> Tuple[int, str]:
        """Runs a command with no console window and no input; returns its exit code and its output (stdout and stderr together)."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.platform == "win32" else 0
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=flags, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            return 124, out + "\n(stopped: it took longer than the time allowed)"
        except OSError as exc:
            return 127, str(exc)
        return done.returncode, done.stdout or ""

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def fetch(self, url: str, dest: Path) -> str:
        """Downloads `url` to `dest` and returns its SHA-256; OSError when the network fails."""
        import hashlib
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo"}), timeout=60) as r, open(dest, "wb") as out:
                while chunk := r.read(1 << 20):
                    digest.update(chunk)
                    out.write(chunk)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise OSError(str(getattr(exc, "reason", exc))) from None
        return digest.hexdigest()

    def http_json(self, url: str) -> Any:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "TaDaMoo", "Accept": "application/vnd.github+json"}), timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
            raise OSError(str(getattr(exc, "reason", exc))) from None

    def find_in_path(self, pattern: str) -> List[str]:
        """Files whose name matches `pattern` in the folders of the PATH."""
        found: List[str] = []
        for folder in self.environ().get("PATH", "").split(os.pathsep):
            if folder:
                found += glob.glob(os.path.join(folder, pattern))
        return sorted(found)

    def refresh_path(self) -> None:
        """What an installer added to the PATH reaches this process, which started before it."""
        if self.platform != "win32":
            return
        try:
            import winreg
        except ImportError:
            return
        added: List[str] = []
        for hive, sub_key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"), (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, sub_key) as key:
                    added += [os.path.expandvars(p) for p in winreg.QueryValueEx(key, "Path")[0].split(";") if p]
            except OSError:
                continue
        have = os.environ.get("PATH", "").split(os.pathsep)
        os.environ["PATH"] = os.pathsep.join(have + [p for p in added if p not in have])


HOST = Host()


def _python() -> str:
    return HOST.which("python") or HOST.which("py") or ""


# The release of the JADX headless MCP JAR that REA audited (REA never installs it itself): its address and its SHA-256, read from the add-on when it is there.
JADX_URL = "https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/jadx-headless-mcp-0.7.1-all.jar"
JADX_SHA256 = "6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75"


def _jadx_ways() -> List[Dict[str, Any]]:
    url, sha = JADX_URL, JADX_SHA256
    release = addons.npm_entry("rea-agents", "dist/android/JadxRelease.js")
    if release:
        try:
            text = release.read_text(encoding="utf-8")
            found_url, found_sha = re.search(r'url:\s*"(https://github\.com/[^"]+\.jar)"', text), re.search(r'sha256:\s*"([0-9a-f]{64})"', text)
            if found_url and found_sha:
                url, sha = found_url.group(1), found_sha.group(1)
        except OSError:
            pass
    return [{"how": "download", "url": url, "sha256": sha, "to": "{tools}/" + url.rsplit("/", 1)[-1]}]


def _github(repo: str, asset: str, to: str, tag: str = "", **more: Any) -> Dict[str, Any]:
    return {"how": "github", "repo": repo, "asset": asset, "to": to, "tag": tag, **more}


# Each tool: what it is for, the variable REA reads, how to find it, the ways to install it in the order to try them (`methods`; none: by hand),
# the systems it exists for (`platforms`; none listed: all) and what it needs installed first.
# `find` is "file" (the variable is the program) or "dir" (the variable is a folder that holds `marker`); `which_dir` finds a folder
# from a program on the PATH: [names, how many levels up].
TOOLS: List[Dict[str, Any]] = [
    {"id": "ghidra", "env": "GHIDRA_INSTALL_DIR", "find": "dir", "marker": ["ghidraRun.bat", "ghidraRun"], "which": [],
     "globs": [r"C:\Tools\Ghidra*\ghidra_*", r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\NationalSecurityAgency.Ghidra*\ghidra_*",
               r"%ProgramFiles%\ghidra_*", r"%USERPROFILE%\ghidra_*", "/opt/ghidra*", "/usr/share/ghidra"],
     "methods": [{"how": "winget", "id": "NationalSecurityAgency.Ghidra"}], "link": "https://ghidra-sre.org", "needs": ["jdk"]},
    {"id": "jdk", "env": "JAVA_HOME", "find": "dir", "marker": ["bin/java.exe", "bin/java"], "which": [], "which_dir": [["java"], 2],
     "globs": [r"C:\Tools\Java*\jdk*", r"%ProgramFiles%\Microsoft\jdk-*", r"%ProgramFiles%\Eclipse Adoptium\jdk-*", r"%ProgramFiles%\Java\jdk*",
               r"%USERPROFILE%\scoop\apps\*jdk*\current", "/usr/lib/jvm/*"],
     "methods": [{"how": "winget", "id": "Microsoft.OpenJDK.21"}, {"how": "winget", "id": "EclipseAdoptium.Temurin.21.JDK"}],
     "link": "https://learn.microsoft.com/java/openjdk/download"},
    {"id": "jadx", "env": "REA_JADX_MCP_JAR", "find": "file", "which": [], "globs": ["{tools}/jadx-headless-mcp*.jar"],
     "methods": _jadx_ways, "link": "https://github.com/1013503897/jadx-headless-mcp", "needs": ["jdk"]},
    {"id": "apktool", "env": "REA_APKTOOL_COMMAND", "find": "file", "which": ["apktool", "apktool.bat", "apktool.cmd"],
     "globs": ["{tools}/apktool/apktool.bat", r"C:\Tools\apktool*\apktool*.bat", r"C:\Tools\apktool*\apktool*.jar", r"%USERPROFILE%\scoop\shims\apktool*",
               r"%USERPROFILE%\scoop\apps\apktool\current\apktool*", r"%ProgramData%\chocolatey\bin\apktool*", r"%ProgramFiles%\apktool\apktool*",
               r"%USERPROFILE%\apktool\apktool*", "/usr/local/bin/apktool", "/usr/bin/apktool"],
     "methods": [{"how": "launcher", "jar": "apktool*.jar", "to": "{tools}/apktool/apktool.bat", "only": ["win32"]},
                 _github("iBotPeaches/Apktool", r"^apktool_[\d.]+\.jar$", "{tools}/apktool", kind="jar", name="apktool.jar", wrapper="apktool", only=["win32"])],
     "link": "https://apktool.org", "needs": ["jdk"], "version": ["--version"]},
    {"id": "adb", "env": "REA_ADB_PATH", "find": "file", "which": ["adb"],
     "globs": [r"%ANDROID_HOME%\platform-tools\adb.exe", r"%ANDROID_SDK_ROOT%\platform-tools\adb.exe", r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"],
     "methods": [{"how": "winget", "id": "Google.PlatformTools"}], "link": "https://developer.android.com/tools/releases/platform-tools", "version": ["version"]},
    {"id": "chrome", "env": "REA_BROWSER_EXECUTABLE", "find": "file", "which": ["chrome", "google-chrome", "chromium"],
     "globs": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
               r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
     "methods": [{"how": "winget", "id": "Google.Chrome"}], "link": "https://www.google.com/chrome"},
    {"id": "tshark", "env": "REA_TSHARK_COMMAND", "find": "file", "which": ["tshark"], "globs": [r"%ProgramFiles%\Wireshark\tshark.exe"],
     "methods": [{"how": "winget", "id": "WiresharkFoundation.Wireshark"}], "link": "https://www.wireshark.org", "version": ["--version"]},
    {"id": "wabt", "env": "REA_WABT_BIN_DIRECTORY", "find": "dir", "marker": ["wasm-validate.exe", "wasm-validate"], "which": [], "which_dir": [["wasm-validate"], 1],
     "globs": [r"C:\Tools\wabt*\bin", r"C:\Tools\wabt*", "{tools}/wabt*/bin", "{tools}/wabt*", r"%USERPROFILE%\scoop\apps\wabt\current\bin",
               r"%ProgramFiles%\wabt*\bin"],
     "methods": [_github("WebAssembly/wabt", r"windows-x64\.tar\.gz$", "{tools}/wabt-1.0.42", tag="1.0.42", kind="archive", only=["win32"]),
                 _github("WebAssembly/wabt", r"windows-x64\.tar\.gz$", "{tools}/wabt-latest", kind="archive", only=["win32"])],
     "link": "https://github.com/WebAssembly/wabt/releases"},
    {"id": "unblob", "env": "REA_BINWALK_COMMAND", "find": "file", "which": ["unblob", "binwalk"], "globs": [], "platforms": ["linux"],
     "methods": [{"how": "pip", "package": "unblob"}], "link": "https://unblob.org", "version": ["--version"]},
    {"id": "pwntools", "env": "REA_PWNTOOLS_PYTHON", "find": "python", "which": [], "globs": [], "methods": [{"how": "pip", "package": "pwntools"}],
     "link": "https://docs.pwntools.com"},
    {"id": "rizin", "env": "REA_RIZIN_COMMAND", "find": "file", "which": ["rizin"],
     "globs": [r"C:\Tools\rizin*\bin\rizin.exe", "{tools}/rizin/bin/rizin.exe", "{tools}/rizin*/bin/rizin.exe", r"%ProgramFiles%\rizin\bin\rizin.exe",
               r"%ProgramFiles%\Rizin\bin\rizin.exe", r"%LOCALAPPDATA%\Programs\rizin\bin\rizin.exe",
               r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rizin.Rizin*\bin\rizin.exe", r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Rizin.Rizin*\*\bin\rizin.exe"],
     "methods": [{"how": "winget", "id": "Rizin.Rizin"},
                 _github("rizinorg/rizin", r"^rizin-windows-shared64-v[\d.]+\.zip$", "{tools}/rizin", kind="archive", only=["win32"])],
     "link": "https://rizin.re", "version": ["-v"]},
]
BY_ID = {t["id"]: t for t in TOOLS}


def _marker_in(folder: str, markers: List[str]) -> bool:
    return any(HOST.exists(os.path.join(folder, *m.split("/"))) for m in markers)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _version_of(tool: Dict[str, Any], path: str) -> str:
    name = Path(path).name
    if m := re.search(r"(?:ghidra_|jdk-|jdk|wabt-?)(\d+(?:[._]\d+)*(?:\+\d+)?)", name):
        return m.group(1).replace("_", ".")
    argv = tool.get("version")
    if argv and tool["find"] == "file":
        code, out = HOST.run([path, *argv], VERSION_TIMEOUT)
        return _first_line(out) if code == 0 else ""
    return ""


def _patterns(tool: Dict[str, Any]) -> List[str]:
    """The places to look; {tools} is the folder where the person puts programs that have no installer."""
    return [p.replace("{tools}", str(userdata.path("rea-tools"))) for p in tool["globs"]]


def _detect_path(tool: Dict[str, Any]) -> str:
    env = HOST.environ()
    given = env.get(tool["env"], "")
    if tool["find"] == "python":
        py = given or _python()
        return py if py and _has_module(py, "pwn") else ""
    if tool["find"] == "dir":
        if given and _marker_in(given, tool["marker"]):
            return given
        names, up = tool.get("which_dir", [[], 0])
        for name in names:
            if found := HOST.which(name):
                folder = os.path.dirname(os.path.realpath(found)) if up else ""
                for _ in range(max(up - 1, 0)):
                    folder = os.path.dirname(folder)
                if folder and _marker_in(folder, tool["marker"]):
                    return folder
        for pattern in _patterns(tool):
            for folder in reversed(HOST.glob(pattern)):
                if _marker_in(folder, tool["marker"]):
                    return folder
        return ""
    if given and HOST.exists(given):
        return given
    for name in tool["which"]:
        if found := HOST.which(name):
            return found
    for pattern in _patterns(tool):
        for hit in reversed(HOST.glob(pattern)):
            if HOST.exists(hit):
                return hit
    return ""


def _has_module(python: str, module: str) -> bool:
    probe = f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('{module}') else 1)"
    code, _ = HOST.run([python, "-c", probe], VERSION_TIMEOUT)
    return code == 0


def _tools_dir() -> str:
    return str(userdata.path("rea-tools"))


def _on(tool: Dict[str, Any]) -> bool:
    """True when the program exists for this system."""
    only = tool.get("platforms")
    return not only or any(HOST.platform.startswith(p) for p in only)


def _ways(tool: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ways to install this program on this system, in the order to try them."""
    if not _on(tool):
        return []
    methods = tool.get("methods") or []
    methods = methods() if callable(methods) else methods
    ways = rea_install.applicable(methods, HOST.platform, HOST.which, _python())
    return [w for w in ways if w["how"] != "launcher" or HOST.find_in_path(w["jar"])]       # a launcher needs the jar to be there already


def install_command(tool: Dict[str, Any]) -> List[str]:
    """The command of the first way that is a command (winget or pip); empty when there is none."""
    for way in _ways(tool):
        if argv := rea_install.command(way, _python()):
            return argv
    return []


def tool_view(tool: Dict[str, Any]) -> Dict[str, Any]:
    path = _detect_path(tool)
    ways = _ways(tool)
    return {"id": tool["id"], "env": tool["env"], "detected": bool(path), "path": path, "version": _version_of(tool, path) if path else "",
            "command": install_command(tool), "steps": [rea_install.describe(w, _tools_dir()) for w in ways], "installable": bool(ways),
            "supported": _on(tool), "only": tool.get("platforms", []), "link": tool["link"], "needs": tool.get("needs", [])}


def tools_view() -> List[Dict[str, Any]]:
    return [tool_view(t) for t in TOOLS]


def _env_for_record(views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from tadamoo.discovery.installed import env_spec
    return env_spec([{"name": v["env"], "value": v["path"]} for v in views if v["detected"]])


# -- the state kept, and what was added ----------------------------------------------------------------------------

def _state_file() -> Path:
    return userdata.path("rea.json")


def read_state() -> Dict[str, Any]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": False}
    return {"enabled": bool(data.get("enabled"))} if isinstance(data, dict) else {"enabled": False}


def _write_state(enabled: bool) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    tmp.replace(path)


def _capdir() -> Path:
    return userdata.path("capabilities")


def _skill_dir() -> Path:
    return _capdir() / "skills" / SKILL_NAME


def _addon() -> Dict[str, Any]:
    node, rea = addons.node_exe(), addons.npm_entry("rea-agents", "scripts/rea.mjs")
    skill = addons.npm_entry("rea-agents", f"skills/{SKILL_NAME}/SKILL.md")
    return {"node": node, "rea": rea, "skill": skill.parent if skill else None}


def _record_ours() -> Optional[Dict[str, Any]]:
    from tadamoo.discovery.installed import InstalledStore
    rec = InstalledStore(_capdir()).get(MCP_ID)
    return rec if rec and rec.get("source") == SOURCE else None


def _skill_ours() -> bool:
    return (_skill_dir() / MARKER).is_file()


def register(views: Optional[List[Dict[str, Any]]] = None) -> None:
    """Puts REA's server and skill in the capability store (or brings them up to date). Raises ReaError when it cannot."""
    from tadamoo.discovery.installed import InstalledStore
    add = _addon()
    if not (add["node"] and add["rea"]):
        raise ReaError("The REA add-on is not installed with this copy of TaDaMoo.", "no_addon", 404)
    store = InstalledStore(_capdir())
    existing = store.get(MCP_ID)
    if existing is not None and existing.get("source") != SOURCE:
        raise ReaError("A capability called rea is already on this PC; remove it first.", "name_taken", 409)
    if _skill_dir().exists() and not _skill_ours():
        raise ReaError(f"A skill folder called {SKILL_NAME} is already on this PC; remove it first.", "name_taken", 409)
    views = views if views is not None else tools_view()
    version = str(addons.info().get("rea-agents", {}).get("version", ""))
    from datetime import datetime
    store.put({"id": MCP_ID, "name": MCP_NAME, "kind": "mcp", "source": SOURCE, "route": "local", "version": version,
               "command": [str(add["node"]), str(add["rea"]), "mcp"], "env": _env_for_record(views),
               "description": "REA: reverse engineering of programs, apps, firmware and traffic (shipped add-on rea-agents).",
               "installed_at": datetime.now().isoformat(timespec="seconds")})
    if add["skill"]:
        target = _skill_dir()
        shutil.copytree(add["skill"], target, dirs_exist_ok=True)
        (target / MARKER).write_text("added by TaDaMoo for the REA switch; removed when it is turned off\n", encoding="utf-8")


def unregister() -> None:
    """Removes the server and the skill that `register` added, and nothing else."""
    from tadamoo.discovery.installed import InstalledStore
    if _record_ours() is not None:
        InstalledStore(_capdir()).delete(MCP_ID)
    if _skill_ours():
        shutil.rmtree(_skill_dir(), ignore_errors=True)


# -- what the page asks for -----------------------------------------------------------------------------------------

def status(views: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    add = _addon()
    node_version = ""
    if add["node"]:
        code, out = HOST.run([str(add["node"]), "--version"], VERSION_TIMEOUT)
        node_version = _first_line(out) if code == 0 else ""
    rec = _record_ours()
    return {"enabled": read_state()["enabled"], "platform": HOST.platform,
            "addon": {"present": bool(add["node"] and add["rea"]), "node": str(add["node"] or ""), "node_version": node_version,
                      "rea": str(add["rea"] or ""), "version": str(addons.info().get("rea-agents", {}).get("version", ""))},
            "mcp": {"registered": rec is not None, "command": rec.get("command", []) if rec else []},
            "skills": {"imported": _skill_ours(), "names": [SKILL_NAME] if _skill_ours() else []},
            "tools": views if views is not None else tools_view(), "install": job_view()}


def enable() -> Dict[str, Any]:
    with _state_lock:
        views = tools_view()
        register(views)
        _write_state(True)
        return status(views)


def disable() -> Dict[str, Any]:
    with _state_lock:
        unregister()
        _write_state(False)
        return status()


def reapply() -> bool:
    """At server start: when the switch is on, registers again (the add-on may have been updated or moved). True if it did."""
    with _state_lock:
        if not read_state()["enabled"]:
            return False
        register()
        return True


def start_if_enabled(refresh: Optional[Callable[[], Any]] = None) -> None:
    try:
        if reapply() and refresh is not None:
            refresh()
    except Exception:                                            # a missing add-on must not stop the server
        pass


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_KEEP else "..." + text[-OUTPUT_KEEP:]


_job: Dict[str, Any] = {"running": False, "plan": [], "current": "", "results": [], "started": 0.0, "finished": 0.0}
_job_thread: Optional[threading.Thread] = None


def job_view() -> Dict[str, Any]:
    with _state_lock:
        return {**_job, "plan": list(_job["plan"]), "results": [dict(r) for r in _job["results"]]}


def _installed_now(tool: Dict[str, Any]) -> bool:
    HOST.refresh_path()
    return bool(_detect_path(tool))


def _install_tool(tool: Dict[str, Any], failed: set) -> Dict[str, Any]:
    res: Dict[str, Any] = {"tool": tool["id"], "ok": False, "code": None, "command": [], "output": "", "attempts": 0, "method": "", "skipped": "", "log": []}
    if _detect_path(tool):
        return {**res, "ok": True, "skipped": "present"}
    if not _on(tool):
        return {**res, "skipped": "platform"}
    blocked = [n for n in tool.get("needs", []) if n in failed or not _detect_path(BY_ID[n])]
    if blocked:
        return {**res, "skipped": "needs", "output": ", ".join(blocked)}
    ways = _ways(tool)
    if not ways:
        return {**res, "skipped": "manual"}
    delays = rea_install.RETRY_DELAYS
    for way in ways:
        res["method"], res["command"] = way["how"], rea_install.command(way, _python())
        for attempt in range(1, len(delays) + 2):
            res["attempts"] += 1
            code, out = rea_install.run_way(HOST, way, _tools_dir(), _python())
            res["code"], res["output"] = code, _tail(out)
            res["log"].append(f"{way['how']}, try {attempt}: exit {code}")
            if code == 0 and _installed_now(tool):
                return {**res, "ok": True}
            if code == 0:
                res["output"] += "\n(the installer finished, but the program is not found yet)"
                break
            if attempt <= len(delays) and rea_install.transient(code, out):
                HOST.sleep(delays[attempt - 1])
                continue
            break
    return res


def _run_plan(plan: List[str]) -> None:
    failed: set = set()
    try:
        for tool_id in plan:
            with _state_lock:
                _job["current"] = tool_id
            res = _install_tool(BY_ID[tool_id], failed)
            if not res["ok"]:
                failed.add(tool_id)
            with _state_lock:
                _job["results"].append(res)
    finally:
        with _state_lock:
            _job.update(running=False, current="", finished=time.time())
        _install_lock.release()
        try:
            with _state_lock:
                if read_state()["enabled"] and _record_ours() is not None:
                    register(tools_view())                           # the record learns where the new programs are
        except Exception:
            pass


def install(tool_id: Any = None, everything: bool = False, wait: bool = False) -> Dict[str, Any]:
    """Installs one program, or every missing one that can be installed here, each after what it needs, in the background.
    Only on the person's click. With `wait` the call returns when it is done."""
    global _job_thread
    if everything:
        ids = [t["id"] for t in TOOLS if _on(t) and _ways(t) and not _detect_path(t)]
    else:
        if not isinstance(tool_id, str) or tool_id not in BY_ID:
            raise ReaError("That program is not one REA uses.", "unknown_tool")
        ids = [tool_id]
    try:
        plan = rea_install.order(ids, BY_ID, lambda n: not _detect_path(BY_ID[n]))
    except ValueError as exc:
        raise ReaError(str(exc), "loop") from None
    if not _install_lock.acquire(blocking=False):
        raise ReaError("An installation is already running.", "busy", 409)
    with _state_lock:
        _job.update(running=True, plan=plan, current="", results=[], started=time.time(), finished=0.0)
    _job_thread = threading.Thread(target=_run_plan, args=(plan,), name="rea-install", daemon=True)
    _job_thread.start()
    if wait:
        _job_thread.join()
    views = tools_view()
    out = {"started": True, "plan": plan, "install": job_view(), "status": status(views)}
    if wait:
        out["results"] = out["install"]["results"]
    return out


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_KEEP else "..." + text[-OUTPUT_KEEP:]


# -- routes ----------------------------------------------------------------------------------------------------------

def route_get(path: str) -> Tuple[int, Dict[str, Any]]:
    if path == "/api/rea":
        return 200, status()
    return 404, {"error": {"message": "not found"}}


def route_post(path: str, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    action = path[len("/api/rea/"):]
    try:
        if action == "enable":
            return 200, enable()
        if action == "disable":
            return 200, disable()
        if action == "install":
            return 200, install(req.get("tool"), req.get("all") is True)
    except ReaError as exc:
        return exc.status, error_body(exc)
    return 404, {"error": {"message": "not found"}}


def error_body(exc: ReaError) -> Dict[str, Any]:
    return {"error": {"type": "rea", "message": str(exc), "code": exc.code}}
