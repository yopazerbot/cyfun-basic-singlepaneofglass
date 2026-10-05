"""Build app/cyfun/framework/basic_2025.json from the official CCB self-assessment workbook.

Usage:
    python scripts/build_framework.py "path/to/CyFun2025_ Self-Assessment_tool_BASIC_v2026_02_20.xlsx"

The official workbook is published by the Centre for Cybersecurity Belgium (CCB) on
https://www.cyfun.eu and is not redistributed in this repository. This script reads the
function sheets (GOVERN, IDENTIFY, PROTECT, DETECT, RESPOND, RECOVER) and the
'Maturity Levels' sheet, and writes a JSON file that the application loads at start.

Nothing in the output is typed in by hand: requirement text, category descriptions,
key-measure flags and the row positions used for the workbook export all come from the
workbook. Implementation guidance summaries live in guidance_basic_2025.json and are
merged at runtime.
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

import openpyxl

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

FUNCTIONS = [
    ("GV", "GOVERN"),
    ("ID", "IDENTIFY"),
    ("PR", "PROTECT"),
    ("DE", "DETECT"),
    ("RS", "RESPOND"),
    ("RC", "RECOVER"),
]

# Workbook labels that deviate from the canonical NIST-style identifier.
ID_FIXES = {
    "ID.AM-5.1": "ID.AM-05.1",
    "DE.CM-03-1": "DE.CM-03.1",
}

CAT_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<id>[A-Z]{2}\.[A-Z]{2})\)\s*:?\s*(?P<desc>.*)$", re.S)
SUB_RE = re.compile(r"^(?P<id>[A-Z]{2}\.[A-Z]{2}-\d{2})\s*:\s*(?P<title>.+)$", re.S)
REQ_RE = re.compile(r"^(?P<id>[A-Z]{2}\.[A-Z]{2}-\d{1,2}[.-]\d)\s*:\s*(?P<text>.+)$", re.S)


def clean(s: str | None) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def build(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    intro = wb["Introduction"]
    tool_version = intro["Q6"].value
    framework_version = intro["T18"].value
    if isinstance(tool_version, datetime):
        tool_version = tool_version.date().isoformat()
    if isinstance(framework_version, datetime):
        framework_version = framework_version.date().isoformat()

    maturity = []
    ml = wb["Maturity Levels"]
    for r in range(2, 7):
        maturity.append(
            {
                "value": int(ml.cell(r, 2).value),
                "name": clean(ml.cell(r, 1).value),
                "documentation": clean(ml.cell(r, 3).value),
                "implementation": clean(ml.cell(r, 4).value),
            }
        )

    functions = []
    for fid, sheet in FUNCTIONS:
        ws = wb[sheet]
        categories: list[dict] = []
        cat = sub = None
        for r in range(3, ws.max_row + 1):
            a, c, d, e = (ws.cell(r, col).value for col in (1, 3, 4, 5))
            if a:
                m = CAT_RE.match(clean(a))
                if not m:
                    raise SystemExit(f"{sheet}!A{r}: unexpected category text: {a!r}")
                cat = {
                    "id": m["id"],
                    "name": m["name"].strip(),
                    "description": m["desc"].strip(),
                    "subcategories": [],
                }
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
                    raise SystemExit(f"{sheet}!E{r}: unexpected requirement text: {e!r}")
                raw_id = m["id"]
                sub["requirements"].append(
                    {
                        "id": ID_FIXES.get(raw_id, raw_id),
                        "workbook_id": raw_id,
                        "text": m["text"].strip(),
                        "key_measure": clean(c) == "Key Measure",
                        "sheet": sheet,
                        "row": r,
                    }
                )
        functions.append({"id": fid, "name": sheet, "categories": categories})

    reqs = [q for f in functions for c in f["categories"] for s in c["subcategories"] for q in s["requirements"]]
    kms = [q["id"] for q in reqs if q["key_measure"]]
    return {
        "framework": "CyberFundamentals (CyFun) 2025",
        "level": "BASIC",
        "owner": "Centre for Cybersecurity Belgium (CCB)",
        "source": {
            "workbook": path.name,
            "tool_version": tool_version,
            "framework_version": framework_version,
            "website": "https://www.cyfun.eu",
        },
        "thresholds": {
            "key_measure_min": 2.5,
            "total_min": 2.5,
            "na_allowed": 1,
            "na_value": 2.5,
            "scale_min": 1,
            "scale_max": 5,
        },
        "maturity_levels": maturity,
        "counts": {
            "functions": len(functions),
            "categories": sum(len(f["categories"]) for f in functions),
            "subcategories": sum(len(c["subcategories"]) for f in functions for c in f["categories"]),
            "requirements": len(reqs),
            "key_measures": len(kms),
        },
        "key_measures": kms,
        "functions": functions,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1])
    out = Path(__file__).resolve().parents[1] / "app" / "cyfun" / "framework" / "basic_2025.json"
    data = build(src)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    c = data["counts"]
    print(f"wrote {out}")
    print(
        f"functions={c['functions']} categories={c['categories']} subcategories={c['subcategories']} "
        f"requirements={c['requirements']} key_measures={c['key_measures']}"
    )


if __name__ == "__main__":
    main()
