"""Knowledge archives: cutting text into passages, the word ranking, hybrid search with a fake embedder, the formats, the limits and the paths."""
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from tadamoo.rag import LIMITS, RagError, RagStore
from tadamoo.rag import search as ranking
from tadamoo.rag.chunking import chunk_text
from tadamoo.rag.extract import FORMATS, extract

SAILING = ("Sailing boats use the wind. A sloop has one mast and two sails, the mainsail and the jib. To tack, the helmsman turns the bow through the wind.\n\n"
           "The keel keeps the boat upright and stops it sliding sideways. Racing yachts have deep keels and light hulls.")
COOKING = ("Risotto needs short grain rice, hot stock and patience. Stir often and add the stock a ladle at a time.\n\n"
           "Parmesan and butter are stirred in at the end, off the heat, to make it creamy.")
ITALIAN = "La città è bagnata dalla pioggia. Perché non andiamo a prendere un caffè? Il perché è semplice: fa freddo.\n\nI gatti dormono sul divano."


class Fake:
    """An embedder with no model: each text becomes counts of a few topic words, so meaning can be tested without one."""
    model = "fake-1"
    TOPICS = (("boat", "sail", "yacht", "mast", "keel", "sloop"), ("rice", "stock", "risotto", "butter", "parmesan"), ("cat", "gatti", "divano"))

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = np.zeros((len(texts), len(self.TOPICS) + 1), dtype=np.float32)
        for r, t in enumerate(texts):
            low = t.lower()
            for c, words in enumerate(self.TOPICS):
                out[r, c] = sum(low.count(w) for w in words)
            out[r, -1] = 0.01
        return out


def store(tmp_path, embedder=None, **kw):
    holder = {"e": embedder}
    s = RagStore(tmp_path / "rag", embedder=lambda: holder["e"], **kw)
    s.holder = holder
    return s


def write(tmp_path, name, content):
    p = tmp_path / "src" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return p


def add(s, tmp_path, aid, name, text):
    return s.add_file(aid, name, write(tmp_path, name, text))


# -- chunking ------------------------------------------------------------------------------------------------

def test_passages_follow_paragraphs_and_start_with_the_tail_of_the_one_before():
    paras = [f"Paragraph {i} " + "word " * 60 for i in range(8)]
    chunks = chunk_text("\n\n".join(paras), size=700, overlap=100)
    assert len(chunks) > 1 and all(len(c) <= 700 + 100 for c in chunks)
    assert chunks[0].startswith("Paragraph 0") and "Paragraph 7" in chunks[-1]
    tail = chunks[0][-100:].split(" ", 1)[1].strip()
    assert tail.split()[-1] in chunks[1].split("\n\n")[0]                                     # the second passage opens with the end of the first


def test_a_long_paragraph_is_cut_at_a_sentence_and_nothing_is_lost():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_text(text, size=300, overlap=0)
    assert all(len(c) <= 300 for c in chunks) and all(c.endswith(".") for c in chunks)
    assert " ".join(chunks).split() == text.split()
    assert chunk_text("x" * 2500, size=1000, overlap=0) == ["x" * 1000, "x" * 1000, "x" * 500]
    assert chunk_text("   \n\n  ") == []
    with pytest.raises(ValueError):
        chunk_text("text", size=10)
    with pytest.raises(ValueError):
        chunk_text("text", size=100, overlap=60)


# -- the word ranking ----------------------------------------------------------------------------------------

def test_a_rare_word_outweighs_a_common_one_and_accents_do_not_matter():
    index = ranking.Bm25(["the cat sat on the mat", "the dog sat on the log", "a quokka sat on the beach"])
    scores = ranking.bm25([index], "the quokka")[0]
    assert max(scores, key=scores.get) == 2 and scores[2] > 3 * scores.get(0, 0.0)
    assert ranking.tokens("Perché la città") == ["perche", "la", "citta"]
    assert ranking.bm25([index], "zzz") == [{}] and ranking.bm25([index], "") == [{}]


def test_scores_from_several_archives_share_their_statistics():
    one, two = ranking.Bm25(["alpha beta", "gamma delta"]), ranking.Bm25(["alpha alpha beta", "epsilon"])
    together = ranking.bm25([one, two], "alpha")
    assert together[1][0] > together[0][0] > 0


