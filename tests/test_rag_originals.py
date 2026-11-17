"""The original files kept with the documents: one read-only copy by hash, given back as a file to save elsewhere, and swept when no document names it."""
import hashlib
import os
import stat

import pytest

from sources_support import body, doc_of, make
from tadamoo.rag import RagError, RagStore, project_key
from tadamoo.rag.originals import Originals

PROJ = project_key("project-one")
TEXT = "The wind carries the boat along the shore, and the delta is wide. " * 8


def add(store, tmp_path, aid, name, text=TEXT):
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return store.add_file(aid, name, p)["document"], p


def test_an_added_file_is_kept_read_only_and_given_back_with_its_name_and_the_listing_says_so(tmp_path):
    store = RagStore(tmp_path / "rag")
    aid = store.create("Boats", project=PROJ)["id"]
    doc, src = add(store, tmp_path, aid, "notes.txt")
    path, name = store.original(aid, doc["id"])
    assert name == "notes.txt" and path.read_bytes() == src.read_bytes() and path.name == hashlib.sha256(src.read_bytes()).hexdigest()
    assert not os.access(path, os.W_OK) and not (path.stat().st_mode & stat.S_IWRITE)
    assert store.get(aid)["document_list"][0]["has_original"] is True
    src.unlink()
    assert store.original(aid, doc["id"])[0].read_bytes()                                     # the kept copy outlives the file it came from
    with pytest.raises(RagError):
        store.original(aid, "d00000000")
    with pytest.raises(RagError):
        store.original(aid, "../x")


def test_with_the_setting_off_nothing_is_kept_and_the_error_says_how_to_have_it(tmp_path):
    store = RagStore(tmp_path / "rag")
    store.settings.write({"keep_originals": False})
    aid = store.create("Boats", project=PROJ)["id"]
    doc, _ = add(store, tmp_path, aid, "notes.txt")
    assert not (tmp_path / "rag" / "originals").exists() and store.get(aid)["document_list"][0]["has_original"] is False
    with pytest.raises(RagError, match="add the file again"):
        store.original(aid, doc["id"])


def test_the_same_file_in_two_archives_is_one_copy_and_a_sweep_removes_only_what_no_document_names(tmp_path):
    store = RagStore(tmp_path / "rag")
    a, b = (store.create(n, project=PROJ)["id"] for n in ("A", "B"))
    da, _ = add(store, tmp_path, a, "notes.txt")
    db, _ = add(store, tmp_path, b, "again.txt")
    assert len(list((tmp_path / "rag" / "originals").rglob("*"))) > 0 and sum(1 for _ in store.originals().keys()) == 1
    (tmp_path / "rag" / "originals" / ("f" * 2) / ("f" * 2)).mkdir(parents=True)
    store.originals().put(_write(tmp_path / "orphan.bin", b"orphan"), hashlib.sha256(b"orphan").hexdigest())
    assert store.sweep_originals() == 1
    store.remove_document(a, da["id"])
    assert store.sweep_originals() == 0                                                         # the other archive still names it
    store.remove_document(b, db["id"])
    assert store.sweep_originals() == 1 and sum(1 for _ in store.originals().keys()) == 0


def _write(path, data):
    path.write_bytes(data)
    return path


def test_a_file_that_is_not_the_one_its_hash_names_is_refused_and_leaves_nothing(tmp_path):
    o = Originals(tmp_path / "o")
    src = _write(tmp_path / "a.bin", b"abc")
    with pytest.raises(ValueError):
        o.put(src, hashlib.sha256(b"other").hexdigest())
    assert not o.has(hashlib.sha256(b"other").hexdigest()) and not list((tmp_path / "o").rglob("*.part"))
    assert o.put(src, hashlib.sha256(b"abc").hexdigest()) is True and o.put(src, hashlib.sha256(b"abc").hexdigest()) is False
    assert o.get("not-a-hash") is None and not o.has("../x")


def test_a_source_that_changes_keeps_the_new_original_too(tmp_path):
    store, sync, aid = make(tmp_path)
    src = tmp_path / "docs"
    src.mkdir()
    f = src / "alpha.md"
    f.write_text(body(1, "alpha"), encoding="utf-8")
    os.utime(f, (1_700_000_000, 1_700_000_000))
    sync.add_source(aid, "folder", {"path": str(src)})
    sync.update(aid)
    first = doc_of(store, aid, "alpha")
    assert store.original(aid, first["id"])[0].read_bytes() == f.read_bytes()
    f.write_text(body(9, "alpha"), encoding="utf-8")
    os.utime(f, (1_700_009_000, 1_700_009_000))
    sync.update(aid)
    now = doc_of(store, aid, "alpha")
    assert now["sha256"] != first["sha256"] and store.original(aid, now["id"])[0].read_bytes() == f.read_bytes()
