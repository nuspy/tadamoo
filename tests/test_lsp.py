"""Language servers for the agent, against a stand-in server (tests/fake_lsp.py): problems, definitions, references, config, a server that dies."""
import json
import sys
from pathlib import Path

import pytest

from harness.lsp import LspClient, LspError, LspHub, from_uri, register_lsp_tools, to_uri
from harness.tools import ToolRegistry

FAKE = str(Path(__file__).with_name("fake_lsp.py"))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\ny = x\n", encoding="utf-8")
    (root / "bad.py").write_text("x = 1\nvalue = BAD\nz = 2\n", encoding="utf-8")
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    return root


def hub_for(ws, tmp_path, pull=False):
    cfg = tmp_path / "lsp.json"
    cfg.write_text(json.dumps({"servers": {"python": [sys.executable, FAKE]}}), encoding="utf-8")
    h = LspHub(ws, config_path=cfg, which=lambda name: None)
    if pull:
        h.config["python"] = [sys.executable, "-c", "import os; os.environ['FAKE_PULL']='1'; exec(open(r'%s').read())" % FAKE]
    return h


def tools(ws, tmp_path, pull=False):
    reg = ToolRegistry()
    hub = hub_for(ws, tmp_path, pull)

    def resolve(path):
        p = (ws / path).resolve()
        p.relative_to(ws.resolve())                         # PermissionError-like for the tests: ValueError is turned below
        return p
    assert register_lsp_tools(reg, hub, resolve)
    return reg, hub


@pytest.mark.parametrize("pull", [False, True])
def test_problems_come_with_line_column_and_kind(ws, tmp_path, pull):
    reg, hub = tools(ws, tmp_path, pull)
    try:
        assert reg.execute("diagnostics", {"path": "ok.py"}) == "No problems reported in ok.py."
        out = reg.execute("diagnostics", {"path": "bad.py"})
        assert out == "bad.py:2:9 error: bad word [E1]"
    finally:
        hub.close()


def test_a_changed_file_is_looked_at_again(ws, tmp_path):
    reg, hub = tools(ws, tmp_path)
    try:
        assert "No problems" in reg.execute("diagnostics", {"path": "ok.py"})
        (ws / "ok.py").write_text("x = BAD\n", encoding="utf-8")
        assert "ok.py:1:5 error" in reg.execute("diagnostics", {"path": "ok.py"})
        (ws / "ok.py").write_text("x = 2\n", encoding="utf-8")
        assert "No problems" in reg.execute("diagnostics", {"path": "ok.py"})
    finally:
        hub.close()


def test_definitions_and_references_are_places_with_the_line_they_are_on(ws, tmp_path):
    reg, hub = tools(ws, tmp_path)
    try:
        assert reg.execute("go_to_definition", {"path": "bad.py", "line": 2, "column": 1}) == "bad.py:1:1  x = 1"
        refs = reg.execute("find_references", {"path": "bad.py", "line": 1, "column": 1}).splitlines()
        assert refs == ["bad.py:2:5  value = BAD", "bad.py:3:1  z = 2"]
        assert "count from 1" in reg.execute("go_to_definition", {"path": "bad.py", "line": 0, "column": 1})
        assert "count from 1" in reg.execute("find_references", {"path": "bad.py", "line": 1, "column": 0})
    finally:
        hub.close()


def test_what_cannot_be_asked_is_said_not_raised(ws, tmp_path):
    reg, hub = tools(ws, tmp_path)
    try:
        assert "No language server is known" in reg.execute("diagnostics", {"path": "notes.txt"})
        assert "is not a file" in reg.execute("diagnostics", {"path": "missing.py"})
        assert reg.get("diagnostics").untrusted and reg.get("find_references").untrusted
    finally:
        hub.close()


def test_a_server_that_is_not_installed_offers_no_tools(ws, tmp_path):
    hub = LspHub(ws, config_path=tmp_path / "none.json", which=lambda name: None)
    assert hub.available() == [] and register_lsp_tools(ToolRegistry(), hub, lambda p: ws / p) is False
    hub2 = LspHub(ws, config_path=tmp_path / "none.json", which=lambda name: "C:/x/pyright-langserver.exe" if name == "pyright-langserver" else None)
    assert hub2.available() == ["python"] and hub2.command_for("python") == ["pyright-langserver", "--stdio"]
    assert hub2.command_for("rust") is None
    with pytest.raises(LspError, match="rust"):
        hub2.client_for(ws / "x.rs")


def test_only_the_persons_own_file_names_a_command(ws, tmp_path):
    (ws / "lsp.json").write_text(json.dumps({"servers": {"python": ["calc.exe"]}}), encoding="utf-8")
    (ws / ".basalt").mkdir()
    (ws / ".basalt" / "lsp.json").write_text(json.dumps({"servers": {"python": ["calc.exe"]}}), encoding="utf-8")
    hub = LspHub(ws, config_path=tmp_path / "mine.json", which=lambda name: None)
    assert hub.config == {} and hub.available() == []
    (tmp_path / "mine.json").write_text(json.dumps({"servers": {"python": ["srv", "--stdio"], "go": "gopls", "c": [], "rust": [1]}}), encoding="utf-8")
    assert LspHub(ws, config_path=tmp_path / "mine.json", which=lambda n: None).config == {"python": ["srv", "--stdio"]}


def test_a_server_that_dies_is_reported_and_started_again(ws, tmp_path):
    reg, hub = tools(ws, tmp_path)
    try:
        assert "No problems" in reg.execute("diagnostics", {"path": "ok.py"})
        client = next(iter(hub._clients.values()))
        client.notify("boom", None)
        client.proc.wait(5)
        assert not client.alive
        assert "No problems" in reg.execute("diagnostics", {"path": "ok.py"})          # a new process
        assert next(iter(hub._clients.values())) is not client
    finally:
        hub.close()


def test_a_command_that_cannot_start_says_so(ws, tmp_path):
    with pytest.raises(LspError, match="could not be started"):
        LspClient(["definitely-not-a-program-xyz"], ws).start()


def test_uris_go_both_ways(tmp_path):
    p = tmp_path / "a b" / "é.py"
    p.parent.mkdir()
    p.write_text("x", encoding="utf-8")
    assert to_uri(p).startswith("file:///") and Path(from_uri(to_uri(p))).resolve() == p.resolve()


def test_the_agent_gets_the_tools_only_when_asked_and_a_server_exists(ws, tmp_path, monkeypatch):
    from harness.stack import AgentConfig, build_agent
    data = tmp_path / "userdata"
    data.mkdir()
    monkeypatch.setenv("BASALT_DATA", str(data))
    (data / "lsp.json").write_text(json.dumps({"servers": {"python": [sys.executable, FAKE]}}), encoding="utf-8")

    def built(**kw):
        return build_agent(AgentConfig(workspace=str(ws), endpoint="http://127.0.0.1:1", data_dir=str(tmp_path / "agent"), use_memory=False, **kw))
    off = built()
    assert "diagnostics" not in off.agent.tools.names() and off.lsp is None
    off.close()
    on = built(lsp=True)
    try:
        assert {"diagnostics", "go_to_definition", "find_references"} <= set(on.agent.tools.names()) and on.lsp is not None
        assert "bad word" in on.agent.tools.execute("diagnostics", {"path": "bad.py"})
        client = next(iter(on.lsp._clients.values()))
    finally:
        on.close()
    client.proc.wait(5)
    assert not client.alive                                                          # closing the agent ends the servers
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "nothing"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    none = built(lsp=True)
    assert "diagnostics" not in none.agent.tools.names() and none.lsp is None
    none.close()
