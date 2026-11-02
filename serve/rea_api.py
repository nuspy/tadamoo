"""Reverse engineering with REA (the rea-agents add-on): the switch in the Agent tab, the programs REA can use and their installers.

Turning it on registers REA's MCP server (the add-on's Node.js running `rea.mjs mcp`) and copies REA's skill into Basalt's own
capability store, where both are born approved. Turning it off removes exactly those two things. `rea setup` is never run: it
rewrites the configuration of other coding agents. A program is installed only when the person presses its button, with the
command shown on the page; the command comes from the table below, never from the request.
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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from serve import addons, userdata

MCP_NAME = "rea"
MCP_ID = f"mcp:{MCP_NAME}"
SOURCE = "rea-addon"
SKILL_NAME = "reverse-engineer-anything"
MARKER = ".basalt-rea"
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


HOST = Host()


def _winget(package: str) -> List[str]:
    return ["winget", "install", "--id", package, "--exact", "--accept-package-agreements", "--accept-source-agreements"]


def _python() -> str:
    return HOST.which("python") or HOST.which("py") or ""


# Each tool: what it is for, the variable REA reads, how to find it, and the one command that installs it (none: install it by hand).
# `find` is "file" (the variable is the program) or "dir" (the variable is a folder that holds `marker`).
TOOLS: List[Dict[str, Any]] = [
    {"id": "ghidra", "env": "GHIDRA_INSTALL_DIR", "find": "dir", "marker": ["ghidraRun.bat", "ghidraRun"], "which": [],
     "globs": [r"C:\Tools\Ghidra*\ghidra_*", r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\NationalSecurityAgency.Ghidra*\ghidra_*",
               r"%ProgramFiles%\ghidra_*", r"%USERPROFILE%\ghidra_*", "/opt/ghidra*", "/usr/share/ghidra"],
     "winget": "NationalSecurityAgency.Ghidra", "link": "https://ghidra-sre.org", "needs": ["jdk"]},
    {"id": "jdk", "env": "JAVA_HOME", "find": "dir", "marker": ["bin/java.exe", "bin/java"], "which": [],
     "globs": [r"C:\Tools\Java*\jdk*", r"%ProgramFiles%\Microsoft\jdk-*", r"%ProgramFiles%\Eclipse Adoptium\jdk-*", r"%ProgramFiles%\Java\jdk*",
               "/usr/lib/jvm/*"],
     "winget": "Microsoft.OpenJDK.21", "link": "https://learn.microsoft.com/java/openjdk/download"},
    {"id": "jadx", "env": "REA_JADX_MCP_JAR", "find": "file", "which": [], "globs": ["{tools}/jadx-headless-mcp*.jar"],
     "winget": None, "link": "https://github.com/skylot/jadx", "needs": ["jdk"]},
    {"id": "apktool", "env": "REA_APKTOOL_COMMAND", "find": "file", "which": ["apktool", "apktool.bat"], "globs": [], "winget": None,
     "link": "https://apktool.org", "needs": ["jdk"], "version": ["--version"]},
    {"id": "adb", "env": "REA_ADB_PATH", "find": "file", "which": ["adb"], "globs": [r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"],
     "winget": "Google.PlatformTools", "link": "https://developer.android.com/tools/releases/platform-tools", "version": ["version"]},
    {"id": "chrome", "env": "REA_BROWSER_EXECUTABLE", "find": "file", "which": ["chrome", "google-chrome", "chromium"],
     "globs": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
               r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"],
     "winget": "Google.Chrome", "link": "https://www.google.com/chrome"},
    {"id": "tshark", "env": "REA_TSHARK_COMMAND", "find": "file", "which": ["tshark"], "globs": [r"%ProgramFiles%\Wireshark\tshark.exe"],
     "winget": "WiresharkFoundation.Wireshark", "link": "https://www.wireshark.org", "version": ["--version"]},
    {"id": "wabt", "env": "REA_WABT_BIN_DIRECTORY", "find": "dir", "marker": ["wasm-validate.exe", "wasm-validate"], "which": [],
     "globs": [r"C:\Tools\wabt*\bin", r"C:\Tools\wabt*", "{tools}/wabt*/bin"],
     "winget": None, "link": "https://github.com/WebAssembly/wabt/releases"},
    {"id": "unblob", "env": "REA_BINWALK_COMMAND", "find": "file", "which": ["unblob", "binwalk"], "globs": [], "winget": None,
     "pip": "unblob", "link": "https://unblob.org", "version": ["--version"]},
    {"id": "pwntools", "env": "REA_PWNTOOLS_PYTHON", "find": "python", "which": [], "globs": [], "winget": None, "pip": "pwntools",
     "link": "https://docs.pwntools.com"},
    {"id": "rizin", "env": "REA_RIZIN_COMMAND", "find": "file", "which": ["rizin"], "globs": [r"C:\Tools\rizin*\bin\rizin.exe"],
     "winget": None, "link": "https://rizin.re", "version": ["-v"]},
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


def install_command(tool: Dict[str, Any]) -> List[str]:
    """The command that installs a tool on this system; empty when it has to be installed by hand."""
    if tool.get("pip"):
        py = _python()
        return [py, "-m", "pip", "install", "--user", tool["pip"]] if py else []
    if tool.get("winget") and HOST.platform == "win32" and HOST.which("winget"):
        return _winget(tool["winget"])
    return []


def tool_view(tool: Dict[str, Any]) -> Dict[str, Any]:
    path = _detect_path(tool)
    return {"id": tool["id"], "env": tool["env"], "detected": bool(path), "path": path, "version": _version_of(tool, path) if path else "",
            "command": install_command(tool), "link": tool["link"], "needs": tool.get("needs", [])}


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
        raise ReaError("The REA add-on is not installed with this copy of Basalt.", "no_addon", 404)
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
        (target / MARKER).write_text("added by Basalt for the REA switch; removed when it is turned off\n", encoding="utf-8")


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
            "tools": views if views is not None else tools_view()}


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


def _install_one(tool: Dict[str, Any]) -> Dict[str, Any]:
    argv = install_command(tool)
    if not argv:
        return {"tool": tool["id"], "command": [], "code": None, "output": "", "skipped": "manual"}
    code, out = HOST.run(argv, INSTALL_TIMEOUT)
    return {"tool": tool["id"], "command": argv, "code": code, "output": _tail(out)}


def install(tool_id: Any = None, everything: bool = False) -> Dict[str, Any]:
    """Runs the install command of one tool, or of every missing tool that has one. Only on the person's click."""
    if everything:
        todo = [t for t in TOOLS if not _detect_path(t) and install_command(t)]
    else:
        if not isinstance(tool_id, str) or tool_id not in BY_ID:
            raise ReaError("That program is not one REA uses.", "unknown_tool")
        todo = [BY_ID[tool_id]]
    if not _install_lock.acquire(blocking=False):
        raise ReaError("An installation is already running.", "busy", 409)
    try:
        results = [_install_one(t) for t in todo]
    finally:
        _install_lock.release()
    views = tools_view()
    with _state_lock:
        if read_state()["enabled"] and _record_ours() is not None:
            register(views)                                      # the record learns where the new programs are
    return {"results": results, "status": status(views)}


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
