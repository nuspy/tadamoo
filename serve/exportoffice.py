"""The Word (.docx) and OpenDocument (.odt) writers for serve/exportdoc.py: each is a zip package of hand-built XML, with only the
standard library. Every text and attribute value goes through `esc`, which also drops the characters XML cannot hold."""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from serve.exportdoc import (Code, Diagram, Doc, Heading, Image, Label, Lst, Meta, Paragraph, Quote, Rule, Run, Table, clean)

TEXT_WIDTH_PX = 600                 # the widest an image is drawn, in CSS pixels (A4 with 2 cm margins)
TEXT_HEIGHT_PX = 800


def esc(text: Any) -> str:
    """Text for an XML text node or a double-quoted attribute."""
    return (clean(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _fit(img: Image) -> Tuple[float, float]:
    """The size an image is drawn at, in CSS pixels: its own, shrunk to fit the page."""
    w, h = float(img.width), float(img.height)
    scale = min(1.0, TEXT_WIDTH_PX / w, TEXT_HEIGHT_PX / h)
    return max(1.0, w * scale), max(1.0, h * scale)


def _zip(parts: List[Tuple[str, bytes]], stored_first: Optional[str] = None) -> bytes:
    buf = io.BytesIO()
    now = time.gmtime()[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts:
            info = zipfile.ZipInfo(name, date_time=now)
            info.compress_type = zipfile.ZIP_STORED if name == stored_first else zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, data)
    return buf.getvalue()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ======================================================================== Word

W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
EMU = 9525                          # per CSS pixel
TEXT_TWIPS = 9638                   # A4 width less 2 cm margins


def _style(sid: str, name: str, kind: str = "paragraph", based: str = "", nxt: str = "", ppr: str = "", rpr: str = "", q: bool = True, extra: str = "") -> str:
    return (f'<w:style w:type="{kind}" w:styleId="{sid}"><w:name w:val="{name}"/>'
            + (f'<w:basedOn w:val="{based}"/>' if based else "") + (f'<w:next w:val="{nxt}"/>' if nxt else "")
            + ("<w:qFormat/>" if q else "") + (f"<w:pPr>{ppr}</w:pPr>" if ppr else "") + (f"<w:rPr>{rpr}</w:rPr>" if rpr else "") + extra + "</w:style>")


def _docx_styles(lang: str) -> str:
    mono = '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas" w:eastAsia="Consolas"/>'

    def head(n: int, size: int, before: int) -> str:
        return _style(f"Heading{n}", f"heading {n}", based="Normal", nxt="Normal",
                      ppr=f'<w:keepNext/><w:keepLines/><w:spacing w:before="{before}" w:after="80"/><w:outlineLvl w:val="{n - 1}"/>',
                      rpr=f'<w:b/><w:color w:val="1F2933"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')

    rules = (
        _style("Normal", "Normal").replace('<w:style w:type="paragraph"', '<w:style w:type="paragraph" w:default="1"', 1)
        + _style("Title", "Title", based="Normal", nxt="Normal", ppr='<w:spacing w:after="120"/>', rpr='<w:b/><w:sz w:val="48"/><w:szCs w:val="48"/>')
        + head(1, 36, 360) + head(2, 30, 320) + head(3, 26, 240) + head(4, 24, 200) + head(5, 22, 160) + head(6, 22, 160)
        + _style("Meta", "Basalt Meta", based="Normal", ppr='<w:spacing w:before="40" w:after="40"/>', rpr='<w:i/><w:color w:val="57606A"/><w:sz w:val="20"/><w:szCs w:val="20"/>')
        + _style("Label", "Basalt Label", based="Normal", nxt="Normal", ppr='<w:keepNext/><w:spacing w:before="200" w:after="40"/>', rpr="<w:b/>")
        + _style("Code", "Basalt Code", based="Normal",
                 ppr=('<w:keepLines/><w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="D8DEE4"/><w:left w:val="single" w:sz="4" w:space="4" w:color="D8DEE4"/>'
                      '<w:bottom w:val="single" w:sz="4" w:space="4" w:color="D8DEE4"/><w:right w:val="single" w:sz="4" w:space="4" w:color="D8DEE4"/></w:pBdr>'
                      '<w:shd w:val="clear" w:color="auto" w:fill="F4F6F8"/><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/><w:contextualSpacing/>'),
                 rpr=mono + '<w:sz w:val="18"/><w:szCs w:val="18"/>')
        + _style("Quote", "Quote", based="Normal", ppr='<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="B8C0C8"/></w:pBdr><w:ind w:left="360"/>',
                 rpr='<w:color w:val="3A424A"/>')
        + _style("ListParagraph", "List Paragraph", based="Normal", ppr='<w:spacing w:after="40"/>')
        + _style("TableText", "Basalt Table Text", based="Normal", ppr='<w:spacing w:before="40" w:after="40"/>')
        + _style("DefaultParagraphFont", "Default Paragraph Font", kind="character", q=False, extra="").replace('w:type="character"', 'w:type="character" w:default="1"')
        + _style("InlineCode", "Basalt Inline Code", kind="character", rpr=mono + '<w:shd w:val="clear" w:color="auto" w:fill="EEF1F4"/>')
        + _style("Hyperlink", "Hyperlink", kind="character", q=False, rpr='<w:color w:val="0563C1"/><w:u w:val="single"/>')
        + '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/>'
          '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
          '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
        + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/>'
          '<w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/><w:left w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/>'
          '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/><w:right w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/>'
          '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="C4CCD4"/></w:tblBorders></w:tblPr></w:style>'
        + '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/></w:style>'
    )
    locale = "it-IT" if lang == "it" else "en-US"
    return (XML_HEAD + f'<w:styles {W_NS}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
            f'<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="{locale}" w:eastAsia="{locale}" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>' + rules + "</w:styles>")


def _docx_numbering(ordered_ids: List[int]) -> str:
    def levels(bullet: bool) -> str:
        out = []
        for lvl in range(9):
            left, sym = 720 * (lvl + 1), ("•", "o", "▪")[lvl % 3]
            fmt = ('<w:numFmt w:val="bullet"/>' f'<w:lvlText w:val="{sym}"/>') if bullet else ('<w:numFmt w:val="decimal"/>' f'<w:lvlText w:val="%{lvl + 1}."/>')
            out.append(f'<w:lvl w:ilvl="{lvl}"><w:start w:val="1"/>{fmt}<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{left}" w:hanging="360"/></w:pPr></w:lvl>')
        return "".join(out)
    nums = '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    for nid in ordered_ids:
        nums += (f'<w:num w:numId="{nid}"><w:abstractNumId w:val="1"/>'
                 + "".join(f'<w:lvlOverride w:ilvl="{l}"><w:startOverride w:val="1"/></w:lvlOverride>' for l in range(9)) + "</w:num>")
    return (XML_HEAD + f'<w:numbering {W_NS}><w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>{levels(True)}</w:abstractNum>'
            f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>{levels(False)}</w:abstractNum>{nums}</w:numbering>')


class _Word:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.rels: List[Tuple[str, str, str, bool]] = []      # (id, type, target, external)
        self.media: List[Tuple[str, bytes]] = []
        self.ordered: List[int] = []
        self.links: Dict[str, str] = {}
        self.next_doc_id = 1

    def rel(self, kind: str, target: str, external: bool = False) -> str:
        rid = f"rId{len(self.rels) + 10}"
        self.rels.append((rid, kind, target, external))
        return rid

    # ---- inlines
    def text(self, text: str, props: str) -> str:
        out = []
        for part in re.split(r"(\t|\n)", clean(text)):
            if part == "\t":
                out.append(f"<w:r>{props}<w:tab/></w:r>")
            elif part == "\n":
                out.append(f"<w:r>{props}<w:br/></w:r>")
            elif part:
                out.append(f'<w:r>{props}<w:t xml:space="preserve">{esc(part)}</w:t></w:r>')
        return "".join(out)

    def image(self, img: Image) -> str:
        ext = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif"}[img.mime]
        name = f"image{len(self.media) + 1}.{ext}"
        self.media.append((f"word/media/{name}", img.data))
        rid = self.rel(f"{REL}/image", f"media/{name}")
        w, h = _fit(img)
        cx, cy = int(w * EMU), int(h * EMU)
        n = self.next_doc_id
        self.next_doc_id += 1
        return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
                f'<wp:docPr id="{n}" name="Picture {n}" descr="{esc(img.alt)}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="{esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
                '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')

    def runs(self, runs: List[Run]) -> str:
        out = []
        for r in runs:
            if r.br:
                out.append("<w:r><w:br/></w:r>")
                continue
            if r.image:
                out.append(self.image(r.image))
                continue
            props = ""
            if r.code or r.href:
                props += f'<w:rStyle w:val="{"Hyperlink" if r.href else "InlineCode"}"/>'
            if r.bold:
                props += "<w:b/>"
            if r.italic:
                props += "<w:i/>"
            body = self.text(r.text, f"<w:rPr>{props}</w:rPr>" if props else "")
            if r.href:
                rid = self.links.get(r.href) or self.links.setdefault(r.href, self.rel(f"{REL}/hyperlink", r.href, external=True))
                body = f'<w:hyperlink r:id="{rid}" w:history="1">{body}</w:hyperlink>'
            out.append(body)
        return "".join(out)

    # ---- blocks
    def para(self, style: str, runs: str, num: str = "", ind: int = 0, extra: str = "") -> str:
        ppr = f'<w:pStyle w:val="{style}"/>' + extra + num + (f'<w:ind w:left="{ind}"/>' if ind else "")
        return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>"

    def blocks(self, blocks: List[Any], ind: int = 0, quote: bool = False) -> str:
        return "".join(self.block(b, ind, quote) for b in blocks)

    def block(self, b: Any, ind: int, quote: bool) -> str:
        if isinstance(b, Heading):
            return self.para(f"Heading{min(max(b.level, 1), 6)}", self.runs(b.runs))
        if isinstance(b, Paragraph):
            return self.para("Quote" if quote else "Normal", self.runs(b.runs), ind=ind)
        if isinstance(b, Label):
            return self.para("Label", self.runs(b.runs))
        if isinstance(b, Meta):
            return self.para("Meta", self.runs(b.runs))
        if isinstance(b, Code):
            return self.code(b.text, ind)
        if isinstance(b, Diagram):
            if b.png:
                cap = self.para("Meta", self.text(self.doc.labels["diagram"], ""))
                return self.para("Normal", self.image(Image(b.png.data, b.png.mime, b.png.width, b.png.height, self.doc.labels["diagram"])), ind=ind) + cap
            return self.para("Label", self.text(self.doc.labels["diagram_source"], "")) + self.code(b.source, ind)
        if isinstance(b, Rule):
            return '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="B8C0C8"/></w:pBdr></w:pPr></w:p>'
        if isinstance(b, Quote):
            return self.blocks(b.blocks, ind + 360, True)
        if isinstance(b, Lst):
            return self.lst(b, 0, ind)
        if isinstance(b, Table):
            return self.table(b)
        return ""

    def code(self, text: str, ind: int) -> str:
        lines = clean(text).split("\n") or [""]
        return "".join(self.para("Code", self.text(line, ""), ind=ind) if line else self.para("Code", "", ind=ind) for line in lines)

    def lst(self, lst: Lst, level: int, ind: int) -> str:
        level = min(level, 8)
        if lst.ordered:
            num_id = 2 + len(self.ordered)
            self.ordered.append(num_id)
        else:
            num_id = 1
        numpr = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
        out = []
        for item in lst.items:
            first = True
            for blk in item:
                if first and isinstance(blk, Paragraph):
                    out.append(self.para("ListParagraph", self.runs(blk.runs), num=numpr))
                    first = False
                    continue
                if first:
                    out.append(self.para("ListParagraph", "", num=numpr))
                    first = False
                if isinstance(blk, Lst):
                    out.append(self.lst(blk, level + 1, ind))
                else:
                    out.append(self.block(blk, ind + 720 * (level + 1), False))
            if first:
                out.append(self.para("ListParagraph", "", num=numpr))
        return "".join(out)

    def table(self, t: Table) -> str:
        cols = max(1, len(t.header))
        width = TEXT_TWIPS // cols

        def cell(runs: List[Run], head: bool) -> str:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEF1F4"/>' if head else ""
            if head:
                runs = [Run(r.text, True, r.italic, r.code, r.href, r.br, r.image) for r in runs]
            return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>{self.para("TableText", self.runs(runs))}</w:tc>'

        grid = f'<w:gridCol w:w="{width}"/>' * cols
        rows = ('<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>' + "".join(cell(c, True) for c in t.header) + "</w:tr>")
        for row in t.rows:
            rows += "<w:tr><w:trPr><w:cantSplit/></w:trPr>" + "".join(cell(c, False) for c in (row + [[] for _ in t.header])[:cols]) + "</w:tr>"
        return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{width * cols}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
                '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>'
                f'<w:tblGrid>{grid}</w:tblGrid>{rows}</w:tbl>' + self.para("Normal", ""))


def write_docx(doc: Doc) -> bytes:
    w = _Word(doc)
    body = ("" if doc.bare else w.para("Title", w.text(doc.title, ""))) + w.blocks(doc.blocks)
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
            'w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>')
    document = XML_HEAD + f"<w:document {W_NS}><w:body>{body}{sect}</w:body></w:document>"
    rels = [("rId1", f"{REL}/styles", "styles.xml", False), ("rId2", f"{REL}/numbering", "numbering.xml", False), ("rId3", f"{REL}/settings", "settings.xml", False)] + w.rels
    rel_xml = XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
        f'<Relationship Id="{i}" Type="{t}" Target="{esc(target)}"' + (' TargetMode="External"' if ext else "") + "/>" for i, t, target, ext in rels) + "</Relationships>"
    exts = {n.rsplit(".", 1)[1] for n, _ in w.media}
    types = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif"}
    content_types = (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                     + "".join(f'<Default Extension="{e}" ContentType="{types[e]}"/>' for e in sorted(exts))
                     + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                       '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                       '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
                       '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
                       '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                       '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
    root_rels = (XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
                 '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                 f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/></Relationships>')
    now = _now()
    core = (XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{esc(doc.title)}</dc:title><dc:creator>Basalt</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
    app = XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Basalt</Application></Properties>'
    settings = (XML_HEAD + f'<w:settings {W_NS}><w:defaultTabStop w:val="720"/><w:compat>'
                '<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>')
    parts: List[Tuple[str, bytes]] = [
        ("[Content_Types].xml", content_types.encode()), ("_rels/.rels", root_rels.encode()), ("word/document.xml", document.encode()),
        ("word/_rels/document.xml.rels", rel_xml.encode()), ("word/styles.xml", _docx_styles(doc.lang).encode()),
        ("word/numbering.xml", _docx_numbering(w.ordered).encode()), ("word/settings.xml", settings.encode()),
        ("docProps/core.xml", core.encode()), ("docProps/app.xml", app.encode()),
    ] + w.media
    return _zip(parts)


# ======================================================================== OpenDocument

ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
          'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
          'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"')
BULLETS = ("•", "◦", "▪")


def _cm(px: float) -> str:
    return f"{px * 0.026458:.3f}cm"


def _odf_text(text: str, at_start: bool = True) -> str:
    """Text for a paragraph: runs of spaces, tabs and line ends become the elements ODF uses (it collapses plain white space)."""
    out = []
    first = at_start
    for part in re.split(r"( +|\t|\n)", clean(text)):
        if not part:
            continue
        if part == "\t":
            out.append("<text:tab/>")
        elif part == "\n":
            out.append("<text:line-break/>")
        elif part[0] == " ":
            out.append(f'<text:s text:c="{len(part)}"/>' if first or len(part) > 1 else " ")
        else:
            out.append(esc(part))
        first = part == "\n"
    return "".join(out)


class _Odf:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.pictures: List[Tuple[str, bytes, str]] = []
        self.span_styles: Dict[Tuple[bool, bool, bool], str] = {}
        self.para_styles: Dict[Tuple[str, int], str] = {}
        self.tables = 0
        self.frames = 0

    def span_style(self, bold: bool, italic: bool, code: bool) -> str:
        key = (bold, italic, code)
        if key not in self.span_styles:
            self.span_styles[key] = f"T{len(self.span_styles) + 1}"
        return self.span_styles[key]

    def para_style(self, parent: str, indent_cm: int) -> str:
        if not indent_cm:
            return parent
        key = (parent, indent_cm)
        if key not in self.para_styles:
            self.para_styles[key] = f"P{len(self.para_styles) + 1}"
        return self.para_styles[key]

    def image(self, img: Image) -> str:
        self.frames += 1
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}[img.mime]
        name = f"image{len(self.pictures) + 1}.{ext}"
        self.pictures.append((f"Pictures/{name}", img.data, img.mime))
        w, h = _fit(img)
        return (f'<draw:frame draw:style-name="fr1" draw:name="Image{self.frames}" text:anchor-type="as-char" svg:width="{_cm(w)}" svg:height="{_cm(h)}" draw:z-index="0">'
                f'<draw:image xlink:href="Pictures/{name}" xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/><svg:desc>{esc(img.alt)}</svg:desc></draw:frame>')

    def runs(self, runs: List[Run]) -> str:
        out = []
        for r in runs:
            if r.br:
                out.append("<text:line-break/>")
                continue
            if r.image:
                out.append(self.image(r.image))
                continue
            body = _odf_text(r.text, at_start=False)
            if r.bold or r.italic or r.code:
                body = f'<text:span text:style-name="{self.span_style(r.bold, r.italic, r.code)}">{body}</text:span>'
            if r.href:
                body = (f'<text:a xlink:type="simple" xlink:href="{esc(r.href)}" text:style-name="Internet_20_link" '
                        f'text:visited-style-name="Visited_20_Internet_20_Link">{body}</text:a>')
            out.append(body)
        return "".join(out)

    def p(self, style: str, inner: str) -> str:
        return f'<text:p text:style-name="{style}">{inner}</text:p>'

    def blocks(self, blocks: List[Any], indent: int = 0, quote: bool = False) -> str:
        return "".join(self.block(b, indent, quote) for b in blocks)

    def block(self, b: Any, indent: int, quote: bool) -> str:
        if isinstance(b, Heading):
            n = min(max(b.level, 1), 6)
            return f'<text:h text:style-name="Heading_20_{n}" text:outline-level="{n}">{self.runs(b.runs)}</text:h>'
        if isinstance(b, Paragraph):
            return self.p(self.para_style("Quotations" if quote else "Text_20_body", indent), self.runs(b.runs))
        if isinstance(b, Label):
            return self.p("Label", self.runs(b.runs))
        if isinstance(b, Meta):
            return self.p("Meta", self.runs(b.runs))
        if isinstance(b, Code):
            return self.code(b.text, indent)
        if isinstance(b, Diagram):
            if b.png:
                return (self.p(self.para_style("Text_20_body", indent), self.image(Image(b.png.data, b.png.mime, b.png.width, b.png.height, self.doc.labels["diagram"])))
                        + self.p("Meta", esc(self.doc.labels["diagram"])))
            return self.p("Label", esc(self.doc.labels["diagram_source"])) + self.code(b.source, indent)
        if isinstance(b, Rule):
            return '<text:p text:style-name="Rule"/>'
        if isinstance(b, Quote):
            return self.blocks(b.blocks, indent + 1, True)
        if isinstance(b, Lst):
            return self.lst(b, True)
        if isinstance(b, Table):
            return self.table(b)
        return ""

    def code(self, text: str, indent: int) -> str:
        return self.p(self.para_style("Preformatted_20_Text", indent), _odf_text(text))

    def lst(self, lst: Lst, top: bool) -> str:
        style = ' text:style-name="' + ("LNum" if lst.ordered else "LBul") + '"' if top else ""
        out = []
        for item in lst.items:
            inner = ""
            for blk in item:
                if isinstance(blk, Lst):
                    inner += self.lst(blk, blk.ordered != lst.ordered)
                elif isinstance(blk, Paragraph):
                    inner += self.p("List_20_Paragraph", self.runs(blk.runs))
                else:
                    inner += self.block(blk, 0, False)
            out.append(f"<text:list-item>{inner or self.p('List_20_Paragraph', '')}</text:list-item>")
        return f"<text:list{style}>" + "".join(out) + "</text:list>"

    def table(self, t: Table) -> str:
        self.tables += 1
        cols = max(1, len(t.header))

        def cell(runs: List[Run], style: str) -> str:
            return f'<table:table-cell table:style-name="{style}" office:value-type="string">{self.p("Table_20_Contents", self.runs(runs))}</table:table-cell>'

        head = "<table:table-header-rows><table:table-row>" + "".join(cell(c, "CellH") for c in t.header) + "</table:table-row></table:table-header-rows>"
        rows = "".join("<table:table-row>" + "".join(cell(c, "Cell") for c in (row + [[] for _ in t.header])[:cols]) + "</table:table-row>" for row in t.rows)
        return (f'<table:table table:name="Table{self.tables}" table:style-name="Tbl"><table:table-column table:style-name="Col" table:number-columns-repeated="{cols}"/>'
                f"{head}{rows}</table:table>")


