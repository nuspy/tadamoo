"""find_online_capability: the question put to the person, what is installed only on their choice, and the process that carries it."""
import io
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_discovery import FakeServices  # noqa: E402
from harness.capabilities import CapabilityIndex, register_capability_tools  # noqa: E402
from harness.discovery.candidates import CandidateStore  # noqa: E402
from harness.discovery.finder import Finder  # noqa: E402
from harness.discovery.installer import Installer, Run  # noqa: E402
from harness.discovery.settings import DiscoverySettings  # noqa: E402
from harness.discovery.tool import register_discovery_tools  # noqa: E402
from harness.tools.registry import EffectClass, ToolRegistry  # noqa: E402
from test_discovery_install import Runner  # noqa: E402


def pdf_world(w):
    w.add_repo("acme/pdf-mcp", stars=3000, forks=300)
    w.add_npm("@acme/pdf-mcp", "3.1.0", weekly=60_000, repo="acme/pdf-mcp", description="Extract tables from PDF files")
    w.add_registry("io.github.acme/pdf-mcp", "Extract tables from PDF files", repo_url="https://github.com/acme/pdf-mcp", version="3.1.0",
                   packages=[{"registryType": "npm", "identifier": "@acme/pdf-mcp", "version": "3.1.0", "transport": {"type": "stdio"}}])
    w.add_npm("pdf-lite-mcp", "0.2.0", weekly=300, repo=None, description="small pdf tables helper", date="2026-09-01T00:00:00Z")
    w.add_registry("com.example/pdf-cloud", "Hosted PDF tables", remotes=[{"type": "streamable-http", "url": "https://pdf.example.com/mcp"}])


@pytest.fixture
def fake():
    f = FakeServices()
    pdf_world(f.world)
    yield f
    f.close()


class Set:
    """The tool wired to the local services, a fake installer runner and a scripted person."""

    def __init__(self, tmp_path, fake, answers, npm=True):
        ws, self.caps = tmp_path / "ws", tmp_path / "caps"
        ws.mkdir(parents=True, exist_ok=True)
        self.index = CapabilityIndex(self.caps, ws, home=tmp_path / "home", appdata=tmp_path / "none")
        self.registry = ToolRegistry()
        self.registry.register("search_web", "Searches the web with a built-in engine", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: "r")
        self.settings = DiscoverySettings(self.caps)
        self.finder = Finder(self.settings, CandidateStore(), bases=fake.bases)
        self.runner = Runner()
        self.installer = Installer(self.caps, self.finder.store, index=self.index, runner=self.runner, which=lambda n: "npm" if (npm and n == "npm") else None)
        self.asked, self.answers = [], list(answers)
        self.asker = register_discovery_tools(self.registry, self.index, finder=self.finder, installer=self.installer, bases=fake.bases)
        self.asker.ask = self._ask

    def _ask(self, payload):
        self.asked.append(payload)
        answer = self.answers.pop(0)
        return answer(payload) if callable(answer) else answer

    def run(self, need="extract tables from pdf", **kw):
        return self.registry.execute("find_online_capability", {"need": need, **kw})


def test_the_choice_is_put_to_the_person_with_everything_they_need_to_decide(tmp_path, fake):
    s = Set(tmp_path, fake, ["cancel"])
    out = s.run()
    q = s.asked[0]
    assert q["kind"] == "capability_choice" and q["mode"] == "confirm" and q["free_prefix"] == "other:"
    cands = q["facts"]["candidates"]
    assert 1 <= len(cands) <= 5 and cands[0]["name"] == "pdf-mcp" and cands[0]["source"] == "registry" and cands[0]["version"] == "3.1.0"
    top = cands[0]
    assert top["score"] >= 70 and "60,000 downloads a week" in top["reasons"] and top["facts"][0]["code"] and top["command"].startswith("pdf-mcp")
    assert any(w["code"] == "runs_program" for w in top["warnings"]) and top["keys"] == []
    values = [o["value"] for o in q["options"]]
    assert values == [f"install:{i}" for i in range(1, len(cands) + 1)] + ["other", "cancel"]
    assert "popularity and upkeep, not safety" in q["question"]
    assert "chose not to install" in out and s.runner.calls == [] and not (s.caps / "mcp").exists()


