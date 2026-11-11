"""Reads one document with Docling and prints what it found as JSON lines on stdout. Run by the Python of a Docling environment, never imported.

    python docling_worker.py --file PATH --format .pdf [--window 40] [--url http://127.0.0.1:7242] [--cpu]

Lines:  {"event": "start", "format", "pages"}  {"event": "progress", "page", "pages", "device"}  {"event": "blocks", "blocks": [...]}
        {"event": "done", "pages", "device"}  or  {"event": "error", "message"}.

A block is `{type: heading|paragraph|list|table|code|caption|footnote|page_break, text, page, level?}`, a table as Markdown. A big PDF is
converted in windows of `--window` pages, so the memory a page needs is given back before the next window. The device is chosen by asking the
server for VRAM first (a lease, tadamoo/vram_lease.py): with the lease the GPU is used, without it the CPU. Closing stdin ends the work. Nothing
here imports the rest of TaDaMoo but the standard-library lease client, which is read by its path."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
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


def watch_stdin(stop: threading.Event) -> None:
    """A closed stdin is the order to stop."""
    try:
        while sys.stdin.read(1):
            pass
    except Exception:
        pass
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


def blocks_of(document, offset: int = 0) -> list:
    """The blocks of a converted Docling document, in reading order. Running headers and footers that Docling classified are left out."""
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


def build_converter(device: str):
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
    options = PdfPipelineOptions(accelerator_options=AcceleratorOptions(device=device, num_threads=4))
    options.do_ocr = True
    options.do_table_structure = True
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options), InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--format", required=True)
    parser.add_argument("--window", type=int, default=40)
    parser.add_argument("--url", default="")
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    threading.Thread(target=watch_stdin, args=(stop,), daemon=True).start()
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
        pages = page_count(args.file) if pdf else 0
        emit({"event": "start", "format": args.format, "pages": pages, "device": device})
        converter = build_converter(device)
        windows = [(a, min(a + args.window - 1, pages)) for a in range(1, pages + 1, args.window)] if pdf and pages > args.window else [None]
        total = 0
        for window in windows:
            result = converter.convert(args.file, page_range=window) if window else converter.convert(args.file)
            blocks = blocks_of(result.document)
            total = max([total] + [b["page"] for b in blocks])
            emit({"event": "blocks", "blocks": blocks})
            emit({"event": "progress", "page": window[1] if window else total, "pages": pages or total, "device": device})
            del result
        emit({"event": "done", "pages": pages or total, "device": device})
        return 0
    except Exception as exc:                                   # the reader says what went wrong; the caller falls back
        emit({"event": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return 1
    finally:
        if lease is not None:
            lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
