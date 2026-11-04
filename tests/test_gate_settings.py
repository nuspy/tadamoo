"""Settings > General > Checks before an answer ends: the values are kept and checked, a conversation's process is started with them, and every
string of the page exists in English and Italian."""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.session import parse_args as session_args
from tadamoo.stack import AgentConfig, agent_settings, build_agent
from serve.agent_sessions import SessionManager
from serve.gate_prefs import GateError, GatePrefs
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


def test_the_defaults_are_the_gate_off_the_classifier_on_and_a_bad_value_is_refused_whole(tmp_path):
    prefs = GatePrefs(tmp_path / "g.json")
    first = prefs.read()
    off = {"review": False, "completion_check": False, "course": False, "todo_open": False}
    assert first == {"verify_commands": [], "verify_gate": "always", "verify_rounds": 3, "budget_grace": True, "error_classifier": True, "chat_notes": off}
    saved = prefs.write({"verify_commands": "python -m pytest -x -q\n\nruff check .\n", "verify_gate": "unattended", "verify_rounds": 2, "budget_grace": False,
                         "error_classifier": False})
    assert saved["verify_commands"] == ["python -m pytest -x -q", "ruff check ."] and saved["verify_gate"] == "unattended" and saved["verify_rounds"] == 2
    assert saved["budget_grace"] is False and saved["error_classifier"] is False
    for bad, field in (({"verify_gate": "sometimes"}, "verify_gate"), ({"verify_rounds": 4}, "verify_rounds"), ({"verify_rounds": 0}, "verify_rounds"),
                       ({"verify_rounds": True}, "verify_rounds"), ({"verify_rounds": "2"}, "verify_rounds"), ({"budget_grace": "no"}, "budget_grace"),
                       ({"error_classifier": 1}, "error_classifier"), ({"verify_commands": 5}, "verify_commands"), ({"verify_commands": ["x" * 600]}, "verify_commands"),
                       ({"verify_commands": [f"c{i}" for i in range(20)]}, "verify_commands")):
        with pytest.raises(GateError) as raised:
            prefs.write(bad)
        assert raised.value.field == field and str(raised.value)
    assert prefs.read() == saved                                                   # a refused value changes nothing
    shown = prefs.write({"chat_notes": {"review": True}})["chat_notes"]
    assert shown == {**off, "review": True} and prefs.write({"verify_rounds": 2})["chat_notes"] == shown          # one note on, the others stay as they were
    for bad in ({"chat_notes": {"review": "yes"}}, {"chat_notes": {"nothing": True}}, {"chat_notes": ["review"]}):
        with pytest.raises(GateError) as raised:
            prefs.write(bad)
        assert raised.value.field == "chat_notes"
    prefs.write({"chat_notes": off})
    assert prefs.read() == saved
    (tmp_path / "g.json").write_text("{broken", encoding="utf-8")
    assert prefs.read() == first                                                   # a damaged file is the defaults, not a crash


def test_the_process_of_a_conversation_is_started_with_the_settings_and_they_reach_the_agent(tmp_path):
    prefs = GatePrefs(tmp_path / "g.json")
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", gate_flags=prefs.flags, command=None)
    rec = {"mode": "default", "workspace": str(tmp_path)}
    plain = manager._command("s1", rec, tmp_path / "h")
    assert plain[plain.index("--verify-gate") + 1] == "always" and "--verify-command" not in plain and "--no-budget-grace" not in plain and "--no-error-classifier" not in plain
    prefs.write({"verify_commands": ["pytest -q", "ruff check ."], "verify_gate": "always", "verify_rounds": 2, "budget_grace": False, "error_classifier": False})
    flags = prefs.flags()
    command = manager._command("s1", rec, tmp_path / "h")
    start = command.index(flags[0])
    assert command[start:start + len(flags)] == flags
    assert "--verify-gate" not in manager._command("s2", {**rec, "ask_only": True}, tmp_path / "h")       # a conversation without tools has nothing to check
    settings = agent_settings(session_args(flags))
    assert settings["verify_commands"] == ["pytest -q", "ruff check ."] and settings["verify_gate"] == "always" and settings["verify_rounds"] == 2
    assert settings["budget_grace"] is False and settings["error_classifier"] is False and "--no-error-classifier" in flags


def test_a_built_agent_gets_a_gate_only_when_commands_are_listed_and_knows_if_it_is_unattended(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    off = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d1"), use_memory=False), client=object())
    on = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, verify_commands=["pytest -q"], verify_gate="unattended",
                                 unattended=True, budget_grace=False), client=object())
    quiet = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d3"), use_memory=False, verify_commands=["pytest -q"], verify_gate="unattended"), client=object())
    try:
        assert off.agent.gate is None and off.agent.budget_grace is True
        assert on.agent.gate.enabled and on.agent.budget_grace is False and on.agent.fork().gate is not on.agent.gate
        assert quiet.agent.gate is not None and not quiet.agent.gate.enabled                  # unattended mode in a conversation someone watches
        with pytest.raises(ValueError, match="verify_gate"):
            build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d4"), use_memory=False, verify_gate="sometimes"), client=object())
    finally:
        for built in (off, on, quiet):
            built.close()


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    url = start_server([answer("x")])

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    return call


def test_the_settings_page_reads_and_saves_through_the_api(api):
    code, view = api("GET", "/api/gate")
    assert code == 200 and view["verify_gate"] == "always" and view["modes"] == ["off", "unattended", "always"] and view["limits"]["verify_rounds"] == [1, 3]
    assert view["defaults"]["budget_grace"] is True and view["defaults"]["error_classifier"] is True
    code, view = api("POST", "/api/gate", {"verify_commands": ["pytest -q"], "verify_gate": "always"})
    assert code == 200 and view["verify_commands"] == ["pytest -q"] and view["verify_gate"] == "always"
    assert api("GET", "/api/gate")[1]["verify_commands"] == ["pytest -q"]
    code, body = api("POST", "/api/gate", {"verify_rounds": 9})
    assert code == 400 and body["error"]["field"] == "verify_rounds"
    assert api("GET", "/api/gate")[1]["verify_rounds"] == 3


def test_every_string_of_the_page_exists_in_english_and_italian_and_the_page_is_wired():
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    source = (web / "gate.js").read_text(encoding="utf-8")
    used = {k for k in re.findall(r'T\("(gt\.[a-z.]+)"', source) if not k.endswith(".")} | {"gt.mode." + m for m in ("off", "unattended", "always")} | {"gt.title"}
    assert len(used) > 15
    notes = {"ch.gate.retry", "ch.gate.pass", "ch.gate.stop", "ch.ev.budget_warning", "ch.ev.budget_grace", "ch.ev.stop_denied"}      # the lines in the chat
    assert '"ch.gate." + d.decision' in (web / "agentchat.js").read_text(encoding="utf-8") and '"ch.ev." + d.type' in (web / "agentchat.js").read_text(encoding="utf-8")
    for key in used | notes | {"pv.f.ctx", "pv.f.ctx.help", "pv.f.out", "pv.f.out.help"}:
        assert i18n.count(f'"{key}":') == 2, key                                       # once in English and once in Italian
    html = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="web/gate.js"' in html and 'id="gt-root"' in html
    assert "innerHTML" not in source                                                       # what the server says is put in as text
