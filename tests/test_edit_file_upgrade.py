"""edit_file: several edits at once, a tolerant match only when it is unique, and errors that say where the text really is."""
import pytest

from harness import editing
from harness.editing import EditError, apply_edits, restore_endings, to_lf
from harness.tools.filesystem import WorkspaceSandbox


@pytest.fixture
def sb(tmp_path):
    return WorkspaceSandbox(tmp_path / "ws")


def edit(text, target, replacement, **extra):
    return apply_edits(text, [{"target": target, "replacement": replacement, **extra}])


def put(sb, name, text):
    """A file as the person had it: written outside the tools, with its bytes kept."""
    (sb.root / name).write_bytes(text.encode("utf-8"))


def disk(sb, name):
    return (sb.root / name).read_bytes().decode("utf-8")


# -- the exact pass and the tolerant passes -------------------------------------------------------------------


def test_an_exact_unique_target_is_replaced_and_says_nothing_more():
    out = edit("a = 1\nb = 2\n", "b = 2", "b = 3")
    assert out.text == "a = 1\nb = 3\n" and out.notes == [] and out.applied == 1


def test_trailing_whitespace_in_the_file_is_ignored_when_one_place_matches():
    out = edit("x = 1   \ny = 2\t\nz = 3\n", "x = 1\ny = 2", "x = 10\ny = 20")
    assert out.text == "x = 10\ny = 20\nz = 3\n"
    assert "lines 1-2" in out.notes[0] and "trailing whitespace" in out.notes[0]


def test_a_target_with_the_wrong_indentation_is_reindented_to_the_file():
    text = "class A:\n    def f(self):\n        if x:\n            return 1\n        return 2\n"
    out = edit(text, "if x:\n    return 1", "if x:\n    return 10\n    log()")
    assert out.text == "class A:\n    def f(self):\n        if x:\n            return 10\n            log()\n        return 2\n"
    assert "lines 3-4" in out.notes[0] and "indentation" in out.notes[0]


def test_a_space_indented_target_in_a_tab_file_gets_tabs():
    text = "def f():\n\tif x:\n\t\treturn 1\n\treturn 2\n"
    out = edit(text, "if x:\n    return 1", "if x:\n    return 10\n    log()")
    assert out.text == "def f():\n\tif x:\n\t\treturn 10\n\t\tlog()\n\treturn 2\n"


def test_a_tab_indented_target_in_a_space_file_gets_spaces():
    text = "def f():\n    if x:\n        return 1\n"
    out = edit(text, "\tif x:\n\t\treturn 1", "\tif x:\n\t\treturn 10")
    assert out.text == "def f():\n    if x:\n        return 10\n"


def test_a_two_space_target_is_scaled_to_a_four_space_file():
    text = "def f():\n    if x:\n        if y:\n            go()\n"
    out = edit(text, "if x:\n  if y:\n    go()", "if x:\n  if y:\n    go()\n    stop()")
    assert out.text == "def f():\n    if x:\n        if y:\n            go()\n            stop()\n"


def test_a_mixed_indent_file_keeps_the_indent_of_the_lines_the_replacement_does_not_touch():
    text = "def f():\n\tif x:\n        return 1\n\treturn 2\n"          # a tab line, then a space line
    out = edit(text, "return 2", "return 3")
    assert out.text == "def f():\n\tif x:\n        return 1\n\treturn 3\n"


def test_unicode_quotes_dashes_and_odd_spaces_are_matched_to_plain_text():
    text = "print(\u201chello\u201d)\nx = a \u2013 b\ny = c\u00a0+ d\n"
    out = edit(text, 'print("hello")', 'print("bye")')
    assert out.text.startswith('print("bye")') and "Unicode" in out.notes[0]
    assert edit(text, "x = a - b", "x = a + b").text.splitlines()[1] == "x = a + b"
    assert edit(text, "y = c + d", "y = c").text.splitlines()[2] == "y = c"
    assert edit("wait\u2026\n", "wait...", "done").text == "done\n"           # NFKC: one character becomes three


