"""The figures of documents that are not read page by page: Word files (the pictures are taken straight from the file, in the order of the text), and, with the same last step,
the pictures of web pages and wikis that a source hands over as bytes.

An item is `{data, alt, caption, anchor, source, url}`: the bytes of the picture, the description its author gave it, the caption next to it, a piece of the text it sits beside (so
that the picture is linked to the passage that holds that text) and where it came from. `finish` turns items into the same records as the PDF figures (`figures.extract`): the picture
cut down and saved as WebP with a thumbnail, its words read, its label and the sentences that speak of it. A picture is never made of the document's own page, so `page` is 0."""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main", "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "v": "urn:schemas-microsoft-com:vml", "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
MIN_SIDE_PX = 48                   # a picture thinner than this is a bullet or a rule
MIN_AREA_PX = 4000
ANCHOR_CHARS = 200
ALT_CHARS = 400
_CAPTION_STYLE = re.compile(r"caption|didascalia|legende|beschriftung", re.IGNORECASE)


def _q(tag: str) -> str:
    prefix, name = tag.split(":")
    return "{" + NS[prefix] + "}" + name


def _text(p: ET.Element) -> str:
    return " ".join("".join(t.text or "" for t in p.iter(_q("w:t"))).split())


def docx_items(path: Path) -> List[Dict[str, Any]]:
    """The pictures of a Word file in the order of the text: bytes, the author's description, the caption beside it, and the text it sits beside. A file that cannot be opened has none."""
    from tadamoo.rag.ingest import figures
    try:
        z = zipfile.ZipFile(path)
        body = ET.fromstring(z.read("word/document.xml"))
        rels = {r.get("Id"): r.get("Target", "") for r in ET.fromstring(z.read("word/_rels/document.xml.rels")).iter(_q("rel:Relationship")) if r.get("TargetMode") != "External"}
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return []
    paragraphs = list(body.iter(_q("w:p")))
    texts = [_text(p) for p in paragraphs]

    def is_caption(i: int) -> bool:
        if not 0 <= i < len(paragraphs):
            return False
        style = paragraphs[i].find("w:pPr/w:pStyle", NS)
        return bool(texts[i]) and (bool(style is not None and _CAPTION_STYLE.search(style.get(_q("w:val")) or "")) or bool(figures._LABEL.match(texts[i])))

    items: List[Dict[str, Any]] = []
    with z:
        for i, p in enumerate(paragraphs):
            found = []
            for blip in p.iter(_q("a:blip")):
                found.append((blip.get(_q("r:embed")), next((d for d in p.iter(_q("wp:docPr"))), None)))
            for img in p.iter(_q("v:imagedata")):
                found.append((img.get(_q("r:id")), None))
            for rid, doc_pr in found:
                target = rels.get(rid or "")
                if not target:
                    continue
                member = posixpath.normpath(posixpath.join("word", target)) if not target.startswith("/") else target.lstrip("/")
                try:
                    data = z.read(member)
                except KeyError:
                    continue
                caption = next((texts[j] for j in (i + 1, i - 1, i) if is_caption(j)), "")
                near = next((texts[j] for j in (i, i - 1, i + 1, i - 2) if 0 <= j < len(texts) and texts[j] and not is_caption(j) and len(texts[j]) >= 20), "")
                alt = ""
                if doc_pr is not None:
                    alt = " ".join(str(doc_pr.get("descr") or doc_pr.get("title") or "").split())[:ALT_CHARS]
                items.append({"data": data, "alt": alt, "caption": caption[:ALT_CHARS], "anchor": near[:ANCHOR_CHARS], "source": "docx", "url": ""})
    return items


def finish(items: Sequence[Dict[str, Any]], blocks: Sequence[Dict[str, Any]], folder: Path, python: str = "", progress: Optional[Callable[[Dict[str, Any]], None]] = None,
           cancel: Optional[Any] = None, command: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """The figure records for `items`: each picture opened, flattened to RGB, cut down to `figures.MAX_SIDE` and saved as WebP with its thumbnail in `folder`, its words read, its
    label taken from the caption or from the words, and the sentences of the document that name it. The same picture twice is kept once; one too small to mean anything is dropped."""
    from PIL import Image
    from tadamoo.rag.ingest import figures
    tell = progress or (lambda event: None)
    tell({"event": "stage", "stage": "figures"})
    out: List[Dict[str, Any]] = []
    files: List[Path] = []
    seen = set()
    for item in items[: figures.MAX_FIGURES]:
        if cancel is not None and cancel.is_set():
            from tadamoo.rag.errors import RagError
            raise RagError("the reading was cancelled", "cancelled")
        digest = hashlib.sha1(item["data"]).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        try:
            with Image.open(io.BytesIO(item["data"])) as raw:
                raw.load()
                image = raw.convert("RGBA")
        except Exception:
            continue                                                            # a format PIL cannot read (EMF, WMF): left out
        if min(image.size) < MIN_SIDE_PX or image.width * image.height < MIN_AREA_PX:
            continue
        flat = Image.new("RGB", image.size, "white")
        flat.paste(image, mask=image.getchannel("A"))
        if max(flat.size) > figures.MAX_SIDE:
            flat.thumbnail((figures.MAX_SIDE, figures.MAX_SIDE), Image.LANCZOS)
        n = len(out) + 1
        file, thumb, w, h = figures.save(flat, folder, n)
        out.append(figures.Figure(n=n, page=0, box=[0.0, 0.0, 1.0, 1.0], source=item.get("source", "docx"), caption=item.get("caption", ""), alt=item.get("alt", ""),
                                  anchor=item.get("anchor", ""), url=item.get("url", ""), image=file, thumb=thumb, width=w, height=h,
                                  sha=hashlib.sha1((folder / file).read_bytes()).hexdigest()[:24]))
        files.append(folder / file)
        tell({"event": "progress", "page": n, "pages": len(items)})
    read = figures.ocr(files, python, cancel, command)
    for f, p in zip(out, files):
        lines = read.get(str(p)) or read.get(p.as_posix()) or []
        f["ocr"] = figures.labels_in(lines, int(f["height"]), False)
        label, number = figures.label_of(f["caption"]) if f["caption"] else ("", "")
        if not label:
            label, number = next(((l, k) for l, k in (figures.label_of(str(x["text"])) for x in sorted(lines, key=lambda x: -x["box"][1] if x.get("box") else 0)
                                                     if float(x.get("score", 1)) >= figures.OCR_MIN_SCORE) if l), ("", ""))
        f["label"], f["number"] = label, number
        f["refs"] = figures.refs_to(number, blocks, -1)
    return [dict(f) for f in out]
