"""Pure derivation of subtotal Facts from extracted values."""

import json
import pathlib

from fdi.core.derivation import derive_facts
from fdi.core.taxonomy import parse_taxonomy


def _row(tag, sr, particulars, origin="extracted", block="PL01"):
    return {
        "block_id": block, "tag": tag, "sr_no": sr, "particulars": particulars,
        "flag": None, "origin": origin, "is_new": False, "excluded": False,
        "is_section_header": False, "depth": None,
        "redirect_dataset2": None, "redirect_dataset1": None,
    }


_STATEMENTS = [{
    "sheet": "S", "statement": "profit_and_loss",
    "rows": [
        _row("RFSP", "1.1", "Revenue from Sale of Products"),
        _row("RFSS", "1.2", "Revenue from Sale of Services"),
        _row("OTOR", "1.5", "Other Operating Revenue"),
        _row("RFOGRS", None, "Total Gross Revenue (Total of 1.1 to 1.5)", origin="derived"),
    ],
}]


def _vals(**kw):
    return {t: {"value": v, "confidence": c} for t, (v, c) in kw.items()}


def test_sum_subtotal_computes_and_propagates_confidence():
    values = _vals(RFSP=(100.0, 0.9), RFSS=(50.0, 0.8), OTOR=(10.0, 0.95))
    out = derive_facts(_STATEMENTS, values)
    f = out["RFOGRS"]
    assert f["status"] == "ok"
    assert f["value"] == 160.0
    assert f["confidence"] == 0.8  # min of inputs
    assert set(f["derivation"]["inputs"]) == {"RFSP", "RFSS", "OTOR"}


def test_missing_input_yields_missing_status_not_a_guess():
    values = _vals(RFSP=(100.0, 0.9), RFSS=(50.0, 0.8))  # OTOR absent
    out = derive_facts(_STATEMENTS, values)
    assert out["RFOGRS"]["status"] == "missing_inputs"
    assert out["RFOGRS"]["value"] is None


def test_nested_subtotal_resolves_in_dependency_order():
    statements = [{
        "sheet": "S", "statement": "assets",
        "rows": [
            _row("A", "1.1", "A", block="BS01"),
            _row("B", "1.2", "B", block="BS01"),
            _row("SUB", "1", "Subtotal (Total of 1.1 to 1.2)", origin="derived", block="BS01"),
            _row("C", "2", "C", block="BS01"),
            _row("TOP", None, "Grand total (Total of 1 to 2)", origin="derived", block="BS01"),
        ],
    }]
    values = _vals(A=(10.0, 0.9), B=(20.0, 0.9), C=(5.0, 0.9))
    out = derive_facts(statements, values)
    assert out["SUB"]["value"] == 30.0
    assert out["TOP"]["status"] == "ok"
    assert out["TOP"]["value"] == 35.0  # SUB(30) + C(5)


def test_diff_subtotal():
    statements = [{
        "sheet": "S", "statement": "profit_and_loss",
        "rows": [
            _row("GROSS", "1", "Gross"),
            _row("TAX", "2", "Tax"),
            _row("NET", None, "Net (1 minus 2)", origin="derived"),
        ],
    }]
    out = derive_facts(statements, _vals(GROSS=(100.0, 0.9), TAX=(30.0, 0.9)))
    assert out["NET"]["status"] == "ok"
    assert out["NET"]["value"] == 70.0


def test_unresolvable_formula_is_flagged_not_computed():
    statements = [{
        "sheet": "S", "statement": "profit_and_loss",
        "rows": [_row("WEIRD", None, "Some total with no numbers", origin="derived")],
    }]
    out = derive_facts(statements, {})
    assert out["WEIRD"]["status"] == "unresolved"
    assert out["WEIRD"]["value"] is None


def test_against_real_taxonomy_resolves_some_subtotals():
    raw = json.loads(
        (pathlib.Path(__file__).resolve().parents[3]
         / "docs" / "architecture" / "taxonomy" / "taxonomy.raw.json").read_text("utf-8")
    )
    doc = parse_taxonomy(raw)
    # Give every extracted tag a value of 1.0 so resolvable subtotals compute.
    values = {}
    for st in doc["statements"]:
        for r in st["rows"]:
            if r["tag"] and r["origin"] == "extracted" and not r["excluded"]:
                values[r["tag"]] = {"value": 1.0, "confidence": 1.0}
    out = derive_facts(doc["statements"], values)
    ok = [t for t, f in out.items() if f["status"] == "ok"]
    # Measured: ~6 subtotals resolve PRECISELY from sr_no ranges (most leaves
    # carry no sr_no, and the accuracy-first policy refuses to guess the rest,
    # flagging them unresolved/missing_inputs instead). The engine is the
    # deliverable; raising this needs richer sr_no coverage in the taxonomy.
    assert len(ok) >= 5
    assert all(out[t]["value"] is not None for t in ok)
