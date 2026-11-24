"""The Excel tools: outline and read to find what to change, ordered all-or-nothing edits, creation from rows, and the refusal of what a save would damage."""
import zipfile
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")
from openpyxl import Workbook, load_workbook  # noqa: E402

from test_docx_parts import Sandbox  # noqa: E402
from tadamoo.tools.office_io import OfficeError  # noqa: E402
from tadamoo.tools.xlsx_edit import XlsxTools  # noqa: E402


def make(path: Path, formulas=True) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["item", "qty", "price", "total"])
    for i, (name, qty, price) in enumerate([("bolt", 10, 0.5), ("nut", 20, 0.25), ("gear", 3, 12.0)], 2):
        ws.append([name, qty, price, f"=B{i}*C{i}" if formulas else qty * price])
    ws.append(["", "", "sum", "=SUM(D2:D4)" if formulas else 0])
    ws2 = wb.create_sheet("Notes")
    ws2["A1"] = "Prices valid until March"
    wb.save(path)
    return path


@pytest.fixture
def tools(tmp_path):
    make(tmp_path / "b.edited.xlsx")
    return XlsxTools(Sandbox(tmp_path)), tmp_path / "b.edited.xlsx"


def test_the_outline_and_the_reader_show_values_and_formulas(tools):
    t, _ = tools
    out = t.outline("b.edited.xlsx")
    assert "2 sheets" in out and "Budget" in out and "4 rows" not in out and "item | qty | price | total" in out and "3 formulas" not in out and "4 formulas" in out
    assert "D2==B2*C2" in t.read("b.edited.xlsx", "Budget", "A1:D3").replace(" → None", "") or "=B2*C2" in t.read("b.edited.xlsx", "Budget", "A1:D3")
    assert "bolt" in t.read("b.edited.xlsx", "1", "A1:B3") and "Prices valid" in t.read("b.edited.xlsx", "notes", "A1:A1")


def test_edits_apply_in_order_and_keep_the_formulas(tools):
    t, path = tools
    out = t.edit("b.edited.xlsx", [{"kind": "set_cell", "sheet": "Budget", "cell": "B2", "value": 12},
                            {"kind": "append_rows", "sheet": "Budget", "rows": [["washer", 5, 0.1, "=B6*C6"]]},
                            {"kind": "set_format", "sheet": "Budget", "range": "A1:D1", "bold": True, "fill": "DDDDDD"},
                            {"kind": "find_replace", "find": "March", "replace": "April"},
                            {"kind": "add_sheet", "name": "Extra"}])
    assert out["edits"] == 5
    wb = load_workbook(path)
    ws = wb["Budget"]
    assert ws["B2"].value == 12 and ws["D2"].value == "=B2*C2" and ws["A6"].value == "washer" and ws["D6"].value == "=B6*C6"
    assert ws["A1"].font.bold and wb["Notes"]["A1"].value == "Prices valid until April" and "Extra" in wb.sheetnames


def test_one_bad_edit_changes_nothing(tools):
    t, path = tools
    before = path.read_bytes()
    with pytest.raises(OfficeError, match="Nothing was changed"):
        t.edit("b.edited.xlsx", [{"kind": "set_cell", "cell": "A1", "value": "x"}, {"kind": "set_cell", "sheet": "Nope", "cell": "A1", "value": 1}, {"kind": "fly"}])
    assert path.read_bytes() == before


def test_a_row_shift_is_refused_where_formulas_would_point_at_the_wrong_cells(tools, tmp_path):
    t, _ = tools
    with pytest.raises(OfficeError, match="formulas, merged cells or tables"):
        t.edit("b.edited.xlsx", [{"kind": "insert_rows", "sheet": "Budget", "at": 2}])
    make(tmp_path / "plain.edited.xlsx", formulas=False)
    ws = load_workbook(tmp_path / "plain.edited.xlsx")["Budget"]
    t.edit("plain.edited.xlsx", [{"kind": "insert_rows", "sheet": "Budget", "at": 2}])
    assert load_workbook(tmp_path / "plain.edited.xlsx")["Budget"]["A3"].value == "bolt" and ws.max_row == 5


def test_renaming_or_deleting_a_sheet_that_formulas_use_is_refused(tools, tmp_path):
    t, _ = tools
    wb = load_workbook(tmp_path / "b.edited.xlsx")
    wb["Notes"]["B1"] = "=Budget!A2"
    wb.save(tmp_path / "b.edited.xlsx")
    with pytest.raises(OfficeError, match="formulas refer to the sheet"):
        t.edit("b.edited.xlsx", [{"kind": "rename_sheet", "sheet": "Budget", "new_name": "Costs"}])
    with pytest.raises(OfficeError, match="formulas refer to the sheet"):
        t.edit("b.edited.xlsx", [{"kind": "delete_sheet", "sheet": "Budget"}])


