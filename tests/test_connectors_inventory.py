"""The inventory read from the capability index and the personal skills, and the read API of the Connectors tab."""
from __future__ import annotations

import pytest

from serve.connectors_api import ConnectorError, ConnectorsApi
from tadamoo.connectors import inventory

ENTRIES = [
    {"id": "builtin:wallet", "name": "Wallet", "kind": "builtin", "origin": "builtin", "description": "", "tags": [], "state": "approved"},
    {"id": "skill:pdf", "name": "pdf", "kind": "skill", "origin": "user", "description": "Read and fill PDF forms", "tags": ["pdf", "forms"], "state": "approved"},
    {"id": "skill:docx", "name": "docx", "kind": "skill", "origin": "plugin:office", "description": "Word files", "tags": ["word"], "state": "approved"},
    {"id": "skill:xlsx", "name": "xlsx", "kind": "skill", "origin": "plugin:office", "description": "Excel files", "tags": ["excel"], "state": "off"},
    {"id": "mcp:disk", "name": "disk", "kind": "mcp", "origin": "claude_desktop", "description": "Disk usage", "tags": [], "state": "off"},
]
PERSONAL = [{"slug": "daily-report", "name": "Daily report", "description": "Collects the day", "problems": []},
            {"slug": "broken", "name": "Broken", "description": "", "problems": [{"message": "uses a tool that is not there"}]}]


def api(off=()):
    return ConnectorsApi(lambda: ENTRIES, lambda: (PERSONAL, off))


def test_built_in_capabilities_are_not_connectors_and_a_plugin_is_made_from_its_skills():
    inv = inventory.build(ENTRIES, PERSONAL)
    assert "builtin:wallet" not in inv.items
    plugin = inv.items["plugin:office"]
    assert plugin.kind == "plugin" and set(plugin.children) == {"skill:docx", "skill:xlsx"}
    assert [p.id for p in inv.parents_of("skill:docx")] == ["plugin:office"]
    assert inv.items["mcp:disk"].kind == "mcp_server" and inv.items["personal:daily-report"].scope == "personal"


def test_states_follow_the_switches_and_a_plugin_is_partial_when_one_skill_is_off():
    inv = inventory.build(ENTRIES, PERSONAL, personal_off=["daily-report"])
    assert inv.state("skill:pdf") == "active" and inv.state("skill:xlsx") == "disabled" and inv.state("mcp:disk") == "disabled"
    assert inv.state("personal:daily-report") == "disabled" and inv.state("personal:broken") == "error"
    assert inv.tristate("plugin:office") == "partial"


def test_the_view_counts_filters_and_splits_by_scope():
    got = api().view()
    assert got["counts"] == {"all": 7, "personal": 2, "external": 5} and got["available"] == []
    assert {i["id"] for i in api().view(scope="personal")["installed"]} == {"personal:daily-report", "personal:broken"}
    assert [i["id"] for i in api().view(kind="mcp_server")["installed"]] == ["mcp:disk"]
    assert [i["id"] for i in api().view(query="excel")["installed"]] == ["skill:xlsx"]
    assert [i["id"] for i in api().view(state="error")["installed"]] == ["personal:broken"]


def test_an_item_gives_its_tree_and_what_removing_it_would_do():
    got = api().item("plugin:office")
    assert {t["id"] for t in got["tree"]} == {"skill:docx", "skill:xlsx"} and got["tri"] == "partial"
    assert sorted(got["removal"]["removed"]) == ["skill:docx", "skill:xlsx"] and got["removal"]["kept"] == []
    child = api().item("skill:xlsx")
    assert child["parents"] == ["plugin:office"] and child["state"] == "disabled" and child["why"] == ""


@pytest.mark.parametrize("args", [{"kind": "robot"}, {"state": "asleep"}, {"scope": "everyone"}])
def test_a_wrong_filter_is_refused_with_its_name(args):
    with pytest.raises(ConnectorError):
        api().view(**args)


def test_an_unknown_item_is_gone():
    with pytest.raises(ConnectorError) as e:
        api().item("skill:nothing")
    assert e.value.code == "gone"


def test_the_skills_of_a_hidden_folder_are_not_a_plugin():
    junk = {"id": "skill:old", "name": "old", "kind": "skill", "origin": "plugin:.trash", "description": "", "tags": [], "state": "approved"}
    inv = inventory.build([junk, *ENTRIES])
    assert "skill:old" not in inv.items and "plugin:.trash" not in inv.items
