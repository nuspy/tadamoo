"""search_code, tree, the .gitignore matcher, and read_file's numbered and block modes."""
import json
import re
from pathlib import Path

import pytest

from tadamoo.agent_loop import AgentLoop, FINAL
from tadamoo.gitignore import GitIgnore, glob_regex, parse_rules
from tadamoo.permissions import PermissionManager, PermissionLevel
from tadamoo.tools import EffectClass, create_standard_tool_registry
from tadamoo.tools.filesystem import WorkspaceSandbox
from tadamoo.tools.search_code import MAX_HITS
from tests.test_agent_loop import ScriptedClient, call, say


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def tools(ws):
    registry, sandbox = create_standard_tool_registry(ws)
    return registry, sandbox


def make(ws, files):
    for name, text in files.items():
        path = ws / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)


# -- the .gitignore matcher ----------------------------------------------------------------------------------------


def ignored(ws, rules, *paths):
    (ws / ".gitignore").write_text(rules, encoding="utf-8")
    g = GitIgnore(ws)
    return [g.ignored(ws / p, is_dir=p.endswith("/")) for p in paths]


def test_gitignore_name_extension_and_folder_rules(ws):
    rules = "*.log\nbuild/\n/top.txt\ndocs/*.tmp\n# comment\n\n"
    got = ignored(ws, rules, "a.log", "x/y/b.log", "build/", "src/build/", "top.txt", "sub/top.txt", "docs/a.tmp", "docs/sub/a.tmp", "keep.py")
    assert got == [True, True, True, True, True, False, True, False, False]


def test_gitignore_a_folder_rule_does_not_match_a_file_of_that_name(ws):
    assert ignored(ws, "build/\n", "build", "build/") == [False, True]


def test_gitignore_negation_and_the_last_rule_wins(ws):
    assert ignored(ws, "*.log\n!keep.log\n", "a.log", "keep.log", "dir/keep.log") == [True, False, False]


def test_gitignore_double_star_question_mark_and_classes(ws):
    got = ignored(ws, "**/gen/*.c\nout/**\nfile?.txt\n[ab].md\n", "gen/x.c", "p/q/gen/x.c", "gen/sub/x.c", "out/a/b", "file1.txt", "file12.txt", "a.md", "c.md")
    assert got == [True, True, False, True, True, False, True, False]


