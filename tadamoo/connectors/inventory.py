"""The inventory of the Connectors tab read from what is on this PC: the capability index (skills, plugins' skills, MCP servers found by discovery) and the personal skills
made with "Make a skill". Nothing here writes or runs anything."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from tadamoo.connectors.model import Inventory, Item

PLUGIN_PREFIX = "plugin:"


def _words(entry: Dict[str, Any]) -> tuple:
    tags = entry.get("tags") or []
    return tuple(str(t) for t in tags if isinstance(t, str))


def _origin(entry: Dict[str, Any]) -> str:
    return str(entry.get("origin") or "")


def items_from_index(entries: Iterable[Dict[str, Any]]) -> List[Item]:
    """The items for the entries of `CapabilityIndex.entries`. A skill whose origin is `plugin:<name>` is a child of the item for that plugin, which is made here (a plugin has no entry
    of its own); a remote MCP server or a local one is an `mcp_server`; the built-in capabilities of the harness are not connectors and are left out."""
    items: List[Item] = []
    plugins: Dict[str, List[str]] = {}
    for e in entries:
        kind = e.get("kind")
        if kind == "builtin":
            continue
        enabled = e.get("state") != "off"
        origin = _origin(e)
        if origin.startswith(PLUGIN_PREFIX + "."):                 # a hidden folder (a trash, a cache) is not a plugin
            continue
        parents = ()
        if kind == "skill" and origin.startswith(PLUGIN_PREFIX):
            name = origin[len(PLUGIN_PREFIX):]
            parents = (PLUGIN_PREFIX + name,)
            plugins.setdefault(name, []).append(str(e["id"]))
        items.append(Item(id=str(e["id"]), kind="mcp_server" if kind == "mcp" else "skill", name=str(e.get("name") or e["id"]), description=str(e.get("description") or ""),
                          keywords=_words(e), source=origin, scope="external", installed=True, enabled=enabled, parents=parents))
    for name, kids in sorted(plugins.items()):
        items.append(Item(id=PLUGIN_PREFIX + name, kind="plugin", name=name, source="plugin", scope="external", installed=True, enabled=True, children=tuple(kids)))
    return items


def items_from_personal(skills: Sequence[Dict[str, Any]], off: Iterable[str] = ()) -> List[Item]:
    """The items for the personal skills (`PersonalSkills.list()`); `off` holds the slugs the person has switched off. A skill with problems in its script needs attention."""
    gone = set(off)
    return [Item(id="personal:" + str(s["slug"]), kind="personal_skill", name=str(s.get("name") or s["slug"]), description=str(s.get("description") or ""),
                 source="personal", scope="personal", installed=True, enabled=str(s["slug"]) not in gone,
                 error="; ".join(str(p.get("message", p)) for p in (s.get("problems") or [])[:2]) if s.get("problems") else "")
            for s in skills]


def build(entries: Iterable[Dict[str, Any]], personal: Sequence[Dict[str, Any]] = (), personal_off: Iterable[str] = ()) -> Inventory:
    """The inventory of everything found: external items from discovery, personal skills from their store."""
    return Inventory(items_from_index(entries) + items_from_personal(personal, personal_off))


def describe(inv: Inventory, item: Item) -> Dict[str, Any]:
    """What the tab shows for one item: its card data and, for a plugin or a child, the relations."""
    return {"id": item.id, "kind": item.kind, "name": item.name, "description": item.description, "keywords": list(item.keywords), "source": item.source, "scope": item.scope,
            "installed": item.installed, "state": inv.state(item.id), "tri": inv.tristate(item.id), "version": item.version, "update_available": item.update_available,
            "children": [c.id for c in inv.children_of(item.id)], "parents": [p.id for p in inv.parents_of(item.id)],
            "why": inv.why_inactive(item.id), "used_by": [p.name for p in inv.used_by(item.id)] if len(inv.parents_of(item.id)) > 1 else []}