def test_text_search_finds_the_passage_and_names_its_archive_and_document(tmp_path):
    s = store(tmp_path)
    a, b = s.create("Boats")["id"], s.create("Kitchen")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)
    add(s, tmp_path, b, "rice.md", COOKING)
    found = s.search([a, b], "how does a sloop tack")
    assert found["mode"] == "lexical" and found["hits"][0]["archive"] == "Boats" and found["hits"][0]["document"] == "boats.txt"
    assert found["hits"][0]["semantic"] is None and found["hits"][0]["lexical"] > 0
    only = s.search([b], "how does a sloop tack")
    assert only["hits"] == []
    assert s.search([b], "creamy parmesan risotto")["hits"][0]["document"] == "rice.md"
    assert s.search([a], "perche")["hits"] == []
    c = s.create("Lingua")["id"]
    add(s, tmp_path, c, "it.txt", ITALIAN)
    assert s.search([c], "perche citta")["hits"][0]["document"] == "it.txt"                  # accents fold


def test_a_floor_drops_weak_matches_and_top_k_limits_the_answer(tmp_path):
    s = store(tmp_path)
    a = s.create("Mix")["id"]
    add(s, tmp_path, a, "boats.txt", "\n\n".join(f"Boat {i} floats on the water near the harbour. " + "filler " * 80 for i in range(30)))
    assert len(s.search([a], "boat harbour", top_k=3)["hits"]) == 3
    assert s.search([a], "boat harbour", min_share=5)["hits"] == []
    with pytest.raises(RagError):
        s.search([a], 5)


# -- hybrid search -------------------------------------------------------------------------------------------

def test_with_an_embedder_vectors_are_kept_and_the_search_is_hybrid(tmp_path):
    fake = Fake()
    s = store(tmp_path, fake)
    a = s.create("Boats")["id"]
    out = add(s, tmp_path, a, "boats.txt", SAILING)
    assert out["embedded"] is True and (tmp_path / "rag" / a / "embeddings.npy").is_file()
    vectors = np.load(tmp_path / "rag" / a / "embeddings.npy")
    assert vectors.dtype == np.float32 and np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert s.get(a)["mode"] == "hybrid" and s.get(a)["embed_model"] == "fake-1"
    found = s.search([a], "yacht keel")
    assert found["mode"] == "hybrid" and found["hits"][0]["semantic"] is not None and found["hits"][0]["mode"] == "hybrid"


def test_vectors_find_what_the_words_do_not(tmp_path):
    s = store(tmp_path, Fake())
    a, b = s.create("Boats")["id"], s.create("Kitchen")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)
    add(s, tmp_path, b, "rice.txt", COOKING)
    query = "what holds the mast"                                                                # no word in common with the kitchen text
    assert s.search([a, b], query)["hits"][0]["archive"] == "Boats"
    s.holder["e"] = None
    assert s.search([a], "boat sail")["mode"] == "lexical"                                       # the embedder is gone: words alone, not an error
    assert s.get(a)["mode"] == "text"


def test_an_embedder_that_fails_at_query_time_falls_back_to_words(tmp_path):
    fake = Fake()
    s = store(tmp_path, fake)
    a = s.create("Boats")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)

    def boom(texts):
        raise RuntimeError("model unloaded")
    fake.embed = boom
    found = s.search([a], "sloop mainsail")
    assert found["mode"] == "lexical" and "embedder failed" in found["note"] and found["hits"]


def test_an_embedder_that_fails_at_ingest_leaves_the_document_searchable_by_words(tmp_path):
    class Broken:
        model = "broken"

        def embed(self, texts):
            raise RuntimeError("no memory")
    s = store(tmp_path, Broken())
    a = s.create("Boats")["id"]
    out = add(s, tmp_path, a, "boats.txt", SAILING)
    assert out["embedded"] is False and "embedder failed" in out["note"] and not (tmp_path / "rag" / a / "embeddings.npy").exists()
    assert s.search([a], "keel")["hits"]


