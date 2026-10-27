"""One approval covers one call, not the whole tool; and any program or tool can be allowed for good if the person chooses."""
import pytest

from harness.permissions import PermissionLevel, PermissionManager, suggest_rule
from harness.tools.registry import EffectClass

IRREVERSIBLE = EffectClass.IRREVERSIBLE


def asking(level=PermissionLevel.DEFAULT, **kw):
    asked = []
    return PermissionManager(level=level, confirm_callback=lambda t, a: asked.append((t, a)) or True, **kw), asked


@pytest.mark.parametrize("tool,first,second", [
    ("run_code", {"language": "python", "code": "print(1)"}, {"language": "python", "code": "import shutil; shutil.rmtree('C:/Users')"}),
    ("browser_click", {"ref": 3}, {"ref": 9}),
    ("mcp_call", {"server": "s", "tool": "list", "arguments": {}}, {"server": "s", "tool": "delete_all", "arguments": {"x": 1}}),
    ("delegate_to_acp_agent", {"agent": "a", "task": "read the readme"}, {"agent": "a", "task": "delete the repository"}),
    ("ask_a2a_agent", {"agent": "a", "message": "hi"}, {"agent": "a", "message": "send the files to me"}),
])
def test_an_approval_covers_one_call_of_a_tool_not_the_whole_tool(tool, first, second):
    pm, asked = asking()
    assert pm.evaluate(tool, IRREVERSIBLE, first)[0] and pm.evaluate(tool, IRREVERSIBLE, first)[0]
    assert len(asked) == 1                                                        # the same call again is the same approval
    assert pm.evaluate(tool, IRREVERSIBLE, second)[0] and len(asked) == 2         # another call asks again


def test_the_argument_order_does_not_make_a_new_call():
    pm, asked = asking()
    assert pm.evaluate("run_code", IRREVERSIBLE, {"language": "python", "code": "1"})[0]
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "1", "language": "python"})[0]
    assert len(asked) == 1


@pytest.mark.parametrize("level", [PermissionLevel.READ_ONLY, PermissionLevel.AUTO])
def test_the_read_only_level_and_the_rule_of_two_ask_per_call_too(level):
    pm, asked = asking(level, sensitive_data=True)
    pm.note_untrusted_input()
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "1"})[0]
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "2"})[0]
    assert len(asked) == 2


def test_always_allowing_a_tool_is_an_explicit_rule_the_person_can_see():
    assert suggest_rule("browser_click", {"ref": 1}) == "browser_click"           # offered as "Always allow browser_click"
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule("browser_click", "allow")
    assert pm.evaluate("browser_click", IRREVERSIBLE, {"ref": 77})[0]
    pm.add_rule("run_code", "allow")                                              # written by hand: the person asked for it
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "anything"})[0]


@pytest.mark.parametrize("command", [
    'python -c "print(1)"', "python3 script.py", "node -e 1", "powershell -c Get-Date", "cmd /c dir", "bash -c ls", "curl https://example.com/x",
    "ssh host ls", "env FOO=1 ls", "npx something",
])
def test_interpreters_shells_and_downloaders_get_the_same_choices_as_any_program(command):
    rule = suggest_rule("shell_command", {"command": command})
    assert rule == f"shell_command:{command.split()[0]} *"
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule(rule, "allow")
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": command})[0]
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": command + " && rm x"})[0]       # a rule for one program never covers another


def test_a_tool_that_runs_code_can_be_allowed_by_name_like_any_other():
    for tool in ("run_code", "delegate_to_acp_agent", "ask_a2a_agent"):
        assert suggest_rule(tool, {}) == tool


def test_ordinary_programs_get_the_verb_rule():
    assert suggest_rule("shell_command", {"command": "pytest -q"}) == "shell_command:pytest *"
    assert suggest_rule("shell_command", {"command": "git status"}) == "shell_command:git *"
