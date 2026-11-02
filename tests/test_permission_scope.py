"""What a person can allow: this call, this conversation or for good, for any program or tool; a command with several statements is allowed
when each statement is covered; at the Auto level nothing is asked."""
import pytest

from harness.permissions import PermissionLevel, PermissionManager, statements, suggest_rules
from harness.tools.registry import EffectClass

IRREVERSIBLE = EffectClass.IRREVERSIBLE


def asking(level=PermissionLevel.DEFAULT, **kw):
    asked = []
    return PermissionManager(level=level, confirm_callback=lambda t, a: asked.append((t, a)) or True, **kw), asked


@pytest.mark.parametrize("tool,first,second", [
    ("run_code", {"language": "python", "code": "print(1)"}, {"language": "python", "code": "import shutil; shutil.rmtree('C:/Users')"}),
    ("browser_click", {"ref": 3}, {"ref": 9}),
    ("mcp_call", {"server": "s", "tool": "list", "arguments": {}}, {"server": "s", "tool": "delete_all", "arguments": {"x": 1}}),
])
def test_an_approval_of_one_call_covers_that_call_and_the_next_different_call_asks_again(tool, first, second):
    pm, asked = asking()
    assert pm.evaluate(tool, IRREVERSIBLE, first)[0] and pm.evaluate(tool, IRREVERSIBLE, first)[0]
    assert len(asked) == 1
    assert pm.evaluate(tool, IRREVERSIBLE, second)[0] and len(asked) == 2


def test_the_argument_order_does_not_make_a_new_call():
    pm, asked = asking()
    assert pm.evaluate("run_code", IRREVERSIBLE, {"language": "python", "code": "1"})[0]
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "1", "language": "python"})[0]
    assert len(asked) == 1


def test_a_rule_the_person_chose_covers_the_tool_or_the_program_for_good():
    assert suggest_rules("browser_click", {"ref": 1}) == ["browser_click"]
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule("browser_click", "allow")
    assert pm.evaluate("browser_click", IRREVERSIBLE, {"ref": 77})[0]
    pm.add_rule("run_code", "allow")
    assert pm.evaluate("run_code", IRREVERSIBLE, {"code": "anything"})[0]
    pm.add_rule("shell_command", "allow")                                         # the tool's own name: every command
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": "anything at all && more"})[0]


@pytest.mark.parametrize("command", [
    'python -c "print(1)"', "python3 script.py", "node -e 1", "powershell -c Get-Date", "cmd /c dir", "bash -c ls", "curl https://example.com/x",
    "ssh host ls", "env FOO=1 ls", "npx something",
])
def test_interpreters_shells_and_downloaders_get_the_same_choices_as_any_program(command):
    assert suggest_rules("shell_command", {"command": command}) == [f"shell_command:{command.split()[0]} *"]
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule(suggest_rules("shell_command", {"command": command})[0], "allow")
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": command})[0]


SCRIPT = ('curl.exe -s -c jar.txt -o p1.html "https://shop.example/a;b/"' + chr(10)
          + 'Write-Output "=== cookie jar ==="' + chr(10)
          + 'type jar.txt')


def test_a_command_of_several_statements_is_offered_one_rule_for_each_and_allowed_when_all_are_covered():
    assert statements(SCRIPT) == ['curl.exe -s -c jar.txt -o p1.html "https://shop.example/a;b/"', 'Write-Output "=== cookie jar ==="', "type jar.txt"]
    assert suggest_rules("shell_command", {"command": SCRIPT}) == ["shell_command:curl.exe *", "shell_command:Write-Output *", "shell_command:type *"]
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    for rule in suggest_rules("shell_command", {"command": SCRIPT})[:2]:
        pm.add_rule(rule, "allow")
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": SCRIPT})[0]            # `type` is not covered yet
    pm.add_rule("shell_command:type *", "allow")
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": SCRIPT})[0]
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": "type jar.txt | Write-Output x 2>&1"})[0]
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": "type jar.txt && del /s *"})[0]       # an uncovered statement still asks


def test_a_command_built_at_run_time_is_offered_no_rule_and_asks_each_time():
    assert statements("echo $(whoami)") is None and suggest_rules("shell_command", {"command": "echo $(whoami)"}) == []
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule("shell_command:echo *", "allow")
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": "echo $(whoami)"})[0]


def test_a_deny_rule_still_wins_over_an_allow_rule():
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: True)
    pm.add_rule("shell_command", "allow")
    pm.add_rule("shell_command:rm *", "deny")
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": "ls && rm -rf x"})[0]


def test_a_statement_with_no_plain_verb_is_allowed_only_as_itself():
    rules = suggest_rules("shell_command", {"command": "$x = 1"})
    pm = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: False)
    pm.add_rule(rules[0], "allow")
    assert pm.evaluate("shell_command", IRREVERSIBLE, {"command": "$x = 1"})[0]
    assert not pm.evaluate("shell_command", IRREVERSIBLE, {"command": "$x = 2"})[0]


def test_ordinary_programs_get_the_verb_rule_and_a_payment_or_a_phone_call_none():
    assert suggest_rules("shell_command", {"command": "pytest -q"}) == ["shell_command:pytest *"]
    assert suggest_rules("shell_command", {"command": "git status"}) == ["shell_command:git *"]
    assert suggest_rules("phone_call", {}) == ["phone_call"] and suggest_rules("request_payment", {}) == ["request_payment"]
    assert suggest_rules("write_file:/r/.git/hooks/x", {}) == ["write_file:/r/.git/hooks/x"]


@pytest.mark.parametrize("tool", ["phone_call", "shell_command", "run_code", "browser_click", "write_file"])
def test_at_the_auto_level_only_the_outward_call_after_outside_content_and_a_secret_is_asked(tool):
    asked = []
    pm = PermissionManager(level=PermissionLevel.AUTO, confirm_callback=lambda t, a: asked.append(t) or False, sensitive_data=True)
    pm.note_untrusted_input()
    pm.note_private_data()
    assert pm.evaluate(tool, IRREVERSIBLE, {"command": "rm x", "code": "1"})[0] and not asked           # not outward: nothing is asked
    assert not pm.evaluate(tool, IRREVERSIBLE, {"command": "rm x", "code": "1"}, outward=True)[0] and asked
