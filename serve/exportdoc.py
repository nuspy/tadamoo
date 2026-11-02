"""The conversation as a document: a small model (headings, paragraphs, lists, tables, images ...), the builder that fills it from the
page's snapshot and the person's selection, a reader for the Markdown of the answers, and the Markdown and HTML writers. The Word and
OpenDocument writers are in serve/exportoffice.py.

Everything that comes in is untrusted: model, tool and page text. Text is cleaned of characters XML cannot hold, link addresses are
limited to http, https and mailto (and the app's own file links, which become the address of the file on this server), and images are
embedded only when their bytes really are a PNG, JPEG or GIF.
"""
from __future__ import annotations

import base64
import binascii
import html as htmllib
import json
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

KINDS = {"prompts": "prompt", "reasoning": "reasoning", "actions": "action", "answers": "answer"}
MAX_ITEMS = 5000                    # items one export may hold
MAX_TEXT = 4_000_000                # characters of one text
MAX_IMAGE = 12 << 20                # bytes of one embedded image
MAX_IMAGES = 200
MAX_DEPTH = 8                       # nesting of lists and quotes read from Markdown
MAX_ARGS = 20000                    # characters of an action's arguments

LABELS: Dict[str, Dict[str, str]] = {
    "en": {
        "you": "You", "assistant": "Assistant", "reasoning": "Reasoning", "action": "Action", "arguments": "Arguments",
        "result": "Result", "failed": "failed", "sources": "Sources", "documents": "Documents", "attached": "Attached",
        "diagram": "Diagram", "diagram_source": "Diagram source", "exported": "Exported on {0}", "contents": "Contents",
        "k_prompt": "prompts", "k_reasoning": "reasoning", "k_action": "actions", "k_answer": "answers", "no_address": "(no network address)",
        "untitled": "Conversation", "print_hint": "In the print window choose \u201cSave as PDF\u201d as the printer.",
        "no_image": "image not included", "created_by": "Exported from Basalt",
    },
    "it": {
        "you": "Tu", "assistant": "Assistente", "reasoning": "Ragionamento", "action": "Azione", "arguments": "Argomenti",
        "result": "Risultato", "failed": "non riuscita", "sources": "Fonti", "documents": "Documenti", "attached": "Allegati",
        "diagram": "Diagramma", "diagram_source": "Sorgente del diagramma", "exported": "Esportata il {0}", "contents": "Contenuto",
        "k_prompt": "richieste", "k_reasoning": "ragionamento", "k_action": "azioni", "k_answer": "risposte", "no_address": "(senza indirizzo di rete)",
        "untitled": "Conversazione", "print_hint": "Nella finestra di stampa scegli \u00abSalva come PDF\u00bb come stampante.",
        "no_image": "immagine non inclusa", "created_by": "Esportata da Basalt",
    },
}


class ExportError(ValueError):
    """The request cannot be turned into a document; the message is meant for the person."""


# ---------------------------------------------------------------- the model

@dataclass
class Image:
    data: bytes
    mime: str
    width: int
    height: int
    alt: str = ""


@dataclass
class Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: Optional[str] = None
    br: bool = False
    image: Optional[Image] = None


@dataclass
class Heading:
    level: int
    runs: List[Run]


@dataclass
class Paragraph:
    runs: List[Run]


@dataclass
class Label:
    """A small bold line that names what follows (Reasoning, Action ...)."""
    runs: List[Run]


@dataclass
class Meta:
    """A small grey line (the date of the export, the time of a message)."""
    runs: List[Run]


@dataclass
class Code:
    text: str
    lang: str = ""


@dataclass
class Quote:
    blocks: List[Any]


@dataclass
class Lst:
    ordered: bool
    items: List[List[Any]]


@dataclass
class Table:
    header: List[List[Run]]
    rows: List[List[List[Run]]]


@dataclass
class Rule:
    pass


@dataclass
class Diagram:
    source: str
    svg: str = ""
    png: Optional[Image] = None


@dataclass
class Doc:
    title: str
    lang: str
    labels: Dict[str, str]
    blocks: List[Any] = field(default_factory=list)
    bare: bool = False                  # one message as it is: no title line, no export header, no speaker heading


# ---------------------------------------------------------------- cleaning

