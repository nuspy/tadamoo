"""The verify gate: the project's own commands run before a turn ends after edits, a failure goes back to the model, and the gate keeps to
its rounds, its permissions and the Target Loop's own checks."""
import copy
import json
import sys

import pytest

from harness import gate as gate_module
from harness import target as tg
from harness.agent_loop import FINAL, VERIFY_FAILED, AgentLoop
from harness.gate import CommandResult, VerifyGate, clean_commands, read_result, trim_output
from harness.permissions import PermissionLevel, PermissionManager
from harness.tools import create_standard_tool_registry


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def call(name, args, call_id="c1"):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def write(path, text, call_id="c1"):
    return call("write_file", {"path": path, "content": text, "overwrite": True}, call_id)


CHECK = "import sys\nsys.exit(0 if open('state.txt').read().strip() == 'good' else 1)\n"
CHECK_COMMAND = f'"{sys.executable}" check.py'


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "check.py").write_text(CHECK, encoding="utf-8")
    return root


def build(ws, client, commands=(CHECK_COMMAND,), mode="always", rounds=3, level=PermissionLevel.DEFAULT, unattended=False, events=None, **kw):
    registry, sandbox = create_standard_tool_registry(ws)
    edits = lambda: len(sandbox.changes)
    gate = VerifyGate(list(commands), mode, rounds, timeout=60, edits=edits, unattended=unattended)
    agent = AgentLoop(client, registry, permissions=PermissionManager(level=level), gate=gate, changes=edits, on_event=events.append if events is not None else None, **kw)
    return agent, gate, sandbox


def hidden(client):
    return [m["content"] for m in client.sent[-1] if m["role"] == "user" and m["content"].startswith("[harness]")]


# -- the policy ---------------------------------------------------------------------------------------------------------

def test_the_gate_is_off_by_default_and_on_only_as_the_person_chose():
    commands = ["pytest -q"]
    assert VerifyGate(commands).enabled is False                                                 # nothing listed as on: off
    assert VerifyGate(commands, "always").enabled is True
    assert VerifyGate(commands, "unattended").enabled is False and VerifyGate(commands, "unattended", unattended=True).enabled is True
    assert VerifyGate([], "always").enabled is False                                             # on with nothing to run runs nothing
    assert VerifyGate(commands, "sometimes").mode == "off"
    assert VerifyGate(commands, "always", rounds=99).max_rounds == 3 and VerifyGate(commands, "always", rounds=0).max_rounds == 3


def test_commands_are_cleaned_and_a_result_is_read_from_the_shell_s_answer():
    assert clean_commands("pytest -q\n\n  pytest -q \nruff check .") == ["pytest -q", "ruff check ."]
    assert len(clean_commands([f"c{i}" for i in range(20)])) == gate_module.MAX_COMMANDS
    ok = read_result("t", {"exit_code": 0, "stdout": "1 passed", "stderr": ""}, "executed", False)
    assert ok.ok and ok.ran and not ok.unusable
    bad = read_result("t", {"exit_code": 1, "stdout": "1 failed", "stderr": ""}, "executed", False)
    assert not bad.ok and not bad.unusable
    for output in ({"exit_code": 127, "stdout": "", "stderr": "x"}, {"exit_code": 1, "stdout": "", "stderr": "'pytest' is not recognized as an internal or external command"},
                   {"exit_code": 5, "stdout": "collected 0 items\nno tests ran", "stderr": ""}):
        assert read_result("t", output, "executed", False).unusable
    assert read_result("t", "Permission denied", "denied", True).unusable
    lines = "\n".join(f"line {i}" if i != 3 else "AssertionError: boom" for i in range(300))
    shown = trim_output(lines)
    assert "AssertionError: boom" in shown and "line 299" in shown and "line 100" not in shown and "left out" in shown


# -- in the loop --------------------------------------------------------------------------------------------------------

def test_a_turn_without_edits_is_not_checked(ws):
    client = Scripted(say("Nothing to change."))
    agent, gate, _ = build(ws, client)
    agent.run("hello")
    assert agent.stop_reason == FINAL and len(client.sent) == 1


def test_a_failed_check_goes_back_to_the_model_and_a_fix_ends_the_turn(ws):
    client = Scripted(write("state.txt", "bad"), say("Done."), write("state.txt", "good", "c2"), say("Fixed it."))
    events = []
    agent, gate, _ = build(ws, client, events=events)
    agent.run("make it good")
    assert agent.stop_reason == FINAL and len(client.sent) == 4
    message = hidden(client)[-1]
    assert "checks failed" in message and "try 1 of 3" in message and CHECK_COMMAND in message and "exit code 1" in message
    assert "<untrusted_data" in message                                                         # what a command printed is data
    gate_events = [e for e in events if e["type"] == "gate"]
    assert [e["decision"] for e in gate_events] == ["retry", "pass"] and gate_events[0]["results"][0]["ok"] is False
    assert any(e["type"] == "tool_start" and e.get("gate") for e in events)                      # the runs are shown like any call
    assert agent.messages[-1]["content"] == "Fixed it."


def test_at_most_three_fixes_then_the_run_ends_as_verify_failed(ws):
    def edit(i):
        return write("state.txt", "bad " + "x" * (i + 1) + chr(97 + i), f"w{i}")
    steps = [edit(0), say("a")]
    for i in range(1, 6):
        steps += [edit(i), say(f"b{i}")]
    # the output differs each time, so the same-failure stop does not apply: the rounds do
    (ws / "check.py").write_text("import sys\nprint(open('state.txt').read())\nsys.exit(1)\n", encoding="utf-8")
    client = Scripted(*steps)
    agent, gate, _ = build(ws, client, rounds=3)
    agent.run("go")
    assert agent.stop_reason == VERIFY_FAILED and "still fail after 3 fixes" in agent.error and len(client.sent) == 8


