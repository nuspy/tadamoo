"""The text of a document, by its format: plain text and Markdown, HTML, EPUB, Word (.docx) and PDF.

Word and PDF reading is `tadamoo.attachments`' (stdlib for Word; pypdf or PyMuPDF for PDF, whichever is installed, and neither is ever
installed from here). HTML and EPUB are read with the standard library. The format is the extension's, and for a PDF or a Word file the
first bytes must agree. What comes out is data: nothing in it is ever run or obeyed."""
from __future__ import annotations

import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import List
from xml.etree import ElementTree as ET

from tadamoo import attachments
from tadamoo.rag.errors import RagError

TEXT_EXT = (".txt", ".md", ".markdown", ".text")
HTML_EXT = (".html", ".htm")
FORMATS = TEXT_EXT + HTML_EXT + (".pdf", ".docx", ".epub")
MAX_UNPACKED = 60_000_000                    # a zip whose members add up to more than this is not read
_SKIP = frozenset(("script", "style", "noscript", "template", "svg"))
_BREAK = frozenset(("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre", "table", "ul", "ol", "dd", "dt"))
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def check_name(name: str) -> str:
    """The extension of `name` when it is a supported format; else the reason, naming the formats that are."""
    ext = Path(name).suffix.lower()
    if ext not in FORMATS:
        raise RagError(f"{ext or 'a file without an extension'} is not a supported format: use " + ", ".join(FORMATS))
    return ext


def extract(path: Path, name: str) -> str:
    """The cleaned text of the file at `path`, read as the format `name` says."""
    ext = check_name(name)
    with path.open("rb") as f:
        head = f.read(8192)
    try:
        if ext == ".pdf":
            if not head.startswith(b"%PDF-"):
                raise RagError("the file is not a PDF, whatever its name says")
            text = attachments.pdf_text(path)
            if text is None:
                raise RagError("PDF files need the package pypdf or PyMuPDF (fitz), and neither is installed here")
        elif ext == ".docx":
            if not head.startswith(b"PK\x03\x04"):
                raise RagError("the file is not a Word document, whatever its name says")
            _check_zip(path)
            text = attachments.docx_text(path)
        elif ext == ".epub":
            text = _epub_text(path)
        else:
            if b"\x00" in head:
                raise RagError("the file is not text")
            raw = path.read_bytes()
            text = _decode(raw)
            if ext in HTML_EXT:
                text = _html_text(text)
    except RagError:
        raise
    except Exception:                                      # a damaged, encrypted or odd file of a library's own kind
        raise RagError(f"the {ext[1:].upper()} file could not be read: it may be damaged or protected") from None
    text = clean(text)
    if len(text) < 20:
        raise RagError("the file holds no text to search" + (" (a scanned PDF has none: there is no text recognition here)" if ext == ".pdf" else ""))
    return text


def clean(text: str) -> str:
    """Line ends made plain, control characters dropped, runs of blank lines and spaces shortened."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" ?\n ?", "\n", text)).strip()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _check_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
            raise RagError("the archive unpacks to more than 60 MB: too large to read here")


def _epub_text(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        raise RagError("the file is not an EPUB, whatever its name says")
    out: List[str] = []
    with zipfile.ZipFile(path) as z:
        _check_zip(path)
        for member in sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))):
            text = _html_text(_decode(z.read(member)))
            if text.strip():
                out.append(text)
    if not out:
        raise RagError("the EPUB holds no pages of text")
    return "\n\n".join(out)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BREAK:
            self.parts.append("\n\n" if tag.startswith("h") or tag == "p" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BREAK:
            self.parts.append("\n\n" if tag.startswith("h") or tag == "p" else "\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def _html_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                                      # markup so broken the parser gives up: what it read so far is kept
        pass
    return "".join(parser.parts)
