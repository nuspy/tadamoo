"""The read side of the Connectors tab.

    GET /api/connectors?scope=all|personal|external&kind=&state=&q=   the counts and the two bands (installed, available) of a sub-tab
    GET /api/connectors/item?id=                                      one item with its relations and, for a plugin, its tree
    POST /api/connectors/review  {id}                                 the facts sheet of an item that is not installed yet and the advisory review of a model
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.connectors import facts, inventory, model, providers as prov, review as review_mod
from tadamoo.connectors.model import Inventory


class ConnectorError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ConnectorsApi:
    def __init__(self, entries: Any, personal: Any, providers: Any = None, get: Any = None, reviewer: Optional[Callable[[], Any]] = None) -> None:
        """`entries` returns the entries of the capability index, `personal` the personal skills (and the slugs switched off); both are called on every request. `providers` is the
        list the person set (tadamoo/connectors/providers.py) and `get` fetches an address: with them the "available" band is made from the enabled providers."""
        self._entries = entries
        self._personal = personal
        self.providers, self._get = providers, get
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

    def facts(self, item_id: str) -> Dict[str, Any]:
        """The facts sheet and the excerpts a review reads, for an item a provider lists: a server of a registry or a skill in a folder. Nothing is downloaded or run."""
        inv = self.inventory()
        it = inv.items.get(item_id)
        if it is None:
            raise ConnectorError("That item is not there any more: look again.", "gone")
        if it.installed:
            raise ConnectorError("Only an item that is not installed yet can be looked at here.", "installed")
        if item_id.startswith("registry:") and self._get is not None:
            name = item_id[len("registry:"):]
            try:
                data = json.loads(self._get(it.source.rstrip("/") + "/v0/servers?limit=100&search=" + name.rsplit("/", 1)[-1]).decode("utf-8", "replace"))
            except (ValueError, OSError):
                raise ConnectorError("The registry did not answer.", "offline") from None
            rows = [(r.get("server", r) if isinstance(r, dict) else {}) for r in (data.get("servers") if isinstance(data, dict) else None) or []]
            server = next((s for s in rows if s.get("name") == name), None)
            if server is None:
                raise ConnectorError("The registry no longer lists this server.", "gone")
            return {"sheet": facts.from_server(server), "excerpts": ""}
        if item_id.startswith("folder:"):
            folder = Path(it.source) / item_id[len("folder:"):]
            if not folder.is_dir() or folder.resolve().parent != Path(it.source).resolve():
                raise ConnectorError("That folder is not there any more.", "gone")
            parts: List[str] = []
            for f in [folder / "SKILL.md"] + sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".py", ".js", ".sh", ".ps1", ".bat"))[:3]:
                try:
                    parts.append(f"--- {f.relative_to(folder).as_posix()}\n" + f.read_text(encoding="utf-8", errors="replace")[:1500])
                except OSError:
                    continue
            return {"sheet": facts.from_skill(folder), "excerpts": "\n".join(parts)}
        raise ConnectorError("This kind of item cannot be looked at yet.", "kind")

    def review(self, item_id: str) -> Dict[str, Any]:
        """The facts sheet (counted by a program) and the review (advice from a model, kept nowhere)."""
        got = self.facts(item_id)
        client = None
        try:
            client = self.reviewer() if self.reviewer else None
        except Exception:                                      # no model to ask: the facts stand alone
            client = None
        return {"id": item_id, "facts": got["sheet"], "review": review_mod.review(client, got["sheet"], got["excerpts"])}

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