def _odf_styles(lang: str) -> str:
    country = "IT" if lang == "it" else "US"

    def pstyle(name: str, display: str = "", parent: str = "Standard", ppr: str = "", tpr: str = "", extra: str = "") -> str:
        disp = f' style:display-name="{display}"' if display else ""
        par = f' style:parent-style-name="{parent}"' if parent else ""
        return (f'<style:style style:name="{name}"{disp} style:family="paragraph"{par}{extra}>'
                + (f"<style:paragraph-properties {ppr}/>" if ppr else "") + (f"<style:text-properties {tpr}/>" if tpr else "") + "</style:style>")

    heading = "".join(pstyle(f"Heading_20_{n}", f"Heading {n}", "Standard", f'fo:margin-top="{m}cm" fo:margin-bottom="0.15cm" fo:keep-with-next="always"',
                             f'fo:font-size="{s}pt" fo:font-weight="bold" fo:color="#1f2933"', f' style:next-style-name="Text_20_body" style:default-outline-level="{n}"')
                      for n, s, m in ((1, 18, .6), (2, 15, .55), (3, 13, .4), (4, 12, .35), (5, 11, .3), (6, 11, .3)))
    border = 'fo:border="0.5pt solid #d8dee4" fo:padding="0.12cm" fo:background-color="#f4f6f8"'
    return (XML_HEAD + f'<office:document-styles {ODF_NS} office:version="1.2"><office:font-face-decls>'
            '<style:font-face style:name="Calibri" svg:font-family="Calibri"/><style:font-face style:name="Consolas" svg:font-family="Consolas" style:font-family-generic="modern" style:font-pitch="fixed"/>'
            "</office:font-face-decls><office:styles>"
            f'<style:default-style style:family="paragraph"><style:paragraph-properties fo:orphans="2" fo:widows="2"/><style:text-properties style:font-name="Calibri" fo:font-size="11pt" fo:language="{lang or "en"}" fo:country="{country}"/></style:default-style>'
            + pstyle("Standard", "Standard", "")
            + pstyle("Title", "Title", "Standard", 'fo:margin-bottom="0.3cm"', 'fo:font-size="24pt" fo:font-weight="bold"', ' style:next-style-name="Text_20_body"')
            + heading
            + pstyle("Text_20_body", "Text body", "Standard", 'fo:margin-top="0cm" fo:margin-bottom="0.21cm" fo:line-height="120%"')
            + pstyle("List_20_Paragraph", "List Paragraph", "Standard", 'fo:margin-top="0cm" fo:margin-bottom="0.1cm"')
            + pstyle("Meta", "Basalt Meta", "Standard", 'fo:margin-top="0.1cm" fo:margin-bottom="0.1cm"', 'fo:font-size="10pt" fo:font-style="italic" fo:color="#57606a"')
            + pstyle("Label", "Basalt Label", "Standard", 'fo:margin-top="0.4cm" fo:margin-bottom="0.1cm" fo:keep-with-next="always"', 'fo:font-weight="bold"')
            + pstyle("Quotations", "Quotations", "Standard", 'fo:margin-left="0.8cm" fo:margin-top="0cm" fo:margin-bottom="0.21cm" fo:border-left="2pt solid #b8c0c8" fo:padding-left="0.3cm"',
                     'fo:color="#3a424a"')
            + pstyle("Preformatted_20_Text", "Preformatted Text", "Standard", f'fo:margin-top="0.1cm" fo:margin-bottom="0.2cm" {border}',
                     f'style:font-name="Consolas" fo:font-size="9pt" fo:hyphenate="false"', ' style:class="html"')
            + pstyle("Table_20_Contents", "Table Contents", "Standard", 'fo:margin-top="0.05cm" fo:margin-bottom="0.05cm"')
            + pstyle("Rule", "Basalt Rule", "Standard", 'fo:margin-top="0.2cm" fo:margin-bottom="0.2cm" fo:border-bottom="0.5pt solid #b8c0c8"')
            + '<style:style style:name="Source_20_Text" style:display-name="Source Text" style:family="text"><style:text-properties style:font-name="Consolas" fo:background-color="#eef1f4"/></style:style>'
            + '<style:style style:name="Internet_20_link" style:display-name="Internet link" style:family="text"><style:text-properties fo:color="#0563c1" style:text-underline-style="solid" style:text-underline-width="auto" style:text-underline-color="font-color"/></style:style>'
            + '<style:style style:name="Visited_20_Internet_20_Link" style:display-name="Visited Internet Link" style:family="text"><style:text-properties fo:color="#551a8b" style:text-underline-style="solid" style:text-underline-width="auto" style:text-underline-color="font-color"/></style:style>'
            + "</office:styles><office:automatic-styles>"
              '<style:page-layout style:name="Mpm1"><style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" style:print-orientation="portrait" '
              'fo:margin-top="2cm" fo:margin-bottom="2cm" fo:margin-left="2cm" fo:margin-right="2cm"/></style:page-layout>'
              '</office:automatic-styles><office:master-styles><style:master-page style:name="Standard" style:page-layout-name="Mpm1"/></office:master-styles></office:document-styles>')


