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
    state = {"hits": []}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            state["hits"].append(path)
            body, ctype = b"{}", "application/json"
            if path == "/gh/repos/acme/skills":
                body = json.dumps({"full_name": "acme/skills", "default_branch": "main"}).encode()
            elif path.startswith("/gh/repos/acme/skills/commits/"):
                body, ctype = COMMIT.encode(), "text/plain"
            elif path in (f"/gh/repos/acme/skills/git/trees/{COMMIT}", "/gh/repos/acme/skills/git/trees/main"):
                body = json.dumps({"tree": [{"path": p, "type": "blob", "mode": "100644", "size": len(d), "sha": blob_sha(d)} for p, d in FILES.items()]}).encode()
            elif path.startswith(f"/raw/acme/skills/{COMMIT}/") or path.startswith("/raw/acme/skills/main/"):
                body, ctype = FILES[path.split("/", 5)[5]], "text/plain"
            elif path == "/reg/v0/servers":
                body = json.dumps({"servers": [{"server": {"name": "io.example/cloud", "description": "A cloud service", "version": "1.0.0",
                                                          "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com/cloud"}]}}]}).encode()
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
    assert s.read() == {"originals_dir": ""}
    where = tmp_path / "AI" / "connectors"
    assert s.write(str(where)) == {"originals_dir": str(where)} and where.is_dir()
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
