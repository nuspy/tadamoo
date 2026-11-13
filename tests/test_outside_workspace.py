"""A file tool reaching outside the working folder: the permission level decides it, as for any other call. Auto lets it go, the other
levels ask, a deny rule refuses it, and the person's secrets are refused at every level."""
from pathlib import Path

import pytest

from tadamoo.permissions import PermissionLevel, PermissionManager
from tadamoo.tools import WorkspaceSandbox, create_standard_tool_registry


def build(tmp_path, level=PermissionLevel.DEFAULT, answer=True, asked=None, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "inside.txt").write_bytes(b"inside text\n")
    other = tmp_path / "elsewhere"
    (other / "deep").mkdir(parents=True, exist_ok=True)
    (other / "note.txt").write_bytes(b"outside text\n")
    (other / "deep" / "more.txt").write_bytes(b"outside again\n")

    def confirm(tool, arguments):
        if asked is not None:
            asked.append((tool, arguments))
        return answer

    perms = PermissionManager(level=level, confirm_callback=confirm, **kw)
    registry, sandbox = create_standard_tool_registry(ws, permissions=perms)
    return registry, sandbox, other, perms


def text_of(path):
    return path.read_bytes().decode("utf-8")


# -- automatic: nothing is asked ----------------------------------------------------------------------------------------


def test_at_the_auto_level_a_file_tool_reads_and_writes_outside_without_asking(tmp_path):
    asked = []
    registry, sandbox, other, _ = build(tmp_path, PermissionLevel.AUTO, asked=asked)
    assert sandbox.read_file(str(other / "note.txt")) == "outside text\n"
    assert sandbox.write_file(str(other / "made.txt"), "new\n")["status"] == "success"
    assert text_of(other / "made.txt") == "new\n"
    assert sandbox.edit_file(str(other / "note.txt"), "outside", "changed")["status"] == "success"
    assert text_of(other / "note.txt") == "changed text\n"
    assert any(e["name"] == "note.txt" for e in sandbox.list_directory(str(other)))
    assert asked == []


def test_at_the_auto_level_the_search_tools_walk_a_folder_outside(tmp_path):
    asked = []
    registry, _, other, _ = build(tmp_path, PermissionLevel.AUTO, asked=asked)
    found = registry.execute("search_code", {"pattern": "outside", "path": str(other)})
    assert "note.txt" in found and "more.txt" in found and other.as_posix() in found
    tree = registry.execute("tree", {"path": str(other)})
    assert "note.txt" in tree and "deep" in tree
    assert asked == []


# -- ask first: it asks, it does not refuse ------------------------------------------------------------------------------


def test_at_the_ask_first_level_a_path_outside_is_asked_about_and_a_yes_lets_it_go(tmp_path):
    asked = []
    registry, sandbox, other, _ = build(tmp_path, PermissionLevel.DEFAULT, answer=True, asked=asked)
    assert sandbox.read_file(str(other / "note.txt")) == "outside text\n"
    assert asked and asked[0][0] == "read_file"
    assert str(other / "note.txt") in asked[0][1]["path"] and "outside the working folder" in asked[0][1]["outside_workspace"]


def test_at_the_ask_first_level_a_no_refuses_the_call_and_writes_nothing(tmp_path):
    asked = []
    registry, sandbox, other, _ = build(tmp_path, PermissionLevel.DEFAULT, answer=False, asked=asked)
    with pytest.raises(PermissionError, match="outside the working folder"):
        sandbox.read_file(str(other / "note.txt"))
    with pytest.raises(PermissionError):
        sandbox.write_file(str(other / "made.txt"), "new\n")
    assert not (other / "made.txt").exists()
    assert [t for t, _ in asked] == ["read_file", "write_file"]


