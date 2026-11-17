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
