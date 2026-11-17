"""The read side of the Connectors tab.

    GET /api/connectors?scope=all|personal|external&kind=&state=&q=   the counts and the two bands (installed, available) of a sub-tab
    GET /api/connectors/item?id=                                      one item with its relations and, for a plugin, its tree
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from tadamoo.connectors import inventory, model
from tadamoo.connectors.model import Inventory


class ConnectorError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ConnectorsApi:
    def __init__(self, entries: Any, personal: Any) -> None:
        """`entries` returns the entries of the capability index, `personal` the personal skills (and the slugs switched off); both are called on every request."""
        self._entries = entries
        self._personal = personal

    def inventory(self) -> Inventory:
        skills, off = self._personal()
        return inventory.build(self._entries(), skills, off)

    def view(self, scope: str = "all", kind: str = "", state: str = "", query: str = "") -> Dict[str, Any]:
        inv = self.inventory()
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
