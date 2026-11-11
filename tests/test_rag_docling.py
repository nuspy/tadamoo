"""The Docling worker as a subprocess (with stand-in workers: no model is loaded), the fall-back to the basic readers, the long job that adds a document,
the `read_document` tool with its cache and its pages, and the knowledge index in the `[state]` note."""
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from serve.rag_jobs import Jobs
from tadamoo import awareness, capstate
from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag.ingest import docling_client, formats, read_document
from tadamoo.rag.ingest import reading as rd
from tadamoo.rag.ingest.docling_client import Cancelled
from tadamoo.rag.ingest.reading import read_pages, register_document_tools
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.tools import knowledge_index
from tadamoo.tools.registry import ToolRegistry

PROJ = project_key("docling")


def worker(tmp_path, body, name="w.py"):
    path = tmp_path / name
    path.write_text("import json, sys, time\n\n\ndef emit(o):\n    sys.stdout.write(json.dumps(o) + '\\n')\n    sys.stdout.flush()\n\n\n" + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


OK = """
emit({"event": "start", "format": ".pdf", "pages": 3, "device": "cpu"})
emit({"event": "blocks", "blocks": [{"type": "heading", "text": "Patent", "page": 1, "level": 1}, {"type": "paragraph", "text": "First page text about reprogramming cells.", "page": 1}]})
emit({"event": "progress", "page": 2, "pages": 3, "device": "cpu"})
emit({"event": "blocks", "blocks": [{"type": "page_break", "text": "", "page": 3}, {"type": "table", "text": "| a | b |\\n| --- | --- |\\n| 1 | 2 |", "page": 3}]})
emit({"event": "done", "pages": 3, "device": "cpu"})
"""


def test_the_worker_prints_progress_and_blocks_and_the_client_collects_them(tmp_path):
    seen = []
    got = docling_client.read("", tmp_path / "x.pdf", ".pdf", progress=seen.append, command=worker(tmp_path, OK))
    assert got["pages"] == 3 and got["device"] == "cpu" and [b["type"] for b in got["blocks"]] == ["heading", "paragraph", "page_break", "table"]
    assert [e["event"] for e in seen] == ["start", "progress", "done"] and seen[1]["page"] == 2


def test_a_worker_that_fails_or_stops_early_or_says_nothing_is_an_error_not_a_partial_document(tmp_path):
    with pytest.raises(RagError, match="could not read the file: ValueError: bad"):
        docling_client.read("", tmp_path / "x", ".pdf", command=worker(tmp_path, 'emit({"event": "error", "message": "ValueError: bad"})'))
    with pytest.raises(RagError, match="ended before the document was read"):
        docling_client.read("", tmp_path / "x", ".pdf", command=worker(tmp_path, 'emit({"event": "blocks", "blocks": [{"type": "paragraph", "text": "half", "page": 1}]})', "half.py"))
    with pytest.raises(RagError, match="stopped answering"):
        docling_client.read("", tmp_path / "x", ".pdf", idle_s=0.6, command=worker(tmp_path, "time.sleep(30)", "hang.py"))


def test_a_cancel_ends_the_worker_and_nothing_is_kept(tmp_path):
    cancel = threading.Event()
    slow = worker(tmp_path, 'emit({"event": "start", "pages": 9})\nfor i in range(100):\n    emit({"event": "progress", "page": i, "pages": 100})\n    time.sleep(0.1)\n', "slow.py")
    threading.Timer(0.5, cancel.set).start()
    began = time.monotonic()
    with pytest.raises(Cancelled):
        docling_client.read("", tmp_path / "x", ".pdf", cancel=cancel, command=slow)
    assert time.monotonic() - began < 8


def test_the_python_of_docling_is_the_setting_else_the_gpu_environment_else_the_cpu_one(tmp_path, monkeypatch):
    monkeypatch.delenv("TADAMOO_DOCLING", raising=False)
    gpu, cpu = tmp_path / "docling-gpu" / "Scripts", tmp_path / "docling" / "Scripts"
    for folder in (gpu, cpu):
        folder.mkdir(parents=True)
        (folder / "python.exe").write_text("", encoding="utf-8")
    roots = (str(tmp_path),)
    assert docling_client.find_python("", roots) == str(gpu / "python.exe")
    (gpu / "python.exe").unlink()
    assert docling_client.find_python("", roots) == str(cpu / "python.exe")
    assert docling_client.find_python(str(cpu / "python.exe"), ()) == str(cpu / "python.exe") and docling_client.find_python(str(tmp_path / "none.exe"), roots) == ""
    assert docling_client.find_python("", (str(tmp_path / "nothing"),)) == ""
    monkeypatch.setenv("TADAMOO_DOCLING", "off")
    assert docling_client.find_python("", roots) == ""


def test_formats_grow_with_docling_and_a_format_only_docling_reads_is_refused_without_it(tmp_path):
    assert ".xlsx" not in formats(None, "") and ".pdf" in formats(None, "")
    assert {".xlsx", ".pptx", ".png"} <= set(formats(None, "python"))
    sheet = tmp_path / "t.xlsx"
    sheet.write_bytes(b"PK\x03\x04" + b"0" * 30)
    with pytest.raises(RagError, match="not a supported format"):
        read_document(sheet, "t.xlsx", None, python="")


def test_when_docling_fails_on_a_format_the_basic_readers_have_they_read_it_and_say_why(tmp_path, monkeypatch):
    doc = tmp_path / "a.md"
    doc.write_text("# Title\n\nSome words in a document about harbours and tides.", encoding="utf-8")
    monkeypatch.setattr(docling_client, "read", lambda *a, **k: (_ for _ in ()).throw(RagError("Docling could not read the file: boom")))
    got = read_document(doc, "a.md", None, python=sys.executable)
    assert got["extraction"] == "basic" and "boom" in got["notes"][0] and got["blocks"][0]["type"] == "heading"
    sheet = tmp_path / "t.xlsx"
    sheet.write_bytes(b"PK\x03\x04" + b"0" * 30)
    with pytest.raises(RagError, match="boom"):
        read_document(sheet, "t.xlsx", None, python=sys.executable)                                 # no basic reader to fall back on
    import tadamoo.rag.ingest as ingest
    monkeypatch.setattr(ingest, "basic_blocks", lambda *a, **k: (_ for _ in ()).throw(RagError("no PDF reader here")))
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(RagError, match="boom.*no PDF reader here"):
        read_document(pdf, "p.pdf", None, python=sys.executable)                                    # both reasons reach the person


def stub_docling(monkeypatch, pages=3):
    calls = []

    def read(python, path, ext, progress=None, cancel=None, **kw):
        calls.append(ext)
        for page in range(1, pages + 1):
            if progress:
                progress({"event": "progress", "page": page, "pages": pages, "device": "cuda"})
        blocks = []
        for page in range(1, pages + 1):
            blocks += [{"type": "page_break", "text": "", "page": page}, {"type": "heading", "text": f"Chapter {page}", "page": page, "level": 1},
                       {"type": "paragraph", "text": f"The text of page {page} describes the lighthouse number {page * 13} and its keeper.", "page": page}]
        return {"blocks": blocks, "pages": pages, "device": "cuda"}
    monkeypatch.delenv("TADAMOO_DOCLING", raising=False)
    monkeypatch.setattr(docling_client, "read", read)
    return calls


def test_a_document_is_added_by_a_job_that_reports_its_pages_and_leaves_the_archive_alone_when_it_fails(tmp_path, monkeypatch):
    stub_docling(monkeypatch, pages=4)
    store = RagStore(tmp_path / "rag")
    store.settings.write({"docling_python": sys.executable})
    aid = store.create("Scans", "", PROJ)["id"]
    scan = tmp_path / "plan.xlsx"
    scan.write_bytes(b"PK\x03\x04 pretend sheet")
    jobs, steps = Jobs(), []
    job = jobs.start("add plan.xlsx", lambda progress, cancel: store.add_file(aid, "plan.xlsx", scan, lambda e: (steps.append(dict(e)), progress(e)), cancel), aid)
    done = jobs.wait(job["id"])
    assert done["state"] == "done" and done["page"] == 4 and done["pages"] == 4 and done["device"] == "cuda" and done["stage"] == "indexing"
    doc = done["result"]["document"]
    assert doc["extraction"] == "docling" and doc["pages"] == 4 and store.get(aid)["extraction"] == "docling"
    assert [s["stage"] for s in steps if s["event"] == "stage"] == ["reading", "cutting", "indexing"]
    hit = store.search([aid], "lighthouse keeper number 26")["hits"][0]
    assert hit["page_start"] == 2 and hit["path"].endswith("Chapter 2")
    before = store.get(aid)
    bad = jobs.start("add again", lambda progress, cancel: store.add_file(aid, "plan.xlsx", scan, progress, cancel), aid)
    failed = jobs.wait(bad["id"])
    assert failed["state"] == "failed" and "already in the archive" in failed["error"] and store.get(aid) == before


def test_a_cancelled_job_adds_nothing(tmp_path, monkeypatch):
    stub_docling(monkeypatch)
    store = RagStore(tmp_path / "rag")
    store.settings.write({"docling_python": sys.executable})
    aid = store.create("Scans", "", PROJ)["id"]
    scan = tmp_path / "plan.xlsx"
    scan.write_bytes(b"PK\x03\x04 pretend sheet")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RagError) as e:
        store.add_file(aid, "plan.xlsx", scan, None, cancel)
    assert e.value.code == "cancelled" and store.get(aid)["documents"] == 0 and not list((tmp_path / "rag" / aid / "blocks").glob("*"))


