"""Reading the unit a statement is printed in, and exact decimals from
printed cells (S5)."""

from datetime import date
from decimal import Decimal

import pytest

from fdi.core.numbers import parse_amount, parse_decimal, printed_places
from fdi.core.reporting_unit import (
    NUMBER,
    RATIO,
    RUPEES,
    UNKNOWN,
    column_period_ends,
    normalise,
    table_unit,
    unit_for_kind,
    unit_from_text,
)


@pytest.mark.parametrize(
    ("text", "label", "scale"),
    [
        ("(₹ crore)", "INR crore", "10000000"),
        ("(₹ crores)", "INR crore", "10000000"),
        ("(₹ in lakhs)", "INR lakh", "100000"),
        ("(Rs. in Lakhs)", "INR lakh", "100000"),
        ("(All amounts in ₹ crore, unless otherwise stated)", "INR crore", "10000000"),
        ("INR million", "INR million", "1000000"),
        ("Amounts in crores of rupees", "INR crore", "10000000"),
        ("(₹)", "INR", "1"),
    ],
)
def test_unit_declarations(text: str, label: str, scale: str) -> None:
    got = unit_from_text(text)
    assert got is not None
    assert (got.label, got.scale, got.currency) == (label, Decimal(scale), "INR")


def test_text_with_no_unit_declares_none() -> None:
    assert unit_from_text("Revenue from operations") is None
    assert unit_from_text("crore") is None          # a scale with no currency
    assert unit_from_text(None) is None


def test_the_unit_nearest_the_table_wins_even_when_the_span_splits_it() -> None:
    # MEASURED on TCS FY2024-25: Azure DI's table span began inside the
    # unit line, splitting `(₹ c|rore)` across the boundary.
    before = "Balance Sheet (₹ in lakhs)\n...\nStatement of Profit and Loss\n(₹ c"
    content = before + "rore)\nNote\nYear ended March 31, 2025\nRevenue 1,000"
    ocr = {"content": content, "tables": [{"spans": [{"offset": len(before), "length": 50}]}]}
    assert table_unit(ocr, 0).label == "INR crore"


def test_a_table_caption_wins_and_no_declaration_is_unknown() -> None:
    ocr = {"content": "nothing here", "tables": [
        {"caption": {"content": "Other income (₹ in lakhs)"}, "spans": [{"offset": 0}]},
        {"spans": [{"offset": 5, "length": 3}]},
    ]}
    assert table_unit(ocr, 0).label == "INR lakh"
    assert table_unit(ocr, 1) == UNKNOWN
    assert table_unit(ocr, 7) == UNKNOWN
    assert table_unit({}, 0) == UNKNOWN


def test_a_lines_kind_decides_its_unit() -> None:
    crore = unit_from_text("(₹ crore)")
    assert crore is not None
    assert unit_for_kind("currency", crore) == crore
    assert unit_for_kind("per_share", crore) == RUPEES
    assert unit_for_kind("count", crore) == NUMBER
    assert unit_for_kind("ratio", crore) == RATIO
    assert unit_for_kind("unknown", crore) == UNKNOWN


def test_normalise_refuses_an_unknown_scale() -> None:
    crore = unit_from_text("(₹ crore)")
    assert crore is not None
    assert normalise(Decimal("2.5"), crore) == Decimal("25000000.0")
    assert normalise(Decimal("2.5"), UNKNOWN) is None


def test_the_printed_unit_text_is_not_part_of_a_units_identity() -> None:
    assert unit_from_text("(₹ crore)") == unit_from_text("(₹ crores)")


def test_column_period_ends() -> None:
    assert column_period_ends(
        ["", "Note", "Year ended March 31, 2026", "Year ended March 31, 2025"]
    ) == (date(2026, 3, 31), date(2025, 3, 31))
    assert column_period_ends(["As at 31 March 2026", "As at 31st March, 2025"]) == (
        date(2026, 3, 31), date(2025, 3, 31))
    assert column_period_ends(["Particulars", "Note"]) == ()


@pytest.mark.parametrize(
    ("text", "value", "places"),
    [
        ("2,67,021", "267021", 0),
        ("14,372.80", "14372.80", 2),
        ("(2,681.75)", "-2681.75", 2),
        ("₹ 1,10,270 Cr", "110270", 0),
        ("−468", "-468", 0),
    ],
)
def test_parse_decimal_is_exact_and_keeps_printed_places(
    text: str, value: str, places: int
) -> None:
    got = parse_decimal(text)
    assert got == Decimal(value)
    assert got is not None and printed_places(got) == places
    assert float(got) == parse_amount(text)        # one grammar, two types


@pytest.mark.parametrize("text", ["-", "", "NIL", "Revenue", None, 12.5, "1/2"])
def test_parse_decimal_rejects_what_is_not_an_amount(text: object) -> None:
    assert parse_decimal(text) is None
