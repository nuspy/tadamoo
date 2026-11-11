"""Word (.docx) tools: an outline and a reader to find what to change, one call that applies several edits atomically (optionally as tracked
changes), and one that creates a document from blocks. The model supplies text only; the file is changed by this code (python-docx and lxml).

Paragraphs are addressed pN, tables tN and cells tN.rR.cC, all counted from 1 over the body of the document. Every address of one
`docx_edit` call is resolved against the document as it was before the call, so the edits of one call do not move each other."""
from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from docx.oxml.ns import qn

from tadamoo.tools import docx_runs as dr
from tadamoo.tools.docx_runs import EditError
from tadamoo.tools.docx_schemas import (CREATE_PARAMS, CREATE_TEXT, EDIT_PARAMS, EDIT_TEXT, OUTLINE_PARAMS, OUTLINE_TEXT, READ_PARAMS,
                                        READ_TEXT)
from tadamoo.tools.registry import EffectClass, ToolRegistry

W_TBL, W_TR, W_TC, W_SECT = qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:sectPr")
READ_CHARS = 40_000
PREVIEW = 80
OUTLINE_LINES = 400
MAX_ERRORS = 20
HEADING = re.compile(r"^heading (\d)$", re.I)
PAR = re.compile(r"^p(\d+)(?:\s*-\s*p?(\d+))?$", re.I)
TBL = re.compile(r"^t(\d+)$", re.I)
CELL = re.compile(r"^t(\d+)\.r(\d+)\.c(\d+)$", re.I)
OBJECTS = ("w:drawing", "w:pict", "w:object")
UNTRACKED_RPR = ("w:b", "w:bCs", "w:i", "w:iCs")                   # not carried from a neighbouring paragraph into new text


def available() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


def comments_of(doc: Any) -> List[Any]:
    """The comments of a document; looking never adds an empty comments part to it."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    try:
        return list(doc.part.part_related_by(RT.COMMENTS).comments)
    except KeyError:
        return []


def _preview(text: str, n: int = PREVIEW) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"


# -- the document and its addresses --------------------------------------------------------------------------------------


class Body:
    """A document's body as numbered paragraphs and tables, as it was when this object was made."""

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self.body = doc.element.body
        self.paras = list(self.body.iterchildren(dr.W_P))
        self.tables = list(self.body.iterchildren(W_TBL))
        self.pnum = {p: i + 1 for i, p in enumerate(self.paras)}
        self._rows = {t: t.findall(W_TR) for t in self.tables}                # rows and cells as they were, so edits do not move addresses
        self._cells = {tr: tr.findall(W_TC) for rows in self._rows.values() for tr in rows}
        self.edges = [c for c in self.body if c.tag != W_SECT]
        self.styles = {s.style_id: s.name for s in doc.styles}
        self.default_style = next((s.name for s in doc.styles if s.type == 1 and s.element.get(qn("w:default")) in ("1", "true")), "Normal")

    def style_name(self, p: Any) -> str:
        ppr = p.find(dr.W_PPR)
        sid = ppr.find(qn("w:pStyle")).get(qn("w:val")) if ppr is not None and ppr.find(qn("w:pStyle")) is not None else None
        return self.styles.get(sid, sid) if sid else self.default_style

    def heading_level(self, p: Any) -> Optional[int]:
        """0 for a title, 1-9 for a heading, None for any other paragraph."""
        name = self.style_name(p)
        found = HEADING.match(name)
        if found:
            return int(found.group(1))
        if name.lower() == "title":
            return 0
        ppr = p.find(dr.W_PPR)
        lvl = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
        return int(lvl.get(qn("w:val"))) + 1 if lvl is not None and lvl.get(qn("w:val"), "").isdigit() and int(lvl.get(qn("w:val"))) < 9 else None

    def label(self, p: Any) -> str:
        return f"p{self.pnum[p]}" if p in self.pnum else "a new paragraph"

    def rows(self, tbl: Any) -> List[Any]:
        return self._rows[tbl] if tbl in self._rows else tbl.findall(W_TR)

    def cells(self, tr: Any) -> List[Any]:
        return self._cells[tr] if tr in self._cells else tr.findall(W_TC)

    @staticmethod
    def cell_paras(tc: Any) -> List[Any]:
        return tc.findall(dr.W_P)

    def cell_text(self, tc: Any) -> str:
        return "\n".join(dr.text_of(p) for p in self.cell_paras(tc))

    def grid(self, tbl: Any) -> Tuple[int, int]:
        rows = self.rows(tbl)
        return len(rows), max((len(self.cells(r)) for r in rows), default=0)

    # -- targets ----------------------------------------------------------------------------------------------------

    def parse(self, target: Any) -> Tuple[str, Tuple[int, ...]]:
        if not isinstance(target, str) or not target.strip():
            raise EditError("a target is needed: a paragraph (p12), a range (p10-p20), a table (t2) or a cell (t2.r3.c1)")
        t = target.strip().lower()
        if m := PAR.match(t):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            if a < 1 or b < a:
                raise EditError(f"'{target}' is not a valid paragraph range (from p1; the first number is the lower one)")
            if b > len(self.paras):
                raise EditError(f"'{target}' does not exist: the document has {len(self.paras)} paragraphs (p1-p{len(self.paras)})")
            return "par", (a, b)
        if m := TBL.match(t):
            n = int(m.group(1))
            if not 1 <= n <= len(self.tables):
                raise EditError(f"table '{target}' does not exist: the document has {len(self.tables)} tables")
            return "table", (n,)
        if m := CELL.match(t):
            n, r, c = (int(g) for g in m.groups())
            if not 1 <= n <= len(self.tables):
                raise EditError(f"table t{n} does not exist: the document has {len(self.tables)} tables")
            rows = self.rows(self.tables[n - 1])
            if not 1 <= r <= len(rows):
                raise EditError(f"'{target}' does not exist: t{n} has {len(rows)} rows (r1-r{len(rows)})")
            cols = len(self.cells(rows[r - 1]))
            if not 1 <= c <= cols:
                raise EditError(f"'{target}' does not exist: row {r} of t{n} has {cols} cells (c1-c{cols})")
            return "cell", (n, r, c)
        raise EditError(f"'{target}' is not an address: use pN, pN-pM, tN or tN.rR.cC (counted from 1)")

    def table(self, ref: Any) -> Tuple[int, Any]:
        kind, nums = self.parse(ref)
        if kind != "table":
            raise EditError(f"'{ref}' is not a table: use tN")
        return nums[0], self.tables[nums[0] - 1]

    def cell(self, ref: Any) -> Any:
        kind, nums = self.parse(ref)
        if kind != "cell":
            raise EditError(f"'{ref}' is not a cell: use tN.rR.cC")
        return self.cells(self.rows(self.tables[nums[0] - 1])[nums[1] - 1])[nums[2] - 1]

    def paragraphs_in(self, target: Any) -> List[Any]:
        """The paragraphs a target covers (for a table or a cell, those inside it); every paragraph and cell when `target` is "all"."""
        if isinstance(target, str) and target.strip().lower() in ("all", "document"):
            out: List[Any] = []
            for child in self.body:
                if child.tag == dr.W_P:
                    out.append(child)
                elif child.tag == W_TBL:
                    out += [p for tr in self.rows(child) for tc in self.cells(tr) for p in self.cell_paras(tc)]
            return out
        kind, nums = self.parse(target)
        if kind == "par":
            return self.paras[nums[0] - 1:nums[1]]
        if kind == "table":
            return [p for tr in self.rows(self.tables[nums[0] - 1]) for tc in self.cells(tr) for p in self.cell_paras(tc)]
        return self.cell_paras(self.cell(target))


