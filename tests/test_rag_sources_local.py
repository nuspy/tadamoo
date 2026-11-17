"""Refreshing an archive from a file or a folder on this PC: what a check sees, what an update re-reads, the vectors that are kept, the versions, the documents that go."""
import os
import threading
import time

import pytest

from sources_support import Counting, body, doc_of, make
from tadamoo.rag import RagError


def put(folder, name, text, mtime=None):
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def states(report):
    return {i["name"]: i["state"] for i in report["items"]}


def folder_archive(tmp_path, embedder=None, **kw):
    store, sync, aid = make(tmp_path, embedder, **kw)
    src = tmp_path / "docs"
    put(src, "alpha.md", body(1, "alpha"), 1_700_000_000)
    put(src, "beta.md", body(1, "beta"), 1_700_000_000)
    put(src, "sub/gamma.txt", "Gamma plain text. " + "The wind carries the boat. " * 20, 1_700_000_000)
    sid = sync.add_source(aid, "folder", {"path": str(src)})["id"]
    return store, sync, aid, src, sid


def test_a_source_is_validated_and_nothing_is_read_when_it_is_added(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    assert store.get(aid)["documents"] == 0
    assert sync.sources(aid)["sources"][0]["kind"] == "folder"
    with pytest.raises(RagError, match="already"):
        sync.add_source(aid, "folder", {"path": str(src)})
    with pytest.raises(RagError, match="no such folder"):
        sync.add_source(aid, "folder", {"path": str(tmp_path / "nope")})
    with pytest.raises(RagError, match="no such file"):
        sync.add_source(aid, "file", {"path": str(tmp_path / "nope.md")})
    with pytest.raises(RagError, match="kind"):
        sync.add_source(aid, "ftp", {"path": "x"})


def test_a_source_inside_the_data_folder_is_refused(tmp_path):
    secret = tmp_path / "data"
    secret.mkdir()
    put(secret, "vault.md", "secret")
    store, sync, aid = make(tmp_path, forbidden=[secret])
    with pytest.raises(RagError, match="data folder"):
        sync.add_source(aid, "folder", {"path": str(secret)})
    with pytest.raises(RagError, match="data folder"):
        sync.add_source(aid, "file", {"path": str(secret / "vault.md")})


def test_check_finds_new_files_then_update_adds_them_then_nothing_changed(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    first = sync.check(aid)
    assert first["counts"] == {"new": 3}
    assert store.get(aid)["documents"] == 0                      # a check reads nothing and changes nothing
    done = sync.update(aid)
    assert done["counts"] == {"added": 3}
    assert store.get(aid)["documents"] == 3
    assert states(sync.check(aid)) == {"alpha.md": "unchanged", "beta.md": "unchanged", "sub - gamma.txt": "unchanged"}
    assert sync.check(aid)["summary"] == "nothing changed"


def test_a_changed_file_is_read_again_and_only_the_new_text_is_embedded(tmp_path):
    emb = Counting()
    store, sync, aid, src, sid = folder_archive(tmp_path, emb)
    sync.update(aid)
    before = len(emb.texts)
    old_doc = doc_of(store, aid, "alpha")
    assert store.get(aid)["mode"] == "hybrid"
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_500)
    report = sync.check(aid)
    assert states(report)["alpha.md"] == "changed" and states(report)["beta.md"] == "unchanged"
    done = sync.update(aid)
    item = next(i for i in done["items"] if i["name"] == "alpha.md")
    assert item["state"] == "updated"
    assert item["passages"]["added"] >= 1 and item["passages"]["removed"] >= 1 and item["passages"]["kept"] >= 3
    embedded_now = len(emb.texts) - before
    assert embedded_now == item["passages"]["added"] and done["reused"] > 0         # unchanged passages keep their vectors
    new_doc = doc_of(store, aid, "alpha")
    assert new_doc["id"] == old_doc["id"] and new_doc["sha256"] != old_doc["sha256"] and new_doc["revision"] == 2 and new_doc["updated"] > 0
    assert store.get(aid)["mode"] == "hybrid" and store.load(aid).vectors.shape[0] == store.get(aid)["chunks"]
    hits = store.search([aid], "revised paragraph version 2 gamma", top_k=3)["hits"]
    assert hits and "version 2" in hits[0]["text"]
    assert sync.check(aid)["counts"] == {"unchanged": 3}
    # the replaced passages are not searchable any more
    assert all("version 1" not in h["text"] for h in store.search([aid], "alpha paragraph", top_k=20)["hits"])


def test_a_touched_file_with_the_same_content_is_not_read_again(tmp_path):
    emb = Counting()
    store, sync, aid, src, sid = folder_archive(tmp_path, emb)
    sync.update(aid)
    before = len(emb.texts)
    os.utime(src / "beta.md", (1_800_000_000, 1_800_000_000))
    report = sync.check(aid)
    assert states(report)["beta.md"] == "unchanged"
    assert "same" in next(i for i in report["items"] if i["name"] == "beta.md")["detail"]
    assert sync.update(aid)["counts"] == {"unchanged": 3}
    assert len(emb.texts) == before


def test_the_previous_version_is_kept_and_can_be_put_back_and_stays_put(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    doc = doc_of(store, aid, "alpha")
    old_sha = doc["sha256"]
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_500)
    sync.update(aid)
    versions = sync.versions(aid, doc["id"])
    assert [v["n"] for v in versions] == [1] and versions[0]["sha256"] == old_sha
    back = sync.rollback(aid, doc["id"], 1)
    assert back["restored"] == 1 and back["pinned"]
    assert doc_of(store, aid, "alpha")["sha256"] == old_sha
    assert [v["n"] for v in sync.versions(aid, doc["id"])] == [2, 1]                         # the version that was undone is kept too
    assert all("revised paragraph" not in c["text"] for c in store.load(aid).chunks)
    pinned = sync.check(aid)
    assert states(pinned)["alpha.md"] == "pinned"
    assert sync.update(aid)["counts"].get("updated") is None                                 # an update does not undo it
    assert doc_of(store, aid, "alpha")["sha256"] == old_sha
    forced = sync.update(aid, force=[doc["id"]])
    assert forced["counts"] == {"updated": 1, "unchanged": 2}
    assert doc_of(store, aid, "alpha")["sha256"] != old_sha
    assert sync.check(aid)["counts"] == {"unchanged": 3}


def test_versions_are_kept_in_a_limited_number(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.settings.write({"keep_versions": 2})
    sync.update(aid)
    doc = doc_of(store, aid, "alpha")
    for n in range(2, 6):
        put(src, "alpha.md", body(n, "alpha"), 1_700_000_000 + n * 100)
        sync.update(aid)
    kept = [v["n"] for v in sync.versions(aid, doc["id"])]
    assert kept == [4, 3]
    assert store._backend(aid).list_blobs(aid, "version") == [f"{doc['id']}/3", f"{doc['id']}/4"]
    sync.settings.write({"keep_versions": 0})
    put(src, "alpha.md", body(9, "alpha"), 1_700_009_000)
    sync.update(aid)
    assert sync.versions(aid, doc["id"]) == []


def test_a_file_that_goes_is_marked_and_never_removed_until_the_person_decides(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    (src / "beta.md").unlink()
    report = sync.check(aid)
    assert states(report)["beta.md"] == "gone"
    done = sync.update(aid)
    assert done["counts"]["gone"] == 1 and store.get(aid)["documents"] == 3
    beta = doc_of(store, aid, "beta")
    sync.resolve_gone(aid, beta["id"], "keep")
    kept = sync.check(aid)
    assert kept["counts"].get("kept") == 1 and "gone" not in kept["counts"]
    assert sync.sources(aid)["attention"]["counts"] == {}
    sync.resolve_gone(aid, beta["id"], "remove")
    assert store.get(aid)["documents"] == 2 and not any(d["id"] == beta["id"] for d in store.get(aid)["document_list"])
    assert sync.check(aid)["counts"] == {"unchanged": 2}


def test_an_update_marks_a_document_the_source_lost_as_a_past_version_and_the_citation_says_so_and_it_comes_back_when_the_file_does(tmp_path):
    from tadamoo.rag.recall import citation
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    beta = doc_of(store, aid, "beta")
    assert not beta.get("past")
    (src / "beta.md").rename(tmp_path / "beta.keep")
    sync.check(aid)
    assert not doc_of(store, aid, "beta").get("past")                                  # a check alone writes nothing
    sync.update(aid, dry_run=True)
    assert not doc_of(store, aid, "beta").get("past")
    done = sync.update(aid)
    marked = doc_of(store, aid, "beta")
    assert marked["past"]["note"] == "past version, no longer available at its source" and marked["past"]["since"] and store.get(aid)["documents"] == 3
    assert [i for i in done["items"] if i["name"] == "beta.md"][0]["marked"] is True
    hit = store.search([aid], "beta", top_k=3)["hits"][0]
    assert hit["document"] == "beta.md" and hit["past"] and citation(hit).endswith(", past version, no longer available]")
    assert not store.search([aid], "alpha", top_k=1)["hits"][0]["past"]
    (tmp_path / "beta.keep").rename(src / "beta.md")
    sync.update(aid)
    assert not doc_of(store, aid, "beta").get("past")


def test_a_failed_document_keeps_its_old_version_and_the_others_go_through(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    old = doc_of(store, aid, "alpha")
    put(src, "alpha.md", "x", 1_700_000_500)                       # too short to hold a text to search
    put(src, "beta.md", body(2, "beta"), 1_700_000_500)
    done = sync.update(aid)
    assert states(done)["alpha.md"] == "failed" and states(done)["beta.md"] == "updated"
    assert "no text" in next(i for i in done["items"] if i["name"] == "alpha.md")["detail"]
    assert doc_of(store, aid, "alpha")["sha256"] == old["sha256"]
    assert any("Alpha paragraph" in c["text"] or "Alpha" in c["text"] for c in store.load(aid).chunks if c["doc"] == old["id"])
    assert sync.sources(aid)["attention"]["counts"].get("failed") == 1
    assert sync.check(aid)["counts"]["changed"] == 1                        # it is still pending: the next run tries again


def test_a_cancelled_update_changes_nothing(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    chunks = store.get(aid)["chunks"]
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_500)
    stop = threading.Event()
    stop.set()
    with pytest.raises(RagError) as e:
        sync.update(aid, cancel=stop)
    assert e.value.code == "cancelled"
    assert store.get(aid)["chunks"] == chunks and "revised" not in " ".join(c["text"] for c in store.load(aid).chunks)


def test_a_failure_in_the_commit_puts_everything_back(tmp_path, monkeypatch):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    alpha = doc_of(store, aid, "alpha")
    blocks_before = store._backend(aid).get_blob(aid, "blocks", alpha["id"])
    chunks_before = store._backend(aid).read_chunks(aid)
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_500)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "_commit", boom)
    with pytest.raises(OSError):
        sync.update(aid)
    assert store._backend(aid).get_blob(aid, "blocks", alpha["id"]) == blocks_before
    assert store._backend(aid).read_chunks(aid) == chunks_before
    assert sync.versions(aid, alpha["id"]) == []
    monkeypatch.undo()
    assert sync.update(aid)["counts"]["updated"] == 1


def test_a_preview_says_what_would_change_and_changes_nothing(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    stamp = store.get(aid)["updated"]
    put(src, "alpha.md", body(2, "alpha"), 1_700_000_500)
    put(src, "delta.md", body(1, "delta"), 1_700_000_500)
    shown = sync.update(aid, dry_run=True)
    assert states(shown)["alpha.md"] == "would update" and states(shown)["delta.md"] == "would add"
    alpha = next(i for i in shown["items"] if i["name"] == "alpha.md")
    assert alpha["passages"]["added"] >= 1 and alpha["examples"]["added"] and alpha["examples"]["removed"]
    assert store.get(aid)["updated"] == stamp and store.get(aid)["documents"] == 3


def test_a_file_source_follows_one_file_and_adopts_a_document_added_by_hand(tmp_path):
    store, sync, aid = make(tmp_path)
    p = put(tmp_path, "one.md", body(1, "alpha"), 1_700_000_000)
    store.add_file(aid, "one.md", p)                                                         # added by hand, no source
    sid = sync.add_source(aid, "file", {"path": str(p)})["id"]
    done = sync.update(aid)
    assert done["counts"] == {"linked": 1} and store.get(aid)["documents"] == 1
    put(tmp_path, "one.md", body(2, "alpha"), 1_700_000_900)
    assert sync.update(aid)["counts"] == {"updated": 1}
    p.unlink()
    assert states(sync.check(aid))["one.md"] == "gone"
    assert sync.remove_source(aid, sid)["documents"] == 0 and store.get(aid)["documents"] == 1


def test_a_source_that_cannot_be_read_is_an_error_not_a_change(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    import shutil
    shutil.rmtree(src)
    report = sync.check(aid)
    assert report["counts"] == {"error": 3} and report["errors"][0]["kind"] == "offline"
    assert store.get(aid)["documents"] == 3


def test_removing_a_source_can_remove_its_documents(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.update(aid)
    assert sync.remove_source(aid, sid, remove_documents=True)["documents"] == 3
    assert store.get(aid)["documents"] == 0 and sync.sources(aid)["links"] == {}


def test_only_one_run_at_a_time_and_an_archive_without_a_source_says_so(tmp_path):
    store, sync, aid = make(tmp_path)
    with pytest.raises(RagError, match="no source"):
        sync.check(aid)
    store2, sync2, aid2, src, sid = folder_archive(tmp_path / "two")
    with sync2._only(aid2):
        with pytest.raises(RagError) as e:
            sync2.check(aid2)
        assert e.value.code == "busy"


def test_an_update_waits_while_a_heavy_job_has_the_gpu(tmp_path):
    waiting = {"n": 3}

    def busy():
        waiting["n"] -= 1
        return "a heavy job is running" if waiting["n"] > 0 else ""
    store, sync, aid, src, sid = folder_archive(tmp_path, busy=busy)
    stages = []
    done = sync.update(aid, progress=lambda e: stages.append(e.get("stage")))
    assert "waiting" in stages and done["counts"] == {"added": 3}
    sync.busy = lambda: "a heavy job is running"
    sync.settings.write({"wait_minutes": 0})
    with pytest.raises(RagError) as e:
        sync.update(aid)
    assert e.value.code == "busy" and "heavy job" in str(e.value)
    assert sync.check(aid)["counts"] == {"unchanged": 3}                 # a check needs no GPU and is not held


def test_attention_moves_only_when_something_new_is_pending(tmp_path):
    store, sync, aid, src, sid = folder_archive(tmp_path)
    sync.check(aid)
    t1 = sync.sources(aid)["attention"]["t"]
    assert t1 > 0 and sync.sources(aid)["attention"]["counts"] == {"new": 3}
    time.sleep(0.01)
    sync.check(aid)
    assert sync.sources(aid)["attention"]["t"] == t1                          # the same news is not news again
    sync.update(aid)
    assert sync.sources(aid)["attention"]["counts"] == {}
    put(src, "beta.md", body(2, "beta"), 1_700_000_900)
    time.sleep(0.01)
    sync.check(aid)
    assert sync.sources(aid)["attention"]["t"] > t1
