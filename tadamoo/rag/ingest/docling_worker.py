"""Reads one document with Docling and prints what it found as JSON lines on stdout. Run by the Python of a Docling environment, never imported.

    python docling_worker.py --file PATH --format .pdf [--window 40] [--url http://127.0.0.1:7242] [--cpu] [--from-page N] [--paged] [--skip 3,9]

Lines:  {"event": "start", "format", "pages", "device", "from_page"}  {"event": "progress", "page", "pages", "device"}  {"event": "blocks", "blocks": [...]}
        {"event": "skipped", "page", "message"}  {"event": "empty", "page", "reason": "image"|"text"|"unknown"}  {"event": "done", "pages", "device"}
        {"event": "figures", "figures": [{"page", "box": [x0, y0, x1, y1], "caption", "kind"}]}: the pictures and charts Docling found, each with its place on the page
        as fractions of the page (top left is 0, 0) and the caption Docling linked to it.
        or  {"event": "error", "message"}. `empty` is a page that came out with no text although it has a picture or a text layer.

A block is `{type: heading|paragraph|list|table|code|caption|footnote|page_break, text, page, level?}`, a table as Markdown. A big PDF is
converted in windows of `--window` pages, so the memory a page needs is given back before the next window; a window that fails is read page by page,
each page tried three times (as it is, with a new converter, with a new converter on the CPU) and a page that fails all three is `skipped`. `--from-page`,
`--paged` and `--skip` are how the caller resumes after the worker itself died. The device is chosen by asking the
server for VRAM first (a lease, tadamoo/vram_lease.py): with the lease the GPU is used, without it the CPU. Closing stdin ends the work: a thread
looks at the pipe twice a second without ever blocking on it (a thread blocked on stdin keeps the main thread from loading a DLL on Windows).
Nothing here imports the rest of TaDaMoo but the standard-library lease client, which is read by its path."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

DOCLING_MIB = 3000


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def lease_client():
    """The lease client module of the repository, loaded by its path (the package `tadamoo` is not installed in this environment)."""
    path = Path(__file__).resolve().parents[2] / "vram_lease.py"
    spec = importlib.util.spec_from_file_location("tadamoo_vram_lease", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stdin_closed() -> bool:
    """Whether the other end of stdin is gone, asked without waiting for anything to be written."""
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
            available = ctypes.c_ulong(0)
            return not ctypes.windll.kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
        import select
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(readable) and not sys.stdin.buffer.peek(1)
    except Exception:
        return False


def watch_stdin(stop: threading.Event) -> None:
    """A closed stdin is the order to stop."""
    while not stdin_closed():
        time.sleep(0.5)
    stop.set()
    os._exit(3)


def page_count(path: str) -> int:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 0


def page_pieces(raw: str, prov: list) -> list:
    """[(page, text)] for a text item that Docling spread over several pages (a scanned page read as one block): each piece is the part of the text that
    belongs to its page by the character span Docling gives. The whole text on its first page when the spans are missing or do not fit."""
    first = int(prov[0].page_no) if prov else 0
    spans = []
    for p in prov:
        span = getattr(p, "charspan", None)
        if not span or len(span) != 2 or not 0 <= int(span[0]) <= int(span[1]):
            return [(first, raw)]
        spans.append((int(p.page_no), min(int(span[0]), len(raw)), min(int(span[1]), len(raw))))      # the span may run a character or two past the text
    pieces: list = []
    for page, a, b in sorted(spans, key=lambda s: s[1]):
        if pieces and pieces[-1][0] == page:
            pieces[-1] = (page, pieces[-1][1] + raw[a:b])
        else:
            pieces.append((page, raw[a:b]))
    return pieces if "".join(t for _, t in pieces).strip() == raw.strip() or len("".join(t for _, t in pieces)) >= 0.9 * len(raw) else [(first, raw)]


def blocks_of(document, offset: int = 0) -> list:
    """The blocks of a converted Docling document, in reading order. Running headers and footers that Docling classified are left out. A text item that
    covers several pages is cut at the page borders Docling records, so every part is cited on its own page."""
    out: list = []
    items = []
    for item, level in document.iterate_items():
        items.append((item, level))
    current = 0
    group: list = []

    def flush_list() -> None:
        nonlocal group
        if group:
            out.append({"type": "list", "text": "\n".join(f"- {t}" for t, _ in group), "page": group[0][1]})
            group = []

    for item, _ in items:
        label = getattr(getattr(item, "label", None), "value", "") or ""
        prov = getattr(item, "prov", None) or []
        page = int(prov[0].page_no) if prov else 0
        if label == "list_item":
            if group and group[0][1] != page:
                flush_list()
            group.append(((getattr(item, "text", "") or "").strip(), page))
            continue
        flush_list()
        if page and page != current:
            current = page
            out.append({"type": "page_break", "text": "", "page": page})
        if label in ("page_header", "page_footer", "picture", "chart", "empty_value", "marker"):
            continue
        if label == "table":
            try:
                text = item.export_to_markdown(document)
            except TypeError:
                text = item.export_to_markdown()
            kind, extra = "table", {}
        else:
            text = (getattr(item, "text", "") or "").strip()
            if len(prov) > 1 and label in ("text", "paragraph", "code", "caption", "footnote"):
                pieces = page_pieces(getattr(item, "text", "") or "", prov)
                if len(pieces) > 1:
                    for number, part in pieces:
                        if number != current:
                            current = number
                            out.append({"type": "page_break", "text": "", "page": number})
                        if part.strip():
                            out.append({"type": "caption" if label == "caption" else "code" if label == "code" else "footnote" if label == "footnote" else "paragraph",
                                        "text": part.strip(), "page": number})
                    continue
            if label in ("section_header", "title"):
                kind, extra = "heading", {"level": 1 if label == "title" else max(1, min(6, int(getattr(item, "level", 1) or 1) + 1))}
            elif label == "code":
                kind, extra = "code", {}
            elif label == "caption":
                kind, extra = "caption", {}
            elif label == "footnote":
                kind, extra = "footnote", {}
            else:
                kind, extra = "paragraph", {}
        if text.strip():
            out.append({"type": kind, "text": text, "page": page, **extra})
    flush_list()
    return out


def figures_of(document) -> list:
    """The pictures and charts of a converted Docling document: `{page, box, caption, kind}`, the box as fractions of the page with the origin at the top left, so
    whoever draws the page at any size can cut the figure out."""
    out: list = []
    for item, _ in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        prov = getattr(item, "prov", None) or []
        if label not in ("picture", "chart") or not prov:
            continue
        try:
            page = document.pages[int(prov[0].page_no)]
            width, height = float(page.size.width), float(page.size.height)
            box = prov[0].bbox.to_top_left_origin(height)
            x0, x1 = sorted((box.l / width, box.r / width))
            y0, y1 = sorted((box.t / height, box.b / height))
        except Exception:
            continue
        try:
            caption = (item.caption_text(document) or "").strip()
        except Exception:
            caption = ""
        out.append({"page": int(prov[0].page_no), "box": [round(max(0.0, x0), 4), round(max(0.0, y0), 4), round(min(1.0, x1), 4), round(min(1.0, y1), 4)],
                    "caption": caption[:400], "kind": label})
    return out


def build_converter(device: str):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
    options = PdfPipelineOptions(accelerator_options=AcceleratorOptions(device=device, num_threads=4))
    options.do_ocr = True
    options.do_table_structure = True
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options), InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)})


PAGE_TRIES = 3


def free_memory() -> None:
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class Reader:
    """Converts pages with a Docling converter that can be built again, on the same device or on the CPU, when a page fails."""

    def __init__(self, path: str, device: str) -> None:
        self.path, self.device = path, device
        self.converter = build_converter(device)
        self.figures: list = []                                  # the figures of the pages converted since they were last printed

    def rebuild(self, device: str) -> None:
        self.converter = None
        free_memory()
        self.device = device
        self.converter = build_converter(device)

    def convert(self, first: int, last: int) -> list:
        result = self.converter.convert(self.path, page_range=(first, last))
        try:
            blocks = blocks_of(result.document)
            self.figures += figures_of(result.document)
            return blocks
        finally:
            del result

    def emit_figures(self) -> None:
        if self.figures:
            emit({"event": "figures", "figures": self.figures})
            self.figures = []

    def page(self, number: int) -> tuple:
        """(blocks, "") for one page, tried `PAGE_TRIES` times: again as it is, then with a new converter, then with a new converter on the CPU.
        (None, the last error) when every try failed."""
        message = ""
        for attempt in range(PAGE_TRIES):
            try:
                if attempt == 1:
                    self.rebuild(self.device)
                elif attempt == 2 and self.device != "cpu":
                    self.rebuild("cpu")
                return self.convert(number, number), ""
            except Exception as exc:
                message = f"{type(exc).__name__}: {str(exc)[:200]}"
        return None, message


def pages_of(blocks: list) -> set:
    return {int(b.get("page") or 0) for b in blocks if b.get("type") != "page_break"}


def what_is_on(path: str, number: int) -> str:
    """"blank" for a page with no text layer and no picture, "image" for a page that is a picture, "text" for a page whose own text layer has words."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
        try:
            page = doc[number - 1]
            if len(page.get_textpage().get_text_range().strip()) > 20:
                return "text"
            return "image" if any(o.type == 3 for o in page.get_objects()) else "blank"
        finally:
            doc.close()
    except Exception:
        return "unknown"