# -- text of the document --------------------------------------------------------------------------------------------------


def _marked(p: Any) -> str:
    """The paragraph's text with tracked insertions as {+ +} and tracked deletions as [- -]; for the diff recorded after an edit."""
    def visit(node: Any) -> str:
        out = ""
        for child in node:
            if child.tag == dr.W_R:
                for el in child:
                    if el.tag in (dr.W_T, dr.W_DEL_T):
                        out += el.text or ""
                    elif el.tag in dr.ATOMS:
                        out += dr.ATOMS[el.tag]
            elif child.tag == dr.W_INS:
                out += "{+" + visit(child) + "+}"
            elif child.tag == dr.W_DEL:
                out += "[-" + visit(child) + "-]"
            elif child.tag in dr.CONTAINERS:
                out += visit(child)
        return out
    return visit(p)


def text_dump(doc: Any) -> str:
    """The document as lines of text (one per paragraph, one per table row, then the comments): what the Changes view diffs."""
    lines: List[str] = []
    for child in doc.element.body:
        if child.tag == dr.W_P:
            lines.append(_marked(child))
        elif child.tag == W_TBL:
            for tr in child.findall(W_TR):
                lines.append("| " + " | ".join(" / ".join(_marked(p) for p in tc.findall(dr.W_P)) for tc in tr.findall(W_TC)) + " |")
    lines += [f"[comment, {c.author}] {c.text}" for c in comments_of(doc)]
    return "\n".join(lines) + "\n"


def _markdown_table(body: Body, tbl: Any) -> str:
    rows = body.rows(tbl)
    out = []
    for i, tr in enumerate(rows):
        cells = [body.cell_text(tc).replace("|", "\\|").replace("\n", "<br>") for tc in body.cells(tr)]
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("|" + "---|" * max(1, len(cells)))
    return "\n".join(out)


def _fmt_note(p: Any) -> str:
    found = dr.format_ranges(p)
    return "  {" + "; ".join(f'{k}: ' + ", ".join(f'"{_preview(s, 40)}"' for s in v[:6]) for k, v in found.items()) + "}" if found else ""