def test_a_nested_gitignore_applies_below_its_folder_and_can_override(ws):
    (ws / "sub").mkdir()
    (ws / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (ws / "sub" / ".gitignore").write_text("!special.tmp\nlocal.txt\n", encoding="utf-8")
    g = GitIgnore(ws)
    assert g.ignored(ws / "a.tmp", False) and g.ignored(ws / "sub" / "b.tmp", False)
    assert not g.ignored(ws / "sub" / "special.tmp", False)
    assert g.ignored(ws / "sub" / "local.txt", False) and not g.ignored(ws / "local.txt", False)


def test_gitignore_escapes_trailing_spaces_and_bad_patterns_do_not_break_it():
    assert parse_rules("\\#hash\n\\!bang\nspace \n[unclosed\n") != []
    assert re.search(glob_regex("a b"), "a b")


# -- search_code ---------------------------------------------------------------------------------------------------


def test_search_finds_lines_with_path_line_and_text(tools, ws):
    make(ws, {"src/a.py": "x = 1\nneedle = 2\n", "src/b.py": "needle()\n", "c.txt": "nothing\n"})
    out = tools[0].execute("search_code", {"pattern": r"needle"})
    assert "src/a.py:2: needle = 2" in out and "src/b.py:1: needle()" in out and "c.txt" not in out
    assert "2 hits in 2 files" in out


def test_search_context_marks_context_lines_and_separates_blocks(tools, ws):
    make(ws, {"a.txt": "\n".join(f"l{i}" for i in range(1, 21)) + "\n"})
    out = tools[0].execute("search_code", {"pattern": r"^l(3|15)$", "context": 1})
    lines = out.splitlines()
    assert "a.txt-2- l2" in lines and "a.txt:3: l3" in lines and "a.txt-4- l4" in lines and "--" in lines
    assert "a.txt:15: l15" in lines and "a.txt-16- l16" in lines


def test_search_respects_gitignore_binary_and_big_files(tools, ws):
    make(ws, {".gitignore": "ignored/\n*.gen\n", "ignored/a.txt": "needle\n", "b.gen": "needle\n", "bin.dat": b"needle\x00\x01",
              "big.txt": "needle\n" + "x" * 1_100_000, "ok.txt": "needle\n", "node_modules/m.js": "needle\n", ".git/config": "needle\n"})
    out = tools[0].execute("search_code", {"pattern": "needle"})
    assert "ok.txt:1:" in out and "1 hit in 1 file" in out
    for hidden in ("ignored/", "b.gen", "bin.dat", "big.txt", "node_modules", ".git"):
        assert hidden not in out.split("[")[0]
    assert "2 binary or large files skipped" in out


def test_search_is_capped_at_sixty_hits_with_a_note(tools, ws):
    make(ws, {f"f{i:02d}.txt": "hit\nhit\nhit\n" for i in range(30)})
    out = tools[0].execute("search_code", {"pattern": "hit"})
    assert len([ln for ln in out.splitlines() if ln.count(":") >= 2 and not ln.startswith("[")]) == MAX_HITS
    assert "stopped at 60 hits" in out and "refine the search" in out
    few = tools[0].execute("search_code", {"pattern": "hit", "max_results": 5})
    assert "stopped at 5 hits" in few


def test_search_exactly_at_the_cap_is_not_called_capped(tools, ws):
    make(ws, {"a.txt": "hit\n" * 60})
    out = tools[0].execute("search_code", {"pattern": "hit"})
    assert "60 hits" in out and "refine" not in out


def test_search_path_glob_and_case(tools, ws):
    make(ws, {"src/a.py": "Needle\n", "src/b.js": "needle\n", "docs/c.py": "needle\n"})
    reg = tools[0]
    assert "src/b.js" not in reg.execute("search_code", {"pattern": "(?i)needle", "glob": "*.py"})
    out = reg.execute("search_code", {"pattern": "(?i)needle", "path": "src"})
    assert "src/a.py" in out and "src/b.js" in out and "docs" not in out
    assert "No match" in reg.execute("search_code", {"pattern": "NEEDLE"})
    assert "docs/c.py" in reg.execute("search_code", {"pattern": "needle", "glob": "docs/**/*.py,src/*.js"})
    assert "src/a.py:1" in reg.execute("search_code", {"pattern": "Needle", "path": "src/a.py"})
    with pytest.raises(FileNotFoundError):
        reg.execute("search_code", {"pattern": "x", "path": "nowhere"})


def test_search_bad_patterns_and_paths_give_clear_errors(tools, ws):
    with pytest.raises(ValueError, match="not a valid regular expression"):
        tools[0].execute("search_code", {"pattern": "(unclosed"})
    with pytest.raises(ValueError, match="Give the pattern"):
        tools[0].execute("search_code", {"pattern": ""})
    with pytest.raises(PermissionError):
        tools[0].execute("search_code", {"pattern": "x", "path": ".."})


def test_search_handles_crlf_unicode_and_long_lines(tools, ws):
    make(ws, {"w.txt": "caf\u00e9 first\r\nsecond needle\r\n", "long.txt": "needle " + "y" * 1000 + "\n"})
    out = tools[0].execute("search_code", {"pattern": r"needle$|^caf\u00e9"})
    assert "w.txt:1: caf\u00e9 first" in out and "w.txt:2: second needle" in out and "\r" not in out
    long = tools[0].execute("search_code", {"pattern": "needle y"})
    assert len(max(long.splitlines(), key=len)) < 300


def test_search_does_not_follow_a_link_out_of_the_workspace(tools, ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n", encoding="utf-8")
    make(ws, {"in.txt": "needle\n"})
    try:
        (ws / "link").symlink_to(outside, target_is_directory=True)
        (ws / "flink.txt").symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    out = tools[0].execute("search_code", {"pattern": "needle"})
    assert "in.txt" in out and "secret" not in out and "link" not in out


def test_search_skips_the_persons_protected_files(ws):
    registry, sandbox = create_standard_tool_registry(ws)
    make(ws, {"vault.json": "needle\n", "plain.txt": "needle\n"})
    sandbox.protected_hit = lambda p: str(p) if p.name == "vault.json" else ""
    out = registry.execute("search_code", {"pattern": "needle"})
    assert "plain.txt" in out and "vault.json" not in out


# -- tree ----------------------------------------------------------------------------------------------------------


def test_tree_shows_line_counts_per_file_and_folder(tools, ws):
    make(ws, {"a.py": "1\n2\n3\n", "pkg/b.py": "x\ny\n", "pkg/sub/c.py": "z\n", "pkg/sub/deep/d.py": "q\n", "empty.txt": "", "nonl.txt": "abc"})
    out = tools[0].execute("tree", {})
    lines = out.splitlines()
    assert lines[0] == "./ - 6 files, 8 lines"
    assert "  pkg/ - 3 files, 4 lines" in lines and "    sub/ - 2 files, 2 lines" in lines
    assert "  a.py - 3 lines" in lines and "    b.py - 2 lines" in lines and "  empty.txt - 0 lines" in lines and "  nonl.txt - 1 lines" in lines
    assert not any("deep" in ln or "d.py" in ln for ln in lines)                      # depth 2: grandchildren, not great-grandchildren
    deeper = tools[0].execute("tree", {"depth": 4})
    assert "deep/ - 1 file, 1 lines" in deeper and "d.py - 1 lines" in deeper
    one = tools[0].execute("tree", {"depth": 1})
    assert "pkg/ - 3 files, 4 lines" in one and "b.py" not in one


def test_tree_is_gitignore_aware_and_marks_binary_files(tools, ws):
    make(ws, {".gitignore": "gen/\n*.o\n", "gen/g.py": "x\n", "a.o": "x\n", "keep.py": "k\n", "img.bin": b"\x00\x01", "node_modules/x.js": "x\n"})
    out = tools[0].execute("tree", {})
    assert "keep.py - 1 lines" in out and "img.bin - binary or large" in out
    assert "gen" not in out and "a.o" not in out and "node_modules" not in out
    assert out.splitlines()[0].startswith("./ - 3 files")                                # .gitignore, keep.py, img.bin


def test_tree_of_a_subfolder_and_errors(tools, ws):
    make(ws, {"pkg/a.py": "1\n", "top.py": "t\n"})
    out = tools[0].execute("tree", {"path": "pkg"})
    assert out.splitlines()[0] == "pkg/ - 1 file, 1 lines" and "top.py" not in out
    with pytest.raises(NotADirectoryError):
        tools[0].execute("tree", {"path": "top.py"})
    with pytest.raises(PermissionError):
        tools[0].execute("tree", {"path": ".."})


def test_tree_cuts_a_very_long_listing_and_says_how_to_narrow_it(tools, ws):
    make(ws, {f"f{i:03d}.txt": "x\n" for i in range(450)})
    out = tools[0].execute("tree", {})
    assert out.count("\n") <= 402 and "more entries not shown" in out


# -- read_file: numbers and blocks ---------------------------------------------------------------------------------


CODE = ("import os\n\n\nclass A:\n    '''doc'''\n\n    def one(self):\n        x = 1\n        return x\n\n    # two\n    @staticmethod\n"
        "    def two():\n        if True:\n            y = 2\n        return y\n\n\ndef top():\n    return 3\n")


def test_numbered_prefixes_each_line_with_its_number(tools, ws):
    make(ws, {"a.txt": "alpha\nbeta\ngamma\n"})
    out = tools[0].execute("read_file", {"path": "a.txt", "numbered": True})
    assert out == "     1\talpha\n     2\tbeta\n     3\tgamma\n"
    part = tools[0].execute("read_file", {"path": "a.txt", "numbered": True, "offset": 2, "limit": 1})
    assert part.startswith("     2\tbeta\n") and "lines 2-2 of 3" in part


def test_anchor_line_returns_the_enclosing_block_by_indentation(tools, ws):
    make(ws, {"a.py": CODE})
    out = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 8})            # x = 1 inside A.one
    body = out.split("\n[")[0]
    assert body.splitlines()[0] == "    def one(self):" and body.splitlines()[-1] == "        return x"
    assert "block around line 8: lines 7-9 of 20" in out