def test_output_keeps_the_original_and_never_replaces_a_file_unasked(tools, tmp_path):
    t, path = tools
    before = path.read_bytes()
    t.edit("b.edited.xlsx", [{"kind": "set_cell", "cell": "B2", "value": 99}], output="copy.xlsx")
    assert path.read_bytes() == before and load_workbook(tmp_path / "copy.xlsx")["Budget"]["B2"].value == 99
    with pytest.raises(OfficeError, match="already exists"):
        t.edit("b.edited.xlsx", [{"kind": "set_cell", "cell": "B2", "value": 1}], output="copy.xlsx")
    t.edit("b.edited.xlsx", [{"kind": "set_cell", "cell": "B2", "value": 1}], output="copy.xlsx", overwrite=True)
    assert load_workbook(tmp_path / "copy.xlsx")["Budget"]["B2"].value == 1


def test_a_workbook_with_a_chart_is_refused_not_damaged(tools, tmp_path):
    t, path = tools
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp_path / "chart.xlsx", "w") as out:
        for item in zin.infolist():
            out.writestr(item, zin.read(item.filename))
        out.writestr("xl/charts/chart1.xml", "<c/>")
    assert "WARNING" in t.outline("chart.xlsx") and "charts" in t.outline("chart.xlsx")
    with pytest.raises(OfficeError, match="charts"):
        t.edit("chart.xlsx", [{"kind": "set_cell", "cell": "A1", "value": "x"}])


def test_a_workbook_is_created_from_rows(tools, tmp_path):
    t, _ = tools
    t.create("new.xlsx", [{"name": "Data", "rows": [["a", "b"], [1, 2.5], ["=A2+B2", None]]}])
    ws = load_workbook(tmp_path / "new.xlsx")["Data"]
    assert ws["A1"].font.bold and ws["A3"].value == "=A2+B2" and ws.freeze_panes == "A2"
    with pytest.raises(OfficeError, match="already exists"):
        t.create("new.xlsx", [{"name": "Data", "rows": [["a"]]}])


def test_the_original_is_never_written_and_the_copy_goes_on_in_place(tools):
    t, _ = tools
    tmp_path = _.parent
    make(tmp_path / "orig.xlsx")
    before = (tmp_path / "orig.xlsx").read_bytes()
    out = t.edit("orig.xlsx", [{"kind": "set_cell", "sheet": "Budget", "cell": "B2", "value": 5}])
    assert out["written"] == "orig.edited.xlsx" and (tmp_path / "orig.xlsx").read_bytes() == before
    assert load_workbook(tmp_path / "orig.edited.xlsx")["Budget"]["B2"].value == 5
    t.edit("orig.edited.xlsx", [{"kind": "set_cell", "sheet": "Budget", "cell": "B2", "value": 6}])      # a working copy is written in place
    assert load_workbook(tmp_path / "orig.edited.xlsx")["Budget"]["B2"].value == 6
    assert t.edit("orig.xlsx", [{"kind": "set_cell", "sheet": "Budget", "cell": "B2", "value": 7}])["written"] == "orig.edited2.xlsx"
    assert (tmp_path / "orig.xlsx").read_bytes() == before


def test_an_attached_file_is_read_by_its_id_and_edited_into_a_copy_in_the_workspace(tools, tmp_path_factory):
    t, _ = tools
    tmp_path = _.parent
    stored = tmp_path_factory.mktemp("store") / "named.xlsx"
    make(stored)
    t.sandbox.attachment_files = lambda att_id: (stored, "Prices 2026.xlsx")
    assert "Budget" in t.outline("attachment:abc")
    out = t.edit("attachment:abc", [{"kind": "set_cell", "sheet": "Budget", "cell": "B2", "value": 5}])
    assert out["written"] == "Prices 2026.edited.xlsx"
    assert load_workbook(tmp_path / "Prices 2026.edited.xlsx")["Budget"]["B2"].value == 5
    assert load_workbook(stored)["Budget"]["B2"].value != 5
    with pytest.raises(Exception):
        t.edit("attachment:abc", [{"kind": "set_cell", "cell": "A1", "value": 1}], output="Prices 2026.edited.xlsx")