# -- read_document -------------------------------------------------------------------------------------------------

BOOK = "\n\n<!-- page -->\n\n".join(f"## Chapter {i}\n\nThe words of chapter {i} tell about the north wind number {i * 17}." for i in range(1, 7))


def tool(tmp_path, monkeypatch=None):
    registry = ToolRegistry()
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    register_document_tools(registry, root, tmp_path / "rag", tmp_path / "cache")
    return registry, root


def test_read_document_gives_the_pages_asked_for_with_markers_and_the_total(tmp_path):
    registry, root = tool(tmp_path)
    (root / "book.md").write_text(BOOK, encoding="utf-8")
    out = registry.execute("read_document", {"path": "book.md", "pages": "2-3"})
    assert out["status"] == "ok" and out["pages_total"] == 6 and out["pages"] == [2, 3] and out["extraction"] == "basic"
    assert "<!-- page 2 -->" in out["text"] and "chapter 3" in out["text"] and "chapter 1" not in out["text"] and "chapter 4" not in out["text"]
    assert "pages 4-6 have not been read" in out["note"]
    assert registry.execute("read_document", {"path": "book.md", "pages": "5-"})["pages"] == [5, 6]
    assert "chapter 2" in registry.execute("read_document", {"path": str(root / "book.md"), "pages": "2", "format": "text"})["text"]
    assert registry.get("read_document").untrusted is True and registry.get("read_document").effect.value == "pure"


