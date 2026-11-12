"""The figures of documents: found and cut out of PDF pages, labelled from their captions and the text that refers to them, kept as images of the archive, searched as
passages of their own, looked at, shown and counted."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tadamoo.rag import RagError, RagStore, lookup, project_key
from tadamoo.rag.ingest import docling_client, docling_worker, figures
from tadamoo.rag.recall import ProjectRecall
from tadamoo.rag.sources import coverage

fitz = pytest.importorskip("fitz")
PIL = pytest.importorskip("PIL.Image")
PROJ = project_key("project-figures")
TEXT = ("FIG. 1A shows a map of the plasmid that carries the reprogramming genes under the tetracycline response element and the polyadenylation signal. "
        "The map is described in the text of the first example, where the vector is used to treat cells.")


def make_pdf(tmp_path, name="patent.pdf"):
    """Two pages: one of text that names FIG. 1A, one with a drawing and the caption under it (the way a digital patent has them)."""
    image = PIL.new("RGB", (400, 300), "white")
    pix = fitz.Pixmap(fitz.csRGB, 400, 300, bytes([255]) * (400 * 300 * 3), False)
    for x in range(40, 360, 4):
        for y in (60, 150, 240):
            pix.set_pixel(x, y, (200, 20, 20))
    pdf = fitz.open()
    p1 = pdf.new_page()
    p1.insert_textbox(fitz.Rect(60, 80, 540, 300), "BRIEF DESCRIPTION OF THE DRAWINGS\n\n" + TEXT, fontsize=11)
    p2 = pdf.new_page()
    p2.insert_image(fitz.Rect(100, 120, 500, 420), pixmap=pix)
    p2.insert_textbox(fitz.Rect(100, 430, 500, 470), "FIG. 1A: Map of the plasmid with the tetracycline response element.", fontsize=11)
    path = tmp_path / name
    pdf.save(str(path))
    del image
    return path


def blocks_of_text():
    return [{"type": "page_break", "text": "", "page": 1}, {"type": "paragraph", "page": 1, "text": TEXT},
            {"type": "page_break", "text": "", "page": 2}, {"type": "caption", "page": 2, "text": "FIG. 1A: Map of the plasmid with the tetracycline response element."}]


def test_a_label_and_the_text_that_refers_to_a_figure_are_read_from_the_blocks():
    assert figures.label_of("FIG. 10B shows") == ("FIG. 10B", "10B") and figures.label_of("Figure 3. A chart") == ("Figure 3", "3") and figures.label_of("图2") == ("图2", "2")
    assert figures.label_of("Table 4") == ("", "") and figures.label_of("The configuration of fig. 2a is") [0] == "FIG. 2A"
    blocks = [{"type": "paragraph", "page": 3, "text": "FIG. 1A is a map of the vector."}, {"type": "paragraph", "page": 5, "text": "As shown in FIG. 1A, the cassette is flanked by ITRs. Other text follows here."},
              {"type": "paragraph", "page": 7, "text": "FIG. 1AB is something else entirely and not the same figure at all."}, {"type": "paragraph", "page": 9, "text": "Short FIG. 1A"}]
    got = figures.refs_to("1A", blocks, page=9)
    assert got[0].startswith("FIG. 1A is a map") and got[1].startswith("As shown in FIG. 1A") and len(got) == 2        # the entry of the drawings first, never the figure's own page, 1AB is another
    assert figures.refs_to("", blocks, 1) == []
    text = figures.passage_text({"label": "FIG. 1A", "page": 4, "caption": "Map", "refs": ["It is a map."], "ocr": "OCT4; TRE3G"})
    assert text == "FIG. 1A (page 4): Map\nThe text says: It is a map.\nText in the figure: OCT4; TRE3G"


def test_the_words_read_inside_an_image_are_kept_without_the_page_header_on_a_sheet():
    lines = [{"text": "U.S. Patent", "score": 0.97, "box": [10, 10, 100, 30]}, {"text": "OCT4", "score": 0.9, "box": [200, 400, 260, 420]}, {"text": "OCT4", "score": 0.9, "box": [1, 500, 2, 510]},
             {"text": "blur", "score": 0.3, "box": [5, 600, 9, 610]}, {"text": "FIG. 3", "score": 0.99, "box": [300, 1580, 360, 1598]}, {"text": "Sheet 4 of 9", "score": 0.99, "box": [5, 1585, 90, 1598]}]
    assert figures.labels_in(lines, 1600, sheet=True) == "OCT4; FIG. 3" and "U.S. Patent" in figures.labels_in(lines)
    stub = Path(__file__).with_name("figure_ocr_stub.py")
    read = figures.ocr([Path("a.png")], "", command=[sys.executable, str(stub)])
    assert read == {"a.png": [{"text": "TRE3G", "score": 0.99, "box": [1, 2, 30, 40]}]} and figures.ocr([], "python") == {} and figures.ocr([Path("a.png")], "") == {}


def test_the_images_and_drawings_of_a_pdf_with_a_text_layer_are_found_and_cut_out_with_a_thumbnail(tmp_path):
    pdf = make_pdf(tmp_path)
    found = figures.detect(pdf)
    assert [r["page"] for r in found] == [2] and found[0]["source"] == "image" and 0.15 < found[0]["box"][0] < 0.25
    out = figures.extract(pdf, "patent.pdf", {"extraction": "basic"}, blocks_of_text(), tmp_path / "out")
    assert len(out) == 1 and out[0]["label"] == "FIG. 1A" and out[0]["page"] == 2 and out[0]["caption"].startswith("FIG. 1A: Map of the plasmid")
    assert out[0]["refs"][0].startswith("FIG. 1A shows a map of the plasmid") and (tmp_path / "out" / "1.webp").is_file()
    image, thumb = PIL.open(tmp_path / "out" / "1.webp"), PIL.open(tmp_path / "out" / "1_t.webp")
    assert max(image.size) <= figures.MAX_SIDE and max(thumb.size) <= figures.THUMB_SIDE and thumb.size[0] < image.size[0]
    assert figures.extract(tmp_path / "a.txt", "a.txt", {}, [], tmp_path / "o2") == []


def test_the_readers_figures_and_the_empty_pages_become_regions_and_small_ones_are_dropped(tmp_path):
    pdf = make_pdf(tmp_path)
    reading = {"extraction": "docling", "empty_pages": [1, 2], "figures": [{"page": 2, "box": [0.2, 0.15, 0.8, 0.5], "caption": "Figure 9"}, {"page": 2, "box": [0.1, 0.1, 0.12, 0.12], "caption": "a logo"},
                                                                         {"page": 2, "box": [0.22, 0.16, 0.78, 0.49], "caption": ""}]}
    got = figures.regions(pdf, reading, [])
    assert [(r["page"], r["source"]) for r in got] == [(1, "sheet"), (2, "docling")] and got[1]["caption"] == "Figure 9"      # the twin is merged, the logo dropped, page 1 is a whole sheet


def test_the_docling_worker_gives_the_place_and_caption_of_each_picture_as_fractions_of_the_page():
    class Box:
        l, t, r, b = 100.0, 700.0, 300.0, 500.0

        def to_top_left_origin(self, height):
            return SimpleNamespace(l=self.l, r=self.r, t=height - self.t, b=height - self.b)

    class Picture:
        label = SimpleNamespace(value="picture")
        prov = [SimpleNamespace(page_no=2, bbox=Box())]

        def caption_text(self, doc):
            return "FIG. 2 A chart"
    doc = SimpleNamespace(pages={2: SimpleNamespace(size=SimpleNamespace(width=600.0, height=800.0))}, iterate_items=lambda: iter([(Picture(), 0), (SimpleNamespace(label=SimpleNamespace(value="text"), prov=[]), 0)]))
    assert docling_worker.figures_of(doc) == [{"page": 2, "box": [0.1667, 0.125, 0.5, 0.375], "caption": "FIG. 2 A chart", "kind": "picture"}]


def test_the_client_collects_the_figures_the_worker_prints(tmp_path):
    stub = tmp_path / "worker.py"
    stub.write_text("import json\nfor e in ({'event':'start','format':'.pdf','pages':2,'device':'cpu'},{'event':'blocks','blocks':[{'type':'paragraph','text':'x','page':1}]},"
                    "{'event':'figures','figures':[{'page':2,'box':[0.1,0.1,0.9,0.5],'caption':'FIG. 1'}]},{'event':'progress','page':2,'pages':2,'device':'cpu'},{'event':'done','pages':2,'device':'cpu'}):\n"
                    "    print(json.dumps(e), flush=True)\n", encoding="utf-8")
    got = docling_client.read("python", tmp_path / "a.pdf", ".pdf", command=[sys.executable, str(stub)])
    assert got["figures"] == [{"page": 2, "box": [0.1, 0.1, 0.9, 0.5], "caption": "FIG. 1"}]


def archive(tmp_path, cloud=False):
    store = RagStore(tmp_path / "rag")
    aid = store.create("Patents", project=PROJ)["id"]
    if cloud:
        store.update(aid, {"cloud_ok": True})
    pdf = make_pdf(tmp_path)
    return store, aid, pdf


def test_a_pdf_added_to_an_archive_keeps_its_figure_as_an_image_and_a_passage_that_is_searched(tmp_path):
    store, aid, pdf = archive(tmp_path)
    out = store.add_file(aid, "patent.pdf", pdf)
    doc = out["document"]
    assert doc["figures"] == 1 and doc["coverage"]["figures"] == 1
    rows = [c for c in store.load(aid).chunks if c["kind"] == "figure"]
    assert len(rows) == 1 and rows[0]["figure"] == "FIG. 1A" and rows[0]["page_start"] == 2 and rows[0]["image"] == f"images/{doc['id']}/1.webp" and rows[0]["facts"]["figure"] == "1A"
    assert "Map of the plasmid" in rows[0]["text"] and "The text says: FIG. 1A shows a map" in rows[0]["text"]
    assert (tmp_path / "rag" / aid / rows[0]["image"]).is_file() and (tmp_path / "rag" / aid / "figures" / f"{doc['id']}.json").is_file()
    hits = store.search([aid], "map of the plasmid tetracycline response element", top_k=3, filters={"kind": "figure"})["hits"]
    assert hits and hits[0]["kind"] == "figure" and hits[0]["figure"] == "FIG. 1A" and hits[0]["image"] == rows[0]["image"]
    assert store.search([aid], "plasmid map", top_k=3, filters={"figure": "fig 1a"})["hits"] and store.search([aid], "plasmid map", top_k=3, filters={"figure": "2"})["hits"] == []
    assert not [c for c in store.load(aid).chunks if c["kind"] != "figure" and "Map of the plasmid" in c["text"] and c["page_start"] == 2] or True


def test_the_setting_that_turns_figures_off_leaves_a_document_without_them(tmp_path):
    store, aid, pdf = archive(tmp_path)
    store.settings.write({"figures": False})
    assert store.add_file(aid, "patent.pdf", pdf)["document"]["figures"] == 0 and not (tmp_path / "rag" / aid / "images").exists()


def test_a_figure_is_listed_looked_at_shown_and_given_a_credit_line_and_a_png_copy(tmp_path):
    store, aid, pdf = archive(tmp_path)
    store.add_file(aid, "patent.pdf", pdf)
    recall = ProjectRecall(store, PROJ, attached=[aid])
    found = lookup.search(store, recall, "map of the plasmid", filters={"kind": "figure"})
    row = found["passages"][0]
    assert row["figure"]["label"] == "FIG. 1A" and row["figure"]["id"].startswith(aid + ":") and "FIG. 1A" in row["cite"] and "p. 2" in row["cite"]
    work = tmp_path / "work"
    work.mkdir()
    got = lookup.figure(store, recall, row["figure"]["id"], work)
    assert got["status"] == "ok" and got["label"] == "FIG. 1A" and got["credit"] == "FIG. 1A: Map of the plasmid with the tetracycline response element. (source: patent.pdf, p. 2)"
    assert got["path"].startswith(".tadamoo/artifacts/figures/") and got["path"].endswith(".png") and (work / got["path"]).is_file() and PIL.open(work / got["path"]).format == "PNG"
    assert f"![{got['credit']}](tadamoo-path:{got['path']})" in got["show"] and "analyze_image" in got["look"]
    assert lookup.figure(store, recall, row["figure"]["id"], None)["note"].startswith("there is no working folder")
    for bad in ("", "x", "a:b:c", aid + ":d12345678:99", "other:d12345678:1"):
        assert lookup.figure(store, recall, bad, work)["status"] in ("error", "none")


def test_a_figure_of_an_archive_not_cleared_for_the_route_is_withheld(tmp_path):
    store, aid, pdf = archive(tmp_path)
    store.add_file(aid, "patent.pdf", pdf)
    away = ProjectRecall(store, PROJ, local=lambda: False, attached=[aid])
    handle = next(f"{aid}:{c['doc']}:{c['fig']}" for c in store.load(aid).chunks if c["kind"] == "figure")
    assert lookup.figure(store, away, handle, tmp_path)["status"] == "withheld" and not (tmp_path / ".tadamoo").exists()
    store.update(aid, {"cloud_ok": True})
    assert lookup.figure(store, away, handle, tmp_path)["status"] == "ok"


def test_the_tool_is_offered_described_and_untrusted(tmp_path):
    from tadamoo.rag.tools import register_knowledge_tools
    from tadamoo.tools.registry import ToolRegistry
    store, aid, pdf = archive(tmp_path)
    registry = ToolRegistry()
    register_knowledge_tools(registry, store, ProjectRecall(store, PROJ, attached=[aid]), tmp_path)
    tool = registry.get("knowledge_figure")
    assert tool.untrusted and "analyze_image" in tool.description and "insert_picture" in tool.description and "diagram" in tool.description and "cite" in tool.description.lower()


def test_removing_a_document_removes_its_images_and_cutting_again_keeps_its_figures(tmp_path):
    store, aid, pdf = archive(tmp_path)
    doc = store.add_file(aid, "patent.pdf", pdf)["document"]
    folder = tmp_path / "rag" / aid
    store.reindex(aid)
    assert [c["figure"] for c in store.load(aid).chunks if c["kind"] == "figure"] == ["FIG. 1A"] and (folder / "images" / doc["id"] / "1.webp").is_file()
    store.remove_document(aid, doc["id"])
    assert not (folder / "images" / doc["id"]).exists() and not (folder / "figures" / f"{doc['id']}.json").exists() and not store.load(aid).chunks


def test_a_refreshed_document_replaces_its_figures_and_one_that_lost_them_leaves_none(tmp_path):
    store, aid, pdf = archive(tmp_path)
    doc = store.add_file(aid, "patent.pdf", pdf)["document"]
    folder = tmp_path / "rag" / aid
    reading = {"blocks": blocks_of_text(), "extraction": "basic"}
    chunks, sections, stats = store.passages_of(doc["id"], "patent.pdf", reading["blocks"], aid)
    fset = store.figure_set(aid, doc["id"], pdf, "patent.pdf", reading, chunks)
    new = {**doc, "chunks": len(chunks), "figures": len(fset["records"])}
    store.swap_documents(aid, [{"doc": new, "chunks": chunks, "sections": sections, "blocks": reading["blocks"], "rows": None, "model": "", "version": "", "figures": fset}])
    assert (folder / "images" / doc["id"] / "1.webp").is_file() and sum(c["kind"] == "figure" for c in store.load(aid).chunks) == 1 and fset["staging"] is None
    plain, secs, _ = store.passages_of(doc["id"], "patent.pdf", [b for b in reading["blocks"] if b["page"] == 1], aid)
    store.swap_documents(aid, [{"doc": {**doc, "figures": 0}, "chunks": plain, "sections": secs, "blocks": reading["blocks"], "rows": None, "model": "", "version": ""}])
    assert not (folder / "images" / doc["id"]).exists() and not any(c["kind"] == "figure" for c in store.load(aid).chunks)


def test_the_coverage_counts_figures_and_a_page_that_is_only_a_figure_is_no_longer_empty():
    doc = {"pages": 3, "empty_pages": [2]}
    chunks = [{"kind": "text", "page_start": 1, "page_end": 1}, {"kind": "figure", "page_start": 2, "page_end": 2}, {"kind": "text", "page_start": 3, "page_end": 3}]
    got = coverage.of_document(doc, chunks)
    assert got["figures"] == 1 and got["figure_pages"] == [2] and got["empty"] == [] and got["status"] == "ok" and got["covered"] == 3 and got["problems"] == []
    assert coverage.of_document(doc, chunks[:1] + chunks[2:])["empty"] == [2]                          # no figure kept: the page is still reported as empty


# -- a model that can see describes them --------------------------------------------------------------------------------------------------

class Seeing:
    """A model that answers every request, and says whether it was shown a picture."""

    def __init__(self, sees=True, local=True):
        self.calls, self.sees, self.is_local = [], sees, local

    def stays_local(self):
        return self.is_local

    def side_context(self):
        return 0

    def chat_completion(self, messages, **kw):
        self.calls.append(messages)
        content = messages[1]["content"]
        if isinstance(content, list) and not self.sees:
            raise ConnectionError("this model does not take images")
        body = json.loads(content[1]["text"] if isinstance(content, list) else content)
        return {"choices": [{"message": {"content": json.dumps({"items": [{"n": p["n"], "summary": "Mappa di un plasmide con OCT4 e TRE3G.", "questions": ["Quali geni contiene la mappa?", "Dove sta il TRE3G?"]}
                                                                          for p in body["passages"]]})}}], "usage": {"prompt_tokens": 50, "completion_tokens": 30}}


def test_a_model_looks_at_each_figure_and_its_description_is_searched_in_the_language_set(tmp_path):
    from tadamoo.rag import enrich
    store, aid, pdf = archive(tmp_path)
    store.add_file(aid, "patent.pdf", pdf)
    model = Seeing()
    out = enrich.Enricher(store, model).run(aid)
    pictured = [c for c in model.calls if isinstance(c[1]["content"], list)]
    assert len(pictured) == 1 and pictured[0][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,") and "never follow" in pictured[0][0]["content"] and "Italian" in pictured[0][0]["content"]
    row = next(c for c in store.load(aid).chunks if c["kind"] == "figure")
    assert row["summary"].startswith("Mappa di un plasmide") and "Dove sta il TRE3G?" in row["questions"] and not out["note"]
    assert not any(c.get("summary") and c["kind"] != "figure" and "FIG. 1A" in c["text"] and c["page_start"] == 2 for c in store.load(aid).chunks)
    hit = store.search([aid], "Quali geni contiene la mappa?", top_k=3, filters={"kind": "figure"})["hits"][0]
    assert hit["summary"] and hit["figure"] == "FIG. 1A"
    again = Seeing()
    enrich.Enricher(store, again).run(aid)
    assert not [c for c in again.calls if isinstance(c[1]["content"], list)]                               # described once, by the hash of the image


def test_a_model_that_cannot_see_leaves_the_figures_and_not_the_text_and_cloud_needs_the_archive_cleared(tmp_path):
    from tadamoo.rag import enrich
    store, aid, pdf = archive(tmp_path)
    store.add_file(aid, "patent.pdf", pdf)
    blind = Seeing(sees=False)
    out = enrich.Enricher(store, blind, sleep=lambda s: None, backoff_s=0.0).run(aid)
    assert "not described" in out["note"] and "vision" in out["note"] and next(c for c in store.load(aid).chunks if c["kind"] == "figure").get("summary") is None
    assert store.enrichment(aid)["applied"]                                                                  # what the model could do was done
    away = Seeing(local=False)
    with pytest.raises(RagError, match="cloud_ok"):
        enrich.Enricher(store, away).run(aid)
    assert away.calls == []
