"""Files the person attaches to a conversation: stored as they came, recognized by what they hold, read in pieces, shown to the model as data.

The original is never changed. What a file is is decided by its content (its first bytes), not by the name or the type the browser
declared. Text and data files are read directly; Word, Excel and PowerPoint files are Office Open XML archives and are read here
with the standard library alone; a PDF needs a library that may not be installed (then the manifest says so and the model asks the
person); images go to the model's vision through `analyze_image`; audio and video go to the callAPIcall service for a transcript.

What comes out of a file is **data, not an instruction**: it is fenced as untrusted text whatever the file says.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from harness.context_patterns import bm25_scores

NAME_CHARS = 120
FAMILIES = ("text", "data", "document", "image", "audio", "video", "pdf", "other")
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".ini", ".cfg", ".toml", ".yaml", ".yml", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".c",
            ".h", ".cpp", ".hpp", ".cu", ".java", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat", ".sql", ".tex", ".srt", ".vtt", ".diff", ".patch"}
DATA_EXT = {".csv", ".tsv", ".json", ".jsonl", ".ndjson"}
MAX_STORED = 2_000_000_000        # a file larger than this is refused at upload (the streaming limit of the server)
MAX_EXTRACT_BYTES = 60_000_000    # an archive larger than this is not unpacked in memory
INLINE_TOKEN_SHARE = 0.4          # at most this share of the context window is taken by documents placed in a message
CHARS_PER_TOKEN = 3.5
QUOTES_OVER_TOKENS = 20_000       # above this many tokens of documents the model is asked to quote before answering
MAX_ROWS_SHOWN = 20
CHUNK_LINES = 40


# -- what a file is ----------------------------------------------------------------------------------------------


def sniff(head: bytes, name: str, size: int, zip_names: Optional[List[str]] = None) -> Tuple[str, str]:
    """(family, kind) from the first bytes. The extension only tells text from data among files that are text."""
    ext = Path(name).suffix.lower()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "webp"
    if head[:2] == b"BM" and size > 54:
        return "image", "bmp"
    if head.startswith(b"%PDF-"):
        return "pdf", "pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio", "wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio", "mp3"
    if head[:4] in (b"OggS", b"fLaC") or head[:4] == b"FORM":
        return "audio", head[:4].decode("latin-1").strip().lower()
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return ("audio", "m4a") if brand in (b"M4A ", b"M4B ") else ("video", "mp4")
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video", "mkv"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "avi"
    if head[:4] == b"PK\x03\x04":
        names = zip_names or []
        for prefix, kind, family in (("word/", "docx", "document"), ("xl/", "xlsx", "data"), ("ppt/", "pptx", "document")):
            if any(n.startswith(prefix) for n in names):
                return family, kind
        return "other", "zip"
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.start < len(head) - 4:                  # a cut in the middle of a character at the end is not a failure
                return "other", "binary"
        if ext in DATA_EXT:
            return "data", ext[1:]
        return "text", ext[1:] if ext in TEXT_EXT else "txt"
    return "other", "binary"


def _zip_names(path: Path) -> List[str]:
    try:
        with zipfile.ZipFile(path) as z:
            return z.namelist()[:400]
    except (zipfile.BadZipFile, OSError):
        return []


def clean_name(name: Any) -> str:
    name = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", str(name or "").strip()).strip(". ")
    return (name or "file")[:NAME_CHARS]


# -- extracting text ----------------------------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _read_member(z: zipfile.ZipFile, name: str, limit: int = MAX_EXTRACT_BYTES) -> bytes:
    info = z.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"{name} is too large to read here")
    return z.read(name)


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(_read_member(z, "word/document.xml"))
    out: List[str] = []

    def para(p: ET.Element) -> str:
        return "".join((t.text or "") if t.tag == W + "t" else "\t" if t.tag == W + "tab" else "\n" if t.tag == W + "br" else "" for t in p.iter())

    body = root.find(W + "body")
    for child in (body if body is not None else []):
        if child.tag == W + "p":
            style = child.find(f"{W}pPr/{W}pStyle")
            text = para(child).strip()
            if text:
                level = re.match(r"Heading(\d)", (style.get(W + "val") or "") if style is not None else "")
                out.append(("#" * int(level.group(1)) + " " if level else "") + text)
        elif child.tag == W + "tbl":
            for tr in child.iter(W + "tr"):
                out.append(" | ".join(" ".join(para(p).strip() for p in tc.iter(W + "p")).strip() for tc in tr.iter(W + "tc")))
    return "\n".join(out)


def pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        out = []
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(_read_member(z, n))
            texts = ["".join(t.text or "" for t in p.iter(A + "t")).strip() for p in root.iter(A + "p")]
            out.append(f"## Slide {i}\n" + "\n".join(t for t in texts if t))
    return "\n\n".join(out)


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_sheets(path: Path, max_rows: int = 200_000) -> Dict[str, List[List[str]]]:
    """Every sheet as rows of text, in the workbook's order."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(_read_member(z, "xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(S + "t")) for si in root.iter(S + "si")]
        wb = ET.fromstring(_read_member(z, "xl/workbook.xml"))
        rels = ET.fromstring(_read_member(z, "xl/_rels/workbook.xml.rels")) if "xl/_rels/workbook.xml.rels" in names else None
        targets = {r.get("Id"): r.get("Target") for r in (rels if rels is not None else [])}
        sheets: Dict[str, List[List[str]]] = {}
        for i, sh in enumerate(wb.iter(S + "sheet"), 1):
            rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = targets.get(rid) or f"worksheets/sheet{i}.xml"
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if member not in names:
                continue
            rows: List[List[str]] = []
            for row in ET.fromstring(_read_member(z, member)).iter(S + "row"):
                cells: List[str] = []
                for c in row.iter(S + "c"):
                    idx = _col(c.get("r") or "A1")
                    while len(cells) <= idx:
                        cells.append("")
                    v = c.find(S + "v")
                    if c.get("t") == "s" and v is not None:
                        cells[idx] = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                    elif c.get("t") == "inlineStr":
                        cells[idx] = "".join(t.text or "" for t in c.iter(S + "t"))
                    else:
                        cells[idx] = (v.text or "") if v is not None else ""
                rows.append(cells)
                if len(rows) >= max_rows:
                    break
            sheets[sh.get("name") or f"Sheet{i}"] = rows
    return sheets


