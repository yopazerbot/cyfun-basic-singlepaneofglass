"""Maturity scoring exactly as the official CCB self-assessment workbooks compute it.

Per requirement: a documentation score and an implementation score, each 1..5.
A requirement marked not applicable counts the level's N/A value on both dimensions
(2,5 at BASIC, 3 at IMPORTANT and ESSENTIAL). Each level allows a maximum number of
N/A (1, 3, 5); key measures can never be N/A and, at ESSENTIAL, neither can controls
linked to management aspects.

Category score (per dimension) = average of the workbook's aggregation groups, each group
being the average of its requirements. In the normal case one group is one subcategory;
where a CCB workbook formula deviates from that (see the "quirks" in the framework JSON),
the groups follow the workbook so the numbers match the submitted file.
Category maturity = average of the two dimensions.
Total maturity = average of the category maturities.

Thresholds (from the Maturity Levels sheet of each workbook):
    BASIC      key measures >= 2,5                       total >= 2,5
    IMPORTANT  key measures >= 3                         total >= 3
    ESSENTIAL  key measures >= 3   each category >= 3    total >= 3,5
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .framework import Category, Framework, Requirement


def _maturity(doc: float | None, impl: float | None) -> float | None:
    if doc is None or impl is None:
        return None
    return (doc + impl) / 2


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
        return _maturity(self.doc, self.impl)

    def meets(self, target: float) -> bool:
        return self.maturity is not None and self.maturity >= target


@dataclass
class GroupResult:
    id: str
    name: str
    doc: float | None
    impl: float | None

    @property
    def maturity(self) -> float | None:
        return _maturity(self.doc, self.impl)


@dataclass
class Summary:
    level: str
    target: float  # requirement-level focus target (= total threshold)
    km_min: float
    cat_min: float | None
    total_min: float
    requirements: dict[str, ReqResult]
    subcategories: dict[str, GroupResult]
    categories: dict[str, GroupResult]
    functions: dict[str, GroupResult]
    total_maturity: float | None
    key_measures: list[ReqResult]
    scored_count: int
    total_count: int
    na_count: int
    na_allowed: int
    na_ids: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # Provisional values over the requirements scored so far (None where nothing is scored yet).
    # Shown while the assessment is incomplete; never used for the pass/fail decision.
    partial_categories: dict[str, GroupResult] = field(default_factory=dict)
    partial_functions: dict[str, GroupResult] = field(default_factory=dict)
    provisional_total: float | None = None

    def category_value(self, category_id: str) -> tuple[float | None, bool]:
        """(value, provisional) for display: the exact value when complete, else the partial one."""
        return _display_value(self.categories[category_id], self.partial_categories.get(category_id))

    def function_value(self, function_id: str) -> tuple[float | None, bool]:
        return _display_value(self.functions[function_id], self.partial_functions.get(function_id))

    @property
    def key_measures_failing(self) -> list[ReqResult]:
        return [k for k in self.key_measures if not k.meets(self.km_min)]

    @property
    def key_measures_passing(self) -> int:
        return sum(1 for k in self.key_measures if k.meets(self.km_min))

    @property
    def categories_failing(self) -> list[GroupResult]:
        if self.cat_min is None:
            return []
        return [c for c in self.categories.values() if c.maturity is None or c.maturity < self.cat_min]

    @property
    def complete(self) -> bool:
        return self.scored_count == self.total_count

    @property
    def total_meets_target(self) -> bool:
        return self.total_maturity is not None and self.total_maturity >= self.total_min

    @property
    def passes(self) -> bool:
        return self.complete and self.total_meets_target and not self.key_measures_failing and not self.categories_failing and not self.problems

    @property
    def below_target(self) -> list[ReqResult]:
        """Requirements whose own maturity is under the level's total threshold (remediation focus)."""
        return [r for r in self.requirements.values() if r.maturity is not None and r.maturity < self.target]


def _display_value(exact: GroupResult, partial: GroupResult | None) -> tuple[float | None, bool]:
    if exact.maturity is not None:
        return exact.maturity, False
    return (partial.maturity if partial else None), True


def _avg(values: list[float | None]) -> float | None:
    """Average, or None when the list is empty or any value is missing."""
    if not values or any(v is None for v in values):
        return None
    return sum(values) / len(values)


