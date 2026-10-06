"""Extract the goal statement of every requirement from the CCB CyFun 2025 booklets.

Usage:
    python scripts/build_goals.py path/to/CyFun2025_Booklet_BASIC_E.pdf path/to/CyFun2025_Booklet_IMPORTANT_E.pdf path/to/CyFun2025_Booklet_ESSENTIAL_E.pdf

Each requirement in the booklets is followed by an "Implementation guidance" block that opens
with one or two sentences stating the goal of the control. Only that opening statement is
extracted (a short quotation, reproduced with acknowledgement of the CCB as source); the
detailed guidance stays in the booklets. Output: app/cyfun/framework/goals_2025.json
mapping requirement id to {"goal": text, "source": booklet file name}.

Requires pymupdf (pip install pymupdf). The booklets are not redistributed in this repository.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

REQ = re.compile(r"^\s*((?:GV|ID|PR|DE|RS|RC)\.[A-Z]{2}-\d{1,2}\.\d{1,2})\b")
STOP = re.compile(
    r"^(To |In order|The following|Consider|Organisations should|Organisations are|This can be|Possible|For (this|that|the)|Key (elements|aspects|practices)|Examples?)",
    re.I,
)


def normalise(rid: str) -> str:
    cat, a, b = re.fullmatch(r"([A-Z]{2}\.[A-Z]{2})-(\d{1,2})\.(\d{1,2})", rid).groups()
    return f"{cat}-{int(a):02d}.{b}"


def extract(path: Path) -> dict[str, str]:
    doc = fitz.open(path)
    lines = [ln.rstrip() for page in doc for ln in page.get_text().split("\n")]
    lines = [ln for ln in lines if not re.fullmatch(r"\s*[A-Z]\s*", ln)]  # vertical sidebar letters
    goals: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = REQ.match(lines[i])
        if not m:
            i += 1
            continue
        rid = normalise(m.group(1))
        end = min(i + 14, len(lines))
        found = next((j for j in range(i + 1, end) if lines[j].strip().lower().startswith("implementation guidance")), None)
        if found is not None:
            k = found + 1
            para: list[str] = []
            while k < len(lines) and k < found + 12:
                s = lines[k].strip()
                if not s:
                    k += 1
                    continue
                if s.startswith(("·", "•")) or (para and STOP.match(s)) or REQ.match(s):
                    break
                para.append(s)
                k += 1
            goal = re.sub(r"-\s+", "", " ".join(para))
            goal = re.sub(r"\s+", " ", goal).strip()
            if goal and rid not in goals:
                goals[rid] = goal
        i = end if found is None else found
    return goals


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    merged: dict[str, dict] = {}
    for arg in sys.argv[1:]:
        p = Path(arg)
        goals = extract(p)
        print(f"{p.name}: {len(goals)} goal statements")
        for rid, goal in goals.items():
            merged.setdefault(rid, {"goal": goal, "source": p.name})
    out = Path(__file__).resolve().parents[1] / "app" / "cyfun" / "framework" / "goals_2025.json"
    out.write_text(json.dumps(dict(sorted(merged.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(merged)} requirements")


if __name__ == "__main__":
    main()
