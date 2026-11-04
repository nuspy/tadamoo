"""Pictures and diagrams in an answer: the pictures of an article, one fetched into the conversation, and the instructions by level."""
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.attachments import OutputStore
from tadamoo.tools.registry import ToolRegistry
from tadamoo.tools.search import PageError
from tadamoo.visuals import IMAGE_RULES, CHART_RULES, fetch_image, format_images, guidance, page_images, register_visual_tools, sniff


def png(width=200, height=120):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


ARTICLE = b"""<html><head><title>News</title>
<meta property="og:image" content="/img/headline.png">
<link rel="stylesheet" href="/x.css"></head><body>
<header><img src="/img/logo.png" width="300" height="80" alt="site logo"></header>
<article><figure><img src="/img/photo.png" alt="The bridge at dawn" width="800" height="500"><figcaption>The bridge, seen from the north</figcaption></figure>
<img src="/img/tiny.png" width="1" height="1"><img src="data:image/gif;base64,AAAA" alt="inline"></article>
<aside><img src="/img/sidebar.png" alt="Related story"></aside></body></html>"""


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        routes = {"/article": (200, "text/html", ARTICLE), "/img/headline.png": (200, "image/png", png()), "/img/photo.png": (200, "image/png", png(400, 250)),
                  "/page.svg": (200, "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>"),
                  "/fake.png": (200, "image/png", b"<html>not a picture</html>"),
                  "/huge.png": (200, "image/png", png(10, 10) + b"\0" * 9_000_000)}
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/img/headline.png"); self.end_headers(); return
        code, kind, body = routes.get(self.path, (404, "text/plain", b"no"))
        self.send_response(code); self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


@pytest.fixture
def site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_what_the_bytes_are_decides_not_what_the_server_said():
    assert sniff(png()) == "png" and sniff(b"\xff\xd8\xff\xe0abc") == "jpg" and sniff(b"GIF89a....") == "gif" and sniff(b"RIFF\0\0\0\0WEBPVP8 ") == "webp"
    assert sniff(b"<svg></svg>") is None and sniff(b"<html>") is None and sniff(b"") is None


def test_an_article_lists_its_own_picture_first_then_the_ones_in_it_and_skips_logos_pixels_and_inline_data(site):
    found = page_images(site + "/article", allow_private=True)
    assert [f["source"] for f in found] == ["page's own", "in the article", "elsewhere on the page"]
    assert [f["url"].rsplit("/", 1)[-1] for f in found] == ["headline.png", "photo.png", "sidebar.png"]
    assert found[1]["alt"] == "The bridge at dawn" and found[1]["caption"] == "The bridge, seen from the north"
    text = format_images(site + "/article", found)
    assert "headline.png" in text and "logo" not in text and "tiny" not in text
    assert "No usable picture" in format_images("x", [])


def test_a_picture_is_fetched_through_redirects_and_a_page_a_fake_or_a_huge_file_is_refused(site):
    data, kind, final = fetch_image(site + "/redirect", allow_private=True)
    assert (kind, final.endswith("headline.png"), sniff(data)) == ("png", True, "png")
    for bad in ("/fake.png", "/page.svg", "/article", "/huge.png", "/missing.png"):
        with pytest.raises(PageError):
            fetch_image(site + bad, allow_private=True)


def test_a_private_address_is_refused_unless_the_person_allowed_it(site):
    with pytest.raises(PageError):
        fetch_image(site + "/img/headline.png")
    with pytest.raises(PageError):
        page_images(site + "/article")


def test_show_image_keeps_the_picture_in_the_conversation_and_tells_how_to_show_it(site, tmp_path):
    registry, outputs = ToolRegistry(), OutputStore(tmp_path / "out")
    register_visual_tools(registry, outputs, "s1", allow_private=True)
    assert {"page_images", "show_image"} <= {t["function"]["name"] for t in registry.schemas()} if hasattr(registry, "schemas") else True
    listing = registry.get("page_images").handler(site + "/article")
    assert "headline.png" in listing
    done = registry.get("show_image").handler(site + "/img/headline.png", "The bridge")
    assert done.startswith("[file:") and "![short caption](tadamoo-file:" in done
    (item,) = outputs.list("s1")
    assert item["name"].endswith(".png") and item["name"].startswith("The_bridge")
    assert "Could not get that picture" in registry.get("show_image").handler(site + "/fake.png", "x")
    assert registry.get("page_images").untrusted and registry.get("show_image").untrusted


def test_the_instructions_follow_the_levels_and_say_nothing_when_both_are_off():
    assert guidance("", "") == ""
    for level in IMAGE_RULES:
        assert IMAGE_RULES[level] in guidance(level, "") and "Diagrams" not in guidance(level, "")
    for level in CHART_RULES:
        assert CHART_RULES[level] in guidance("", level) and "Pictures" not in guidance("", level)
    both = guidance("high", "very_low")
    assert "page_images" in both and "mermaid" in both and "Never write a picture address" in both
    assert guidance("bogus", "") == ""
