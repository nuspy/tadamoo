"""Downloads and installs the candidate the person chose, and registers it.

The model never supplies a command, a package or an address: it names a candidate by the id this process gave it, the candidate
is looked up in memory (candidates.CandidateStore), every field it builds a command from is checked against a strict pattern,
and the commands below are fixed in this file.

* npm: `npm install --prefix <folder> <package>@<exact version> --ignore-scripts --no-audit --no-fund`, then the integrity npm
  recorded in the lock file is compared with the one the registry published for that release. A different hash deletes the folder
  and raises IntegrityError with both hashes; the person may install anyway.
  The server starts from the installed program, not through `npx`.
* PyPI: the wheel is downloaded alone (`--no-deps --only-binary`), its sha256 is compared with the ones PyPI lists for the
  release, then it is installed (uv if it is on the PC, else pip) into a folder of its own with `--only-binary` / `--no-build`,
  so no package's build script runs. Its dependencies come from the index without their own hash check.
* A remote server: nothing is downloaded; its address is recorded.
* A skill: only its folder, from the commit that was shown, file by file; each file's git hash is checked against the tree.

No install script of any package runs unless the person allowed scripts for that install. Registering writes the entry into the capability system as installed by Basalt; the entry's hash (the start command, the
address, or the text of the skill) is checked to be the one that was shown.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.capabilities import _read_skill, mcp_spec_sha
from harness.discovery import sources as src
from harness.discovery.candidates import Candidate, CandidateStore, safe_relative
from harness.discovery.installed import InstalledStore, env_spec
from harness.discovery.settings import DiscoverySettings
from harness.sandbox import safe_environment

INSTALL_TIMEOUT_S = 900.0
MB = 1_000_000
BOOT = ("import sys,importlib,functools;d,m,f=sys.argv[1:4];sys.argv=[f]+sys.argv[4:];sys.path.insert(0,d);"
        "sys.exit(functools.reduce(getattr,f.split('.'),importlib.import_module(m))())")


class InstallError(Exception):
    """The install did not happen; the message says why and is safe to show to the person and the model."""


class UnknownCandidate(InstallError):
    pass


class IntegrityError(InstallError):
    """What was downloaded differs from what the registry described; `got` and `expected` are the two hashes."""

    def __init__(self, message: str, got: str = "", expected: str = "") -> None:
        super().__init__(message)
        self.got, self.expected = got, expected


class AlreadyInstalled(InstallError):
    """Basalt already installed this one; an update replaces it."""


@dataclass
class Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[List[str], Optional[Path], float], Run]


def default_runner(command: List[str], cwd: Optional[Path], timeout: float) -> Run:
    """Runs one fixed command without a shell, in a cleaned environment, and gives its end and its output."""
    try:
        done = subprocess.run(command, cwd=str(cwd) if cwd else None, env=safe_environment(), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return Run(124, "", f"timed out after {int(timeout)} seconds")
    except OSError as e:
        return Run(127, "", f"{type(e).__name__}: {e}")
    return Run(done.returncode, done.stdout or "", done.stderr or "")


def _tail(run: Run) -> str:
    text = (run.stderr or run.stdout or "").strip().splitlines()
    return " | ".join(text[-4:])[:500]


def _forget(path: Path) -> None:
    def retry(func, p, _exc):                                  # a read-only file on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=retry)


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


class Installer:
    def __init__(self, root: Path, store: CandidateStore, *, index: Any = None, runner: Optional[Runner] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None, github: Optional[src.GithubClient] = None,
                 python: str = "", now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.root = Path(root)
        self.store, self.index, self.runner, self.which, self.github, self.now = store, index, runner or default_runner, which or shutil.which, github, now
        self.python = python or sys.executable
        self.records = InstalledStore(self.root)

    def reading(self, store: CandidateStore) -> "Installer":
        """The same installer, reading its candidates from `store`."""
        return Installer(self.root, store, index=self.index, runner=self.runner, which=self.which, github=self.github, python=self.python, now=self.now)

    # -- what is needed -----------------------------------------------------------------------------------------

    def missing(self, c: Candidate) -> str:
        """What the PC lacks to install this candidate, in a sentence; empty when nothing."""
        if c.route == "npm" and not self.which("npm"):
            return "npm is not installed on this PC (it comes with Node.js: https://nodejs.org). Install it, or choose another candidate."
        if c.route == "pypi":
            if not self.which("uv") and self.runner([self.python, "-m", "pip", "--version"], None, 60.0).returncode != 0:
                return "neither uv nor pip is available to install a Python package. Install uv (https://docs.astral.sh/uv/) or pip, or choose another candidate."
        if c.route == "skill" and self.github is None:
            return "the skill cannot be downloaded in this conversation."
        return ""

    # -- install ------------------------------------------------------------------------------------------------

    def install(self, candidate_id: str, *, allow_scripts: bool = False, anyway: bool = False, update: bool = False) -> Dict[str, Any]:
        """Installs a candidate. `allow_scripts` lets the package's install scripts run (npm) or a source build (PyPI); `anyway` goes on when the
        hash differs from the registry's, recording the difference; `update` replaces a version Basalt installed (the old one comes back if the new fails)."""
        c = self.store.get(candidate_id)
        if c is None:
            raise UnknownCandidate("No such candidate: only one the search just returned can be installed.")
        if bad := c.check():
            raise InstallError(f"It cannot be installed: {bad}.")
        if not c.installable:
            raise InstallError(f"It cannot be installed: {c.problem or 'it needs something Basalt cannot give'}.")
        if why := self.missing(c):
            raise InstallError(why)
        entry_id = f"{c.kind}:{c.name}"
        old = self.records.get(entry_id)
        if old is not None and not update:
            raise AlreadyInstalled(f"{entry_id} is already installed by Basalt. Update it to get this version.")
        if old is None and self.index is not None and self.index.get(entry_id) is not None:
            raise InstallError(f"There is already a capability called {entry_id} on this PC.")
        aside = self._set_aside(old) if old is not None else None
        try:
            record = self._build(c, entry_id, allow_scripts, anyway)
        except BaseException:
            if aside is not None:
                self._bring_back(old, aside)
            raise
        if aside is not None and aside.name and aside.exists():
            _forget(aside)
        return record

    def _set_aside(self, old: Dict[str, Any]) -> Path:
        """Takes the installed version out of the way (its record, and its folder under another name) so a new one can be built."""
        folder = str(old.get("folder") or "")
        aside = Path("")
        if folder:
            target = (self.root / folder).resolve()
            if target.parent.parent != self.root.resolve() or target.parent.name not in ("mcp", "skills"):
                raise InstallError("The folder recorded is not one Basalt makes; nothing was changed.")
            if target.exists():
                aside = target.with_name(target.name + ".previous")
                if aside.exists():
                    _forget(aside)
                target.rename(aside)
        self.records.delete(old["id"])
        if self.index is not None:
            self.index.refresh()
        return aside

    def _bring_back(self, old: Dict[str, Any], aside: Path) -> None:
        if aside.name and aside.exists():
            aside.rename(aside.with_name(aside.name[:-len(".previous")]))
        self.records.put(old)
        if self.index is not None:
            self.index.refresh()

    def _build(self, c: Candidate, entry_id: str, allow_scripts: bool, anyway: bool) -> Dict[str, Any]:
        build = {"npm": self._npm, "pypi": self._pypi, "remote": self._remote, "skill": self._skill}[c.route]
        record = build(c) if c.route == "remote" else build(c, allow_scripts, anyway)
        record.update(id=record.get("id", entry_id), name=c.name, kind=c.kind, source=c.source, route=c.route, version=c.version, description=c.description[:300],
                      installed_at=self.now().isoformat(timespec="seconds"), score=c.score.total if c.score else None, repo=c.repo, env=env_spec(c.env),
                      scripts_allowed=allow_scripts)
        self.records.put(record)
        if self.index is not None:
            self.index.refresh()
            shown = self.index.get(record["id"])
            if shown is None or shown["sha"] != record["sha"] or shown["state"] != "approved" or shown["origin"] != "basalt":
                self.remove(record["id"])
                raise InstallError("The capability system did not take the entry as it was registered; it was removed.")
        return record

    def _folder(self, kind: str, name: str) -> Path:
        folder = self.root / kind / name
        if folder.exists():
            raise InstallError(f"The folder {folder} is already there. Remove it before installing again.")
        folder.mkdir(parents=True)
        return folder

    def _npm(self, c: Candidate, allow_scripts: bool = False, anyway: bool = False) -> Dict[str, Any]:
        folder = self._folder("mcp", c.name)
        try:
            npm = self.which("npm") or "npm"
            run = self.runner([npm, "install", "--prefix", str(folder), f"{c.package}@{c.version}", *([] if allow_scripts else ["--ignore-scripts"]),
                               "--no-audit", "--no-fund"], folder, INSTALL_TIMEOUT_S)
            if run.returncode != 0:
                raise InstallError(f"npm could not install {c.package}@{c.version}: {_tail(run)}")
            got = self._lock_integrity(folder, c.package)
            differs = not c.integrity or got != c.integrity
            if differs and not anyway:
                raise IntegrityError(f"The package installed is not the release the registry described (hash {got[:24] or 'missing'}..., expected {c.integrity[:24] or 'none published'}...). "
                                     "It was deleted.", got, c.integrity)
            bins = self._installed_bins(folder, c)
            shim = folder / "node_modules" / ".bin" / (bins + (".cmd" if os.name == "nt" else ""))
            if not shim.is_file():
                raise InstallError(f"The package installed no program called {bins}.")
        except BaseException:
            _forget(folder)
            raise
        command = [str(shim), *c.args]
        return {"folder": f"mcp/{c.name}", "command": command, "package": c.package, "integrity": got, "sha": mcp_spec_sha(command[0], command[1:]),
                **({"integrity_differs": {"got": got, "expected": c.integrity}} if differs else {})}

    @staticmethod
    def _lock_integrity(folder: Path, package: str) -> str:
        try:
            lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
            return str(lock["packages"][f"node_modules/{package}"]["integrity"])
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    @staticmethod
    def _installed_bins(folder: Path, c: Candidate) -> str:
        last = c.package.rsplit("/", 1)[-1]
        return last if last in c.bins else next(iter(c.bins))

    def _pypi(self, c: Candidate, allow_scripts: bool = False, anyway: bool = False) -> Dict[str, Any]:
        folder = self._folder("mcp", c.name)
        site, wheels = folder / "site", folder / "_wheel"
        differs: Dict[str, str] = {}
        try:
            uv = self.which("uv")
            spec = f"{c.package}=={c.version}"
            pip = [self.python, "-m", "pip"]
            checked = False
            target: str = spec
            if self.runner([*pip, "--version"], None, 60.0).returncode == 0:
                run = self.runner([*pip, "download", spec, "--no-deps", *([] if allow_scripts else ["--only-binary=:all:"]), "-d", str(wheels)],
                                  folder, INSTALL_TIMEOUT_S)
                if run.returncode != 0:
                    raise InstallError(f"pip could not download {spec} as a ready-made wheel (a package that has to be built is not installed): {_tail(run)}")
                files = sorted(p for p in wheels.iterdir() if p.is_file()) if wheels.is_dir() else []
                if len(files) != 1:
                    raise InstallError("pip downloaded no single file for the release.")
                digest = hashlib.sha256(files[0].read_bytes()).hexdigest()
                if digest not in c.digests:
                    if not anyway:
                        raise IntegrityError(f"The wheel downloaded (sha256 {digest[:16]}...) is not one PyPI lists for {spec}. It was deleted.",
                                             digest, ", ".join(sorted(c.digests)))
                    differs = {"got": digest, "expected": ", ".join(sorted(c.digests))}
                checked, target = not differs, str(files[0])
            if uv:
                run = self.runner([uv, "pip", "install", "--target", str(site), "--python", self.python, *([] if allow_scripts else ["--no-build"]), target],
                                  folder, INSTALL_TIMEOUT_S)
            else:
                run = self.runner([*pip, "install", "--target", str(site), *([] if allow_scripts else ["--only-binary=:all:"]), "--no-input", target],
                                  folder, INSTALL_TIMEOUT_S)
            if run.returncode != 0:
                raise InstallError(f"The install of {spec} failed: {_tail(run)}")
            module, func = self._entry_point(site, c.package)
        except BaseException:
            _forget(folder)
            raise
        if wheels.is_dir():
            _forget(wheels)
        command = [self.python, "-c", BOOT, str(site), module, func, *c.args]
        return {"folder": f"mcp/{c.name}", "command": command, "package": c.package, "integrity": "wheel sha256 checked" if checked else "not checked (pip is not available)",
                "sha": mcp_spec_sha(command[0], command[1:]), **({"integrity_differs": differs} if differs else {})}

    @staticmethod
    def _entry_point(site: Path, package: str) -> "tuple[str, str]":
        want = _norm(package)
        for info in sorted(site.glob("*.dist-info")):
            if not _norm(info.name.rsplit("-", 1)[0]).startswith(want) and _norm(info.name.rsplit("-", 1)[0]) != want:
                continue
            ep = info / "entry_points.txt"
            if not ep.is_file():
                continue
            parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
            parser.optionxform = str                                      # type: ignore[assignment]
            parser.read_string(ep.read_text(encoding="utf-8", errors="replace"))
            if parser.has_section("console_scripts") and parser.items("console_scripts"):
                items = dict(parser.items("console_scripts"))
                target = items.get(package) or next(iter(items.values()))
                module, _, func = target.strip().split("[")[0].partition(":")
                if module.strip() and func.strip():
                    return module.strip(), func.strip()
        raise InstallError("The package declares no program to start (no console script).")

    def _remote(self, c: Candidate) -> Dict[str, Any]:
        return {"url": c.url, "folder": "", "sha": mcp_spec_sha("", [], c.url)}

    def _skill(self, c: Candidate, allow_scripts: bool = False, anyway: bool = False) -> Dict[str, Any]:
        owner, _, repo = c.repo.partition("/")
        tree = self.github.tree(owner, repo, c.commit)
        prefix = c.path.rstrip("/") + "/"
        files = [e for e in tree["tree"] if isinstance(e, dict) and e.get("type") == "blob" and str(e.get("path", "")).startswith(prefix) and e.get("mode") != "120000"]
        if not files or not any(e["path"] == prefix + "SKILL.md" for e in files):
            raise InstallError("The folder has no SKILL.md at that commit.")
        limits = DiscoverySettings(self.root).load()
        top_files, top_total, top_one = (limits[k] for k in ("max_skill_files", "max_skill_mb", "max_skill_file_mb"))
        if (top_files and len(files) > top_files) or (top_total and sum(int(e.get("size") or 0) for e in files) > top_total * MB) \
                or (top_one and any(int(e.get("size") or 0) > top_one * MB for e in files)):
            raise InstallError("The skill's folder is larger than the limits set in Settings, under Capabilities, Search online.")
        folder = self._folder("skills", c.name)
        differs: List[Dict[str, str]] = []
        try:
            for e in files:
                rel = e["path"][len(prefix):]
                if not safe_relative(rel):
                    raise InstallError(f"A file in the folder has a path that is not allowed: {rel[:60]}")
                data = self.github.raw_file(owner, repo, c.commit, e["path"], int(top_one * MB) + 1 if top_one else 2**31)
                got = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
                if got != e.get("sha"):
                    if not anyway:
                        raise IntegrityError(f"{rel} does not match the commit's own hash for it. The skill was deleted.", got, str(e.get("sha")))
                    differs.append({"file": rel, "got": got, "expected": str(e.get("sha"))})
                dest = folder / rel
                if folder.resolve() not in dest.resolve().parents:
                    raise InstallError("A file would be written outside the skill's folder.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            found = _read_skill(folder / "SKILL.md", "basalt")
            if found is None:
                raise InstallError("SKILL.md could not be read.")
            (folder / ".basalt-source.json").write_text(f'{{"repo": "{c.repo}", "path": "{c.path}", "commit": "{c.commit}"}}', encoding="utf-8")
        except BaseException:
            _forget(folder)
            raise
        return {"id": found.id, "folder": f"skills/{c.name}", "commit": c.commit, "sha": found.sha, **({"integrity_differs": differs} if differs else {})}

    # -- the list and the removal ---------------------------------------------------------------------------------

    def listing(self) -> List[Dict[str, Any]]:
        return self.records.listing()

    def remove(self, entry_id: str) -> bool:
        """Deletes what an install put on disk, and forgets the record. False if Basalt did not install it."""
        record = self.records.get(entry_id)
        if record is None:
            return False
        folder = str(record.get("folder") or "")
        if folder:
            target = (self.root / folder).resolve()
            base = self.root.resolve()
            if target.parent.parent != base or target.parent.name not in ("mcp", "skills"):
                raise InstallError("The folder recorded is not one Basalt makes; nothing was deleted.")
            if target.exists():
                _forget(target)
        self.records.delete(entry_id)
        if self.index is not None:
            self.index.refresh()
        return True
