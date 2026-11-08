"""Read-before-write guards, the repeated-read answer, the per-file failure counter and the syntax check after a write."""
import json

import pytest

from tadamoo.agent_loop import AgentLoop, FINAL
from tadamoo.editing import EditError
from tadamoo.tools import create_standard_tool_registry
from tadamoo.tools.filesystem import WorkspaceSandbox
from test_agent_loop import ScriptedClient, call, say


@pytest.fixture
def sb(tmp_path):
    return WorkspaceSandbox(tmp_path / "ws")


def put(sb, name, text):
    (sb.root / name).write_bytes(text.encode("utf-8"))


BIG = "".join(f"line {i:03d} with some text\n" for i in range(1, 41))


# -- whole-file writes -----------------------------------------------------------------------------------------


def test_a_new_file_is_written_without_reading_anything(sb):
    assert sb.write_file("new.txt", "hello\n")["status"] == "success"


def test_overwriting_a_file_never_read_is_refused_with_the_way_out(sb):
    put(sb, "a.txt", BIG)
    out = sb.write_file("a.txt", "different\n")
    assert out["status"] == "refused" and out["harness_refusal"] is True
    assert "not read it" in out["message"] and "overwrite=true" in out["message"]
    assert (sb.root / "a.txt").read_text(encoding="utf-8") == BIG and sb.changes == []
    assert sb.write_file("a.txt", "different\n", overwrite=True)["status"] == "success"


