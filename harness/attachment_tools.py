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

from harness.attachments import AttachmentStore, OutputStore, manifest, read_range, search_chunks
from harness.integrations._http import ServiceError

MAX_OUTPUT_BYTES = 200_000_000
TRANSCRIPT_SHOWN = 6000


def register_attachment_tools(registry: Any, store: AttachmentStore, outputs: OutputStore, session: str, read_workspace_file: Callable[[str], Path],
                              call_client: Any = None, read_chars: int = 12000, wait_s: float = 900.0,
                              sleep: Callable[[float], None] = time.sleep) -> None:
    from harness.tools.registry import EffectClass

    def known(att_id: str) -> Dict[str, Any]:
        return store.get(session, str(att_id))

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
            return f"No attachment '{id}'. list_attachments shows the ids."
        text, note = store.text(session, meta["id"])
        if text is None:
            return manifest(meta, None, note)
        if meta["family"] == "data" and start <= 1 and not end and len(text) > read_chars * 3:
            return "This is a large table. Read it in ranges with start and end, or search it; do not add up its rows yourself.\n" + read_range(text, 1, 60, read_chars)
        return read_range(text, start, end or None, read_chars)

    def search_attachment(id: str, query: str) -> str:
        try:
            meta = known(id)
        except KeyError:
            return f"No attachment '{id}'. list_attachments shows the ids."
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
            return f"No attachment '{id}'. list_attachments shows the ids."
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
            tmp = Path(tempfile.mkdtemp(prefix="basalt-media-"))
            try:
                copy = tmp / meta["name"]
                shutil.copyfile(folder / "file", copy)
                try:
                    res = call_client.start_media_analysis(str(copy), title=meta["name"])
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
        if state.get("state") == "error":
            job_file.unlink(missing_ok=True)
            return f"The service could not transcribe it: {state.get('error') or 'unknown error'}."
        from harness.integrations.call_api_client import format_transcript
        segments = call_client.media_transcript(job) or []
        transcript = format_transcript(segments)
        if not transcript:
            return "The service finished but the recording has no speech it could write down."
        store._write(folder, "text.txt", transcript)
        return (f"Transcript ({len(segments)} segments). The service's own summary is not used.\n" + transcript[:TRANSCRIPT_SHOWN] +
                ("\n[...] read the rest with read_attachment." if len(transcript) > TRANSCRIPT_SHOWN else ""))

    def finish(meta: Dict[str, Any]) -> str:
        mark = f"[file:{meta['id']}:{meta['name']}]"
        if meta["verified"]:
            return f"{mark} Saved for the person: {meta['name']} ({meta['check']}). They get a button to save it where they want. Do not describe it as saved elsewhere."
        return f"{mark} The file was written but it did not pass the check: {meta['check']}. Fix it and write it again before telling the person it is done."

    def write_output_file(name: str, content: str) -> str:
        data = str(content).encode("utf-8")
        if len(data) > MAX_OUTPUT_BYTES:
            return "That file is too large to hand over this way."
        return finish(outputs.save(session, name, data))

    def publish_output_file(path: str) -> str:
        try:
            src = read_workspace_file(path)
            if not src.is_file():
                return f"{path} is not a file."
            if src.stat().st_size > MAX_OUTPUT_BYTES:
                return "That file is too large to hand over this way."
            return finish(outputs.save(session, src.name, src.read_bytes()))
        except (OSError, PermissionError) as exc:
            return f"Could not read {path}: {exc}"

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}
    registry.register("list_attachments", "Lists the files the person attached to this conversation, with what each is and how to read it.",
                      obj({}, []), EffectClass.PURE, list_attachments, untrusted=True)
    registry.register("read_attachment", "Reads lines of an attached file (text read from the file, whatever its format). Use start and end to go on.",
                      obj({"id": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["id"]), EffectClass.PURE, read_attachment, untrusted=True)
    registry.register("search_attachment", "Finds the passages of an attached file that match a question or words (five at most, with their line numbers).",
                      obj({"id": {"type": "string"}, "query": {"type": "string"}}, ["id", "query"]), EffectClass.PURE, search_attachment, untrusted=True)
    registry.register("transcribe_attachment", "Writes down what is said in an attached audio or video file, with the local callAPIcall service. It can take minutes.",
                      obj({"id": {"type": "string"}}, ["id"]), EffectClass.REVERSIBLE, transcribe_attachment, untrusted=True)
    registry.register("write_output_file", "Makes a text file (txt, md, csv, json, html, code) for the person and offers it to them to save. It is opened again and checked.",
                      obj({"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"]), EffectClass.REVERSIBLE, write_output_file)
    registry.register("publish_output_file", "Offers a file you made in the workspace (docx, xlsx, pdf, image...) to the person to save. It is opened again and checked first.",
                      obj({"path": {"type": "string"}}, ["path"]), EffectClass.REVERSIBLE, publish_output_file)
