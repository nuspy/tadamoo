"""The conversation as a document: the model built from the page's snapshot, the Markdown reader, and the Markdown, HTML, Word and
OpenDocument writers. They run on the standard library only, so no office program is needed (an optional pandoc round trip is skipped
when pandoc is absent)."""
import base64
import io
import itertools
import re
import shutil
import struct
import subprocess
import zipfile
import zlib
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

import pytest

from serve import exportdoc as E
from serve import exportoffice as O

ALL = {"prompts": True, "reasoning": True, "actions": True, "answers": True}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def png(w=40, h=20):
    raw = b"".join(b"\x00" + b"\xc8\x32\x32" * w for _ in range(h))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def jpeg(w=32, h=16):
    return b"\xff\xd8\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\xff\xd9"


def gif(w=10, h=5):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00;"


def uri(data, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


SVG = "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><rect width='10' height='10'/></svg>"
CODE = "graph TD; A-->B"


def snapshot(**over):
    snap = {
        "title": "Plan the trip", "lang": "en", "base": "http://127.0.0.1:8080/", "session": "abcd1234", "exported_at": 1792000000, "tz": 0,
        "assets": {"tadamoo-file:0123456789ab": uri(png())},
        "diagrams": {CODE: {"svg": SVG, "png": uri(png(60, 30))}},
        "items": [
            {"kind": "prompt", "text": "PROMPT-MARK where to go?\nsecond line", "time": 1792000000, "attachments": ["notes.txt"]},
            {"kind": "reasoning", "turn": 1, "text": "REASON-MARK the model thinks", "time": 1792000001},
            {"kind": "action", "turn": 1, "name": "search_web", "description": "ACTION-DESC-MARK looks it up", "arguments": {"query": "ACTION-ARG-MARK"},
             "result": "[file:0123456789ab:guide.pdf] ACTION-RESULT-MARK", "status": "ok", "sources": [{"url": "https://example.com/page?a=1&b=2", "host": "example.com"}]},
            {"kind": "answer", "turn": 1, "text": "ANSWER-MARK\n\n# Title\n\nGo to **Rome** and see [the site](https://rome.example/x).\n\n![map](tadamoo-file:0123456789ab)\n\n"
                                                  "- one\n- two\n  - nested\n\n1. first\n2. second\n\n> a quote\n\n| city | days |\n|---|---|\n| Rome | 3 |\n\n"
                                                  "```py\nprint('hi')\n```\n\n```mermaid\n" + CODE + "\n```\n", "time": 1792000002},
        ],
    }
    snap.update(over)
    return snap


def unzip(data):
    return zipfile.ZipFile(io.BytesIO(data))


def xml_text(data):
    """Every text and attribute value of an XML part, joined."""
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        out.append(el.text or "")
        out.append(el.tail or "")
        out.extend(el.attrib.values())
    return "\n".join(out)


def all_writers(doc):
    return {"md": E.write_md(doc), "html": E.write_html(doc), "docx": O.write_docx(doc), "odt": O.write_odt(doc)}


def readable(fmt, data):
    """The visible text of a written file."""
    if fmt in ("md", "html"):
        return data.decode("utf-8")
    z = unzip(data)
    return "\n".join(xml_text(z.read(n)) for n in z.namelist() if n.endswith(".xml") and "media" not in n)


# ---------------------------------------------------------------- the selection

def test_a_kind_that_is_not_ticked_never_appears():
    marks = {"prompts": "PROMPT-MARK", "reasoning": "REASON-MARK", "actions": ("ACTION-DESC-MARK", "ACTION-ARG-MARK", "ACTION-RESULT-MARK"), "answers": "ANSWER-MARK"}
    for picks in itertools.product([False, True], repeat=4):
        if not any(picks):
            continue
        sel = dict(zip(("prompts", "reasoning", "actions", "answers"), picks))
        doc = E.build(snapshot(), sel)
        for fmt, data in all_writers(doc).items():
            text = readable(fmt, data)
            for kind, mark in marks.items():
                for m in (mark,) if isinstance(mark, str) else mark:
                    assert (m in text) == sel[kind], (fmt, sel, m)


def test_nothing_ticked_is_refused():
    with pytest.raises(E.ExportError):
        E.build(snapshot(), {"prompts": False, "reasoning": False, "actions": False, "answers": False})
    with pytest.raises(E.ExportError):
        E.build(snapshot(), {"prompts": "yes"})                                      # only a real true counts
    with pytest.raises(E.ExportError):
        E.build({"items": "x"}, ALL)


def test_sources_and_documents_of_the_actions_go_with_the_answers_even_when_the_actions_are_not_chosen():
    doc = E.build(snapshot(), {"answers": True})
    md = E.write_md(doc).decode()
    assert "https://example.com/page?a=1&b=2" in md and "guide.pdf" in md
    assert "http://127.0.0.1:8080/api/outputs/abcd1234/0123456789ab" in md
    assert "ACTION-RESULT-MARK" not in md and "search_web" not in md
    without = E.build(snapshot(), {"prompts": True}).blocks
    assert not any(isinstance(b, E.Lst) for b in without)                              # no answers: no list of sources


def test_each_answer_has_its_own_sources_and_a_new_request_starts_a_new_group():
    items = [
        {"kind": "prompt", "text": "q1"},
        {"kind": "action", "turn": 1, "name": "browse_url", "result": "x", "sources": [{"url": "https://one.example/"}]},
        {"kind": "answer", "turn": 1, "text": "a1"},
        {"kind": "prompt", "text": "q2"},
        {"kind": "action", "turn": 3, "name": "browse_url", "result": "x", "sources": [{"url": "https://two.example/"}]},
        {"kind": "answer", "turn": 3, "text": "a2"},
    ]
    md = E.write_md(E.build(snapshot(items=items), {"prompts": True, "answers": True})).decode()
    assert md.index("https://one.example/") < md.index("q2") < md.index("https://two.example/")


# ---------------------------------------------------------------- hostile text

HOSTILE = "<script>alert(1)</script> ]]> \x00\x01\x0b\x1b\ud800 &amp; \"quote\" 'single' <img src=x onerror=alert(2)>"


def hostile_snapshot():
    evil_links = ("[a](javascript:alert(1)) [b](JaVaScRiPt:alert(2)) [c](data:text/html,<script>x</script>) [d](file:///c:/secret) [e](vbscript:x) "
                  "[f](//evil.example/x) [g](https://user:pass@evil.example/) [h](http://ok.example/a\"onmouseover=\"x) ![i](javascript:alert(3)) "
                  "![j\" onerror=\"alert(4)](data:text/html;base64,PHNjcmlwdD4=) <javascript:alert(5)> [k](mailto:me@example.com) [l](tadamoo-file:ZZZ)")
    return snapshot(
        title=HOSTILE, assets={"tadamoo-file:0123456789ab": "data:image/png;base64," + base64.b64encode(b"<script>alert(1)</script>").decode()},
        items=[
            {"kind": "prompt", "text": HOSTILE, "attachments": [HOSTILE], "images": [{"name": HOSTILE, "url": "data:text/html;base64,PHNjcmlwdD4="}]},
            {"kind": "reasoning", "turn": 1, "text": HOSTILE},
            {"kind": "action", "turn": 1, "name": HOSTILE, "description": HOSTILE, "arguments": {"x": HOSTILE}, "result": HOSTILE,
             "sources": [{"url": "javascript:alert(1)", "host": HOSTILE}, {"url": "https://ok.example/", "host": HOSTILE}]},
            {"kind": "answer", "turn": 1, "text": HOSTILE + "\n\n" + evil_links + "\n\n```" + HOSTILE.replace("\n", " ") + "\n" + HOSTILE + "\n```\n\n| " + HOSTILE + " |\n|---|\n| " + HOSTILE + " |\n"},
        ])


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, k, v) for k, v in attrs)


