"""The wiki, the reference documents and the project notes are reached from the Chat and the Agent: the settings are kept and checked, the
conversation's process is started with them, and the wiki answers (or says where and why it did not) with a fake service."""
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_services import FakeWikibridge
from harness.session import parse_args as session_args
from harness.stack import AgentConfig, agent_settings, build_agent
from serve.agent_sessions import SessionManager
from serve.knowledge_prefs import KnowledgeError, KnowledgePrefs, check_wiki
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


@pytest.fixture
def wiki():
    fake = FakeWikibridge()
    yield fake
    fake.close()


def free_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def test_the_settings_have_defaults_limits_and_clear_refusals(tmp_path):
    prefs = KnowledgePrefs(tmp_path / "k.json")
    first = prefs.read()
    assert first["memory_min_score"] == 1.5 and first["memory_notes"] == 4 and first["wiki_enabled"] is False and first["reference_folder"] == ""
    saved = prefs.write({"memory_min_score": 2.5, "memory_notes": 2, "memory_budget_tokens": 300, "wiki_enabled": True, "wiki_url": "http://127.0.0.1:8775/"})
    assert saved["memory_min_score"] == 2.5 and saved["wiki_url"] == "http://127.0.0.1:8775" and saved["memory_notes"] == 2
    for bad, field in (({"memory_notes": 99}, "memory_notes"), ({"memory_min_score": "high"}, "memory_min_score"), ({"memory_budget_tokens": 5}, "memory_budget_tokens"),
                       ({"wiki_url": "ftp://x"}, "wiki_url"), ({"wiki_url": "http://user:pw@host"}, "wiki_url"), ({"wiki_enabled": "yes"}, "wiki_enabled"),
                       ({"reference_folder": str(tmp_path / "nowhere")}, "reference_folder"), ({"reference_strategy": "all"}, "reference_strategy"),
                       ({"wiki_project": "a b"}, "wiki_project"), ({"memory_notes": 2.5}, "memory_notes")):
        with pytest.raises(KnowledgeError) as raised:
            prefs.write(bad)
        assert raised.value.field == field and str(raised.value)
    assert prefs.read() == saved                                              # a refused value changes nothing


