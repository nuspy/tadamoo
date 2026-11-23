"""A Markdown file turned into another document, with no model: the same readers and writers as the conversation export (serve/exportdoc.py,
serve/exportoffice.py), applied to the file's text. Word (.docx), OpenDocument (.odt) and a single-file web page (.html) are written here with the
standard library only; a PDF is the web page printed by a browser with no window (Edge, Chrome or Chromium, the same program the agent's own browser
uses), so it is offered only where one is installed. Where Pandoc is on the PATH, EPUB and RTF are offered too.

"Both" is one zip with the Markdown as it is and the converted file. Everything that comes in is untrusted text: it goes through the export readers,
which clean it and limit what links and images can be."""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from serve import exportdoc
from serve.exportdoc import Context, Doc, ExportError

MAX_BYTES = 8 << 20                       # the largest Markdown file converted
PRINT_SECONDS = 60
CONTENT_TYPES = {
    "docx": exportdoc.CONTENT_TYPES["docx"],
    "odt": exportdoc.CONTENT_TYPES["odt"],
    "html": exportdoc.CONTENT_TYPES["html"],
    "pdf": "application/pdf",
    "epub": "application/epub+zip",
    "rtf": "application/rtf",
}
LABELS = {
    "docx": {"en": "Word (.docx)", "it": "Word (.docx)"},
    "odt": {"en": "OpenDocument text (.odt)", "it": "Testo OpenDocument (.odt)"},
    "pdf": {"en": "PDF (.pdf)", "it": "PDF (.pdf)"},
    "html": {"en": "Web page (.html)", "it": "Pagina web (.html)"},
    "epub": {"en": "E-book (.epub)", "it": "E-book (.epub)"},
    "rtf": {"en": "Rich text (.rtf)", "it": "Rich text (.rtf)"},
}
ORDER = ("docx", "pdf", "odt", "html", "epub", "rtf")
MODES = ("converted", "both")


def _browser() -> Optional[str]:
    from tadamoo.browser import find_browser
    return find_browser()


def _pandoc() -> Optional[str]:
    return shutil.which("pandoc")


def formats(language: str = "en") -> List[Dict[str, str]]:
    """The formats a Markdown file can be turned into on this PC, in the order the person is offered them."""
    have = {"docx": True, "odt": True, "html": True, "pdf": _browser() is not None, "epub": _pandoc() is not None, "rtf": _pandoc() is not None}
    lang = language if language in ("it", "en") else "en"
    return [{"id": f, "label": LABELS[f][lang], "ext": f} for f in ORDER if have[f]]


def _doc(text: str, title: str, language: str) -> Doc:
    lang = language if language in exportdoc.LABELS else "en"
    labels = exportdoc.LABELS[lang]
    ctx = Context({}, labels)
    return Doc(title=title, lang=lang, labels=labels, blocks=exportdoc.parse_blocks(text, ctx), bare=True)


def _print_pdf(page: bytes) -> bytes:
    exe = _browser()
    if exe is None:
        raise ExportError("A PDF needs Edge, Chrome or Chromium on this PC.")
    with tempfile.TemporaryDirectory(prefix="mdpdf-") as tmp:
        src, out = Path(tmp) / "page.html", Path(tmp) / "page.pdf"
        src.write_bytes(page)
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--no-pdf-header-footer", f"--user-data-dir={Path(tmp) / 'profile'}",
               f"--print-to-pdf={out}", src.as_uri()]
        try:
            subprocess.run(cmd, check=False, timeout=PRINT_SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExportError(f"The browser could not print the PDF ({type(exc).__name__}).") from None
        data = out.read_bytes() if out.is_file() else b""
    if not data.startswith(b"%PDF"):
        raise ExportError("The browser did not make a PDF.")
    return data


def _pandoc_convert(text: str, fmt: str) -> bytes:
    exe = _pandoc()
    if exe is None:
        raise ExportError("Pandoc is not installed on this PC.")
    with tempfile.TemporaryDirectory(prefix="mdpandoc-") as tmp:
        out = Path(tmp) / f"out.{fmt}"
        try:
            subprocess.run([exe, "-f", "gfm", "-t", fmt, "-o", str(out), "--sandbox", "-"], input=text.encode("utf-8"), check=True, timeout=PRINT_SECONDS,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExportError(f"Pandoc could not make the {fmt} file ({type(exc).__name__}).") from None
        return out.read_bytes()


def convert(markdown: bytes, name: str, fmt: str, language: str = "en") -> Tuple[bytes, str, str]:
    """(bytes, content type, file name) of the Markdown `markdown` as `fmt`; the name is the original's with the new extension."""
    if fmt not in CONTENT_TYPES:
        raise ExportError("Choose a format: " + ", ".join(ORDER) + ".")
    if len(markdown) > MAX_BYTES:
        raise ExportError("That file is too large to convert.")
    text = markdown.decode("utf-8", errors="replace")
    stem = re.sub(r"\.(md|markdown|txt)$", "", exportdoc.clean(name), flags=re.I) or "document"
    doc = _doc(text, stem, language)
    if fmt == "html":
        data = exportdoc.write_html(doc)
    elif fmt == "pdf":
        data = _print_pdf(exportdoc.write_html(doc))
    elif fmt in ("docx", "odt"):
        from serve import exportoffice
        data = exportoffice.write_docx(doc) if fmt == "docx" else exportoffice.write_odt(doc)
    else:
        data = _pandoc_convert(text, fmt)
    return data, CONTENT_TYPES[fmt], exportdoc.file_name(stem, "md").rsplit(".", 1)[0] + "." + fmt


def bundle(markdown: bytes, name: str, fmt: str, language: str = "en") -> Tuple[bytes, str, str]:
    """One zip with the Markdown file as it is and the converted one."""
    data, _, converted = convert(markdown, name, fmt, language)
    md_name = exportdoc.file_name(re.sub(r"\.(md|markdown|txt)$", "", exportdoc.clean(name), flags=re.I) or "document", "md")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo(md_name, (2026, 1, 1, 0, 0, 0)), markdown)
        z.writestr(zipfile.ZipInfo(converted, (2026, 1, 1, 0, 0, 0)), data)
    return buf.getvalue(), "application/zip", converted.rsplit(".", 1)[0] + ".zip"


def prepare(markdown: bytes, name: str, mode: Any, fmt: Any, language: str = "en") -> Tuple[bytes, str, str]:
    """What a download asks for: `converted` (only the converted file) or `both` (the Markdown and the converted one, zipped)."""
    if mode not in MODES:
        raise ExportError("mode is converted or both.")
    return (convert if mode == "converted" else bundle)(markdown, name, str(fmt), language)
