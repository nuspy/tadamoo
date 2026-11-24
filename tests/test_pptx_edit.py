"""The PowerPoint tools: outline and read to find what to change, ordered all-or-nothing edits that keep the formatting, and creation of a deck."""
import base64
from pathlib import Path

import pytest

pptx = pytest.importorskip("pptx")
from pptx import Presentation  # noqa: E402
from pptx.util import Cm  # noqa: E402

from test_docx_parts import Sandbox  # noqa: E402
from tadamoo.tools.office_io import OfficeError  # noqa: E402
from tadamoo.tools.pptx_edit import PptxTools  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make(path: Path) -> Path:
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text_frame.text = "Offer 2026"
    body = s1.placeholders[1].text_frame
    body.text = "The price is "
    r = body.paragraphs[0].add_run()
    r.text = "1,200.00"
    r.font.bold = True
    body.paragraphs[0].add_run().text = " euro."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text_frame.text = "Items"
    table = s2.shapes.add_table(3, 2, Cm(2), Cm(5), Cm(12), Cm(4)).table
    for r_, row in enumerate([["Item", "Price"], ["Valve", "1,200.00"], ["Pump", "88.00"]]):
        for c, v in enumerate(row):
            table.cell(r_, c).text = v
    s2.notes_slide.notes_text_frame.text = "Mention 1,200.00 first"
    prs.save(path)
    return path


@pytest.fixture
def tools(tmp_path):
    make(tmp_path / "d.edited.pptx")
    return PptxTools(Sandbox(tmp_path)), tmp_path / "d.edited.pptx"


def table_id(t):
    prs = Presentation(str(t[1]))
    return [sh.shape_id for sh in prs.slides[1].shapes if getattr(sh, "has_table", False) and sh.has_table][0]


def test_the_outline_and_the_reader_name_every_address(tools):
    t, _ = tools
    out = t.outline("d.edited.pptx")
    assert "2 slides" in out and "s1 [Title and Content] Offer 2026" in out and "table 3x2" in out and "(has notes)" in out
    sid = table_id(tools)
    assert "Valve | 1,200.00" in t.read("d.edited.pptx", "s2") and t.read("d.edited.pptx", f"s2.sh{sid}.r2.c2") == "1,200.00" and "Mention" in t.read("d.edited.pptx", "s2.notes")


def test_a_replacement_across_runs_keeps_the_bold_and_reaches_tables_and_notes(tools):
    t, path = tools
    out = t.edit("d.edited.pptx", [{"kind": "replace_text", "find": "1,200.00", "replace": "1,500.00", "count": 0}])
    assert "3 of 3" in out["applied"][0]
    prs = Presentation(str(path))
    runs = prs.slides[0].placeholders[1].text_frame.paragraphs[0].runs
    assert [r.text for r in runs] == ["The price is ", "1,500.00", " euro."] and runs[1].font.bold
    assert prs.slides[1].notes_slide.notes_text_frame.text == "Mention 1,500.00 first"
    assert prs.slides[1].shapes[-1].table.cell(1, 1).text == "1,500.00"


def test_slides_are_added_moved_deleted_and_get_notes_in_order(tools):
    t, path = tools
    t.edit("d.edited.pptx", [{"kind": "add_slide", "layout": "Title and Content", "title": "Terms", "body": ["30 days", "24 months"], "notes": "say it slowly"},
                      {"kind": "move_slide", "slide": 3, "to": 1}, {"kind": "delete_slide", "slide": 3}, {"kind": "set_notes", "slide": 1, "text": "new notes"}])
    prs = Presentation(str(path))
    assert [s.shapes.title.text_frame.text for s in prs.slides] == ["Terms", "Offer 2026"]
    assert prs.slides[0].notes_slide.notes_text_frame.text == "new notes"
    assert [p.text for p in prs.slides[0].placeholders[1].text_frame.paragraphs] == ["30 days", "24 months"]


def test_shapes_are_set_deleted_and_added(tools, tmp_path):
    t, path = tools
    (tmp_path / "logo.png").write_bytes(PNG)
    prs = Presentation(str(path))
    body_id = [sh.shape_id for sh in prs.slides[0].shapes if sh.placeholder_format.idx == 1][0]
    t.edit("d.edited.pptx", [{"kind": "set_text", "target": f"s1.sh{body_id}", "text": "First line\nSecond line"},
                      {"kind": "insert_picture", "slide": 1, "image": "logo.png", "width_cm": 2, "alt": "logo"},
                      {"kind": "add_textbox", "slide": 2, "text": "Valid 30 days", "size": 12}])
    prs = Presentation(str(path))
    assert [p.text for p in prs.slides[0].placeholders[1].text_frame.paragraphs] == ["First line", "Second line"]
    assert any(sh.shape_type == 13 for sh in prs.slides[0].shapes) and prs.slides[1].shapes[-1].text_frame.text == "Valid 30 days"
    t.edit("d.edited.pptx", [{"kind": "delete_shape", "target": f"s1.sh{body_id}"}])
    assert all(sh.shape_id != body_id for sh in Presentation(str(path)).slides[0].shapes)


def test_one_bad_edit_changes_nothing_and_output_keeps_the_original(tools, tmp_path):
    t, path = tools
    before = path.read_bytes()
    with pytest.raises(OfficeError, match="Nothing was changed"):
        t.edit("d.edited.pptx", [{"kind": "replace_text", "find": "Offer", "replace": "Quote"}, {"kind": "delete_slide", "slide": 9}, {"kind": "replace_text", "find": "zzz", "replace": "y"}])
    assert path.read_bytes() == before
    t.edit("d.edited.pptx", [{"kind": "replace_text", "find": "Offer", "replace": "Quote"}], output="copy.pptx")
    assert path.read_bytes() == before and Presentation(str(tmp_path / "copy.pptx")).slides[0].shapes.title.text_frame.text == "Quote 2026"


def test_a_deck_is_created_from_slides(tools, tmp_path):
    t, _ = tools
    t.create("new.pptx", [{"title": "Plan"}, {"title": "Steps", "body": ["one", "two"], "notes": "n"}])
    prs = Presentation(str(tmp_path / "new.pptx"))
    assert len(prs.slides) == 2 and prs.slides[1].placeholders[1].text_frame.text == "one\ntwo"
    with pytest.raises(OfficeError, match="already exists"):
        t.create("new.pptx", [{"title": "x"}])
