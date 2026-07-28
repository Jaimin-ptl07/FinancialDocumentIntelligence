"""Pure confidence calibration + triangulation (M3)."""

from fdi.core.confidence import calibrate, triangulate
from fdi.core.factsheet import build_factsheet


def _facts(**kw):
    return {t: {"value": v, "confidence": c, "origin": "extracted"} for t, (v, c) in kw.items()}


def test_reconciliation_pass_boosts_and_marks_high():
    facts = _facts(TOTCAR=(100.0, 0.8), TOTA=(100.0, 0.8))
    vals = [{"rule_id": "recon_current_assets", "kind": "reconciliation",
             "tags": ["TOTCAR", "TOTA"], "severity": "error", "passed": True, "delta": 0.0}]
    cal = calibrate(facts, vals)
    assert cal["TOTCAR"]["confidence"] == 0.9  # 0.8 + 0.10 boost
    assert cal["TOTCAR"]["band"] == "high"
    assert any("corroborated" in s for s in cal["TOTCAR"]["signals"])


def test_failed_error_check_forces_failed_band():
    facts = _facts(TOTCAR=(0.95, 0.95), TOTA=(0.95, 0.95))
    vals = [{"rule_id": "recon_current_assets", "kind": "reconciliation",
             "tags": ["TOTCAR", "TOTA"], "severity": "error", "passed": False, "delta": 12.0}]
    cal = calibrate(facts, vals)
    assert cal["TOTCAR"]["band"] == "failed"  # despite high base confidence
    assert any("failed check" in s for s in cal["TOTCAR"]["signals"])


def test_bands_by_threshold():
    facts = _facts(A=(0.9, 0.9), B=(0.7, 0.7), C=(0.3, 0.3))
    cal = calibrate(facts, [])
    assert cal["A"]["band"] == "high"
    assert cal["B"]["band"] == "needs_review"
    assert cal["C"]["band"] == "low"


def test_triangulate_agreement_boosts():
    cands = {"RFSP": [
        {"value": 100.0, "confidence": 0.8, "source": "face"},
        {"value": 100.3, "confidence": 0.7, "source": "note"},
    ]}
    out = triangulate(cands, tolerance=1.0)
    assert out["RFSP"]["agreement"] == "agree"
    assert out["RFSP"]["value"] == 100.0  # highest-confidence reading
    assert out["RFSP"]["confidence"] > 0.8


def test_triangulate_conflict_flags_review():
    cands = {"RFSP": [
        {"value": 100.0, "confidence": 0.8, "source": "face"},
        {"value": 200.0, "confidence": 0.7, "source": "note"},
    ]}
    out = triangulate(cands, tolerance=1.0)
    assert out["RFSP"]["agreement"] == "conflict"
    assert out["RFSP"]["band"] == "needs_review"
    assert out["RFSP"]["confidence"] < 0.8


def test_factsheet_attaches_bands_and_summary():
    statements = [{
        "sheet": "S", "statement": "assets",
        "rows": [
            {"block_id": "BS01", "tag": "TOTCAR", "sr_no": None, "particulars": "CA reported",
             "origin": "extracted", "excluded": False, "is_section_header": False},
            {"block_id": "BS01", "tag": "X41", "sr_no": "4.1", "particulars": "x",
             "origin": "extracted", "excluded": False, "is_section_header": False},
            {"block_id": "BS01", "tag": "TOTA", "sr_no": None,
             "particulars": "Total Current Assets (Total of 4.1 to 4.1)", "origin": "derived",
             "excluded": False, "is_section_header": False},
        ],
    }]
    extracted = {
        "TOTCAR": {"value": 50.0, "confidence": 0.8},
        "X41": {"value": 50.0, "confidence": 0.8},
    }
    fs = build_factsheet(statements, extracted)
    # TOTA derived = 50 (sum of X41); reconciles with TOTCAR=50 -> boost + high
    assert fs["facts"]["TOTA"]["value"] == 50.0
    assert "band" in fs["facts"]["TOTCAR"]
    assert fs["summary"]["bands"]["high"] >= 1
    assert set(fs["summary"]["bands"]) == {"high", "needs_review", "low", "failed"}