def test_a_document_added_without_the_embedder_drops_the_vectors_and_reindex_restores_them(tmp_path):
    s = store(tmp_path, Fake())
    a = s.create("Mixed")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)
    s.holder["e"] = None
    out = add(s, tmp_path, a, "rice.txt", COOKING)
    assert out["embedded"] is False and s.get(a)["embed_model"] == "" and not (tmp_path / "rag" / a / "embeddings.npy").exists()
    s.holder["e"] = Fake()
    assert s.get(a)["reindex"] is True
    done = s.reindex(a)
    assert done["embedded"] is True and done["chunks"] == s.get(a)["chunks"] and s.get(a)["mode"] == "hybrid" and s.get(a)["reindex"] is False


def test_vectors_of_another_model_are_never_mixed_in(tmp_path):
    s = store(tmp_path, Fake())
    a = s.create("Boats")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)

    class Other(Fake):
        model = "fake-2"
    s.holder["e"] = Other()
    assert s.get(a)["mode"] == "text" and s.get(a)["reindex"] is True
    assert s.search([a], "keel")["mode"] == "lexical"
    out = add(s, tmp_path, a, "rice.txt", COOKING)
    assert out["embedded"] is False and "index it again" in out["note"]


def test_removing_a_document_removes_its_passages_and_vectors(tmp_path):
    s = store(tmp_path, Fake())
    a = s.create("Mixed")["id"]
    first = add(s, tmp_path, a, "boats.txt", SAILING)["document"]
    add(s, tmp_path, a, "rice.txt", COOKING)
    total = s.get(a)["chunks"]
    done = s.remove_document(a, first["id"])
    assert done["chunks"] == total - first["chunks"] and len(np.load(tmp_path / "rag" / a / "embeddings.npy")) == done["chunks"]
    assert [d["name"] for d in s.get(a)["document_list"]] == ["rice.txt"] and not (tmp_path / "rag" / a / "texts" / (first["id"] + ".txt")).exists()
    assert s.search([a], "sloop")["hits"] == [] or s.search([a], "sloop")["hits"][0]["document"] == "rice.txt"
    with pytest.raises(RagError) as e:
        s.remove_document(a, first["id"])
    assert e.value.code == "not_found"


# -- the formats ---------------------------------------------------------------------------------------------

