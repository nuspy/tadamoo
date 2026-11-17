"""The inventory of the Connectors tab: states derived from parents, the tri-state of a plugin, what removing it does, the single filter, the two bands, the cards the model sees."""
import pytest

from tadamoo.connectors import model as m
from tadamoo.connectors.model import Inventory, Item


def inventory(**over):
    plugin = Item("plugin:news", "plugin", "News research", "Reads the news", ("news",), "providerA", installed=True, children=("mcp:feeds", "skill:summarise", "skill:shared"))
    feeds = Item("mcp:feeds", "mcp_server", "Feeds", "RSS reader", ("rss",), "providerA", installed=True)
    summ = Item("skill:summarise", "skill", "Summarise", "Short summaries", ("summary",), "providerA", installed=True)
    shared = Item("skill:shared", "skill", "Shared helper", "Used by two plugins", (), "providerA", installed=True)
    other = Item("plugin:mail", "plugin", "Mail", "Mail tools", ("mail",), "providerB", installed=True, children=("skill:shared",))
    mine = Item("pskill:weekly", "personal_skill", "Weekly report", "Made from my requests", ("report",), "personal", scope="personal", installed=True)
    free = Item("mcp:calendar", "mcp_server", "Calendar", "Calendar server", ("calendar",), "registry", installed=False)
    items = [plugin, feeds, summ, shared, other, mine, free]
    return Inventory([replace_item(i, over.get(i.id, {})) for i in items])


def replace_item(item, changes):
    from dataclasses import replace
    return replace(item, **changes) if changes else item


def test_a_child_of_a_plugin_that_is_off_is_inactive_and_keeps_its_own_switch():
    inv = inventory(**{"plugin:news": {"enabled": False}})
    assert inv.state("plugin:news") == "disabled"
    assert inv.state("mcp:feeds") == "inactive" and inv.items["mcp:feeds"].enabled
    assert "News research" in inv.why_inactive("mcp:feeds")
    assert inventory().state("mcp:feeds") == "active"


def test_an_item_with_two_parents_is_active_while_one_of_them_is():
    inv = inventory(**{"plugin:news": {"enabled": False}})
    assert inv.state("skill:shared") == "active" and inv.state("skill:summarise") == "inactive"
    both = inventory(**{"plugin:news": {"enabled": False}, "plugin:mail": {"enabled": False}})
    assert both.state("skill:shared") == "inactive" and "are not active" in both.why_inactive("skill:shared")
    assert [p.name for p in inv.used_by("skill:shared")] == ["News research", "Mail"]


def test_the_state_says_what_the_item_needs_before_anything_else():
    inv = inventory(**{"mcp:feeds": {"needs_config": True}, "skill:summarise": {"error": "script missing"}})
    assert inv.state("mcp:feeds") == "needs_config" and inv.state("skill:summarise") == "error" and inv.state("mcp:calendar") == "not_installed"
    assert inv.state("pskill:weekly") == "active"


def test_a_parent_is_on_off_or_partial_by_its_children():
    assert inventory().tristate("plugin:news") == "on"
    assert inventory(**{"skill:summarise": {"enabled": False}}).tristate("plugin:news") == "partial"
    assert inventory(**{"plugin:news": {"enabled": False}}).tristate("plugin:news") == "off"


def test_removing_a_plugin_keeps_what_another_plugin_uses_and_says_so():
    plan = inventory().removal_plan("plugin:news")
    assert sorted(i.id for i in plan["removed"]) == ["mcp:feeds", "skill:summarise"] and [i.id for i in plan["kept"]] == ["skill:shared"]
    mail = inventory().removal_plan("plugin:mail")
    assert mail["removed"] == [] and [i.id for i in mail["kept"]] == ["skill:shared"]            # the news plugin still uses it


def test_the_relations_are_read_from_both_sides_and_a_cycle_does_not_hang():
    a = Item("a", "plugin", "A", installed=True, children=("b",))
    b = Item("b", "skill", "B", installed=True, parents=("a",), children=("a",))
    inv = Inventory([a, b])
    assert [p.id for p in inv.parents_of("b")] == ["a"] and inv.state("b") in ("active", "inactive")
    with pytest.raises(ValueError):
        Inventory([a, a])


def test_the_two_bands_the_scope_switch_and_the_single_filter():
    inv = inventory()
    everything = m.view(inv, kind="mcp_server")
    assert [i.name for i in everything["installed"]] == ["Feeds"] and [i.name for i in everything["available"]] == ["Calendar"]
    assert [i.name for i in m.view(inv, kind="personal_skill", scope="personal")["installed"]] == ["Weekly report"]
    assert m.view(inv, kind="skill", scope="personal")["installed"] == []
    assert [i.name for i in m.view(inv, query="RSS reader")["installed"]] == ["Feeds"]
    assert [i.name for i in m.view(inv, query="providerb")["installed"]] == ["Mail"]
    assert [i.name for i in m.view(inv, state="not_installed")["available"]] == ["Calendar"]
    assert m.counts(inv) == {"all": 7, "personal": 1, "external": 6}
    with pytest.raises(ValueError):
        m.view(inv, scope="mine")


def test_the_model_sees_one_line_for_each_active_item_within_a_budget():
    inv = inventory(**{"plugin:news": {"enabled": False}})
    cards, left = m.session_index(inv, 10_000)
    assert "Feeds: RSS reader" not in "".join(cards) and any(c.startswith("Weekly report [report]") for c in cards) and left == 0
    small, left = m.session_index(inv, 40, pinned=["pskill:weekly"])
    assert small[0].startswith("Weekly report") and left >= 1
    with_extra, _ = m.session_index(inv, 10_000, extra=["mcp:feeds"])
    assert any(c.startswith("Feeds") for c in with_extra)
    assert len(m.card(Item("x", "skill", "X", "word " * 100, ("a", "b")))) <= 140 + 20
