"""Workbook export. A synthetic workbook with the CCB layout is built for CI; when the real
CCB BASIC workbook is available next to the repository, the test also runs against it."""

from __future__ import annotations

import io
import warnings
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from cyfun.export_xlsx import ExportError, ExportInput, fill_workbook
from cyfun.framework import load_framework
from cyfun.scoring import ReqInput, compute

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT.parent / "CyFun2025_ Self-Assessment_tool_BASIC_v2026_02_20.xlsx"


def synthetic_template() -> bytes:
    """Same sheets, rows and formulas as the CCB BASIC tool, without styling or protection."""
    fw = load_framework()
    wb = openpyxl.Workbook()
    intro = wb.active
    intro.title = "Introduction"
    intro["Q6"] = date(2026, 2, 20)
    intro["T27"] = date(2026, 2, 20)
    sheets = {}
    for name in ("GOVERN", "IDENTIFY", "PROTECT", "DETECT", "RESPOND", "RECOVER"):
        ws = wb.create_sheet(name)
        ws["A2"], ws["F2"], ws["G2"] = "Category", "Documentation Score", "Implementation Score"
        sheets[name] = ws
    for r in fw.requirements:
        ws = sheets[r.sheet]
        ws.cell(r.row, 5, f"{r.workbook_id}: {r.text}")
        ws.cell(r.row, 6, 1)
        ws.cell(r.row, 7, 1)
    # subcategory and category formulas, same shape as the CCB workbook
    for f in fw.functions:
        ws = sheets[f.name]
        for c in f.categories:
            sub_rows = []
            for s in c.subcategories:
                rows = [q.row for q in s.requirements]
                first = rows[0]
                sub_rows.append(first)
                for col, src in (("H", "F"), ("I", "G")):
                    parts = [f'IF(OR($F{x}="N/A",$G{x}="N/A"),2.5,${src}{x})' for x in rows]
                    ws[f"{col}{first}"] = f"=AVERAGE({','.join(parts)})" if len(parts) > 1 else f"={parts[0]}"
            cat_first = sub_rows[0]
            ws[f"J{cat_first}"] = "=AVERAGE(" + ",".join(f"H{x}" for x in sub_rows) + ")"
            ws[f"K{cat_first}"] = "=AVERAGE(" + ",".join(f"I{x}" for x in sub_rows) + ")"
    summ = wb.create_sheet("BASIC Summary")
    row = 5
    for f in fw.functions:
        ws_name = f.name
        for c in f.categories:
            first = c.subcategories[0].requirements[0].row
            summ[f"B{row}"] = c.name
            summ[f"E{row}"] = f"={ws_name}!J{first}"
            summ[f"F{row}"] = f"={ws_name}!K{first}"
            summ[f"D{row}"] = f"=AVERAGE(E{row},F{row})"
            row += 1
    summ["M4"] = "=SUM(D5:D21)/COUNT(D5:D21)"
    summ["N11"] = "=Introduction!T27"
    for i, k in enumerate(fw.key_measures):
        r = 25 + i
        summ[f"L{r}"] = k.id
        summ[f"P{r}"] = f"={k.sheet}!F{k.row}"
        summ[f"Q{r}"] = f"={k.sheet}!G{k.row}"
        summ[f"O{r}"] = f"=AVERAGE(P{r},Q{r})"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def sample_inputs(fw):
    exp, sco = {}, {}
    for i, r in enumerate(fw.requirements):
        if r.id == "PR.AA-06.1":
            exp[r.id] = ExportInput(None, None, True, "No own premises; offices are serviced.")
            sco[r.id] = ReqInput(None, None, True)
        else:
            d, m = (i % 5) + 1, ((i * 2) % 5) + 1
            exp[r.id] = ExportInput(d, m, False, f"Justification {r.id} <&> ok")
            sco[r.id] = ReqInput(d, m)
    return exp, sco


def _check(template: bytes):
    fw = load_framework()
    exp, sco = sample_inputs(fw)
    out, info = fill_workbook(template, fw, exp, date(2026, 10, 5))
    assert info["requirements_written"] == 34
    summary = compute(fw, sco)
    wb = openpyxl.load_workbook(io.BytesIO(out), data_only=True)
    ws = wb["BASIC Summary"]
    assert abs(ws["M4"].value - summary.total_maturity) < 1e-9
    assert wb["Introduction"]["T27"].value.date() == date(2026, 10, 5)
    for i, c in enumerate(fw.categories):
        g = summary.categories[c.id]
        assert abs(ws[f"E{5 + i}"].value - g.doc) < 1e-9
        assert abs(ws[f"F{5 + i}"].value - g.impl) < 1e-9
        assert abs(ws[f"D{5 + i}"].value - g.maturity) < 1e-9
    for i, k in enumerate(summary.key_measures):
        assert abs(ws[f"O{25 + i}"].value - k.maturity) < 1e-9
    gov = wb["GOVERN"]
    assert gov["F3"].value == exp["GV.OC-03.1"].doc
    assert gov["L3"].value == "Justification GV.OC-03.1 <&> ok"
    pro = wb["PROTECT"]
    assert pro["F10"].value == "N/A" and pro["G10"].value == "N/A"
    assert pro["H10"].value == 2.5
    # formulas are still formulas
    wbf = openpyxl.load_workbook(io.BytesIO(out))
    assert str(wbf["BASIC Summary"]["M4"].value).startswith("=")
    assert str(wbf["GOVERN"]["H3"].value).startswith("=IF(")
    return out


def test_fill_synthetic_workbook():
    _check(synthetic_template())


def test_rejects_wrong_workbook():
    fw = load_framework()
    wb = openpyxl.Workbook()
    wb.active.title = "Something"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ExportError):
        fill_workbook(buf.getvalue(), fw, {}, None)


def test_leaves_unscored_rows_alone():
    fw = load_framework()
    out, info = fill_workbook(synthetic_template(), fw, {"GV.OC-03.1": ExportInput(4, 3, False, "x")}, None)
    assert info["requirements_written"] == 1
    wb = openpyxl.load_workbook(io.BytesIO(out), data_only=True)
    assert wb["GOVERN"]["F3"].value == 4
    assert wb["GOVERN"]["F4"].value == 1  # template default untouched


@pytest.mark.skipif(not REAL.exists(), reason="official CCB workbook not available")
def test_fill_real_ccb_workbook():
    import zipfile

    template = REAL.read_bytes()
    out = _check(template)
    z1, z2 = zipfile.ZipFile(io.BytesIO(template)), zipfile.ZipFile(io.BytesIO(out))
    assert z1.namelist() == z2.namelist()
    changed = {n for n in z1.namelist() if z1.read(n) != z2.read(n)}
    assert changed <= {
        "xl/workbook.xml",
        "xl/worksheets/sheet1.xml",
        "xl/worksheets/sheet3.xml",
        "xl/worksheets/sheet4.xml",
        "xl/worksheets/sheet5.xml",
        "xl/worksheets/sheet6.xml",
        "xl/worksheets/sheet7.xml",
        "xl/worksheets/sheet8.xml",
        "xl/worksheets/sheet10.xml",
    }
    assert "sheetProtection" in z2.read("xl/worksheets/sheet3.xml").decode()
    assert "xl/charts/chart1.xml" in z2.namelist()
