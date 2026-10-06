"""Build app/cyfun/framework/<level>_2025.json from an official CCB self-assessment workbook.

Usage:
    python scripts/build_framework.py BASIC     "path/to/CyFun2025_ Self-Assessment_tool_BASIC_v2026_02_20.xlsx"
    python scripts/build_framework.py IMPORTANT "path/to/CyFun2025_ Self-Assessment_tool_IMPORTANT_v2026_02_20.xlsx"
    python scripts/build_framework.py ESSENTIAL "path/to/CyFun2025_Self-Assessment_tool_ESSENTIAL_v3.1.xlsx"

The workbooks are published by the Centre for Cybersecurity Belgium (CCB) on https://www.cyfun.eu
and are not redistributed in this repository. The script reads the six function sheets, the
Maturity Levels sheet and the Introduction sheet, and writes a JSON file the application loads.

Nothing in the output is typed in by hand: requirement text, assurance level per requirement,
key-measure and management-aspect flags, thresholds, N/A rules, the cell positions used by the
workbook export, and the aggregation groups behind each category score all come from the
workbook itself. The aggregation groups are parsed from the workbook's own formulas, so the
application reproduces the category and total maturity exactly as Excel computes them in the
submitted file, including the places where a workbook deviates from its subcategory structure
(those deviations are listed under "quirks").
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.utils import column_index_from_string

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

FUNCTIONS = [("GV", "GOVERN"), ("ID", "IDENTIFY"), ("PR", "PROTECT"), ("DE", "DETECT"), ("RS", "RESPOND"), ("RC", "RECOVER")]
LEVELS = ("BASIC", "IMPORTANT", "ESSENTIAL")
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

CAT_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<id>[A-Z]{2}\.[A-Z]{2})\)\s*:?\s*(?P<desc>.*)$", re.S)
SUB_RE = re.compile(r"^(?P<id>[A-Z]{2}\.[A-Z]{2}-\d{1,2})\s*:\s*(?P<title>.+)$", re.S)
REQ_RE = re.compile(r"^(?P<id>[A-Z]{2}\.[A-Z]{2}-\d{1,2}[.-]\d{1,2})\s*:?\s*(?P<text>.+)$", re.S)


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def normalise_id(raw: str) -> str:
    m = re.fullmatch(r"([A-Z]{2}\.[A-Z]{2})-(\d{1,2})[.-](\d{1,2})", raw)
    if not m:
        raise SystemExit(f"cannot normalise requirement id {raw!r}")
    return f"{m.group(1)}-{int(m.group(2)):02d}.{m.group(3)}"


def parse_threshold(value) -> float | None:
    s = clean(value).lower().replace(",", ".")
    if not s or s.startswith("n/a"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*5", s)
    if not m:
        raise SystemExit(f"cannot parse threshold {value!r}")
    return float(m.group(1))


def find_cell(ws, predicate):
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None and predicate(c.value):
                return c
    return None


def formula_cells(formula: str, col: str, wsf) -> list[int]:
    """Rows of the cells in column `col` that a category formula aggregates (refs and ranges)."""
    rows: list[int] = []
    col_idx = column_index_from_string(col)
    for m in re.finditer(rf"\$?{col}\$?(\d+)(?::\$?{col}\$?(\d+))?", formula or ""):
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        for r in range(a, b + 1):
            if r == a == b or wsf.cell(r, col_idx).value is not None:  # a range only counts cells that hold a value or formula
                rows.append(r)
    return rows


def formula_rows(formula: str, doc: str, impl: str) -> list[int]:
    """Requirement rows referenced by a subcategory formula through the score columns."""
    return sorted({int(m.group(1)) for m in re.finditer(rf"\$?[{doc}{impl}]\$?(\d+)", formula or "")})


def build(level: str, path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    wbf = openpyxl.load_workbook(path, data_only=False)
    intro = wb["Introduction"]

    # versions -----------------------------------------------------------------------
    dates = [c.value for row in intro.iter_rows(min_col=17, max_col=17) for c in row if isinstance(c.value, datetime)]
    tool_version = max(dates).date().isoformat() if dates else ""
    req_label = find_cell(intro, lambda v: clean(v).lower() == "requirements")
    framework_version = ""
    if req_label is not None:
        v = intro.cell(req_label.row, req_label.column + 2).value
        framework_version = v.date().isoformat() if isinstance(v, datetime) else clean(v)
    date_label = find_cell(intro, lambda v: "completed by the entity on" in clean(v).lower())
    if date_label is None:
        raise SystemExit("completion date label not found on the Introduction sheet")
    date_cell = f"{col_letter(date_label.column + 3)}{date_label.row}"
    if not isinstance(intro[date_cell].value, datetime):
        raise SystemExit(f"expected a date in Introduction!{date_cell}")

    # directions: N/A rules -----------------------------------------------------------
    directions = clean(find_cell(intro, lambda v: "Directions" in clean(v)).value)
    m = re.search(r"Excluded measures?:\s*For the \w+ assurance level,\s*(\w+)\s+measures? may be excluded", directions, re.I)
    if not m:
        raise SystemExit("cannot parse the number of excludable measures from the directions")
    na_allowed = WORDS.get(m.group(1).lower()) or int(m.group(1))
    na_excludes = ["key_measure"]
    if re.search(r"controls linked to (the )?management aspects cannot be excluded", directions, re.I):
        na_excludes.append("management_aspect")

    # maturity levels and thresholds -------------------------------------------------
    ml = wb["Maturity Levels"]
    maturity = [
        {
            "value": int(ml.cell(r, 2).value),
            "name": clean(ml.cell(r, 1).value),
            "documentation": clean(ml.cell(r, 3).value),
            "implementation": clean(ml.cell(r, 4).value),
        }
        for r in range(2, 7)
    ]
    labels = {clean(ml.cell(r, 6).value).lower(): ml.cell(r, 7).value for r in range(2, 5)}
    km_min = parse_threshold(next(v for k, v in labels.items() if "key measure" in k))
    cat_min = parse_threshold(next(v for k, v in labels.items() if "category" in k))
    total_min = parse_threshold(next(v for k, v in labels.items() if "total" in k))

    # layout -------------------------------------------------------------------------
    first = wb["GOVERN"]
    # a workbook with an "Assurance level" column E has every later column one to the right
    shifted = clean(first["E2"].value).lower() == "assurance level"
    off = 1 if shifted else 0
    cols = {"level": 5 if shifted else None, "req": 5 + off, "doc": 6 + off, "impl": 7 + off, "sub_doc": 8 + off, "comment": 12 + off}
    doc_l, impl_l = col_letter(cols["doc"]), col_letter(cols["impl"])
    sub_doc_l, sub_impl_l = col_letter(cols["sub_doc"]), col_letter(cols["sub_doc"] + 1)
    cat_doc_l, cat_impl_l = col_letter(cols["sub_doc"] + 2), col_letter(cols["sub_doc"] + 3)
    na_value = None
    for r in range(3, first.max_row + 1):
        f = wbf["GOVERN"].cell(r, cols["sub_doc"]).value
        if isinstance(f, str) and "N/A" in f:
            mm = re.search(r'="N/A"\),\s*([\d.,]+),', f)
            if mm:
                na_value = float(mm.group(1).replace(",", "."))
                break
    if na_value is None:
        raise SystemExit("cannot read the N/A substitution value from the GOVERN formulas")
    summary_sheet = next(ws.title for ws in wb.worksheets if "summary" in ws.title.lower())

    # requirements -------------------------------------------------------------------
    functions = []
    seen: set[str] = set()
    quirks: list[str] = []
    for fid, sheet in FUNCTIONS:
        ws = wb[sheet]
        wsf = wbf[sheet]
        if clean(ws.cell(2, cols["req"]).value).lower() != "requirement":
            raise SystemExit(f"{sheet}: requirement column not where expected")
        categories: list[dict] = []
        cat = sub = None
        row_to_id: dict[int, str] = {}
        for r in range(3, ws.max_row + 1):
            a = ws.cell(r, 1).value
            b = ws.cell(r, 2).value
            c = ws.cell(r, 3).value
            d = ws.cell(r, 4).value
            e = ws.cell(r, cols["req"]).value
            lvl = clean(ws.cell(r, cols["level"]).value) if cols["level"] else "Basic"
            if a:
                m = CAT_RE.match(clean(a))
                if not m:
                    raise SystemExit(f"{sheet}!A{r}: unexpected category text: {a!r}")
                cat = {"id": m["id"], "name": m["name"].strip(), "description": m["desc"].strip(), "row": r, "subcategories": []}
                categories.append(cat)
            if d:
                m = SUB_RE.match(clean(d))
                if not m:
                    raise SystemExit(f"{sheet}!D{r}: unexpected subcategory text: {d!r}")
                sub = {"id": m["id"], "title": m["title"].strip(), "requirements": []}
                cat["subcategories"].append(sub)
            if e:
                m = REQ_RE.match(clean(e))
                if not m:
                    raise SystemExit(f"{sheet}!{col_letter(cols['req'])}{r}: unexpected requirement text: {e!r}")
                rid = normalise_id(m["id"])
                if rid in seen:
                    raise SystemExit(f"duplicate requirement id {rid} at {sheet} row {r}")
                seen.add(rid)
                if lvl.upper() not in LEVELS:
                    raise SystemExit(f"{sheet} row {r}: unexpected assurance level {lvl!r}")
                row_to_id[r] = rid
                sub["requirements"].append(
                    {
                        "id": rid,
                        "workbook_id": m["id"],
                        "text": m["text"].strip(),
                        "level": lvl.capitalize(),
                        "key_measure": clean(c).lower() == "key measure",
                        "management_aspect": bool(clean(b)),
                        "management_label": clean(b),
                        "sheet": sheet,
                        "row": r,
                    }
                )

        # aggregation groups from the workbook's own category formulas -------------------
        for cat in categories:
            r0 = cat.pop("row")
            groups: dict[str, list[list[str]]] = {}
            for dim, cat_col, sub_col in (("doc", cat_doc_l, sub_doc_l), ("impl", cat_impl_l, sub_impl_l)):
                formula = wsf.cell(r0, column_index_from_string(cat_col)).value
                if not isinstance(formula, str) or not formula.startswith("="):
                    raise SystemExit(f"{sheet}!{cat_col}{r0}: category formula expected for {cat['id']}")
                dim_groups: list[list[str]] = []
                for cr in formula_cells(formula, sub_col, wsf):
                    sub_formula = wsf.cell(cr, column_index_from_string(sub_col)).value
                    rows = formula_rows(str(sub_formula), doc_l, impl_l) if isinstance(sub_formula, str) else [cr]
                    ids = [row_to_id[x] for x in rows if x in row_to_id]
                    if ids:
                        dim_groups.append(ids)
                groups[dim] = dim_groups
            cat["score_groups"] = groups
            expected = [[q["id"] for q in s["requirements"]] for s in cat["subcategories"]]
            all_ids = {q["id"] for s in cat["subcategories"] for q in s["requirements"]}
            for dim in ("doc", "impl"):
                counted = {i for g in groups[dim] for i in g}
                missing = sorted(all_ids - counted)
                if missing:
                    quirks.append(f"{cat['id']} ({dim}): the workbook formula leaves {', '.join(missing)} out of the category average.")
                if groups[dim] != expected and not missing:
                    quirks.append(f"{cat['id']} ({dim}): the workbook averages {groups[dim]} instead of the subcategories {expected}.")
        functions.append({"id": fid, "name": sheet, "categories": categories})

    reqs = [q for f in functions for c in f["categories"] for s in c["subcategories"] for q in s["requirements"]]
    return {
        "framework": "CyberFundamentals (CyFun) 2025",
        "level": level,
        "owner": "Centre for Cybersecurity Belgium (CCB)",
        "source": {"workbook": path.name, "tool_version": tool_version, "framework_version": framework_version, "website": "https://www.cyfun.eu"},
        "thresholds": {
            "key_measure_min": km_min,
            "category_min": cat_min,
            "total_min": total_min,
            "na_allowed": na_allowed,
            "na_value": na_value,
            "na_excludes": na_excludes,
            "scale_min": 1,
            "scale_max": 5,
        },
        "layout": {
            "level_col": col_letter(cols["level"]) if cols["level"] else None,
            "req_col": col_letter(cols["req"]),
            "doc_col": doc_l,
            "impl_col": impl_l,
            "comment_col": col_letter(cols["comment"]),
            "date_cell": date_cell,
            "intro_sheet": "Introduction",
            "summary_sheet": summary_sheet,
            "function_sheets": [s for _, s in FUNCTIONS],
        },
        "maturity_levels": maturity,
        "counts": {
            "functions": len(functions),
            "categories": sum(len(f["categories"]) for f in functions),
            "subcategories": sum(len(c["subcategories"]) for f in functions for c in f["categories"]),
            "requirements": len(reqs),
            "key_measures": sum(1 for q in reqs if q["key_measure"]),
            "management_aspects": sum(1 for q in reqs if q["management_aspect"]),
            "by_level": {lv: sum(1 for q in reqs if q["level"].upper() == lv) for lv in LEVELS},
        },
        "key_measures": [q["id"] for q in reqs if q["key_measure"]],
        "quirks": quirks,
        "functions": functions,
    }


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1].upper() not in LEVELS:
        raise SystemExit(__doc__)
    level = sys.argv[1].upper()
    src = Path(sys.argv[2])
    out = Path(__file__).resolve().parents[1] / "app" / "cyfun" / "framework" / f"{level.lower()}_2025.json"
    data = build(level, src)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    c = data["counts"]
    t = data["thresholds"]
    print(f"wrote {out}")
    print(f"{level}: requirements={c['requirements']} by_level={c['by_level']} key_measures={c['key_measures']} management_aspects={c['management_aspects']}")
    print(
        f"thresholds: km>={t['key_measure_min']} category>={t['category_min']} total>={t['total_min']} na_allowed={t['na_allowed']} na_value={t['na_value']} na_excludes={t['na_excludes']}"
    )
    print(f"layout: {data['layout']}  tool_version={data['source']['tool_version']}")
    for q in data["quirks"]:
        print("quirk:", q)


if __name__ == "__main__":
    main()
