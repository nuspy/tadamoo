"""The providers of the Connectors tab: the list the person edits, the registry and folder adapters, and the available band made from them."""
import json

import pytest

from serve.connectors_api import ConnectorError, ConnectorsApi
from tadamoo.connectors import providers as prov

REGISTRY = {"servers": [
    {"server": {"name": "io.github.acme/weather", "description": "Weather   forecasts\nfor any city", "version": "1.2.0"}},
    {"server": {"name": "io.github.acme/mail", "description": "Read and send mail", "version": "0.3.1"}},
    {"server": {"name": ""}}, "not a row"], "metadata": {}}


def get_for(payload):
    seen = []

    def get(url):
        seen.append(url)
        if isinstance(payload, Exception):
            raise payload
        return payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    get.seen = seen
    return get


def test_the_list_starts_with_the_official_registry_and_can_be_added_to_switched_off_removed_and_reset(tmp_path):
    p = prov.Providers(tmp_path / "p.json")
    assert [r["id"] for r in p.list()] == ["mcp-registry"] and p.list()[0]["trust"] == "official"
    folder = tmp_path / "skills"
    folder.mkdir()
    rows = p.add("folder", str(folder), "known")
    assert len(rows) == 2 and rows[1]["kind"] == "folder" and rows[1]["enabled"]
    with pytest.raises(prov.ProviderError, match="already"):
        p.add("folder", str(folder))
    for bad in (("robot", "x"), ("folder", str(tmp_path / "none")), ("mcp_registry", "ftp://x"), ("mcp_registry", ""), ("folder", str(folder), "doubtful")):
        with pytest.raises(prov.ProviderError):
            p.add(*bad)
    assert p.set_enabled(rows[1]["id"], False)[1]["enabled"] is False and [r["id"] for r in prov.Providers(tmp_path / "p.json").list()] == ["mcp-registry", rows[1]["id"]]
    assert [r["id"] for r in p.remove(rows[1]["id"])] == ["mcp-registry"]
    with pytest.raises(prov.ProviderError):
        p.remove("nothing")
    p.add("mcp_registry", "https://reg.example")
    assert [r["id"] for r in p.reset()] == ["mcp-registry"]
    (tmp_path / "p.json").write_text("garbage", encoding="utf-8")
    assert [r["id"] for r in p.list()] == ["mcp-registry"]                                         # a damaged file gives the defaults, not a crash


def test_a_registry_gives_servers_that_are_not_installed_and_a_bad_answer_gives_none():
    got = prov.registry_items("https://reg.example/", get_for(REGISTRY), "weather")
    assert [i.name for i in got] == ["weather", "mail"] and got[0].description == "Weather forecasts for any city" and not got[0].installed and got[0].version == "1.2.0"
    assert got[0].id == "registry:io.github.acme/weather" and got[0].kind == "mcp_server" and "weather" in got[0].keywords
    g = get_for(REGISTRY)
    prov.registry_items("https://reg.example/", g, "weather")
    assert g.seen == ["https://reg.example/v0/servers?limit=100&search=weather"]
    for bad in (b"<html>", b"[]", OSError("no network")):
        assert prov.registry_items("https://reg.example", get_for(bad)) == []


def test_a_folder_gives_its_skills_and_the_available_list_follows_the_enabled_providers_and_survives_one_that_fails(tmp_path):
    folder = tmp_path / "skills"
    (folder / "pdf-forms").mkdir(parents=True)
    (folder / "pdf-forms" / "SKILL.md").write_text("---\nname: pdf-forms\ndescription: Fill PDF forms\n---\nbody", encoding="utf-8")
    (folder / "not-a-skill").mkdir()
    assert [i.name for i in prov.folder_items(str(folder))] == ["pdf-forms"]
    rows = [{"id": "a", "kind": "mcp_registry", "ref": "https://reg.example", "enabled": True}, {"id": "b", "kind": "folder", "ref": str(folder), "enabled": True},
            {"id": "c", "kind": "folder", "ref": str(folder), "enabled": False}]
    assert sorted(i.name for i in prov.available(rows, get_for(REGISTRY))) == ["mail", "pdf-forms", "weather"]
    assert [i.name for i in prov.available(rows, get_for(OSError("down")))] == ["pdf-forms"]
    assert prov.available([], get_for(REGISTRY)) == []


