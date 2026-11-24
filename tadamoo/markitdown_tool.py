"""Files turned into Markdown by program, not by the model: Microsoft's MarkItDown (MIT), run with no plugins and no model.

A document the person attaches or the agent finds is read as Markdown (headings, lists, tables) at no cost in model tokens, and the same text comes out every
time. MarkItDown is optional: where it is not installed everything here answers "not available" and the callers use the readers they had before.

Text that comes out of a file is data, never instructions: the callers fence it like any other text read from a file."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional, Tuple

# what MarkItDown reads with the extras the requirements ask for (docx, pptx, xlsx, pdf); the others come with it
FORMATS = (".docx", ".pptx", ".xlsx", ".xls", ".pdf", ".html", ".htm", ".epub", ".csv", ".json", ".xml", ".zip", ".msg", ".ipynb", ".rtf")
MAX_BYTES = 120 << 20
MAGIC = {".docx": b"PK", ".pptx": b"PK", ".xlsx": b"PK", ".epub": b"PK", ".pdf": b"%PDF"}
_lock = threading.Lock()
_engine: Any = None
_missing = False


def _get() -> Any:
    global _engine, _missing
    if _missing:
        return None
    with _lock:
        if _engine is None:
            try:
                from markitdown import MarkItDown
                _engine = MarkItDown(enable_plugins=False)
            except Exception:                              # not installed, or a dependency of it is broken: the old readers stay
                _missing = True
                return None
        return _engine


def available() -> bool:
    return _get() is not None


def supports(name: Any) -> bool:
    return Path(str(name)).suffix.lower() in FORMATS


def pdf_text(path: Path) -> Tuple[Optional[str], str]:
    """(text, note) of a PDF from its text layer, page by page with `[page N]` markers (a page with no text says so: it is a scan, for OCR). pypdf keeps the
    spaces between the words that MarkItDown's PDF reader (pdfminer) drops in many PDFs. `unavailable` when pypdf is not installed."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "unavailable"
    import io
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            return None, "too_large"
        data = Path(path).read_bytes()
        if not data.startswith(MAGIC[".pdf"]):             # a damaged file would be read as plain text, which is not its content
            return None, "failed: not_pdf"
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            return None, "failed: encrypted"
        parts, with_text = [], 0
        for n, page in enumerate(reader.pages, 1):
            body = (page.extract_text() or "").replace("\r\n", "\n").strip()
            if body:
                with_text += 1
                parts.append(f"[page {n}]\n{body}\n")
            else:
                parts.append(f"[page {n}: no text layer]\n")
    except Exception as exc:                               # a damaged file: the caller falls back
        broken = type(exc).__name__ in ("PdfStreamError", "PdfReadError", "EmptyFileError")   # the signature was there, the body was not a PDF
        return None, "failed: not_pdf" if broken else f"failed: {type(exc).__name__}"
    return ("\n".join(parts), "") if with_text else (None, "empty")


def convert(path: Path, name: str = "") -> Tuple[Optional[str], str]:
    """(markdown, note): the file as Markdown, or None with why not (`unavailable`, `unsupported`, `too_large`, `empty`, or `failed: <kind>`).
    `name` is the file's own name when the stored copy has none (an attachment is kept as plain `file`): its extension tells MarkItDown what it is."""
    path = Path(path)
    ext = Path(name or path.name).suffix.lower()
    if ext == ".pdf":                                      # the text layer, with its spaces; a PDF with none is empty, for OCR
        text, note = pdf_text(path)
        if note != "unavailable":
            return text, note
    engine = _get()
    if engine is None:
        return None, "unavailable"
    if ext not in FORMATS:
        return None, "unsupported"
    try:
        from markitdown import StreamInfo
        if path.stat().st_size > MAX_BYTES:
            return None, "too_large"
        with path.open("rb") as stream:
            magic = MAGIC.get(ext)
            if magic and not stream.read(len(magic)).startswith(magic):   # a damaged file would be read as plain text, which is not its content
                return None, "failed: not_" + ext[1:]
            stream.seek(0)
            result = engine.convert_stream(stream, stream_info=StreamInfo(extension=ext, filename=Path(name or path.name).name))
    except Exception as exc:                               # a damaged file, an unsupported variant: the caller falls back
        return None, f"failed: {type(exc).__name__}"
    text = (getattr(result, "text_content", "") or "").replace("\r\n", "\n").strip()
    if ext == ".pdf" and text.startswith("%PDF"):          # the signature was there but the body was not a PDF: it came back as plain text
        return None, "failed: not_pdf"
    return (text + "\n", "") if text else (None, "empty")


def outline(markdown: str, limit: int = 40) -> str:
    """The headings of a Markdown text, one per line with their line numbers: what to look at before reading any of it."""
    rows = []
    in_code = False
    for n, line in enumerate(markdown.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            rows.append(f"{n}: {line.strip()[:100]}")
            if len(rows) >= limit:
                rows.append("...")
                break
    return "\n".join(rows)
