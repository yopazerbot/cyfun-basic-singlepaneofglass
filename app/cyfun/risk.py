"""CCB 'CyFun-Selection' risk assessment: which assurance level applies.

score = sum over 5 attack categories x 5 threat actors of
        probability(Low 0 / Med 0.5 / High 1) * impact(Low 0 / Med 5 / High 10)
        * attack type (Global 1 / Targeted 2) * organisation size (Small 1 / Medium 2 / Large 3)
level:  0..99 BASIC, 100..199 IMPORTANT, 200+ ESSENTIAL
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("Low", "Med", "High")
SIZES = {1: "Small", 2: "Medium", 3: "Large"}


@dataclass
class RiskResult:
    total: float
    level: str
    per_actor: list[float]
    per_row: list[float]
    cells: list[list[float]]


def sector(model: dict, sector_id: str) -> dict:
    found = next((s for s in model["sectors"] if s["id"] == sector_id), None)
    return found or model["sectors"][0]


def default_matrix(model: dict, sector_id: str) -> dict:
    s = sector(model, sector_id)
    return {
        "rows": [
            {
                "attack": r["attack"],
                "attack_type": r["attack_type"],
                "impact": r["impact"],
                "probability": list(r["probability"]),
            }
            for r in s["rows"]
        ]
    }


def level_for(model: dict, total: float) -> str:
    for lv in model["criteria"]["levels"]:
        if lv["min"] <= total <= lv["max"]:
            return lv["level"]
    return model["criteria"]["levels"][-1]["level"]


def validate_matrix(matrix: dict) -> list[str]:
    errors: list[str] = []
    rows = matrix.get("rows") or []
    if len(rows) != 5:
        errors.append("The matrix must have 5 attack categories.")
    for i, row in enumerate(rows, start=1):
        if row.get("impact") not in LEVELS:
            errors.append(f"Row {i}: impact must be Low, Med or High.")
        probs = row.get("probability") or []
        if len(probs) != 5 or any(p not in LEVELS for p in probs):
            errors.append(f"Row {i}: five probabilities (Low, Med, High) are required.")
        if row.get("attack_type") not in (1, 2):
            errors.append(f"Row {i}: attack type must be 1 (global) or 2 (targeted).")
    return errors


def compute(model: dict, matrix: dict, organisation_size: int) -> RiskResult:
    crit = model["criteria"]
    prob = crit["probability"]
    imp = crit["impact"]
    cells: list[list[float]] = []
    per_row: list[float] = []
    n_actors = len(matrix["rows"][0]["probability"]) if matrix["rows"] else 0
    per_actor = [0.0] * n_actors
    for row in matrix["rows"]:
        impact = imp[row["impact"]]
        atype = row["attack_type"]
        vals = []
        for i, p in enumerate(row["probability"]):
            v = prob[p] * impact * atype * organisation_size
            vals.append(v)
            per_actor[i] += v
        cells.append(vals)
        per_row.append(sum(vals))
    total = sum(per_row)
    return RiskResult(total=total, level=level_for(model, total), per_actor=per_actor, per_row=per_row, cells=cells)
