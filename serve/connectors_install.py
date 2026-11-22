"""Installing and removing what the Connectors tab lists, with the machinery the harness already has (tadamoo/discovery): a server of a registry or a folder of a GitHub repository is made a
candidate, installed after the person pressed the button (their authorisation), and recorded; a skill in a folder of this PC is copied. What was installed lives in the capabilities folder of
TaDaMoo, where the index reads it. A downloaded skill is also kept untouched in the originals folder the person chose, so changes are only ever made on the installed copy.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.capabilities import _read_skill
from tadamoo.discovery import sources as src
from tadamoo.discovery.candidates import CandidateStore, safe_relative
from tadamoo.discovery.finder import Finder, resolve_bases
from tadamoo.discovery.installed import InstalledStore, github_token
from tadamoo.discovery.installer import AlreadyInstalled, InstallError, Installer, IntegrityError
from tadamoo.discovery.settings import DiscoverySettings
from tadamoo.merge import tree
from tadamoo.merge.errors import MergeProblem
from tadamoo.merge.session import Session, Unit, hash_folder

MB = 1 << 20
SETTINGS = "settings.json"
MAX_ACCOUNTS = 10
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")


def _accounts(rec: Dict[str, Any]) -> List[str]:
    """The record's own account names, or a single implicit "default" for a record made before this feature (its values are read from the old, un-suffixed places)."""
    accs = rec.get("accounts")
    return [str(a) for a in accs] if isinstance(accs, list) and accs else ["default"]


def _default_account(rec: Dict[str, Any]) -> str:
    accs = _accounts(rec)
    d = str(rec.get("default_account") or "")
    if d in accs:
        return d
    return accs[0] if len(accs) == 1 else ""


class ConnectorInstallError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


REVIEW_MODES = ("ours", "nvidia", "both")


