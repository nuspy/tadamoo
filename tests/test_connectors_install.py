"""Installing and removing what the Connectors tab lists, on a local stand-in for GitHub and the MCP registry (no network)."""
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from serve.capabilities_api import CapabilityApi                    # noqa: E402
from serve.connectors_api import ConnectorError, ConnectorsApi      # noqa: E402
from serve.connectors_install import ConnectorInstaller, ConnectorSettings   # noqa: E402
from tadamoo.connectors.providers import Providers                  # noqa: E402

SKILL = b"---\nname: pdf-tools\ndescription: Read and fill PDF forms\n---\nUse it for forms.\n"
SCRIPT = b"import subprocess\nsubprocess.run(['echo', 'hi'])\n"
FILES = {"skills/pdf/SKILL.md": SKILL, "skills/pdf/run.py": SCRIPT}
COMMIT = "a" * 40


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@pytest.fixture
def stub():
    state = {"hits": [], "head": COMMIT, "registry_server": {"name": "io.example/cloud", "description": "A cloud service", "version": "1.0.0",
                                                              "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com/cloud"}]}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            state["hits"].append(path)
            body, ctype = b"{}", "application/json"
            if path == "/gh/repos/acme/skills":
                body = json.dumps({"full_name": "acme/skills", "default_branch": "main"}).encode()
            elif path.startswith("/gh/repos/acme/skills/commits/"):
                body, ctype = state["head"].encode(), "text/plain"
            elif path in (f"/gh/repos/acme/skills/git/trees/{COMMIT}", f"/gh/repos/acme/skills/git/trees/{'b' * 40}", "/gh/repos/acme/skills/git/trees/main"):
                body = json.dumps({"tree": [{"path": p, "type": "blob", "mode": "100644", "size": len(d), "sha": blob_sha(d)} for p, d in FILES.items()]}).encode()
            elif path.startswith(f"/raw/acme/skills/{COMMIT}/") or path.startswith(f"/raw/acme/skills/{'b' * 40}/") or path.startswith("/raw/acme/skills/main/"):
                body, ctype = FILES[path.split("/", 5)[5]], "text/plain"
            elif path in ("/reg/v0/servers", "/reg/v0.1/servers"):
                body = json.dumps({"servers": [{"server": state["registry_server"]}]}).encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