def test_hostile_text_is_escaped_in_every_format():
    doc = E.build(hostile_snapshot(), ALL)
    for fmt, data in all_writers(doc).items():
        raw = data.decode("utf-8") if fmt in ("md", "html") else None
        if fmt == "html":
            parser = Tags()
            parser.feed(raw)
            assert "script" not in parser.tags and "iframe" not in parser.tags, fmt
            for tag, key, value in parser.attrs:
                assert not key.startswith("on"), (tag, key)
                if key == "href":
                    assert re.match(r"(https?://|mailto:)", value), value
                if key == "src":
                    assert value.startswith("data:image/"), value
            assert "<script" not in raw.lower().replace("&lt;script", "")
        elif fmt == "md":
            assert "](javascript" not in raw.lower() and "](data:text" not in raw.lower() and "](file:" not in raw.lower() and "](vbscript" not in raw.lower()
            assert "\x00" not in raw and "\x1b" not in raw
        else:
            z = unzip(data)
            for name in z.namelist():
                if name.endswith((".xml", ".rels")):
                    part = z.read(name)
                    ET.fromstring(part)                                                   # well formed
                    text = part.decode("utf-8")
                    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text), name
                    assert "<script" not in text.lower(), name
                    for el in ET.fromstring(part).iter():                                 # as text it is only text; no attribute may carry one
                        for value in el.attrib.values():
                            assert not re.match(r"\s*(javascript|vbscript|file|data):", value, re.I), (name, value)
        if fmt in ("md", "html"):
            assert "\ud800" not in raw


