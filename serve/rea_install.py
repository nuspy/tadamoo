"""Installing the programs REA uses: the order (a program after what it needs), several ways to install each one, a few tries when the
network fails, and a check, made by the caller, that the program is found afterwards.

A way to install is one of: `winget` (a package id), `pip` (a package), `download` (a file with its SHA-256), `github` (a release asset,
checked against the digest GitHub gives, then unpacked or copied). The commands come from the table in rea_api, never from a request."""
from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

INSTALL_TIMEOUT = 1800
RETRY_DELAYS = (5, 20)                       # seconds to wait before the second and the third try of the same way
NETWORK_CODE = 6
# Output that says the network failed, so that trying again is worth it; anything else (no such package, a build error) is not retried.
TRANSIENT = re.compile(r"(?i)could not resolve|name or service not known|timed out|timeout|connection (?:reset|refused|aborted|closed)|"
                       r"temporar|network (?:error|is unreachable)|0x80072e|0x8a15|download failed|ssl|eof occurred|http error (?:5\d\d|429|408)|"
                       r"try again|server returned|unexpected end|rate limit|stopped: it took longer")
APKTOOL_BAT = ('@echo off\r\nset "JAVA_CMD=java"\r\nif defined JAVA_HOME if exist "%JAVA_HOME%\\bin\\java.exe" set "JAVA_CMD=%JAVA_HOME%\\bin\\java.exe"\r\n'
               '"%JAVA_CMD%" -jar "%~dp0apktool.jar" %*\r\n')


def transient(code: Optional[int], output: str) -> bool:
    return code in (NETWORK_CODE, 124) or bool(TRANSIENT.search(output or ""))


def order(ids: List[str], by_id: Dict[str, Dict[str, Any]], missing: Callable[[str], bool]) -> List[str]:
    """`ids` and every missing program they need, each after what it needs. ValueError for a loop in the needs."""
    seen: set = set()
    out: List[str] = []

    def visit(tool_id: str, trail: Tuple[str, ...]) -> None:
        if tool_id in trail:
            raise ValueError("circular needs: " + " -> ".join(trail + (tool_id,)))
        if tool_id in seen:
            return
        for dep in by_id[tool_id].get("needs", []):
            if dep in by_id and missing(dep):
                visit(dep, trail + (tool_id,))
        seen.add(tool_id)
        out.append(tool_id)

    for tool_id in ids:
        visit(tool_id, ())
    return out


def describe(method: Dict[str, Any], tools_dir: str) -> str:
    how = method["how"]
    if how in ("winget", "pip"):
        return " ".join(command(method, ""))
    dest = method["to"].replace("{tools}", tools_dir)
    if how == "launcher":
        return f"Make {dest}, a launcher for the jar that is already on this PC in a folder of the PATH ({method['jar']}); nothing is downloaded."
    if how == "download":
        return f"Download {method['url']} to {dest} and check its SHA-256."
    tag = method.get("tag") or "latest"
    return f"Download the {tag} release asset matching {method['asset']} from github.com/{method['repo']} and put it in {dest}."


def command(method: Dict[str, Any], python: str) -> List[str]:
    """The command of a winget or pip way (empty for the others)."""
    if method["how"] == "winget":
        argv = ["winget", "install", "--id", method["id"], "--exact", "--accept-package-agreements", "--accept-source-agreements"]
        return argv + (["--version", method["version"]] if method.get("version") else [])
    if method["how"] == "pip":
        return [python or "python", "-m", "pip", "install", "--user", *(["--only-binary", ":all:"] if method.get("binary_only") else []), method["package"]]
    return []


def applicable(methods: List[Dict[str, Any]], platform: str, which: Callable[[str], Optional[str]], python: str) -> List[Dict[str, Any]]:
    """The ways that can work on this system, in the order to try them."""
    out = []
    for m in methods:
        if m.get("only") and platform not in m["only"]:
            continue
        if m["how"] == "winget" and not (platform == "win32" and which("winget")):
            continue
        if m["how"] == "pip" and not python:
            continue
        out.append(m)
    return out


# -- the ways -------------------------------------------------------------------------------------------------------