def test_the_persons_choice_installs_that_candidate_and_the_model_is_told_how_to_go_on(tmp_path, fake):
    s = Set(tmp_path, fake, ["install:1"])
    out = s.run()
    assert "Installed mcp:pdf-mcp 3.1.0" in out and 'call_capability(id="mcp:pdf-mcp"' in out and "Go on with the task" in out
    assert s.index.get("mcp:pdf-mcp")["state"] == "approved"
    assert s.runner.calls and s.runner.calls[0][:2] == ["npm", "install"]


def test_a_remote_server_chosen_is_registered_without_a_download(tmp_path, fake):
    s = Set(tmp_path, fake, [lambda q: f"install:{[c['route'] for c in q['facts']['candidates']].index('remote') + 1}"])
    out = s.run()
    assert "Installed mcp:pdf-cloud" in out and "sign in" in out and s.runner.calls == []


def test_other_returns_what_the_person_said_in_their_own_words(tmp_path, fake):
    s = Set(tmp_path, fake, ["other:use the pdftotext command that is already installed"])
    out = s.run()
    assert "said: use the pdftotext command that is already installed" in out and s.runner.calls == []
    assert Set(tmp_path / "b", fake, ["other"]).run().startswith("The person wants something else")


def test_an_answer_that_was_cancelled_installs_nothing(tmp_path, fake):
    s = Set(tmp_path, fake, [""])
    assert "chose not to install" in s.run() and s.runner.calls == []
    s2 = Set(tmp_path / "b", fake, ["install:99"])
    assert "not one of the options" in s2.run() and s2.runner.calls == []


def test_the_best_is_installed_by_itself_only_when_the_person_turned_that_on_and_it_is_above_their_minimum(tmp_path, fake):
    s = Set(tmp_path, fake, ["cancel"])
    s.run()
    assert len(s.asked) == 1                                                  # off by default: it asks
    on = Set(tmp_path / "on", fake, [])
    on.settings.save({"auto_install": True, "min_score": 60})
    out = on.run()
    assert on.asked == [] and "Installed mcp:pdf-mcp" in out and "without asking" in out
    high = Set(tmp_path / "high", fake, ["cancel"])
    high.settings.save({"auto_install": True, "min_score": 100})
    high.run()
    assert len(high.asked) == 1 and high.runner.calls == []                   # nothing reaches the minimum: it asks


def test_without_a_way_to_ask_nothing_is_installed_and_the_options_come_back_as_text(tmp_path, fake):
    s = Set(tmp_path, fake, [])
    s.asker.ask = None
    out = s.run()
    assert "nothing was installed" in out and "pdf-mcp" in out and "score" in out and s.runner.calls == []


def test_what_the_model_can_pass_is_a_need_and_nothing_that_could_be_a_command(tmp_path, fake):
    s = Set(tmp_path, fake, [])
    tool = s.registry.get("find_online_capability")
    assert set(tool.parameters["properties"]) == {"need", "look_anyway"} and tool.untrusted is True
    assert tool.effect == EffectClass.REVERSIBLE
    from harness.tools.registry import ToolArgumentError
    for bad in ({"need": "pdf", "package": "evil"}, {"need": "pdf", "command": "rm -rf /"}, {"need": "pdf", "id": "c1"}):
        with pytest.raises(ToolArgumentError):
            s.registry.execute("find_online_capability", bad)


def test_what_is_already_here_is_offered_before_going_online(tmp_path, fake):
    s = Set(tmp_path, fake, ["cancel"])
    out = s.run("web search")
    assert "Already available" in out and "search_web" in out and s.asked == [] and fake.world.requests == []
    skill_dir = tmp_path / "home" / ".claude" / "skills" / "pdf-tables"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pdf-tables\ndescription: Extract tables from a PDF file\n---\nDo it.\n", encoding="utf-8")
    s.index.refresh()
    out = s.run()
    assert "skill:pdf-tables" in out and "pending" in out and "approves it in Settings" in out and fake.world.requests == []
    s.run(look_anyway=True)
    assert s.asked and fake.world.requests                                    # asked for it again: it looks online


def test_a_search_with_every_source_off_says_so_and_asks_nothing(tmp_path, fake):
    s = Set(tmp_path, fake, [])
    s.settings.save({"sources": {k: False for k in ("registry", "npm", "pypi", "github", "skills")}})
    assert "turned off" in s.run() and fake.world.requests == []


