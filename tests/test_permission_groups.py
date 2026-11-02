"""Permission rules that name a group of tools (`group:read`, `group:shell`) and the rule that refuses a tool."""
from tadamoo.permissions import GROUPS, PermissionLevel, PermissionManager, group_of
from tadamoo.tools.registry import EffectClass


def test_a_group_rule_covers_every_tool_of_the_group_and_no_other():
    perms = PermissionManager(level=PermissionLevel.AUTO)
    perms.add_rule("group:shell", "deny")
    assert not perms.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": "dir"})[0]
    assert not perms.evaluate("run_code", EffectClass.IRREVERSIBLE, {"code": "1"})[0]
    assert perms.evaluate("write_file", EffectClass.REVERSIBLE, {"path": "a"})[0]


def test_a_pattern_in_a_group_covers_the_tools_it_matches():
    perms = PermissionManager(level=PermissionLevel.AUTO)
    perms.add_rule("group:browser", "deny")
    assert not perms.evaluate("browser_click", EffectClass.IRREVERSIBLE, {})[0] and perms.evaluate("browse_url", EffectClass.PURE, {})[0]


def test_an_allow_group_answers_the_question_for_its_tools():
    asked = []
    perms = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: asked.append(t) or False)
    perms.add_rule("group:write", "allow")
    assert perms.evaluate("edit_file", EffectClass.IRREVERSIBLE, {"path": "a"})[0] and not asked


def test_the_rule_that_refuses_a_tool_is_named():
    perms = PermissionManager(level=PermissionLevel.AUTO)
    perms.add_rule("group:web", "deny")
    assert perms.rule_for("browse_url").pattern == "group:web" and perms.rule_for("read_file") is None


def test_an_unknown_group_matches_nothing():
    perms = PermissionManager(level=PermissionLevel.AUTO)
    perms.add_rule("group:nope", "deny")
    assert group_of("group:nope") is None and perms.evaluate("write_file", EffectClass.REVERSIBLE, {"path": "a"})[0]
    assert set(GROUPS) >= {"read", "write", "shell", "web"}
