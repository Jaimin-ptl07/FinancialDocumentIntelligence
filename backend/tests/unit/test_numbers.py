"""Deterministic amount parsing -- the model never interprets separators."""

import pytest

from fdi.core.numbers import looks_numeric, parse_amount, row_amounts


@pytest.mark.parametrize(("printed", "expected"), [
    # Indian lakh/crore grouping. The measured bug: the model read '8,685' as
    # 8.685 and returned 8.
    ("2,20,938", 220938.0),
    ("8,685", 8685.0),
    ("1,833", 1833.0),
    ("164101", 164101.0),
    # RITES prints BOTH separators -- comma groups, dot decimates.
    ("14,372.80", 14372.80),
    ("2,332.79", 2332.79),
    ("7.55", 7.55),
    # Accounting negatives.
    ("(468)", -468.0),
    ("(2,681.75)", -2681.75),
    ("(1,066)", -1066.0),
    # Currency symbols and trailing unit words.
    ("₹ 1,10,270", 110270.0),
    ("1,10,270 Cr", 110270.0),
    ("2,750 crore", 2750.0),
])
def test_printed_amounts_parse_exactly(printed, expected):
    assert parse_amount(printed) == pytest.approx(expected)


@pytest.mark.parametrize("printed", [
    "-", "--", "–", "*", ".*", "NA", "N/A", "Nil", "", "   ",
    "Revenue from operations", "Year ended March 31, 2026", "6(a)",
])
def test_non_amounts_return_none_rather_than_a_guess(printed):
    """A nil marker is NOT zero. Reading '-' as 0 would invent a value, and a
    starred dash means 'less than half a unit', not nothing."""
    assert parse_amount(printed) is None


def test_never_raises_on_any_input():
    for junk in (None, [], {}, object(), True, float("nan")):
        parse_amount(junk)          # must not raise
    assert parse_amount(None) is None
    assert parse_amount(True) is None       # bool is not an amount


def test_plain_numbers_pass_through():
    assert parse_amount(42) == 42.0
    assert parse_amount(-3.5) == -3.5


def test_looks_numeric_distinguishes_a_value_row_from_a_caption():
    """A row with no amount anywhere is a section header. Mapping captions as
    concepts produced several wrong assignments before this check."""
    assert looks_numeric("1,833") is True
    assert looks_numeric("(468)") is True
    assert looks_numeric("Items that will not be reclassified") is False
    assert looks_numeric("-") is False


def test_is_nil_marker_separates_an_explicit_nil_from_an_empty_cell():
    """`parse_amount` returns None for both, deliberately -- neither is a
    number. But a caption row has no amount cells (empty) while a nil row has
    amount cells the issuer filled in with an explicit nil, and a nil row misread
    as a caption re-labels every row beneath it."""
    from fdi.core.numbers import is_nil_marker

    assert is_nil_marker("-") is True
    assert is_nil_marker(" – ") is True
    assert is_nil_marker("NIL") is True
    assert is_nil_marker("N/A") is True
    assert is_nil_marker("") is False
    assert is_nil_marker("   ") is False
    assert is_nil_marker("1,833") is False
    assert is_nil_marker("Gross block") is False
    assert is_nil_marker(None) is False
    assert is_nil_marker(0) is False


def test_row_amounts_preserves_period_order():
    row = {0: "Revenue from operations", 1: "10", 2: "2,20,938", 3: "2,14,853"}
    # note: col 1 is the Note column and DOES parse as a number -- callers that
    # know the note column exclude it; the helper itself is positional.
    assert row_amounts(row) == [10.0, 220938.0, 214853.0]
    assert row_amounts({0: "TOTAL INCOME", 1: "", 2: "2,30,974"}) == [230974.0]
    assert row_amounts({0: "Expenses", 1: "", 2: ""}) == []
