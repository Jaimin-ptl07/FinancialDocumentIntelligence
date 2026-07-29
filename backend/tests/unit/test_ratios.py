"""Pure ratio/CARO computation over Facts."""

from fdi.core.ratios import compute_ratio, compute_ratios


def _v(**kw):
    return {t: {"value": val, "confidence": c} for t, (val, c) in kw.items()}


def _get(results, key):
    return next(r for r in results if r["key"] == key)


def test_current_ratio_ok():
    vals = _v(TOTCAR=(200.0, 0.9), TOTCLR=(100.0, 0.8))
    r = _get(compute_ratios(vals), "current_ratio")
    assert r["status"] == "ok"
    assert r["value"] == 2.0
    assert r["confidence"] == 0.8


def test_missing_input_skipped_with_reason():
    r = _get(compute_ratios(_v(TOTCAR=(200.0, 0.9))), "current_ratio")
    assert r["status"] == "missing_inputs"
    assert r["value"] is None


def test_divide_by_zero_is_undefined_not_raise():
    vals = _v(TOTBOR=(50.0, 0.9), TOTEQNR=(0.0, 0.9))
    r = _get(compute_ratios(vals), "debt_equity")
    assert r["status"] == "undefined"
    assert r["value"] is None


def test_all_ratios_evaluated_and_traceable():
    results = compute_ratios({})
    assert len(results) == 4
    for r in results:
        assert r["status"] == "missing_inputs"
        assert r["explanation"]
        assert r["inputs"]["numerator"] and r["inputs"]["denominator"]
        assert r["caro_clause"]


def test_net_profit_margin():
    vals = _v(PBMI=(30.0, 0.7), RFONET=(300.0, 0.95))
    r = compute_ratio(
        {"key": "net_profit_margin", "label": "NPM", "caro_clause": "Sch III",
         "numerator": ["PBMI"], "denominator": ["RFONET"], "explanation": "p/rev"},
        vals,
    )
    assert r["value"] == 0.1
    assert r["confidence"] == 0.7