def test_the_process_of_a_conversation_is_started_with_the_settings(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("Il codice è LANTERN.", encoding="utf-8")
    prefs = KnowledgePrefs(tmp_path / "k.json")
    assert "--wiki" not in prefs.flags() and "--reference" not in prefs.flags()
    prefs.write({"wiki_enabled": True, "wiki_url": "http://127.0.0.1:9999", "wiki_project": "cervinia", "reference_folder": str(docs),
                 "reference_strategy": "rag", "memory_min_score": 2, "memory_notes": 3, "memory_budget_tokens": 400})
    flags = prefs.flags()
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", knowledge_flags=prefs.flags, command=None)
    command = manager._command("s1", {"mode": "default", "workspace": str(tmp_path)}, tmp_path / "h")
    start = command.index(flags[0])
    assert command[start:start + len(flags)] == flags                          # the process gets the settings, in one piece
    settings = agent_settings(session_args(flags))
    assert settings["wiki"] is True and settings["wiki_url"] == "http://127.0.0.1:9999" and settings["wiki_project"] == "cervinia"
    assert settings["reference_docs"] == {"a.md": "Il codice è LANTERN."} and settings["reference_strategy"] == "rag"
    assert (settings["memory_min_score"], settings["memory_notes"], settings["memory_budget_tokens"]) == (2.0, 3, 400)
    for item in docs.iterdir():
        item.unlink()
    docs.rmdir()
    assert "--reference" not in prefs.flags()                                  # a folder that is gone is left out, not a reason not to start


def test_a_plan_step_with_a_wiki_question_gets_an_answer_when_the_wiki_is_reachable(tmp_path, wiki):
    cfg = AgentConfig(workspace=str(tmp_path), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, wiki=True, wiki_url=wiki.url)
    built = build_agent(cfg, client=object())
    try:
        tools = built.agent.tools
        assert {"wiki_search", "wiki_page", "wiki_context"} <= set(tools.names())
        out = tools.execute("wiki_context", {"query": "allergie di Diana", "project": "cervinia"})
        assert out["blocks"][0]["text"] == "Allergica agli ovini."
        assert any(path.startswith("/rag/context") for _, path, _, _ in wiki.requests)
    finally:
        built.close()


def test_when_the_wiki_cannot_be_reached_the_error_says_the_address_and_the_reason(tmp_path):
    url = free_port_url()
    cfg = AgentConfig(workspace=str(tmp_path), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, wiki=True, wiki_url=url, wiki_project="cervinia")
    built = build_agent(cfg, client=object())
    try:
        with pytest.raises(RuntimeError) as raised:
            built.agent.tools.execute("wiki_context", {"query": "allergie"})
        message = str(raised.value)
        assert url in message and "offline" in message and "Settings" in message
    finally:
        built.close()


def test_the_check_of_the_connection_says_what_it_found(wiki):
    ok = check_wiki(wiki.url)
    assert ok["status"] == "ok" and ok["address"] == wiki.url and ok["projects"] == ["cervinia", "work"] and ok["error"] == ""
    url = free_port_url()
    bad = check_wiki(url)
    assert bad["status"] == "offline" and bad["address"] == url and url in bad["error"]


def test_reference_documents_given_by_the_chat_process_are_searched(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rules.md").write_text("The freeze hotfix code word is LANTERN.", encoding="utf-8")
    settings = agent_settings(session_args(["--reference", str(docs), "--reference-strategy", "rag"]))
    built = build_agent(AgentConfig(**{**settings, "use_memory": False, "use_checkpoints": False}, data_dir=str(tmp_path / "d")), client=object())
    try:
        assert "kb_search" in built.agent.tools.names()
        assert "LANTERN" in built.agent.tools.execute("kb_search", {"query": "freeze hotfix code word"})[0]
    finally:
        built.close()


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    url = start_server([answer("x")])

    def call(method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    return call


def test_the_settings_page_reads_saves_and_checks_the_wiki(api, wiki):
    code, view = api("GET", "/api/knowledge")
    assert code == 200 and view["wiki_enabled"] is False and view["limits"]["memory_notes"] == [0, 20] and view["defaults"]["memory_notes"] == 4
    code, view = api("POST", "/api/knowledge", {"wiki_enabled": True, "wiki_url": wiki.url, "memory_notes": 2})
    assert code == 200 and view["wiki_enabled"] is True and view["wiki_url"] == wiki.url and view["memory_notes"] == 2
    assert api("GET", "/api/knowledge")[1]["memory_notes"] == 2
    code, body = api("POST", "/api/knowledge", {"memory_notes": 500})
    assert code == 400 and body["error"]["field"] == "memory_notes"
    code, found = api("POST", "/api/knowledge/wiki-check", {})
    assert code == 200 and found["status"] == "ok" and found["address"] == wiki.url
    code, found = api("POST", "/api/knowledge/wiki-check", {"wiki_url": free_port_url()})
    assert found["status"] == "offline" and found["address"] in found["error"]


def test_only_the_pages_own_origin_can_change_these_settings(api):
    code, _ = api("POST", "/api/knowledge", {"memory_notes": 1}, headers={"Origin": "http://evil.example"})
    assert code == 403
    assert api("GET", "/api/knowledge")[1]["memory_notes"] == 4


def test_every_string_of_the_settings_page_exists_in_english_and_italian():
    import re
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    used = set(re.findall(r'T\("(kn\.[a-z.]+)"', (web / "knowledge.js").read_text(encoding="utf-8")))
    used = {k for k in used if not k.endswith(".")} | {"kn.strategy." + k for k in ("auto", "cag", "rag", "none")} | {"kn.title"}
    assert len(used) > 20
    for key in used:
        assert i18n.count(f'"{key}":') == 2, key                               # once in English and once in Italian
    assert 'src="web/knowledge.js"' in (web / "index.html").read_text(encoding="utf-8")
