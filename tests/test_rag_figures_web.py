"""The pictures of web pages, wiki pages and HTML files added by hand: found in the page, fetched with the sources' own client (or read from beside the file), cut down to WebP and
linked to the passage that holds the text beside them."""
import io
from pathlib import Path

import pytest
from PIL import Image

from sources_support import Site, doc_of, make
from tadamoo.rag import RagStore, project_key
from tadamoo.rag.ingest import figures_web

PROJ = project_key("project-one")
TEXT = "The delta is wide and the wind carries the boat along the shore, where the reeds grow thick and the water is shallow for the whole afternoon."


def png(size=(300, 180), seed=0):
    img = Image.new("RGB", size, (210, 230, 250))
    for x in range(0, size[0], 6):
        for y in range(0, size[1], 4):
            img.putpixel((x, y), ((x * 3 + seed) % 255, (y * 7) % 255, 80))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PAGE = (f"<html><head><title>Delta notes</title></head><body><h1>Delta notes</h1><p>{TEXT}</p>"
        '<figure><img src="/img/map.png" alt="Map of the delta" width="300"><figcaption>Figure 1. Map of the delta</figcaption></figure>'
        '<div class="thumb"><img src="//cdn.example/x.png"><div class="thumbcaption">A second view of the shore</div></div>'
        '<img src="data:image/png;base64,AAAA"><img src="/img/logo.svg"><img src="/img/dot.png" width="16" height="16">'
        '<img src="/img/small.png" srcset="/img/small.png 1x, /img/big.png 2x" alt="Boat">'
        "<script><img src='/img/in-script.png'></script></body></html>")


def test_the_page_gives_its_pictures_with_description_caption_and_the_text_beside_each_and_leaves_out_data_svg_and_icons(tmp_path):
    page = tmp_path / "p.html"
    page.write_text(PAGE, encoding="utf-8")
    asked = []
    got = figures_web.items(page, "http://wiki.example/wiki/Delta", lambda url: (asked.append(url), b"bytes")[1])
    assert asked == ["http://wiki.example/img/map.png", "http://cdn.example/x.png", "http://wiki.example/img/big.png"]                   # the largest of a srcset; no data, svg, icon or script
    first = got[0]
    assert first["alt"] == "Map of the delta" and first["caption"] == "Figure 1. Map of the delta" and first["anchor"].startswith("The delta is wide") and first["source"] == "web"
    assert got[1]["caption"] == "A second view of the shore" and got[2]["alt"] == "Boat"


def test_a_picture_that_does_not_come_does_not_stop_the_page():
    page = Path(__file__).with_name("_tmp_web_page.html")
    page.write_text('<p>' + TEXT + '</p><img src="/a.png"><img src="/b.png">', encoding="utf-8")
    try:
        def get(url):
            if url.endswith("a.png"):
                raise OSError("refused")
            return b"ok"
        assert [i["url"] for i in figures_web.items(page, "http://h.example/", get)] == ["http://h.example/b.png"]
    finally:
        page.unlink(missing_ok=True)


def test_a_page_added_by_hand_reads_pictures_from_beside_it_and_never_from_outside_its_folder(tmp_path):
    folder = tmp_path / "site"
    (folder / "img").mkdir(parents=True)
    (folder / "img" / "map.png").write_bytes(png())
    (tmp_path / "secret.png").write_bytes(png(seed=9))
    (folder / "page.html").write_text(f'<html><body><p>{TEXT}</p><img src="img/map.png" alt="Local map"><img src="../secret.png" alt="Outside"></body></html>', encoding="utf-8")
    store = RagStore(tmp_path / "rag")
    aid = store.create("Local", project=PROJ)["id"]
    doc = store.add_file(aid, "page.html", folder / "page.html")["document"]
    assert doc["figures"] == 1
    row = next(c for c in store.load(aid).chunks if c["kind"] == "figure")
    assert "Local map" in row["text"] and "Outside" not in row["text"] and row["section"] == next(c for c in store.load(aid).chunks if c["kind"] != "figure")["section"]


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


def test_a_web_source_fetches_the_pictures_of_its_page_through_its_own_client_and_keeps_them_as_figures(tmp_path, site):
    site.pages["/img/map.png"] = png()
    site.types["/img/map.png"] = "image/png"
    site.pages["/page.html"] = (f'<html><head><title>Delta notes</title></head><body><h1>Delta notes</h1><p>{TEXT}</p><figure><img src="/img/map.png" alt="Map of the delta">'
                                '<figcaption>Figure 1. Map of the delta</figcaption></figure></body></html>').encode()
    store, sync, aid = make(tmp_path)
    sync.add_source(aid, "url", {"url": site.url + "/page.html"})
    assert sync.update(aid)["counts"] == {"added": 1}
    doc = doc_of(store, aid, "Delta notes")
    assert doc["figures"] == 1 and any(h[0] == "/img/map.png" for h in site.hits)
    row = next(c for c in store.load(aid).chunks if c["kind"] == "figure")
    assert row["figure"] == "Figure 1" and row["caption"] == "Figure 1. Map of the delta" and store.figure(aid, doc["id"], 1)["image"][:4] == b"RIFF"
