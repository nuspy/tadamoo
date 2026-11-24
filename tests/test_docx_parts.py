"""Word text outside the body: headers, footers, footnotes, endnotes and text boxes are listed, read and changed with the same run-level editor."""
import re
import shutil
import zipfile
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
from docx import Document  # noqa: E402
from docx.oxml import parse_xml  # noqa: E402
from lxml import etree  # noqa: E402

from tadamoo.tools import docx_parts  # noqa: E402
from tadamoo.tools.docx_edit import DocxTools  # noqa: E402
from tadamoo.tools.docx_runs import EditError  # noqa: E402

WNS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NOTES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes xmlns:w="%s">'
         '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
         '<w:footnote w:id="1"><w:p><w:r><w:t xml:space="preserve">See the price of </w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>1,200.00</w:t></w:r>'
         '<w:r><w:t xml:space="preserve"> euro.</w:t></w:r></w:p></w:footnote></w:footnotes>' % WNS)


class Sandbox:
    def __init__(self, root):
        self.root = Path(root)

    def resolve_safe(self, path, tool=""):
        return self.root / path

    def _where(self, path, safe):
        return ""

    def _owner_block(self, *a, **k):
        return None

    def _guard_git(self, *a, **k):
        return None

    def _record(self, path, kind, old, new, keep_text=False):
        return {"status": "ok", "path": path, "kind": kind}


def build(path: Path) -> Path:
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Offer 2026-114 - DRAFT"
    doc.sections[0].footer.paragraphs[0].text = "Confidential - DRAFT"
    doc.add_paragraph("The price is 1,200.00 euro.")
    box = parse_xml('<w:r xmlns:w="%s" xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape><v:textbox><w:txbxContent><w:p><w:r><w:t>Valid until DRAFT-31</w:t></w:r></w:p>'
                           '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>' % WNS)
    doc.paragraphs[-1]._p.append(box)
    tmp = path.with_suffix(".tmp.docx")
    doc.save(tmp)
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", b'<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>')
            if item.filename == "word/_rels/document.xml.rels":
                data = data.replace(b"</Relationships>", b'<Relationship Id="rIdFn1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/></Relationships>')
            out.writestr(item, data)
        out.writestr("word/footnotes.xml", NOTES)
    tmp.unlink()
    return path


@pytest.fixture
def tools(tmp_path):
    build(tmp_path / "offer.docx")
    return DocxTools(Sandbox(tmp_path)), tmp_path / "offer.docx"


def test_the_outline_lists_what_the_file_keeps_outside_the_body(tools):
    t, _ = tools
    out = t.outline("offer.docx")
    assert "headers:" in out and "Offer 2026-114" in out and "footers:" in out and "footnotes:" in out and "textboxes:" in out and "Valid until" in out


def test_each_part_can_be_read_by_its_name(tools):
    t, _ = tools
    assert "DRAFT" in t.read("offer.docx", "headers") and "Confidential" in t.read("offer.docx", "footers")
    assert "1,200.00" in t.read("offer.docx", "footnotes") and "Valid until" in t.read("offer.docx", "textboxes")
    assert "separator" not in t.read("offer.docx", "footnotes")


def test_a_replacement_in_every_part_keeps_the_formatting_and_the_other_parts(tools):
    t, path = tools
    out = t.edit("offer.docx", [{"kind": "replace_text", "target": "everywhere", "find": "DRAFT", "replace": "FINAL", "count": 0},
                                {"kind": "replace_text", "target": "footnotes", "find": "1,200.00", "replace": "1,500.00"},
                                {"kind": "replace_text", "target": "all", "find": "1,200.00", "replace": "1,500.00"}])
    assert out["edits"] == 3
    doc = Document(str(path))
    assert doc.sections[0].header.paragraphs[0].text == "Offer 2026-114 - FINAL" and doc.sections[0].footer.paragraphs[0].text == "Confidential - FINAL"
    assert "1,500.00" in doc.paragraphs[0].text
    with zipfile.ZipFile(path) as z:
        notes = z.read("word/footnotes.xml").decode("utf-8")
        body = z.read("word/document.xml").decode("utf-8")
    assert "1,500.00" in notes and "1,200.00" not in notes and re.search(r"<w:b/></w:rPr><w:t[^>]*>1,500.00</w:t>", notes) and "separator" in notes
    assert "Valid until FINAL-31" in body


def test_a_text_that_is_nowhere_in_the_part_says_so_and_changes_nothing(tools):
    t, path = tools
    before = path.read_bytes()
    with pytest.raises(EditError, match="not in footers"):
        t.edit("offer.docx", [{"kind": "replace_text", "target": "footers", "find": "nothing like this", "replace": "x"}])
    assert path.read_bytes() == before
