from cyfun.framework import load_framework
from cyfun.scoring import ReqInput, compute, summary_to_dict, validate_input


def all_scores(fw, doc, impl):
    return {r.id: ReqInput(doc, impl) for r in fw.requirements}


def test_framework_counts():
    fw = load_framework()
    assert len(fw.functions) == 6
    assert len(fw.categories) == 17
    assert sum(len(c.subcategories) for c in fw.categories) == 28
    assert len(fw.requirements) == 34
    assert len(fw.key_measures) == 13
    assert [k.id for k in fw.key_measures] == fw.meta["key_measures"]


def test_every_requirement_has_guidance():
    fw = load_framework()
    missing = [r.id for r in fw.requirements if not r.guidance or not r.evidence_examples]
    assert missing == []


def test_uniform_scores():
    fw = load_framework()
    s = compute(fw, all_scores(fw, 1, 1))
    assert s.total_maturity == 1.0
    assert s.complete and not s.passes
    s = compute(fw, all_scores(fw, 3, 3))
    assert s.total_maturity == 3.0
    assert s.passes
    assert s.key_measures_passing == 13


def test_threshold_boundary():
    fw = load_framework()
    s = compute(fw, all_scores(fw, 2, 3))
    assert s.total_maturity == 2.5
    assert s.passes
    inputs = all_scores(fw, 2, 3)
    inputs["PR.AA-03.2"] = ReqInput(2, 2)  # key measure falls to 2.0
    s = compute(fw, inputs)
    assert not s.passes
    assert [k.requirement.id for k in s.key_measures_failing] == ["PR.AA-03.2"]


def test_category_averages_subcategories_not_requirements():
    """PR.AA has 4 subcategories with 1, 2, 4 and 1 requirements. The workbook averages
    subcategory scores, so a single-requirement subcategory weighs as much as PR.AA-05."""
    fw = load_framework()
    inputs = all_scores(fw, 1, 1)
    for rid in ("PR.AA-05.1", "PR.AA-05.2", "PR.AA-05.3", "PR.AA-05.4"):
        inputs[rid] = ReqInput(5, 5)
    s = compute(fw, inputs)
    assert s.subcategories["PR.AA-05"].doc == 5
    assert s.categories["PR.AA"].doc == (1 + 1 + 5 + 1) / 4


def test_not_applicable_counts_as_2_5_once():
    fw = load_framework()
    inputs = all_scores(fw, 1, 1)
    inputs["PR.AA-06.1"] = ReqInput(None, None, True)
    s = compute(fw, inputs)
    assert s.requirements["PR.AA-06.1"].doc == 2.5
    assert s.requirements["PR.AA-06.1"].impl == 2.5
    assert s.na_ids == ["PR.AA-06.1"]
    assert s.problems == []
    inputs["GV.OC-03.1"] = ReqInput(None, None, True)
    s = compute(fw, inputs)
    assert len(s.problems) == 1
    assert "at most 1" in s.problems[0]
    assert not s.passes


def test_key_measure_cannot_be_not_applicable():
    fw = load_framework()
    req = fw.get("PR.DS-11.1")
    assert validate_input(req, ReqInput(None, None, True), fw.thresholds)
    inputs = all_scores(fw, 3, 3)
    inputs["PR.DS-11.1"] = ReqInput(None, None, True)
    s = compute(fw, inputs)
    assert any("key measure" in p for p in s.problems)
    assert not s.passes


def test_incomplete_assessment_has_no_total():
    fw = load_framework()
    inputs = all_scores(fw, 3, 3)
    inputs.pop("RC.RP-01.1")
    s = compute(fw, inputs)
    assert s.total_maturity is None
    assert s.scored_count == 33
    assert not s.passes
    d = summary_to_dict(s)
    assert d["total_maturity"] is None and d["scored"] == 33


def test_validate_range():
    fw = load_framework()
    req = fw.get("GV.OC-03.1")
    assert validate_input(req, ReqInput(0, 3), fw.thresholds)
    assert validate_input(req, ReqInput(3, 6), fw.thresholds)
    assert validate_input(req, ReqInput(3, 3), fw.thresholds) == []


def test_provisional_values_while_incomplete():
    fw = load_framework()
    inputs = {r.id: ReqInput(3, 3) for r in fw.requirements[:10]}
    s = compute(fw, inputs)
    assert s.total_maturity is None and not s.passes
    assert s.provisional_total == 3.0
    first = fw.categories[0]
    assert s.category_value(first.id) == (3.0, False)  # GV.OC has one requirement, scored
    untouched = fw.categories[-1]
    assert s.category_value(untouched.id) == (None, True)
    full = compute(fw, {r.id: ReqInput(4, 2) for r in fw.requirements})
    assert full.provisional_total == full.total_maturity == 3.0
