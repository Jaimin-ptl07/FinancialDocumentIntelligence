"""Pure validation + factsheet orchestration."""

from fdi.core.factsheet import build_factsheet
from fdi.core.validation import validate


def _v(**kw):
    return {t: {"value": val, "confidence": c} for t, (val, c) in kw.items()}


def test_reconciliation_pass_within_tolerance():
    vals = _v(TOTCAR=(100.0, 0.9), TOTA=(100.4, 0.9))
    [r] = [x for x in validate(vals, tolerance=1.0) if x["rule_id"] == "recon_current_assets"]
    assert r["passed"] is True
    assert r["kind"] == "reconciliation"
    assert r["severity"] == "error"


def test_reconciliation_fail_outside_tolerance_reports_delta():
    vals = _v(TOTCAR=(100.0, 0.9), TOTA=(90.0, 0.9))
    [r] = [x for x in validate(vals) if x["rule_id"] == "recon_current_assets"]
    assert r["passed"] is False
    assert r["delta"] == 10.0
    assert r["expected"] == 100.0 and r["actual"] == 90.0


def test_identity_check_present_when_tags_present():
    vals = _v(TOTNCNCA=(300.0, 0.9), TOTL=(180.0, 0.9), TOTEQNR=(120.0, 0.9))
    [r] = [x for x in validate(vals) if x["rule_id"] == "identity_balance_sheet"]
    assert r["passed"] is True  # 300 == 180 + 120
    assert r["severity"] == "warn"


def test_negative_total_flagged():
    vals = _v(TOTL=(-5.0, 0.9))
    dom = [x for x in validate(vals) if x["kind"] == "domain"]
    assert any(x["tags"] == ["TOTL"] and not x["passed"] for x in dom)


def test_missing_inputs_skip_rule_no_false_failure():
    assert [x for x in validate(_v(TOTCAR=(100.0, 0.9))) if x["kind"] == "reconciliation"] == []


def test_factsheet_merges_and_summarizes():
    statements = [{
        "sheet": "S", "statement": "profit_and_loss",
        "rows": [
            {"block_id": "PL01", "tag": "RFSP", "sr_no": "1.1", "particulars": "A",
             "origin": "extracted", "excluded": False, "is_section_header": False},
            {"block_id": "PL01", "tag": "OTOR", "sr_no": "1.5", "particulars": "B",
             "origin": "extracted", "excluded": False, "is_section_header": False},
            {"block_id": "PL01", "tag": "RFOGRS", "sr_no": None,
             "particulars": "Total (Total of 1.1 to 1.5)", "origin": "derived",
             "excluded": False, "is_section_header": False},
        ],
    }]
    extracted = _v(RFSP=(100.0, 0.9), OTOR=(20.0, 0.8))
    fs = build_factsheet(statements, extracted)
    # derived subtotal is computed and merged into facts with origin derived
    assert fs["facts"]["RFOGRS"]["value"] == 120.0
    assert fs["facts"]["RFOGRS"]["origin"] == "derived"
    assert fs["facts"]["RFSP"]["origin"] == "extracted"
    assert fs["summary"]["extracted"] == 2
    assert fs["summary"]["derived_ok"] == 1
    assert "ratios" in fs and "validations" in fs