def test_the_reading_is_cached_by_the_file_content_and_the_reader(tmp_path, monkeypatch):
    registry, root = tool(tmp_path)
    (root / "book.md").write_text(BOOK, encoding="utf-8")
    calls = []
    real = rd.read_document
    monkeypatch.setattr(rd, "read_document", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    registry.execute("read_document", {"path": "book.md", "pages": "1"})
    registry.execute("read_document", {"path": "book.md", "pages": "4"})
    assert len(calls) == 1 and len(list((tmp_path / "cache").glob("*.json.gz"))) == 1
    (root / "book.md").write_text(BOOK + "\n\nAn added line to the last page of the book.", encoding="utf-8")
    registry.execute("read_document", {"path": "book.md", "pages": "6"})
    assert len(calls) == 2                                                                           # another content: read again
    stub_docling(monkeypatch)
    (tmp_path / "rag").mkdir(exist_ok=True)
    RagStore(tmp_path / "rag").settings.write({"docling_python": sys.executable})
    assert registry.execute("read_document", {"path": "book.md", "pages": "1"})["extraction"] == "docling"       # another reader: another entry


def test_read_document_refuses_what_it_should_and_says_so_in_words(tmp_path):
    registry, root = tool(tmp_path)
    (root / "a.exe").write_bytes(b"MZ")
    (root / "secret.md").write_text("# Secret\n\nA secret of the person kept here in the vault folder.", encoding="utf-8")
    for args, word in (({"path": "missing.md"}, "no such file"), ({"path": "a.exe"}, "not a format"), ({"path": ""}, "path"), ({"path": "secret.md", "pages": "x-y"}, "pages")):
        out = registry.execute("read_document", args)
        assert out["status"] == "error" and word in out["note"], args
    guarded = ToolRegistry()
    register_document_tools(guarded, root, None, tmp_path / "cache2", protected=lambda p: "vault" if p.name == "secret.md" else "")
    assert "secrets" in guarded.execute("read_document", {"path": "secret.md"})["note"]
    assert read_pages(root / "secret.md", tmp_path / "cache3", {"docling_python": "", "flagged": "keep"})["pages_total"] >= 1


def test_the_description_tells_a_model_when_to_read_a_document_in_any_language(tmp_path):
    registry, _ = tool(tmp_path)
    text = registry.get("read_document").description
    for word in ("scanned PDFs", "Word, Excel and PowerPoint", "OCR", "leggi questo PDF", "was steht", "pages"):
        assert word in text


# -- what the model is told ----------------------------------------------------------------------------------------

def test_the_state_note_lists_the_archives_the_conversation_can_use_and_tells_when_they_change(tmp_path):
    store = RagStore(tmp_path / "rag")
    mine = store.create("Boats", "Everything about small boats and sailing", PROJ, "Here")["id"]
    other = store.create("Forests", "", project_key("else"), "Else")["id"]
    recall = ProjectRecall(store, PROJ)
    index = knowledge_index(store, recall)
    assert index().startswith("Boats (0 documents, text search; Everything about small boats")
    first = index()
    assert index() is first                                                                          # worked out again only when something changed
    recall.attach([other])
    assert "Forests (project Else; 0 documents, text search)" in index()
    for i in range(14):
        store.create(f"Extra {i}", "", PROJ)
    assert index().endswith("and 4 more") and index().count("documents") == 12
    from types import SimpleNamespace
    agent = SimpleNamespace(sampling={}, permissions=None, max_turns=0, tools=SimpleNamespace(get=lambda n: None), knowledge_index=index)
    state = capstate.read(agent, window=4096, unattended=False)
    teller = capstate.Teller()
    note = teller.note(state)
    assert "knowledge=Boats" in note and teller.note(state) == ""
    store.update(mine, {"description": "Changed words"})
    assert "knowledge=" in teller.note(capstate.read(agent, window=4096, unattended=False))
    bare = SimpleNamespace(sampling={}, permissions=None, max_turns=0, tools=SimpleNamespace(get=lambda n: None))
    assert "knowledge" not in capstate.read(bare, window=4096, unattended=False)


def test_the_self_description_says_documents_are_read_with_read_document_and_archives_are_listed_in_the_note():
    kw = dict(model="", window=4096, mask_at=0.5, compaction_at=0.8, compaction=True, max_turns=10, max_tokens=None, max_seconds=None, absent=[], lessons="")
    text = awareness.self_model(**kw, knowledge=True, documents=True)
    assert "read_document" in text and "OCR" in text and "`knowledge=`" in text
    assert "read_document" not in awareness.self_model(**kw)


def test_the_harness_offers_read_document_in_every_role_and_lists_the_archives_in_its_note(tmp_path):
    from tadamoo.stack import AgentConfig, build_agent
    store = RagStore(tmp_path / "rag")
    store.create("Boats", "Small boats", PROJ, "Here")
    (tmp_path / "w").mkdir()
    for role in ("build", "ask", "plan", "general"):
        built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, rag_dir=str(tmp_path / "rag"),
                                        rag_project=PROJ, role=role))
        try:
            assert "read_document" in built.role.allowed() and "read_document" in built.builder.blocks["self"]
            assert "knowledge=Boats (0 documents, text search; Small boats)" in built.agent.state_note()
        finally:
            built.close()


