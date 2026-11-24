"""The PDF tools: what can be done exactly is done (forms, pages, text and pictures on a page, highlights, metadata), text is replaced in place only where the
page holds it in a font that can show the new letters, and what cannot be done says so. The original is never changed unless asked."""
import base64
from pathlib import Path

import pytest

pytest.importorskip("pikepdf")
pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")
from pypdf import PdfReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from test_docx_parts import Sandbox  # noqa: E402
from tadamoo.tools.office_io import OfficeError  # noqa: E402
from tadamoo.tools.pdf_edit import PdfTools, page_list  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make(path: Path) -> Path:
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 12)
    c.drawString(72, 760, "The total price is EUR 1,200.00 payable within 30 days.")
    c.drawString(72, 740, "Warranty 24 months.")
    c.showPage()
    c.drawString(72, 760, "Appendix: technical annex with no prices.")
    c.showPage()
    c.drawString(72, 760, "Third page.")
    c.showPage()
    c.save()
    return path


def make_form(path: Path) -> Path:
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.drawString(72, 800, "Name:")
    c.acroForm.textfield(name="full_name", x=120, y=790, width=200, height=20)
    c.showPage()
    c.save()
    return path


def text(path: Path) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


@pytest.fixture
def tools(tmp_path):
    make(tmp_path / "o.pdf")
    return PdfTools(Sandbox(tmp_path)), tmp_path / "o.pdf", tmp_path


def test_page_lists_are_read_with_ranges_and_open_ends():
    assert page_list("1-2,4", 5) == [1, 2, 4] and page_list("3-", 5) == [3, 4, 5] and page_list(None, 3) == [1, 2, 3]
    with pytest.raises(OfficeError):
        page_list("2-9", 5)


def test_the_outline_and_the_finder_describe_the_pages(tools):
    t, _, _ = tools
    out = t.outline("o.pdf")
    assert "3 pages" in out and "p1:" in out and "no form fields" in out and "The total price" in out
    found = t.find("o.pdf", "1,200.00")
    assert "p1 x=" in found and "size=12.0" in found and "not in the text layer" in t.find("o.pdf", "nothing here")


def test_a_price_is_replaced_in_place_and_the_original_is_kept(tools):
    t, path, tmp = tools
    before = path.read_bytes()
    out = t.edit("o.pdf", [{"kind": "replace_text", "find": "1,200.00", "replace": "1,500.00"}])
    assert path.read_bytes() == before and out["written"] == "o.edited.pdf" and "1 of 1" in out["applied"][0]
    new = text(tmp / "o.edited.pdf")
    assert "1,500.00" in new and "1,200.00" not in new and "Warranty 24 months." in new and len(PdfReader(str(tmp / "o.edited.pdf")).pages) == 3
    t.edit("o.pdf", [{"kind": "replace_text", "find": "Warranty", "replace": "Guarantee"}], in_place=True)
    assert "Guarantee 24 months." in text(path)


def test_a_replacement_that_cannot_be_made_in_place_says_why_and_cover_text_leaves_the_old_words(tools):
    t, path, tmp = tools
    with pytest.raises(OfficeError, match="is not on"):
        t.edit("o.pdf", [{"kind": "replace_text", "find": "absent words", "replace": "x"}])
    with pytest.raises(OfficeError, match="Latin"):
        t.edit("o.pdf", [{"kind": "replace_text", "find": "1,200.00", "replace": "日本"}])
    out = t.edit("o.pdf", [{"kind": "cover_text", "find": "24 months", "replace": "36 months"}], output="c.pdf")
    assert "still in the file" in out["applied"][0] and "36 months" in text(tmp / "c.pdf") and "24 months" in text(tmp / "c.pdf")


def test_pages_are_deleted_kept_rotated_reordered_and_appended(tools):
    t, _, tmp = tools
    t.edit("o.pdf", [{"kind": "delete_pages", "pages": "2"}, {"kind": "rotate_pages", "pages": "1", "degrees": 90}], output="a.pdf")
    r = PdfReader(str(tmp / "a.pdf"))
    assert len(r.pages) == 2 and r.pages[0].get("/Rotate") == 90 and "Third page." in r.pages[1].extract_text()
    t.edit("o.pdf", [{"kind": "reorder_pages", "order": [3, 1, 2]}], output="r.pdf")
    assert PdfReader(str(tmp / "r.pdf")).pages[0].extract_text().startswith("Third page.")
    t.edit("o.pdf", [{"kind": "append_pdf", "file": "o.pdf", "pages": "3", "at": 1}], output="m.pdf")
    assert len(PdfReader(str(tmp / "m.pdf")).pages) == 4 and "Third page." in PdfReader(str(tmp / "m.pdf")).pages[1].extract_text()
    with pytest.raises(OfficeError, match="at least one page"):
        t.edit("o.pdf", [{"kind": "delete_pages", "pages": "1-3"}])
    with pytest.raises(OfficeError, match="every page"):
        t.edit("o.pdf", [{"kind": "reorder_pages", "order": [1, 2]}])


def test_text_pictures_watermarks_highlights_and_metadata_are_added(tools):
    t, _, tmp = tools
    (tmp / "logo.png").write_bytes(PNG)
    t.edit("o.pdf", [{"kind": "add_text", "page": 2, "x": 72, "y": 300, "text": "Approved by Rossi", "size": 14},
                     {"kind": "add_image", "page": 1, "image": "logo.png", "x": 400, "y": 50, "width": 40},
                     {"kind": "watermark", "text": "DRAFT", "pages": "1"},
                     {"kind": "highlight", "find": "Warranty"},
                     {"kind": "set_metadata", "title": "Offer", "author": "Ada"}], output="e.pdf")
    r = PdfReader(str(tmp / "e.pdf"))
    assert "Approved by Rossi" in r.pages[1].extract_text() and "DRAFT" in r.pages[0].extract_text()
    assert r.metadata.title == "Offer" and len(r.pages[0].get("/Annots", [])) == 1


def test_a_form_is_filled_and_an_unknown_field_is_named(tmp_path):
    make_form(tmp_path / "f.pdf")
    t = PdfTools(Sandbox(tmp_path))
    assert "full_name" in t.outline("f.pdf")
    t.edit("f.pdf", [{"kind": "fill_form", "fields": {"full_name": "Ada Lovelace"}}], output="g.pdf")
    assert PdfReader(str(tmp_path / "g.pdf")).get_fields()["full_name"].get("/V") == "Ada Lovelace"
    with pytest.raises(OfficeError, match="no such field: nope"):
        t.edit("f.pdf", [{"kind": "fill_form", "fields": {"nope": "x"}}])


def test_one_bad_edit_writes_nothing_and_a_name_is_never_overwritten_unasked(tools):
    t, _, tmp = tools
    with pytest.raises(OfficeError, match="Nothing was changed"):
        t.edit("o.pdf", [{"kind": "watermark", "text": "A"}, {"kind": "delete_pages", "pages": "9"}], output="x.pdf")
    assert not (tmp / "x.pdf").exists()
    t.edit("o.pdf", [{"kind": "set_metadata", "title": "A"}], output="y.pdf")
    with pytest.raises(OfficeError, match="already exists"):
        t.edit("o.pdf", [{"kind": "set_metadata", "title": "B"}], output="y.pdf")
    assert t.edit("o.pdf", [{"kind": "set_metadata", "title": "B"}])["written"] == "o.edited.pdf"
    assert t.edit("o.pdf", [{"kind": "set_metadata", "title": "C"}])["written"] == "o.edited2.pdf"