def test_a_file_read_in_full_can_be_overwritten(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt")
    assert sb.write_file("a.txt", BIG + "one more\n")["status"] == "success"


def test_a_file_read_in_ranges_that_cover_it_counts_as_read_in_full(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt", offset=1, limit=20)
    assert sb.write_file("a.txt", BIG)["status"] == "refused"                      # half of it is not enough
    sb.read_file("a.txt", offset=21, limit=0)
    assert sb.write_file("a.txt", BIG + "x\n")["status"] == "success"


def test_a_file_read_in_part_is_refused_and_the_message_says_so(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt", offset=1, limit=5)
    out = sb.write_file("a.txt", BIG)
    assert out["status"] == "refused" and "only part" in out["message"] and "40 lines" in out["message"]


def test_a_file_changed_on_disk_since_the_read_is_refused(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt")
    put(sb, "a.txt", BIG + "added by someone else\n")
    out = sb.write_file("a.txt", BIG)
    assert out["status"] == "refused" and "changed on disk" in out["message"]
    sb.read_file("a.txt")
    assert sb.write_file("a.txt", BIG)["status"] == "success"


def test_what_a_tool_wrote_itself_can_be_written_again_without_a_new_read(sb):
    sb.write_file("a.txt", BIG)
    assert sb.write_file("a.txt", BIG + "x\n")["status"] == "success"
    assert sb.write_file("a.txt", BIG + "x\ny\n")["status"] == "success"


def test_an_edit_keeps_the_read_state_and_does_not_make_the_file_known(sb):
    put(sb, "a.txt", BIG)
    sb.edit_file("a.txt", "line 001", "line one")
    assert sb.write_file("a.txt", BIG)["status"] == "refused"                    # it was never read
    sb.read_file("a.txt")
    sb.edit_file("a.txt", "line 002", "line two")
    assert sb.write_file("a.txt", BIG)["status"] == "success"                    # read in full, then its own edit


def test_a_command_that_changes_a_file_makes_it_stale(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt")
    with sb.watching():
        put(sb, "a.txt", BIG.upper())
    assert sb.write_file("a.txt", BIG)["status"] == "refused"


def test_a_partial_summary_is_refused_unless_overwrite(sb):
    put(sb, "a.py", BIG)
    sb.read_file("a.py")
    for marker in ("# ... rest unchanged ...", "// ... existing code ...", "# the rest of the file is the same as before", "... remaining code unchanged"):
        out = sb.write_file("a.py", BIG + marker + "\n")
        assert out["status"] == "refused" and "left as it was" in out["message"], marker
    assert sb.write_file("a.py", BIG + "# ... rest unchanged ...\n", overwrite=True)["status"] == "success"


def test_a_file_that_already_has_such_text_may_keep_it(sb):
    notes = BIG + "# ... rest unchanged ...\n"
    put(sb, "notes.md", notes)
    sb.read_file("notes.md")
    assert sb.write_file("notes.md", notes + "more\n")["status"] == "success"


def test_a_write_under_half_the_old_length_is_refused_unless_overwrite(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt")
    out = sb.write_file("a.txt", "short\n")
    assert out["status"] == "refused" and "under half" in out["message"]
    assert sb.write_file("a.txt", "short\n", overwrite=True)["status"] == "success"


def test_a_small_file_may_shrink_and_an_empty_one_is_replaced_freely(sb):
    put(sb, "tiny.txt", "x" * 100)
    sb.read_file("tiny.txt")
    assert sb.write_file("tiny.txt", "y")["status"] == "success"
    put(sb, "empty.txt", "")
    assert sb.write_file("empty.txt", "now text\n")["status"] == "success"


def test_write_file_keeps_the_content_exactly_including_crlf(sb):
    sb.write_file("crlf.txt", "a\r\nb\r\n")
    assert (sb.root / "crlf.txt").read_bytes() == b"a\r\nb\r\n"


# -- repeated reads ---------------------------------------------------------------------------------------------


def test_the_same_read_again_answers_unchanged_and_the_third_is_refused(sb):
    put(sb, "a.txt", BIG)
    assert sb.read_file("a.txt") == BIG
    second = sb.read_file("a.txt")
    assert second["status"] == "unchanged" and "harness_refusal" not in second
    third = sb.read_file("a.txt")
    assert third["status"] == "refused" and third["harness_refusal"] is True


def test_a_different_range_a_change_or_a_write_starts_the_count_again(sb):
    put(sb, "a.txt", BIG)
    sb.read_file("a.txt")
    assert isinstance(sb.read_file("a.txt", offset=5, limit=3), str)               # another range: a fresh answer
    assert isinstance(sb.read_file("a.txt"), str)                                  # and the first is not "unchanged" after another read in between
    put(sb, "a.txt", BIG + "x\n")
    assert isinstance(sb.read_file("a.txt"), str)                                  # the file changed
    sb.read_file("a.txt")
    sb.edit_file("a.txt", "line 001", "line one")
    assert isinstance(sb.read_file("a.txt"), str)                                  # an edit changed it


def test_a_refused_third_read_is_not_a_tool_failure_in_the_agent_loop(tmp_path):
    registry, sandbox = create_standard_tool_registry(tmp_path / "ws")
    put(sandbox, "a.txt", BIG)
    same, other = {"path": "a.txt"}, {"path": "."}
    steps = [call("read_file", same, "c1"), call("list_directory", other, "c2"), call("read_file", same, "c3"),
             call("list_directory", other, "c4"), call("read_file", same, "c5"), say("done")]
    agent = AgentLoop(client=ScriptedClient(*steps), tool_registry=registry, max_turns=10)
    turns = agent.run("read it")
    reads = [c for t in turns for c in t.tool_calls if c.tool_name == "read_file"]
    assert agent.stop_reason == FINAL and len(reads) == 3
    assert [c.is_error for c in reads] == [False, False, False]
    assert reads[1].output["status"] == "unchanged" and reads[2].output["harness_refusal"] is True
    assert agent.loop_detector.error_repeats == 0


# -- failed edits on one file -----------------------------------------------------------------------------------


def miss(sb, name="m.py", target="no such text anywhere"):
    with pytest.raises(EditError) as err:
        sb.edit_file(name, target, "x")
    return str(err.value)


def test_the_second_failed_edit_on_a_file_carries_an_excerpt_and_the_third_says_to_reread(sb):
    put(sb, "m.py", "".join(f"row {i}\n" for i in range(1, 31)))
    first = miss(sb)
    assert "failed edit" not in first
    second = miss(sb)
    assert "failed edit 2 on m.py" in second and "    1 | row 1" in second and "re-read" not in second.lower()
    third = miss(sb)
    assert "failed edit 3" in third and "read the file again" in third and "write_file" in third


def test_the_excerpt_is_taken_around_the_closest_text_when_there_is_one(sb):
    put(sb, "m.py", "".join(f"row {i}\n" for i in range(1, 80)) + "target_function(a, b)\n" + "".join(f"after {i}\n" for i in range(1, 40)))
    miss(sb, target="target_function(a, c)")
    second = miss(sb, target="target_function(a, c)")
    assert "target_function(a, b)" in second and "row 1\n" not in second.split("The file now:")[1]


def test_a_successful_edit_resets_the_failure_count(sb):
    put(sb, "m.py", "a = 1\nb = 2\n")
    miss(sb)
    sb.edit_file("m.py", "a = 1", "a = 5")
    assert "failed edit" not in miss(sb)


def test_failures_are_counted_per_file(sb):
    put(sb, "m.py", "a = 1\n")
    put(sb, "n.py", "b = 1\n")
    miss(sb, "m.py")
    assert "failed edit" not in miss(sb, "n.py")


# -- syntax check ------------------------------------------------------------------------------------------------


def test_the_default_guard_warns_and_still_writes(sb):
    assert sb.edit_guard == "warn"
    put(sb, "m.py", "def f():\n    return 1\n")
    sb.read_file("m.py")
    result = sb.edit_file("m.py", "return 1", "return (1")
    assert result["status"] == "success" and "line 2" in result["syntax_error"] and "column" in result["syntax_error"]
    assert "no longer parses" in result["warning"]
    assert "return (1" in (sb.root / "m.py").read_text(encoding="utf-8")


def test_block_writes_nothing_and_says_so(sb):
    sb.edit_guard = "block"
    put(sb, "m.py", "def f():\n    return 1\n")
    with pytest.raises(ValueError, match="Nothing was written"):
        sb.edit_file("m.py", "return 1", "return (1")
    assert (sb.root / "m.py").read_text(encoding="utf-8") == "def f():\n    return 1\n" and sb.changes == []
    with pytest.raises(ValueError, match="Nothing was written"):
        sb.write_file("new.json", "{bad")
    assert not (sb.root / "new.json").exists()


def test_off_skips_the_check(sb):
    sb.edit_guard = "off"
    put(sb, "m.py", "x = 1\n")
    assert "syntax_error" not in sb.edit_file("m.py", "x = 1", "x = (")


def test_a_file_that_did_not_parse_before_is_not_blamed(sb):
    put(sb, "m.py", "def f(:\n    pass\n")
    assert "syntax_error" not in sb.edit_file("m.py", "pass", "return")


def test_a_new_broken_file_is_reported(sb):
    result = sb.write_file("data.json", '{"a": }')
    assert "syntax_error" in result and "line 1" in result["syntax_error"]


@pytest.mark.parametrize("name,good,bad", [
    ("a.py", "x = 1\n", "x = = 1\n"),
    ("a.json", '{"a": 1}', '{"a": 1,}'),
    ("a.yaml", "a:\n  - 1\n", "a: [1\n"),
])
def test_python_json_and_yaml_are_checked_in_process(sb, name, good, bad):
    if name.endswith("yaml"):
        pytest.importorskip("yaml")
    assert "syntax_error" not in sb.write_file(name, good)
    assert "syntax_error" in sb.write_file(name, bad)


def test_toml_is_checked_when_a_parser_exists(sb):
    try:
        import tomllib  # noqa: F401
    except ImportError:
        pytest.importorskip("tomli")
    sb.write_file("c.toml", 'a = "x"\n')
    sb.read_file("c.toml")
    assert "syntax_error" in sb.write_file("c.toml", 'a = "x\n')


def test_yaml_custom_tags_are_not_syntax_errors(sb):
    pytest.importorskip("yaml")
    assert "syntax_error" not in sb.write_file("t.yaml", "a: !Ref Thing\nb: !Sub x\n")


def test_javascript_is_checked_only_when_node_is_found(sb, monkeypatch):
    from tadamoo import syntax_check
    monkeypatch.setattr(syntax_check.shutil, "which", lambda name: None)
    assert syntax_check.check("a.js", "function (") is None                         # no node: not checked, not an error
    monkeypatch.undo()
    if syntax_check.shutil.which("node"):
        assert syntax_check.check("a.js", "function (") is not None
        assert syntax_check.check("a.js", "function f() { return 1 }\n") is None


def test_other_file_types_are_not_checked(sb):
    assert "syntax_error" not in sb.write_file("notes.txt", "def (((\n")


# -- a file held by an editor ----------------------------------------------------------------------------------


def test_the_guards_follow_the_editors_text_and_ask_it_once_per_call(sb):
    put(sb, "a.txt", "on disk\n" * 30)
    buffers = {sb.resolve_safe("a.txt"): "unsaved\n" * 30}
    asked = []
    sb.read_through = lambda p: asked.append(p) or buffers.get(p)
    sb.write_through = lambda p, text: buffers.__setitem__(p, text)
    assert sb.write_file("a.txt", "x" * 300)["status"] == "refused"
    assert len(asked) == 1
    asked.clear()
    assert sb.read_file("a.txt").startswith("unsaved") and len(asked) == 1
    asked.clear()
    assert sb.write_file("a.txt", "unsaved\n" * 31)["status"] == "success" and len(asked) == 1
    buffers[sb.resolve_safe("a.txt")] = "typed by the person\n" * 31
    assert sb.write_file("a.txt", "again\n" * 40)["status"] == "refused"
    assert (sb.root / "a.txt").read_text(encoding="utf-8") == "on disk\n" * 30                 # the file on disk was never touched
