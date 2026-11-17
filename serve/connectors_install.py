"""Installing and removing what the Connectors tab lists, with the machinery the harness already has (tadamoo/discovery): a server of a registry or a folder of a GitHub repository is made a
candidate, installed after the person pressed the button (their authorisation), and recorded; a skill in a folder of this PC is copied. What was installed lives in the capabilities folder of
TaDaMoo, where the index reads it. A downloaded skill is also kept untouched in the originals folder the person chose, so changes are only ever made on the installed copy.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from tadamoo.capabilities import _read_skill
from tadamoo.discovery import sources as src
from tadamoo.discovery.candidates import CandidateStore, safe_relative
from tadamoo.discovery.finder import Finder, resolve_bases
from tadamoo.discovery.installed import InstalledStore, github_token
from tadamoo.discovery.installer import AlreadyInstalled, InstallError, Installer, IntegrityError
from tadamoo.discovery.settings import DiscoverySettings

MB = 1 << 20
SETTINGS = "settings.json"


class ConnectorInstallError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ConnectorSettings:
    """The few settings of the Connectors tab kept in its own folder: where the downloaded originals go."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / SETTINGS

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"originals_dir": str(data.get("originals_dir") or "") if isinstance(data, dict) else ""}

    def write(self, originals_dir: Any) -> Dict[str, Any]:
        text = str(originals_dir or "").strip()
        if len(text) > 500 or "\x00" in text:
            raise ConnectorInstallError("originals_dir: a folder of this PC", "bad_folder")
        if text:
            folder = Path(text)
            if not folder.is_absolute() or ".." in folder.parts:
                raise ConnectorInstallError("originals_dir: the full path of a folder of this PC", "bad_folder")
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ConnectorInstallError(f"that folder cannot be made: {e}", "bad_folder") from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"originals_dir": text}), encoding="utf-8")
        return self.read()