def test_docx_hyperlink_targets_are_only_http_https_and_mailto():
    z = unzip(O.write_docx(E.build(hostile_snapshot(), ALL)))
    rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    targets = [r.attrib["Target"] for r in rels if r.attrib.get("TargetMode") == "External"]
    assert targets and all(re.match(r"(https?://|mailto:)", t) for t in targets), targets
    assert not any("user:pass" in t or "evil.example" in t for t in targets)


def test_odt_links_are_only_http_https_and_mailto():
    z = unzip(O.write_odt(E.build(hostile_snapshot(), ALL)))
    root = ET.fromstring(z.read("content.xml"))
    hrefs = [el.attrib["{http://www.w3.org/1999/xlink}href"] for el in root.iter("{urn:oasis:names:tc:opendocument:xmlns:text:1.0}a")]
    assert hrefs and all(re.match(r"(https?://|mailto:)", h) for h in hrefs), hrefs


def test_an_image_must_really_be_a_png_jpeg_or_gif():
    assert E.decode_data_url(uri(png())).mime == "image/png"
    assert E.decode_data_url(uri(jpeg(), "image/jpeg")).mime == "image/jpeg"
    got = E.decode_data_url(uri(gif(), "image/gif"))
    assert (got.mime, got.width, got.height) == ("image/gif", 10, 5)
    assert E.decode_data_url(uri(b"<html><script>1</script></html>")) is None
    assert E.decode_data_url(uri(b"RIFF....WEBPVP8 ", "image/webp")) is None
    assert E.decode_data_url("data:image/png;base64,@@@") is None
    assert E.decode_data_url("https://example.com/a.png") is None
    assert E.decode_data_url(uri(png(70000, 1))) is None                                # absurd size


def test_a_picture_that_is_not_one_becomes_a_note_not_a_broken_image():
    doc = E.build(hostile_snapshot(), ALL)
    for fmt, data in all_writers(doc).items():
        if fmt in ("docx", "odt"):
            assert not any(n.startswith(("word/media", "Pictures")) for n in unzip(data).namelist()), fmt


def test_a_diagram_svg_with_a_doctype_is_not_used():
    bad = '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>'
    assert E.clean_svg(bad) == "" and E.clean_svg("<html></html>") == "" and E.clean_svg(SVG) == SVG
    snap = snapshot(diagrams={CODE: {"svg": bad, "png": ""}})
    html = E.write_html(E.build(snap, ALL)).decode()
    assert "image/svg+xml" not in html and "graph TD" in html


def test_urls():
    for ok in ("https://example.com/a?b=1", "http://example.com", "mailto:a@example.com", "https://例え.jp/x"):
        assert E.safe_url(ok)
    for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x.example/", "//x.example/", "https://u:p@x.example/", "https://x.example/a b",
                "https://x.example/\x01", "", "http://", "tadamoo-file:0123456789ab", None, 5):
        assert E.safe_url(bad) is None, bad


# ---------------------------------------------------------------- Markdown -> blocks

def ctx(**over):
    return E.Context(snapshot(**over), E.LABELS["en"])