def test_one_answer_covers_the_folder_for_a_tool_that_walks_it(tmp_path):
    asked = []
    registry, sandbox, other, _ = build(tmp_path, PermissionLevel.DEFAULT, answer=True, asked=asked)
    assert any(e["name"] == "note.txt" for e in sandbox.list_directory(str(other)))
    assert any(e["name"] == "more.txt" for e in sandbox.list_directory(str(other / "deep")))
    registry.execute("search_code", {"pattern": "outside", "path": str(other)})
    registry.execute("tree", {"path": str(other)})
    assert [t for t, _ in asked] == ["list_directory", "search_code", "tree"]


def test_a_file_tool_asks_for_each_path_and_a_pass_at_auto_is_not_an_answer(tmp_path):
    asked = []
    registry, sandbox, other, perms = build(tmp_path, PermissionLevel.DEFAULT, answer=True, asked=asked)
    third = other / "third.txt"
    third.write_bytes(b"third\n")
    sandbox.read_file(str(other / "note.txt"))
    sandbox.read_file(str(other / "note.txt"))
    sandbox.read_file(str(other / "deep" / "more.txt"))
    assert len(asked) == 2                                     # the same file twice is one answer, another file is another
    perms.level = PermissionLevel.AUTO
    sandbox.read_file(str(third))
    assert len(asked) == 2                                     # at the auto level nothing is asked
    perms.level = PermissionLevel.DEFAULT
    sandbox.read_file(str(third))
    assert len(asked) == 3                                     # a pass at the auto level is not the person's answer


def test_the_persons_allow_rule_for_the_tool_answers_the_question(tmp_path):
    asked = []
    registry, sandbox, other, perms = build(tmp_path, PermissionLevel.DEFAULT, answer=False, asked=asked)
    perms.add_rule("read_file", "allow")
    assert sandbox.read_file(str(other / "note.txt")) == "outside text\n"
    assert asked == []


# -- what still holds at every level ------------------------------------------------------------------------------------


def test_a_deny_rule_refuses_a_path_outside_even_at_the_auto_level(tmp_path):
    registry, sandbox, other, perms = build(tmp_path, PermissionLevel.AUTO)
    perms.add_rule("write_file", "deny")
    with pytest.raises(PermissionError, match="denied by rule"):
        sandbox.write_file(str(other / "made.txt"), "new\n")
    assert not (other / "made.txt").exists()


def test_plan_mode_asks_about_a_read_outside_and_holds_back_a_write(tmp_path):
    asked = []
    registry, sandbox, other, perms = build(tmp_path, PermissionLevel.DEFAULT, asked=asked, plan_mode=True)
    assert "outside text" in sandbox.read_file(str(other / "note.txt"))
    assert asked and asked[0][0] == "read_file"
    with pytest.raises(PermissionError, match="Plan mode"):
        sandbox.write_file(str(other / "new.txt"), "x")
    assert perms.plan_mode


def test_the_persons_secrets_are_refused_outside_at_every_level(tmp_path):
    for level in (PermissionLevel.DEFAULT, PermissionLevel.AUTO):
        registry, sandbox, other, _ = build(tmp_path, level)
        secret = other / "vault.json"
        secret.write_bytes(b"{}")
        sandbox.protected_hit = lambda p, s=secret: str(s) if Path(p) == s else ""
        with pytest.raises(PermissionError, match="one of the person's secrets"):
            sandbox.read_file(str(secret))


def test_paths_inside_the_working_folder_never_ask(tmp_path):
    for level in (PermissionLevel.READ_ONLY, PermissionLevel.DEFAULT, PermissionLevel.AUTO):
        asked = []
        registry, sandbox, other, _ = build(tmp_path, level, answer=False, asked=asked)
        assert sandbox.read_file("inside.txt") == "inside text\n"
        assert sandbox.write_file(f"new-{level.value}.txt", "x\n")["status"] == "success"
        assert asked == []


def test_a_sandbox_with_no_permission_manager_still_keeps_the_folder_boundary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    sandbox = WorkspaceSandbox(ws)
    with pytest.raises(PermissionError, match="escapes workspace boundary"):
        sandbox.read_file(str(tmp_path / "elsewhere.txt"))