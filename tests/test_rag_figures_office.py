"""The figures of a Word file: pictures taken from the file in the order of the text, linked to the passage that holds the text beside them, kept as images and as passages."""
import io
import zipfile

import pytest
from PIL import Image

from tadamoo.rag import RagStore, project_key
from tadamoo.rag.ingest import figures, figures_office

PROJ = project_key("project-one")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSDECL = (f'xmlns:w="{W}" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
INTRO = "The tetracycline response element controls the expression of the reporter gene in the plasmid that was built for the experiments described here."
AFTER = "As shown in Figure 1, the response element sits upstream of the promoter and the reporter follows it by a short linker sequence."


def png(size, seed=0):
    img = Image.new("RGB", size, (200, 220, 255))
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 5):
            img.putpixel((x, y), ((x * 3 + seed) % 255, (y * 5) % 255, 90))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def para(text="", style=""):
    pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{pr}<w:r><w:t>{text}</w:t></w:r></w:p>" if text else "<w:p/>"


def picture(rid, alt):
    return (f'<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="{alt}"/><a:graphic><a:graphicData><pic:pic><pic:blipFill>'
            f'<a:blip r:embed="{rid}"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')


def make_docx(path):
    body = (para("Reporter plasmid", "Heading1") + para(INTRO) + picture("rId1", "Map of the plasmid with the tetracycline response element") + para("Figure 1. Map of the plasmid", "Caption")
            + picture("rId2", "bullet") + para(AFTER) + picture("rId3", "same map again"))
    rels = "".join(f'<Relationship Id="rId{i}" Type="x" Target="media/image{i}.png"/>' for i in (1, 2, 3))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NSDECL}><w:body>{body}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("word/media/image1.png", png((320, 200)))
        z.writestr("word/media/image2.png", png((20, 20), 3))                  # a bullet
        z.writestr("word/media/image3.png", png((320, 200)))                   # the same picture again
    return path


def test_the_pictures_come_from_the_file_in_order_with_their_description_caption_and_the_text_beside_them(tmp_path):
    items = figures_office.docx_items(make_docx(tmp_path / "a.docx"))
    assert [i["alt"] for i in items] == ["Map of the plasmid with the tetracycline response element", "bullet", "same map again"]
    first = items[0]
    assert first["caption"] == "Figure 1. Map of the plasmid" and first["anchor"].startswith("The tetracycline response element") and first["source"] == "docx"
    assert figures_office.docx_items(tmp_path / "missing.docx") == [] and figures_office.docx_items(tmp_path / "x.docx") == []


def test_a_small_picture_and_a_repeated_one_are_left_out_and_the_rest_is_saved_as_webp_with_a_label_and_the_sentence_that_names_it(tmp_path):
    items = figures_office.docx_items(make_docx(tmp_path / "a.docx"))
    blocks = [{"type": "paragraph", "text": AFTER, "page": 1}]
    got = figures_office.finish(items, blocks, tmp_path / "out")
    assert len(got) == 1 and got[0]["label"] == "Figure 1" and got[0]["page"] == 0 and got[0]["refs"] == [AFTER]
    assert Image.open(tmp_path / "out" / got[0]["image"]).format == "WEBP" and (tmp_path / "out" / got[0]["thumb"]).is_file() and len(got[0]["sha"]) == 24
    text = figures.passage_text(got[0])
    assert text.startswith("Figure 1: Figure 1. Map of the plasmid") and "(page" not in text.splitlines()[0] and "Description in the document: Map of the plasmid" in text


