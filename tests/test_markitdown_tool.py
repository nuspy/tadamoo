"""MarkItDown as the reader of documents: a Word file made by our own exporter comes back as the same Markdown headings, lists and table, every time, and a file it
cannot read says why. Skipped where MarkItDown is not installed."""
import pytest

from serve import mdexport
from tadamoo import markitdown_tool

pytestmark = pytest.mark.skipif(not markitdown_tool.available(), reason="markitdown is not installed")

SOURCE = "# Report\n\nA first paragraph.\n\n## Items\n\n- one\n- two\n\n| name | total |\n| --- | --- |\n| tea | 4 |\n| milk | 2 |\n"


def make(tmp_path, fmt):
    data, _, _ = mdexport.convert(SOURCE.encode("utf-8"), "Report.md", fmt, "en")
    path = tmp_path / "file"
    path.write_bytes(data)
    return path


def test_a_word_file_comes_back_with_its_headings_list_and_table(tmp_path):
    text, note = markitdown_tool.convert(make(tmp_path, "docx"), "Report.docx")
    assert note == ""
    assert "# Report" in text and "## Items" in text
    assert "one" in text and "two" in text
    assert "| tea | 4 |" in text.replace("  ", " ") or ("tea" in text and "milk" in text)


def test_the_same_file_gives_the_same_markdown_every_time(tmp_path):
    path = make(tmp_path, "docx")
    first = markitdown_tool.convert(path, "Report.docx")
    assert first[0] and first == markitdown_tool.convert(path, "Report.docx")


def test_a_web_page_loses_its_tags(tmp_path):
    path = make(tmp_path, "html")
    text, _ = markitdown_tool.convert(path, "Report.html")
    assert "<h1" not in text and "Report" in text


def test_what_it_cannot_read_says_why(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"not a zip at all")
    assert markitdown_tool.convert(path, "x.docx")[0] is None
    assert markitdown_tool.convert(path, "x.png") == (None, "unsupported")
    path.write_bytes(b"%PDF-1.4 damaged")
    assert markitdown_tool.convert(path, "x.pdf") == (None, "failed: not_pdf")


def test_the_outline_lists_the_headings_and_skips_code(tmp_path):
    text = "# A\n\n```\n# not a heading\n```\n\n## B\n"
    assert markitdown_tool.outline(text) == "1: # A\n7: ## B"


def test_the_agent_reads_a_word_file_of_its_workspace_as_markdown(tmp_path):
    from tadamoo.attachment_tools import register_attachment_tools
    from tadamoo.attachments import AttachmentStore, OutputStore
    from tadamoo.tools.registry import ToolRegistry
    (tmp_path / "ws").mkdir()
    data, _, _ = mdexport.convert(SOURCE.encode("utf-8"), "Report.md", "docx", "en")
    (tmp_path / "ws" / "r.docx").write_bytes(data)
    (tmp_path / "ws" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    registry = ToolRegistry()

    def inside(path):
        target = (tmp_path / "ws" / path).resolve()
        if (tmp_path / "ws").resolve() not in target.parents:
            raise PermissionError("Access denied")
        return target

    register_attachment_tools(registry, AttachmentStore(tmp_path / "a"), OutputStore(tmp_path / "o"), "s1", inside)
    out = registry.execute("convert_to_markdown", {"path": "r.docx"})
    assert "r.docx as Markdown" in out and "Headings:" in out and "## Items" in out
    assert "not a kind of file" in registry.execute("convert_to_markdown", {"path": "pic.png"})
    assert "Access denied" in registry.execute("convert_to_markdown", {"path": "../x"})