def write_odt(doc: Doc) -> bytes:
    o = _Odf(doc)
    body = ("" if doc.bare else f'<text:p text:style-name="Title">{esc(doc.title)}</text:p>') + o.blocks(doc.blocks)
    levels_b = "".join(
        f'<text:list-level-style-bullet text:level="{n}" text:bullet-char="{BULLETS[(n - 1) % 3]}"><style:list-level-properties text:list-level-position-and-space-mode="label-alignment">'
        f'<style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="{0.9 * n:.2f}cm" fo:text-indent="-0.6cm" fo:margin-left="{0.9 * n:.2f}cm"/></style:list-level-properties></text:list-level-style-bullet>'
        for n in range(1, 11))
    levels_n = "".join(
        f'<text:list-level-style-number text:level="{n}" style:num-suffix="." style:num-format="1"><style:list-level-properties text:list-level-position-and-space-mode="label-alignment">'
        f'<style:list-level-label-alignment text:label-followed-by="listtab" text:list-tab-stop-position="{0.9 * n:.2f}cm" fo:text-indent="-0.6cm" fo:margin-left="{0.9 * n:.2f}cm"/></style:list-level-properties></text:list-level-style-number>'
        for n in range(1, 11))
    auto = ('<style:style style:name="fr1" style:family="graphic"><style:graphic-properties style:vertical-pos="top" style:vertical-rel="baseline" '
            'fo:margin-left="0cm" fo:margin-right="0cm" fo:margin-top="0cm" fo:margin-bottom="0cm" style:wrap="none"/></style:style>'
            '<style:style style:name="Tbl" style:family="table"><style:table-properties style:width="17cm" table:align="margins"/></style:style>'
            '<style:style style:name="Col" style:family="table-column"/>'
            '<style:style style:name="Cell" style:family="table-cell"><style:table-cell-properties fo:padding="0.1cm" fo:border="0.5pt solid #c4ccd4"/></style:style>'
            '<style:style style:name="CellH" style:family="table-cell"><style:table-cell-properties fo:padding="0.1cm" fo:border="0.5pt solid #c4ccd4" fo:background-color="#eef1f4"/></style:style>'
            f'<text:list-style style:name="LBul">{levels_b}</text:list-style><text:list-style style:name="LNum">{levels_n}</text:list-style>')
    for (bold, italic, code), name in o.span_styles.items():
        props = ('fo:font-weight="bold" ' if bold else "") + ('fo:font-style="italic" ' if italic else "") + ('style:font-name="Consolas" fo:background-color="#eef1f4" ' if code else "")
        auto += f'<style:style style:name="{name}" style:family="text"><style:text-properties {props.strip()}/></style:style>'
    for (parent, indent), name in o.para_styles.items():
        auto += f'<style:style style:name="{name}" style:family="paragraph" style:parent-style-name="{parent}"><style:paragraph-properties fo:margin-left="{0.8 * indent:.2f}cm"/></style:style>'
    content = (XML_HEAD + f'<office:document-content {ODF_NS} office:version="1.2"><office:font-face-decls>'
               '<style:font-face style:name="Calibri" svg:font-family="Calibri"/><style:font-face style:name="Consolas" svg:font-family="Consolas" style:font-family-generic="modern" style:font-pitch="fixed"/>'
               f"</office:font-face-decls><office:automatic-styles>{auto}</office:automatic-styles><office:body><office:text>{body}</office:text></office:body></office:document-content>")
    meta = (XML_HEAD + f'<office:document-meta {ODF_NS} office:version="1.2"><office:meta><dc:title>{esc(doc.title)}</dc:title><meta:generator>Basalt</meta:generator>'
            f"<meta:creation-date>{_now()}</meta:creation-date></office:meta></office:document-meta>")
    manifest = (XML_HEAD + '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
                '<manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/><manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
                '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
                + "".join(f'<manifest:file-entry manifest:full-path="{esc(n)}" manifest:media-type="{m}"/>' for n, _, m in o.pictures) + "</manifest:manifest>")
    parts: List[Tuple[str, bytes]] = [
        ("mimetype", b"application/vnd.oasis.opendocument.text"), ("content.xml", content.encode()), ("styles.xml", _odf_styles(doc.lang).encode()),
        ("meta.xml", meta.encode()), ("META-INF/manifest.xml", manifest.encode()),
    ] + [(n, d) for n, d, _ in o.pictures]
    return _zip(parts, stored_first="mimetype")