def spans(first: int, last: int, size: int, skip: set) -> list:
    """The runs of pages from `first` to `last`, at most `size` long, with the pages in `skip` left out."""
    out, a = [], first
    while a <= last:
        if a in skip:
            a += 1
            continue
        b = a
        while b < last and b - a + 1 < size and b + 1 not in skip:
            b += 1
        out.append((a, b))
        a = b + 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--format", required=True)
    parser.add_argument("--window", type=int, default=40)
    parser.add_argument("--url", default="")
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--from-page", type=int, default=1)
    parser.add_argument("--paged", action="store_true")
    parser.add_argument("--skip", default="")
    args = parser.parse_args()
    stop = threading.Event()
    lease = None
    device = "cpu"
    try:
        if not args.cpu:
            try:
                module = lease_client()
                lease = module.VramLease("docling", DOCLING_MIB, "reading " + Path(args.file).name, base_url=args.url or None).acquire()
                device = "cuda" if lease.gpu_ok else "cpu"
            except Exception:
                lease, device = None, "cpu"
        pdf = args.format.lower() == ".pdf"
        reader = Reader(args.file, device)                     # the libraries first: the PDF library loaded before them stops numpy from loading
        pages = page_count(args.file) if pdf else 0
        emit({"event": "start", "format": args.format, "pages": pages, "device": device, "from_page": args.from_page})
        threading.Thread(target=watch_stdin, args=(stop,), daemon=True).start()
        total = 0
        if pdf and pages:
            skip = {int(n) for n in args.skip.split(",") if n.strip()}
            for first, last in spans(max(1, args.from_page), pages, 1 if args.paged else args.window, skip):
                blocks = None
                if first != last:
                    try:
                        blocks = reader.convert(first, last)
                    except Exception:
                        pass
                if blocks is not None:
                    have = pages_of(blocks)
                    for number in range(first, last + 1):             # a page with nothing: read alone, and said so when it stays empty
                        if number in have:
                            continue
                        again, _ = reader.page(number)
                        if again and pages_of(again):
                            blocks += again
                        else:
                            kind = what_is_on(args.file, number)
                            if kind != "blank":
                                emit({"event": "empty", "page": number, "reason": kind})
                    total = max([total] + [b["page"] for b in blocks])
                    emit({"event": "blocks", "blocks": blocks})
                    reader.emit_figures()
                    emit({"event": "progress", "page": last, "pages": pages, "device": reader.device})
                    continue
                for number in range(first, last + 1):             # one page, or a window that failed: its pages one by one
                    got, message = reader.page(number)
                    if got is None:
                        emit({"event": "skipped", "page": number, "message": message})
                    else:
                        emit({"event": "blocks", "blocks": got})
                        reader.emit_figures()
                        if not pages_of(got) and what_is_on(args.file, number) != "blank":
                            emit({"event": "empty", "page": number, "reason": what_is_on(args.file, number)})
                    emit({"event": "progress", "page": number, "pages": pages, "device": reader.device})
        else:
            document = reader.converter.convert(args.file).document
            blocks = blocks_of(document)
            total = max([0] + [b["page"] for b in blocks])
            emit({"event": "blocks", "blocks": blocks})
            emit({"event": "figures", "figures": figures_of(document)})
        emit({"event": "done", "pages": pages or total, "device": reader.device})
        return 0
    except Exception as exc:                                   # the reader says what went wrong; the caller tries again or falls back
        emit({"event": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return 1
    finally:
        if lease is not None:
            lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