def test_anchor_on_a_header_returns_that_block_and_max_levels_goes_up(tools, ws):
    make(ws, {"a.py": CODE})
    on_def = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 13})         # def two(): with its decorator and comment
    assert on_def.startswith("    # two\n    @staticmethod\n    def two():") and "return y" in on_def and "def one" not in on_def
    up = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 15, "max_levels": 3})
    assert up.startswith("class A:") and "def one" in up and "def two" in up and "def top" not in up
    flat = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 15, "max_levels": 0})
    assert flat.split("\n[")[0] == "            y = 2"


def test_anchor_with_siblings_adds_the_blocks_beside_it(tools, ws):
    make(ws, {"a.py": CODE})
    alone = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 8})
    with_siblings = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 8, "include_siblings": True})
    assert "def two" not in alone and "def one" in with_siblings and "def two" in with_siblings and "def top" not in with_siblings


def test_anchor_numbered_blank_anchor_and_slice_limit(tools, ws):
    make(ws, {"a.py": CODE, "big.py": "def f():\n" + "".join(f"    v{i} = {i}\n" for i in range(3000))})
    numbered = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 8, "numbered": True})
    assert numbered.startswith("     7\t    def one(self):")
    blank = tools[0].execute("read_file", {"path": "a.py", "anchor_line": 10})            # a blank line stands for the next code line
    assert "# two" in blank
    big = tools[1].read_file("big.py", anchor_line=5, max_chars=500_000)
    assert big.count("\n    v") == 1999
    assert "only the first 2000 lines are shown" in big
    small = tools[0].execute("read_file", {"path": "big.py", "anchor_line": 5, "limit": 10})
    assert "only the first 10 lines are shown" in small