class ConnectorSettings:
    """The few settings of the Connectors tab kept in its own folder: where the downloaded originals go, and which review(s) run before an install
    (our own, NVIDIA SkillEvaluator's, or both) with whether its Tier 3 (a live agent run, needs Docker) may be used."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / SETTINGS

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        mode = data.get("review_mode")
        return {"originals_dir": str(data.get("originals_dir") or ""), "review_mode": mode if mode in REVIEW_MODES else "ours",
                "review_tier3_docker": data.get("review_tier3_docker") is True}

    def write(self, originals_dir: Any = None, review_mode: Any = None, review_tier3_docker: Any = None) -> Dict[str, Any]:
        now = self.read()
        if originals_dir is not None:
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
            now["originals_dir"] = text
        if review_mode is not None:
            if review_mode not in REVIEW_MODES:
                raise ConnectorInstallError(f"review_mode: one of {', '.join(REVIEW_MODES)}", "bad_review_mode")
            now["review_mode"] = review_mode
        if review_tier3_docker is not None:
            if not isinstance(review_tier3_docker, bool):
                raise ConnectorInstallError("review_tier3_docker: true or false", "bad_review_mode")
            now["review_tier3_docker"] = review_tier3_docker
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(now), encoding="utf-8")
        return self.read()


class ConnectorInstaller:
    def __init__(self, caps: Any, directory: Path, vault_dir: Any = None, http: Optional[src.Http] = None, runner: Any = None, which: Any = None) -> None:
        """`caps` is the CapabilityApi (its index reads what is installed); `directory` is the Connectors folder of the user data. `runner` and `which` are the ones tests give
        an Installer to avoid a real npm/pip; left out, the Installer runs the real ones."""
        self.caps, self.vault_dir = caps, vault_dir
        self.dir = Path(directory)
        self.settings = ConnectorSettings(directory)
        self.http = http or src.Http()
        self.runner, self.which = runner, which
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
        return Installer(self.caps.index.dir, store, index=self.caps.index, github=self._github(), runner=self.runner, which=self.which)

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
        """`kind`: `server` (source: the registry's `server` object), `github_skill` (repo, path, pin, name, description), `github_mcp_server` (an MCP server kept in a GitHub
        repository, not published to any registry: same source fields, started from what its own package.json or pyproject.toml declares) or `folder_skill` (folder). `flags`: allow_scripts, anyway, update.
        Returns the record that was made."""
        store = CandidateStore()
        try:
            with self._lock:
                if kind == "folder_skill":
                    record = self._install_folder(Path(source["folder"]), flags.get("update", False))
                else:
                    finder = self.finder(store)
                    if kind == "server":
                        c = finder.candidate_for_server(source["server"])
                    elif kind == "github_mcp_server":
                        c = finder.candidate_for_mcp_repo(source["repo"], source["path"], source.get("pin", ""), source.get("name", ""), source.get("description", ""))
                    else:
                        c = finder.candidate_for_skill(source["repo"], source["path"], source.get("pin", ""), source.get("name", ""), source.get("description", ""))
                    record = self._installer(store).install(c.id, allow_scripts=bool(flags.get("allow_scripts")), anyway=bool(flags.get("anyway")), update=bool(flags.get("update")))
                    if kind in ("github_skill", "github_mcp_server"):
                        self._keep_original(record, source)
                        records = InstalledStore(self.caps.index.dir)
                        records.put({**record, "pin": source.get("pin", ""), "source_path": source["path"]})            # what an update asks GitHub about
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

    def _keep_original(self, record: Dict[str, Any], source: Dict[str, Any], download: Optional[Path] = None) -> None:
        """A copy of what was downloaded, as it was, in the originals folder: kept apart from the installed copy, which is the one that may be changed. After a merge `download` is
        the new version as it came from GitHub, because the installed copy then holds the person's changes too."""
        folder = str(record.get("folder") or "")
        if not folder:
            return
        installed = download or Path(self.caps.index.dir) / folder
        name = f"{source['repo'].replace('/', '__')}@{str(record.get('commit') or 'unknown')[:12]}__{source['path'].replace('/', '_')}"
        target = self._originals_base() / "github" / name
        try:
            if installed.is_dir() and not target.exists():
                shutil.copytree(installed, target, ignore=shutil.ignore_patterns(".tadamoo-source.json"))
        except OSError:
            pass                                             # the originals are a convenience for the updates: a folder that cannot be written does not undo the install

    # -- configure: the variables a server reads --------------------------------------------------------

    def _record(self, entry_id: str) -> Dict[str, Any]:
        rec = InstalledStore(self.caps.index.dir).get(entry_id)
        if rec is None:
            raise ConnectorInstallError("TaDaMoo did not install that one, so it cannot be set up from here.", "foreign")
        return rec

    def _vault_has(self, entry: str) -> bool:
        if not self.vault_dir:
            return False
        try:
            from tadamoo.vault import Vault
            return bool(Vault(self.vault_dir).reveal(entry, "value"))
        except Exception:
            return False

    @staticmethod
    def _entry_key(spec: Dict[str, Any], account: str, accs: List[str]) -> str:
        """The vault entry a secret is kept under: the plain name the record always used, for the first (or only) account, so an install made before
        multiple accounts existed still finds its own secret; a later account's is kept apart by its name."""
        entry = str(spec.get("entry") or "")
        return entry if account == accs[0] else f"{entry}::{account}"

    def _resolve_account(self, rec: Dict[str, Any], account: str) -> str:
        accs = _accounts(rec)
        if account:
            if account not in accs:
                raise ConnectorInstallError("no such account", "bad_account")
            return account
        d = _default_account(rec)
        if d:
            return d
        if len(accs) == 1:
            return accs[0]
        raise ConnectorInstallError("more than one account and none is the default: name which one", "account_needed")

    def accounts(self, entry_id: str) -> Dict[str, Any]:
        rec = self._record(entry_id)
        return {"id": entry_id, "accounts": _accounts(rec), "default": _default_account(rec)}

    def add_account(self, entry_id: str, name: str) -> Dict[str, Any]:
        name = (name or "").strip()
        if not ACCOUNT_RE.match(name):
            raise ConnectorInstallError("a name: letters, digits, spaces, _ . -, up to 60 characters", "bad_account")
        with self._lock:
            rec = self._record(entry_id)
            accs = _accounts(rec)
            if name.lower() in (a.lower() for a in accs):
                raise ConnectorInstallError(f"there is already an account called {name}", "bad_account")
            if len(accs) >= MAX_ACCOUNTS:
                raise ConnectorInstallError("too many accounts: remove one first", "bad_account")
            rec["accounts"] = accs + [name]
            if not rec.get("default_account") and len(accs) == 1:
                rec["default_account"] = accs[0]                         # the one account there was stays the default once a second one exists
            InstalledStore(self.caps.index.dir).put(rec)
        return self.accounts(entry_id)

    def remove_account(self, entry_id: str, name: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(entry_id)
            accs = _accounts(rec)
            if name not in accs:
                raise ConnectorInstallError("no such account", "bad_account")
            if len(accs) <= 1:
                raise ConnectorInstallError("the last account cannot be removed; uninstall the connector instead", "bad_account")
            if self.vault_dir:
                from tadamoo.vault import Vault
                vault = Vault(self.vault_dir)
                for spec in rec.get("env") or []:
                    if isinstance(spec, dict) and spec.get("secret"):
                        vault.delete(self._entry_key(spec, name, accs))
            values = dict(rec.get("account_values") or {})
            values.pop(name, None)
            rec["account_values"] = values
            rec["accounts"] = [a for a in accs if a != name]
            if rec.get("default_account") == name:
                rec["default_account"] = ""
            InstalledStore(self.caps.index.dir).put(rec)
        return self.accounts(entry_id)

    def set_default_account(self, entry_id: str, name: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(entry_id)
            accs = _accounts(rec)
            if name and name not in accs:
                raise ConnectorInstallError("no such account", "bad_account")
            rec["default_account"] = name or ""
            InstalledStore(self.caps.index.dir).put(rec)
        return self.accounts(entry_id)

    def config(self, entry_id: str, account: str = "") -> Dict[str, Any]:
        """The variables the installed server reads, for one account: for each, whether it is secret and whether it is set. A secret's value is never given back; a plain value is."""
        rec = self._record(entry_id)
        accs = _accounts(rec)
        acc = self._resolve_account(rec, account)
        plain = (rec.get("account_values") or {}).get(acc) or {}
        rows = []
        for spec in rec.get("env") or []:
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            name, secret = str(spec["name"]), bool(spec.get("secret"))
            value = plain.get(name, spec.get("value") if acc == accs[0] and name not in plain else "")        # a pre-multi-account value still reads back for the first account
            rows.append({"name": name, "required": bool(spec.get("required")), "secret": secret, "entry": spec.get("entry", ""),
                         "set": self._vault_has(self._entry_key(spec, acc, accs)) if secret else bool(value), "value": "" if secret else str(value or "")})
        return {"id": entry_id, "name": rec.get("name", ""), "kind": rec.get("kind", ""), "variables": rows, "remote": bool(rec.get("url")),
                "account": acc, "accounts": accs, "default_account": _default_account(rec)}

    def configure(self, entry_id: str, values: Dict[str, Any], account: str = "") -> Dict[str, Any]:
        """Sets the variables of one account: a secret goes to the vault, bound to an entry of its own per account; a plain value is kept in the record, per account.
        An empty plain value clears it, an empty secret leaves what is there."""
        records = InstalledStore(self.caps.index.dir)
        rec = self._record(entry_id)
        accs = _accounts(rec)
        acc = self._resolve_account(rec, account)
        known = {str(s["name"]): s for s in rec.get("env") or [] if isinstance(s, dict) and s.get("name")}
        if not isinstance(values, dict) or any(k not in known for k in values):
            raise ConnectorInstallError("values: only the variables this server reads", "bad_values")
        for name, value in values.items():
            if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
                raise ConnectorInstallError(f"{name}: a text of at most 4000 characters", "bad_values")
        with self._lock:
            account_values = dict(rec.get("account_values") or {})
            plain = dict(account_values.get(acc) or {})
            for name, value in values.items():
                spec = known[name]
                if spec.get("secret"):
                    if value:
                        if not self.vault_dir:
                            raise ConnectorInstallError("there is no vault to keep a secret in", "no_vault")
                        from tadamoo.vault import Vault
                        label = f"{rec.get('name', entry_id)}: {name}" + (f" ({acc})" if acc != accs[0] else "")
                        Vault(self.vault_dir).put(self._entry_key(spec, acc, accs), kind="secret", label=label, fields={"value": value})
                else:
                    plain[name] = value.strip()
                    spec.pop("value", None)                              # the schema keeps no value of its own once an account has one
            account_values[acc] = plain
            rec["account_values"] = account_values
            records.put(rec)
        self.caps.index.refresh()
        return self.config(entry_id, acc)

    # -- updates ----------------------------------------------------------------------------------------

    @staticmethod
    def _registry_match(finder: Finder, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The registry's current listing for an installed MCP server's record (matched by its package identifier or its remote URL, the two things
        a record keeps that a listing can be recognised by), or `None` when the registry no longer has it."""
        finder._prepare()
        for row in finder.registry.search(str(rec.get("name") or "").replace("-", " ")):
            s = row.get("server", {})
            if (rec.get("package") and any(str(p.get("identifier")) == rec["package"] for p in s.get("packages") or [] if isinstance(p, dict))) or \
               (rec.get("url") and any(str(r.get("url")) == rec["url"] for r in s.get("remotes") or [] if isinstance(r, dict))):
                return s
        return None

    def updates(self) -> Dict[str, Any]:
        """For each thing TaDaMoo installed from GitHub or a registry, whether a newer one is there. Nothing is downloaded: only the newest commit or version is asked. One that does not answer is listed with the reason."""
        out, errors = [], []
        github, finder = self._github(), self.finder(CandidateStore())
        for rec in InstalledStore(self.caps.index.dir).load().values():
            try:
                if rec.get("route") in ("skill", "github_mcp") and rec.get("repo") and rec.get("commit"):
                    owner, _, project = str(rec["repo"]).partition("/")
                    ref = rec.get("pin") or str(github.repo(owner, project).get("default_branch") or "HEAD")
                    head = github.head_commit(owner, project, ref)
                    if head != rec["commit"]:
                        out.append({"id": rec["id"], "name": rec.get("name", ""), "from": str(rec["commit"])[:10], "to": head[:10],
                                   "kind": "skill" if rec["route"] == "skill" else "mcp"})
                elif rec.get("source") == "registry" and rec.get("version"):
                    s = self._registry_match(finder, rec)
                    if s is not None and str(s.get("version") or "") not in ("", str(rec["version"])):
                        out.append({"id": rec["id"], "name": rec.get("name", ""), "from": str(rec["version"]), "to": str(s["version"]), "kind": "mcp"})
            except Exception as e:                       # a source that does not answer is told, and does not stop the others
                errors.append({"id": rec.get("id", ""), "why": str(e)[:160]})
        return {"updates": out, "errors": errors}

    def update(self, entry_id: str, flags: Dict[str, bool]) -> Dict[str, Any]:
        """Installs the newer one over the installed one (the old one comes back if the new one fails): a GitHub skill at its current commit, or an
        MCP server (npm, PyPI or remote) at the registry's current listing. The downloaded original of a new skill commit is kept next to the old
        one. Accounts and their configured values carry over to the new record (a variable the new version no longer reads is simply unused, never
        shown again). This replaces the installed copy whole: a change the person made directly to a skill's files is lost; `update_plan`/
        `apply_update` below merge a skill instead, keeping it."""
        rec = self._record(entry_id)
        flags = {**flags, "update": True}
        carry = {k: rec[k] for k in ("accounts", "default_account", "account_values") if k in rec}
        if rec.get("route") in ("skill", "github_mcp"):
            out = self.install("github_skill" if rec["route"] == "skill" else "github_mcp_server",
                               {"repo": rec["repo"], "path": rec.get("source_path") or "", "pin": rec.get("pin", ""), "name": rec.get("name", ""),
                                "description": rec.get("description", "")}, flags)
        elif rec.get("source") == "registry":
            s = self._registry_match(self.finder(CandidateStore()), rec)
            if s is None:
                raise ConnectorInstallError("The registry no longer lists this server.", "gone")
            out = self.install("server", {"server": s}, flags)
        else:
            raise ConnectorInstallError("This kind of connector cannot be updated from here.", "kind")
        if carry:
            with self._lock:
                records = InstalledStore(self.caps.index.dir)
                new_rec = records.get(out["id"])
                if new_rec is not None:
                    new_rec.update(carry)
                    records.put(new_rec)
        return out

    # -- three-way merge of an update -------------------------------------------------------------------

    def _originals_base(self) -> Path:
        """Where the downloaded originals are kept: the folder the person chose, else one of our own beside the Connectors settings (an original is what a merge needs, so it is always kept)."""
        chosen = self.settings.read()["originals_dir"]
        return Path(chosen) if chosen else self.dir / "originals"

    def _original_dir(self, rec: Dict[str, Any]) -> Optional[Path]:
        """Where `_keep_original` put the download this skill was installed at: the common base for a merge."""
        if not rec.get("repo") or not rec.get("commit"):
            return None
        name = f"{rec['repo'].replace('/', '__')}@{str(rec['commit'])[:12]}__{str(rec.get('source_path') or '').replace('/', '_')}"
        return self._originals_base() / "github" / name

    def _new_commit(self, rec: Dict[str, Any]) -> tuple:
        """The repository, the current ref and the commit it resolves to now, for a skill's own record."""
        repo, path, pin = str(rec.get("repo") or ""), str(rec.get("source_path") or ""), str(rec.get("pin") or "")
        github = self._github()
        owner, _, project = repo.partition("/")
        ref = pin or str(github.repo(owner, project).get("default_branch") or "HEAD")
        return repo, path, github.head_commit(owner, project, ref)

    def _session(self) -> Session:
        return Session(self.dir / ".merge")

    def update_plan(self, entry_id: str, instructions: Optional[List[str]] = None, language: str = "en", client: Any = None) -> Dict[str, Any]:
        """What updating this GitHub skill to its current commit would do, merged with what the person changed (tadamoo/merge/): the three versions of every file that
        differs (installed, new, merged) with a description in words when a model is there. Nothing on disk changes; the proposal is kept under a `token`, and a file both
        sides changed in the same place needs a choice before `apply_update` can run. `has_base` says whether the original download was kept, so that a real three-way merge
        was possible, or every difference had to be called a conflict for lack of one to compare against."""
        rec = self._record(entry_id)
        if rec.get("route") != "skill" or not rec.get("repo"):
            raise ConnectorInstallError("Only a skill from GitHub can be merged from here.", "kind")
        session = self._session()
        token, work = session.new_work()
        try:
            repo, path, to_commit = self._new_commit(rec)
            installed = Path(self.caps.index.dir) / str(rec.get("folder") or "")
            ours, theirs, base = work / "in" / "ours", work / "in" / "theirs", work / "in" / "base"
            ours.mkdir(parents=True)
            theirs.mkdir(parents=True)
            if installed.is_dir():
                shutil.copytree(installed, ours, dirs_exist_ok=True)
            self.fetch_skill(repo, path, to_commit, theirs, files_max=1000, file_bytes=2 * MB)
            original = self._original_dir(rec)
            has_base = bool(original and original.is_dir())
            if has_base:
                shutil.copytree(original, base)
            name = str(rec.get("name") or "")
            unit = Unit(f"skill:{name}", name, ours, theirs, base if has_base else None, "original" if has_base else "", Path(path).name)
            meta = {"entry_id": entry_id, "name": name, "repo": repo, "path": path, "from_commit": str(rec.get("commit") or ""), "to_commit": to_commit}
            out = session.propose(token, work, meta, [unit], instructions or [], client, language)
        except MergeProblem as e:
            shutil.rmtree(work, ignore_errors=True)
            raise ConnectorInstallError(str(e), e.code) from None
        except ConnectorInstallError:
            shutil.rmtree(work, ignore_errors=True)
            raise
        except (ValueError, OSError) as e:
            shutil.rmtree(work, ignore_errors=True)
            raise ConnectorInstallError(str(e)[:300] or type(e).__name__, "merge") from None
        return {"id": entry_id, "from_commit": str(rec.get("commit") or ""), "to_commit": to_commit, **out}

    @staticmethod
    def _sync_folder(source: Path, target: Path) -> None:
        """Makes the files of `target` the files of `source`: what `source` lacks is removed, the rest written; a tool folder in `target` (cache, packages) is left alone."""
        want = tree.read_tree(source)
        for rel in set(tree.read_tree(target)) - set(want):
            (target / rel).unlink(missing_ok=True)
        for rel, data in want.items():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            (target / rel).write_bytes(data)

    def apply_update(self, token: str, choice: str = "merge", resolutions: Any = None) -> Dict[str, Any]:
        """Does what the person chose for a proposed update. `merge`: writes the merge, a conflict resolved as `resolutions` says (`ours`, `theirs`, or `merged` once the
        proposal holds no marker), the installed folder backed up first and put back if anything fails partway. `overwrite`: installs the new version whole, as `update` does.
        `cancel`: nothing. The new commit is recorded and its own download kept as the original, never what was merged."""
        session = self._session()
        try:
            meta, work, rows = session.load(token)
        except MergeProblem as e:
            raise ConnectorInstallError(str(e), e.code) from None
        entry_id = meta["entry_id"]
        if choice == "cancel":
            session.discard(token)
            return {"id": entry_id, "status": "cancelled"}
        if choice == "overwrite":
            out = self.update(entry_id, {})
            session.discard(token)
            return {"id": entry_id, "status": "replaced", "installed": out}
        if choice != "merge":
            raise ConnectorInstallError("choice: merge, overwrite or cancel", "bad_choice")
        with self._lock:
            rec = self._record(entry_id)
            installed = Path(self.caps.index.dir) / str(rec.get("folder") or "")
            if str(rec.get("commit") or "") != meta["from_commit"] or hash_folder(installed) != rows[0]["ours_hash"]:
                raise ConnectorInstallError("The installed copy changed after this update was read: read it again.", "stale")
            try:
                chosen = session.resolutions(rows, resolutions)
            except MergeProblem as e:
                raise ConnectorInstallError(str(e), e.code) from None
            open_ = session.open_files(rows, chosen)
            if open_:
                raise ConnectorInstallError("some files need a choice (\"ours\", \"theirs\", or \"merged\" once it holds no conflict): " + ", ".join(sorted(open_)[:5]), "conflicts")
            final = session.write_finals(work, rows, chosen)[rows[0]["unit"].key]
            backup = Path(tempfile.mkdtemp(prefix="tadamoo-mergebak-"))
            try:
                shutil.copytree(installed, backup / "ours")
                self._sync_folder(final, installed)
                (installed / ".tadamoo-source.json").write_text(json.dumps({"repo": meta["repo"], "path": meta["path"], "commit": meta["to_commit"]}), encoding="utf-8")
                rec["commit"], rec["version"] = meta["to_commit"], meta["to_commit"][:10]
                found = _read_skill(installed / "SKILL.md", "tadamoo")
                if found is not None:
                    rec["sha"] = found.sha
                InstalledStore(self.caps.index.dir).put(rec)
                self._keep_original(rec, {"repo": meta["repo"], "path": meta["path"]}, rows[0]["unit"].theirs)
            except BaseException:
                self._sync_folder(backup / "ours", installed)
                raise
            finally:
                shutil.rmtree(backup, ignore_errors=True)
        self.caps.index.refresh()
        session.discard(token)
        return {"id": entry_id, "status": "merged", "to_commit": meta["to_commit"], "counts": tree.counts(rows[0]["files"])}

    # -- remove -----------------------------------------------------------------------------------------

    def uninstall(self, entry_id: str) -> bool:
        """Deletes what TaDaMoo installed under `entry_id` (its folder and its record); the originals stay."""
        try:
            with self._lock:
                return self._installer(CandidateStore()).remove(entry_id)
        except InstallError as e:
            raise ConnectorInstallError(str(e), "uninstall") from None