def test_a_word_file_added_to_an_archive_keeps_its_figure_linked_to_the_passage_that_holds_the_text_beside_it(tmp_path):
    store = RagStore(tmp_path / "rag")
    aid = store.create("Plasmids", project=PROJ)["id"]
    doc = store.add_file(aid, "plasmid.docx", make_docx(tmp_path / "plasmid.docx"))["document"]
    assert doc["figures"] == 1
    chunks = store.load(aid).chunks
    row = next(c for c in chunks if c["kind"] == "figure")
    home = next(c for c in chunks if c["kind"] != "figure" and "tetracycline response element controls" in c["text"])
    assert row["section"] == home["section"] and row["path"] == home["path"] and row["figure"] == "Figure 1" and row["image_sha"]
    assert store.figure(aid, doc["id"], 1)["image"][:4] == b"RIFF"
    found = store.search([aid], "map of the plasmid", top_k=3, filters={"kind": "figure"})["hits"]
    assert found and found[0]["kind"] == "figure"
    text_hits = [h for h in store.search([aid], "tetracycline response element expression reporter gene", top_k=5)["hits"] if h["kind"] != "figure"]
    assert text_hits and text_hits[0]["figures"] == [{"fig": 1, "label": "Figure 1", "caption": row["caption"][:200]}]                # the figure completes its passage
    assert found[0]["figures"] == []                                                                                            # a figure passage has none: it is the figure


def test_a_scan_is_one_figure_and_a_presentation_a_workbook_and_a_book_give_the_pictures_kept_inside_them(tmp_path):
    scan = tmp_path / "scan.png"
    scan.write_bytes(png((400, 300), 2))
    got = figures.extract(scan, "scan.png", {}, [], tmp_path / "o1")
    assert len(got) == 1 and got[0]["source"] == "image" and Image.open(tmp_path / "o1" / got[0]["image"]).format == "WEBP"
    for ext, folder in ((".pptx", "ppt/media/"), (".xlsx", "xl/media/"), (".epub", "OEBPS/images/")):
        pkg = tmp_path / f"a{ext}"
        with zipfile.ZipFile(pkg, "w") as z:
            z.writestr(folder + "b.png", png((300, 200), 5))
            z.writestr(folder + "a.png", png((300, 200), 1))
            z.writestr(folder + "icon.png", png((16, 16)))
            z.writestr("other/c.png", png((300, 200), 8))
        made = figures.extract(pkg, pkg.name, {}, [], tmp_path / ("o" + ext[1:]))
        want = 2 if ext != ".epub" else 3                                  # an EPUB keeps its pictures anywhere; the others in one folder
        assert len(made) == want and made[0]["source"] == ext[1:], ext
    assert figures.extract(tmp_path / "x.txt", "x.txt", {}, [], tmp_path / "o9") == []


def make_epub(path):
    container = '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest><item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    one = f'<html><body><p>{INTRO}</p><figure><img src="../images/map.png" alt="Map of the plasmid"/><figcaption>Figure 1. The plasmid map.</figcaption></figure><p>{AFTER}</p></body></html>'
    two = f'<html><body><p>A second chapter that talks about the sequencing results in some detail for the reader.</p><img src="../images/gel.png" alt="Gel"/><img src="../images/map.png" alt="Again"/></body></html>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/text/one.xhtml", one)
        z.writestr("OEBPS/text/two.xhtml", two)
        z.writestr("OEBPS/images/map.png", png((300, 200), 3))
        z.writestr("OEBPS/images/gel.png", png((300, 200), 9))


def test_an_epub_gives_its_pictures_in_the_order_of_the_book_each_with_its_caption_and_the_text_beside_it(tmp_path):
    book = tmp_path / "book.epub"
    make_epub(book)
    items = figures_office.epub_items(book)
    assert [i["alt"] for i in items] == ["Map of the plasmid", "Gel"]                       # the spine order, not the manifest's; a picture used twice is kept once
    assert items[0]["caption"] == "Figure 1. The plasmid map." and items[0]["anchor"].startswith("The tetracycline") and items[0]["source"] == "epub"
    assert items[1]["anchor"].startswith("A second chapter")
    made = figures.extract(book, "book.epub", {}, [], tmp_path / "o")
    assert len(made) == 2 and made[0]["source"] == "epub"