def test_anchor_past_the_end_binary_crlf_and_tabs(tools, ws):
    make(ws, {"c.py": "def f():\r\n    return 1\r\n", "t.py": "def f():\n\tif x:\n\t\ty = 1\n\treturn 2\n", "b.bin": b"\x00\x00"})
    assert "past the end" in tools[0].execute("read_file", {"path": "c.py", "anchor_line": 99})
    crlf = tools[0].execute("read_file", {"path": "c.py", "anchor_line": 2})
    assert "\r" not in crlf and crlf.startswith("def f():\n    return 1")
    tabs = tools[0].execute("read_file", {"path": "t.py", "anchor_line": 3})
    assert tabs.startswith("\tif x:\n\t\ty = 1")
    with pytest.raises(ValueError, match="binary"):
        tools[0].execute("read_file", {"path": "b.bin", "anchor_line": 1})


def test_c_style_blocks_include_the_closing_bracket(tools, ws):
    make(ws, {"a.c": "int f() {\n    if (x) {\n        y();\n    }\n    return 1;\n}\n\nint g() {\n    return 2;\n}\n"})
    out = tools[0].execute("read_file", {"path": "a.c", "anchor_line": 3, "max_levels": 1})
    assert out.split("\n[")[0] == "    if (x) {\n        y();\n    }"
    whole = tools[0].execute("read_file", {"path": "a.c", "anchor_line": 3, "max_levels": 2})
    assert whole.split("\n[")[0].endswith("}") and "int g" not in whole


# -- registration, permissions, descriptions ---------------------------------------------------------------------------


def test_both_tools_are_registered_as_pure_with_short_descriptions(tools):
    registry = tools[0]
    for name in ("search_code", "tree"):
        tool = registry.get(name)
        assert tool.effect == EffectClass.PURE and not tool.untrusted and not tool.outward
        assert len(tool.description) < 400
    others = set(registry.names()) - {"search_code", "tree"}
    for name in ("search_code", "tree", "read_file", "write_file", "edit_file"):
        text = registry.get(name).description
        for other in others - {name}:
            if other in ("tree",):
                continue
            assert not re.search(rf"\b{other}\b", text) or other in ("read_file", "write_file", "edit_file"), (name, other)


def test_descriptions_do_not_name_tools_that_may_be_missing(tools):
    for name in ("search_code", "tree", "read_file", "write_file", "edit_file"):
        text = tools[0].get(name).description
        for optional in ("repo_map", "repo_diagram", "search_web", "browse_url", "diagnostics", "shell_command", "run_code", "spawn_subagent", "todo_write"):
            assert optional not in text


def test_pure_tools_run_in_plan_mode_and_writes_do_not(ws, tmp_path):
    registry, _ = create_standard_tool_registry(ws)
    permissions = PermissionManager(PermissionLevel.DEFAULT, confirm_callback=lambda tool, args: True)
    permissions.plan_mode = True
    for name in ("search_code", "tree", "read_file"):
        assert permissions.evaluate(name, registry.get(name).effect, {})[0] is True
    for name in ("write_file", "edit_file"):
        assert permissions.evaluate(name, registry.get(name).effect, {})[0] is False


def test_the_schemas_validate_the_new_arguments(tools):
    registry = tools[0]
    registry.validate("edit_file", {"path": "a", "edits": [{"target": "x", "replacement": "y"}], "replace_all": True})
    registry.validate("edit_file", {"path": "a", "target": "x", "replacement": "y", "expected_count": 2})
    registry.validate("read_file", {"path": "a", "numbered": True, "anchor_line": 3, "max_levels": 2, "include_siblings": True})
    registry.validate("write_file", {"path": "a", "content": "", "overwrite": True})
    registry.validate("search_code", {"pattern": "x", "path": ".", "glob": "*.py", "context": 2, "max_results": 5})
    registry.validate("tree", {"path": ".", "depth": 3})


def test_search_and_tree_run_through_the_agent_loop(ws):
    registry, _ = create_standard_tool_registry(ws)
    make(ws, {"a.py": "needle = 1\n"})
    client = ScriptedClient(call("search_code", {"pattern": "needle"}, "c1"), call("tree", {}, "c2"), say("done"))
    agent = AgentLoop(client=client, tool_registry=registry)
    agent.run("find it")
    tool_messages = [m["content"] for m in client.sent[-1] if m["role"] == "tool"]
    assert "a.py:1: needle = 1" in tool_messages[0] and "a.py - 1 lines" in tool_messages[1]
    assert agent.stop_reason == FINAL