def _tracked_counts(doc: Any) -> Tuple[int, int]:
    root = doc.element.body
    return len(root.xpath(".//w:ins")), len(root.xpath(".//w:del"))


# -- the tools -------------------------------------------------------------------------------------------------------------


class DocxTools:
    def __init__(self, sandbox: Any) -> None:
        self.sandbox = sandbox

    # -- files ---------------------------------------------------------------------------------------------------------

    def _existing(self, path: str) -> Path:
        safe = self.sandbox.resolve_safe(path)
        if safe.suffix.lower() != ".docx":
            raise EditError(f"'{path}' is not a .docx file. These tools open Word documents saved as .docx (not .doc, .docm or .odt).")
        if not safe.is_file():
            raise FileNotFoundError(f"File not found: {path}.{self.sandbox._where(path, safe)}")
        return safe

    @staticmethod
    def _open(safe: Path, path: str) -> Any:
        from docx import Document
        try:
            return Document(str(safe))
        except Exception as exc:
            raise EditError(f"'{path}' could not be opened as a Word document ({type(exc).__name__}: {exc}); it may be damaged, "
                            "password-protected or not really a .docx.") from None

    @staticmethod
    def _save(doc: Any, safe: Path) -> None:
        """Writes to a temporary file beside the target, checks it opens, then replaces the target; any failure leaves the target as it was."""
        from docx import Document
        safe.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=safe.parent, prefix=".tadamoo-", suffix=".tmp")
        os.close(fd)
        try:
            doc.save(tmp)
            Document(tmp)
            if safe.exists():
                os.chmod(tmp, safe.stat().st_mode & 0o777)
            os.replace(tmp, safe)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- docx_outline -----------------------------------------------------------------------------------------------------

    def outline(self, path: str, detail: bool = False) -> str:
        safe = self._existing(path)
        doc = self._open(safe, path)
        body = Body(doc)
        n_img = len(doc.element.body.xpath(".//w:drawing")) + len(doc.element.body.xpath(".//w:pict[not(ancestor::mc:Fallback)]"))
        n_comments = len(comments_of(doc))
        ins, dels = _tracked_counts(doc)
        tracked = f"{ins} insertions, {dels} deletions" if ins or dels else "none"
        head = [f"{path}: {len(body.paras)} paragraphs, {len(body.tables)} tables, {n_img} images, {n_comments} comments, tracked changes: {tracked}",
                "Addresses count from 1 over the body: paragraphs pN, tables tN, cells tN.rR.cC. Headers, footers, footnotes and text boxes are not listed."]
        sections: List[Dict[str, Any]] = [{"level": 0, "title": None, "first": None, "pars": [], "tables": []}]
        t_no = p_no = 0
        for child in body.body:
            if child.tag == dr.W_P:
                p_no += 1
                lvl = body.heading_level(child)
                if lvl is not None and dr.text_of(child).strip():
                    sections.append({"level": lvl, "title": _preview(dr.text_of(child)), "first": p_no, "pars": [], "tables": []})
                else:
                    sections[-1]["pars"].append(p_no)
                    if detail:
                        sections[-1].setdefault("lines", []).append(f"    p{p_no} [{body.style_name(child)}] {_preview(dr.text_of(child)) or '(empty)'}")
            elif child.tag == W_TBL:
                t_no += 1
                rows, cols = body.grid(child)
                first = " | ".join(_preview(body.cell_text(tc), 24) for tc in body.cells(body.rows(child)[0])) if rows else ""
                sections[-1]["tables"].append(f"t{t_no} {rows}x{cols}, after p{p_no}: {_preview(first)}")
        lines = list(head)
        for sec in sections:
            pars = sec["pars"]
            span = f" (p{pars[0]}-p{pars[-1]})" if len(pars) > 1 else f" (p{pars[0]})" if pars else ""
            if sec["title"] is None:
                if not pars and not sec["tables"]:
                    continue
                lines.append(f"(before the first heading): {len(pars)} paragraphs{span}")
            else:
                label = "Title" if sec["level"] == 0 else f"H{sec['level']}"
                lines.append(f"{'  ' * max(0, sec['level'] - 1)}{label} p{sec['first']} \"{sec['title']}\": {len(pars)} paragraphs{span}")
            lines += [f"{'  ' * max(0, sec['level'])}  table {t}" for t in sec["tables"]]
            lines += sec.get("lines", [])
        if len(lines) > OUTLINE_LINES:
            lines = lines[:OUTLINE_LINES] + [f"[outline cut at {OUTLINE_LINES} lines; docx_read with a range shows the rest]"]
        return "\n".join(lines)

    # -- docx_read ----------------------------------------------------------------------------------------------------------

    def read(self, path: str, target: str) -> str:
        safe = self._existing(path)
        body = Body(self._open(safe, path))
        kind, nums = body.parse(target)
        out: List[str] = []
        if kind == "par":
            for n in range(nums[0], nums[1] + 1):
                p = body.paras[n - 1]
                out.append(f"p{n} [{body.style_name(p)}] {dr.text_of(p)}{_fmt_note(p)}")
        elif kind == "table":
            tbl = body.tables[nums[0] - 1]
            rows, cols = body.grid(tbl)
            style = tbl.find(qn("w:tblPr")).find(qn("w:tblStyle")) if tbl.find(qn("w:tblPr")) is not None else None
            out.append(f"t{nums[0]}: {rows} rows x {cols} columns" + (f", style {style.get(qn('w:val'))}" if style is not None else "")
                       + "; cells are tN.rR.cC from 1")
            out.append(_markdown_table(body, tbl))
        else:
            tc = body.cell(target)
            paras = body.cell_paras(tc)
            out.append(f"{target.strip().lower()} [{body.style_name(paras[0]) if paras else ''}] {body.cell_text(tc)}" + (_fmt_note(paras[0]) if paras else ""))
        text = "\n".join(out)
        if len(text) > READ_CHARS:
            cut = text[:READ_CHARS].rsplit("\n", 1)[0]
            text = cut + f"\n[cut at {READ_CHARS} characters; read the next range from the paragraph after the last one shown]"
        return text

    # -- docx_edit ------------------------------------------------------------------------------------------------------------

    def edit(self, path: str, edits: List[Dict[str, Any]], track_changes: bool = False) -> Dict[str, Any]:
        safe = self._existing(path)
        blocked = self.sandbox._owner_block(path, safe, append=False)
        if blocked:
            return blocked
        if not isinstance(edits, list) or not edits:
            raise EditError("'edits' must be a non-empty list of edit objects, each with a 'kind'.")
        self.sandbox._guard_git("docx_edit", path, safe)
        doc = self._open(safe, path)
        old_dump = text_dump(doc)
        session = _Session(doc, track_changes)
        applied: List[str] = []
        problems: List[str] = []
        for i, item in enumerate(edits, 1):
            try:
                applied.append(session.apply(item))
            except EditError as exc:
                problems.append(f"edit {i} ({session.name(item)}): {exc}")
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                problems.append(f"edit {i} ({session.name(item)}): {type(exc).__name__}: {exc}")
        if problems:
            shown = problems[:MAX_ERRORS] + ([f"... and {len(problems) - MAX_ERRORS} more"] if len(problems) > MAX_ERRORS else [])
            raise EditError(f"Nothing was changed: {len(problems)} of {len(edits)} edits cannot be applied; the file is as it was.\n- " + "\n- ".join(shown)
                            + "\nAddresses refer to the document as it is now (before this call); read it again with docx_read if unsure.")
        session.finish()
        self._save(doc, safe)
        result = self.sandbox._record(path, "modified", old_dump, text_dump(doc), keep_text=False)
        result.update(edits=len(edits), applied=applied, tracked=bool(track_changes),
                      note="Paragraph and table numbers may have changed; run docx_outline or docx_read again before editing further.")
        return result

    # -- docx_create ----------------------------------------------------------------------------------------------------------

    def create(self, path: str, blocks: List[Dict[str, Any]], overwrite: bool = False) -> Dict[str, Any]:
        from docx import Document
        safe = self.sandbox.resolve_safe(path)
        if safe.suffix.lower() != ".docx":
            raise EditError(f"'{path}' must end in .docx.")
        if safe.is_dir():
            raise IsADirectoryError(f"'{path}' is a folder, not a file.")
        exists = safe.exists()
        if exists and not overwrite:
            raise EditError(f"'{path}' already exists. Send overwrite=true to replace it, or edit it with docx_edit.")
        if not isinstance(blocks, list) or not blocks:
            raise EditError("'blocks' must be a non-empty list of block objects, each with a 'kind'.")
        blocked = self.sandbox._owner_block(path, safe, append=False)
        if blocked:
            return blocked
        self.sandbox._guard_git("docx_create", path, safe)
        old_dump = ""
        if exists:
            try:
                old_dump = text_dump(Document(str(safe)))
            except Exception:
                old_dump = ""
        doc = Document()
        problems: List[str] = []
        for i, block in enumerate(blocks, 1):
            try:
                _add_block(doc, block)
            except (EditError, KeyError, ValueError, TypeError) as exc:
                problems.append(f"block {i}: {exc}")
        if problems:
            raise EditError("Nothing was written:\n- " + "\n- ".join(problems[:MAX_ERRORS]))
        self._save(doc, safe)
        result = self.sandbox._record(path, "modified" if exists else "added", old_dump, text_dump(doc), keep_text=False)
        result["blocks"] = len(blocks)
        return result