def pdf_bytes(text):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs += [f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def docx_bytes(paragraphs):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def epub_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("OEBPS/ch1.xhtml", "<html><head><style>p{color:red}</style></head><body><h1>First</h1><p>The lighthouse keeper climbed the stairs.</p></body></html>")
        z.writestr("OEBPS/ch2.xhtml", "<html><body><p>The storm broke at midnight &amp; the lamp stayed lit.</p></body></html>")
    return buf.getvalue()


def test_every_supported_format_is_read(tmp_path):
    text = "The lighthouse keeper climbed the stairs. " * 3
    assert "lighthouse keeper" in extract(write(tmp_path, "a.txt", text), "a.txt")
    assert "lighthouse keeper" in extract(write(tmp_path, "a.md", "# Title\n\n" + text), "a.md")
    assert extract(write(tmp_path, "a.txt", b"Caff\xe8 \xe8 pronto, venite pure a bere"), "a.txt").startswith("Caffè")       # not UTF-8: read as Windows text
    html = extract(write(tmp_path, "a.html", f"<html><head><title>T</title><script>alert(1)</script></head><body><h2>Head</h2><p>{text}</p><p>Second &amp; last paragraph here.</p></body></html>"), "a.html")
    assert "alert" not in html and "Head" in html and "Second & last" in html and "\n\n" in html
    epub = extract(write(tmp_path, "a.epub", epub_bytes()), "a.epub")
    assert epub.index("lighthouse keeper") < epub.index("storm broke") and "color:red" not in epub and "midnight & the lamp" in epub
    docx = extract(write(tmp_path, "a.docx", docx_bytes(["First paragraph of the letter.", "Second paragraph of the letter."])), "a.docx")
    assert "First paragraph" in docx and "Second paragraph" in docx
    for ext in FORMATS:
        assert ext.startswith(".")


@pytest.mark.skipif(not any(__import__("importlib").util.find_spec(m) for m in ("pypdf", "fitz")), reason="no PDF library installed")
def test_a_pdf_is_read_when_a_library_is_there(tmp_path):
    assert "Hello archive world" in extract(write(tmp_path, "a.pdf", pdf_bytes("Hello archive world, this is a page")), "a.pdf")


def test_a_pdf_without_a_library_is_refused_naming_the_packages(tmp_path, monkeypatch):
    from tadamoo import attachments
    monkeypatch.setattr(attachments, "pdf_text", lambda path: None)
    with pytest.raises(RagError, match="pypdf or PyMuPDF"):
        extract(write(tmp_path, "a.pdf", pdf_bytes("Hello")), "a.pdf")


def test_files_that_are_not_what_they_say_or_hold_nothing_are_refused(tmp_path):
    for name, content, message in (("a.doc", b"x" * 100, "not a supported format"), ("a", b"text text text text text", "without an extension"),
                                   ("a.pdf", b"just text, not a pdf at all here", "not a PDF"), ("a.docx", b"plain text pretending to be Word", "not a Word"),
                                   ("a.epub", b"plain text pretending to be a book", "not an EPUB"), ("a.txt", b"abc\x00\x01\x02" * 10, "not text"),
                                   ("a.txt", b"   \n  ", "no text"), ("a.docx", docx_bytes([]), "no text"), ("a.docx", b"PK\x03\x04 broken zip content", "could not be read")):
        with pytest.raises(RagError, match=message):
            extract(write(tmp_path, name, content), name)


def test_a_zip_that_unpacks_to_too_much_is_not_read(tmp_path, monkeypatch):
    from tadamoo.rag import extract as ex
    monkeypatch.setattr(ex, "MAX_UNPACKED", 100)
    with pytest.raises(RagError, match="too large"):
        extract(write(tmp_path, "a.epub", epub_bytes()), "a.epub")


# -- limits and paths ----------------------------------------------------------------------------------------

def test_limits_on_archives_documents_size_and_duplicates(tmp_path, monkeypatch):
    from tadamoo.rag import store as st
    s = store(tmp_path)
    a = s.create("One")["id"]
    add(s, tmp_path, a, "a.txt", SAILING)
    with pytest.raises(RagError, match="already in the archive"):
        add(s, tmp_path, a, "copy.txt", SAILING)
    with pytest.raises(RagError, match="already an archive"):
        s.create("one")
    monkeypatch.setitem(st.LIMITS, "documents", 1)
    with pytest.raises(RagError, match="at most 1 documents"):
        add(s, tmp_path, a, "b.txt", COOKING)
    monkeypatch.setitem(st.LIMITS, "documents", 200)
    monkeypatch.setitem(st.LIMITS, "chunks", 2)
    with pytest.raises(RagError, match="passages"):
        add(s, tmp_path, a, "big.txt", "\n\n".join("Paragraph %d " % i + "x" * 900 for i in range(6)))
    monkeypatch.setitem(st.LIMITS, "chunks", 20000)
    monkeypatch.setitem(st.LIMITS, "document_bytes", 50)
    with pytest.raises(RagError, match="at most"):
        add(s, tmp_path, a, "c.txt", COOKING)
    with pytest.raises(RagError, match="at most"):
        s.add_bytes(a, "c.txt", b"x" * 51)
    monkeypatch.setitem(st.LIMITS, "document_bytes", 1000)
    with pytest.raises(RagError, match="empty"):
        add(s, tmp_path, a, "e.txt", b"")
    monkeypatch.setitem(st.LIMITS, "document_chars", 10)
    with pytest.raises(RagError, match="characters"):
        add(s, tmp_path, a, "d.txt", COOKING)
    assert [d["name"] for d in s.get(a)["document_list"]] == ["a.txt"]                    # every refusal left the archive as it was
    monkeypatch.setitem(st.LIMITS, "archives", 1)
    with pytest.raises(RagError, match="at most 1 archives"):
        s.create("Two")
    for bad in ("", "  ", "x" * 61, "tab\tname", 5):
        with pytest.raises(RagError):
            s.create(bad)
    with pytest.raises(RagError):
        s.create("Ok", "d" * 501)


def test_an_archive_id_cannot_climb_out_of_the_folder(tmp_path):
    s = store(tmp_path)
    s.create("One")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "meta.json").write_text("{}")
    for bad in ("../secret", "..", ".", "a/b", "a\\b", "", "UPPER", "-x", "x" * 65, ".incoming", None, 5, ["a"]):
        for call in (s.get, s.load, lambda i: s.delete(i, True), lambda i: s.add_bytes(i, "a.txt", b"x" * 40), s.reindex):
            with pytest.raises(RagError) as e:
                call(bad)
            assert e.value.code == "not_found"
    assert (tmp_path / "secret" / "meta.json").exists()