def test_a_unicode_match_never_starts_inside_one_characters_expansion():
    with pytest.raises(EditError, match="not found"):
        edit("wait\u2026\n", "wait..", "x")


def test_python_yaml_and_makefile_text_keep_their_own_indentation_rules():
    yaml = "services:\n  web:\n    image: a\n    ports:\n      - 80\n"
    assert edit(yaml, "ports:\n  - 80", "ports:\n  - 81").text.endswith("    ports:\n      - 81\n")
    make = "all:\n\tgcc a.c\n\tgcc b.c\n"
    assert edit(make, "gcc a.c", "gcc -O2 a.c").text == "all:\n\tgcc -O2 a.c\n\tgcc b.c\n"
    assert edit(make, "  gcc b.c", "  gcc -O2 b.c").text == "all:\n\tgcc a.c\n\tgcc -O2 b.c\n"      # spaces sent, a tab in the Makefile


def test_the_strictest_pass_that_finds_anything_decides():
    text = "x = 1  \n    x = 1\n"
    with pytest.raises(EditError, match="appears 2 times"):
        edit(text, "x = 1", "x = 2")                          # exact: the substring is on both lines
    out = edit("a\n  b  \n", "  b", "  c")
    assert out.text == "a\n  c  \n"                           # exact wins, the trailing spaces are left


# -- ambiguity, misses and nonsense targets --------------------------------------------------------------------


def test_several_matches_return_their_line_numbers_and_apply_nothing():
    text = "x = 1\ny\nx = 1\nz\n"
    with pytest.raises(EditError) as err:
        edit(text, "x = 1", "x = 2")
    assert "appears 2 times" in str(err.value) and "lines 1, 3" in str(err.value) and "replace_all" in str(err.value)


def test_a_tolerant_pass_with_several_candidates_is_ambiguous_not_applied():
    text = "  a\nb\n    a\nb\n"
    with pytest.raises(EditError, match="ignoring indentation"):
        edit(text, "a\nb ", "x")


def test_replace_all_and_expected_count_work_on_exact_text_only():
    text = "x = 1\ny\nx = 1\n"
    out = edit(text, "x = 1", "x = 2", replace_all=True)
    assert out.text == "x = 2\ny\nx = 2\n" and "2 places" in out.notes[0]
    assert edit(text, "x = 1", "x = 2", expected_count=2).text == "x = 2\ny\nx = 2\n"
    with pytest.raises(EditError, match="not the 3 expected"):
        edit(text, "x = 1", "x = 2", expected_count=3)
    with pytest.raises(EditError, match="exact text only"):
        edit("  x = 1\n", "x =  1", "y", replace_all=True)
    with pytest.raises(EditError, match="whole number"):
        edit(text, "x = 1", "x = 2", expected_count=0)


def test_a_miss_returns_the_closest_text_with_line_numbers_and_a_visible_whitespace_diff():
    text = "".join(f"filler {i}\n" for i in range(1, 25)) + "def add(a, b):\n\treturn a + b\nend\n"
    with pytest.raises(EditError) as err:
        edit(text, "def add(a, b):\n    return a - b", "x")
    message = str(err.value)
    assert "lines 25-26" in message and "% similar" in message
    assert "  25 | def add(a, b):" in message and "  26 | \treturn a + b" in message       # the real lines, numbered
    assert "-····return·a·-·b" in message and "+→return·a·+·b" in message                  # sent against file, whitespace visible
    assert "→ is a tab" in message
    assert "Next:" in message and " 1. " in message and " 2. " in message                   # numbered recovery steps
    assert err.value.closest == 25


def test_a_miss_with_nothing_close_says_so_and_still_gives_steps():
    with pytest.raises(EditError) as err:
        edit("alpha\nbeta\n", "zzzzzzzzzzzzzzzzzzzzz qqqqqqqqqqqq", "x")
    assert "No text in the file is close" in str(err.value) and "Next:" in str(err.value) and err.value.closest == 0


