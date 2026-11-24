"""The check of a 'documents' task: python check.py <kind>. It looks at what the model left in the workspace and never at how it got there:
the original must be as it was (by hash) and a new file of the same kind must hold the change."""
import hashlib
import sys
from pathlib import Path

ORIGINALS = {'offer.docx': '3eeec37b4966929cc01d3bab64057cff71d5f1ed7d3d6665488e570e08429882', 'budget.xlsx': '9a0dffd9b45e622d61f9fb48700130f0aae674e2ef80bda51c1a99e768bcd83d', 'plan.pptx': '0cba8b099ba7a66c61edd0149236e313baccfa2313cb836c562ded3b8f75a550', 'price.pdf': 'afa1aada09bdd998a385cad61e5a76fa23cdbe782253e1ad01f9d3be67525152'}
KINDS = {"word": ("offer.docx", ".docx"), "excel": ("budget.xlsx", ".xlsx"), "powerpoint": ("plan.pptx", ".pptx"), "pdf": ("price.pdf", ".pdf")}


def fail(text):
    print("FAIL:", text)
    sys.exit(1)


kind = sys.argv[1]
original, suffix = KINDS[kind]
here = Path(".")
if hashlib.sha256((here / original).read_bytes()).hexdigest() != ORIGINALS[original]:
    fail(original + " was changed: the original must be left as it is")
copies = [p for p in here.iterdir() if p.suffix.lower() == suffix and p.name != original and not p.name.startswith(".")]
if not copies:
    fail("no new " + suffix + " file")


def word(p):
    from docx import Document
    d = Document(str(p))
    body = "\n".join(x.text for x in d.paragraphs)
    head = "\n".join(x.text for s in d.sections for x in s.header.paragraphs)
    if "1,500.00" not in body or "1,500.00" not in head or "1,200.00" in body + head:
        fail("the price is not 1,500.00 in the body and the header")
    bold = [r for x in d.paragraphs for r in x.runs if "1,500.00" in r.text and r.bold]
    if not bold:
        fail("the new price lost its bold")


def excel(p):
    from openpyxl import load_workbook
    ws = load_workbook(str(p))["Budget"]
    rows = [[c.value for c in row] for row in ws.iter_rows()]
    rent = [r for r in rows if r and str(r[0]).strip().lower() == "rent"]
    if not rent or rent[0][1] != 900 or rent[0][2] != 950:
        fail("no row 'Rent' with 900 and 950")
    if not str(rent[0][3] or "").startswith("="):
        fail("the total of the Rent row is not a formula")
    if rows[1][3] != "=B2+C2":
        fail("an existing formula was changed")


def powerpoint(p):
    from pptx import Presentation
    prs = Presentation(str(p))
    texts, notes = [], []
    for s in prs.slides:
        for sh in s.shapes:
            if sh.has_text_frame:
                texts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                texts += [c.text for r in sh.table.rows for c in r.cells]
        if s.has_notes_slide:
            notes.append(s.notes_slide.notes_text_frame.text)
    allt = "\n".join(texts + notes)
    if "1,500.00" not in allt or "1,200.00" in allt:
        fail("the price is not changed everywhere (text, table, notes)")
    if not any("next steps" in t.lower() for t in texts):
        fail("no slide titled Next steps")


def pdf(p):
    from pypdf import PdfReader
    text = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
    if "1,500.00" not in text or "1,200.00" in text:
        fail("the price is not 1,500.00 in the text of the page")


last = None
for copy in sorted(copies):
    try:
        {"word": word, "excel": excel, "powerpoint": powerpoint, "pdf": pdf}[kind](copy)
        print("ok:", copy.name)
        sys.exit(0)
    except SystemExit as e:
        if e.code == 0:
            raise
        last = e
sys.exit(1)
