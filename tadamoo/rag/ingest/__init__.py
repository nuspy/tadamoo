"""Reading a document into structured blocks, cleaning them and cutting them into passages that keep their place in the document.

`read_document` is the one way in: Docling when an environment for it is set or found, else the basic readers; the answer says which
(`extraction`: "docling" or "basic")."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError
from tadamoo.rag.ingest import docling_client
from tadamoo.rag.ingest.basic import BASIC_FORMATS, basic_blocks

Progress = Callable[[Dict[str, Any]], None]


def formats(settings: Optional[Dict[str, Any]] = None, python: Optional[str] = None) -> List[str]:
    """The extensions that can be read now: the basic ones, and with Docling the rest of what it reads."""
    found = python if python is not None else docling_client.find_python((settings or {}).get("docling_python", ""))
    return list(dict.fromkeys(BASIC_FORMATS + (docling_client.DOCLING_FORMATS if found else ())))


def extraction_of(settings: Optional[Dict[str, Any]] = None) -> str:
    return "docling" if docling_client.find_python((settings or {}).get("docling_python", "")) else "basic"


def read_document(path: Path, name: str, settings: Optional[Dict[str, Any]] = None, progress: Optional[Progress] = None,
                  cancel: Optional[threading.Event] = None, python: Optional[str] = None, url: str = "") -> Dict[str, Any]:
    """`{blocks, pages, extraction, device, notes}` for the file `path` called `name`; `skipped_pages` lists the pages Docling could not read
    (each tried three times: a note says so). When Docling fails on a format the basic readers have, those are used and `notes` says why."""
    ext = Path(name).suffix.lower()
    found = python if python is not None else docling_client.find_python((settings or {}).get("docling_python", ""))
    notes: List[str] = []
    if ext not in formats(settings, found):
        raise RagError(f"{ext or 'a file without an extension'} is not a supported format: use " + ", ".join(formats(settings, found)))
    if found and ext in docling_client.DOCLING_FORMATS:
        try:
            got = docling_client.read(found, path, ext, progress=progress, cancel=cancel, url=url, cpu=(settings or {}).get("docling_device") == "cpu")
            skipped = sorted(got.pop("skipped", None) or [])
            if skipped:
                notes.append(f"{len(skipped)} page{'s' if len(skipped) > 1 else ''} could not be read after three tries and "
                             f"{'are' if len(skipped) > 1 else 'is'} missing: {', '.join(str(n) for n in skipped)}. Read {'them' if len(skipped) > 1 else 'it'} by hand")
            empty = got.pop("empty", None) or {}
            if empty:
                notes.append(f"{len(empty)} page{'s' if len(empty) > 1 else ''} gave no text although {'they have' if len(empty) > 1 else 'it has'} a picture or text: "
                             f"{', '.join(str(n) for n in sorted(empty))}. Check {'them' if len(empty) > 1 else 'it'} by hand")
            return {**got, "extraction": "docling", "notes": notes, "skipped_pages": skipped, "empty_pages": sorted(empty)}
        except docling_client.Cancelled:
            raise
        except RagError as e:
            if ext not in BASIC_FORMATS:
                raise
            notes.append(str(e) + "; read with the basic reader")
    try:
        got = basic_blocks(path, name)
    except RagError as e:
        raise RagError(f"{notes[-1]}; {e}" if notes else str(e)) from None            # the reason Docling gave is not lost
    if progress is not None:
        progress({"event": "done", "page": got["pages"], "pages": got["pages"], "device": "cpu"})
    return {**got, "extraction": "basic", "device": "cpu", "notes": notes}