def test_the_view_lists_the_available_band_from_the_providers_without_repeating_what_is_installed(tmp_path):
    p = prov.Providers(tmp_path / "p.json")
    entries = [{"id": "mcp:weather", "name": "weather", "kind": "mcp", "origin": "claude_desktop", "description": "", "tags": [], "state": "approved"}]
    api = ConnectorsApi(lambda: entries, lambda: ([], ()), p, get_for(REGISTRY))
    view = api.view(kind="mcp_server")
    assert [i["name"] for i in view["installed"]] == ["weather"] and [i["name"] for i in view["available"]] == ["mail"]          # weather is installed: listed once
    assert api.view(scope="personal")["available"] == []
    assert api.provider_rows()["providers"][0]["id"] == "mcp-registry" and "folder" in api.provider_rows()["kinds"]


def test_the_providers_are_changed_through_the_api_with_errors_named(tmp_path):
    api = ConnectorsApi(lambda: [], lambda: ([], ()), prov.Providers(tmp_path / "p.json"), get_for(REGISTRY))
    assert api.change_providers({"action": "disable", "id": "mcp-registry"})["providers"][0]["enabled"] is False
    assert api.change_providers({"action": "reset"})["providers"][0]["enabled"] is True
    for bad in ({"action": "explode"}, {"action": "remove", "id": "none"}, {"action": "add", "kind": "folder", "ref": str(tmp_path / "none")}):
        with pytest.raises(ConnectorError):
            api.change_providers(bad)
    with pytest.raises(ConnectorError):
        ConnectorsApi(lambda: [], lambda: ([], ())).change_providers({"action": "reset"})


TREE = {"tree": [{"path": "README.md"}, {"path": "skills/pdf/SKILL.md"}, {"path": "skills/docx/SKILL.md"}, {"path": "skills/docx/helper.py"}, {"path": "other/SKILL.md.bak"}]}


def github_get(pin_seen):
    def get(url):
        pin_seen.append(url)
        if url == "https://api.github.com/repos/acme/skills":
            return json.dumps({"default_branch": "main"}).encode()
        if "/git/trees/" in url:
            return json.dumps(TREE).encode()
        if url.endswith("skills/pdf/SKILL.md"):
            return b"---\nname: pdf-tools\ndescription: Read and fill PDF forms\n---\nBody"
        raise OSError("not there")
    return get


def test_a_github_repository_is_a_provider_written_owner_slash_name_with_an_optional_pin(tmp_path):
    p = prov.Providers(tmp_path / "p.json")
    assert p.add("github", "acme/skills@v1.2", "known")[-1]["ref"] == "acme/skills@v1.2"
    for bad in ("acme", "acme/skills/extra", "https://github.com/acme/skills", "acme/ skills", "-x/y"):
        with pytest.raises(prov.ProviderError):
            p.add("github", bad)


def test_the_skills_of_a_github_repository_are_listed_by_name_and_description_at_the_default_branch_or_the_pin():
    seen = []
    items = prov.github_items("acme/skills", github_get(seen))
    assert [(i.id, i.name, i.description) for i in items] == [("github:acme/skills:skills/docx", "docx", ""), ("github:acme/skills:skills/pdf", "pdf-tools", "Read and fill PDF forms")]
    assert items[0].source == "github.com/acme/skills@main" and not items[0].installed and any("/git/trees/main" in u for u in seen)
    seen.clear()
    prov.github_items("acme/skills@v1.2", github_get(seen))
    assert not any(u.endswith("/repos/acme/skills") for u in seen) and any("/git/trees/v1.2" in u for u in seen)        # a pinned version asks nothing about the default branch
    assert prov.github_items("acme/skills", get_for(OSError("down"))) == [] and prov.github_items("acme/skills", get_for(b"not json")) == []


def test_an_enabled_github_provider_adds_its_skills_to_the_available_band_and_a_failing_one_does_not_stop_the_others():
    rows = [{"id": "g", "kind": "github", "ref": "acme/skills", "trust": "known", "enabled": True}, {"id": "off", "kind": "github", "ref": "x/y", "trust": "known", "enabled": False}]
    got = prov.available(rows, github_get([]))
    assert [i.name for i in got] == ["docx", "pdf-tools"]

