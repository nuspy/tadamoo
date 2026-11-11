"""Coverage of each reading and the verify of a whole archive."""
import json
import os

from sources_support import Counting, body, doc_of, make
from tadamoo.rag.sources import coverage, verify as verifying
from tadamoo.rag.sources.local import sha_of


def chunk(a, b, text="some passage text of a page", flag=""):
    return {"page_start": a, "page_end": b, "text": text, **({"flag": flag} if flag else {})}


def test_every_page_with_a_passage_is_covered_and_the_rest_is_named():
    doc = {"pages": 6, "skipped_pages": [4], "empty_pages": [5]}
    cov = coverage.of_document(doc, [chunk(1, 2), chunk(2, 3), chunk(6, 6, flag="instructions")])
    assert cov["covered"] == 4 and cov["uncovered"] == [] and cov["skipped"] == [4] and cov["empty"] == [5] and cov["flagged"] == 1
    assert cov["status"] == coverage.INCOMPLETE and len(cov["problems"]) == 2


def test_a_page_nobody_explains_is_listed_as_uncovered():
    cov = coverage.of_document({"pages": 5}, [chunk(1, 1), chunk(2, 2), chunk(5, 5)])
    assert cov["uncovered"] == [3, 4] and cov["status"] == coverage.CHECK and "3-4" in cov["problems"][0]


def test_a_document_without_pages_or_passages():
    assert coverage.of_document({"pages": 0}, [chunk(0, 0)])["status"] == coverage.OK
    empty = coverage.of_document({"pages": 3}, [])
    assert empty["status"] == coverage.INCOMPLETE and "no passage" in empty["problems"][0]


def test_the_archive_sums_its_documents_and_failures():
    docs = [{"id": "d1", "name": "a", "coverage": {"status": "ok", "pages": 10, "covered": 10, "flagged": 0, "problems": []}},
            {"id": "d2", "name": "b", "coverage": {"status": "check", "pages": 4, "covered": 3, "flagged": 2, "problems": ["1 page hold no passage"]}},
            {"id": "d3", "name": "c"}]
    total = coverage.of_archive(docs, [])
    assert total["pages"] == 14 and total["covered"] == 13 and total["unknown"] == 1 and total["status"] == coverage.CHECK and total["problems"][0]["name"] == "b"
    assert coverage.of_archive(docs, [{"name": "x", "reason": "boom"}])["status"] == coverage.INCOMPLETE


def test_a_document_is_added_with_its_coverage(tmp_path):
    store, sync, aid = make(tmp_path)
    p = tmp_path / "a.md"
    p.write_text(body(1, "alpha"), encoding="utf-8")
    doc = store.add_file(aid, "a.md", p)["document"]
    assert doc["coverage"]["status"] == "ok" and doc["coverage"]["passages"] == doc["chunks"]
    assert store.get(aid)["document_list"][0]["coverage"]["flagged"] == 0


def healthy(tmp_path):
    emb = Counting()
    store, sync, aid = make(tmp_path, emb)
    src = tmp_path / "docs"
    src.mkdir()
    (src / "alpha.md").write_text(body(1, "alpha"), encoding="utf-8")
    (src / "beta.md").write_text(body(1, "beta"), encoding="utf-8")
    sync.add_source(aid, "folder", {"path": str(src)})
    sync.update(aid)
    return store, sync, aid, src


def kinds(report):
    return [(p["level"], p["text"][:40]) for p in report["problems"]]


def test_a_healthy_archive_verifies(tmp_path):
    store, sync, aid, src = healthy(tmp_path)
    report = verifying.verify(sync, aid, deep=True)
    assert report["ok"] and report["errors"] == 0 and report["problems"] == []
    assert report["documents"] == 2 and report["coverage"]["status"] == "ok" and report["coverage"]["ok"] == 2


def test_a_cut_short_update_is_found(tmp_path):
    store, sync, aid, src = healthy(tmp_path)
    f = store.folder(aid) / "chunks.jsonl"
    rows = f.read_text(encoding="utf-8").splitlines()
    f.write_text("\n".join(rows[:-2]) + "\n", encoding="utf-8")
    store._cache.clear()
    report = verifying.verify(sync, aid)
    assert not report["ok"]
    assert any("holds" in p["text"] and p["level"] == "error" for p in report["problems"])
    assert any("counts" in p["text"] and p["name"] for p in report["problems"])


def test_a_missing_saved_reading_and_a_wrong_coverage_record_are_found(tmp_path):
    store, sync, aid, src = healthy(tmp_path)
    alpha, beta = doc_of(store, aid, "alpha"), doc_of(store, aid, "beta")
    (store.folder(aid) / "blocks" / (alpha["id"] + ".json.gz")).unlink()
    meta = json.loads((store.folder(aid) / "meta.json").read_text(encoding="utf-8"))
    next(d for d in meta["documents"] if d["id"] == beta["id"])["coverage"]["passages"] = 99
    (store.folder(aid) / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    store._cache.clear()
    report = verifying.verify(sync, aid)
    texts = " | ".join(p["text"] for p in report["problems"])
    assert "saved reading" in texts and "coverage record" in texts and not report["ok"]


def test_a_deep_verify_cuts_the_readings_again_and_compares(tmp_path):
    store, sync, aid, src = healthy(tmp_path)
    f = store.folder(aid) / "chunks.jsonl"
    rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
    rows[0]["text"] = rows[0]["text"] + " tampered"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    store._cache.clear()
    assert verifying.verify(sync, aid, deep=False)["ok"]
    deep = verifying.verify(sync, aid, deep=True)
    assert not deep["ok"] and any("saved reading gives" in p["text"] for p in deep["problems"])


def test_the_sources_are_compared_and_documents_without_one_are_counted(tmp_path):
    store, sync, aid, src = healthy(tmp_path)
    extra = tmp_path / "hand.md"
    extra.write_text(body(1, "gamma"), encoding="utf-8")
    store.add_file(aid, "hand.md", extra)
    (src / "alpha.md").write_text(body(2, "alpha"), encoding="utf-8")
    (src / "beta.md").unlink()
    report = verifying.verify(sync, aid)
    texts = [p["text"] for p in report["problems"]]
    assert any("newer version" in t for t in texts) and any("no longer has" in t for t in texts) and any("came from no source" in t for t in texts)
    assert report["out_of_date"] == 2 and report["ok"]                         # out of date is a warning, not a damaged archive


def test_a_deep_verify_hashes_local_files_and_counts_pdf_pages(tmp_path, monkeypatch):
    store, sync, aid, src = healthy(tmp_path)
    path = src / "alpha.md"
    st = path.stat()
    path.write_text(body(1, "alpha").replace("Alpha", "Omega"), encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))                         # same time and size: only the hash can tell
    assert path.stat().st_size == st.st_size
    report = verifying.verify(sync, aid, deep=True)
    assert any("content differs" in p["text"] for p in report["problems"])
    monkeypatch.setattr(verifying, "pdf_pages", lambda p: 7)
    pdf = src / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    store.annotate_document(aid, doc_of(store, aid, "beta")["id"], {"pages": 3, "sha256": sha_of(pdf)})
    sync.sources(aid)
    link = sync.state(aid).read()["links"][doc_of(store, aid, "beta")["id"]]
    sync.state(aid).change(lambda d: d["links"].__setitem__(doc_of(store, aid, "beta")["id"], {**link, "key": "paper.pdf"}))
    report = verifying.verify(sync, aid, deep=True, sources=False)
    assert any("the PDF has 7 pages" in p["text"] for p in report["problems"])
