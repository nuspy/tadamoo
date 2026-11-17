"""The inventory of the Connectors tab: skills, personal skills, MCP servers, MCP apps and plugins as one graph, with the state the person sees and the short card the model sees.

An item has its own switch (`enabled`) and what it needs (`needs_config`, `error`). What it is *effectively* is derived from its parents: a child of a plugin that is off is inactive, and
keeps its own switch, so that turning the plugin on again gives back what the person had set. An item with several parents is active while one of them is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

KINDS = ("skill", "personal_skill", "mcp_server", "mcp_app", "plugin")
SCOPES = ("personal", "external")                     # personal: made by TaDaMoo with "Make a skill"; external: from a provider, a folder or the system
DESC_CHARS = 140


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    name: str
    description: str = ""
    keywords: Tuple[str, ...] = ()
    source: str = ""                                 # the provider or folder it comes from, or "personal"
    scope: str = "external"
    installed: bool = False
    enabled: bool = True
    needs_config: bool = False
    error: str = ""
    version: str = ""
    update_available: bool = False
    parents: Tuple[str, ...] = ()
    children: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind: one of {', '.join(KINDS)}")
        if self.scope not in SCOPES:
            raise ValueError(f"scope: one of {', '.join(SCOPES)}")


def effective_states() -> Tuple[str, ...]:
    return ("not_installed", "error", "needs_config", "disabled", "inactive", "active")


class Inventory:
    """A set of items with the relations between them. The relations are read from both sides: a plugin that names a skill as its child makes the plugin the skill's parent."""

    def __init__(self, items: Iterable[Item]) -> None:
        self.items: Dict[str, Item] = {}
        for it in items:
            if it.id in self.items:
                raise ValueError(f"two items have the id {it.id}")
            self.items[it.id] = it
        self._parents: Dict[str, List[str]] = {i: list(it.parents) for i, it in self.items.items()}
        self._children: Dict[str, List[str]] = {i: list(it.children) for i, it in self.items.items()}
        for i, it in self.items.items():
            for c in it.children:
                if c in self.items and i not in self._parents[c]:
                    self._parents[c].append(i)
            for p in it.parents:
                if p in self.items and i not in self._children[p]:
                    self._children[p].append(i)
        for rel in (self._parents, self._children):
            for i in rel:
                rel[i] = [x for x in rel[i] if x in self.items]

    def parents_of(self, item_id: str) -> List[Item]:
        return [self.items[p] for p in self._parents.get(item_id, [])]

    def children_of(self, item_id: str) -> List[Item]:
        return [self.items[c] for c in self._children.get(item_id, [])]

    def roots(self) -> List[Item]:
        return [it for i, it in self.items.items() if not self._parents[i]]

    def descendants(self, item_id: str) -> List[Item]:
        out: List[Item] = []
        seen = {item_id}
        stack = [item_id]
        while stack:
            for c in self._children.get(stack.pop(), []):
                if c not in seen:
                    seen.add(c)
                    out.append(self.items[c])
                    stack.append(c)
        return out

    def own_state(self, item_id: str) -> str:
        """What the item is by itself, whatever its parents are: not_installed, error, needs_config, disabled or active."""
        it = self.items[item_id]
        if not it.installed:
            return "not_installed"
        if it.error:
            return "error"
        if it.needs_config:
            return "needs_config"
        return "active" if it.enabled else "disabled"

    def state(self, item_id: str, _path: Tuple[str, ...] = ()) -> str:
        """What the item is now: not_installed, error, needs_config, disabled (its own switch), inactive (a parent is not active) or active."""
        own = self.own_state(item_id)
        if own != "active":
            return own
        parents = [p for p in self._parents[item_id] if p not in _path]
        if parents and not any(self.state(p, _path + (item_id,)) == "active" for p in parents):
            return "inactive"
        return "active"

    def why_inactive(self, item_id: str) -> str:
        """The sentence that says why a child is inactive, naming its parents."""
        it = self.items[item_id]
        if self.state(item_id) != "inactive":
            return ""
        names = ", ".join(f"«{p.name}»" for p in self.parents_of(item_id) if self.state(p.id) != "active")
        return f"«{it.name}» is switched on, but {names} {'is' if ',' not in names else 'are'} not active: it works again when {'it is' if ',' not in names else 'one of them is'} active."

    def tristate(self, item_id: str) -> str:
        """For a parent: on (every child is active), off (none is) or partial; an item without children is on or off by its own state."""
        kids = self._children[item_id]
        if not kids:
            return "on" if self.state(item_id) == "active" else "off"
        if self.own_state(item_id) != "active":
            return "off"
        active = [self.own_state(c) == "active" for c in kids]                         # the children's own switches: another parent does not make this one look on
        return "on" if all(active) else ("off" if not any(active) else "partial")

    def used_by(self, item_id: str) -> List[Item]:
        """The parents that use the item; more than one is what the tree writes as «also used by»."""
        return self.parents_of(item_id)

    def removal_plan(self, item_id: str) -> Dict[str, List[Item]]:
        """What removing the item does to its children: `removed` are the ones nobody else uses, `kept` are used by another parent that stays."""
        removed: List[Item] = []
        kept: List[Item] = []
        gone = {item_id}
        for d in self.descendants(item_id):
            others = [p for p in self._parents[d.id] if p not in gone]
            (kept if others else removed).append(d)
            if not others:
                gone.add(d.id)
        return {"removed": removed, "kept": kept}


