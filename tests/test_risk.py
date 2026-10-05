import pytest

from cyfun import risk
from cyfun.framework import load_risk_model


def test_criteria_from_workbook():
    m = load_risk_model()
    assert m["criteria"]["probability"] == {"Low": 0.0, "Med": 0.5, "High": 1.0}
    assert m["criteria"]["impact"] == {"Low": 0.0, "Med": 5.0, "High": 10.0}
    assert [lv["level"] for lv in m["criteria"]["levels"]] == ["BASIC", "IMPORTANT", "ESSENTIAL"]
    assert len(m["sectors"]) == 16


@pytest.mark.parametrize("sector_id", [s["id"] for s in load_risk_model()["sectors"]])
def test_sector_defaults_reproduce_workbook(sector_id):
    m = load_risk_model()
    sec = risk.sector(m, sector_id)
    res = risk.compute(m, risk.default_matrix(m, sector_id), sec["default_size"])
    assert res.total == sec["workbook_total"]
    assert res.level == sec["workbook_level"]


def test_small_organisation_lowers_level():
    m = load_risk_model()
    matrix = risk.default_matrix(m, "II.6_Digital providers")
    assert risk.compute(m, matrix, 3).level == "IMPORTANT"
    assert risk.compute(m, matrix, 1).total == 55.0
    assert risk.compute(m, matrix, 1).level == "BASIC"


def test_validate_matrix():
    m = load_risk_model()
    good = risk.default_matrix(m, "II.2_Waste Mgmt")
    assert risk.validate_matrix(good) == []
    bad = risk.default_matrix(m, "II.2_Waste Mgmt")
    bad["rows"][0]["impact"] = "Huge"
    bad["rows"][1]["probability"] = ["Low"]
    assert len(risk.validate_matrix(bad)) == 2
