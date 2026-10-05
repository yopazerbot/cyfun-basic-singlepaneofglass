"""Maturity scoring exactly as the official CCB BASIC self-assessment workbook computes it.

Per requirement: a documentation score and an implementation score, each 1..5.
If a requirement is marked not applicable (allowed once at BASIC, never on a key
measure), both dimensions count as 2.5.
Subcategory score = average of its requirements (per dimension).
Category score = average of its subcategories (per dimension).
Category maturity = average of the two dimensions.
Total maturity = average of the 17 category maturities.
Key measure maturity = average of the two dimensions of that requirement.
BASIC passes when total maturity >= 2.5 and every key measure maturity >= 2.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .framework import Framework, Requirement

TARGET = 2.5


@dataclass
class ReqInput:
    doc: int | None = None
    impl: int | None = None
    not_applicable: bool = False


@dataclass
class ReqResult:
    requirement: Requirement
    doc: float | None
    impl: float | None
    not_applicable: bool
    scored: bool

    @property
    def maturity(self) -> float | None:
        if self.doc is None or self.impl is None:
            return None
        return (self.doc + self.impl) / 2

    @property
    def meets_target(self) -> bool:
        return self.maturity is not None and self.maturity >= TARGET


@dataclass
class GroupResult:
    id: str
    name: str
    doc: float | None
    impl: float | None

    @property
    def maturity(self) -> float | None:
        if self.doc is None or self.impl is None:
            return None
        return (self.doc + self.impl) / 2


@dataclass
class Summary:
    requirements: dict[str, ReqResult]
    subcategories: dict[str, GroupResult]
    categories: dict[str, GroupResult]
    functions: dict[str, GroupResult]
    total_maturity: float | None
    key_measures: list[ReqResult]
    scored_count: int
    total_count: int
    na_count: int
    na_ids: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def key_measures_failing(self) -> list[ReqResult]:
        return [k for k in self.key_measures if not k.meets_target]

    @property
    def key_measures_passing(self) -> int:
        return sum(1 for k in self.key_measures if k.meets_target)

    @property
    def complete(self) -> bool:
        return self.scored_count == self.total_count

    @property
    def total_meets_target(self) -> bool:
        return self.total_maturity is not None and self.total_maturity >= TARGET

    @property
    def passes(self) -> bool:
        return self.complete and self.total_meets_target and not self.key_measures_failing and not self.problems

    @property
    def below_target(self) -> list[ReqResult]:
        """Requirements whose own maturity is under 2.5 (remediation focus)."""
        return [r for r in self.requirements.values() if r.maturity is not None and r.maturity < TARGET]


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals or len(vals) != len(values):
        return None
    return sum(vals) / len(vals)


def validate_input(req: Requirement, inp: ReqInput, thresholds: dict) -> list[str]:
    errors: list[str] = []
    lo, hi = thresholds["scale_min"], thresholds["scale_max"]
    if inp.not_applicable and req.key_measure:
        errors.append(f"{req.id} is a key measure and cannot be marked not applicable.")
    for label, v in (("documentation", inp.doc), ("implementation", inp.impl)):
        if v is not None and not (lo <= v <= hi):
            errors.append(f"{req.id}: {label} score must be between {lo} and {hi}.")
    return errors


def compute(fw: Framework, inputs: dict[str, ReqInput]) -> Summary:
    na_value = fw.thresholds["na_value"]
    req_results: dict[str, ReqResult] = {}
    na_ids: list[str] = []
    problems: list[str] = []

    for r in fw.requirements:
        inp = inputs.get(r.id, ReqInput())
        if inp.not_applicable:
            if r.key_measure:
                problems.append(f"Key measure {r.id} is marked not applicable; this is not permitted.")
            na_ids.append(r.id)
            req_results[r.id] = ReqResult(r, na_value, na_value, True, True)
        else:
            scored = inp.doc is not None and inp.impl is not None
            req_results[r.id] = ReqResult(
                r,
                float(inp.doc) if inp.doc is not None else None,
                float(inp.impl) if inp.impl is not None else None,
                False,
                scored,
            )

    if len(na_ids) > fw.thresholds["na_allowed"]:
        joined = ", ".join(na_ids)
        problems.append(f"{len(na_ids)} requirements are marked not applicable; BASIC allows at most {fw.thresholds['na_allowed']} ({joined}).")

    subs: dict[str, GroupResult] = {}
    cats: dict[str, GroupResult] = {}
    funcs: dict[str, GroupResult] = {}
    for f in fw.functions:
        for c in f.categories:
            for s in c.subcategories:
                rr = [req_results[q.id] for q in s.requirements]
                subs[s.id] = GroupResult(s.id, s.title, _avg([x.doc for x in rr]), _avg([x.impl for x in rr]))
            ss = [subs[s.id] for s in c.subcategories]
            cats[c.id] = GroupResult(c.id, c.name, _avg([x.doc for x in ss]), _avg([x.impl for x in ss]))
        cc = [cats[c.id] for c in f.categories]
        funcs[f.id] = GroupResult(f.id, f.name, _avg([x.doc for x in cc]), _avg([x.impl for x in cc]))

    total = _avg([cats[c.id].maturity for c in fw.categories])
    kms = [req_results[k.id] for k in fw.key_measures]
    scored_count = sum(1 for x in req_results.values() if x.scored)
    return Summary(
        requirements=req_results,
        subcategories=subs,
        categories=cats,
        functions=funcs,
        total_maturity=total,
        key_measures=kms,
        scored_count=scored_count,
        total_count=len(fw.requirements),
        na_count=len(na_ids),
        na_ids=na_ids,
        problems=problems,
    )


def summary_to_dict(s: Summary) -> dict:
    """JSON-friendly form used for snapshots and exports."""
    return {
        "total_maturity": s.total_maturity,
        "passes": s.passes,
        "complete": s.complete,
        "scored": s.scored_count,
        "total": s.total_count,
        "not_applicable": s.na_ids,
        "problems": s.problems,
        "functions": {k: {"doc": v.doc, "impl": v.impl, "maturity": v.maturity} for k, v in s.functions.items()},
        "categories": {k: {"doc": v.doc, "impl": v.impl, "maturity": v.maturity} for k, v in s.categories.items()},
        "subcategories": {k: {"doc": v.doc, "impl": v.impl, "maturity": v.maturity} for k, v in s.subcategories.items()},
        "requirements": {
            k: {
                "doc": v.doc,
                "impl": v.impl,
                "maturity": v.maturity,
                "not_applicable": v.not_applicable,
                "key_measure": v.requirement.key_measure,
            }
            for k, v in s.requirements.items()
        },
        "key_measures": [{"id": k.requirement.id, "maturity": k.maturity, "passes": k.meets_target} for k in s.key_measures],
    }
