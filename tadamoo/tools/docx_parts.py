"""The parts of a Word file outside its body: headers, footers, footnotes, endnotes and text boxes, as paragraphs the run-level editor can change.

Headers, footers and text boxes are live python-docx / lxml elements. Footnotes and endnotes are parts python-docx keeps as bytes, so they
are parsed here and written back by `Parts.finish` when something in them changed."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from docx.oxml.ns import qn
from lxml import etree

from tadamoo.tools import docx_runs as dr

SCOPES = ("headers", "footers", "footnotes", "endnotes", "textboxes", "extras")
NOTE_PARTS = {"footnotes": "/word/footnotes.xml", "endnotes": "/word/endnotes.xml"}
W_FOOTNOTE, W_ENDNOTE, W_TXBX = qn("w:footnote"), qn("w:endnote"), qn("w:txbxContent")
SEPARATORS = ("separator", "continuationSeparator", "continuationNotice")


def _all_paragraphs(root: Any) -> List[Any]:
    """Every paragraph under `root`, those of a table cell included, but not those of a text box nested in them (text boxes are their own scope)."""
    return [p for p in root.iter(dr.W_P) if not _inside_textbox(p, root)]


def _inside_textbox(p: Any, root: Any) -> bool:
    node = p.getparent()
    while node is not None and node is not root:
        if node.tag == W_TXBX:
            return True
        node = node.getparent()
    return False


class Parts:
    """The extra parts of one document: what they hold now, and the writing back of the ones that are not live."""

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self._notes: Dict[str, Tuple[Any, Any]] = {}       # scope -> (part, parsed root)

    def _headers_footers(self, kind: str) -> List[Any]:
        seen, out = set(), []
        for section in self.doc.sections:
            for attr in (kind, f"first_page_{kind}", f"even_page_{kind}"):
                item = getattr(section, attr)
                el = item._element
                if id(el) in seen:
                    continue
                seen.add(id(el))
                out += _all_paragraphs(el)
        return out

    def _notes_root(self, scope: str) -> Any:
        if scope in self._notes:
            return self._notes[scope][1]
        for part in self.doc.part.package.iter_parts():
            if str(part.partname) == NOTE_PARTS[scope]:
                root = getattr(part, "_element", None)
                if root is None:
                    root = etree.fromstring(part.blob)
                self._notes[scope] = (part, root)
                return root
        self._notes[scope] = (None, None)
        return None

    def paragraphs(self, scope: str) -> List[Any]:
        if scope == "headers":
            return self._headers_footers("header")
        if scope == "footers":
            return self._headers_footers("footer")
        if scope in NOTE_PARTS:
            root = self._notes_root(scope)
            if root is None:
                return []
            tag = W_FOOTNOTE if scope == "footnotes" else W_ENDNOTE
            return [p for note in root.iter(tag) if note.get(qn("w:type")) not in SEPARATORS for p in _all_paragraphs(note)]
        if scope == "textboxes":
            seen, out = set(), []
            for box in self.doc.element.body.iter(W_TXBX):
                for p in box.iter(dr.W_P):
                    if id(p) not in seen:
                        seen.add(id(p))
                        out.append(p)
            return out
        if scope == "extras":
            out = []
            for name in ("headers", "footers", "footnotes", "endnotes", "textboxes"):
                out += self.paragraphs(name)
            return out
        raise KeyError(scope)

    def counts(self) -> Dict[str, int]:
        return {name: len(self.paragraphs(name)) for name in ("headers", "footers", "footnotes", "endnotes", "textboxes")}

    def finish(self) -> None:
        """Writes the notes parts that were parsed here back into the document (a live part needs nothing)."""
        for part, root in self._notes.values():
            if part is not None and getattr(part, "_element", None) is None:
                part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def describe(doc: Any) -> List[str]:
    """Lines for the outline: what the document keeps outside its body, with a short preview, so the model knows it is there."""
    parts = Parts(doc)
    out = []
    for name in ("headers", "footers", "footnotes", "endnotes", "textboxes"):
        pars = parts.paragraphs(name)
        texts = [" ".join(dr.text_of(p).split()) for p in pars]
        texts = [t for t in texts if t]
        if texts:
            shown = " | ".join(t if len(t) <= 60 else t[:59] + "…" for t in texts[:4])
            out.append(f"{name}: {len(texts)} paragraphs with text: {shown}" + (" …" if len(texts) > 4 else ""))
    return out
