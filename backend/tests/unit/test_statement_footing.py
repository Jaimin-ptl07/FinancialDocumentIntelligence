"""Footing is what PROVES the right face table was chosen, whatever found it.
`income - expenses = PBT` ties on TCS, RITES and RIL."""

from fdi.core.statements import FaceRow, describe_footing


def _row(label: str, current: float | None) -> FaceRow:
    return FaceRow(label=label, caption="", ref="", current=current, prior=None)


_PL = [
    _row("Total income", 100.0),
    _row("Total expenses", 70.0),
    _row("Profit before tax", 30.0),
]


def test_a_footing_pl_passes():
    result = describe_footing(_PL, "profit_and_loss")
    assert result["passed"] is True
    assert result["checked"] is True
    assert "profit before tax" in result["rule"]


def test_a_non_footing_pl_fails_loudly():
    rows = [_row("Total income", 100.0), _row("Total expenses", 70.0),
            _row("Profit before tax", 44.0)]
    assert describe_footing(rows, "profit_and_loss")["passed"] is False


def test_a_missing_line_is_unchecked_not_failed():
    """None means 'could not check'. Reporting it as a failure would discard a
    correct table that merely labels a line unusually."""
    rows = [_row("Total income", 100.0), _row("Profit before tax", 30.0)]
    result = describe_footing(rows, "profit_and_loss")
    assert result["passed"] is None
    assert result["checked"] is False


def test_balance_sheet_foots_on_the_accounting_identity():
    rows = [_row("Total equity", 40.0), _row("Total liabilities", 60.0),
            _row("Total assets", 100.0)]
    assert describe_footing(rows, "balance_sheet")["passed"] is True


def test_a_statement_with_no_rule_is_unchecked():
    assert describe_footing(_PL, "cash_flow")["checked"] is False
    assert describe_footing(_PL, "cash_flow")["passed"] is None


def test_tolerance_absorbs_rounding_in_a_printed_statement():
    rows = [_row("Total income", 100.0), _row("Total expenses", 70.0),
            _row("Profit before tax", 30.4)]
    assert describe_footing(rows, "profit_and_loss")["passed"] is True


def test_empty_rows_never_raise():
    assert describe_footing([], "profit_and_loss")["passed"] is None


def test_exceptional_items_pick_the_subtotal_the_arithmetic_produces():
    """MEASURED ON TCS FY2025-26 (Rs crore). income - expenses equals PROFIT
    BEFORE EXCEPTIONAL ITEMS AND TAX; PROFIT BEFORE TAX is 4,526 lower because
    three exceptional items sit between them. Checking the lower line called a
    perfectly-footing face a failure on BOTH bases."""
    rows = [
        _row("TOTAL INCOME", 271423.0),
        _row("TOTAL EXPENSES", 201410.0),
        _row("PROFIT BEFORE EXCEPTIONAL ITEMS AND TAX", 70013.0),
        _row("Re-structuring expenses", 1388.0),
        _row("Statutory impact of new Labour Codes", 2128.0),
        _row("Provision towards legal claim", 1010.0),
        _row("PROFIT BEFORE TAX", 65487.0),
    ]
    result = describe_footing(rows, "profit_and_loss")
    assert result["passed"] is True
    assert "profit before exceptional items and tax" in result["rule"]


def test_a_filer_with_no_exceptional_items_falls_back_to_profit_before_tax():
    """The specific target is absent, so the general one applies -- the pre-
    existing behaviour must not regress."""
    result = describe_footing(_PL, "profit_and_loss")
    assert result["passed"] is True
    assert result["rule"].endswith("= profit before tax")


def test_the_two_profit_labels_never_shadow_each_other():
    """`verify_footing` matches on substring, so this is what makes ordering
    safe: the specific label does NOT contain the general one."""
    assert "profit before tax" not in "profit before exceptional items and tax"


def test_a_genuinely_broken_face_still_fails():
    rows = [
        _row("TOTAL INCOME", 271423.0),
        _row("TOTAL EXPENSES", 201410.0),
        _row("PROFIT BEFORE EXCEPTIONAL ITEMS AND TAX", 99999.0),
    ]
    assert describe_footing(rows, "profit_and_loss")["passed"] is False


def test_balance_sheet_accepts_the_equity_and_liabilities_caption():
    """Schedule III prints "TOTAL EQUITY AND LIABILITIES"; requiring the words
    "total assets" left every Indian balance sheet unchecked."""
    rows = [
        _row("Total equity", 40.0), _row("Total liabilities", 60.0),
        _row("TOTAL EQUITY AND LIABILITIES", 100.0),
    ]
    assert describe_footing(rows, "balance_sheet")["passed"] is True