def _add_block(doc: Any, block: Any) -> None:
    if not isinstance(block, dict) or not isinstance(block.get("kind"), str):
        raise EditError("each block is an object with a 'kind': heading, paragraph, bullet, numbered, table or page_break")
    kind = block["kind"]
    allowed = {"heading": {"text", "level"}, "paragraph": {"text", "bold", "italic", "style"}, "bullet": {"text", "level"},
               "numbered": {"text"}, "table": {"rows", "header", "style"}, "page_break": set()}
    if kind not in allowed:
        raise EditError(f"unknown kind '{kind}': use heading, paragraph, bullet, numbered, table or page_break")
    extra = set(block) - allowed[kind] - {"kind"}
    if extra:
        raise EditError(f"a {kind} block has no field {', '.join(sorted(extra))}; its fields are {', '.join(sorted(allowed[kind])) or 'none'}")
    text = block.get("text", "")
    if kind != "table" and kind != "page_break" and not isinstance(text, str):
        raise EditError(f"a {kind} block needs 'text' as a string")
    try:
        if kind == "heading":
            level = int(block.get("level", 1))
            if not 0 <= level <= 9:
                raise EditError("heading level is 0 (title) to 9")
            doc.add_heading(text, level)
        elif kind == "paragraph":
            p = doc.add_paragraph(style=block.get("style"))
            run = p.add_run(text)
            run.bold = True if block.get("bold") else None
            run.italic = True if block.get("italic") else None
        elif kind == "bullet":
            level = int(block.get("level", 1))
            doc.add_paragraph(text, style="List Bullet" if level <= 1 else f"List Bullet {min(level, 3)}")
        elif kind == "numbered":
            doc.add_paragraph(text, style="List Number")
        elif kind == "page_break":
            doc.add_page_break()
        else:
            rows = block.get("rows")
            if not (isinstance(rows, list) and rows and all(isinstance(r, list) for r in rows)):
                raise EditError("a table block needs 'rows': a list of rows, each a list of cell texts")
            cols = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=cols)
            table.style = block.get("style") or "Table Grid"
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    cell = table.cell(r, c)
                    cell.text = "" if value is None else str(value)
                    if r == 0 and block.get("header", True):
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
    except KeyError as exc:
        raise EditError(f"style not found: {exc}") from None


