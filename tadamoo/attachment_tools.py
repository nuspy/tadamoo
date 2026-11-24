"""The agent's tools for the files of a conversation: list, read in pieces, search, transcribe a recording, and hand back a file it made.

Text read from an attachment is data: it is fenced as untrusted by the tool registry and the model is told so. A recording goes to the
local callAPIcall service (a worker that does one job at a time); the transcript is kept next to the file and then read like any text.
A file the agent writes is opened again and checked before the person is offered it.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.attachments import AttachmentStore, OutputStore, manifest, query_tables, read_range, search_chunks
from tadamoo.integrations._http import ServiceError

MAX_OUTPUT_BYTES = 200_000_000
TRANSCRIPT_SHOWN = 6000


def register_attachment_tools(registry: Any, store: AttachmentStore, outputs: OutputStore, session: str, read_workspace_file: Callable[[str], Path],
                              call_client: Any = None, read_chars: int = 12000, wait_s: float = 900.0,
                              sleep: Callable[[float], None] = time.sleep) -> None:
    from tadamoo.tools.registry import EffectClass

    def known(att_id: str) -> Dict[str, Any]:
        return store.get(session, str(att_id))

    def missing(att_id: Any) -> str:
        """The answer for an id that is not an attachment, with the ones that are: a model that took the id of a data block for it needs the real ones at once."""
        items = store.list(session)
        have = "; ".join(f"{m['name']} = {m['id']}" for m in items) or "none"
        return f"No attachment '{att_id}'. An attachment id is 12 characters, not the id of a data block. The attachments of this conversation: {have}."

    def list_attachments() -> str:
        items = store.list(session)
        if not items:
            return "No file is attached to this conversation."
        out = []
        for m in items:
            text, note = store.text(session, m["id"])
            out.append("- " + manifest(m, text, note))
        return "\n".join(out)

    def read_attachment(id: str, start: int = 1, end: int = 0) -> str:
        try:
            meta = known(id)
        except KeyError:
            return missing(id)
        text, note = store.text(session, meta["id"])
        if text is None:
            return manifest(meta, None, note)
        if meta["family"] == "data" and start <= 1 and not end and len(text) > read_chars * 3:
            return "This is a large table. Ask it with query_attachment (SQL: a lookup, a count, a total); lines are not row numbers, and adding rows up by hand goes wrong.\n" + read_range(text, 1, 60, read_chars)
        return read_range(text, start, end or None, read_chars)

    def query_attachment(id: str, sql: str) -> str:
        try:
            meta = known(id)
        except KeyError:
            return missing(id)
        if meta["family"] != "data":
            return f"{meta['name']} is not a table. Use read_attachment or search_attachment."
        try:
            return query_tables(store.tables(session, meta["id"]), str(sql))
        except (OSError, ValueError, KeyError) as exc:
            return f"The table could not be read: {exc}"

    def search_attachment(id: str, query: str) -> str:
        try:
            meta = known(id)
        except KeyError:
            return missing(id)
        text, note = store.text(session, meta["id"])
        if text is None:
            return manifest(meta, None, note)
        hits = search_chunks(text, query)
        if not hits:
            return "Nothing in this file matches. Try other words, or read it in ranges."
        return "\n\n".join(f"Lines {h['from']}-{h['to']}:\n{h['text']}" for h in hits)

    def transcribe_attachment(id: str) -> str:
        try:
            meta = known(id)
        except KeyError:
            return missing(id)
        if meta["family"] not in ("audio", "video"):
            return f"{meta['name']} is not a recording."
        text, _ = store.text(session, meta["id"]) if (store._folder(session, meta["id"]) / "text.txt").exists() else (None, "")
        if text is not None:
            return f"The transcript is ready ({text.count(chr(10)) + 1} lines).\n" + text[:TRANSCRIPT_SHOWN] + ("\n[...] read the rest with read_attachment." if len(text) > TRANSCRIPT_SHOWN else "")
        if call_client is None or not call_client.is_available():
            return ("The transcription service (callAPIcall) is not running. Tell the person to start it; the recording stays attached "
                    "and nothing is lost.")
        folder = store._folder(session, meta["id"])
        job_file = folder / "job.txt"
        job = job_file.read_text(encoding="utf-8").strip() if job_file.exists() else ""
        if not job:
            tmp = Path(tempfile.mkdtemp(prefix="tadamoo-media-"))
            try:
                copy = tmp / meta["name"]
                shutil.copyfile(folder / "file", copy)
                try:
                    res = call_client.start_media_analysis(str(copy), title=meta["name"], analyze=False)
                except ValueError as exc:
                    return f"The service does not take this file: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if res.get("status") == "busy":
                return "The service is busy with another recording. Try again in a few minutes."
            if res.get("status") != "queued":
                return str(res.get("error") or "The service could not take the file.")
            job = res["id"]
            job_file.write_text(job, encoding="utf-8")
        state = call_client.wait_media_analysis(job, timeout_s=wait_s, sleep=sleep)
        if state.get("state") in ("timeout", "unavailable"):
            return (f"The transcription is still running ({state.get('progress', '?')}). Call transcribe_attachment('{meta['id']}') again later: "
                    "it picks the same job up.") if state["state"] == "timeout" else "The service stopped answering. Tell the person; try again once it is running."
        from tadamoo.integrations.call_api_client import format_transcript
        segments = call_client.media_transcript(job) or []
        if state.get("state") == "error" and not segments:
            job_file.unlink(missing_ok=True)
            return f"The service could not transcribe it: {state.get('error') or 'unknown error'}."
        transcript = format_transcript(segments)
        if not transcript:
            return "The service finished but the recording has no speech it could write down."
        store._write(folder, "text.txt", transcript)
        return (f"Transcript ({len(segments)} segments). The service's own summary is not used.\n" + transcript[:TRANSCRIPT_SHOWN] +
                ("\n[...] read the rest with read_attachment." if len(transcript) > TRANSCRIPT_SHOWN else ""))

    def counts(data: bytes) -> str:
        """For a text file, its words and lines; empty for a file that is not text."""
        if b"\x00" in data[:8000]:
            return ""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        return f", {len(text.split())} words, {len(text.splitlines())} lines"

    def finish(meta: Dict[str, Any], data: bytes = b"") -> str:
        mark = f"[file:{meta['id']}:{meta['name']}]"
        if meta["verified"]:
            return (f"{mark} Saved for the person: {meta['name']} ({meta['check']}{counts(data)}). "
                    "This copy is outside your working folder, so file and shell tools cannot open it. "
                    "They get a button to save it where they want. Do not describe it as saved elsewhere.")
        return f"{mark} The file was written but it did not pass the check: {meta['check']}. Fix it and write it again before telling the person it is done."

    def write_output_file(name: str, content: str) -> str:
        data = str(content).encode("utf-8")
        if len(data) > MAX_OUTPUT_BYTES:
            return "That file is too large to hand over this way."
        return finish(outputs.save(session, name, data), data)

    def publish_output_file(path: str) -> str:
        try:
            src = read_workspace_file(path)
            if not src.is_file():
                return f"{path} is not a file."
            if src.stat().st_size > MAX_OUTPUT_BYTES:
                return "That file is too large to hand over this way."
            data = src.read_bytes()
            return finish(outputs.save(session, src.name, data), data)
        except (OSError, PermissionError) as exc:
            return f"Could not read {path}: {exc}"

    def convert_to_markdown(path: str, start: int = 1, end: int = 0) -> str:
        from tadamoo import markitdown_tool
        try:
            src = read_workspace_file(path)
            if not src.is_file():
                return f"{path} is not a file."
        except (OSError, PermissionError) as exc:
            return f"Could not read {path}: {exc}"
        if not markitdown_tool.available():
            return "The document converter (MarkItDown) is not installed here; use the file tools that read this kind of file."
        text, note = markitdown_tool.convert(src, src.name)
        if text is None:
            return {"unsupported": f"{src.name} is not a kind of file the converter reads (Word, PowerPoint, Excel, PDF, web page, e-book, CSV, JSON, XML)."}.get(note, f"The converter could not read {src.name} ({note}).")
        outline = markitdown_tool.outline(text) or "(none)"
        head = f"{src.name} as Markdown, {len(text.splitlines())} lines. Headings:\n{outline}\n\n"
        return head + read_range(text, start, end or None, read_chars)

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    registry.register("list_attachments", "Lists the files the person attached to this conversation, with what each is and how to read it.",
                      obj({}, []), EffectClass.PURE, list_attachments, untrusted=True)
    registry.register("read_attachment", "Reads lines of an attached file (text read from the file, whatever its format). Use start and end to go on.",
                      obj({"id": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["id"]), EffectClass.PURE, read_attachment, untrusted=True)
    registry.register("query_attachment", "Asks a table file (CSV, TSV, Excel) a question in SQL: a SELECT over its sheets, as tables whose first row gives the column names "
                      "(numbers are numbers). Use it for a value, a count, a total, a filter or a sort; it never loads the table into the conversation. Errors name the tables and columns.",
                      obj({"id": {"type": "string"}, "sql": {"type": "string", "description": "One SELECT statement"}}, ["id", "sql"]), EffectClass.PURE,
                      query_attachment, untrusted=True)
    registry.register("search_attachment", "Finds the passages of an attached file that match a question or words (five at most, with their line numbers).",
                      obj({"id": {"type": "string"}, "query": {"type": "string"}}, ["id", "query"]), EffectClass.PURE, search_attachment, untrusted=True)
    registry.register("convert_to_markdown", "Reads a Word, PowerPoint, Excel, PDF, web page, e-book, CSV, JSON or XML file of the workspace as Markdown (headings, lists, tables kept), made by program with no model. Use start and end to go on.",
                      obj({"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]), EffectClass.PURE, convert_to_markdown, untrusted=True)
    registry.register("transcribe_attachment", "Writes down what is said in an attached audio or video file, with the local callAPIcall service. It can take minutes.",
                      obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, transcribe_attachment, untrusted=True)
    registry.register("write_output_file", "Makes a text file (txt, md, csv, json, html, code) for the person and offers it to them to save. It is opened again and checked.",
                      obj({"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]), EffectClass.REVERSIBLE, write_output_file)
    registry.register("publish_output_file", "Offers a file you made in the workspace (docx, xlsx, pdf, image...) to the person to save. It is opened again and checked first.",
                      obj({"path": {"type": "string"}}, ["path"]), EffectClass.REVERSIBLE, publish_output_file)