def make(tmp_path, stub, providers):
    caps_dir = tmp_path / "capabilities"
    caps_dir.mkdir()
    (caps_dir / "discovery.json").write_text(json.dumps({"base_urls": {"github": stub["url"] + "/gh", "github_raw": stub["url"] + "/raw", "registry": stub["url"] + "/reg"}}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    caps = CapabilityApi(caps_dir, tmp_path / "work", home=home, appdata=tmp_path / "appdata")
    (tmp_path / "providers.json").write_text(json.dumps(providers), encoding="utf-8")
    installer = ConnectorInstaller(caps, tmp_path / "connectors")
    def get(url):                                                                              # the real addresses of GitHub go to the stand-in
        import urllib.request
        url = url.replace("https://api.github.com", stub["url"] + "/gh").replace("https://raw.githubusercontent.com", stub["url"] + "/raw")
        return urllib.request.urlopen(url, timeout=5).read()
    api = ConnectorsApi(lambda: caps.overview()["entries"], lambda: ([], ()), Providers(tmp_path / "providers.json"), get, None, installer)
    return api, caps, installer


def test_the_originals_folder_is_a_full_path_made_when_set_and_empty_by_default(tmp_path):
    s = ConnectorSettings(tmp_path / "c")
    assert s.read() == {"originals_dir": "", "review_mode": "ours", "review_tier3_docker": False}
    where = tmp_path / "AI" / "connectors"
    assert s.write(str(where)) == {"originals_dir": str(where), "review_mode": "ours", "review_tier3_docker": False} and where.is_dir()
    for bad in ("relative/dir", str(tmp_path / ".." / "x"), "x" * 600):
        with pytest.raises(Exception):
            s.write(bad)


def test_a_skill_in_a_folder_of_this_pc_is_copied_recorded_found_by_the_index_and_removed_leaving_the_original(tmp_path, stub):
    src = tmp_path / "skills" / "memo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: memo\ndescription: Keeps notes\n---\nBody.\n", encoding="utf-8")
    (src / "extra.txt").write_text("more", encoding="utf-8")
    api, caps, _ = make(tmp_path, stub, [{"id": "f", "kind": "folder", "ref": str(tmp_path / "skills"), "trust": "known", "enabled": True}])
    assert any(i["id"] == "folder:memo" for i in api.view("all", "skill")["available"])
    got = api.install("folder:memo", {})["installed"]
    assert got["id"] == "skill:memo" and got["source"] == "folder"
    assert (tmp_path / "capabilities" / "skills" / "memo" / "extra.txt").is_file() and (src / "SKILL.md").is_file()
    entry = next(e for e in caps.overview()["entries"] if e["id"] == "skill:memo")
    assert entry["origin"] == "tadamoo"
    again = api.view("all", "skill")
    assert not any(i["id"] == "folder:memo" for i in again["available"]) or any(i["id"] == "skill:memo" and i["installed"] for i in again["installed"])
    with pytest.raises(ConnectorError) as e:
        api.install("folder:memo", {})
    assert e.value.code in ("installed", "install", "gone")
    assert api.uninstall("skill:memo") == {"removed": "skill:memo"}
    assert not (tmp_path / "capabilities" / "skills" / "memo").exists() and (src / "SKILL.md").is_file()
    assert all(e["id"] != "skill:memo" for e in caps.overview()["entries"])


def test_a_skill_of_a_github_repository_is_read_before_installing_then_installed_at_its_commit_and_its_original_is_kept(tmp_path, stub):
    api, caps, installer = make(tmp_path, stub, [{"id": "g", "kind": "github", "ref": "acme/skills@main", "trust": "known", "enabled": True}])
    installer.settings.write(str(tmp_path / "originals"))
    item = next(i for i in api.view("all", "skill")["available"] if i["id"].startswith("github:"))
    assert item["id"] == "github:acme/skills:skills/pdf" and item["name"] == "pdf-tools"
    sheet = api.facts(item["id"])
    assert sheet["sheet"]["scripts"] == ["run.py"] and "runs commands" in sheet["sheet"]["signs"] and "Use it for forms." in sheet["excerpts"]
    got = api.install(item["id"], {})["installed"]
    assert got["id"] == "skill:pdf-tools" and got["version"] == COMMIT[:10]
    installed = tmp_path / "capabilities" / "skills" / "pdf-tools"
    assert (installed / "run.py").read_bytes() == SCRIPT
    original = next((tmp_path / "originals" / "github").iterdir())
    assert original.name.startswith("acme__skills@" + COMMIT[:12]) and (original / "SKILL.md").read_bytes() == SKILL
    (installed / "run.py").write_bytes(b"changed")                                          # changes are made only on the installed copy
    assert (original / "run.py").read_bytes() == SCRIPT
    assert api.uninstall("skill:pdf-tools")["removed"] == "skill:pdf-tools" and original.is_dir()


def test_a_remote_server_of_a_registry_is_installed_as_a_record_and_listed_by_the_index(tmp_path, stub):
    api, caps, _ = make(tmp_path, stub, [{"id": "r", "kind": "mcp_registry", "ref": stub["url"] + "/reg", "trust": "official", "enabled": True}])
    item = next(i for i in api.view("all", "mcp_server")["available"] if i["id"] == "registry:io.example/cloud")
    sheet = api.facts(item["id"])["sheet"]
    assert sheet["hosts"] == ["mcp.example.com"]
    got = api.install(item["id"], {})["installed"]
    assert got["kind"] == "mcp" and got["name"] == "io-example-cloud" or got["name"].endswith("cloud")
    entry = next(e for e in caps.overview()["entries"] if e["kind"] == "mcp" and e["origin"] == "tadamoo")
    assert "mcp.example.com" in entry["command"]
    assert api.uninstall(entry["id"])["removed"] == entry["id"]
    assert not any(e["origin"] == "tadamoo" for e in caps.overview()["entries"])


def test_an_mcp_servers_update_is_found_in_the_registry_and_installed_over_it(tmp_path, stub):
    api, caps, installer = make(tmp_path, stub, [{"id": "r", "kind": "mcp_registry", "ref": stub["url"] + "/reg", "trust": "official", "enabled": True}])
    item = next(i for i in api.view("all", "mcp_server")["available"] if i["id"] == "registry:io.example/cloud")
    entry_id = api.install(item["id"], {})["installed"]["id"]
    assert api.updates() == {"updates": [], "errors": []}
    stub["registry_server"] = {**stub["registry_server"], "version": "2.0.0"}
    found = api.updates()["updates"]
    assert len(found) == 1 and found[0]["id"] == entry_id and found[0]["from"] == "1.0.0" and found[0]["to"] == "2.0.0" and found[0]["kind"] == "mcp"
    got = api.update(entry_id, {})["installed"]
    assert got["version"] == "2.0.0"
    assert api.updates()["updates"] == []
    with pytest.raises(ConnectorError):
        api.update("mcp:not-ours", {})


def test_updating_a_connector_carries_its_accounts_and_configured_values_over(tmp_path, stub, monkeypatch):
    from tadamoo.discovery.installed import InstalledStore
    api, caps, installer = make(tmp_path, stub, [])
    rec = {"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s", "source": "registry", "route": "remote", "version": "1",
           "env": [{"name": "REGION", "required": False, "secret": False, "entry": "REGION", "value": ""}],
           "accounts": ["default", "work"], "default_account": "default", "account_values": {"default": {"REGION": "eu"}, "work": {"REGION": "us"}}}
    InstalledStore(tmp_path / "capabilities").put(dict(rec))

    def fake_install(kind, source, flags):
        InstalledStore(tmp_path / "capabilities").put({"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s2",
                                                        "source": "registry", "route": "remote", "version": "2",
                                                        "env": [{"name": "REGION", "required": False, "secret": False, "entry": "REGION", "value": ""}]})
        return {"id": "mcp:demo", "kind": "mcp", "version": "2"}
    monkeypatch.setattr(installer, "install", fake_install)
    monkeypatch.setattr(installer, "_registry_match", lambda finder, rec: {"name": "io.example/demo", "version": "2"})

    out = api.update("mcp:demo", {})
    assert out["installed"]["version"] == "2"
    new = InstalledStore(tmp_path / "capabilities").get("mcp:demo")
    assert new["accounts"] == ["default", "work"] and new["default_account"] == "default" and new["account_values"] == {"default": {"REGION": "eu"}, "work": {"REGION": "us"}}
    assert api.config("mcp:demo", "work")["variables"][0]["value"] == "us"                    # still readable through the normal account API after the update


def test_only_what_was_installed_from_here_is_removed_and_nothing_installs_without_an_installer(tmp_path, stub):
    api, caps, _ = make(tmp_path, stub, [])
    skills = tmp_path / "home" / ".claude" / "skills" / "mine"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: mine\ndescription: my own\n---\nx\n", encoding="utf-8")
    caps.rescan()
    with pytest.raises(ConnectorError) as e:
        api.uninstall("skill:mine")
    assert e.value.code == "foreign" and (skills / "SKILL.md").is_file()
    with pytest.raises(ConnectorError):
        api.install("skill:mine", {})
    bare = ConnectorsApi(lambda: [], lambda: ([], ()), Providers(tmp_path / "providers.json"), None, None, None)
    for call in (lambda: bare.install("x", {}), lambda: bare.uninstall("x"), lambda: bare.folder_settings()):
        with pytest.raises(ConnectorError):
            call()


def test_a_variable_of_an_installed_server_is_set_secrets_go_to_the_vault_and_are_never_given_back(tmp_path, stub, monkeypatch):
    from tadamoo.discovery.installed import InstalledStore
    import tadamoo.vault as vault_mod
    kept = {}

    class FakeVault:
        def __init__(self, directory):
            pass

        def put(self, entry, kind="secret", label="", fields=None):
            kept[entry] = (kind, label, dict(fields or {}))

        def reveal(self, entry, field):
            return kept[entry][2][field]
    monkeypatch.setattr(vault_mod, "Vault", FakeVault)
    api, caps, installer = make(tmp_path, stub, [])
    installer.vault_dir = tmp_path / "vault"
    InstalledStore(tmp_path / "capabilities").put({"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s", "source": "registry", "route": "remote",
                                                   "version": "1", "env": [{"name": "API_KEY", "required": True, "secret": True, "entry": "API_KEY", "value": ""},
                                                                           {"name": "REGION", "required": False, "secret": False, "entry": "REGION", "value": ""}]})
    first = api.config("mcp:demo")
    assert [(v["name"], v["secret"], v["set"]) for v in first["variables"]] == [("API_KEY", True, False), ("REGION", False, False)]
    got = api.configure("mcp:demo", {"API_KEY": "sk-secret-123", "REGION": " eu "})
    assert [(v["name"], v["set"], v["value"]) for v in got["variables"]] == [("API_KEY", True, ""), ("REGION", True, "eu")]
    assert "sk-secret-123" not in json.dumps(got) and kept["API_KEY"][2] == {"value": "sk-secret-123"} and "sk-secret-123" not in (tmp_path / "capabilities" / "installed.json").read_text(encoding="utf-8")
    assert api.configure("mcp:demo", {"API_KEY": ""})["variables"][0]["set"] is True                         # an empty secret leaves what is there
    assert api.configure("mcp:demo", {"REGION": ""})["variables"][1]["set"] is False                          # an empty plain value clears it
    for bad in ({"NOPE": "x"}, {"API_KEY": 5}, {"API_KEY": "x" * 5000}):
        with pytest.raises(ConnectorError):
            api.configure("mcp:demo", bad)
    with pytest.raises(ConnectorError) as e:
        api.config("skill:not-ours")
    assert e.value.code == "foreign"


def test_a_second_account_keeps_its_own_secrets_apart_and_the_first_still_reads_its_old_one(tmp_path, stub, monkeypatch):
    from tadamoo.discovery.installed import InstalledStore
    import tadamoo.vault as vault_mod
    kept = {}

    class FakeVault:
        def __init__(self, directory):
            pass

        def put(self, entry, kind="secret", label="", fields=None):
            kept[entry] = (kind, label, dict(fields or {}))

        def reveal(self, entry, field):
            return kept[entry][2][field]

        def delete(self, entry):
            return kept.pop(entry, None) is not None
    monkeypatch.setattr(vault_mod, "Vault", FakeVault)
    api, caps, installer = make(tmp_path, stub, [])
    installer.vault_dir = tmp_path / "vault"
    InstalledStore(tmp_path / "capabilities").put({"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s", "source": "registry", "route": "remote",
                                                   "version": "1", "env": [{"name": "API_KEY", "required": True, "secret": True, "entry": "API_KEY", "value": ""},
                                                                           {"name": "REGION", "required": False, "secret": False, "entry": "REGION", "value": ""}]})
    assert api.accounts("mcp:demo") == {"id": "mcp:demo", "accounts": ["default"], "default": "default"}         # a record made before accounts existed has one, implicit
    api.configure("mcp:demo", {"API_KEY": "sk-first", "REGION": "eu"})
    assert kept["API_KEY"][2]["value"] == "sk-first"                                                              # unsuffixed: an old install's secret is found unchanged

    got = api.add_account("mcp:demo", "work")
    assert got == {"id": "mcp:demo", "accounts": ["default", "work"], "default": "default"}
    with pytest.raises(ConnectorError):
        api.add_account("mcp:demo", "Default")                                                                    # case-insensitive clash
    with pytest.raises(ConnectorError):
        api.add_account("mcp:demo", "bad name!")

    before = api.config("mcp:demo")                                                                                # unnamed: the default account
    assert before["account"] == "default" and before["variables"][0]["set"] is True
    work = api.config("mcp:demo", "work")
    assert work["account"] == "work" and [v["set"] for v in work["variables"]] == [False, False]                  # the new account starts with nothing of its own

    api.configure("mcp:demo", {"API_KEY": "sk-second", "REGION": "us"}, "work")
    assert kept["API_KEY::work"][2]["value"] == "sk-second" and kept["API_KEY"][2]["value"] == "sk-first"          # apart, and the first unchanged
    assert api.config("mcp:demo", "work")["variables"][1]["value"] == "us" and api.config("mcp:demo")["variables"][1]["value"] == "eu"
    with pytest.raises(ConnectorError):
        api.config("mcp:demo", "nobody")

    api.set_default_account("mcp:demo", "work")
    assert api.accounts("mcp:demo")["default"] == "work"
    assert api.config("mcp:demo")["account"] == "work"                                                            # unnamed now resolves to the new default

    with pytest.raises(ConnectorError):
        api.remove_account("mcp:demo", "nobody")
    removed = api.remove_account("mcp:demo", "default")
    assert removed == {"id": "mcp:demo", "accounts": ["work"], "default": "work"} and "API_KEY" not in kept and kept["API_KEY::work"]
    with pytest.raises(ConnectorError):
        api.remove_account("mcp:demo", "work")                                                                     # the last account cannot be removed


def test_two_or_more_accounts_with_no_default_must_be_named_to_configure(tmp_path, stub):
    from tadamoo.discovery.installed import InstalledStore
    api, caps, installer = make(tmp_path, stub, [])
    InstalledStore(tmp_path / "capabilities").put({"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s", "source": "registry", "route": "remote",
                                                   "version": "1", "env": [{"name": "REGION", "required": False, "secret": False, "entry": "REGION", "value": ""}]})
    api.add_account("mcp:demo", "second")
    assert api.config("mcp:demo")["account"] == "default"                                                        # adding a second keeps the one there was as the default
    api.set_default_account("mcp:demo", "")
    with pytest.raises(ConnectorError) as e:
        api.config("mcp:demo")
    assert e.value.code == "account_needed"
    assert api.config("mcp:demo", "default")["account"] == "default"


def test_configure_ask_gives_the_model_only_field_names_through_the_api(tmp_path, stub):
    from tadamoo.discovery.installed import InstalledStore

    class Client:
        def side_context(self):
            return 60000

        def chat_completion(self, messages, **kw):
            self.seen = messages
            return {"choices": [{"message": {"content": "Look in the provider's dashboard."}}]}

    client = Client()
    caps_dir = tmp_path / "capabilities"
    caps_dir.mkdir()
    (caps_dir / "discovery.json").write_text(json.dumps({"base_urls": {"github": stub["url"] + "/gh", "github_raw": stub["url"] + "/raw", "registry": stub["url"] + "/reg"}}), encoding="utf-8")
    from serve.capabilities_api import CapabilityApi
    caps = CapabilityApi(caps_dir, tmp_path / "work", home=tmp_path / "home2", appdata=tmp_path / "appdata2")
    installer = ConnectorInstaller(caps, tmp_path / "connectors")
    InstalledStore(caps_dir).put({"id": "mcp:demo", "name": "demo", "kind": "mcp", "url": "https://x.example/mcp", "folder": "", "sha": "s", "source": "registry", "route": "remote",
                                  "version": "1", "env": [{"name": "API_KEY", "required": True, "secret": True, "entry": "API_KEY", "value": ""}]})
    (tmp_path / "providers2.json").write_text("[]", encoding="utf-8")
    api = ConnectorsApi(lambda: caps.overview()["entries"], lambda: ([], ()), Providers(tmp_path / "providers2.json"), None, lambda: client, installer)
    out = api.configure_ask("mcp:demo", "Where do I get the key?")
    assert out == {"answer": "Look in the provider's dashboard."}
    assert "API_KEY: required, secret, not set" in client.seen[0]["content"] and client.seen[-1]["content"] == "Where do I get the key?"
    for bad in ("", "x" * 3000, 5):
        with pytest.raises(ConnectorError):
            api.configure_ask("mcp:demo", bad)
    with pytest.raises(ConnectorError):
        api.configure_ask("mcp:demo", "q", history="not a list")


def test_the_update_plan_merges_the_persons_edit_with_upstreams_change_and_flags_the_real_conflict(tmp_path, stub, monkeypatch):
    api, caps, installer = make(tmp_path, stub, [{"id": "g", "kind": "github", "ref": "acme/skills@main", "trust": "known", "enabled": True}])
    installer.settings.write(str(tmp_path / "originals"))
    item = next(i for i in api.view("all", "skill")["available"] if i["id"].startswith("github:"))
    api.install(item["id"], {})
    installed_dir = tmp_path / "capabilities" / "skills" / "pdf-tools"
    (installed_dir / "run.py").write_text("print('mine')\n", encoding="utf-8")            # the person's own edit, upstream touches this file too: conflict
    (installed_dir / "SKILL.md").write_text((SKILL + b"\nmore.\n").decode(), encoding="utf-8")   # the person's own edit, upstream leaves this one alone: kept

    new_commit = "c" * 40

    def fake_new_commit(rec):
        return rec["repo"], rec.get("source_path") or "", new_commit
    monkeypatch.setattr(installer, "_new_commit", fake_new_commit)

    def fake_fetch(repo, path, pin, into, files_max=30, file_bytes=200_000):
        (into / "SKILL.md").write_bytes(SKILL)                                            # unchanged from the base: upstream did not touch it
        (into / "run.py").write_text("print('upstream')\n", encoding="utf-8")             # changed from the base: the real conflict
        (into / "new.txt").write_text("from upstream", encoding="utf-8")                  # a file only the new version has
        return into
    monkeypatch.setattr(installer, "fetch_skill", fake_fetch)

    plan = api.update_plan("skill:pdf-tools")
    assert plan["from_commit"] == COMMIT and plan["to_commit"] == new_commit and plan["has_base"] is True
    assert plan["files"] == {"SKILL.md": "kept", "run.py": "conflict", "new.txt": "upstream"}
    assert plan["counts"] == {"same": 0, "upstream": 1, "kept": 1, "conflict": 1}
    assert plan["review"]["verdict"] == "unknown" and "no model" in plan["review"]["note"]      # no reviewer client here: advisory only, never blocking

    with pytest.raises(ConnectorError) as e:
        api.apply_update("skill:pdf-tools", {})
    assert e.value.code == "conflicts"

    out = api.apply_update("skill:pdf-tools", {"run.py": "upstream"})
    assert out["to_commit"] == new_commit and out["applied"] == {"SKILL.md": "kept", "run.py": "upstream", "new.txt": "upstream"}
    assert (installed_dir / "run.py").read_text() == "print('upstream')\n"                # the conflict, resolved "upstream"
    assert "more." in (installed_dir / "SKILL.md").read_text()                            # kept: the person's edit stands
    assert (installed_dir / "new.txt").read_text() == "from upstream"                     # a plain upstream addition
    from tadamoo.discovery.installed import InstalledStore
    assert InstalledStore(tmp_path / "capabilities").get("skill:pdf-tools")["commit"] == new_commit
    assert json.loads((installed_dir / ".tadamoo-source.json").read_text())["commit"] == new_commit      # the provenance marker is rewritten, not merged away

    with pytest.raises(ConnectorError):
        api.update_plan("skill:not-ours")


def test_without_an_originals_folder_any_real_difference_from_upstream_is_a_conflict(tmp_path, stub, monkeypatch):
    api, caps, installer = make(tmp_path, stub, [{"id": "g", "kind": "github", "ref": "acme/skills@main", "trust": "known", "enabled": True}])
    item = next(i for i in api.view("all", "skill")["available"] if i["id"].startswith("github:"))         # no originals_dir set: there is no base
    api.install(item["id"], {})
    monkeypatch.setattr(installer, "_new_commit", lambda rec: (rec["repo"], rec.get("source_path") or "", "d" * 40))
    monkeypatch.setattr(installer, "fetch_skill", lambda repo, path, pin, into, files_max=30, file_bytes=200_000: (into / "run.py").write_bytes(SCRIPT) and into)
    plan = api.update_plan("skill:pdf-tools")
    assert plan["has_base"] is False and plan["files"] == {"run.py": "same", "SKILL.md": "conflict"}      # run.py untouched: same; SKILL.md absent upstream: conflict, not a guess


def test_a_newer_commit_of_an_installed_skill_is_found_and_installed_over_it_keeping_the_old_original(tmp_path, stub):
    api, caps, installer = make(tmp_path, stub, [{"id": "g", "kind": "github", "ref": "acme/skills@main", "trust": "known", "enabled": True}])
    installer.settings.write(str(tmp_path / "originals"))
    item = next(i for i in api.view("all", "skill")["available"] if i["id"].startswith("github:"))
    api.install(item["id"], {})
    assert api.updates() == {"updates": [], "errors": []}
    stub["head"] = "b" * 40
    found = api.updates()["updates"]
    assert found == [{"id": "skill:pdf-tools", "name": "pdf-tools", "from": COMMIT[:10], "to": ("b" * 40)[:10], "kind": "skill"}]
    got = api.update("skill:pdf-tools", {})["installed"]
    assert got["version"] == ("b" * 40)[:10]
    assert api.updates()["updates"] == []
    names = sorted(p.name for p in (tmp_path / "originals" / "github").iterdir())
    assert len(names) == 2 and any("b" * 12 in n for n in names) and any(COMMIT[:12] in n for n in names)       # both originals are kept, the old one untouched
    with pytest.raises(ConnectorError):
        api.update("skill:nothing", {})


def test_a_github_token_in_the_vault_is_set_removed_and_never_shown_back(tmp_path, stub):
    api, caps, installer = make(tmp_path, stub, [])
    assert api.github_token_status() == {"set": False}                                   # no vault here yet
    installer.vault_dir = tmp_path / "vault"
    assert api.github_token_status() == {"set": False}
    assert api.set_github_token("ghp_abc123") == {"set": True}
    assert api.github_token_status() == {"set": True}
    from tadamoo.vault import Vault
    assert Vault(installer.vault_dir).reveal("github", "value") == "ghp_abc123"
    assert api.set_github_token("") == {"set": False}                                     # an empty token removes the entry
    assert api.github_token_status() == {"set": False}
    with pytest.raises(ConnectorError):
        api.set_github_token(123)