class _Session:
    """The edits of one docx_edit call, applied to one document; every address was fixed when the session began."""

    ALLOWED = {
        "replace_text": {"target", "find", "replace", "count"},
        "set_paragraph": {"target", "text", "style"},
        "insert_paragraph": {"after", "before", "text", "style"},
        "delete_paragraph": {"target"},
        "set_cell": {"target", "text"},
        "insert_table_row": {"table", "after_row", "cells"},
        "delete_table_row": {"table", "row"},
        "add_comment": {"target", "text", "author", "initials", "find"},
        "set_style": {"target", "style"},
    }

    def __init__(self, doc: Any, track: bool) -> None:
        self.doc = doc
        self.body = Body(doc)
        self.track = bool(track)
        self.rev = dr.Revisions(doc.element)
        self.gone: set = set()
        self.removals: List[Any] = []                      # deleted elements stay in place until the call ends, so addresses next to them still work
        self.last_after: Dict[Any, Any] = {}
        self.handlers: Dict[str, Callable[[Dict[str, Any]], str]] = {k: getattr(self, "_do_" + k) for k in self.ALLOWED}

    @staticmethod
    def name(item: Any) -> str:
        if not isinstance(item, dict):
            return "not an object"
        where = item.get("target") or item.get("after") or item.get("before") or item.get("table") or ""
        return f"{item.get('kind', '?')} {where}".strip()

    def apply(self, item: Any) -> str:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            raise EditError("each edit is an object with a 'kind': " + ", ".join(self.ALLOWED))
        kind = item["kind"]
        if kind not in self.ALLOWED:
            raise EditError(f"unknown kind '{kind}': use " + ", ".join(self.ALLOWED))
        extra = set(item) - self.ALLOWED[kind] - {"kind"}
        if extra:
            raise EditError(f"{kind} has no field {', '.join(sorted(extra))}; its fields are {', '.join(sorted(self.ALLOWED[kind]))}")
        return self.handlers[kind](item)

    # -- helpers ---------------------------------------------------------------------------------------------------------

    def _live(self, p: Any) -> Any:
        if p in self.gone:
            raise EditError(f"{self.body.label(p)} was deleted by an earlier edit of this call")
        return p

    def _pars(self, target: Any, single: bool = False) -> List[Any]:
        kind, nums = self.body.parse(target)
        if kind != "par":
            raise EditError(f"'{target}' must be a paragraph or a range of paragraphs (pN, pN-pM)")
        if single and nums[0] != nums[1]:
            raise EditError(f"'{target}' is a range; this edit takes one paragraph")
        return [self._live(p) for p in self.body.paras[nums[0] - 1:nums[1]]]

    def _text(self, item: Dict[str, Any], key: str = "text") -> str:
        value = item.get(key)
        if not isinstance(value, str):
            raise EditError(f"'{key}' is needed and must be a string")
        return value.replace("\r\n", "\n").replace("\r", "\n")

    def _int(self, item: Dict[str, Any], key: str, low: int = 0) -> int:
        value = item.get(key)
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            raise EditError(f"'{key}' is needed and must be a whole number from {low}")
        return value

    def _set_style(self, p: Any, style: str) -> None:
        from docx.text.paragraph import Paragraph
        try:
            Paragraph(p, self.doc._body).style = style
        except KeyError:
            names = [s.name for s in self.doc.styles if s.type == 1]
            raise EditError(f"there is no paragraph style '{style}'. Styles in this document: {', '.join(names[:40])}") from None

    @staticmethod
    def _has_object(p: Any) -> bool:
        return any(p.xpath(f".//{tag}") for tag in OBJECTS)

    def _clear(self, p: Any) -> None:
        """Takes out the visible runs of a paragraph, keeping its properties, bookmarks, comment marks and tracked deletions."""
        for child in list(p):
            if child.tag in (dr.W_R, qn("w:hyperlink"), dr.W_INS, qn("w:smartTag"), qn("w:fldSimple"), qn("w:sdt"), qn("w:moveTo")):
                p.remove(child)

    def _write_text(self, p: Any, text: str) -> None:
        """The paragraph's text becomes `text`, in the formatting of its first run; as a tracked change when asked."""
        if self._has_object(p):
            raise EditError(f"{self.body.label(p)} holds an image or object that setting its text would delete; change the words with replace_text")
        rpr = dr.first_rpr(p)
        if self.track:
            dr.wrap_deleted(dr.walk_runs(p), self.rev)
            if text:
                p.append(dr.track_inserted_run(text, rpr, self.rev))
            return
        self._clear(p)
        if text:
            p.append(dr.new_run(text, rpr))

    def _delete_paragraph(self, p: Any) -> None:
        if p.find(dr.W_PPR) is not None and p.find(dr.W_PPR).find(W_SECT) is not None:
            raise EditError(f"{self.body.label(p)} carries a section break; deleting it would merge two sections")
        if self.track:
            dr.wrap_deleted(dr.walk_runs(p), self.rev)
            dr.mark_paragraph(p, "w:del", self.rev)
        else:
            self.removals.append(p)
        self.gone.add(p)

    def finish(self) -> None:
        for el in self.removals:
            if el.getparent() is not None:
                el.getparent().remove(el)

    # -- the edits -------------------------------------------------------------------------------------------------------

    def _do_replace_text(self, item: Dict[str, Any]) -> str:
        find, repl = item.get("find"), item.get("replace")
        if not isinstance(find, str) or not find:
            raise EditError("'find' is needed: the exact text to look for (not empty)")
        if not isinstance(repl, str):
            raise EditError("'replace' is needed (an empty string deletes the text)")
        repl = repl.replace("\r\n", "\n").replace("\r", "\n")
        count = item.get("count", 1)
        if isinstance(count, str) and count.strip().isdigit():
            count = int(count)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise EditError("'count' is how many occurrences to replace (default 1; 0 for all of them)")
        target = item.get("target", "all")
        pars = [self._live(p) for p in self.body.paragraphs_in(target)]
        total = sum(dr.text_of(p).count(find) for p in pars)
        if not total:
            joined = "\n".join(dr.text_of(p) for p in pars)
            hint = ""
            if find.lower() in joined.lower():
                hint = " The same words exist with different capital letters."
            elif " ".join(find.split()) in " ".join(joined.replace("\xa0", " ").split()):
                hint = " The same words exist with different spaces or line breaks; copy the text exactly from docx_read."
            raise EditError(f"the text '{_preview(find, 60)}' is not in {target} ({len(pars)} paragraphs searched).{hint}")
        left = count or total
        done = 0
        for p in pars:
            pos = 0
            while left > 0:
                text = dr.text_of(p)
                at = text.find(find, pos)
                if at < 0:
                    break
                if self.track:
                    dr.replace_tracked(p, at, at + len(find), repl, self.rev)
                else:
                    dr.replace_plain(p, at, at + len(find), repl)
                pos = at + len(repl)
                left -= 1
                done += 1
        return f"replace_text {target}: {done} of {total} occurrences replaced"

    def _do_set_paragraph(self, item: Dict[str, Any]) -> str:
        (p,) = self._pars(item.get("target"), single=True)
        text = self._text(item)
        style = item.get("style")
        if style is not None and not isinstance(style, str):
            raise EditError("'style' must be a style name")
        if style and self.track:
            dr.track_style_change(p, self.rev)
        self._write_text(p, text)
        if style:
            self._set_style(p, style)
        return f"set_paragraph {self.body.label(p)}"

    def _do_insert_paragraph(self, item: Dict[str, Any]) -> str:
        after, before = item.get("after"), item.get("before")
        if (after is None) == (before is None):
            raise EditError("give exactly one of 'after' or 'before': an address (p12, t2) or end / start of the document")
        ref = after if after is not None else before
        text = self._text(item)
        style = item.get("style")
        word = str(ref).strip().lower()
        if word in ("end", "start"):
            if (word == "end") != (after is not None):
                raise EditError("use 'after': 'end' for the end of the document and 'before': 'start' for its start")
            if not self.body.edges:
                raise EditError("the document body is empty")
            anchor = self.body.edges[-1] if word == "end" else self.body.edges[0]
        else:
            kind, nums = self.body.parse(ref)
            if kind == "par":
                if nums[0] != nums[1]:
                    raise EditError(f"'{ref}' is a range; give one paragraph or table")
                anchor = self.body.paras[nums[0] - 1]
            elif kind == "table":
                anchor = self.body.tables[nums[0] - 1]
            else:
                raise EditError(f"'{ref}' is a cell; insert next to a paragraph (pN) or a table (tN), or use set_cell")
        new = dr.OxmlElement("w:p")
        rpr = None
        if anchor.tag == dr.W_P and not style:
            lvl = self.body.heading_level(anchor)
            ppr = anchor.find(dr.W_PPR)
            if lvl is None and ppr is not None:
                copied = copy.deepcopy(ppr)
                for tag in (dr.W_RPR, W_SECT, qn("w:pPrChange")):
                    for old in copied.findall(tag):
                        copied.remove(old)
                new.append(copied)
            if lvl is None:
                rpr = dr.first_rpr(anchor)
                if rpr is not None:
                    rpr = copy.deepcopy(rpr)
                    for tag in UNTRACKED_RPR:
                        for old in rpr.findall(qn(tag)):
                            rpr.remove(old)
        if style:
            self._set_style(new, style)
        if self.track:
            dr.mark_paragraph(new, "w:ins", self.rev)
            if text:
                new.append(dr.track_inserted_run(text, rpr, self.rev))
        elif text:
            new.append(dr.new_run(text, rpr))
        if after is not None:
            key = anchor
            anchor = self.last_after.get(key, anchor)
            anchor.addnext(new)
            self.last_after[key] = new
        else:
            anchor.addprevious(new)
        return f"insert_paragraph {'after' if after is not None else 'before'} {ref}"

    def _do_delete_paragraph(self, item: Dict[str, Any]) -> str:
        pars = self._pars(item.get("target"))
        for p in pars:
            self._delete_paragraph(p)
        return f"delete_paragraph {item.get('target')}: {len(pars)} paragraph(s)"

    def _do_set_cell(self, item: Dict[str, Any]) -> str:
        tc = self.body.cell(item.get("target"))
        if tc in self.gone or tc.getparent() in self.gone:
            raise EditError(f"{item.get('target')} is in a row deleted by an earlier edit of this call")
        text = self._text(item)
        paras = self.body.cell_paras(tc)
        if not paras:
            raise EditError(f"{item.get('target')} has no paragraph to hold text")
        self._write_text(paras[0], text)
        for extra in paras[1:]:
            self._delete_paragraph(extra)
        return f"set_cell {item.get('target')}"

    def _do_insert_table_row(self, item: Dict[str, Any]) -> str:
        n, tbl = self.body.table(item.get("table"))
        rows = self.body.rows(tbl)
        after_row = self._int(item, "after_row", 0)
        cells = item.get("cells")
        if not isinstance(cells, list) or not all(isinstance(c, str) or c is None for c in cells):
            raise EditError("'cells' is needed: a list of texts, one for each cell of the row")
        if after_row > len(rows):
            raise EditError(f"after_row {after_row} does not exist: t{n} has {len(rows)} rows (0 puts the row first)")
        if not rows:
            raise EditError(f"t{n} has no row to copy the layout from")
        template = rows[max(after_row, 1) - 1]
        new = copy.deepcopy(template)
        tcs = self.body.cells(new)
        if len(cells) > len(tcs):
            raise EditError(f"t{n} rows have {len(tcs)} cells (a merged cell counts once) but {len(cells)} texts were given")
        trpr = new.find(qn("w:trPr"))
        if trpr is not None:
            for tag in ("w:ins", "w:del", "w:trPrChange"):
                for old in trpr.findall(qn(tag)):
                    trpr.remove(old)
        for i, tc in enumerate(tcs):
            paras = self.body.cell_paras(tc)
            for child in list(tc):
                if child.tag == W_TBL or (child.tag == dr.W_P and child is not paras[0]):
                    tc.remove(child)
            if not paras:
                tc.append(dr.OxmlElement("w:p"))
                paras = [tc.find(dr.W_P)]
            p = paras[0]
            rpr = dr.first_rpr(p)
            rpr = copy.deepcopy(rpr) if rpr is not None else None
            for child in list(p):
                if child.tag != dr.W_PPR:
                    p.remove(child)
            text = (cells[i] if i < len(cells) else "") or ""
            if self.track:
                dr.mark_paragraph(p, "w:ins", self.rev)
                if text:
                    p.append(dr.track_inserted_run(text.replace("\r\n", "\n"), rpr, self.rev))
            elif text:
                p.append(dr.new_run(text.replace("\r\n", "\n"), rpr))
        if self.track:
            dr.mark_row(new, "w:ins", self.rev)
        if after_row == 0:
            rows[0].addprevious(new)
        else:
            anchor = rows[after_row - 1]
            key = anchor
            anchor = self.last_after.get(key, anchor)
            anchor.addnext(new)
            self.last_after[key] = new
        return f"insert_table_row t{n} after row {after_row}"

    def _do_delete_table_row(self, item: Dict[str, Any]) -> str:
        n, tbl = self.body.table(item.get("table"))
        rows = self.body.rows(tbl)
        row = self._int(item, "row", 1)
        if row > len(rows):
            raise EditError(f"row {row} does not exist: t{n} has {len(rows)} rows")
        tr = rows[row - 1]
        if tr in self.gone:
            raise EditError(f"row {row} of t{n} was deleted by an earlier edit of this call")
        if len(rows) - sum(1 for r in rows if r in self.gone) <= 1:
            raise EditError(f"row {row} is the last row of t{n}; a table cannot be left with no rows")
        if self.track:
            dr.mark_row(tr, "w:del", self.rev)
            dr.wrap_deleted([r for p in tr.iter(dr.W_P) for r in dr.walk_runs(p)], self.rev)
        else:
            self.removals.append(tr)
        self.gone.add(tr)
        for tc in self.body.cells(tr):
            self.gone.update(self.body.cell_paras(tc))
        return f"delete_table_row t{n} row {row}"

    def _do_add_comment(self, item: Dict[str, Any]) -> str:
        from docx.text.run import Run
        text = self._text(item)
        author = item.get("author") or dr.AUTHOR
        initials = item.get("initials") or "".join(w[0] for w in str(author).split())[:3].upper()
        target = item.get("target")
        kind, _ = self.body.parse(target)
        pars = self._pars(target) if kind == "par" else [self._live(p) for p in self.body.paragraphs_in(target)]
        find = item.get("find")
        if find is not None:
            if not isinstance(find, str) or not find:
                raise EditError("'find' is the exact words the comment is about (not empty)")
            if len(pars) != 1:
                raise EditError("'find' needs a target of one paragraph or one cell")
            at = dr.text_of(pars[0]).find(find)
            if at < 0:
                raise EditError(f"the text '{_preview(find, 60)}' is not in {target}")
            runs = dr.isolate(pars[0], at, at + len(find))
        else:
            runs = [r for p in pars for r in dr.walk_runs(p)]
        if not runs:
            raise EditError(f"{target} has no text to anchor a comment on")
        p0 = pars[0]
        self.doc.add_comment([Run(runs[0], p0), Run(runs[-1], p0)], text=text, author=str(author), initials=str(initials))
        return f"add_comment {target}"

    def _do_set_style(self, item: Dict[str, Any]) -> str:
        style = item.get("style")
        if not isinstance(style, str) or not style:
            raise EditError("'style' is needed: a style name")
        target = item.get("target")
        kind, nums = self.body.parse(target)
        if kind == "table":
            if self.track:
                raise EditError("a table style cannot be written as a tracked change; run this edit with track_changes false")
            from docx.table import Table
            try:
                Table(self.body.tables[nums[0] - 1], self.doc._body).style = style
            except KeyError:
                names = [s.name for s in self.doc.styles if s.type == 3]
                raise EditError(f"there is no table style '{style}'. Table styles in this document: {', '.join(names[:40])}") from None
            return f"set_style {target}"
        if kind != "par":
            raise EditError("set_style takes paragraphs (pN, pN-pM) or a table (tN)")
        for p in self._pars(target):
            if self.track:
                dr.track_style_change(p, self.rev)
            self._set_style(p, style)
        return f"set_style {target}"


def register_docx_tools(registry: ToolRegistry, sandbox: Any) -> bool:
    """Registers the four Word tools; False (and nothing registered) when python-docx is not installed."""
    if not available():
        return False
    tools = DocxTools(sandbox)
    registry.register("docx_outline", OUTLINE_TEXT, OUTLINE_PARAMS, EffectClass.PURE, tools.outline, untrusted=True)
    registry.register("docx_read", READ_TEXT, READ_PARAMS, EffectClass.PURE, tools.read, untrusted=True)
    registry.register("docx_edit", EDIT_TEXT, EDIT_PARAMS, EffectClass.REVERSIBLE, tools.edit)
    registry.register("docx_create", CREATE_TEXT, CREATE_PARAMS, EffectClass.REVERSIBLE, tools.create)
    return True
