"""Run-level machinery for the Word tools: the text of a paragraph as the runs hold it, replacing text across runs, and the markup of
tracked changes (w:ins, w:del, w:pPrChange). Nothing here opens or saves a file."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from docx.oxml import OxmlElement
from docx.oxml.ns import qn

AUTHOR = "TaDaMoo"
SPACE = "{http://www.w3.org/XML/1998/namespace}space"
W_P, W_R, W_T, W_RPR, W_PPR = qn("w:p"), qn("w:r"), qn("w:t"), qn("w:rPr"), qn("w:pPr")
W_DEL_T, W_TAB, W_BR, W_CR, W_NBH = qn("w:delText"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:noBreakHyphen")
W_INS, W_DEL, W_HYPERLINK = qn("w:ins"), qn("w:del"), qn("w:hyperlink")
CONTENT = (W_T, W_TAB, W_BR, W_CR, W_NBH, W_DEL_T)             # the children of a run that carry text
# elements whose runs are visible text of the paragraph; w:del holds removed text and is not entered
CONTAINERS = frozenset(qn(t) for t in ("w:hyperlink", "w:ins", "w:smartTag", "w:fldSimple", "w:sdt", "w:sdtContent", "w:customXml", "w:moveTo"))
ATOMS = {W_TAB: "\t", W_BR: "\n", W_CR: "\n", W_NBH: "-"}


class EditError(ValueError):
    """An edit that cannot be applied; the message says what to change."""


@dataclass
class Unit:
    el: Any            # a w:t, or an atomic w:tab / w:br / w:cr / w:noBreakHyphen
    run: Any
    start: int
    text: str

    @property
    def end(self) -> int:
        return self.start + len(self.text)

    @property
    def atomic(self) -> bool:
        return self.el.tag != W_T


def walk_runs(p: Any) -> List[Any]:
    """The w:r elements whose text is visible in a paragraph, in order."""
    out: List[Any] = []

    def visit(node: Any) -> None:
        for child in node:
            if child.tag == W_R:
                out.append(child)
            elif child.tag in CONTAINERS:
                visit(child)
    visit(p)
    return out


def units_of(p: Any) -> List[Unit]:
    units: List[Unit] = []
    pos = 0
    for run in walk_runs(p):
        for child in run:
            if child.tag == W_T:
                text = child.text or ""
            elif child.tag in ATOMS:
                text = ATOMS[child.tag]
            else:
                continue
            units.append(Unit(child, run, pos, text))
            pos += len(text)
    return units


def text_of(p: Any) -> str:
    return "".join(u.text for u in units_of(p))


def run_format(run: Any) -> Tuple[bool, bool]:
    """(bold, italic) of a run, from its own properties; a style's bold is not looked at."""
    rpr = run.find(W_RPR)
    if rpr is None:
        return False, False

    def on(tag: str) -> bool:
        el = rpr.find(qn(tag))
        return el is not None and el.get(qn("w:val"), "true").lower() not in ("0", "false", "off")
    return on("w:b"), on("w:i")


def format_ranges(p: Any) -> Dict[str, List[str]]:
    """The bold and italic stretches of a paragraph's text ({"bold": ["Intro"], "italic": [...]}), joined across neighbouring runs."""
    segments: List[Tuple[str, bool, bool]] = []
    for u in units_of(p):
        bold, italic = run_format(u.run)
        if segments and segments[-1][1:] == (bold, italic):
            segments[-1] = (segments[-1][0] + u.text, bold, italic)
        else:
            segments.append((u.text, bold, italic))
    found = {"bold": [t.strip() for t, b, _ in segments if b and t.strip()], "italic": [t.strip() for t, _, i in segments if i and t.strip()]}
    return {k: v for k, v in found.items() if v}


# -- building text into runs ---------------------------------------------------------------------------------------------


def make_t(text: str, tag: str = W_T) -> Any:
    t = qn_element(tag)
    t.text = text
    t.set(SPACE, "preserve")
    return t


def qn_element(tag: str) -> Any:
    """A new element; `tag` is "w:name" or "{namespace}name"."""
    return OxmlElement("w:" + tag.rsplit("}", 1)[-1].removeprefix("w:"))


