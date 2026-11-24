"""`ask_document`: puts a whole document in front of the loaded model and asks a question about it.

The server reads the file itself (a PDF page by page, with a scanned page also as its picture; Word, Excel and PowerPoint as text; plain text)
and the model answers from it in a call of its own, so the document never enters the conversation: only the answer does. It is the quickest way
to read a document that fits the model's window, and the only way to look at a scan. A document longer than the window is read by ranges
of pages (`pages`), or in pieces with read_attachment and search_attachment.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Callable

MAX_BYTES = 48 << 20            # the server reads at most 64 MiB of base64
ANSWER_TOKENS = 3000
TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def register_document_ask(registry: Any, resolve: Callable[[str], Path], client: Any) -> None:
    """Adds `ask_document`. `resolve` turns the model's path into a file: the workspace, `attachment:<id>` or `output:<id>`."""
    from tadamoo.tools.registry import EffectClass

    def ask_document(path: str, question: str, pages: str = "") -> str:
        try:
            file = resolve(path)
            size = file.stat().st_size
        except (OSError, PermissionError, KeyError, ValueError) as exc:
            return f"Could not open the document: {exc}"
        if size > MAX_BYTES:
            return f"The file is {size >> 20} MB; the most this reads is {MAX_BYTES >> 20} MB. Read it in pieces with read_attachment or read_document."
        suffix = file.suffix.lower()
        media = TYPES.get(suffix) or mimetypes.guess_type(file.name)[0] or "text/plain"
        name = Path(path.split(":", 1)[1] if path.startswith(("attachment:", "output:")) else path).name or file.name
        spec: dict = {"filename": name, "file_data": f"data:{media};base64," + base64.b64encode(file.read_bytes()).decode("ascii")}
        if str(pages).strip():
            spec["pages"] = str(pages).strip()
        try:
            reply = client.chat_completion(
                messages=[{"role": "user", "content": [{"type": "file", "file": spec}, {"type": "text", "text": str(question)[:4000]}]}],
                temperature=0.0, max_tokens=ANSWER_TOKENS, timeout=600.0, extra={"reasoning_effort": "low"})
            return (reply["choices"][0]["message"].get("content") or "").strip() or "The model returned no answer."
        except Exception as exc:                                  # the server's refusals say why: too long for the window, no vision for a scan
            return (f"The model could not read the document ({type(exc).__name__}: {exc}). If it is too long for the window, ask a range of pages "
                    "(pages=\"1-20\"); for a scan with no vision loaded, use read_document (it reads the pictures with OCR).")

    registry.register(
        "ask_document", "Puts a whole document (PDF, Word, Excel, PowerPoint or text: a file of the workspace or `attachment:<id>`) in front of the model in a call of "
                        "its own and asks it a question; only the answer comes back. Quickest for a document that fits the window, and the way to look at a scanned "
                        "PDF. `pages` is a range such as \"1-20\" for a PDF that is too long. For a table of many rows use query_attachment.",
        {"type": "object", "properties": {"path": {"type": "string"}, "question": {"type": "string"},
                                          "pages": {"type": "string", "description": "A range of pages of a PDF, such as \"1-20\" or \"5\" (default: all)"}},
         "required": ["path", "question"]}, EffectClass.PURE, ask_document, untrusted=True)