def test_the_same_failure_twice_ends_the_run_early(ws):
    client = Scripted(write("state.txt", "bad"), say("a"), write("state.txt", "bad", "c2"), say("b"), say("never"))
    agent, gate, _ = build(ws, client)
    agent.run("go")
    assert agent.stop_reason == VERIFY_FAILED and "same result" in agent.error and len(client.sent) == 4


def test_a_check_that_cannot_run_is_not_the_model_s_bug(ws):
    client = Scripted(write("state.txt", "x"), say("Done."), say("never"))
    agent, gate, _ = build(ws, client, commands=['"%s" -c "import nosuchtoolxyz"' % sys.executable, "definitely-not-a-command-xyz"])
    agent.run("go")
    assert agent.stop_reason == VERIFY_FAILED and "could not be run" in agent.error and len(client.sent) == 2


def test_the_gate_only_looks_after_files_changed_since_it_last_looked(ws):
    client = Scripted(write("state.txt", "good"), say("a"), say("Nothing more."))
    agent, gate, _ = build(ws, client)
    agent.run("first")
    assert agent.stop_reason == FINAL and len(client.sent) == 2
    agent.run("second, no edits")                       # same sandbox, nothing changed since: no run
    assert agent.stop_reason == FINAL and len(client.sent) == 3


def test_the_listed_command_runs_without_asking_but_a_deny_rule_still_holds(ws):
    asked = []
    registry, sandbox = create_standard_tool_registry(ws)
    perms = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda tool, args: asked.append(tool) or False)
    edits = lambda: len(sandbox.changes)
    gate = VerifyGate([CHECK_COMMAND], "always", edits=edits)
    agent = AgentLoop(Scripted(write("state.txt", "good"), say("a")), registry, permissions=perms, gate=gate, changes=edits)
    agent.run("go")
    assert agent.stop_reason == FINAL and asked == []                                         # it ran, and nobody was asked
    perms2 = PermissionManager(level=PermissionLevel.DEFAULT)
    perms2.add_rule("shell_command:*check.py*", "deny")
    registry2, sandbox2 = create_standard_tool_registry(ws)
    edits2 = lambda: len(sandbox2.changes)
    agent2 = AgentLoop(Scripted(write("state.txt", "good2"), say("a")), registry2, permissions=perms2,
                       gate=VerifyGate([CHECK_COMMAND], "always", edits=edits2), changes=edits2)
    agent2.run("go")
    assert agent2.stop_reason == VERIFY_FAILED and "could not be run" in agent2.error          # the person's own deny rule won


def test_the_unattended_mode_is_for_unattended_conversations_only(ws):
    for unattended, expect in ((False, 2), (True, 4)):
        steps = [write("state.txt", "bad"), say("a")] + ([write("state.txt", "good", "c2"), say("b")] if unattended else [])
        client = Scripted(*steps)
        agent, gate, _ = build(ws, client, mode="unattended", unattended=unattended)
        agent.run("go")
        assert len(client.sent) == expect, unattended


def test_a_subagent_has_no_gate_of_its_own(ws):
    client = Scripted(write("state.txt", "bad"), say("a"))
    agent, gate, _ = build(ws, client, subagent=True)
    agent.run("go")
    assert agent.stop_reason == FINAL and len(client.sent) == 2


# -- with the Target Loop -----------------------------------------------------------------------------------------------

def tool(command, ok, gate=False, effect="irreversible"):
    body = json.dumps({"exit_code": 0 if ok else 1, "stdout": "out", "stderr": ""})
    return {"name": "shell_command", "arguments": {"command": command}, "status": "completed" if ok else "failed", "output": body, "effect": effect, **({"gate": True} if gate else {})}


class Quiet:
    """Enough of a loop for `_note_gate` and `_checks`: it records the commands the Target Loop itself asks the conversation to run."""

    def __init__(self):
        self.ran = []

    def run(self, prompt, cancel):
        self.ran.append(prompt)
        return tg.TurnResult(final="ok", tools=[tool("pytest -q", True)])

    def file_exists(self, path):
        return True


def target_loop(conv):
    target = tg.new_target("make it work", {"verify": ["pytest -q"]}, session="s")
    return tg.TargetLoop(target, conv, llm=None, registry=None, save=lambda t: None)


def test_a_check_the_gate_just_ran_is_not_run_a_second_time_by_the_target_loop():
    conv = Quiet()
    loop = target_loop(conv)
    loop._note_gate(tg.TurnResult(tools=[tool("write", True, effect="reversible"), tool("pytest -q", True, gate=True)]))
    loop._ensure_check_criteria()
    checks = loop._checks(tg.Evidence())
    assert conv.ran == [] and checks[0]["ok"] is True and "verify gate" in checks[0]["output"]


def test_a_failed_gate_run_is_a_failed_check_and_a_later_edit_makes_it_stale():
    conv = Quiet()
    loop = target_loop(conv)
    loop._note_gate(tg.TurnResult(tools=[tool("pytest -q", False, gate=True)]))
    loop._ensure_check_criteria()
    assert loop._checks(tg.Evidence())[0]["ok"] is False and conv.ran == []
    loop._note_gate(tg.TurnResult(tools=[tool("pytest -q", True, gate=True), tool("edit", True, effect="reversible")]))
    assert loop.gate_runs == {}                                                                  # a change after the gate's run: the check runs itself
    assert loop._checks(tg.Evidence())[0]["ok"] is True and len(conv.ran) == 1