# A stand-in worker that reads "pages" one at a time and dies on the pages in DIE (as many times as the run numbers in DIE_RUNS allow).
FLAKY = """
import os
args = sys.argv
flag = lambda n: args[args.index(n) + 1] if n in args else ""
first = int(flag("--from-page") or 1)
skip = {int(n) for n in flag("--skip").split(",") if n}
log = os.environ["FLAKY_LOG"]
open(log, "a").write(" ".join(a for a in args[1:] if a.startswith("--") and not a.startswith("--file")) + "\\n")
emit({"event": "start", "pages": 6, "device": "cpu", "from_page": first})
for p in range(first, 7):
    if p in skip:
        continue
    if p == DIE and not (("--paged" in args and "--cpu" in args) and HEAL):
        os._exit(1)
    emit({"event": "blocks", "blocks": [{"type": "paragraph", "text": "text of page %d" % p, "page": p}]})
    emit({"event": "progress", "page": p, "pages": 6, "device": "cpu"})
emit({"event": "done", "pages": 6, "device": "cpu"})
"""


def flaky(tmp_path, die, heal):
    return worker(tmp_path, f"DIE, HEAL = {die}, {heal}\n" + FLAKY, "flaky.py")


def test_a_worker_that_dies_part_way_is_started_again_where_it_stopped_and_what_was_read_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_LOG", str(tmp_path / "runs.log"))
    got = docling_client.read("", tmp_path / "x.pdf", ".pdf", command=flaky(tmp_path, 4, True))
    runs = (tmp_path / "runs.log").read_text().splitlines()
    assert runs == ["", "--paged --from-page", "--paged --cpu --from-page"]          # as it was, page by page, page by page on the CPU
    assert [b["text"] for b in got["blocks"]] == [f"text of page {p}" for p in range(1, 7)] and got["skipped"] == {}


