"""A share COUNT is not a rupee AMOUNT, and mapping one onto the other is how
EQWVR came back as 260 with a contested spread of 3.6 BILLION -- the weighted
average share count 361,80,87,518 competing with share capital 362."""

from fdi.core.quantity import KINDS, classify_quantity


def test_a_share_count_is_a_count():
    assert classify_quantity("Weighted average number of equity shares", 3618087518.0) == "count"
    assert classify_quantity("Number of shares outstanding", 1000000.0) == "count"


def test_earnings_per_share_is_per_share_not_currency():
    """EPS is rupees, but rupees PER SHARE -- it must never satisfy a concept
    expecting a total."""
    assert classify_quantity("Earnings per equity share - Basic and diluted", 136.01) == "per_share"
    assert classify_quantity("Basic (in Rs)", 19.59) == "per_share"


def test_a_percentage_or_ratio_is_a_ratio():
    assert classify_quantity("Effective tax rate (%)", 24.5) == "ratio"
    assert classify_quantity("Current ratio", 1.8) == "ratio"


def test_an_ordinary_statement_line_is_currency():
    assert classify_quantity("Revenue from operations", 267021.0) == "currency"
    assert classify_quantity("Total equity", 108478.0) == "currency"


def test_the_label_decides_before_the_magnitude():
    """A large count and a large amount are both large; only the label separates
    them, so magnitude must never override an explicit label signal."""
    assert classify_quantity("Number of equity shares", 12.0) == "count"


def test_a_face_value_per_share_is_per_share():
    assert classify_quantity("Face value per share", 10.0) == "per_share"


def test_shares_alone_does_not_make_a_count():
    """'Sale of shares' is a rupee amount. Only counting language does."""
    assert classify_quantity("Sale of equity shares", 500.0) == "currency"


def test_an_absent_value_still_classifies_on_its_label():
    assert classify_quantity("Number of equity shares", None) == "count"


def test_an_unlabelled_row_is_unknown_not_currency():
    """Guessing currency would let anything satisfy a monetary concept."""
    assert classify_quantity("", 5.0) == "unknown"


def test_every_result_is_in_the_closed_vocabulary():
    for label in ("Revenue", "Number of shares", "EPS per share", "Tax rate (%)", ""):
        assert classify_quantity(label, 1.0) in KINDS


def test_malformed_input_never_raises():
    assert classify_quantity(None, None) == "unknown"  # type: ignore[arg-type]
    assert classify_quantity(123, "x") == "unknown"  # type: ignore[arg-type]