def _avg_any(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def na_blocked_reason(req: Requirement, thresholds: dict) -> str | None:
    excludes = thresholds.get("na_excludes", ["key_measure"])
    if req.key_measure and "key_measure" in excludes:
        return "key measure"
    if req.management_aspect and "management_aspect" in excludes:
        return "control linked to management aspects"
    return None


def validate_input(req: Requirement, inp: ReqInput, thresholds: dict) -> list[str]:
    errors: list[str] = []
    lo, hi = thresholds["scale_min"], thresholds["scale_max"]
    if inp.not_applicable:
        reason = na_blocked_reason(req, thresholds)
        if reason:
            errors.append(f"{req.id} is a {reason} and cannot be marked not applicable.")
    for label, v in (("documentation", inp.doc), ("implementation", inp.impl)):
        if v is not None and not (lo <= v <= hi):
            errors.append(f"{req.id}: {label} score must be between {lo} and {hi}.")
    return errors


def _as_float(value: int | None) -> float | None:
    return float(value) if value is not None else None


def _category_dimension(cat: Category, dimension: str, results: dict[str, ReqResult], partial: bool = False) -> float | None:
    agg = _avg_any if partial else _avg
    groups = []
    for group in cat.groups(dimension):
        values = [results[i] for i in group if i in results]
        if partial:
            values = [v for v in values if v.scored]
        groups.append(agg([getattr(v, dimension) for v in values]) if values else None)
    return agg(groups)


def compute(fw: Framework, inputs: dict[str, ReqInput]) -> Summary:
    t = fw.thresholds
    na_value = t["na_value"]
    req_results: dict[str, ReqResult] = {}
    na_ids: list[str] = []
    problems: list[str] = []

    for r in fw.requirements:
        inp = inputs.get(r.id, ReqInput())
        if inp.not_applicable:
            reason = na_blocked_reason(r, t)
            if reason:
                problems.append(f"{r.id} is a {reason} and is marked not applicable; this is not permitted at {fw.level}.")
            na_ids.append(r.id)
            req_results[r.id] = ReqResult(r, na_value, na_value, True, True)
        else:
            scored = inp.doc is not None and inp.impl is not None
            req_results[r.id] = ReqResult(r, _as_float(inp.doc), _as_float(inp.impl), False, scored)

    if len(na_ids) > t["na_allowed"]:
        joined = ", ".join(na_ids)
        problems.append(f"{len(na_ids)} requirements are marked not applicable; {fw.level} allows at most {t['na_allowed']} ({joined}).")

    # Exact values decide pass/fail; the partial (provisional) ones cover only what is scored so far.
    subs: dict[str, GroupResult] = {}
    cats: dict[str, GroupResult] = {}
    funcs: dict[str, GroupResult] = {}
    pcats: dict[str, GroupResult] = {}
    pfuncs: dict[str, GroupResult] = {}
    for f in fw.functions:
        for c in f.categories:
            for s in c.subcategories:
                rr = [req_results[q.id] for q in s.requirements]
                subs[s.id] = GroupResult(s.id, s.title, _avg([x.doc for x in rr]), _avg([x.impl for x in rr]))
            cats[c.id] = GroupResult(c.id, c.name, _category_dimension(c, "doc", req_results), _category_dimension(c, "impl", req_results))
            pd = _category_dimension(c, "doc", req_results, partial=True)
            pi = _category_dimension(c, "impl", req_results, partial=True)
            pcats[c.id] = GroupResult(c.id, c.name, pd, pi)
        cc = [cats[c.id] for c in f.categories]
        funcs[f.id] = GroupResult(f.id, f.name, _avg([x.doc for x in cc]), _avg([x.impl for x in cc]))
        pc = [pcats[c.id] for c in f.categories if pcats[c.id].maturity is not None]
        pfuncs[f.id] = GroupResult(f.id, f.name, _avg_any([x.doc for x in pc]), _avg_any([x.impl for x in pc]))

    return Summary(
        level=fw.level,
        target=float(t["total_min"]),
        km_min=float(t["key_measure_min"]),
        cat_min=float(t["category_min"]) if t.get("category_min") is not None else None,
        total_min=float(t["total_min"]),
        requirements=req_results,
        subcategories=subs,
        categories=cats,
        functions=funcs,
        total_maturity=_avg([cats[c.id].maturity for c in fw.categories]),
        key_measures=[req_results[k.id] for k in fw.key_measures],
        scored_count=sum(1 for x in req_results.values() if x.scored),
        total_count=len(fw.requirements),
        na_count=len(na_ids),
        na_allowed=int(t["na_allowed"]),
        na_ids=na_ids,
        problems=problems,
        partial_categories=pcats,
        partial_functions=pfuncs,
        provisional_total=_avg_any([pcats[c.id].maturity for c in fw.categories]),
    )


def _groups_to_dict(groups: dict[str, GroupResult]) -> dict:
    return {k: {"doc": v.doc, "impl": v.impl, "maturity": v.maturity} for k, v in groups.items()}


def summary_to_dict(s: Summary) -> dict:
    """JSON-friendly form used for snapshots and exports."""
    return {
        "level": s.level,
        "thresholds": {"key_measure_min": s.km_min, "category_min": s.cat_min, "total_min": s.total_min, "na_allowed": s.na_allowed},
        "total_maturity": s.total_maturity,
        "provisional_total": s.provisional_total,
        "passes": s.passes,
        "complete": s.complete,
        "scored": s.scored_count,
        "total": s.total_count,
        "not_applicable": s.na_ids,
        "problems": s.problems,
        "categories_failing": [c.id for c in s.categories_failing],
        "functions": _groups_to_dict(s.functions),
        "categories": _groups_to_dict(s.categories),
        "subcategories": _groups_to_dict(s.subcategories),
        "requirements": {
            k: {
                "doc": v.doc,
                "impl": v.impl,
                "maturity": v.maturity,
                "not_applicable": v.not_applicable,
                "key_measure": v.requirement.key_measure,
                "level": v.requirement.level,
            }
            for k, v in s.requirements.items()
        },
        "key_measures": [{"id": k.requirement.id, "maturity": k.maturity, "passes": k.meets(s.km_min)} for k in s.key_measures],
    }
