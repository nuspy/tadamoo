"""Putting a picture in a Word document: where it goes, its width and caption, the alt text, tracked changes, creation from blocks, and what is refused."""
import zipfile

import pytest

docx = pytest.importorskip("docx")
from docx import Document  # noqa: E402

from test_docx_edit import PNG, W, make_doc  # noqa: E402
from tadamoo.tools import create_standard_tool_registry  # noqa: E402


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    registry, sandbox = create_standard_tool_registry(ws)
    make_doc(ws / "a.docx")
    (ws / "fig.png").write_bytes(PNG)
    return registry, ws


def edit(env, edits, **kw):
    return env[0].execute("docx_edit", {"path": "a.docx", "edits": edits, **kw})


def texts(ws):
    return [p.text for p in Document(str(ws / "a.docx")).paragraphs]


def test_a_picture_goes_after_the_paragraph_that_discusses_it_with_its_numbered_caption_and_alt_text(env):
    edit(env, [{"kind": "insert_picture", "after": "p4", "image": "fig.png", "caption": "Map of the plasmid (source: patent.pdf, p. 2)", "alt": "A plasmid map", "width_cm": 8}])
    doc = Document(str(env[1] / "a.docx"))
    par = [p.text for p in doc.paragraphs]
    assert par[4] == "" and par[5] == "Figure 1 - Map of the plasmid (source: patent.pdf, p. 2)" and par[3] == "Second para with a price of 10 euro."
    shape = doc.inline_shapes[0]
    assert abs(shape.width.cm - 8) < 0.05 and doc.paragraphs[4].alignment == 1 and shape._inline.docPr.get("descr") == "A plasmid map"
    assert doc.paragraphs[5].style.name in ("Caption", "Normal")


def test_two_pictures_of_one_call_and_one_already_in_the_document_are_numbered_in_order_and_a_wide_one_is_cut_to_the_text(env):
    edit(env, [{"kind": "insert_picture", "after": "end", "image": "fig.png", "caption": "First"}, {"kind": "insert_picture", "before": "p1", "image": "fig.png", "caption": "Second", "width_cm": 99}])
    par = texts(env[1])
    assert "Figure 1 - First" in par and "Figure 2 - Second" in par
    doc = Document(str(env[1] / "a.docx"))
    assert max(s.width.cm for s in doc.inline_shapes) <= 16.6
    edit(env, [{"kind": "insert_picture", "after": "p2", "image": "fig.png", "caption": "Third"}, {"kind": "insert_picture", "after": "p2", "image": "fig.png", "caption": "Plain", "numbered": False}])
    par = texts(env[1])
    assert "Figure 3 - Third" in par and "Plain" in par and "Figure 3 - Plain" not in par


def test_a_picture_can_be_a_tracked_insertion(env):
    edit(env, [{"kind": "insert_picture", "after": "p4", "image": "fig.png", "caption": "Tracked"}], track_changes=True)
    with zipfile.ZipFile(env[1] / "a.docx") as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.count(f"<{'w:ins'}") >= 2 and "Figure 1 - Tracked" in xml and "w:drawing" in xml


def test_what_is_not_a_usable_picture_or_place_is_refused_and_nothing_is_written(env):
    ws = env[1]
    (ws / "x.webp").write_bytes(b"RIFFxxxxWEBP")
    (ws / "x.png").write_bytes(PNG)
    before = (ws / "a.docx").read_bytes()
    for bad, word in (({"kind": "insert_picture", "after": "p1", "image": "x.webp"}, "WebP"), ({"kind": "insert_picture", "after": "p1", "image": "nothing.png"}, "no picture"),
                      ({"kind": "insert_picture", "after": "p1"}, "relative path"), ({"kind": "insert_picture", "image": "fig.png"}, "exactly one"),
                      ({"kind": "insert_picture", "after": "p1", "image": "fig.png", "width_cm": -2}, "more than 0"), ({"kind": "insert_picture", "after": "t1.r1.c1", "image": "fig.png"}, "cell"),
                      ({"kind": "insert_picture", "after": "p1", "image": "fig.png", "colour": 1}, "colour")):
        with pytest.raises(Exception, match=word):
            edit(env, [bad])
    with pytest.raises(Exception):
        edit(env, [{"kind": "insert_picture", "after": "p1", "image": "../outside.png"}])
    assert (ws / "a.docx").read_bytes() == before


def test_a_new_document_can_hold_pictures_between_its_blocks(env):
    ws = env[1]
    env[0].execute("docx_create", {"path": "n.docx", "blocks": [{"kind": "heading", "text": "Report"}, {"kind": "picture", "image": "fig.png", "caption": "A chart", "width_cm": 6},
                                                              {"kind": "paragraph", "text": "After."}, {"kind": "picture", "image": "fig.png"}]})
    doc = Document(str(ws / "n.docx"))
    assert [p.text for p in doc.paragraphs] == ["Report", "", "Figure 1 - A chart", "After.", ""] and len(doc.inline_shapes) == 2
    out = env[0].execute("docx_outline", {"path": "n.docx"})
    assert "2 images" in str(out)
    with pytest.raises(Exception, match="WebP|picture"):
        (ws / "y.webp").write_bytes(b"x")
        env[0].execute("docx_create", {"path": "m.docx", "blocks": [{"kind": "picture", "image": "y.webp"}]})
