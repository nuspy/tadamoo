"""The figures of a document: found, cut out of the pages, labelled, read and kept as images of the archive, each ready to be a passage of its own.

Where a figure is comes from the reader: Docling gives the pictures and charts of a page with their place and the caption it linked to them (`reading["figures"]`);
a page that gave no text and no picture (`empty_pages`: a scanned sheet of drawings) is one figure, the whole sheet; and for a PDF read by the basic reader the images and
the drawn regions of its pages are found with PyMuPDF. The picture itself is cut from the page drawn by PyMuPDF at the place the reader gave, so it does not depend on
what the reader would save. Each figure gets:

  label     `FIG. 1A`, `Figure 3`, `图2`: from the caption, else from the text read inside the image
  caption   the reader's caption, else the caption line of the page, else the line of the image that starts with the label
  refs      the sentences of the document that speak of it (`FIG. 1A shows ...`, the entry of the brief description of the drawings, the paragraph that discusses it)
  ocr       the words read inside the image (axis labels, gene names, legends), by RapidOCR in the Docling environment, on the CPU
  image     `images/<document id>/<n>.webp` (long side at most 1600 px) and `<n>_t.webp` (a thumbnail of 256 px)

The text of a figure passage (`passage_text`) is those pieces in words, so it is searched and embedded like any other passage; a model that can see may add its own
description later (tadamoo/rag/enrich.py). Everything read from a document or an image is data, never instructions."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSION = 1
MAX_SIDE = 1600
THUMB_SIDE = 256
DPI = 170
MIN_AREA = 0.02                    # a picture smaller than this share of the page is a logo or a rule, not a figure
MIN_SIDE = 0.07                    # nor one thinner than this share of the page
MAX_FIGURES = 3000                 # in one document
OCR_MIN_SCORE = 0.6
OCR_CHARS = 700
REF_CHARS = 320
MAX_REFS = 3
EDGE_TOP, EDGE_BOTTOM = 0.07, 0.955     # on a whole sheet the page header and footer lie outside these
PAD = 0.012                        # the margin kept around a figure, as a share of the page, so a caption written just outside it is read
WORKER = Path(__file__).with_name("ocr_worker.py")
OCR_TIMEOUT_S = 900

_LABEL = re.compile(r"(?:^|\b)(FIGS?\.?|FIGURES?|FIGURA|ABB\.?|图|圖)\s*([0-9]+[A-Za-z]?)(?=$|[^0-9A-Za-z])", re.IGNORECASE)
_NUMBER = r"([0-9]+[A-Za-z]?)"
_REF = r"(?:FIGS?\.?|FIGURES?|FIGURA|ABB\.?|图|圖)\s*"


class Figure(dict):
    """One figure: `{n, page, box, source, label, number, caption, refs, ocr, image, thumb, width, height}` (the box as fractions of the page, top left 0, 0)."""


# -- where the figures are ---------------------------------------------------------------------------------------------------------------

def _area(b: Sequence[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: Sequence[float], b: Sequence[float]) -> float:
    inter = _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])
    return inter / max(1e-9, min(_area(a), _area(b)))


def _fit(box: Sequence[float]) -> List[float]:
    return [round(min(1.0, max(0.0, v)), 4) for v in box]


def keep_region(box: Sequence[float]) -> bool:
    return _area(box) >= MIN_AREA and box[2] - box[0] >= MIN_SIDE and box[3] - box[1] >= MIN_SIDE


def regions(path: Path, reading: Dict[str, Any], blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`[{page, box, caption, source}]` in page order: the reader's figures, a sheet for each empty page that has none, and for a basic reading of a PDF the images and
    drawings of its pages."""
    found: List[Dict[str, Any]] = []
    for f in reading.get("figures") or []:
        box = _fit(f.get("box") or [0, 0, 0, 0])
        if keep_region(box):
            found.append({"page": int(f["page"]), "box": box, "caption": str(f.get("caption") or ""), "source": "docling"})
    have = {r["page"] for r in found}
    for page in reading.get("empty_pages") or []:
        if int(page) not in have:
            found.append({"page": int(page), "box": [0.03, 0.03, 0.97, 0.97], "caption": "", "source": "sheet"})
    if reading.get("extraction") == "basic" and path.suffix.lower() == ".pdf":
        text_pages = {int(b.get("page") or 0) for b in blocks if b.get("type") not in ("page_break",) and str(b.get("text") or "").strip()}
        found += [r for r in detect(path) if r["page"] not in have]
        del text_pages
    merged: List[Dict[str, Any]] = []
    for r in sorted(found, key=lambda r: (r["page"], r["box"][1], r["box"][0])):
        twin = next((m for m in merged if m["page"] == r["page"] and _overlap(m["box"], r["box"]) > 0.7), None)
        if twin is None:
            merged.append(r)
        elif r["caption"] and not twin["caption"]:
            twin["caption"] = r["caption"]
    return merged[:MAX_FIGURES]