def test_a_missing_npm_is_said_and_only_what_can_be_installed_is_offered(tmp_path, fake):
    s = Set(tmp_path, fake, ["cancel"], npm=False)
    s.run()
    cands = s.asked[0]["facts"]["candidates"]
    assert cands and {c["route"] for c in cands} == {"remote"}
    assert any("npm is not installed" in n for n in s.asked[0]["facts"]["notes"])
    none = Set(tmp_path / "b", fake, [], npm=False)
    fake.world.registry = [i for i in fake.world.registry if "remotes" not in i["server"]]
    out = none.run()
    assert "npm is not installed" in out and none.asked == []


def test_an_install_that_fails_is_reported_not_hidden(tmp_path, fake):
    s = Set(tmp_path, fake, ["install:1"])
    s.runner.fail = lambda cmd: True
    out = s.run()
    assert "could not be installed" in out and "boom" in out and s.index.get("mcp:pdf-mcp") is None


def test_a_source_that_fails_is_named_and_the_others_still_answer(tmp_path, fake):
    s = Set(tmp_path, fake, ["cancel"])
    fake.world.github_limited = True
    s.run()
    top = s.asked[0]["facts"]["candidates"][0]
    assert any(w["code"] == "github_unchecked" for w in top["warnings"])


def test_find_capability_points_to_the_online_search_only_when_it_is_offered(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    index = CapabilityIndex(tmp_path / "caps", ws, home=tmp_path / "home", appdata=tmp_path / "none")
    registry = ToolRegistry()
    register_capability_tools(registry, index)
    assert "find_online_capability" not in registry.execute("find_capability", {"query": "zzzqqq"})
    index.online_hint = True
    assert "find_online_capability" in registry.execute("find_capability", {"query": "zzzqqq"})


# ---- the whole path: the process, the Chat's question and the model --------------------------------------------------------

class Feed:
    def __init__(self):
        import queue
        self.q = queue.Queue()

    def put(self, obj):
        self.q.put(json.dumps(obj) + "\n")

    def __iter__(self):
        return self

    def __next__(self):
        line = self.q.get(timeout=30)
        if line is None:
            raise StopIteration
        return line


def run_process(tmp_path, monkeypatch, fake, answers, need="extract tables from pdf"):
    import harness.session as hs
    from test_session import Scripted, call, say
    home = tmp_path / "home"
    (tmp_path / "data" / "capabilities").mkdir(parents=True)
    (tmp_path / "data" / "capabilities" / "discovery.json").write_text(json.dumps({"base_urls": fake.bases}), encoding="utf-8")
    for var, value in (("BASALT_DATA", tmp_path / "data"), ("HOME", home), ("USERPROFILE", home), ("APPDATA", tmp_path / "appdata")):
        monkeypatch.setenv(var, str(value))
    runner = Runner()
    real_which = shutil.which
    monkeypatch.setattr("harness.discovery.installer.default_runner", runner)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "npm" if name == "npm" else real_which(name, *a, **k))
    client = Scripted(call("find_online_capability", need=need), say("Done with the capability."))
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, _c=client: real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: hs.main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p1", "--capabilities",
                                                 "--permission-mode", "auto", "--no-memory", "--no-checkpoints", "--no-planner", "--heartbeat", "0.05"],
                                                stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "send", "id": "m1", "text": "I need to read the tables in a PDF"})

    def events():
        return [json.loads(line) for line in out.getvalue().splitlines()]
    deadline = time.monotonic() + 20
    ask = None
    while time.monotonic() < deadline and ask is None:
        ask = next((e for e in events() if e["event"] == "ask"), None)
        time.sleep(0.05)
    assert ask is not None, [e["event"] for e in events()]
    for value in answers:
        feed.put({"cmd": "answer", "id": ask["id"], "value": value})
        time.sleep(0.4)
    deadline = time.monotonic() + 20
    while '"done"' not in out.getvalue() and time.monotonic() < deadline:
        time.sleep(0.05)
    feed.put({"cmd": "close"})
    t.join(15)
    return events(), client, runner, ask