def _csv_text(sheets: Dict[str, List[List[str]]]) -> str:
    out = []
    for name, rows in sheets.items():
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        out.append(f"## Sheet: {name}\n{buf.getvalue().rstrip()}")
    return "\n\n".join(out)


def pdf_text(path: Path) -> Optional[str]:
    """The text of a PDF with pypdf or PyMuPDF, whichever is installed; None when neither is."""
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(str(path))
        return "\n\n".join(f"## Page {i}\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1))
    except ImportError:
        pass
    try:
        import fitz  # type: ignore
        with fitz.open(str(path)) as doc:
            return "\n\n".join(f"## Page {i}\n{page.get_text().strip()}" for i, page in enumerate(doc, 1))
    except ImportError:
        return None


# -- the store -------------------------------------------------------------------------------------------------------


class AttachmentStore:
    """`<directory>/<session>/<id>/`: the original as `file`, a card `meta.json`, and the text read from it as `text.txt` once extracted."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _folder(self, session: str, att_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or "") or not re.fullmatch(r"[a-f0-9]{12}", att_id or ""):
            raise KeyError(att_id)
        return self.dir / session / att_id

    def add(self, session: str, name: str, data_path: Path) -> Dict[str, Any]:
        """Takes a finished upload (already on disk) into the store; returns its card."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or ""):
            raise ValueError("bad conversation id")
        att_id = secrets.token_hex(6)
        folder = self._folder(session, att_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "file"
        Path(data_path).replace(target)
        size = target.stat().st_size
        with target.open("rb") as f:
            head = f.read(8192)
        sha = hashlib.sha256()
        with target.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                sha.update(block)
        family, kind = sniff(head, name, size, _zip_names(target) if head.startswith(b"PK\x03\x04") else None)
        meta = {"id": att_id, "session": session, "name": clean_name(name), "family": family, "kind": kind, "size": size, "sha256": sha.hexdigest(), "created": time.time()}
        self._write(folder, "meta.json", meta)
        return meta

    @staticmethod
    def _write(folder: Path, name: str, data: Any) -> None:
        tmp = folder / (name + ".tmp")
        tmp.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(folder / name)

    def get(self, session: str, att_id: str) -> Dict[str, Any]:
        try:
            return json.loads((self._folder(session, att_id) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError(att_id) from None

    def path(self, session: str, att_id: str) -> Path:
        self.get(session, att_id)
        return self._folder(session, att_id) / "file"

    def list(self, session: str) -> List[Dict[str, Any]]:
        out = []
        base = self.dir / session
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or "") and base.is_dir():
            for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime):
                try:
                    out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return out

    def remove(self, session: str, att_id: str) -> None:
        import shutil
        self.get(session, att_id)
        shutil.rmtree(self._folder(session, att_id), ignore_errors=True)

    # -- reading ------------------------------------------------------------------------------------------------

    def text(self, session: str, att_id: str) -> Tuple[Optional[str], str]:
        """(text, note): the text read from the file, or None with the reason it cannot be read now."""
        meta = self.get(session, att_id)
        folder = self._folder(session, att_id)
        cached = folder / "text.txt"
        if cached.exists():
            return cached.read_text(encoding="utf-8"), ""
        path, kind, family = folder / "file", meta["kind"], meta["family"]
        try:
            if family in ("text", "data") and kind != "xlsx":
                text = path.read_bytes().decode("utf-8", errors="replace")
            elif kind == "docx":
                text = docx_text(path)
            elif kind == "pptx":
                text = pptx_text(path)
            elif kind == "xlsx":
                text = _csv_text(xlsx_sheets(path))
            elif kind == "pdf":
                got = pdf_text(path)
                if got is None:
                    return None, "needs_pdf"
                text = got
                if len(text.strip()) < 20:
                    return None, "pdf_without_text"
            elif family == "image":
                return None, "image"
            elif family in ("audio", "video"):
                return None, "media"
            else:
                return None, "unreadable"
        except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError) as exc:
            return None, f"damaged: {type(exc).__name__}"
        self._write(folder, "text.txt", text)
        return text, ""


# -- showing a file to the model --------------------------------------------------------------------------------------


def _lines(text: str) -> List[str]:
    """The lines of a text; a final newline does not make an empty last line."""
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def _fence(source: str, text: str) -> str:
    text = text.replace("</untrusted_data>", "[/untrusted_data]").replace("</document_content>", "[/document_content]")
    return f'<untrusted_data source="attachment:{source}">\n{text}\n</untrusted_data>'


def data_summary(name: str, kind: str, text: str) -> str:
    """The schema and first rows of a table or JSON file, so the model sees its shape without all of it."""
    if kind in ("json", "jsonl", "ndjson"):
        try:
            data = json.loads(text) if kind == "json" else [json.loads(ln) for ln in text.splitlines()[:200] if ln.strip()]
        except ValueError:
            return "Looks like JSON but does not parse."
        if isinstance(data, list):
            keys = sorted({k for r in data[:200] if isinstance(r, dict) for k in r})
            return f"A list of {len(data)} items" + (f" with fields: {', '.join(keys[:40])}." if keys else ".")
        if isinstance(data, dict):
            return "An object with keys: " + ", ".join(list(map(str, data))[:40]) + "."
        return f"A JSON value of type {type(data).__name__}."
    delim = "\t" if kind == "tsv" else ","
    parts = re.split(r"^## Sheet: (.*)$", text, flags=re.M) if text.startswith("## Sheet: ") else [None, None, text]
    out = []
    for sheet, body in zip(parts[1::2], parts[2::2]) if len(parts) > 3 else [(None, parts[-1])]:
        rows = list(csv.reader(io.StringIO(body.strip()), delimiter=delim))
        if not rows:
            continue
        header, rest = rows[0], rows[1:]
        out.append((f"Sheet {sheet}: " if sheet else "") + f"{len(rest)} rows, columns: " + ", ".join(h or "(empty)" for h in header[:60]) +
                   ". First rows:\n" + "\n".join(" | ".join(r[:12]) for r in rest[:MAX_ROWS_SHOWN]))
    return "\n\n".join(out) or "Empty."


def manifest(meta: Dict[str, Any], text: Optional[str], note: str) -> str:
    """One line for the model: what the file is and how to read it, without its content."""
    base = f'{meta["name"]} ({meta["kind"]}, {meta["size"]} bytes, id {meta["id"]})'
    if text is not None:
        return f"{base}: {len(_lines(text))} lines. Read it with read_attachment, search it with search_attachment."
    if note == "needs_pdf":
        return f"{base}: cannot be read yet, no PDF reader is installed. Ask the person to approve a PDF skill or to install pypdf, then read it again."
    if note == "pdf_without_text":
        return f"{base}: has no text layer (a scan). It needs OCR: ask the person which skill to use."
    if note == "image":
        return f"{base}: an image. Look at it with analyze_image(path='attachment:{meta['id']}', question=...)."
    if note == "media":
        return f"{base}: {meta['family']}. Get its transcript with transcribe_attachment('{meta['id']}')."
    return f"{base}: cannot be read ({note})."


def compose_message(store: AttachmentStore, session: str, ids: List[str], question: str, window_tokens: int = 32768) -> Tuple[str, bool]:
    """The message the model gets: the documents first, each in its own block with where it came from and what part is shown, then the
    question. Returns (message, documents_were_inlined). Large files are shown by their manifest and read in pieces."""
    budget = int(window_tokens * INLINE_TOKEN_SHARE * CHARS_PER_TOKEN)
    docs: List[str] = []
    used, inlined = 0, False
    for n, att_id in enumerate(ids, 1):
        try:
            meta = store.get(session, att_id)
        except KeyError:
            docs.append(f'<document index="{n}"><source>{att_id}</source><note>This attachment is not there any more.</note></document>')
            continue
        text, note = store.text(session, att_id)
        source = meta["name"].replace("<", "").replace(">", "")
        if text is None:
            docs.append(f'<document index="{n}"><source>{source}</source><note>{manifest(meta, None, note)}</note></document>')
            continue
        lines = _lines(text)
        shape = f"<schema>{data_summary(meta['name'], meta['kind'], text)}</schema>" if meta["family"] == "data" and len(text) > 8000 else ""
        room = max(0, budget - used)
        if len(text) <= room and not shape:
            shown, label = text, "all"
        elif shape:
            shown, label = "", "none"
        else:
            limit = max(0, min(room, budget // 2))
            cut = text[:limit]
            if "\n" in cut and len(text) > limit:
                cut = cut[:cut.rindex("\n")]                       # whole lines when there are lines
            n = len(_lines(cut)) if cut else 0
            shown = cut
            label = f"lines 1-{n} of {len(lines)}" if len(lines) > 1 else f"characters 1-{len(cut)} of {len(text)}"
        used += len(shown)
        inlined = inlined or bool(shown)
        body = _fence(meta["name"], shown) if shown else ""
        docs.append(f'<document index="{n}"><source>{source}</source><type>{meta["kind"]}</type><shown>{label}</shown>{shape}'
                    f'<document_content>{body}</document_content><note>{manifest(meta, text, "")}</note></document>')
    msg = "<documents>\n" + "\n".join(docs) + "\n</documents>\n\n" + question.strip()
    if used > QUOTES_OVER_TOKENS * CHARS_PER_TOKEN:
        msg += "\n\nThe documents are long: first copy the passages that matter to this request into <quotes> tags, then answer from them."
    return msg, inlined


def split_documents(content: str) -> Tuple[str, List[str]]:
    """The inverse for the person's own view: (what they typed, the names of what they attached) from a composed message."""
    m = re.match(r"<documents>\n(.*?)\n</documents>\n\n", content, re.S)
    if not m:
        return content, []
    names = re.findall(r"<source>(.*?)</source>", m.group(1))
    rest = content[m.end():]
    rest = re.sub(r"\n\nThe documents are long: first copy the passages.*$", "", rest, flags=re.S)
    return rest, names


# -- reading and searching in pieces --------------------------------------------------------------------------------------


def read_range(text: str, start: int = 1, end: Optional[int] = None, max_chars: int = 12000) -> str:
    """Lines `start` to `end` (1-based), at most `max_chars` of them, with the line numbers it shows so the model can ask for the next part."""
    lines = _lines(text)
    start = max(1, int(start))
    stop = min(len(lines), int(end) if end else start + 199)
    out, size = [], 0
    for i in range(start, stop + 1):
        ln = lines[i - 1]
        if size + len(ln) + 1 > max_chars and out:
            stop = i - 1
            break
        if len(ln) > max_chars:                                   # one very long line is shown in part, and says so
            ln = ln[:max_chars] + f" [... {len(ln) - max_chars} more characters on this line]"
        out.append(f"{i}: {ln}")
        size += len(ln) + 1
    head = f"Lines {start}-{start + len(out) - 1} of {len(lines)}" + (f" (continue from line {start + len(out)})" if start + len(out) <= len(lines) else " (the end)") + "\n"
    return head + "\n".join(out) if out else f"The file has {len(lines)} lines."


def search_chunks(text: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
    lines = _lines(text)
    chunks = [(i + 1, min(len(lines), i + CHUNK_LINES), "\n".join(lines[i:i + CHUNK_LINES])) for i in range(0, len(lines), CHUNK_LINES - 8)]
    if not chunks:
        return []
    scores = bm25_scores(query, [c[2] for c in chunks])
    ranked = sorted(range(len(chunks)), key=lambda k: (-scores[k], k))
    return [{"from": chunks[k][0], "to": chunks[k][1], "score": round(scores[k], 3), "text": chunks[k][2][:1500]} for k in ranked[:limit] if scores[k] > 0]


# -- files the agent produces ----------------------------------------------------------------------------------------------


def verify_output(path: Path) -> Tuple[bool, str]:
    """Opens a file the agent wrote and checks it holds what its name says: the person is offered a file that opens, not a promise."""
    try:
        size = path.stat().st_size
        if size == 0:
            return False, "the file is empty"
        ext = path.suffix.lower()
        raw = path.read_bytes()
        if ext == ".json":
            json.loads(raw.decode("utf-8"))
        elif ext in (".csv", ".tsv"):
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t" if ext == ".tsv" else ","))
            widths = {len(r) for r in rows if r}
            if len(widths) > 1:
                return False, f"the rows do not all have the same number of columns ({sorted(widths)})"
        elif ext in (".docx", ".xlsx", ".pptx"):
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    return False, f"{bad} is damaged inside the archive"
                if ext == ".docx":
                    docx_text(path)
                elif ext == ".xlsx":
                    xlsx_sheets(path, max_rows=50)
                else:
                    pptx_text(path)
        elif ext == ".pdf":
            if not raw.startswith(b"%PDF-"):
                return False, "it does not start like a PDF"
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            from PIL import Image
            Image.open(io.BytesIO(raw)).verify()
        else:
            raw.decode("utf-8")
        return True, f"{size} bytes, opens correctly"
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError, UnicodeDecodeError) as exc:
        return False, f"it does not open as {path.suffix or 'a file'}: {type(exc).__name__}"
    except Exception as exc:                                  # an image library error, for one
        return False, f"it does not open as {path.suffix or 'a file'}: {type(exc).__name__}"


class OutputStore:
    """`<directory>/<session>/<id>/<name>`: files the agent made for the person, each with a card; the page offers them for saving."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _folder(self, session: str, out_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or "") or not re.fullmatch(r"[a-f0-9]{12}", out_id or ""):
            raise KeyError(out_id)
        return self.dir / session / out_id

    def save(self, session: str, name: str, data: bytes) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or ""):
            raise ValueError("bad conversation id")
        out_id = secrets.token_hex(6)
        folder = self._folder(session, out_id)
        folder.mkdir(parents=True, exist_ok=True)
        name = clean_name(name)
        (folder / name).write_bytes(data)
        ok, how = verify_output(folder / name)
        meta = {"id": out_id, "session": session, "name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "created": time.time(), "verified": ok, "check": how}
        AttachmentStore._write(folder, "meta.json", meta)
        return meta

    def get(self, session: str, out_id: str) -> Tuple[Dict[str, Any], Path]:
        folder = self._folder(session, out_id)
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError(out_id) from None
        return meta, folder / meta["name"]

    def list(self, session: str) -> List[Dict[str, Any]]:
        out = []
        base = self.dir / session
        if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session or "") and base.is_dir():
            for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime):
                try:
                    out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return out