class ConnectorInstaller:
    def __init__(self, caps: Any, directory: Path, vault_dir: Any = None, http: Optional[src.Http] = None) -> None:
        """`caps` is the CapabilityApi (its index reads what is installed); `directory` is the Connectors folder of the user data."""
        self.caps, self.vault_dir = caps, vault_dir
        self.settings = ConnectorSettings(directory)
        self.http = http or src.Http()
        self._lock = threading.Lock()

    # -- the clients -----------------------------------------------------------------------------------

    def _bases(self) -> Dict[str, str]:
        return resolve_bases(DiscoverySettings(self.caps.index.dir).load()["base_urls"])

    def _github(self) -> src.GithubClient:
        used = self._bases()
        return src.GithubClient(self.http, used["github"], used["github_raw"], lambda: github_token(self.vault_dir))

    def finder(self, store: CandidateStore) -> Finder:
        return Finder(DiscoverySettings(self.caps.index.dir), store, http=self.http, token=lambda: github_token(self.vault_dir))

    def _installer(self, store: CandidateStore) -> Installer:
        return Installer(self.caps.index.dir, store, index=self.caps.index, github=self._github())

    # -- what can be fetched to be read before installing --------------------------------------------------

    def fetch_skill(self, repo: str, path: str, pin: str, into: Path, files_max: int = 30, file_bytes: int = 200_000) -> Path:
        """The files of a folder of a GitHub repository at a pinned branch or tag, in a temporary folder, for the facts sheet and the review. Nothing is run."""
        github, (owner, _, project) = self._github(), repo.partition("/")
        ref = pin or str(github.repo(owner, project).get("default_branch") or "HEAD")
        commit = github.head_commit(owner, project, ref)
        prefix = path.rstrip("/") + "/"
        tree = github.tree(owner, project, commit)
        files = [e for e in tree["tree"] if isinstance(e, dict) and e.get("type") == "blob" and str(e.get("path", "")).startswith(prefix) and e.get("mode") != "120000"][:files_max]
        for e in files:
            rel = str(e["path"])[len(prefix):]
            if not safe_relative(rel) or int(e.get("size") or 0) > file_bytes:
                continue
            dest = into / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(github.raw_file(owner, project, commit, str(e["path"]), file_bytes))
        return into

    # -- install ------------------------------------------------------------------------------------------

    def install(self, kind: str, source: Dict[str, Any], flags: Dict[str, bool]) -> Dict[str, Any]:
        """`kind`: `server` (source: the registry's `server` object), `github_skill` (repo, path, pin, name, description) or `folder_skill` (folder). `flags`: allow_scripts, anyway, update.
        Returns the record that was made."""
        store = CandidateStore()
        try:
            with self._lock:
                if kind == "folder_skill":
                    record = self._install_folder(Path(source["folder"]), flags.get("update", False))
                else:
                    finder = self.finder(store)
                    c = finder.candidate_for_server(source["server"]) if kind == "server" else finder.candidate_for_skill(source["repo"], source["path"], source.get("pin", ""),
                                                                                                                       source.get("name", ""), source.get("description", ""))
                    record = self._installer(store).install(c.id, allow_scripts=bool(flags.get("allow_scripts")), anyway=bool(flags.get("anyway")), update=bool(flags.get("update")))
                    if kind == "github_skill":
                        self._keep_original(record, source)
        except IntegrityError as e:
            raise ConnectorInstallError(str(e), "integrity") from None
        except AlreadyInstalled as e:
            raise ConnectorInstallError(str(e), "installed") from None
        except (InstallError, ValueError, src.SourceError) as e:
            raise ConnectorInstallError(str(e), "install") from None
        return {k: record[k] for k in ("id", "name", "kind", "version", "source", "installed_at") if k in record}

    def _install_folder(self, folder: Path, update: bool) -> Dict[str, Any]:
        """A skill in a folder of this PC is copied into the capabilities folder (a link is never followed) and recorded; the folder it came from is left as it is."""
        skill = _read_skill(folder / "SKILL.md", "tadamoo")
        if skill is None:
            raise InstallError("That folder has no readable SKILL.md.")
        limits = DiscoverySettings(self.caps.index.dir).load()
        files = [p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()]
        if (limits["max_skill_files"] and len(files) > limits["max_skill_files"]) or (limits["max_skill_mb"] and sum(p.stat().st_size for p in files) > limits["max_skill_mb"] * MB):
            raise InstallError("The skill's folder is larger than the limits set in Settings, under Capabilities, Search online.")
        records = InstalledStore(self.caps.index.dir)
        entry_id, name = skill.id, folder.name
        if records.get(entry_id) is not None and not update:
            raise AlreadyInstalled(f"{entry_id} is already installed by TaDaMoo. Update it to get this version.")
        if records.get(entry_id) is None and self.caps.index.get(entry_id) is not None:
            raise InstallError(f"There is already a capability called {entry_id} on this PC.")
        target = Path(self.caps.index.dir) / "skills" / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        try:
            for p in files:
                rel = p.relative_to(folder)
                if not safe_relative(rel.as_posix()):
                    raise InstallError(f"A file in the folder has a path that is not allowed: {rel.as_posix()[:60]}")
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target / rel)
            found = _read_skill(target / "SKILL.md", "tadamoo")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        record = {"id": found.id, "folder": f"skills/{name}", "sha": found.sha, "name": found.name, "kind": "skill", "source": "folder", "route": "folder", "version": "",
                  "description": found.description[:300], "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repo": "", "env": [], "scripts_allowed": False,
                  "from_folder": str(folder)}
        records.put(record)
        self.caps.index.refresh()
        return record

    def _keep_original(self, record: Dict[str, Any], source: Dict[str, Any]) -> None:
        """A copy of what was downloaded, as it was, in the originals folder: kept apart from the installed copy, which is the one that may be changed."""
        base = self.settings.read()["originals_dir"]
        folder = str(record.get("folder") or "")
        if not base or not folder:
            return
        installed = Path(self.caps.index.dir) / folder
        name = f"{source['repo'].replace('/', '__')}@{str(record.get('commit') or 'unknown')[:12]}__{source['path'].replace('/', '_')}"
        target = Path(base) / "github" / name
        try:
            if installed.is_dir() and not target.exists():
                shutil.copytree(installed, target)
        except OSError:
            pass                                             # the originals are a convenience for the updates: a folder that cannot be written does not undo the install

    # -- remove -----------------------------------------------------------------------------------------

    def uninstall(self, entry_id: str) -> bool:
        """Deletes what TaDaMoo installed under `entry_id` (its folder and its record); the originals stay."""
        try:
            with self._lock:
                return self._installer(CandidateStore()).remove(entry_id)
        except InstallError as e:
            raise ConnectorInstallError(str(e), "uninstall") from None