def run_way(host: Any, method: Dict[str, Any], tools_dir: str, python: str) -> Tuple[int, str]:
    how = method["how"]
    if how in ("winget", "pip"):
        return host.run(command(method, python), INSTALL_TIMEOUT)
    try:
        if how == "launcher":
            return _launcher(host, method, tools_dir)
        if how == "download":
            return _download(host, method, tools_dir)
        return _github(host, method, tools_dir)
    except OSError as exc:
        return NETWORK_CODE, f"network error: {exc}"
    except (zipfile.BadZipFile, tarfile.TarError, ValueError, KeyError) as exc:
        return 65, f"the downloaded file could not be used: {exc}"


def _launcher(host: Any, method: Dict[str, Any], tools_dir: str) -> Tuple[int, str]:
    """A .bat that starts a jar found in a folder of the PATH: REA wants a command, and a bare jar is not one."""
    jars = host.find_in_path(method["jar"])
    if not jars:
        return 1, f"No file matching {method['jar']} was found in a folder of the PATH."
    jar = sorted(jars)[-1]
    dest = Path(method["to"].replace("{tools}", tools_dir))
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = APKTOOL_BAT.replace('"%~dp0apktool.jar"', f'"{jar}"')
    dest.write_bytes(text.encode("mbcs" if hasattr(__import__("codecs"), "mbcs_encode") else "utf-8", errors="replace"))
    return 0, f"Made {dest}\nIt starts {jar}"


def _fetch_checked(host: Any, url: str, expected: str, name: str) -> Tuple[Path, str]:
    tmp = Path(tempfile.mkdtemp(prefix="tadamoo-rea-"))
    target = tmp / name
    digest = host.fetch(url, target)
    if expected and digest.lower() != expected.lower():
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"SHA-256 mismatch: expected {expected}, got {digest}")
    return target, digest


def _download(host: Any, method: Dict[str, Any], tools_dir: str) -> Tuple[int, str]:
    dest = Path(method["to"].replace("{tools}", tools_dir))
    try:
        got, digest = _fetch_checked(host, method["url"], method.get("sha256", ""), dest.name)
    except ValueError as exc:
        return 65, str(exc)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(got), str(dest))
    shutil.rmtree(got.parent, ignore_errors=True)
    return 0, f"Downloaded {method['url']}\nSaved as {dest}\nSHA-256 {digest} checked."


def _github(host: Any, method: Dict[str, Any], tools_dir: str) -> Tuple[int, str]:
    tag = method.get("tag")
    release = host.http_json(f"https://api.github.com/repos/{method['repo']}/releases/" + (f"tags/{tag}" if tag else "latest"))
    if not isinstance(release, dict) or not release.get("assets"):
        return 1, f"No release {tag or 'latest'} of {method['repo']} was found."
    pattern = re.compile(method["asset"])
    asset = next((a for a in release["assets"] if pattern.search(a.get("name", ""))), None)
    if asset is None:
        return 1, "No file of the release matches " + method["asset"] + ": " + ", ".join(a.get("name", "") for a in release["assets"])[:400]
    digest = str(asset.get("digest") or "")
    expected = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
    try:
        got, got_digest = _fetch_checked(host, asset["browser_download_url"], expected, asset["name"])
    except ValueError as exc:
        return 65, str(exc)
    dest = Path(method["to"].replace("{tools}", tools_dir))
    try:
        if method.get("kind") == "jar":
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(got, dest / method.get("name", asset["name"]))
            if method.get("wrapper") == "apktool":
                (dest / "apktool.bat").write_bytes(APKTOOL_BAT.encode("ascii"))
        else:
            _extract(got, dest)
    finally:
        shutil.rmtree(got.parent, ignore_errors=True)
    note = "SHA-256 checked." if expected else "GitHub gave no SHA-256 for this file."
    return 0, f"Downloaded {asset['name']} ({release.get('tag_name', '')}) from github.com/{method['repo']}\nPut in {dest}\n{note}"


def _extract(archive: Path, dest: Path) -> None:
    """Unpacks a zip or a tar into `dest`; when everything is in one top folder, that folder is not repeated."""
    work = Path(tempfile.mkdtemp(prefix="tadamoo-rea-x-"))
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                _check_names(z.namelist())
                z.extractall(work)
        else:
            with tarfile.open(archive) as t:
                _check_names(t.getnames())
                t.extractall(work, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(work)
        entries = list(work.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else work
        dest.mkdir(parents=True, exist_ok=True)
        for item in root.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _check_names(names: List[str]) -> None:
    for name in names:
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"the archive holds a file outside its folder: {name}")