@pytest.mark.parametrize("target,replacement,words", [
    ("", "x", "must not be empty"),
    ("   \n\t", "x", "only whitespace"),
    ("same", "same", "identical"),
])
def test_empty_whitespace_and_identical_targets_get_their_own_message(target, replacement, words):
    with pytest.raises(EditError) as err:
        edit("same\n", target, replacement)
    assert words in str(err.value) and "Do not re-send this call unchanged" in str(err.value)


def test_a_resent_edit_already_applied_is_a_success_shaped_no_op():
    out = edit("a = 2\n", "a = 1", "a = 2")
    assert out.applied == 0 and not out.changed and out.text == "a = 2\n"
    assert "already applied" in out.notes[0] and "line 1" in out.notes[0]


# -- several edits, atomically ---------------------------------------------------------------------------------


def test_edits_apply_in_order_each_on_the_result_of_the_one_before():
    out = apply_edits("one\n", [{"target": "one", "replacement": "two"}, {"target": "two", "replacement": "three"}])
    assert out.text == "three\n" and out.applied == 2


def test_one_failing_edit_applies_none_and_names_which_failed():
    with pytest.raises(EditError, match="Edit 2 of 3"):
        apply_edits("a\nb\nc\n", [{"target": "a", "replacement": "A"}, {"target": "nope nope", "replacement": "x"}, {"target": "c", "replacement": "C"}])


def test_a_bad_edit_object_is_reported_not_crashed_on():
    with pytest.raises(EditError, match="no field"):
        apply_edits("a\n", [{"target": "a", "replacement": "b", "regex": True}])
    with pytest.raises(EditError, match="as text"):
        apply_edits("a\n", [{"target": "a"}])
    with pytest.raises(EditError, match="must be an object"):
        apply_edits("a\n", ["a"])
    with pytest.raises(EditError, match="no edit"):
        apply_edits("a\n", [])


# -- line endings ----------------------------------------------------------------------------------------------


def test_crlf_files_stay_crlf_and_lf_files_stay_lf(sb):
    put(sb, "crlf.txt", "a\r\nb\r\nc\r\n")
    sb.edit_file("crlf.txt", "b\nc", "B\nC")
    assert disk(sb, "crlf.txt") == "a\r\nB\r\nC\r\n"
    put(sb, "lf.txt", "a\nb\n")
    sb.edit_file("lf.txt", "b", "B")
    assert disk(sb, "lf.txt") == "a\nB\n"


def test_a_target_sent_with_crlf_matches_an_lf_file(sb):
    put(sb, "lf.txt", "a\nb\nc\n")
    sb.edit_file("lf.txt", "a\r\nb", "x\r\ny")
    assert disk(sb, "lf.txt") == "x\ny\nc\n"


def test_a_mixed_file_keeps_each_untouched_lines_own_ending():
    original = "a\r\nb\nc\r\nd\n"
    old = to_lf(original)
    assert restore_endings(original, old, old.replace("c", "C")) == "a\r\nb\nC\r\nd\n"
    grown = restore_endings(original, old, "a\nb\nc\nnew\nd\n")
    assert grown.startswith("a\r\nb\nc\r\n") and grown.endswith("d\n")


def test_the_tolerant_pass_works_on_crlf_text_too(sb):
    put(sb, "w.txt", "def f():\r\n    return 1\r\n")
    result = sb.edit_file("w.txt", "def f():\n  return 1", "def f():\n  return 2")
    assert disk(sb, "w.txt") == "def f():\r\n    return 2\r\n" and result["notes"]


# -- the tool ---------------------------------------------------------------------------------------------------