def runs_text(runs):
    return "".join("\n" if r.br else r.text for r in runs)


def test_the_reader_handles_the_blocks_the_chat_shows():
    text = "# H1\n## H2\n\npara one\nsame para\n\n- a\n- b\n  - c\n\n1. x\n2. y\n\n> q1\n> q2\n\n---\n\n| h1 | h2 |\n|:--|--:|\n| 1 | 2 |\n| 3 |\n\n```js\nlet a = 1;\n```\n"
    blocks = E.parse_blocks(text, ctx(), shift=2)
    kinds = [type(b).__name__ for b in blocks]
    assert kinds == ["Heading", "Heading", "Paragraph", "Lst", "Lst", "Quote", "Rule", "Table", "Code"]
    assert [b.level for b in blocks[:2]] == [3, 4]                                      # pushed under the message's own heading
    assert runs_text(blocks[2].runs) == "para one\nsame para"
    bullets, ordered = blocks[3], blocks[4]
    assert not bullets.ordered and ordered.ordered and len(bullets.items) == 2 and isinstance(bullets.items[1][1], E.Lst)
    assert len(blocks[7].rows) == 2 and blocks[7].rows[1][1] == []                      # a short row is padded
    assert blocks[8].lang == "js" and blocks[8].text == "let a = 1;"


def test_an_unclosed_fence_takes_the_rest_as_code_and_deep_nesting_is_bounded():
    blocks = E.parse_blocks("text\n```\nnever closed\n- not a list", ctx())
    assert isinstance(blocks[-1], E.Code) and "not a list" in blocks[-1].text
    deep = "\n".join("  " * i + "- x" for i in range(60))
    assert E.parse_blocks(deep, ctx())                                                  # no recursion error
    assert E.parse_blocks("> " * 200 + "x", ctx())


def test_inline_marks():
    c = ctx()
    runs = E.parse_inline("a **bold** and *it* and ***both*** and `co*de` and snake_case_name and 2*3*4", c)
    flags = [(r.text, r.bold, r.italic, r.code) for r in runs if r.text.strip()]
    assert ("bold", True, False, False) in flags and ("it", False, True, False) in flags and ("both", True, True, False) in flags
    assert ("co*de", False, False, True) in flags
    assert "snake_case_name" in runs_text(runs)                                          # underscores inside a word stay
    assert runs_text(E.parse_inline(r"\*not italic\* and \[not a link\]", c)) == "*not italic* and [not a link]"
    links = [r for r in E.parse_inline("see https://example.com/a_b, and (https://x.example/y).", c) if r.href]
    assert [r.href for r in links] == ["https://example.com/a_b", "https://x.example/y"]


def test_links_to_the_apps_files_become_the_servers_address_or_a_note():
    runs = E.parse_inline("[the guide](tadamoo-file:0123456789ab) [code](tadamoo-path:src/a%20b.py)", ctx())
    hrefs = [r.href for r in runs if r.href]
    assert hrefs[0] == "http://127.0.0.1:8080/api/outputs/abcd1234/0123456789ab"
    assert hrefs[1] == "http://127.0.0.1:8080/api/agent/sessions/abcd1234/file?path=src%2Fa%2520b.py"
    bare = E.Context({"items": []}, E.LABELS["it"])                                      # no address known: the name stays, with a note
    out = runs_text(E.parse_inline("[la guida](tadamoo-file:0123456789ab)", bare))
    assert "la guida" in out and "senza indirizzo di rete" in out
    assert not [r for r in E.parse_inline("[la guida](tadamoo-file:0123456789ab)", bare) if r.href]
    assert not [r for r in E.parse_inline("[x](tadamoo-file:0123456789ab)", E.Context({"base": "ftp://x/", "session": "s", "items": []}, E.LABELS["en"])) if r.href]


def test_an_image_in_an_answer_is_embedded_or_named():
    runs = E.parse_inline("![map](tadamoo-file:0123456789ab) ![gone](tadamoo-file:ffffffffffff) ![web](https://example.com/p.png)", ctx())
    assert runs[0].image and runs[0].image.alt == "map" and (runs[0].image.width, runs[0].image.height) == (40, 20)
    assert any(r.text == "gone" and r.href.endswith("/api/outputs/abcd1234/ffffffffffff") for r in runs)      # not embedded: its address instead
    assert any(r.href == "https://example.com/p.png" for r in runs)                      # a picture on the web is a link: the server fetches nothing
    bare = E.Context({"items": []}, E.LABELS["en"])
    assert "[gone: image not included]" in runs_text(E.parse_inline("![gone](tadamoo-file:ffffffffffff)", bare))