def detect(path: Path) -> List[Dict[str, Any]]:
    """The images and the drawn regions of the pages of a PDF with a text layer, by PyMuPDF. A page that is one image over its whole area is a scan, not a figure."""
    try:
        import fitz
    except ImportError:
        return []
    out: List[Dict[str, Any]] = []
    try:
        doc = fitz.open(str(path))
    except Exception:
        return []
    try:
        for number, page in enumerate(doc, 1):
            w, h = float(page.rect.width), float(page.rect.height)
            boxes: List[List[float]] = []
            try:
                boxes += [[b[0] / w, b[1] / h, b[2] / w, b[3] / h] for b in (i["bbox"] for i in page.get_image_info())]
            except Exception:
                pass
            try:
                boxes += [[r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h] for r in page.cluster_drawings()]
            except Exception:
                pass
            for box in boxes:
                box = _fit(box)
                if keep_region(box) and _area(box) < 0.85 and not any(_overlap(o["box"], box) > 0.8 for o in out if o["page"] == number):
                    out.append({"page": number, "box": box, "caption": "", "source": "image"})
    finally:
        doc.close()
    return out


# -- cutting them out --------------------------------------------------------------------------------------------------------------------

def render(pdf: Any, page_number: int, box: Sequence[float], dpi: int = DPI) -> Any:
    """A PIL image of the region `box` of page `page_number` (1 based) of the open PyMuPDF document `pdf`, with a margin."""
    import fitz
    from PIL import Image
    page = pdf[page_number - 1]
    w, h = float(page.rect.width), float(page.rect.height)
    pad = [box[0] - PAD, box[1] - PAD, box[2] + PAD, box[3] + PAD]
    clip = fitz.Rect(max(0.0, pad[0]) * w, max(0.0, pad[1]) * h, min(1.0, pad[2]) * w, min(1.0, pad[3]) * h)
    longest = max(clip.width, clip.height) / 72.0 * dpi
    scale = dpi if longest <= MAX_SIDE else dpi * MAX_SIDE / longest
    pix = page.get_pixmap(dpi=max(40, int(scale)), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def save(image: Any, folder: Path, n: int) -> Tuple[str, str, int, int]:
    """Writes the figure and its thumbnail as WebP into `folder`; `(file name, thumbnail name, width, height)`."""
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    name, thumb = f"{n}.webp", f"{n}_t.webp"
    image.save(folder / name, "WEBP", quality=85, method=4)
    small = image.copy()
    small.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
    small.save(folder / thumb, "WEBP", quality=70)
    return name, thumb, image.width, image.height


# -- reading what is in them ---------------------------------------------------------------------------------------------------------------

def ocr(images: Sequence[Path], python: str, cancel: Optional[Any] = None, command: Optional[Sequence[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """`{image path: [{text, score, box}]}` read by the OCR worker of the Docling environment `python` (nothing when there is none or it fails: the figure is kept
    without its labels). `command` replaces the worker's command line (for tests)."""
    if not images or not (python or command):
        return {}
    argv = list(command) if command else [python, "-X", "utf8", str(WORKER)]
    argv += [str(p) for p in images]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=OCR_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
                              env=None)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and "image" in row and isinstance(row.get("lines"), list):
            out[str(row["image"])] = [l for l in row["lines"] if isinstance(l, dict) and isinstance(l.get("text"), str)]
    return out


def labels_in(lines: Sequence[Dict[str, Any]], height: int = 0, sheet: bool = False) -> str:
    """The words read inside an image as one run of text: the confident lines, each once, in the order read, at most `OCR_CHARS` characters. On a whole sheet the
    running header and footer of the page (the top and bottom margins) are left out unless they name a figure."""
    seen: List[str] = []
    for l in lines:
        text = " ".join(str(l["text"]).split())
        if sheet and height and l.get("box") and not label_of(text)[0]:
            middle = (l["box"][1] + l["box"][3]) / 2 / height
            if middle < EDGE_TOP or middle > EDGE_BOTTOM:
                continue
        if float(l.get("score", 1.0)) >= OCR_MIN_SCORE and text and text not in seen:
            seen.append(text)
    return "; ".join(seen)[:OCR_CHARS]


# -- label, caption, the text that refers to it ------------------------------------------------------------------------------------------------

def norm_label(word: str, number: str) -> str:
    """`FIG. 1A` for `fig`/`FIGS.` and `1a`; `Figure 3` for `figure`; the Chinese `图2` as it is."""
    w = word.rstrip(".").lower()
    if w in ("图", "圖"):
        return f"{word}{number.upper()}"
    return ("Figure " if w.startswith("figure") or w == "figura" else "FIG. ") + number.upper()


def label_of(text: str) -> Tuple[str, str]:
    """`(label, number)` of the first figure name a text starts with or holds (`("FIG. 10B", "10B")`), or `("", "")`."""
    m = _LABEL.search(text.strip()[:120])
    return (norm_label(m.group(1), m.group(2)), m.group(2).upper()) if m else ("", "")


def caption_on_page(blocks: Sequence[Dict[str, Any]], page: int) -> str:
    """A caption block of the page that starts with a figure name, if the reader gave one."""
    for b in blocks:
        if int(b.get("page") or 0) == page and b.get("type") in ("caption", "paragraph") and _LABEL.match(str(b.get("text") or "").strip()):
            return " ".join(str(b["text"]).split())[:400]
    return ""


def refs_to(number: str, blocks: Sequence[Dict[str, Any]], page: int) -> List[str]:
    """Up to `MAX_REFS` passages of the document that speak of figure `number`: the sentence that opens with its name (the brief description of the drawings) first, then
    the sentences of the text that name it, never the figure's own page."""
    if not number:
        return []
    named = re.compile(rf"{_REF}{re.escape(number)}(?![0-9A-Za-z])", re.IGNORECASE)
    listed = re.compile(rf"^\s*{_REF}{re.escape(number)}(?![0-9A-Za-z])", re.IGNORECASE)
    first: List[str] = []
    rest: List[str] = []
    for b in blocks:
        if b.get("type") in ("page_break", "heading") or int(b.get("page") or 0) == page:
            continue
        for piece in re.split(r"\n+|(?<!Fig\.)(?<!FIG\.)(?<!Figs\.)(?<!FIGS\.)(?<!Abb\.)(?<!ABB\.)(?<=[.;:。])[ \t]+", str(b.get("text") or "")):
            piece = " ".join(piece.split())
            if len(piece) >= 24 and named.search(piece):
                (first if listed.match(piece) else rest).append(piece[:REF_CHARS])
    out: List[str] = []
    for r in first + rest:
        if r not in out:
            out.append(r)
    return out[:MAX_REFS]


def passage_text(f: Dict[str, Any]) -> str:
    """The text of a figure's passage: its name and caption, what the document says of it, the words inside it."""
    head = f.get("label") or "Figure"
    parts = [head + (f" (page {f['page']})" if f.get("page") else "") + (f": {f['caption']}" if f.get("caption") else "")]
    if f.get("alt"):
        parts.append("Description in the document: " + f["alt"])
    if f.get("refs"):
        parts.append("The text says: " + " | ".join(f["refs"]))
    if f.get("ocr"):
        parts.append("Text in the figure: " + f["ocr"])
    return "\n".join(parts)


# -- the whole step ----------------------------------------------------------------------------------------------------------------------

def extract(path: Path, name: str, reading: Dict[str, Any], blocks: Sequence[Dict[str, Any]], folder: Path, python: str = "",
            progress: Optional[Callable[[Dict[str, Any]], None]] = None, cancel: Optional[Any] = None, command: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Cuts every figure of the PDF at `path` out of its pages into `folder`, reads the words inside, and labels each: the list of `Figure` records, in page order.
    A file that is not a PDF, or that PyMuPDF cannot open, has none. `python` is the Python of the Docling environment (for the OCR; empty: no labels read)."""
    if path.suffix.lower() == ".docx":
        from tadamoo.rag.ingest import figures_office
        return figures_office.finish(figures_office.docx_items(path), blocks, folder, python, progress, cancel, command)
    if path.suffix.lower() != ".pdf":
        return []
    found = regions(path, reading, blocks)
    if not found:
        return []
    try:
        import fitz
        pdf = fitz.open(str(path))
    except Exception:
        return []
    tell = progress or (lambda event: None)
    tell({"event": "stage", "stage": "figures"})
    out: List[Dict[str, Any]] = []
    files: List[Path] = []
    try:
        for n, r in enumerate(found, 1):
            if cancel is not None and cancel.is_set():
                from tadamoo.rag.errors import RagError
                raise RagError("the reading was cancelled", "cancelled")
            try:
                image = render(pdf, r["page"], r["box"])
            except Exception:
                continue
            file, thumb, w, h = save(image, folder, n)
            out.append(Figure(n=n, page=r["page"], box=r["box"], source=r["source"], caption=r["caption"], image=file, thumb=thumb, width=w, height=h,
                              sha=hashlib.sha1((folder / file).read_bytes()).hexdigest()[:24]))
            files.append(folder / file)
            tell({"event": "progress", "page": n, "pages": len(found)})
    finally:
        pdf.close()
    read = ocr(files, python, cancel, command)
    for f, p in zip(out, files):
        lines = read.get(str(p)) or read.get(p.as_posix()) or []
        f["ocr"] = labels_in(lines, int(f["height"]), f["source"] == "sheet")
        label, number = label_of(f["caption"]) if f["caption"] else ("", "")
        if not label:
            label, number = next(((l, n) for l, n in (label_of(str(x["text"])) for x in sorted(lines, key=lambda x: -x["box"][1] if x.get("box") else 0)
                                                     if float(x.get("score", 1)) >= OCR_MIN_SCORE) if l), ("", ""))
        if not f["caption"]:
            f["caption"] = caption_on_page(blocks, f["page"]) if len([x for x in out if x["page"] == f["page"]]) == 1 else ""
            if f["caption"] and not label:
                label, number = label_of(f["caption"])
        f["label"], f["number"] = label, number
        f["refs"] = refs_to(number, blocks, f["page"])
    return [dict(f) for f in out]
