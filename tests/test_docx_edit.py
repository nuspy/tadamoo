"""The Word tools: outline and read for finding what to change, atomic edits (plain and as tracked changes), creation from blocks, and how
they sit in the harness (workspace, checkpoints and rewind, roles, permissions). Fixtures are made with python-docx; none is committed."""
import base64
import io
import zipfile
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
from docx import Document  # noqa: E402

from tadamoo.agent_loop import AgentLoop  # noqa: E402
from tadamoo.checkpoints import CheckpointManager, call_paths  # noqa: E402
from tadamoo.permissions import GROUPS, PermissionLevel, PermissionManager  # noqa: E402
from tadamoo.roles import ASK, CODING_ROLE, GENERAL, ORCHESTRATE, PLAN, RolePolicy  # noqa: E402
from tadamoo.tools import create_standard_tool_registry  # noqa: E402
from tadamoo.tools.docx_runs import EditError  # noqa: E402
from tadamoo.tools.registry import EffectClass  # noqa: E402
from test_agent_loop import ScriptedClient, call, say  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def make_doc(path: Path) -> Path:
    """p1 Title-ish heading, p2 mixed runs, p3 heading, p4 plain, p5 last; table t1 (3x2) after p4."""
    doc = Document()
    doc.add_heading("Introduction", 1)
    p = doc.add_paragraph("Hello ")
    p.add_run("brave").bold = True
    p.add_run(" new ").italic = True
    p.add_run("world, hello again.")
    doc.add_heading("Data", 2)
    doc.add_paragraph("Second para with a price of 10 euro.")
    table = doc.add_table(rows=3, cols=2)
    table.style = "Table Grid"
    for r, row in enumerate([["Name", "Qty"], ["apple", "3"], ["pear", "5"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.add_paragraph("The end.")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    registry, sandbox = create_standard_tool_registry(ws)
    make_doc(ws / "a.edited.docx")
    return registry, sandbox, ws


def run(env, name, **args):
    return env[0].execute(name, args)


def edit(env, edits, **kw):
    return run(env, "docx_edit", path="a.edited.docx", edits=edits, **kw)


def paragraphs(ws, name="a.edited.docx"):
    return [p.text for p in Document(str(ws / name)).paragraphs]


def xml(ws, name="a.edited.docx"):
    with zipfile.ZipFile(ws / name) as z:
        return z.read("word/document.xml").decode("utf-8")


# -- outline and read --------------------------------------------------------------------------------------------------------


def test_the_outline_numbers_headings_tables_and_counts(env):
    registry, sandbox, ws = env
    doc = Document(str(ws / "a.edited.docx"))
    doc.add_picture(io.BytesIO(PNG))
    doc.save(str(ws / "a.edited.docx"))
    text = run(env, "docx_outline", path="a.edited.docx")
    assert "6 paragraphs, 1 tables, 1 images, 0 comments, tracked changes: none" in text
    assert 'H1 p1 "Introduction": 1 paragraphs (p2)' in text
    assert 'H2 p3 "Data"' in text and "(p4-p6)" in text
    assert "table t1 3x2, after p4: Name | Qty" in text
    assert "p2 [Normal]" not in text                                          # paragraphs are listed only on request
    assert "p2 [Normal] Hello brave new world, hello again." in run(env, "docx_outline", path="a.edited.docx", detail=True)


def test_the_outline_reports_comments_and_tracked_changes(env):
    edit(env, [{"kind": "add_comment", "target": "p2", "text": "look"}])
    edit(env, [{"kind": "replace_text", "target": "p4", "find": "10", "replace": "12"}], track_changes=True)
    text = run(env, "docx_outline", path="a.edited.docx")
    assert "1 comments" in text and "tracked changes: 1 insertions, 1 deletions" in text
    assert "tracked changes: none" not in text


def test_read_a_paragraph_a_range_a_table_and_a_cell(env):
    one = run(env, "docx_read", path="a.edited.docx", target="p2")
    assert one.startswith("p2 [Normal] Hello brave new world, hello again.")
    assert 'bold: "brave"' in one and 'italic: "new"' in one
    many = run(env, "docx_read", path="a.edited.docx", target="p1-p3").splitlines()
    assert many[0] == "p1 [Heading 1] Introduction" and many[2].startswith("p3 [Heading 2] Data")
    table = run(env, "docx_read", path="a.edited.docx", target="t1")
    assert "| Name | Qty |" in table and "|---|---|" in table and "| pear | 5 |" in table
    assert run(env, "docx_read", path="a.edited.docx", target="t1.r2.c1").startswith("t1.r2.c1 [Normal] apple")


def test_an_address_that_does_not_exist_says_what_does(env):
    with pytest.raises(EditError, match="the document has 5 paragraphs"):
        run(env, "docx_read", path="a.edited.docx", target="p9")
    with pytest.raises(EditError, match="t1 has 3 rows"):
        run(env, "docx_read", path="a.edited.docx", target="t1.r4.c1")
    with pytest.raises(EditError, match="not an address"):
        run(env, "docx_read", path="a.edited.docx", target="chapter 2")


def test_text_read_from_a_document_is_fenced_as_untrusted_and_the_writers_are_not(env):
    registry = env[0]
    assert registry.get("docx_outline").untrusted and registry.get("docx_read").untrusted
    assert not registry.get("docx_edit").untrusted and not registry.get("docx_create").untrusted


# -- edits -------------------------------------------------------------------------------------------------------------------


def test_replace_text_across_runs_keeps_the_formatting_of_the_first_run(env):
    ws = env[2]
    result = edit(env, [{"kind": "replace_text", "target": "p2", "find": "brave new world", "replace": "BRAVE WORLD"}])
    assert "1 of 1" in result["applied"][0]
    p = Document(str(ws / "a.edited.docx")).paragraphs[1]
    assert p.text == "Hello BRAVE WORLD, hello again."
    assert p.runs[1].text == "BRAVE WORLD" and p.runs[1].bold is True and not p.runs[1].italic
    assert p.runs[0].text == "Hello " and p.runs[0].bold is None


def test_replace_text_count_and_scope(env):
    ws = env[2]
    result = edit(env, [{"kind": "replace_text", "find": "hello", "replace": "bye", "count": 1}])
    assert "1 of 1" in result["applied"][0]                                    # case matters: only "hello again" matches
    assert paragraphs(ws)[1] == "Hello brave new world, bye again."
    edit(env, [{"kind": "replace_text", "find": "e", "replace": "E", "target": "p5", "count": 0}])
    assert paragraphs(ws)[-1] == "ThE End."
    edit(env, [{"kind": "replace_text", "find": "apple", "replace": "plum", "target": "t1"}])
    assert Document(str(ws / "a.edited.docx")).tables[0].cell(1, 0).text == "plum"


def test_replace_text_not_found_hints_at_the_capitals(env):
    with pytest.raises(EditError, match="different capital letters"):
        edit(env, [{"kind": "replace_text", "target": "p2", "find": "HELLO BRAVE", "replace": "x"}])


def test_set_paragraph_with_a_style_and_set_style(env):
    ws = env[2]
    edit(env, [{"kind": "set_paragraph", "target": "p4", "text": "Line one\nLine two", "style": "Heading 3"},
               {"kind": "set_style", "target": "p5", "style": "Heading 1"}])
    doc = Document(str(ws / "a.edited.docx"))
    assert doc.paragraphs[3].style.name == "Heading 3" and doc.paragraphs[3].text == "Line one\nLine two"
    assert doc.paragraphs[4].style.name == "Heading 1"
    with pytest.raises(EditError, match="there is no paragraph style 'Nope'"):
        edit(env, [{"kind": "set_style", "target": "p2", "style": "Nope"}])


def test_insert_and_delete_paragraphs_use_the_original_numbers_and_keep_their_order(env):
    ws = env[2]
    edit(env, [{"kind": "delete_paragraph", "target": "p4"},
               {"kind": "insert_paragraph", "after": "p4", "text": "first new"},
               {"kind": "insert_paragraph", "after": "p4", "text": "second new"},
               {"kind": "insert_paragraph", "before": "p2", "text": "before two"},
               {"kind": "insert_paragraph", "after": "end", "text": "at the end"}])
    assert paragraphs(ws) == ["Introduction", "before two", "Hello brave new world, hello again.", "Data", "first new", "second new", "The end.", "at the end"]


def test_an_inserted_paragraph_takes_the_style_of_a_normal_neighbour_but_not_of_a_heading(env):
    ws = env[2]
    edit(env, [{"kind": "insert_paragraph", "after": "p1", "text": "under the heading"},
               {"kind": "insert_paragraph", "after": "p4", "text": "under the body", "style": "List Bullet"}])
    doc = Document(str(ws / "a.edited.docx"))
    assert doc.paragraphs[1].style.name == "Normal" and doc.paragraphs[1].text == "under the heading"
    assert doc.paragraphs[5].style.name == "List Bullet"


def test_table_edits(env):
    ws = env[2]
    edit(env, [{"kind": "set_cell", "target": "t1.r2.c2", "text": "30"},
               {"kind": "insert_table_row", "table": "t1", "after_row": 2, "cells": ["fig", "7"]},
               {"kind": "insert_table_row", "table": "t1", "after_row": 2, "cells": ["kiwi", "8"]},
               {"kind": "delete_table_row", "table": "t1", "row": 3}])
    rows = [[c.text for c in r.cells] for r in Document(str(ws / "a.edited.docx")).tables[0].rows]
    assert rows == [["Name", "Qty"], ["apple", "30"], ["fig", "7"], ["kiwi", "8"]]
    with pytest.raises(EditError, match="3 texts were given"):
        edit(env, [{"kind": "insert_table_row", "table": "t1", "after_row": 1, "cells": ["a", "b", "c"]}])
    with pytest.raises(EditError, match="last row"):
        edit(env, [{"kind": "delete_table_row", "table": "t1", "row": r} for r in (1, 2, 3, 4)])


def test_add_comment_on_a_paragraph_and_on_exact_words(env):
    ws = env[2]
    edit(env, [{"kind": "add_comment", "target": "p4", "text": "check the price", "author": "Rita", "find": "10 euro"},
               {"kind": "add_comment", "target": "p5", "text": "ok"}])
    doc = Document(str(ws / "a.edited.docx"))
    comments = list(doc.comments)
    assert [(c.author, c.text) for c in comments] == [("Rita", "check the price"), ("TaDaMoo", "ok")]
    assert doc.paragraphs[3].text == "Second para with a price of 10 euro."
    assert xml(ws).count("commentRangeStart") == 2


def test_an_edit_without_comments_does_not_add_a_comments_part(env):
    ws = env[2]
    edit(env, [{"kind": "replace_text", "target": "p5", "find": "end", "replace": "finish"}])
    with zipfile.ZipFile(ws / "a.edited.docx") as z:
        assert not any("comments" in n for n in z.namelist())


# -- atomic ------------------------------------------------------------------------------------------------------------------


def test_one_failing_edit_changes_nothing_and_every_failure_is_listed(env):
    ws = env[2]
    before = (ws / "a.edited.docx").read_bytes()
    with pytest.raises(EditError) as err:
        edit(env, [{"kind": "replace_text", "target": "p2", "find": "brave", "replace": "bold"},
                   {"kind": "delete_paragraph", "target": "p99"},
                   {"kind": "replace_text", "target": "p4", "find": "zzz", "replace": "y"},
                   {"kind": "teleport"}])
    message = str(err.value)
    assert "3 of 4 edits cannot be applied" in message
    assert "edit 2 (delete_paragraph p99)" in message and "edit 3 (replace_text p4)" in message and "edit 4" in message and "edit 1" not in message.replace("edit 1 of", "")
    assert (ws / "a.edited.docx").read_bytes() == before
    assert not list(ws.glob(".tadamoo-*"))


def test_a_failed_save_leaves_the_original_untouched(env, monkeypatch):
    ws = env[2]
    before = (ws / "a.edited.docx").read_bytes()
    import tadamoo.tools.docx_edit as mod
    monkeypatch.setattr(mod.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        edit(env, [{"kind": "replace_text", "target": "p2", "find": "brave", "replace": "bold"}])
    monkeypatch.undo()
    assert (ws / "a.edited.docx").read_bytes() == before and not list(ws.glob(".tadamoo-*"))


def test_an_edit_after_a_delete_of_the_same_paragraph_fails(env):
    with pytest.raises(EditError, match="deleted by an earlier edit"):
        edit(env, [{"kind": "delete_paragraph", "target": "p4"}, {"kind": "replace_text", "target": "p4", "find": "Second", "replace": "x"}])


def test_unknown_fields_are_named(env):
    with pytest.raises(EditError, match="has no field replacement"):
        edit(env, [{"kind": "replace_text", "target": "p2", "find": "a", "replacement": "b"}])


# -- tracked changes ---------------------------------------------------------------------------------------------------------


def test_tracked_replace_across_runs_writes_ins_and_del_and_the_file_still_opens(env):
    ws = env[2]
    result = edit(env, [{"kind": "replace_text", "target": "p2", "find": "brave new world", "replace": "BRAVE WORLD"}], track_changes=True)
    assert result["tracked"] is True
    document = xml(ws)
    assert f"<{'w:del '}" in document and "<w:ins " in document and "<w:delText" in document
    assert 'w:author="TaDaMoo"' in document and "w:date=" in document
    doc = Document(str(ws / "a.edited.docx"))
    assert run(env, "docx_read", path="a.edited.docx", target="p2").startswith("p2 [Normal] Hello BRAVE WORLD, hello again.")      # python-docx's own .text skips w:ins
    deleted = "".join(t.text for t in doc.element.body.iter(f"{W}delText"))
    assert deleted == "brave new world"
    diff = env[1].changes[-1]["diff"]
    assert "[-" in diff and "{+BRAVE WORLD+}" in diff


def test_tracked_insert_delete_set_and_style_changes(env):
    ws = env[2]
    edit(env, [{"kind": "insert_paragraph", "after": "p4", "text": "added line"},
               {"kind": "delete_paragraph", "target": "p5"},
               {"kind": "set_paragraph", "target": "p2", "text": "Rewritten"},
               {"kind": "set_style", "target": "p4", "style": "Heading 2"}], track_changes=True)
    document = xml(ws)
    assert "<w:pPrChange" in document and document.count("<w:ins ") >= 2 and document.count("<w:del ") >= 2
    doc = Document(str(ws / "a.edited.docx"))                                          # still a valid document
    assert "added line" in run(env, "docx_read", path="a.edited.docx", target="p1-p6")
    ids = [e.get(f"{W}id") for e in doc.element.body.iter(f"{W}ins", f"{W}del", f"{W}pPrChange")]
    assert len(ids) == len(set(ids))


def test_tracked_table_rows_and_cells(env):
    ws = env[2]
    edit(env, [{"kind": "insert_table_row", "table": "t1", "after_row": 3, "cells": ["fig", "7"]},
               {"kind": "delete_table_row", "table": "t1", "row": 2},
               {"kind": "set_cell", "target": "t1.r3.c2", "text": "50"}], track_changes=True)
    document = xml(ws)
    assert "<w:trPr><w:ins " in document and "<w:trPr><w:del " in document
    Document(str(ws / "a.edited.docx"))
    with pytest.raises(EditError, match="table style cannot be written as a tracked change"):
        edit(env, [{"kind": "set_style", "target": "t1", "style": "Table Grid"}], track_changes=True)


# -- create ------------------------------------------------------------------------------------------------------------------


def test_create_a_document_from_blocks_and_refuse_to_overwrite(env):
    ws = env[2]
    blocks = [{"kind": "heading", "text": "Report", "level": 0}, {"kind": "heading", "text": "Part", "level": 1},
              {"kind": "paragraph", "text": "Body text", "bold": True}, {"kind": "bullet", "text": "one"}, {"kind": "numbered", "text": "first"},
              {"kind": "table", "rows": [["a", "b"], ["1", "2"]]}, {"kind": "page_break"}, {"kind": "paragraph", "text": "after"}]
    result = run(env, "docx_create", path="new/r.docx", blocks=blocks)
    assert result["kind"] == "added"
    doc = Document(str(ws / "new" / "r.docx"))
    assert [p.style.name for p in doc.paragraphs[:5]] == ["Title", "Heading 1", "Normal", "List Bullet", "List Number"]
    assert doc.paragraphs[2].runs[0].bold is True and doc.tables[0].cell(1, 1).text == "2" and doc.tables[0].cell(0, 0).paragraphs[0].runs[0].bold
    with pytest.raises(EditError, match="already exists"):
        run(env, "docx_create", path="new/r.docx", blocks=blocks)
    result = run(env, "docx_create", path="new/r.docx", blocks=[{"kind": "paragraph", "text": "v2"}], overwrite=True)
    assert result["kind"] == "modified" and paragraphs(ws, "new/r.docx") == ["v2"]
    with pytest.raises(EditError, match="unknown kind 'chart'"):
        run(env, "docx_create", path="b.docx", blocks=[{"kind": "chart"}])
    assert not (ws / "b.docx").exists()


# -- the harness around them -----------------------------------------------------------------------------------------------


def test_paths_outside_the_workspace_and_other_formats_are_refused(env):
    ws = env[2]
    make_doc(ws.parent / "outside.docx")
    with pytest.raises(PermissionError, match="escapes workspace"):
        run(env, "docx_outline", path="../outside.docx")
    with pytest.raises(PermissionError, match="escapes workspace"):
        edit_path = {"kind": "replace_text", "find": "a", "replace": "b"}
        run(env, "docx_edit", path="../outside.docx", edits=[edit_path])
    with pytest.raises(PermissionError, match="escapes workspace"):
        run(env, "docx_create", path="../x.docx", blocks=[{"kind": "paragraph", "text": "x"}])
    (ws / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(EditError, match="not a .docx"):
        run(env, "docx_outline", path="old.doc")
    (ws / "fake.docx").write_bytes(b"not a zip")
    with pytest.raises(EditError, match="could not be opened"):
        run(env, "docx_read", path="fake.docx", target="p1")


def test_a_change_is_recorded_like_edit_file_but_its_text_is_never_offered_for_write_back(env):
    sandbox = env[1]
    edit(env, [{"kind": "replace_text", "target": "p5", "find": "end", "replace": "finish"}])
    (change,) = sandbox.changes
    assert change["file"] == "a.edited.docx" and change["kind"] == "modified" and (change["added"], change["removed"]) == (1, 1)
    assert "-The end." in change["diff"] and "+The finish." in change["diff"]
    assert change["old_text"] is None and change["new_text"] is None          # a binary file: the review view must not write text into it


def loop(ws, tmp_path, steps):
    registry, _ = create_standard_tool_registry(ws)
    cps = CheckpointManager(ws, storage_dir=tmp_path / "cps", settings=tmp_path / "none.json")
    agent = AgentLoop(client=ScriptedClient(*steps), tool_registry=registry, permissions=PermissionManager(level=PermissionLevel.DEFAULT), checkpoints=cps)
    return agent, cps


def test_the_loop_checkpoints_the_docx_first_and_a_rewind_restores_it_byte_for_byte(env, tmp_path):
    ws = env[2]
    original = (ws / "a.edited.docx").read_bytes()
    edits = [{"kind": "replace_text", "target": "p2", "find": "brave", "replace": "bold"}]
    agent, cps = loop(ws, tmp_path, [call("docx_edit", {"path": "a.edited.docx", "edits": edits}), say("done")])
    agent.run("change the word")
    assert (ws / "a.edited.docx").read_bytes() != original and "bold" in paragraphs(ws)[1]
    (only,) = cps.list_checkpoints()
    assert [Path(e["path"]).name for e in only["entries"]] == ["a.edited.docx"]
    cps.restore(only["checkpoint_id"])
    assert (ws / "a.edited.docx").read_bytes() == original


def test_a_new_docx_is_a_checkpoint_entry_that_rewind_removes(env, tmp_path):
    ws = env[2]
    agent, cps = loop(ws, tmp_path, [call("docx_create", {"path": "n.docx", "blocks": [{"kind": "paragraph", "text": "x"}]}), say("done")])
    agent.run("make it")
    assert (ws / "n.docx").exists()
    (only,) = cps.list_checkpoints()
    cps.restore(only["checkpoint_id"])
    assert not (ws / "n.docx").exists()
    assert [p.name for p in call_paths(ws, "docx_edit", {"path": "a.edited.docx", "edits": []})] == ["a.edited.docx"]


def test_the_writing_tools_are_reversible_writes_and_the_readers_are_pure(env):
    registry = env[0]
    assert {n: registry.get(n).effect for n in ("docx_outline", "docx_read", "docx_edit", "docx_create")} == {
        "docx_outline": EffectClass.PURE, "docx_read": EffectClass.PURE, "docx_edit": EffectClass.REVERSIBLE, "docx_create": EffectClass.REVERSIBLE}
    assert {"docx_edit", "docx_create"} <= set(GROUPS["write"]) and {"docx_outline", "docx_read"} <= set(GROUPS["read"])
    perms = PermissionManager(level=PermissionLevel.DEFAULT)
    assert perms.evaluate("docx_edit", EffectClass.REVERSIBLE, {"path": "a.edited.docx", "edits": []})[0]       # like edit_file: workspace edits pass at the default level


def test_read_only_roles_list_the_readers_but_not_the_writers(env):
    registry, _, ws = env
    names = registry.names
    for role in (ASK, ORCHESTRATE):
        policy = RolePolicy(ws, names, role)
        assert {"docx_outline", "docx_read"} <= set(policy.allowed())
        assert not {"docx_edit", "docx_create"} & set(policy.allowed())
        assert "not available in the" in policy.check("docx_edit", {"path": "a.edited.docx"})
    plan = RolePolicy(ws, names, PLAN)
    assert "may change only" in plan.check("docx_edit", {"path": "a.edited.docx"}) and plan.check("docx_edit", {"path": ".tadamoo/plans/x.md"}) == ""
    for role in (CODING_ROLE, GENERAL):
        policy = RolePolicy(ws, names, role)
        assert {"docx_edit", "docx_create", "docx_outline", "docx_read"} <= set(policy.allowed())
        assert policy.check("docx_edit", {"path": "a.edited.docx"}) == ""