# ---------------------------------------------------------------- Markdown and HTML

def test_markdown_output():
    md = E.write_md(E.build(snapshot(), ALL)).decode()
    assert md.startswith("# Plan the trip\n")
    for needle in ("## You", "## Assistant", "**Reasoning**", "> REASON-MARK", "```json", "[the site](https://rome.example/x)", "![map](data:image/png;base64,",
                   "- one", "1. first", "| city | days |", "```py", "```mermaid\n" + CODE, "**Sources**", "**Documents**"):
        assert needle in md, needle
    assert "### Title" in md                                                           # the answer's "# Title" sits under "## Assistant"


def test_one_message_saved_bare_is_the_text_alone_in_every_format():
    items = [{"kind": "answer", "turn": 1, "time": 1792000000, "text": "# Chapter one\n\nThe river runs\n\n## Part\n\n- one\n- two"}]
    doc = E.build(snapshot(items=items, title="A long chapter"), {"answers": True}, bare=True)
    md = E.write_md(doc).decode()
    assert md == "# Chapter one\n\nThe river runs\n\n## Part\n\n- one\n- two\n"                  # no title line, no export header, no speaker, headings in place
    html = E.write_html(doc).decode()
    assert "<title>A long chapter</title>" in html and html.count("<h1>") == 1 and "Assistant" not in html
    for fmt, data in all_writers(doc).items():
        text = readable(fmt, data)
        assert "The river runs" in text and "Exported" not in text and "Assistant" not in text, fmt
    full = E.write_md(E.build(snapshot(items=items, title="A long chapter"), {"answers": True})).decode()
    assert full.startswith("# A long chapter") and "## Assistant" in full and "### Chapter one" in full


def test_markdown_does_not_turn_text_into_structure():
    items = [{"kind": "prompt", "text": "# not a heading\n- not a list\n1. not numbered\n*stars* and _under_ and [x](y) <b>"}]
    md = E.write_md(E.build(snapshot(items=items), {"prompts": True})).decode()
    assert "\\# not a heading" in md and "\\- not a list" in md and "1\\. not numbered" in md
    assert "\\*stars\\*" in md and "\\[x\\]" in md and "\\<b\\>" in md


def test_html_is_one_self_contained_file():
    html = E.write_html(E.build(snapshot(), ALL)).decode()
    assert html.startswith("<!doctype html>") and "<style>" in html and "Content-Security-Policy" in html and "<script" not in html
    assert "default-src 'none'" in html and "img-src data:" in html
    assert not re.search(r'(src|href)="(?!data:|https?://|mailto:)', html)             # nothing else is loaded or linked
    assert "data:image/svg+xml;base64," in html and "<details>" in html                 # the diagram, with its source one click away
    assert html.count("<img") == 2                                                      # the picture and the diagram
    assert 'class="hint"' not in html and 'class="hint"' in E.write_html(E.build(snapshot(), ALL), print_hint=True).decode()


def test_italian_labels_and_local_time():
    snap = snapshot(lang="it", tz=-120, exported_at=1792000000)
    md = E.write_md(E.build(snap, ALL)).decode()
    assert "## Tu · 2026-10-14 19:46" in md and "## Assistente" in md and "**Ragionamento**" in md and "Esportata il 2026-10-14 19:46" in md


# ---------------------------------------------------------------- Word