def test_a_page_that_fails_three_times_is_skipped_with_a_marker_and_the_reading_goes_on(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_LOG", str(tmp_path / "runs.log"))
    got = docling_client.read("", tmp_path / "x.pdf", ".pdf", command=flaky(tmp_path, 4, False))
    assert list(got["skipped"]) == [4] and "reader ended" in got["skipped"][4]
    text = [b["text"] for b in got["blocks"] if b["type"] == "paragraph"]
    assert text == ["text of page 1", "text of page 2", "text of page 3", "[Page 4 of this document could not be read automatically and is missing from this text.]",
                    "text of page 5", "text of page 6"]
    assert [b["page"] for b in got["blocks"] if b["type"] == "page_break"] == [4]
    assert len((tmp_path / "runs.log").read_text().splitlines()) == 4          # three tries on page 4, then the rest after it


def test_a_worker_that_never_starts_is_an_error_and_no_page_is_skipped(tmp_path):
    with pytest.raises(RagError, match="could not read the file: ImportError"):
        docling_client.read("", tmp_path / "x.pdf", ".pdf", command=worker(tmp_path, 'emit({"event": "error", "message": "ImportError: no docling"})', "late.py"))


def test_the_skipped_pages_reach_the_notes_and_the_document_of_the_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(docling_client, "read", lambda *a, **k: {"blocks": [{"type": "paragraph", "text": "text of page 1 about cells", "page": 1}], "pages": 3,
                                                                  "device": "cpu", "skipped": {2: "boom", 3: "boom"}})
    got = read_document(tmp_path / "a.pdf", "a.pdf", {}, python="x")
    assert got["skipped_pages"] == [2, 3] and "2 pages could not be read after three tries and are missing: 2, 3" in got["notes"][0]


def worker_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("docling_worker_under_test", docling_client.WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_worker_cuts_pages_in_windows_and_leaves_out_the_skipped_ones():
    w = worker_module()
    assert w.spans(1, 10, 4, set()) == [(1, 4), (5, 8), (9, 10)]
    assert w.spans(3, 10, 4, {5, 6}) == [(3, 4), (7, 10)] and w.spans(1, 3, 1, {2}) == [(1, 1), (3, 3)]


def test_a_page_is_tried_again_then_with_a_new_converter_then_on_the_cpu(monkeypatch):
    w = worker_module()
    built = []
    monkeypatch.setattr(w, "build_converter", lambda device: built.append(device) or device)
    monkeypatch.setattr(w, "free_memory", lambda: None)
    reader = w.Reader("x.pdf", "cuda")
    calls = []

    def convert(first, last):
        calls.append(reader.device)
        if len(calls) < 3:
            raise RuntimeError("out of memory")
        return [{"type": "paragraph", "text": "ok", "page": first}]
    monkeypatch.setattr(reader, "convert", convert)
    assert reader.page(7) == ([{"type": "paragraph", "text": "ok", "page": 7}], "") and calls == ["cuda", "cuda", "cpu"] and built == ["cuda", "cuda", "cpu"]
    monkeypatch.setattr(reader, "convert", lambda a, b: (_ for _ in ()).throw(ValueError("bad page")))
    got, message = reader.page(8)
    assert got is None and message == "ValueError: bad page"