def matches(item: Item, query: str) -> bool:
    """The single filter: every word of the query is in the name, the keywords, the description, the source or the kind (letters only, any case)."""
    hay = " ".join((item.name, " ".join(item.keywords), item.description, item.source, item.kind.replace("_", " "))).lower()
    return all(w in hay for w in re.findall(r"\w+", (query or "").lower()))


def view(inv: Inventory, kind: Optional[str] = None, scope: str = "all", query: str = "", state: Optional[str] = None) -> Dict[str, List[Item]]:
    """The two bands of a sub-tab: `installed` and `available`, after the scope switch (personal, external or all), the kind of the sub-tab, the state filter and the text filter."""
    if scope not in ("all", *SCOPES):
        raise ValueError("scope: all, personal or external")
    out: Dict[str, List[Item]] = {"installed": [], "available": []}
    for it in inv.items.values():
        if (kind and it.kind != kind) or (scope != "all" and it.scope != scope) or not matches(it, query):
            continue
        if state and inv.state(it.id) != state:
            continue
        out["installed" if it.installed else "available"].append(it)
    for band in out.values():
        band.sort(key=lambda i: (i.name.lower(), i.id))
    return out


def counts(inv: Inventory) -> Dict[str, int]:
    """The numbers beside the scope switch."""
    return {"all": len(inv.items), **{s: sum(1 for i in inv.items.values() if i.scope == s) for s in SCOPES}}


def card(item: Item) -> str:
    """What the model is told about an active item: the name, the keywords and one line. Nothing else enters its context until it uses the item."""
    line = " ".join(item.description.split())[:DESC_CHARS]
    keys = ", ".join(item.keywords[:6])
    return f"{item.name}" + (f" [{keys}]" if keys else "") + (f": {line}" if line else "")


def session_index(inv: Inventory, budget_chars: int, pinned: Sequence[str] = (), extra: Sequence[str] = ()) -> Tuple[List[str], int]:
    """The cards of the items that are active, pinned first, within a budget; the second value is how many were left out (found by `find_capability`). `extra` adds items switched on for this conversation only."""
    active = [it for it in inv.items.values() if (inv.state(it.id) == "active" or it.id in extra and it.installed)]
    active.sort(key=lambda i: (i.id not in pinned, i.id not in extra, i.name.lower()))
    cards: List[str] = []
    used = 0
    for it in active:
        c = card(it)
        if used + len(c) + 1 > budget_chars and cards:
            break
        cards.append(c)
        used += len(c) + 1
    return cards, len(active) - len(cards)