def test_docx_package_is_complete_and_consistent():
    data = O.write_docx(E.build(snapshot(), ALL))
    z = unzip(data)
    names = set(z.namelist())
    for need in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "word/numbering.xml", "word/_rels/document.xml.rels", "docProps/core.xml"):
        assert need in names, need
    for name in names:
        if name.endswith((".xml", ".rels")):
            ET.fromstring(z.read(name))
    types = ET.fromstring(z.read("[Content_Types].xml"))
    overrides = {el.attrib["PartName"] for el in types if el.tag.endswith("Override")}
    defaults = {el.attrib["Extension"] for el in types if el.tag.endswith("Default")}
    for name in names:
        if name != "[Content_Types].xml":
            assert "/" + name in overrides or name.rsplit(".", 1)[-1] in defaults, name   # every part has a type
    rels = {r.attrib["Id"]: r for r in ET.fromstring(z.read("word/_rels/document.xml.rels"))}
    for r in rels.values():
        if r.attrib.get("TargetMode") != "External":
            assert "word/" + r.attrib["Target"] in names, r.attrib
    doc = z.read("word/document.xml").decode()
    for rid in re.findall(r'r:(?:id|embed)="(rId\d+)"', doc):
        assert rid in rels, rid                                                          # every reference has its relationship
    assert len(set(re.findall(r'<wp:docPr id="(\d+)"', doc))) == len(re.findall(r"<wp:docPr ", doc))
    styles = z.read("word/styles.xml").decode()
    for used in set(re.findall(r'<w:pStyle w:val="(\w+)"', doc)) | set(re.findall(r'<w:rStyle w:val="(\w+)"', doc)) | set(re.findall(r'<w:tblStyle w:val="(\w+)"', doc)):
        assert f'w:styleId="{used}"' in styles, used
    nums = z.read("word/numbering.xml").decode()
    for used in set(re.findall(r'<w:numId w:val="(\d+)"', doc)):
        assert f'<w:num w:numId="{used}">' in nums, used
    assert doc.count("<w:tbl>") == 1 and "<w:tblHeader/>" in doc
    assert doc.rstrip().endswith("</w:sectPr></w:body></w:document>") and "</w:tbl><w:p>" in doc    # a table is followed by a paragraph


def test_docx_ordered_lists_restart_and_the_diagram_is_a_picture():
    snap = snapshot(items=[{"kind": "answer", "turn": 0, "text": "1. a\n2. b\n\ntext\n\n1. c\n2. d\n\n```mermaid\n" + CODE + "\n```\n"}])
    z = unzip(O.write_docx(E.build(snap, {"answers": True})))
    doc = z.read("word/document.xml").decode()
    ids = re.findall(r'<w:numId w:val="(\d+)"/>', doc)
    assert len(set(ids)) == 2                                                            # two lists, two numberings
    assert [n for n in z.namelist() if n.startswith("word/media/")] == ["word/media/image1.png"]


def test_docx_without_a_picture_of_the_diagram_keeps_its_source():
    snap = snapshot(diagrams={CODE: {"svg": SVG, "png": ""}}, assets={})
    z = unzip(O.write_docx(E.build(snap, ALL)))
    assert not [n for n in z.namelist() if n.startswith("word/media/")]
    assert CODE.replace(">", "&gt;") in z.read("word/document.xml").decode()
    plain = snapshot(diagrams={}, assets={})
    assert CODE.replace(">", "&gt;") in unzip(O.write_docx(E.build(plain, ALL))).read("word/document.xml").decode()


def test_docx_text_keeps_tabs_spaces_and_lines_in_code():
    snap = snapshot(items=[{"kind": "answer", "turn": 0, "text": "```\nif x:\n\treturn  1\n\n  end\n```"}])
    doc = unzip(O.write_docx(E.build(snap, {"answers": True}))).read("word/document.xml").decode()
    assert "<w:tab/>" in doc and 'xml:space="preserve">return  1<' in doc
    assert doc.count('<w:pStyle w:val="Code"/>') == 4                                   # one paragraph for each line, the empty one too


# ---------------------------------------------------------------- OpenDocument

