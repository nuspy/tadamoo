"""What a document gave and what it did not: the pages it has against the pages that hold passages, the pages that came out empty or could not be read, and the
passages that read as instructions. One small record per document (`coverage` in its meta), written whenever a document is read, and an archive's sum of them.

A page is covered when some passage starts, ends or runs across it. A page that is neither covered nor named as empty or skipped is listed as uncovered:
blank in the original, or text the reading lost; the person judges."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

SHOWN = 50                          # page numbers kept in a record; the count is always exact
OK, CHECK, INCOMPLETE = "ok", "check", "incomplete"


def _span(numbers: Sequence[int]) -> str:
    """`[1, 2, 3, 7]` as `1-3, 7`."""
    out, run = [], []
    for n in sorted(set(numbers)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def of_document(doc: Dict[str, Any], chunks: Iterable[Dict[str, Any]], pages: int = -1) -> Dict[str, Any]:
    """The coverage record of a document from its meta (`pages`, `empty_pages`, `skipped_pages`, `flagged`) and its passages. `pages` overrides the meta's page count."""
    rows = list(chunks)
    expected = int(doc.get("pages") or 0) if pages < 0 else int(pages)
    skipped = sorted({int(n) for n in doc.get("skipped_pages") or []})
    figure_rows = [c for c in rows if c.get("kind") == "figure"]
    text_pages = {p for c in rows if c.get("kind") != "figure" for p in range(int(c.get("page_start") or 0), max(int(c.get("page_start") or 0), int(c.get("page_end") or 0)) + 1)}
    reader_empty = {int(n) for n in doc.get("empty_pages") or []}
    on_figures = {int(c["page_start"]) for c in figure_rows if int(c.get("page_start") or 0) > 0}
    figure_pages = sorted(n for n in on_figures if n in reader_empty or n not in text_pages)                 # pages whose only content is a figure
    empty = sorted(reader_empty - set(figure_pages))                                                           # a page that gave no text but whose figure was kept is not an empty page
    covered = set()
    for c in rows:
        first, last = int(c.get("page_start") or 0), int(c.get("page_end") or 0)
        if first > 0:
            covered.update(range(first, max(first, last) + 1))
    uncovered: List[int] = []
    if expected > 0:
        covered = {n for n in covered if n <= expected}
        uncovered = [n for n in range(1, expected + 1) if n not in covered and n not in skipped and n not in empty]
    problems: List[str] = []
    if not rows:
        problems.append("no passage was made from this document")
    if skipped:
        problems.append(f"{len(skipped)} page{'s' if len(skipped) > 1 else ''} could not be read and {'are' if len(skipped) > 1 else 'is'} missing: {_span(skipped)}")
    if empty:
        problems.append(f"{len(empty)} page{'s' if len(empty) > 1 else ''} gave no text although {'they have' if len(empty) > 1 else 'it has'} a picture or text: {_span(empty)}")
    if uncovered:
        problems.append(f"{len(uncovered)} page{'s' if len(uncovered) > 1 else ''} hold no passage (blank, or the reading lost the text): {_span(uncovered)}")
    flagged = sum(1 for c in rows if c.get("flag"))
    status = INCOMPLETE if (not rows or skipped) else CHECK if (empty or uncovered) else OK
    return {"pages": expected, "covered": len(covered) if expected else 0, "uncovered": uncovered[:SHOWN], "empty": empty[:SHOWN], "skipped": skipped[:SHOWN],
            "passages": len(rows), "flagged": flagged, "status": status, "problems": problems, "figures": len(figure_rows), "figure_pages": figure_pages[:SHOWN]}


def of_archive(documents: Sequence[Dict[str, Any]], failed: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The sum of the documents' records: pages expected and covered, how many documents are ok, to be checked or incomplete, the problems by document, and the
    documents the last refresh could not read (`failed`: `{name, reason}`)."""
    totals = {OK: 0, CHECK: 0, INCOMPLETE: 0}
    pages = covered = flagged = unknown = 0
    problems: List[Dict[str, Any]] = []
    for d in documents:
        cov = d.get("coverage")
        if not isinstance(cov, dict):
            unknown += 1
            continue
        totals[cov.get("status") if cov.get("status") in totals else CHECK] += 1
        pages += int(cov.get("pages") or 0)
        covered += int(cov.get("covered") or 0)
        flagged += int(cov.get("flagged") or 0)
        if cov.get("problems"):
            problems.append({"doc": d["id"], "name": d["name"], "status": cov.get("status"), "problems": list(cov["problems"])})
    status = INCOMPLETE if (totals[INCOMPLETE] or failed) else CHECK if (totals[CHECK] or unknown) else OK
    return {"documents": len(documents), "ok": totals[OK], "check": totals[CHECK], "incomplete": totals[INCOMPLETE], "unknown": unknown, "pages": pages, "covered": covered,
            "flagged": flagged, "failed": [dict(f) for f in failed][:SHOWN], "problems": problems[:SHOWN], "status": status}
