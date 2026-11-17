"""The read side of the Connectors tab.

    GET /api/connectors?scope=all|personal|external&kind=&state=&q=   the counts and the two bands (installed, available) of a sub-tab
    GET /api/connectors/item?id=                                      one item with its relations and, for a plugin, its tree
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from tadamoo.connectors import inventory, model, providers as prov
from tadamoo.connectors.model import Inventory


class ConnectorError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ConnectorsApi:
    def __init__(self, entries: Any, personal: Any, providers: Any = None, get: Any = None) -> None:
        """`entries` returns the entries of the capability index, `personal` the personal skills (and the slugs switched off); both are called on every request. `providers` is the
        list the person set (tadamoo/connectors/providers.py) and `get` fetches an address: with them the "available" band is made from the enabled providers."""
        self._entries = entries
        self._personal = personal
        self.providers, self._get = providers, get

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
