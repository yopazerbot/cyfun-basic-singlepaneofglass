"""Assurance levels: framework content, thresholds, N/A rules and switching the target level."""

import pytest

from cyfun.framework import LEVELS, all_requirement_ids, load_framework
from cyfun.scoring import ReqInput, compute, validate_input

EXPECTED = {
    "BASIC": {"requirements": 34, "key_measures": 13, "km_min": 2.5, "cat_min": None, "total_min": 2.5, "na_allowed": 1, "na_value": 2.5},
    "IMPORTANT": {"requirements": 133, "key_measures": 22, "km_min": 3.0, "cat_min": None, "total_min": 3.0, "na_allowed": 3, "na_value": 3.0},
    "ESSENTIAL": {"requirements": 218, "key_measures": 29, "km_min": 3.0, "cat_min": 3.0, "total_min": 3.5, "na_allowed": 5, "na_value": 3.0},
}


@pytest.mark.parametrize("level", LEVELS)
def test_framework_counts_and_thresholds(level):
    fw = load_framework(level)
    e = EXPECTED[level]
    t = fw.thresholds
    assert len(fw.requirements) == e["requirements"]
    assert len(fw.key_measures) == e["key_measures"]
    assert t["key_measure_min"] == e["km_min"]
    assert t.get("category_min") == e["cat_min"]
    assert t["total_min"] == e["total_min"]
    assert t["na_allowed"] == e["na_allowed"]
    assert t["na_value"] == e["na_value"]
    assert fw.layout["req_col"] and fw.layout["date_cell"]
    assert len({r.id for r in fw.requirements}) == len(fw.requirements)


def test_levels_nest():
    basic = {r.id for r in load_framework("BASIC").requirements}
    important = {r.id for r in load_framework("IMPORTANT").requirements}
    essential = {r.id for r in load_framework("ESSENTIAL").requirements}
    assert basic < important < essential
    assert set(all_requirement_ids()) == essential
    imp = load_framework("IMPORTANT")
    assert imp.count_by_level() == {"Basic": 34, "Important": 99}
    ess = load_framework("ESSENTIAL")
    assert ess.count_by_level() == {"Basic": 34, "Important": 99, "Essential": 85}
    assert ess.thresholds["na_excludes"] == ["key_measure", "management_aspect"]
    assert len(ess.management_aspects) == 16


def test_goals_cover_nearly_all_requirements():
    ess = load_framework("ESSENTIAL")
    missing = [r.id for r in ess.requirements if not r.goal and not r.guidance]
    assert len(missing) <= 10, missing


def test_important_thresholds_and_na_value():
    fw = load_framework("IMPORTANT")
    inputs = {r.id: ReqInput(3, 3) for r in fw.requirements}
    s = compute(fw, inputs)
    assert s.passes and s.total_maturity == 3.0
    inputs[fw.key_measures[0].id] = ReqInput(2, 3)
    s = compute(fw, inputs)
    assert not s.passes and [k.requirement.id for k in s.key_measures_failing] == [fw.key_measures[0].id]
    # three N/A allowed, each counting 3; a fourth is a problem
    candidates = [r for r in fw.requirements if not r.key_measure][:4]
    inputs = {r.id: ReqInput(3, 3) for r in fw.requirements}
    for r in candidates[:3]:
        inputs[r.id] = ReqInput(None, None, True)
    s = compute(fw, inputs)
    assert s.problems == [] and s.requirements[candidates[0].id].doc == 3.0
    inputs[candidates[3].id] = ReqInput(None, None, True)
    s = compute(fw, inputs)
    assert any("at most 3" in p for p in s.problems)


def test_essential_category_threshold_and_management_aspects():
    fw = load_framework("ESSENTIAL")
    inputs = {r.id: ReqInput(4, 4) for r in fw.requirements}
    s = compute(fw, inputs)
    assert s.passes and s.total_maturity == 4.0 and s.cat_min == 3.0
    # one category dragged under 3 fails the level even if the total stays above 3,5
    cat = fw.categories[0]
    for r in cat.requirements:
        inputs[r.id] = ReqInput(1, 1)
    s = compute(fw, inputs)
    assert s.total_maturity is not None and s.total_maturity >= 3.5
    assert [c.id for c in s.categories_failing] == [cat.id]
    assert not s.passes
    # management-aspect controls cannot be N/A at ESSENTIAL
    ma = next(r for r in fw.management_aspects if not r.key_measure)
    assert validate_input(ma, ReqInput(None, None, True), fw.thresholds)
    assert validate_input(ma, ReqInput(None, None, True), load_framework("IMPORTANT").thresholds) == [] if ma.id in load_framework("IMPORTANT").by_id else True


def test_switching_target_level_in_the_app(admin):
    r = admin.post("/journey/organisation", data={"name": "Example BV", "target_level": "ESSENTIAL"}, follow_redirects=False)
    assert r.status_code == 303 and "ESSENTIAL" in r.headers["location"]
    page = admin.get("/assessment").text
    assert "218 requirements" in page and "29 key measures" in page
    assert "categories ≥ 3,00" in page
    assert admin.get("/assessment/GV.SC-05.2").status_code == 200  # ESSENTIAL-only requirement
    assert admin.get("/assessment?level=Essential").status_code == 200
    # management aspect cannot be marked N/A at ESSENTIAL
    fw = load_framework("ESSENTIAL")
    ma = next(x for x in fw.management_aspects if not x.key_measure)
    r = admin.post(f"/assessment/{ma.id}/score", data={"not_applicable": "1", "justification": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    export = admin.get("/audit/export").text
    assert "Self-Assessment tool ESSENTIAL" in export
    # back to BASIC: ESSENTIAL-only requirements are no longer addressable
    r = admin.post("/journey/organisation", data={"name": "Example BV", "target_level": "BASIC"}, follow_redirects=False)
    assert r.status_code == 303
    assert admin.get("/assessment/GV.SC-05.2", follow_redirects=False).status_code == 303
    assert "34 requirements" in admin.get("/assessment").text