def test_odt_package_is_complete_and_consistent():
    data = O.write_odt(E.build(snapshot(), ALL))
    z = unzip(data)
    first = z.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED and not first.extra
    assert z.read("mimetype") == b"application/vnd.oasis.opendocument.text"
    names = set(z.namelist())
    for name in names:
        if name.endswith(".xml"):
            ET.fromstring(z.read(name))
    manifest = ET.fromstring(z.read("META-INF/manifest.xml"))
    listed = {el.attrib["{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}full-path"] for el in manifest}
    assert listed - {"/"} == names - {"mimetype", "META-INF/manifest.xml"}
    content = z.read("content.xml").decode()
    for href in re.findall(r'<draw:image xlink:href="([^"]+)"', content):
        assert href in names, href
    styles = z.read("styles.xml").decode() + content
    for used in set(re.findall(r'text:style-name="([^"]+)"', content)) | set(re.findall(r'table:style-name="([^"]+)"', content)):
        assert f'style:name="{used}"' in styles, used                                    # every style used is defined
    assert "<table:table " in content and "<table:table-header-rows>" in content and "<text:list " in content
    assert len(set(re.findall(r'draw:name="([^"]+)"', content))) == content.count("<draw:frame ")


def test_odt_keeps_spaces_tabs_and_lines():
    assert O._odf_text("a  b\tc\nd e", at_start=True) == 'a<text:s text:c="2"/>b<text:tab/>c<text:line-break/>d e'
    assert O._odf_text("  lead") == '<text:s text:c="2"/>lead'
    assert O._odf_text("x &lt; <y>") == "x &amp;lt; &lt;y&gt;"


# ---------------------------------------------------------------- the entry point

def test_file_names_are_safe():
    f = E.file_name
    assert f("Plan the trip 2026-10-27", "docx") == "Plan the trip 2026-10-27.docx"
    assert f("../../etc/passwd", "md") == "etc passwd.md" or "/" not in f("../../etc/passwd", "md")
    assert "\\" not in f("a\\b:c*d?e\"f<g>h|i", "html") and ":" not in f("a:b", "odt")
    assert f("CON", "pdf") == "conversation.pdf" and f("", "docx") == "conversation.docx" and f(None, "docx") == "conversation.docx"
    assert f("report.docx", "docx") == "report.docx" and f("report.md", "docx") == "report.md.docx"
    assert f("name... ", "md") == "name.md" and len(f("x" * 500, "md")) <= 124
    assert f("tab\tand\nnewline\x00", "md") == "tab and newline.md"
    assert f("Riunione è già finita", "odt") == "Riunione è già finita.odt"


def test_export_returns_bytes_type_and_name_for_each_format():
    for fmt, ctype in (("docx", "wordprocessingml"), ("odt", "opendocument.text"), ("md", "markdown"), ("html", "text/html"), ("pdf", "text/html")):
        data, content_type, name = E.export({"snapshot": snapshot(), "select": ALL, "format": fmt, "name": "my chat"})
        assert ctype in content_type and name == f"my chat.{fmt}" and data
    with pytest.raises(E.ExportError):
        E.export({"snapshot": snapshot(), "select": ALL, "format": "exe"})
    with pytest.raises(E.ExportError):
        E.export([])
    assert b"class=\"hint\"" in E.export({"snapshot": snapshot(), "select": ALL, "format": "pdf", "hint": True})[0]


def test_very_large_or_odd_input_does_not_break():
    many = [{"kind": "answer", "turn": i, "text": f"item {i} **x** [l](https://e.example/{i})"} for i in range(E.MAX_ITEMS + 50)]
    doc = E.build(snapshot(items=many + ["junk", None, {"kind": "nope"}, {"kind": "prompt", "text": 5}]), ALL)
    assert E.write_md(doc)
    odd = snapshot(items=[{"kind": "answer", "text": None}, {"kind": "action", "name": 7, "arguments": float("nan"), "result": ["x"], "sources": "no"},
                          {"kind": "prompt", "text": "x", "time": "soon", "images": "no", "attachments": "no"}], tz="far", exported_at=float("inf"))
    for fmt, data in all_writers(E.build(odd, ALL)).items():
        assert data, fmt


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc is optional")
def test_pandoc_reads_what_is_written(tmp_path):
    doc = E.build(snapshot(), ALL)
    for fmt, data in (("docx", O.write_docx(doc)), ("odt", O.write_odt(doc))):
        path = tmp_path / f"t.{fmt}"
        path.write_bytes(data)
        out = subprocess.run(["pandoc", str(path), "-t", "plain"], capture_output=True, text=True, timeout=60, encoding="utf-8")
        assert out.returncode == 0 and "ANSWER-MARK" in out.stdout and "Rome" in out.stdout, out.stderr