def test_the_question_reaches_the_chat_and_the_answer_installs(tmp_path, monkeypatch, fake):
    events, client, runner, ask = run_process(tmp_path, monkeypatch, fake, ["garbage", "install:1"])
    assert ask["kind"] == "capability_choice" and ask["facts"]["candidates"][0]["name"] == "pdf-mcp" and ask["free_prefix"] == "other:"
    assert any(e["event"] == "error" and e["code"] == "bad_answer" for e in events)                    # a made-up answer is not one of the choices
    done = next(e for e in events if e["event"] == "done")
    assert done["final"] == "Done with the capability."
    tool_msgs = [m["content"] for m in client.sent[1] if m["role"] == "tool"]
    assert "Installed mcp:pdf-mcp" in tool_msgs[-1] and "<untrusted_data" in tool_msgs[-1]
    installed = json.loads((tmp_path / "data" / "capabilities" / "installed.json").read_text(encoding="utf-8"))["items"]
    assert "mcp:pdf-mcp" in installed and runner.calls[0][:2] == ["npm", "install"]


def test_a_sentence_after_other_reaches_the_model_through_the_process(tmp_path, monkeypatch, fake):
    events, client, runner, ask = run_process(tmp_path, monkeypatch, fake, ["other:" + "try the pdftotext program " * 3])
    tool_msgs = [m["content"] for m in client.sent[1] if m["role"] == "tool"]
    assert "said: try the pdftotext program" in tool_msgs[-1] and runner.calls == []
    assert not (tmp_path / "data" / "capabilities" / "installed.json").exists()


def test_a_sentence_that_is_too_long_is_not_an_answer(tmp_path, monkeypatch, fake):
    events, client, runner, ask = run_process(tmp_path, monkeypatch, fake, ["other:" + "x" * 600, "cancel"])
    assert any(e["event"] == "error" and e["code"] == "bad_answer" for e in events)
    assert "chose not to install" in [m["content"] for m in client.sent[1] if m["role"] == "tool"][-1]


def test_a_server_installed_by_basalt_starts_with_the_one_key_it_asks_for_from_the_vault(tmp_path):
    from harness.capabilities import mcp_spec_sha
    from harness.discovery.installed import InstalledStore
    from harness.stack import AgentConfig, build_agent
    from harness.vault import Vault
    from types import SimpleNamespace
    caps, ws, home = tmp_path / "caps", tmp_path / "ws", tmp_path / "home"
    ws.mkdir()
    command = [sys.executable, str(Path(__file__).resolve().parent / "fake_search_mcp.py")]
    sha = mcp_spec_sha(command[0], command[1:])
    InstalledStore(caps).put({"id": "mcp:search", "name": "search", "kind": "mcp", "command": command, "sha": sha, "description": "fake search",
                              "env": [{"name": "FAKE_SEARCH_KEY", "required": True, "secret": True, "entry": "FAKE_SEARCH_KEY", "value": ""}]})
    CapabilityIndex(caps, ws, home=home, appdata=tmp_path / "none").approvals.approve(SimpleNamespace(name="mcp:search", sha256=sha))
    vault = Vault(tmp_path / "vault")

    def build():
        return build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, capabilities=True,
                                       capabilities_dir=str(caps), capabilities_home=str(home), vault_dir=str(tmp_path / "vault")), client=object())
    built = build()
    assert built.agent.tools.get("find_online_capability") is not None and getattr(built.agent.tools, "capability_ask", None) is not None
    out = built.agent.tools.execute("call_capability", {"id": "mcp:search", "tool": "web_search", "arguments": {"query": "q"}})
    assert "needs FAKE_SEARCH_KEY" in out and "vault" in out                  # no key yet: the server is not started
    vault.put("FAKE_SEARCH_KEY", kind="secret", label="key", fields={"value": "sekret-abc-123456"})
    out = built.agent.tools.execute("call_capability", {"id": "mcp:search", "tool": "web_search", "arguments": {"query": "q"}})
    assert "key=sekret-abc-123456" in out                                      # the server got it
    assert "sekret-abc-123456" not in built.agent.secrets.mask_secrets(out)    # and what goes on to the model does not carry it
    assert "sekret-abc-123456" not in "".join(f.read_text(encoding="utf-8", errors="ignore") for f in caps.rglob("*") if f.is_file())
    built.close()