def set_t_text(t: Any, text: str, tag: str = W_T) -> Any:
    """Puts `text` in a w:t; a new line or a tab in it becomes a w:br or w:tab after it. Returns the last element written."""
    pieces = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = []
    buf = ""
    for ch in pieces:
        if ch in "\n\t":
            parts.append(buf)
            parts.append(ch)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    t.text = parts[0]
    t.set(SPACE, "preserve")
    cur = t
    for i in range(1, len(parts), 2):
        sep = qn_element("w:br" if parts[i] == "\n" else "w:tab")
        cur.addnext(sep)
        cur = sep
        if parts[i + 1]:
            nt = make_t(parts[i + 1], tag)
            cur.addnext(nt)
            cur = nt
    return cur


def new_run(text: str, rpr: Any = None, tag: str = W_T) -> Any:
    run = qn_element("w:r")
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t = make_t("", tag)
    run.append(t)
    set_t_text(t, text, tag)
    return run


def prune_run(run: Any) -> None:
    """Takes out a run that holds nothing any more."""
    if run.getparent() is not None and not [c for c in run if c.tag != W_RPR]:
        run.getparent().remove(run)


def first_rpr(p: Any) -> Any:
    for run in walk_runs(p):
        if run.find(W_RPR) is not None:
            return run.find(W_RPR)
    return None


# -- tracked changes -------------------------------------------------------------------------------------------------


class Revisions:
    """Author, date and a running id for the tracked changes of one call."""

    def __init__(self, body: Any, author: str = AUTHOR) -> None:
        ids = [int(v) for v in body.xpath("//@w:id") if str(v).lstrip("-").isdigit()]
        self.next = max(ids, default=0) + 1
        self.author = author
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def mark(self, tag: str) -> Any:
        el = qn_element(tag)
        el.set(qn("w:id"), str(self.next))
        el.set(qn("w:author"), self.author)
        el.set(qn("w:date"), self.date)
        self.next += 1
        return el


def _to_deleted_text(run: Any) -> None:
    for t in run.findall(W_T):
        t.tag = W_DEL_T
        t.set(SPACE, "preserve")


def wrap_deleted(runs: List[Any], rev: Revisions) -> None:
    """Each run becomes tracked deleted text (consecutive runs of one parent share one w:del)."""
    last = None
    for run in runs:
        parent = run.getparent()
        if parent is not None and parent.tag == W_DEL:
            continue
        _to_deleted_text(run)
        if last is not None and last.getparent() is parent and last.getnext() is run:
            last.append(run)
        else:
            last = rev.mark("w:del")
            run.addprevious(last)
            last.append(run)


def track_inserted_run(text: str, rpr: Any, rev: Revisions) -> Any:
    wrapper = rev.mark("w:ins")
    wrapper.append(new_run(text, rpr))
    return wrapper


def mark_paragraph(p: Any, tag: str, rev: Revisions) -> None:
    """The paragraph mark itself is tracked as inserted (w:ins) or deleted (w:del)."""
    ppr = p.find(W_PPR)
    if ppr is None:
        ppr = qn_element("w:pPr")
        p.insert(0, ppr)
    rpr = ppr.find(W_RPR)
    if rpr is None:
        rpr = qn_element("w:rPr")
        later = next((c for c in ppr if c.tag in (qn("w:sectPr"), qn("w:pPrChange"))), None)
        if later is not None:
            later.addprevious(rpr)
        else:
            ppr.append(rpr)
    rpr.insert(0, rev.mark(tag))


def track_style_change(p: Any, rev: Revisions) -> None:
    """Records the paragraph properties as they are now as a w:pPrChange; call it before they are changed."""
    ppr = p.find(W_PPR)
    if ppr is None:
        ppr = qn_element("w:pPr")
        p.insert(0, ppr)
    for old in ppr.findall(qn("w:pPrChange")):
        ppr.remove(old)
    before = qn_element("w:pPr")
    for child in ppr:
        if child.tag not in (W_RPR, qn("w:sectPr")):
            before.append(copy.deepcopy(child))
    change = rev.mark("w:pPrChange")
    change.append(before)
    ppr.append(change)