def test_several_edits_make_one_change_entry_and_one_write(sb):
    put(sb, "m.py", "a = 1\nb = 2\nc = 3\n")
    result = sb.edit_file("m.py", edits=[{"target": "a = 1", "replacement": "a = 10"}, {"target": "c = 3", "replacement": "c = 30"}])
    assert disk(sb, "m.py") == "a = 10\nb = 2\nc = 30\n"
    assert len(sb.changes) == 1 and result["edits"] == 2 and result["added"] == 2 and result["removed"] == 2


def test_a_failed_batch_changes_nothing_and_records_nothing(sb):
    put(sb, "m.py", "a = 1\nb = 2\n")
    with pytest.raises(EditError, match="Edit 2 of 2"):
        sb.edit_file("m.py", edits=[{"target": "a = 1", "replacement": "a = 9"}, {"target": "zzz yyy xxx", "replacement": "q"}])
    assert disk(sb, "m.py") == "a = 1\nb = 2\n" and sb.changes == []


def test_a_resent_edit_changes_nothing_and_adds_no_change_entry(sb):
    put(sb, "m.py", "a = 1\n")
    sb.edit_file("m.py", "a = 1", "a = 2")
    again = sb.edit_file("m.py", "a = 1", "a = 2")
    assert again["status"] == "success" and again["changed"] is False and "already applied" in again["note"]
    assert len(sb.changes) == 1


def test_target_and_edits_together_or_neither_is_a_usage_error(sb):
    put(sb, "m.py", "a = 1\n")
    with pytest.raises(EditError, match="not both"):
        sb.edit_file("m.py", "a", "b", edits=[{"target": "a", "replacement": "b"}])
    with pytest.raises(EditError, match="Give 'target'"):
        sb.edit_file("m.py")
    with pytest.raises(EditError, match="non-empty list"):
        sb.edit_file("m.py", edits=[])


def test_top_level_replace_all_is_the_default_of_every_edit(sb):
    put(sb, "m.txt", "x\ny\nx\n")
    sb.edit_file("m.txt", edits=[{"target": "x", "replacement": "X"}], replace_all=True)
    assert disk(sb, "m.txt") == "X\ny\nX\n"


def test_a_tolerant_edit_names_the_matched_lines_in_the_result(sb):
    put(sb, "m.py", "def f():\n    return 1\n")
    result = sb.edit_file("m.py", "return 1", "return 2")             # exact
    assert "notes" not in result
    result = sb.edit_file("m.py", "def f():\n  return 2", "def f():\n  return 3")
    assert "lines 1-2" in result["notes"][0]


def test_the_write_is_read_back_and_a_mismatch_is_an_error(sb, monkeypatch):
    put(sb, "m.py", "a = 1\n")
    real = WorkspaceSandbox._read_text
    monkeypatch.setattr(WorkspaceSandbox, "_read_text", staticmethod(lambda path: real(path) + "garbage"))
    with pytest.raises(OSError, match="reading it back"):
        sb.edit_file("m.py", "a = 1", "a = 2")


def test_the_old_messages_are_still_there_for_the_old_cases(sb):
    put(sb, "a.py", "x = 1\nx = 1\ny = 2\n")
    with pytest.raises(ValueError, match="appears 2 times"):
        sb.edit_file("a.py", "x = 1", "x = 2")
    with pytest.raises(ValueError, match="must not be empty"):
        sb.edit_file("a.py", "", "z")
    with pytest.raises(ValueError, match="not found"):
        sb.edit_file("a.py", "nope nope nope", "z")


def test_a_closest_window_search_on_a_big_file_is_fast_enough(sb):
    import time
    lines = [f"value_{i} = compute({i}) + other_{i}\n" for i in range(8000)]
    start = time.monotonic()
    with pytest.raises(EditError):
        edit("".join(lines), "value_4000 = compute(4001) + other_4000\nvalue_4001 = compute(4001) + other_4002", "x")
    assert time.monotonic() - start < 20
    assert editing.closest_window(["a", "b"], "")[0] == 0.0