_BAD_XML = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def clean(text: Any) -> str:
    """Text as a string, without the characters XML 1.0 and HTML cannot hold (controls, lone surrogates, U+FFFE and U+FFFF) and with
    every line ending as a newline."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return _BAD_XML.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def one_line(text: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", clean(text)).strip()[:limit]


def safe_url(url: Any) -> Optional[str]:
    """An address a link may have: http, https or mailto, without spaces, controls or credentials; else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000 or re.search(r"[\x00-\x20\x7f-\x9f\u2028\u2029]", url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if not parts.netloc or parts.username or parts.password or not parts.hostname:
            return None
    elif scheme == "mailto":
        if not parts.path:
            return None
    else:
        return None
    return url.replace("<", "%3C").replace(">", "%3E").replace('"', "%22")


def sniff_image(data: bytes) -> Optional[Tuple[str, int, int]]:
    """(mime, width, height) of a PNG, JPEG or GIF read from its own bytes; None for anything else."""
    w = h = 0
    mime = ""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        mime = "image/png"
        w, h = struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        mime = "image/gif"
        w, h = struct.unpack("<HH", data[6:10])
    elif data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1 if marker == 0xFF else 2
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                mime = "image/jpeg"
                break
            i += 2 + length
    if not mime or not (0 < w <= 30000 and 0 < h <= 30000) or w * h > 150_000_000:
        return None
    return mime, w, h


def decode_data_url(url: Any, alt: str = "") -> Optional[Image]:
    """An image from a data: address, only when its bytes are a PNG, JPEG or GIF."""
    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url[:100]:
        return None
    try:
        data = base64.b64decode(url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_IMAGE:
        return None
    seen = sniff_image(data)
    if not seen:
        return None
    return Image(data, seen[0], seen[1], seen[2], one_line(alt, 300))


def clean_svg(svg: Any) -> str:
    """A diagram's SVG when it is a plain SVG document (no DOCTYPE, so no entities); else an empty string. It is only ever shown
    through an <img> element, where nothing in it runs."""
    if not isinstance(svg, str) or not svg or len(svg) > 2_000_000:
        return ""
    svg = clean(svg).strip()
    if re.search(r"<!(DOCTYPE|ENTITY)", svg, re.I) or not re.match(r"(<\?xml[^>]*\?>\s*)?<svg[\s>]", svg):
        return ""
    return svg


# ---------------------------------------------------------------- Markdown -> blocks

class Context:
    """What the reader of Markdown needs from the snapshot: the server's address, the conversation, the pictures and the diagrams."""

    def __init__(self, snapshot: Dict[str, Any], labels: Dict[str, str]) -> None:
        base = snapshot.get("base")
        self.base = ""
        if isinstance(base, str) and safe_url(base) and base.startswith(("http://", "https://")):
            self.base = base if base.endswith("/") else base + "/"
        session = snapshot.get("session")
        self.session = session if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session) else ""
        self.labels = labels
        self.images: Dict[str, Image] = {}
        assets = snapshot.get("assets")
        if isinstance(assets, dict):
            for key, value in list(assets.items())[:MAX_IMAGES]:
                img = decode_data_url(value)
                if isinstance(key, str) and img:
                    self.images[key] = img
        self.diagrams: Dict[str, Dict[str, Any]] = {}
        diagrams = snapshot.get("diagrams")
        if isinstance(diagrams, dict):
            for src, value in list(diagrams.items())[:MAX_IMAGES]:
                if isinstance(src, str) and isinstance(value, dict):
                    png = decode_data_url(value.get("png"), "")
                    self.diagrams[clean(src)] = {"svg": clean_svg(value.get("svg")), "png": png}

    def file_url(self, ident: str) -> Optional[str]:
        if not (self.base and self.session and re.fullmatch(r"[a-f0-9]{12}", ident)):
            return None
        return f"{self.base}api/outputs/{quote(self.session, safe='')}/{ident}"

    def path_url(self, path: str) -> Optional[str]:
        if not (self.base and self.session and path and len(path) <= 400 and "\x00" not in path):
            return None
        return f"{self.base}api/agent/sessions/{quote(self.session, safe='')}/file?path={quote(path, safe='')}"

    def resolve(self, dest: str) -> Tuple[Optional[str], bool]:
        """(address, is_app_file): the address a link may have in the document, or None."""
        dest = dest.strip()
        m = re.fullmatch(r"basalt-file:([a-f0-9]{12})", dest)
        if m:
            return self.file_url(m.group(1)), True
        m = re.fullmatch(r"basalt-path:([^\s<>]{1,400})", dest)
        if m:
            return self.path_url(m.group(1)), True
        return safe_url(dest), False


_ESCAPABLE = set("\\`*_{}[]()#+-.!|<>~\"'&")
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")


def _bare_url(text: str, i: int) -> Tuple[str, int]:
    m = _URL_RE.match(text, i)
    if not m:
        return "", i
    url = m.group(0)
    while url and (url[-1] in ".,;:!?*_]" or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url, i + len(url)


def _link_dest(text: str, i: int) -> Tuple[str, int]:
    """The destination of a link whose "(" is at `i`: (destination, index after the closing ")"), or ("", -1)."""
    depth, j = 1, i + 1
    while j < len(text) and depth:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "\n":
            return "", -1
        j += 1
    if depth:
        return "", -1
    inner = text[i + 1:j - 1].strip()
    if inner.startswith("<") and ">" in inner:
        return inner[1:inner.index(">")], j
    return (inner.split(None, 1) or [""])[0], j


def parse_inline(text: str, ctx: Context, bold: bool = False, italic: bool = False, href: Optional[str] = None, depth: int = 0) -> List[Run]:
    out: List[Run] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            out.append(Run("".join(buf), bold=bold, italic=italic, href=href))
            buf.clear()

    n, i = len(text), 0
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            buf.append(text[i + 1])
            i += 2
            continue
        if c == "\n":
            flush()
            out.append(Run(br=True))
            i += 1
            continue
        if c == "`":
            k = 1
            while i + k < n and text[i + k] == "`":
                k += 1
            end = text.find("`" * k, i + k)
            while end != -1 and end + k < n and text[end + k] == "`":
                end = text.find("`" * k, end + k + 1)
            if end != -1:
                flush()
                code = text[i + k:end].replace("\n", " ")
                if len(code) > 1 and code[0] == " " and code[-1] == " " and code.strip():
                    code = code[1:-1]
                out.append(Run(code, bold=bold, italic=italic, code=True, href=href))
                i = end + k
                continue
            buf.append("`" * k)
            i += k
            continue
        if depth < MAX_DEPTH and (c == "[" or text.startswith("![", i)):
            is_image = c == "!"
            start = i + 1 if is_image else i
            close = _matching_bracket(text, start)
            if close != -1 and close + 1 < n and text[close + 1] == "(":
                dest, after = _link_dest(text, close + 1)
                if after != -1:
                    flush()
                    out.extend(_link_runs(text[start + 1:close], dest, is_image, ctx, bold, italic, href, depth))
                    i = after
                    continue
        if depth < MAX_DEPTH and c in "*_":
            marker = next((m for m in ("***", "___", "**", "__") if text.startswith(m, i)), c)
            inner_start = i + len(marker)
            if inner_start < n and not text[inner_start].isspace() and (c == "*" or i == 0 or not text[i - 1].isalnum()):
                end = _closing(text, marker, inner_start)
                if end != -1:
                    flush()
                    out.extend(parse_inline(text[inner_start:end], ctx, bold or len(marker) >= 2, italic or len(marker) != 2, href, depth + 1))
                    i = end + len(marker)
                    continue
        if c == "<" and re.match(r"<(https?://[^\s<>]+|mailto:[^\s<>]+)>", text[i:]):
            m = re.match(r"<([^\s<>]+)>", text[i:])
            flush()
            url = safe_url(m.group(1))
            out.append(Run(m.group(1), bold=bold, italic=italic, href=url or href))
            i += len(m.group(0))
            continue
        if c == "h" and href is None and text.startswith(("http://", "https://"), i):
            url, after = _bare_url(text, i)
            safe = safe_url(url)
            if safe:
                flush()
                out.append(Run(url, bold=bold, italic=italic, href=safe))
                i = after
                continue
        buf.append(c)
        i += 1
    flush()
    return out


def _matching_bracket(text: str, i: int) -> int:
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            end = text.find("`", j + 1)
            j = end + 1 if end != -1 else j + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _closing(text: str, marker: str, start: int) -> int:
    """The index of the marker that closes an emphasis opened just before `start`, or -1."""
    j = start
    while True:
        j = text.find(marker, j)
        if j == -1:
            return -1
        after = j + len(marker)
        if text[j - 1] == "\\" or j == start or text[j - 1].isspace():
            j += 1
            continue
        if marker[0] == "_" and after < len(text) and text[after].isalnum():
            j += 1
            continue
        if len(marker) == 1 and after < len(text) and text[after] == marker:
            j += 2
            continue
        return j


def _link_runs(label: str, dest: str, is_image: bool, ctx: Context, bold: bool, italic: bool, href: Optional[str], depth: int) -> List[Run]:
    key = dest.strip()
    if is_image:
        alt = one_line(label, 300)
        img = ctx.images.get(key) or (decode_data_url(key, alt) if key.startswith("data:") else None)
        if img:
            return [Run(image=Image(img.data, img.mime, img.width, img.height, alt), bold=bold, italic=italic)]
        url, _ = ctx.resolve(key)
        note = ctx.labels["no_image"]
        if url:
            return [Run(f"{alt or note}", bold=bold, italic=italic, href=url)]
        return [Run(f"[{alt}: {note}]" if alt else f"[{note}]", bold=bold, italic=italic)]
    url, app_file = ctx.resolve(key)
    inner = parse_inline(label, ctx, bold, italic, url or href, depth + 1)
    if url:
        return inner
    if app_file:
        inner.append(Run(" " + ctx.labels["no_address"], bold=bold, italic=italic))
    return inner


_FENCE = re.compile(r"^ {0,3}```([^`\n]*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_RULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_ITEM = re.compile(r"^( {0,6})([-*+]|\d{1,9}[.)])([ \t]+)(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:-]+\|[\s|:-]*$")


def _cells(row: str) -> List[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def parse_blocks(text: str, ctx: Context, depth: int = 0, shift: int = 0) -> List[Any]:
    """Markdown (the safe subset the chat shows) as blocks. `shift` pushes headings down so they sit under the message's own heading."""
    lines = clean(text).split("\n")
    out: List[Any] = []
    para: List[str] = []
    i = 0

    def flush() -> None:
        if para:
            out.append(Paragraph(parse_inline("\n".join(para), ctx)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            flush()
            lang = m.group(1).strip()
            body: List[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^ {0,3}```[ \t]*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body)
            if lang.lower() == "mermaid":
                seen = ctx.diagrams.get(code) or ctx.diagrams.get(code.strip()) or {}
                out.append(Diagram(code, seen.get("svg", ""), seen.get("png")))
            else:
                out.append(Code(code, re.sub(r"[^A-Za-z0-9_+.#-]", "", lang.split()[0] if lang.split() else "")[:30]))
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            out.append(Heading(min(6, len(m.group(1)) + shift), parse_inline(m.group(2), ctx)))
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append(Rule())
            i += 1
            continue
        if line.lstrip().startswith(">") and depth < MAX_DEPTH:
            flush()
            quoted: List[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*> ?", "", lines[i]))
                i += 1
            out.append(Quote(parse_blocks("\n".join(quoted), ctx, depth + 1, shift)))
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            header = [parse_inline(c, ctx) for c in _cells(line)]
            rows: List[List[List[Run]]] = []
            i += 2
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                cells = [parse_inline(c, ctx) for c in _cells(lines[i])]
                rows.append((cells + [[] for _ in header])[:len(header)])
                i += 1
            out.append(Table(header, rows))
            continue
        m = _ITEM.match(line)
        if m and depth < MAX_DEPTH:
            flush()
            block, i = _parse_list(lines, i, ctx, depth, shift)
            out.append(block)
            continue
        para.append(line.strip())
        i += 1
    flush()
    return out


def _parse_list(lines: List[str], i: int, ctx: Context, depth: int, shift: int) -> Tuple[Lst, int]:
    first = _ITEM.match(lines[i])
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items: List[List[Any]] = []
    while i < len(lines):
        m = _ITEM.match(lines[i])
        if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
            break
        content_at = len(m.group(1)) + len(m.group(2)) + len(m.group(3))
        body = [m.group(4)]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and (len(lines[j]) - len(lines[j].lstrip())) > indent and not _is_sibling(lines[j], indent, ordered):
                    body.extend([""] * (j - i))
                    i = j
                    continue
                break
            lead = len(nxt) - len(nxt.lstrip())
            if lead > indent:
                body.append(nxt[min(content_at, lead):])
                i += 1
                continue
            if lead <= indent and (_ITEM.match(nxt) or _HEADING.match(nxt) or _FENCE.match(nxt) or nxt.lstrip().startswith(">") or _RULE.match(nxt)):
                break
            if lead <= indent and not body[-1].strip():
                break
            body.append(nxt.strip())                      # a line that goes on with the item's text
            i += 1
        items.append(parse_blocks("\n".join(body), ctx, depth + 1, shift))
    return Lst(ordered, items), i


def _is_sibling(line: str, indent: int, ordered: bool) -> bool:
    m = _ITEM.match(line)
    return bool(m) and len(m.group(1)) == indent and m.group(2)[0].isdigit() == ordered


def plain_runs(text: str) -> List[Run]:
    """Text exactly as written (the person's message, the model's reasoning): lines kept, nothing read as Markdown."""
    runs: List[Run] = []
    for k, line in enumerate(clean(text).split("\n")):
        if k:
            runs.append(Run(br=True))
        if line:
            runs.append(Run(line))
    return runs


def plain_blocks(text: str) -> List[Any]:
    """Paragraphs of plain text, one for each run of lines between blank lines."""
    blocks: List[Any] = []
    for chunk in re.split(r"\n\s*\n", clean(text).strip()):
        if chunk.strip():
            blocks.append(Paragraph(plain_runs(chunk.strip("\n"))))
    return blocks


# ---------------------------------------------------------------- the snapshot -> a document

def _selection(select: Any) -> Dict[str, bool]:
    if not isinstance(select, dict):
        raise ExportError("Choose what to export.")
    chosen = {kind: select.get(name) is True for name, kind in KINDS.items()}
    if not any(chosen.values()):
        raise ExportError("Choose at least one thing to export.")
    return chosen


def _when(item: Dict[str, Any], tz: Any) -> str:
    t = item.get("time")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        return ""
    offset = tz if isinstance(tz, (int, float)) and not isinstance(tz, bool) and -1000 <= tz <= 1000 else 0
    try:
        return datetime.fromtimestamp(t - offset * 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _file_links(result: str) -> List[Tuple[str, str]]:
    m = re.match(r"\[file:([a-f0-9]{12}):([^\]]{1,120})\]", result)
    return [(m.group(1), one_line(m.group(2), 120))] if m else []


def _sources_of(item: Dict[str, Any]) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    sources = item.get("sources")
    for s in sources[:30] if isinstance(sources, list) else []:
        raw = s.get("url") if isinstance(s, dict) else s
        url = safe_url(raw)
        if url and url.startswith(("http://", "https://")):
            title = one_line(s.get("title") or s.get("host") or "", 200) if isinstance(s, dict) else ""
            found.append((url, title))
    return found


def _args_block(args: Any) -> Optional[Code]:
    if args in (None, "", {}, []):
        return None
    if isinstance(args, str):
        return Code(clean(args)[:MAX_ARGS])
    if isinstance(args, dict) and len(args) == 1 and isinstance(args.get("command"), str):
        return Code(clean(args["command"])[:MAX_ARGS], "sh")
    try:
        text = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(args)
    return Code(clean(text)[:MAX_ARGS], "json")


def _description(item: Dict[str, Any]) -> str:
    for key in ("description", "desc"):
        if isinstance(item.get(key), str) and item[key].strip():
            return one_line(item[key], 600)
    args = item.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("description"), str):
        return one_line(args["description"], 600)
    return ""


def build(snapshot: Any, select: Any, bare: bool = False) -> Doc:
    """The document for a snapshot and a selection. Only the selected kinds appear; the sources and files an answer's actions found are
    listed after it even when the actions themselves were not chosen. `bare`: the text of one message alone, as written."""
    chosen = _selection(select)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
        raise ExportError("There is no conversation to export.")
    lang = snapshot.get("lang") if snapshot.get("lang") in LABELS else "en"
    labels = LABELS[lang]
    ctx = Context(snapshot, labels)
    items = [x for x in snapshot["items"][:MAX_ITEMS] if isinstance(x, dict) and x.get("kind") in KINDS.values()]
    tz = snapshot.get("tz")
    doc = Doc(one_line(snapshot.get("title"), 200) or labels["untitled"], lang, labels, bare=bare)

    stamp = _when({"time": snapshot.get("exported_at")}, tz)
    kinds = [labels["k_" + k] for k in ("prompt", "reasoning", "action", "answer") if chosen[k]]
    if not bare:
        doc.blocks.append(Meta([Run((labels["exported"].format(stamp) + " · " if stamp else "") + labels["contents"] + ": " + ", ".join(kinds))]))

    # an answer is one assistant item (its `turn`); a message of the person starts a new group
    keys: List[Any] = []
    epoch = 0
    for it in items:
        if it["kind"] == "prompt":
            epoch += 1
            keys.append(None)
        else:
            keys.append((epoch, it.get("turn")))
    found: Dict[Any, Dict[str, list]] = {}
    for key, it in zip(keys, items):
        if it["kind"] == "action":
            slot = found.setdefault(key, {"sources": [], "files": []})
            slot["sources"].extend(_sources_of(it))
            slot["files"].extend(_file_links(clean(it.get("result"))))

    def close(key: Any) -> None:
        slot = found.pop(key, None) if key is not None else None
        if not slot or not chosen["answer"]:
            return
        seen: set = set()
        sources = [(u, t) for u, t in slot["sources"] if not (u in seen or seen.add(u))]
        if sources:
            doc.blocks.append(Label([Run(labels["sources"], bold=True)]))
            doc.blocks.append(Lst(False, [[Paragraph([Run(t or u, href=u)] + ([Run(" – " + u)] if t else []))] for u, t in sources]))
        docs = []
        for ident, name in slot["files"]:
            url = ctx.file_url(ident)
            docs.append([Paragraph([Run(name, href=url)] if url else [Run(name + " " + labels["no_address"])])])
        if docs:
            doc.blocks.append(Label([Run(labels["documents"], bold=True)]))
            doc.blocks.append(Lst(False, docs))

    images_left = MAX_IMAGES
    current: Any = None
    headed = False
    for key, it in zip(keys, items):
        kind = it["kind"]
        if key != current:
            close(current)
            current, headed = key, False
        if not chosen[kind]:
            continue
        when = _when(it, tz)
        if kind == "prompt":
            doc.blocks.append(Heading(2, [Run(labels["you"] + (f" · {when}" if when else ""))]))
            doc.blocks.extend(plain_blocks(it.get("text") or ""))
            names = [one_line(n, 200) for n in (it.get("attachments") if isinstance(it.get("attachments"), list) else [])[:50] if isinstance(n, str)]
            if any(names):
                doc.blocks.append(Meta([Run(labels["attached"] + ": " + ", ".join(n for n in names if n))]))
            for im in (it.get("images") if isinstance(it.get("images"), list) else [])[:20]:
                img = decode_data_url(im.get("url"), im.get("name", "")) if isinstance(im, dict) else None
                if img and images_left > 0:
                    images_left -= 1
                    doc.blocks.append(Paragraph([Run(image=img)]))
            continue
        if not headed:
            if not bare:
                doc.blocks.append(Heading(2, [Run(labels["assistant"] + (f" · {when}" if when else ""))]))
            headed = True
        if kind == "reasoning":
            doc.blocks.append(Label([Run(labels["reasoning"], bold=True)]))
            doc.blocks.append(Quote(plain_blocks(it.get("text") or "")))
        elif kind == "action":
            doc.blocks.append(Label([Run(labels["action"] + ": ", bold=True), Run(one_line(it.get("name"), 120) or "tool", bold=True, code=True)]))
            desc = _description(it)
            if desc:
                doc.blocks.append(Paragraph([Run(desc)]))
            args = _args_block(it.get("arguments"))
            if args:
                doc.blocks.append(Meta([Run(labels["arguments"])]))
                doc.blocks.append(args)
            result = clean(it.get("result"))[:MAX_ARGS]
            if result.strip():
                doc.blocks.append(Meta([Run(labels["result"] + (f" ({labels['failed']})" if it.get("status") == "failed" else ""))]))
                doc.blocks.append(Code(result))
        else:
            doc.blocks.extend(parse_blocks(clean(it.get("text"))[:MAX_TEXT], ctx, shift=0 if bare else 2))
    close(current)
    return doc


# ---------------------------------------------------------------- Markdown writer

def md_escape(text: str) -> str:
    text = re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", text)
    return re.sub(r"&(?=#?\w+;)", "&amp;", text)


def md_dest(url: str) -> str:
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _md_run(r: Run) -> str:
    if r.br:
        return "\\\n"
    if r.image:
        alt = md_escape(r.image.alt).replace("\n", " ")
        return f"![{alt}](data:{r.image.mime};base64,{base64.b64encode(r.image.data).decode()})"
    text = r.text
    if r.code:
        ticks = max([len(m) for m in re.findall(r"`+", text)] + [0]) + 1
        body = "`" * ticks + (" " + text + " " if text.startswith("`") or text.endswith("`") else text) + "`" * ticks
    else:
        body = md_escape(text)
    core = body.strip()
    if not core:
        return body
    lead = body[:len(body) - len(body.lstrip())]
    trail = body[len(body.rstrip()):]
    if r.bold and r.italic:
        core = f"***{core}***"
    elif r.bold:
        core = f"**{core}**"
    elif r.italic:
        core = f"*{core}*"
    if r.href:
        core = f"[{core}]({md_dest(r.href)})"
    return lead + core + trail


def md_inline(runs: List[Run]) -> str:
    return "".join(_md_run(r) for r in runs)


_LINE_START = re.compile(r"^(\s*)(?:([#+\-])|(\d+)([.)]))(?=\s|$)")


def _md_paragraph(runs: List[Run]) -> str:
    """A paragraph whose lines cannot be mistaken for a heading, a list item or a numbered item."""
    def safe(line: str) -> str:
        m = _LINE_START.match(line)
        if not m:
            return line
        if m.group(2):
            return m.group(1) + "\\" + m.group(2) + line[m.end():]
        return m.group(1) + m.group(3) + "\\" + m.group(4) + line[m.end():]
    return "\n".join(safe(ln) for ln in md_inline(runs).strip("\n").split("\n"))


def _md_blocks(blocks: List[Any], ctx_labels: Dict[str, str]) -> str:
    parts = [p for p in (_md_block(b, ctx_labels) for b in blocks) if p]
    return "\n\n".join(parts)


def _md_block(b: Any, labels: Dict[str, str]) -> str:
    if isinstance(b, Heading):
        return "#" * b.level + " " + md_inline(b.runs).replace("\n", " ").strip()
    if isinstance(b, Paragraph):
        return _md_paragraph(b.runs)
    if isinstance(b, Label):
        return _md_paragraph(b.runs)
    if isinstance(b, Meta):
        return "*" + md_inline(b.runs).strip() + "*"
    if isinstance(b, Code):
        fence = "`" * max(3, max([len(m) for m in re.findall(r"`+", b.text)] + [0]) + 1)
        return f"{fence}{b.lang}\n{b.text}\n{fence}"
    if isinstance(b, Diagram):
        return f"```mermaid\n{b.source}\n```"
    if isinstance(b, Rule):
        return "---"
    if isinstance(b, Quote):
        inner = _md_blocks(b.blocks, labels)
        return "\n".join(("> " + ln) if ln else ">" for ln in inner.split("\n"))
    if isinstance(b, Lst):
        out = []
        for n, item in enumerate(b.items, 1):
            marker = f"{n}. " if b.ordered else "- "
            tight = all(isinstance(x, (Paragraph, Lst)) for x in item)
            inner = ("\n" if tight else "\n\n").join(p for p in (_md_block(x, labels) for x in item) if p)
            lines = inner.split("\n") or [""]
            out.append(marker + lines[0] + "".join("\n" + (" " * len(marker) + ln if ln else "") for ln in lines[1:]))
        return "\n".join(out)
    if isinstance(b, Table):
        def cell(runs: List[Run]) -> str:
            return md_inline(runs).replace("|", "\\|").replace("\\\n", "<br>").replace("\n", "<br>").strip()
        head = [cell(c) for c in b.header]
        lines = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
        lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in b.rows]
        return "\n".join(lines)
    return ""


def write_md(doc: Doc) -> bytes:
    title = "" if doc.bare else "# " + md_inline([Run(doc.title)]).replace("\n", " ") + "\n\n"
    text = title + _md_blocks(doc.blocks, doc.labels) + "\n"
    return text.encode("utf-8")


# ---------------------------------------------------------------- HTML writer

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 52rem; padding: 1.5rem 1rem 3rem; font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1b1f24; background: #fff; overflow-wrap: anywhere; }
h1 { font-size: 1.8rem; line-height: 1.25; margin: 0 0 .4rem; }
h2 { font-size: 1.25rem; margin: 2rem 0 .5rem; padding-bottom: .25rem; border-bottom: 1px solid #d0d7de; }
h3, h4, h5, h6 { margin: 1.4rem 0 .4rem; line-height: 1.3; }
p { margin: .6rem 0; }
a { color: #0b57d0; }
.meta { color: #57606a; font-size: .875rem; margin: .3rem 0; }
.label { font-weight: 700; margin: 1rem 0 .25rem; }
code { font: .9em ui-monospace, SFMono-Regular, Consolas, Menlo, monospace; background: #eef1f4; padding: .1em .3em; border-radius: 4px; }
pre { margin: .5rem 0; padding: .7rem .9rem; background: #f4f6f8; border: 1px solid #d8dee4; border-radius: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; font-size: .85rem; }
blockquote { margin: .6rem 0; padding: .1rem 1rem; border-left: 4px solid #b8c0c8; color: #3a424a; }
ul, ol { margin: .5rem 0; padding-left: 1.6rem; }
table { border-collapse: collapse; margin: .8rem 0; max-width: 100%; display: block; overflow-x: auto; }
th, td { border: 1px solid #c4ccd4; padding: .35rem .6rem; text-align: left; vertical-align: top; }
th { background: #eef1f4; }
img { max-width: 100%; height: auto; }
figure { margin: .8rem 0; }
figure.diagram img { background: #151a1f; padding: .5rem; border-radius: 6px; }
details { margin: .4rem 0; }
li p { margin: .15rem 0; }
hr { border: 0; border-top: 1px solid #d0d7de; margin: 1.2rem 0; }
.hint { background: #fff4d6; border: 1px solid #e3c36b; padding: .6rem .8rem; border-radius: 6px; margin: 0 0 1rem; }
@media print { .hint { display: none; } body { max-width: none; padding: 0; font-size: 11pt; } h1, h2, h3, h4 { break-after: avoid; } pre, blockquote, table, figure, img, tr { break-inside: avoid; } a { color: #0b57d0; } }
"""


def h(text: str) -> str:
    return htmllib.escape(clean(text), quote=True)


def _html_runs(runs: List[Run]) -> str:
    out = []
    for r in runs:
        if r.br:
            out.append("<br>")
            continue
        if r.image:
            out.append(f'<img src="data:{r.image.mime};base64,{base64.b64encode(r.image.data).decode()}" alt="{h(r.image.alt)}" width="{r.image.width}" height="{r.image.height}">')
            continue
        s = h(r.text)
        if r.code:
            s = f"<code>{s}</code>"
        if r.bold:
            s = f"<strong>{s}</strong>"
        if r.italic:
            s = f"<em>{s}</em>"
        if r.href:
            s = f'<a href="{h(r.href)}" rel="noopener noreferrer" target="_blank">{s}</a>'
        out.append(s)
    return "".join(out)


def _html_block(b: Any, labels: Dict[str, str]) -> str:
    if isinstance(b, Heading):
        return f"<h{b.level}>{_html_runs(b.runs)}</h{b.level}>"
    if isinstance(b, Paragraph):
        return f"<p>{_html_runs(b.runs)}</p>"
    if isinstance(b, Label):
        return f'<p class="label">{_html_runs(b.runs)}</p>'
    if isinstance(b, Meta):
        return f'<p class="meta">{_html_runs(b.runs)}</p>'
    if isinstance(b, Code):
        lang = f' class="language-{h(b.lang)}"' if b.lang else ""
        return f"<pre><code{lang}>{h(b.text)}</code></pre>"
    if isinstance(b, Diagram):
        source = f"<details><summary>{h(labels['diagram_source'])}</summary><pre><code>{h(b.source)}</code></pre></details>"
        if b.svg:
            uri = "data:image/svg+xml;base64," + base64.b64encode(b.svg.encode("utf-8")).decode()
            return f'<figure class="diagram"><img src="{uri}" alt="{h(labels["diagram"])}">{source}</figure>'
        if b.png:
            return f'<figure class="diagram"><img src="data:image/png;base64,{base64.b64encode(b.png.data).decode()}" alt="{h(labels["diagram"])}">{source}</figure>'
        return f"<pre><code>{h(b.source)}</code></pre>"
    if isinstance(b, Rule):
        return "<hr>"
    if isinstance(b, Quote):
        return "<blockquote>" + "".join(_html_block(x, labels) for x in b.blocks) + "</blockquote>"
    if isinstance(b, Lst):
        tag = "ol" if b.ordered else "ul"
        return f"<{tag}>" + "".join("<li>" + "".join(_html_block(x, labels) for x in item) + "</li>" for item in b.items) + f"</{tag}>"
    if isinstance(b, Table):
        head = "".join(f"<th>{_html_runs(c)}</th>" for c in b.header)
        body = "".join("<tr>" + "".join(f"<td>{_html_runs(c)}</td>" for c in row) + "</tr>" for row in b.rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return ""


def write_html(doc: Doc, print_hint: bool = False) -> bytes:
    """One self-contained file: embedded style, images as data addresses, no script. Its policy lets nothing else load."""
    hint = f'<p class="hint">{h(doc.labels["print_hint"])}</p>' if print_hint else ""
    body = "\n".join(_html_block(b, doc.labels) for b in doc.blocks)
    heading = "" if doc.bare else f"<h1>{h(doc.title)}</h1>\n"
    text = (f'<!doctype html>\n<html lang="{doc.lang}">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">\n'
            f"<title>{h(doc.title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n{hint}{heading}{body}\n</body>\n</html>\n")
    return text.encode("utf-8")


# ---------------------------------------------------------------- file names and the entry point

_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)
EXTENSIONS = {"docx": "docx", "md": "md", "pdf": "pdf", "html": "html", "odt": "odt"}
CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "pdf": "text/html; charset=utf-8",           # the page prints it (see write_html(print_hint=True))
}


def file_name(name: Any, fmt: str, fallback: str = "conversation") -> str:
    """A name that is safe in any folder: no separators, no controls, no reserved Windows names, the format's extension added."""
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f-\x9f\u2028\u2029]", " ", clean(name if isinstance(name, str) else ""))
    ext = EXTENSIONS[fmt]
    if stem.lower().endswith("." + ext):
        stem = stem[:-(len(ext) + 1)]
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:120].strip(" .")
    if not stem or _RESERVED.match(stem):
        stem = fallback
    return f"{stem}.{ext}"


def export(req: Any) -> Tuple[bytes, str, str]:
    """{"snapshot": ..., "select": {"prompts", "reasoning", "actions", "answers"}, "format": ..., "name": ...} -> (bytes, content type, file name).
    The "pdf" format returns the page to print, with its hint; the desktop app prints it itself."""
    if not isinstance(req, dict):
        raise ExportError("Send a JSON object.")
    fmt = req.get("format")
    if fmt not in EXTENSIONS:
        raise ExportError("Choose a format: docx, md, pdf, html or odt.")
    doc = build(req.get("snapshot"), req.get("select"), bare=req.get("bare") is True)
    name = file_name(req.get("name") or doc.title, fmt)
    if fmt == "md":
        data = write_md(doc)
    elif fmt == "html":
        data = write_html(doc)
    elif fmt == "pdf":
        data = write_html(doc, print_hint=req.get("hint") is True)
    else:
        from serve import exportoffice
        data = exportoffice.write_docx(doc) if fmt == "docx" else exportoffice.write_odt(doc)
    return data, CONTENT_TYPES[fmt], name
