"""The 'documents' evaluation set (the A/B of the cold tools): its checker accepts what the editing tools make on a copy, and refuses a run that did
nothing or touched the original."""
from pathlib import Path

import pytest

pytest.importorskip("docx")
pytest.importorskip("openpyxl")
pytest.importorskip("pptx")
pytest.importorskip("pypdf")
pytest.importorskip("pikepdf")

from test_docx_parts import Sandbox  # noqa: E402
from tadamoo.eval.tasks import load_task_set, make_workspace, run_checks  # noqa: E402
from tadamoo.tools.docx_edit import DocxTools  # noqa: E402
from tadamoo.tools.pdf_edit import PdfTools  # noqa: E402
from tadamoo.tools.pptx_edit import PptxTools  # noqa: E402
from tadamoo.tools.xlsx_edit import XlsxTools  # noqa: E402

TASKS = {t.id: t for t in load_task_set("documents")[1]}
REPLACE = {"kind": "replace_text", "find": "1,200.00", "replace": "1,500.00", "count": 0}


def workspace(tmp_path, task_id):
    return make_workspace(TASKS[task_id], tmp_path / task_id)


def test_the_set_has_the_four_documents_and_each_starts_as_a_failure(tmp_path):
    assert sorted(TASKS) == ["excel_row", "pdf_price", "pptx_price", "word_price"]
    for task_id, task in TASKS.items():
        ws = workspace(tmp_path, task_id)
        assert run_checks(task, ws, "", []), task_id                       # nothing done: it must not pass


def test_what_the_tools_make_on_a_copy_passes(tmp_path):
    ws = workspace(tmp_path, "word_price")
    DocxTools(Sandbox(ws)).edit("offer.docx", [{**REPLACE, "target": "everywhere"}])
    assert run_checks(TASKS["word_price"], ws, "", []) == []
    ws = workspace(tmp_path, "excel_row")
    XlsxTools(Sandbox(ws)).edit("budget.xlsx", [{"kind": "append_rows", "sheet": "Budget", "rows": [["Rent", 900, 950, "=B5+C5"]]}])
    assert run_checks(TASKS["excel_row"], ws, "", []) == []
    ws = workspace(tmp_path, "pptx_price")
    PptxTools(Sandbox(ws)).edit("plan.pptx", [REPLACE, {"kind": "add_slide", "layout": "Title and Content", "title": "Next steps", "body": ["one", "two"]}])
    assert run_checks(TASKS["pptx_price"], ws, "", []) == []
    ws = workspace(tmp_path, "pdf_price")
    PdfTools(Sandbox(ws)).edit("price.pdf", [REPLACE])
    assert run_checks(TASKS["pdf_price"], ws, "", []) == []


def test_changing_the_original_fails(tmp_path):
    ws = workspace(tmp_path, "word_price")
    DocxTools(Sandbox(ws)).edit("offer.docx", [{**REPLACE, "target": "everywhere"}], output="offer.docx", overwrite=True)
    (ws / "other.docx").write_bytes((ws / "offer.docx").read_bytes())
    failures = run_checks(TASKS["word_price"], ws, "", [])
    assert failures and "original" in failures[0]
