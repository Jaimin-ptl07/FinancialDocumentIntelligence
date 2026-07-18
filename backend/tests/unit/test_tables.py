import pytest

from fdi.core.tables import parse_numeric


@pytest.mark.parametrize(("raw", "expected"), [
    ("12,345", 12345.0),
    ("1,234.56", 1234.56),
    ("(1,234)", -1234.0),
    ("-1,234", -1234.0),
    ("+1,234", 1234.0),
    ("1.2", 1.2),
    ("$12,345.67", 12345.67),
    ("Rs. 1,234", 1234.0),
    ("Rs 1234", 1234.0),
    ("INR 500", 500.0),
    ("₹1,00,000", 100000.0),  # Indian lakh grouping (rupee sign)
    ("12.5%", 12.5),
    ("(12.5%)", -12.5),
    ("  42  ", 42.0),
    ("0", 0.0),
    ("0.00", 0.0),
    (42, 42.0),
    (42.5, 42.5),
])
def test_parse_numeric_recognized_formats(raw: object, expected: float) -> None:
    assert parse_numeric(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "-", "--", "–", "—", "N/A", "n/a", "NA", "Nil", "NIL", "NM", "nm",
    "abc", "1.2.3", "1.5x", "TBD", None, True, False, [], {},
])
def test_parse_numeric_unrecognized_returns_none(raw: object) -> None:
    assert parse_numeric(raw) is None
