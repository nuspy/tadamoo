"""The readers that need nothing installed beyond what TaDaMoo already has: plain text and Markdown, HTML, EPUB, Word and, with pypdf or
PyMuPDF, PDF with a text layer. They give the same blocks as Docling, with a simple guess at headings. A scanned PDF, an image, an Excel file or a PowerPoint
file needs Docling and is refused here with the reason."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.rag.errors import RagError
from tadamoo.rag.extract import TEXT_EXT, check_name, clean, extract
from tadamoo.rag.ingest.blocks import block, markdown_to_blocks, page_break

BASIC_FORMATS = (".txt", ".md", ".markdown", ".text", ".html", ".htm", ".epub", ".docx", ".pdf")
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})[.)]?\s+\S")
_ENDS_SENTENCE = re.compile(r"[.,;:!?。]$")
MAX_HEADING_CHARS = 90
MAX_HEADING_SHARE = 0.3            # when more of the blocks than this look like headings, none of them is taken for one


def _heading_level(text: str) -> int:
    """The level a one-line paragraph would have as a heading, 0 when it does not look like one: numbered (`2.1 Method`) or in capitals."""
    if "\n" in text or len(text) > MAX_HEADING_CHARS or _ENDS_SENTENCE.search(text):
        return 0
    numbered = _NUMBERED.match(text)
    if numbered:
        return min(6, numbered.group(1).count(".") + 1)
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        return 1
    return 0


def guess_headings(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The one-line paragraphs that look like headings made into headings; none when too many would be."""
    found = [(i, _heading_level(b["text"])) for i, b in enumerate(blocks) if b.get("type") == "paragraph"]
    found = [(i, level) for i, level in found if level]
    if not found or len(found) > MAX_HEADING_SHARE * max(1, len(blocks)):
        return blocks
    out = [dict(b) for b in blocks]
    for i, level in found:
        out[i] = {**out[i], "type": "heading", "level": level}
    return out


def _pdf_pages(path: Path) -> List[str]:
    try:
        import pypdf  # type: ignore
        return [(p.extract_text() or "").strip() for p in pypdf.PdfReader(str(path)).pages]
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
    except ImportError:
        raise RagError("PDF files need Docling, or the package pypdf or PyMuPDF (fitz), and none is installed here") from None
    with fitz.open(str(path)) as doc:
        return [page.get_text().strip() for page in doc]


def _pdf_blocks(path: Path) -> List[Dict[str, Any]]:
    with path.open("rb") as f:
        if not f.read(8).startswith(b"%PDF-"):
            raise RagError("the file is not a PDF, whatever its name says")
    try:
        pages = _pdf_pages(path)
    except RagError:
        raise
    except Exception:
        raise RagError("the PDF file could not be read: it may be damaged or protected") from None
    out: List[Dict[str, Any]] = []
    for number, text in enumerate(pages, 1):
        out.append(page_break(number))
        for para in re.split(r"\n\s*\n", clean(text)):
            if para.strip():
                out.append(block("paragraph", para.strip(), number))
    if sum(len(b["text"]) for b in out) < 20:
        raise RagError("the PDF holds no text to search (a scanned PDF needs Docling to be read)")
    return out


def basic_blocks(path: Path, name: str) -> Dict[str, Any]:
    """`{blocks, pages}` for the file, read without Docling; RagError says why a file cannot be."""
    ext = check_name(name)
    if ext == ".pdf":
        blocks = guess_headings(_pdf_blocks(path))
    else:
        text = extract(path, name)
        blocks = markdown_to_blocks(text)
        if ext not in TEXT_EXT or not any(b["type"] == "heading" for b in blocks):
            blocks = guess_headings(blocks)
        if not any(b["type"] == "page_break" for b in blocks):               # a format without pages has no page numbers to cite
            blocks = [{**b, "page": 0} for b in blocks]
    return {"blocks": blocks, "pages": max((b.get("page") or 0 for b in blocks), default=0)}

