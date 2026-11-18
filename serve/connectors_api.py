"""The read side of the Connectors tab.

    GET /api/connectors?scope=all|personal|external&kind=&state=&q=   the counts and the two bands (installed, available) of a sub-tab
    GET /api/connectors/item?id=                                      one item with its relations and, for a plugin, its tree
    POST /api/connectors/review  {id}                                 the facts sheet of an item that is not installed yet and the advisory review of a model
    POST /api/connectors/install {id, allow_scripts?, anyway?, update?}   installs an item a provider lists (the press of the button is the authorisation)
    POST /api/connectors/uninstall {id}                               removes what TaDaMoo installed (the downloaded originals stay)
    GET|POST /api/connectors/settings {originals_dir?, review_mode?, review_tier3_docker?}   where the downloaded originals are kept, and which review(s) run
    GET /api/connectors/nvidia                                        NVIDIA SkillEvaluator's own state (installed, a Python 3.12+ found, Docker found)
    POST /api/connectors/nvidia/install {}                            installs it into its own venv (synchronous; a Python 3.12+ already on this PC, never downloaded)
    GET /api/connectors/accounts?id=                                  the accounts of an installed item and which is the default
    POST /api/connectors/accounts/add|remove|default {id, name}       add, remove or set the default account (a second account onward keeps its own secrets apart)
    GET /api/connectors/config?id=&account=                          the variables of one account (default: the item's default account, or its only one)
    POST /api/connectors/configure/ask {id, question, history?, account?}   a guided answer about filling in the settings; the model sees field names only, never a value
    POST /api/connectors/update/plan {id}                             a GitHub skill's update, file by file (same, upstream, kept, conflict) and a risk review of the diff
    POST /api/connectors/update/apply {id, resolutions}                applies the plan; `resolutions` gives "upstream" or "kept" for every conflicting path
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.connectors import facts, inventory, model, providers as prov, review as review_mod
from tadamoo.connectors.model import Inventory
from serve.connectors_install import ConnectorInstallError


class ConnectorError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def _has_docker() -> bool:
    import shutil
    return bool(shutil.which("docker"))


class ConnectorsApi:
    def __init__(self, entries: Any, personal: Any, providers: Any = None, get: Any = None, reviewer: Optional[Callable[[], Any]] = None, installer: Any = None) -> None:
        """`entries` returns the entries of the capability index, `personal` the personal skills (and the slugs switched off); both are called on every request. `providers` is the
        list the person set (tadamoo/connectors/providers.py) and `get` fetches an address: with them the "available" band is made from the enabled providers."""
        self._entries = entries
        self._personal = personal
        self.providers, self._get = providers, get
        self.installer = installer                            # serve/connectors_install.py: installs and removes (None: the tab only looks)
        self.reviewer = reviewer                              # gives the model client for the review (None: the facts sheet only)

    def inventory(self, query: str = "") -> Inventory:
        skills, off = self._personal()
        extra = prov.available(self.providers.list(), self._get, query) if self.providers is not None and self._get is not None else []
        return inventory.build(self._entries(), skills, off, extra)

    def provider_rows(self) -> Dict[str, Any]:
        return {"providers": self.providers.list() if self.providers is not None else [], "kinds": list(prov.KINDS), "trust": list(prov.TRUST)}

    def change_providers(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """`{action: add, kind, ref, trust?}`, `{action: enable|disable|remove, id}` or `{action: reset}`."""
        if self.providers is None:
            raise ConnectorError("there is no list of providers here", "none")
        action = req.get("action")
        try:
            if action == "add":
                self.providers.add(str(req.get("kind") or ""), req.get("ref"), str(req.get("trust") or "unknown"))
            elif action in ("enable", "disable"):
                self.providers.set_enabled(str(req.get("id") or ""), action == "enable")
            elif action == "remove":
                self.providers.remove(str(req.get("id") or ""))
            elif action == "reset":
                self.providers.reset()
            else:
                raise ConnectorError("action: add, enable, disable, remove or reset", "action")
        except prov.ProviderError as e:
            raise ConnectorError(str(e), "provider") from None
        return self.provider_rows()

    def view(self, scope: str = "all", kind: str = "", state: str = "", query: str = "") -> Dict[str, Any]:
        inv = self.inventory(query)
        if kind and kind not in model.KINDS:
            raise ConnectorError("kind: one of " + ", ".join(model.KINDS), "kind")
        if state and state not in model.effective_states():
            raise ConnectorError("state: one of " + ", ".join(model.effective_states()), "state")
        try:
            bands = model.view(inv, kind or None, scope or "all", query, state or None)
        except ValueError as e:
            raise ConnectorError(str(e), "scope") from None
        return {"counts": model.counts(inv), "installed": [inventory.describe(inv, i) for i in bands["installed"]],
                "available": [inventory.describe(inv, i) for i in bands["available"]]}

    def _item(self, item_id: str, installed: bool = False) -> model.Item:
        it = self.inventory().items.get(item_id)
        if it is None:
            raise ConnectorError("That item is not there any more: look again.", "gone")
        if it.installed != installed:
            raise ConnectorError("Only an item that is not installed yet can be looked at or installed here." if not installed else "That item is not installed.", "installed")
        return it

    def _server(self, it: model.Item) -> Dict[str, Any]:
        """The listing of a registry server, asked again by name."""
        name = it.id[len("registry:"):]
        try:
            data = json.loads(self._get(it.source.rstrip("/") + "/v0/servers?limit=100&search=" + name.rsplit("/", 1)[-1]).decode("utf-8", "replace"))
        except (ValueError, OSError):
            raise ConnectorError("The registry did not answer.", "offline") from None
        rows = [(r.get("server", r) if isinstance(r, dict) else {}) for r in (data.get("servers") if isinstance(data, dict) else None) or []]
        server = next((s for s in rows if s.get("name") == name), None)
        if server is None:
            raise ConnectorError("The registry no longer lists this server.", "gone")
        return server

    @staticmethod
    def _github_parts(it: model.Item) -> Dict[str, str]:
        """`github:owner/repo:folder` and the source `github.com/owner/repo@pin`."""
        _, _, rest = it.id.partition(":")
        repo, _, folder = rest.partition(":")
        pin = it.source.rpartition("@")[2] if "@" in it.source else ""
        return {"repo": repo, "path": folder, "pin": pin, "name": it.name, "description": it.description}

    @staticmethod
    def _skill_excerpts(folder: Path) -> str:
        parts: List[str] = []
        for f in [folder / "SKILL.md"] + sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".py", ".js", ".sh", ".ps1", ".bat"))[:3]:
            try:
                parts.append(f"--- {f.relative_to(folder).as_posix()}\n" + f.read_text(encoding="utf-8", errors="replace")[:1500])
            except OSError:
                continue
        return "\n".join(parts)

    def facts(self, item_id: str) -> Dict[str, Any]:
        """The facts sheet and the excerpts a review reads, for an item a provider lists: a server of a registry, a skill in a folder or a skill in a GitHub repository. Nothing is run."""
        it = self._item(item_id)
        if item_id.startswith("registry:") and self._get is not None:
            return {"sheet": facts.from_server(self._server(it)), "excerpts": ""}
        if item_id.startswith("folder:"):
            folder = Path(it.source) / item_id[len("folder:"):]
            if not folder.is_dir() or folder.resolve().parent != Path(it.source).resolve():
                raise ConnectorError("That folder is not there any more.", "gone")
            return {"sheet": facts.from_skill(folder), "excerpts": self._skill_excerpts(folder), "folder": str(folder)}
        if item_id.startswith("github:") and self.installer is not None:
            parts = self._github_parts(it)
            with tempfile.TemporaryDirectory(prefix="tadamoo-skill-") as tmp:
                try:
                    folder = self.installer.fetch_skill(parts["repo"], parts["path"], parts["pin"], Path(tmp))
                except Exception as e:                         # a repository that does not answer is told, not raised
                    raise ConnectorError(f"GitHub did not give the files: {str(e)[:160]}", "offline") from None
                return {"sheet": {**facts.from_skill(folder), "name": it.name}, "excerpts": self._skill_excerpts(folder)}
        raise ConnectorError("This kind of item cannot be looked at yet.", "kind")

    def _skill_folder(self, item_id: str) -> Any:
        """A context manager yielding the skill's folder on disk for `item_id` ("folder:" or "github:"), or None for a kind NVIDIA's reviewer cannot read
        (an MCP server); a "folder:" item's folder outlives the call, a "github:" one is fetched again into a temporary folder that is cleaned up after."""
        import contextlib
        it = self._item(item_id)
        if item_id.startswith("folder:"):
            folder = Path(it.source) / item_id[len("folder:"):]
            return contextlib.nullcontext(folder if folder.is_dir() else None)
        if item_id.startswith("github:") and self.installer is not None:
            parts = self._github_parts(it)

            @contextlib.contextmanager
            def fetch():
                with tempfile.TemporaryDirectory(prefix="tadamoo-skill-") as tmp:
                    try:
                        yield self.installer.fetch_skill(parts["repo"], parts["path"], parts["pin"], Path(tmp))
                    except Exception:
                        yield None
            return fetch()
        return contextlib.nullcontext(None)

    def install(self, item_id: str, flags: Dict[str, Any]) -> Dict[str, Any]:
        """Installs an item a provider lists. The person's press of the button is the authorisation; `flags` may hold allow_scripts, anyway and update."""
        if self.installer is None:
            raise ConnectorError("Installing is not available here.", "none")
        it = self._item(item_id)
        known = {k: flags.get(k) is True for k in ("allow_scripts", "anyway", "update")}
        try:
            if item_id.startswith("registry:"):
                record = self.installer.install("server", {"server": self._server(it)}, known)
            elif item_id.startswith("folder:"):
                folder = Path(it.source) / item_id[len("folder:"):]
                if not folder.is_dir() or folder.resolve().parent != Path(it.source).resolve():
                    raise ConnectorError("That folder is not there any more.", "gone")
                record = self.installer.install("folder_skill", {"folder": str(folder)}, known)
            elif item_id.startswith("github:"):
                record = self.installer.install("github_skill", self._github_parts(it), known)
            else:
                raise ConnectorError("This kind of item cannot be installed yet.", "kind")
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "install") from None
        return {"installed": record}

    def uninstall(self, item_id: str) -> Dict[str, Any]:
        """Removes what TaDaMoo installed (its folder and its record). Something that was not installed from here is left alone."""
        if self.installer is None:
            raise ConnectorError("Removing is not available here.", "none")
        self._item(item_id, installed=True)
        try:
            gone = self.installer.uninstall(item_id)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), "uninstall") from None
        if not gone:
            raise ConnectorError("TaDaMoo did not install that one, so it is not removed from here.", "foreign")
        return {"removed": item_id}

    def config(self, item_id: str, account: str = "") -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Setting up is not available here.", "none")
        try:
            return self.installer.config(item_id, account)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "config") from None

    def configure(self, item_id: str, values: Any, account: str = "") -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Setting up is not available here.", "none")
        try:
            return self.installer.configure(item_id, values, account)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "config") from None

    def configure_ask(self, item_id: str, question: Any, history: Any = None, account: str = "") -> Dict[str, Any]:
        """A guided answer about filling in this item's settings: the model sees only the field names (never a value) and nothing of this chat is kept once it answers."""
        if self.installer is None:
            raise ConnectorError("Setting up is not available here.", "none")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ConnectorError("question: a text of 1 to 2000 characters", "bad_question")
        if history is not None and not isinstance(history, list):
            raise ConnectorError("history: a list", "bad_question")
        cfg = self.config(item_id, account)
        client = None
        try:
            client = self.reviewer() if self.reviewer else None
        except Exception:                                      # no model to ask: the person is told plainly, nothing crashes
            client = None
        from tadamoo.connectors import configure_help
        return {"answer": configure_help.ask(client, cfg.get("name") or item_id, cfg["variables"], question, history or [])}

    def accounts(self, item_id: str) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Accounts are not available here.", "none")
        try:
            return self.installer.accounts(item_id)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "account") from None

    def add_account(self, item_id: str, name: str) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Accounts are not available here.", "none")
        try:
            return self.installer.add_account(item_id, name)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "account") from None

    def remove_account(self, item_id: str, name: str) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Accounts are not available here.", "none")
        try:
            return self.installer.remove_account(item_id, name)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "account") from None

    def set_default_account(self, item_id: str, name: str) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Accounts are not available here.", "none")
        try:
            return self.installer.set_default_account(item_id, name)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "account") from None

    def updates(self) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Updates are not available here.", "none")
        return self.installer.updates()

    def update(self, item_id: str, flags: Dict[str, Any]) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Updates are not available here.", "none")
        try:
            return {"installed": self.installer.update(item_id, {k: flags.get(k) is True for k in ("allow_scripts", "anyway")})}
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "install") from None

    def update_plan(self, item_id: str) -> Dict[str, Any]:
        """What updating this GitHub skill would do, file by file (tadamoo/connectors/merge3.py), plus the advisory review of what would actually
        change (never of the files that stay the same or that only the person touched)."""
        if self.installer is None:
            raise ConnectorError("Updates are not available here.", "none")
        try:
            got = self.installer.update_plan(item_id)
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "merge") from None
        client = None
        try:
            client = self.reviewer() if self.reviewer else None
        except Exception:
            client = None
        got["review"] = review_mod.review_update(client, got["id"], got.pop("changed_text"))
        return got

    def apply_update(self, item_id: str, resolutions: Any) -> Dict[str, Any]:
        if self.installer is None:
            raise ConnectorError("Updates are not available here.", "none")
        try:
            return self.installer.apply_update(item_id, resolutions if isinstance(resolutions, dict) else {})
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "merge") from None

    def folder_settings(self, req: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Where the downloaded originals are kept, and which review(s) run before an install; any of `originals_dir`, `review_mode`,
        `review_tier3_docker` in `req` changes that one, the rest stay as they were."""
        if self.installer is None:
            raise ConnectorError("There is no folder for downloads here.", "none")
        try:
            keys = ("originals_dir", "review_mode", "review_tier3_docker")
            return self.installer.settings.write(**{k: req[k] for k in keys if req and k in req}) if req and any(k in req for k in keys) \
                else self.installer.settings.read()
        except ConnectorInstallError as e:
            raise ConnectorError(str(e), e.code or "bad_folder") from None

    def github_token_status(self) -> Dict[str, Any]:
        """Whether a GitHub token is in the vault (never the value itself): it raises the rate limit of browsing GitHub as a provider, and reaches the
        private repositories that token can see. Set from Connectors > Providers; read by tadamoo.discovery.installed.github_token."""
        vault_dir = self.installer.vault_dir if self.installer is not None else None
        if not vault_dir:
            return {"set": False}
        from tadamoo.vault import Vault, VaultError
        try:
            Vault(vault_dir).describe("github")
            return {"set": True}
        except VaultError:
            return {"set": False}

    def set_github_token(self, token: Any) -> Dict[str, Any]:
        if self.installer is None or not self.installer.vault_dir:
            raise ConnectorError("There is no vault here.", "none")
        if not isinstance(token, str):
            raise ConnectorError("token: text", "bad_token")
        from tadamoo.vault import Vault
        v, clean = Vault(self.installer.vault_dir), token.strip()
        if clean:
            v.put("github", kind="secret", label="GitHub token (raises the rate limit; also reaches the private repositories it can see)", fields={"value": clean})
        else:
            v.delete("github")
        return self.github_token_status()

    def nvidia_status(self) -> Dict[str, Any]:
        from tadamoo.skillreview_nvidia import shared as nvidia_shared
        rt = nvidia_shared()
        return {**rt.info(), "version": rt.version() if rt.installed() else "", "docker": _has_docker()}

    def nvidia_install(self) -> Dict[str, Any]:
        from tadamoo.skillreview_nvidia import SkillEvaluatorError, shared as nvidia_shared
        rt = nvidia_shared()
        try:
            rt.install()
        except SkillEvaluatorError as e:
            raise ConnectorError(str(e), "nvidia_install") from None
        return self.nvidia_status()

    def review(self, item_id: str) -> Dict[str, Any]:
        """The facts sheet (counted by a program) and the review(s): our own (advice from a model, kept nowhere) and, when Settings > Connectors asks
        for it, NVIDIA SkillEvaluator's static quality/security check (Tier 1, and Tier 3 when that is on and Docker is found)."""
        got = self.facts(item_id)
        client = None
        try:
            client = self.reviewer() if self.reviewer else None
        except Exception:                                      # no model to ask: the facts stand alone
            client = None
        out: Dict[str, Any] = {"id": item_id, "facts": got["sheet"]}
        mode = self.installer.settings.read()["review_mode"] if self.installer is not None else "ours"
        if mode in ("ours", "both"):
            out["review"] = review_mod.review(client, got["sheet"], got["excerpts"])
        if mode in ("nvidia", "both"):
            out["nvidia_review"] = self._nvidia_review(item_id)
        return out

    def _nvidia_review(self, item_id: str) -> Dict[str, Any]:
        from tadamoo.skillreview_nvidia import SkillEvaluatorError, shared as nvidia_shared
        rt = nvidia_shared()
        if not rt.installed():
            return {"available": False, "error": "NVIDIA SkillEvaluator is not installed (Settings > Connectors)."}
        with self._skill_folder(item_id) as folder:
            if folder is None:
                return {"available": False, "error": "This kind of item has no skill folder for NVIDIA SkillEvaluator to read."}
            docker = bool(self.installer and self.installer.settings.read()["review_tier3_docker"] and _has_docker())
            try:
                report = rt.validate(folder, "1,2,3" if docker else "1") if docker else rt.quality_check(folder)
            except SkillEvaluatorError as e:
                return {"available": False, "error": str(e)[:600]}
        return {"available": True, "tier3": docker, "report": report}

    def item(self, item_id: str) -> Dict[str, Any]:
        inv = self.inventory()
        if item_id not in inv.items:
            raise ConnectorError("That item is not there any more: look again.", "gone")
        it = inv.items[item_id]
        out = inventory.describe(inv, it)
        out["tree"] = [inventory.describe(inv, d) for d in inv.descendants(item_id)]
        plan = inv.removal_plan(item_id)
        out["removal"] = {"removed": [i.id for i in plan["removed"]], "kept": [i.id for i in plan["kept"]]}
        return out