def mark_row(tr: Any, tag: str, rev: Revisions) -> None:
    trpr = tr.find(qn("w:trPr"))
    if trpr is None:
        trpr = qn_element("w:trPr")
        first = next((c for c in tr if c.tag == qn("w:tc")), None)
        if first is not None:
            first.addprevious(trpr)
        else:
            tr.append(trpr)
    change = trpr.find(qn("w:trPrChange"))
    if change is not None:
        change.addprevious(rev.mark(tag))
    else:
        trpr.append(rev.mark(tag))


# -- cutting runs ------------------------------------------------------------------------------------------------------


def _shell(run: Any) -> Any:
    clone = qn_element("w:r")
    for k, v in run.attrib.items():
        clone.set(k, v)
    if run.find(W_RPR) is not None:
        clone.append(copy.deepcopy(run.find(W_RPR)))
    return clone


def cut_before(el: Any) -> None:
    """Splits the run so `el` (a child of it) is the first thing in a run of its own."""
    run = el.getparent()
    children = list(run)
    at = children.index(el)
    if all(c.tag == W_RPR for c in children[:at]):
        return
    tail = _shell(run)
    for child in children[at:]:
        tail.append(child)
    run.addnext(tail)


def split_t(unit: Unit, offset: int) -> Unit:
    """Splits a w:t at `offset` characters into two w:t; returns the second as a unit."""
    text = unit.text
    unit.el.text = text[:offset]
    unit.el.set(SPACE, "preserve")
    second = make_t(text[offset:])
    unit.el.addnext(second)
    return Unit(second, unit.run, unit.start + offset, text[offset:])


def isolate(p: Any, s: int, e: int) -> List[Any]:
    """Cuts the runs of paragraph `p` so the characters s to e (exclusive) are whole runs; returns those runs in order."""
    for pos in (s, e):
        for u in units_of(p):
            if u.start < pos < u.end and not u.atomic:
                cut_before(split_t(u, pos - u.start).el)
                break
            if u.start == pos and u.end > u.start:
                cut_before(u.el)
                break
    runs: List[Any] = []
    for u in units_of(p):
        if u.start >= s and u.end <= e and u.end > u.start and u.run not in runs:
            runs.append(u.run)
    return runs


# -- replacing text ------------------------------------------------------------------------------------------------------


def replace_plain(p: Any, s: int, e: int, repl: str) -> None:
    """Replaces the characters s to e (exclusive) of the paragraph; the text goes into the first run the match touches, so it keeps that run's
    formatting. The runs the match ran into lose the matched part."""
    hit = [u for u in units_of(p) if u.end > s and u.start < e]
    if not hit:
        raise EditError("the text to replace is not in the paragraph")
    first = hit[0]
    if first.atomic:
        empty = make_t("")
        first.el.addnext(empty)
        first.run.remove(first.el)
        first = Unit(empty, first.run, first.start, "")
        hit[0] = first
    prefix = first.text[:s - first.start]
    suffix = first.text[e - first.start:] if e < first.end else ""
    set_t_text(first.el, prefix + repl + suffix)
    for u in hit[1:]:
        if u.end <= e:
            u.run.remove(u.el)
            prune_run(u.run)
        else:
            u.el.text = u.text[e - u.start:]
            u.el.set(SPACE, "preserve")


def replace_tracked(p: Any, s: int, e: int, repl: str, rev: Revisions) -> None:
    runs = isolate(p, s, e)
    if not runs:
        raise EditError("the text to replace is not in the paragraph")
    rpr = runs[0].find(W_RPR)
    wrap_deleted(runs, rev)
    last_del = runs[-1].getparent()
    if repl:
        if last_del.getparent() is None or last_del.getparent().tag not in (W_P, W_HYPERLINK):
            raise EditError("the text is inside a tracked insertion made by someone else, so a tracked replacement cannot be written there; "
                            "use track_changes false for it")
        last_del.addnext(track_inserted_run(repl, rpr, rev))