def test_a_document_name_and_a_path_are_checked(tmp_path):
    s = store(tmp_path, forbidden=[tmp_path / "private"])
    a = s.create("One")["id"]
    for bad in ("../x.txt", "a/b.txt", "a\\b.txt", "..", "", "x\x00.txt", "n" * 201):
        with pytest.raises(RagError):
            s.add_bytes(a, bad, b"some text that is long enough to count")
    ok = write(tmp_path, "ok.txt", SAILING)
    (tmp_path / "private").mkdir()
    secret = write(tmp_path, "s.txt", COOKING)
    secret.replace(tmp_path / "private" / "s.txt")
    for bad in ("relative.txt", str(tmp_path / "src" / ".." / "src" / "ok.txt"), str(tmp_path / "missing.txt"), str(tmp_path / "src"), "", None, str(tmp_path / "private" / "s.txt")):
        with pytest.raises(RagError):
            s.add_path(a, bad)
    assert s.add_path(a, str(ok))["document"]["name"] == "ok.txt"
    assert not list((tmp_path / "rag" / ".incoming").glob("*")) if (tmp_path / "rag" / ".incoming").exists() else True


def test_files_are_written_whole_and_survive_a_new_store(tmp_path):
    s = store(tmp_path, Fake())
    a = s.create("Boats", "About sailing")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)
    folder = tmp_path / "rag" / a
    assert not list(folder.rglob("*.tmp"))
    assert sorted(p.name for p in folder.iterdir()) == ["chunks.jsonl", "embeddings.npy", "meta.json", "texts"]
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Boats" and meta["chunks"] == len((folder / "chunks.jsonl").read_text().splitlines())
    again = store(tmp_path, Fake())
    assert again.list()[0]["id"] == a and again.search([a], "keel")["mode"] == "hybrid"


def test_a_damaged_archive_is_left_out_of_the_list_and_chunks_of_unknown_documents_are_ignored(tmp_path):
    s = store(tmp_path)
    a = s.create("Good")["id"]
    add(s, tmp_path, a, "boats.txt", SAILING)
    (tmp_path / "rag" / "bad-123456").mkdir()
    (tmp_path / "rag" / "bad-123456" / "meta.json").write_text("{nope")
    (tmp_path / "rag" / "stray").mkdir()
    assert [x["id"] for x in s.list()] == [a]
    with (tmp_path / "rag" / a / "chunks.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"doc": "dffffffff", "n": 0, "text": "orphan keel keel keel"}) + "\n")
    assert all(h["document"] == "boats.txt" for h in store(tmp_path).search([a], "keel")["hits"])


def test_update_rename_and_delete(tmp_path):
    s = store(tmp_path)
    a, b = s.create("One")["id"], s.create("Two")["id"]
    assert s.update(a, {"name": "Uno", "description": "d", "cloud_ok": True})["name"] == "Uno" and s.get(a)["cloud_ok"] is True
    with pytest.raises(RagError, match="already"):
        s.update(a, {"name": "two"})
    with pytest.raises(RagError, match="unknown field"):
        s.update(a, {"colour": "red"})
    with pytest.raises(RagError):
        s.update(a, {"cloud_ok": "yes"})
    with pytest.raises(RagError, match="confirmation"):
        s.delete(a, None)
    assert s.delete(a, True)["deleted"] is True and not (tmp_path / "rag" / a).exists() and [x["id"] for x in s.list()] == [b]
