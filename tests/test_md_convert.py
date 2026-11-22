"""A Markdown file the agent made (or one in the conversation's folder) downloaded as Word, OpenDocument, PDF or a web page, or together with its Markdown in a zip.
No model is involved: the same readers and writers as the conversation export."""
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import mdexport
from serve.exportdoc import ExportError
from tadamoo import attachments as at
from test_attachments_api import world  # noqa: F401  (fixture)
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixtures of the world above)

MD = "# Rapporto\n\nUn **paragrafo** con `codice`, un [link](https://example.com) e una è accentata.\n\n- uno\n- due\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('ciao')\n```\n".encode()


def text_of(zipped: bytes, member: str) -> str:
    return zipfile.ZipFile(io.BytesIO(zipped)).read(member).decode("utf-8")


def test_word_and_opendocument_are_zip_packages_with_the_text_and_are_the_same_bytes_every_time():
    docx, ctype, name = mdexport.convert(MD, "Il rapporto.md", "docx", "it")
    assert name == "Il rapporto.docx" and "wordprocessingml" in ctype
    body = text_of(docx, "word/document.xml")
    assert "Rapporto" in body and "paragrafo" in body and "è accentata" in body and "print(" in body
    assert mdexport.convert(MD, "Il rapporto.md", "docx", "it")[0] == docx
    odt, ctype, name = mdexport.convert(MD, "Il rapporto.md", "odt", "it")
    assert name == "Il rapporto.odt" and ctype.endswith("opendocument.text") and "paragrafo" in text_of(odt, "content.xml")
    assert mdexport.convert(MD, "Il rapporto.md", "odt", "it")[0] == odt


def test_the_web_page_is_one_file_with_no_script_and_keeps_the_table_and_the_code():
    page, ctype, name = mdexport.convert(MD, "a.md", "html", "en")
    html = page.decode("utf-8")
    assert name == "a.html" and ctype.startswith("text/html") and "<table" in html and "<pre" in html and "<script" not in html and "è accentata" in html


def test_a_pdf_is_made_by_the_browser_where_there_is_one_and_refused_by_name_where_there_is_not(monkeypatch):
    if mdexport._browser() is None:
        pytest.skip("no browser on this PC")
    pdf, ctype, name = mdexport.convert(MD, "a.md", "pdf", "en")
    assert pdf.startswith(b"%PDF") and ctype == "application/pdf" and name == "a.pdf"
    monkeypatch.setattr(mdexport, "_browser", lambda: None)
    assert "pdf" not in [f["id"] for f in mdexport.formats("en")]
    with pytest.raises(ExportError, match="Edge, Chrome or Chromium"):
        mdexport.convert(MD, "a.md", "pdf", "en")


def test_both_is_a_zip_with_the_markdown_as_it_is_and_the_converted_file():
    data, ctype, name = mdexport.prepare(MD, "Il rapporto.md", "both", "docx", "it")
    assert name == "Il rapporto.zip" and ctype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == ["Il rapporto.docx", "Il rapporto.md"] and z.read("Il rapporto.md") == MD


def test_the_formats_are_listed_in_the_persons_language_and_what_cannot_be_made_here_is_left_out(monkeypatch):
    monkeypatch.setattr(mdexport, "_browser", lambda: None)
    monkeypatch.setattr(mdexport, "_pandoc", lambda: None)
    assert [f["id"] for f in mdexport.formats("it")] == ["docx", "odt", "html"]
    assert mdexport.formats("it")[1]["label"] == "Testo OpenDocument (.odt)" and mdexport.formats("en")[1]["label"] == "OpenDocument text (.odt)"
    monkeypatch.setattr(mdexport, "_pandoc", lambda: "pandoc")
    assert [f["id"] for f in mdexport.formats("en")][-2:] == ["epub", "rtf"]


def test_a_wrong_format_a_wrong_mode_and_a_file_that_is_too_large_are_refused():
    with pytest.raises(ExportError):
        mdexport.convert(MD, "a.md", "exe", "en")
    with pytest.raises(ExportError):
        mdexport.prepare(MD, "a.md", "everything", "docx", "en")
    with pytest.raises(ExportError, match="too large"):
        mdexport.convert(b"x" * (mdexport.MAX_BYTES + 1), "a.md", "docx", "en")


def test_the_download_of_an_output_is_converted_and_a_missing_one_is_not_found(world):  # noqa: F811
    api, mgr, sid, tmp = world
    meta = at.OutputStore(tmp / "outputs").save(sid, "Rapporto finale.md", MD)
    code, headers, body = api.raw("GET", f"/api/outputs/{sid}/{meta['id']}/convert?format=docx&mode=converted&lang=it")
    assert code == 200 and "Rapporto%20finale.docx" in headers["Content-Disposition"] and "wordprocessingml" in headers["Content-Type"]
    assert "paragrafo" in text_of(body, "word/document.xml")
    code, headers, body = api.raw("GET", f"/api/outputs/{sid}/{meta['id']}/convert?format=odt&mode=both")
    assert code == 200 and headers["Content-Type"] == "application/zip" and sorted(zipfile.ZipFile(io.BytesIO(body)).namelist()) == ["Rapporto finale.md", "Rapporto finale.odt"]
    assert api.raw("GET", f"/api/outputs/{sid}/{meta['id']}/convert?format=exe&mode=converted")[0] == 400
    assert api.raw("GET", f"/api/outputs/{sid}/{meta['id']}/convert?format=docx&mode=nothing")[0] == 400
    assert api.raw("GET", f"/api/outputs/{sid}/{'1' * 12}/convert?format=docx&mode=converted")[0] == 404


def test_a_markdown_file_in_the_conversations_folder_is_converted_too_and_only_inside_the_folder(world, tmp_path):  # noqa: F811
    api, mgr, sid, tmp = world
    (tmp / "ws" / "nota.md").write_bytes(MD)
    code, headers, body = api.raw("GET", f"/api/agent/sessions/{sid}/file-convert?path=nota.md&format=html&mode=converted")
    assert code == 200 and b"<table" in body and "nota.html" in headers["Content-Disposition"]
    assert api.raw("GET", f"/api/agent/sessions/{sid}/file-convert?path=../outside.md&format=html&mode=converted")[0] in (400, 403, 404)


def test_the_page_asks_which_formats_this_pc_can_make(world):  # noqa: F811
    api, mgr, sid, tmp = world
    code, body = api.json("GET", "/api/export/formats?lang=it")
    ids = [f["id"] for f in body["formats"]]
    assert code == 200 and ids[:1] == ["docx"] and "odt" in ids and "html" in ids
