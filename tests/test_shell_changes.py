"""Edits made by a shell command are recorded like the file tools' edits, so the panel can review them."""
import sys
from pathlib import Path

import pytest

from harness.tools import create_standard_tool_registry
from harness.tools import filesystem

PY = f'"{sys.executable}"'


def run(registry, script: str):
    """Runs a Python script written to a file in the workspace, so quoting never gets in the way."""
    path = Path(registry.sandbox_root) / "_run.py"
    path.write_text(script, encoding="utf-8", newline="\n")
    out = registry.execute("shell_command", {"command": f"{PY} _run.py"})
    path.unlink()
    return out


@pytest.fixture
def tools(tmp_path):
    registry, sandbox = create_standard_tool_registry(tmp_path)
    registry.sandbox_root = tmp_path
    return registry, sandbox, tmp_path


def lines(n):
    return "".join(f"riga {i}\n" for i in range(1, n + 1))


def test_a_command_that_edits_a_file_is_recorded_with_its_blocks(tools):
    registry, sandbox, root = tools
    (root / "note.txt").write_text(lines(45), encoding="utf-8", newline="\n")
    out = run(registry, "t = open('note.txt', encoding='utf-8', newline='').read().split('\\n')\n"
                        "t[2] = 'RIGA TRE'; t[34] = 'RIGA TRENTACINQUE'\n"
                        "open('note.txt', 'w', encoding='utf-8', newline='').write('\\n'.join(t))\n")
    assert out["success"]
    assert [(c["file"], c["kind"], c["added"], c["removed"]) for c in sandbox.changes] == [("note.txt", "modified", 2, 2)]
    change = sandbox.changes[0]
    assert change["old_text"] == lines(45) and "RIGA TRE\n" in change["new_text"]
    assert change["diff"].count("@@ -") == 2                                      # two separate blocks


def test_a_new_file_and_a_deleted_file_are_recorded(tools):
    registry, sandbox, root = tools
    (root / "old.txt").write_text("vecchio\n", encoding="utf-8")
    run(registry, "import os\nopen('new.txt', 'w').write('uno\\ndue\\n')\nos.remove('old.txt')\n")
    got = {c["file"]: (c["kind"], c["added"], c["removed"]) for c in sandbox.changes}
    assert got == {"new.txt": ("added", 2, 0), "old.txt": ("deleted", 0, 1)}


def test_a_command_that_changes_nothing_records_nothing(tools):
    registry, sandbox, root = tools
    (root / "a.txt").write_text("x\n", encoding="utf-8")
    run(registry, "print(open('a.txt').read())")
    run(registry, "import os; os.utime('a.txt', None)")                            # only the time changes
    assert sandbox.changes == []


def test_a_file_the_tools_wrote_is_not_counted_twice(tools):
    registry, sandbox, root = tools
    registry.execute("write_file", {"path": "w.txt", "content": "uno\n"})
    assert len(sandbox.changes) == 1
    run(registry, "print('looking')")
    assert len(sandbox.changes) == 1
    run(registry, "open('w.txt', 'a', newline='').write('due\\n')")
    assert [(c["kind"], c["added"], c["removed"]) for c in sandbox.changes[1:]] == [("modified", 1, 0)]
    assert sandbox.changes[1]["old_text"] == "uno\n"                              # compared with what the tool wrote


def test_binary_large_and_ignored_files_are_left_out(tools, monkeypatch):
    registry, sandbox, root = tools
    monkeypatch.setattr(filesystem, "MAX_RECORDED_TEXT", 100)
    run(registry, "import os\nopen('b.bin', 'wb').write(b'\\x00\\x01' * 50)\nopen('big.txt', 'w').write('x' * 500)\n"
                  "os.makedirs('.git'); open('.git/HEAD', 'w').write('ref')\nos.makedirs('node_modules'); open('node_modules/m.js', 'w').write('1')\n"
                  "open('ok.txt', 'w').write('ciao')\n")
    assert [c["file"] for c in sandbox.changes] == ["ok.txt"]


def test_a_command_that_fails_after_editing_still_records_the_edit(tools):
    registry, sandbox, root = tools
    out = run(registry, "open('half.txt', 'w').write('metà')\nraise SystemExit(3)\n")
    assert not out["success"]
    assert [c["file"] for c in sandbox.changes] == ["half.txt"]


def test_a_command_that_writes_very_many_files_is_capped(tools, monkeypatch):
    registry, sandbox, root = tools
    monkeypatch.setattr(filesystem, "MAX_SHELL_CHANGES", 5)
    run(registry, "for i in range(20):\n    open(f'f{i:02}.txt', 'w').write('x')\n")
    assert len(sandbox.changes) == 5


def test_a_workspace_too_large_to_compare_is_left_alone_and_the_command_still_runs(tools, monkeypatch):
    registry, sandbox, root = tools
    monkeypatch.setattr(filesystem, "MAX_WATCHED_FILES", 3)
    for i in range(5):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    out = run(registry, "open('new.txt', 'w').write('y')")
    assert out["success"] and sandbox.watch_off and sandbox.changes == []
    assert (root / "new.txt").read_text() == "y"


def test_the_panel_receives_a_shell_edit_with_its_review_texts(tools):
    from harness.reporting import PanelReporter
    registry, sandbox, root = tools
    (root / "n.txt").write_text(lines(5), encoding="utf-8", newline="\n")
    sent = []
    reporter = PanelReporter("http://127.0.0.1:1", None, sandbox, "goal")
    reporter._post = lambda path, body: sent.append((path, body)) or True
    run(registry, "open('n.txt', 'a', newline='').write('sei\\n')")
    reporter._send_new_changes()
    edit = sent[0][1]["edits"][0]
    assert sent[0][0] == "/api/changes" and edit["file"] == "n.txt" and edit["review"]["old_text"] == lines(5)
    assert edit["review"]["new_text"] == lines(5) + "sei\n"
