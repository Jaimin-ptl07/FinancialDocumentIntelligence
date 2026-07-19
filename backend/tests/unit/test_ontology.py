import pytest

from fdi.core.ontology import CanonicalConcept, map_label, normalize_label, synonyms_for


def test_canonical_concept_values() -> None:
    assert CanonicalConcept.REVENUE == "REVENUE"
    assert set(CanonicalConcept) == {
        CanonicalConcept.REVENUE, CanonicalConcept.COST_OF_GOODS_SOLD,
        CanonicalConcept.GROSS_PROFIT, CanonicalConcept.OPERATING_EXPENSE,
        CanonicalConcept.NET_PROFIT, CanonicalConcept.TOTAL_ASSETS,
        CanonicalConcept.TOTAL_LIABILITIES, CanonicalConcept.EQUITY, CanonicalConcept.CASH,
        CanonicalConcept.TOTAL_EQUITY_AND_LIABILITIES, CanonicalConcept.TOTAL_INCOME,
        CanonicalConcept.PROFIT_BEFORE_TAX, CanonicalConcept.OPENING_CASH_BALANCE,
        CanonicalConcept.CLOSING_CASH_BALANCE, CanonicalConcept.NET_CHANGE_IN_CASH,
        CanonicalConcept.UNKNOWN,
    }


@pytest.mark.parametrize(("label", "expected"), [
    ("Total Equity and Liabilities", CanonicalConcept.TOTAL_EQUITY_AND_LIABILITIES),
    ("Total Equities and Liabilities", CanonicalConcept.TOTAL_EQUITY_AND_LIABILITIES),
    ("Total Income", CanonicalConcept.TOTAL_INCOME),
    ("Profit Before Tax", CanonicalConcept.PROFIT_BEFORE_TAX),
    ("PBT", CanonicalConcept.PROFIT_BEFORE_TAX),
    ("Profit Before Tax and Exceptional Items", CanonicalConcept.PROFIT_BEFORE_TAX),
    ("Opening Cash and Cash Equivalents", CanonicalConcept.OPENING_CASH_BALANCE),
    (
        "Cash and Cash Equivalents at the Beginning of the Year",
        CanonicalConcept.OPENING_CASH_BALANCE,
    ),
    ("Closing Cash and Cash Equivalents", CanonicalConcept.CLOSING_CASH_BALANCE),
    (
        "Cash and Cash Equivalents at the End of the Year",
        CanonicalConcept.CLOSING_CASH_BALANCE,
    ),
    ("Net Change in Cash and Cash Equivalents", CanonicalConcept.NET_CHANGE_IN_CASH),
    (
        "Net Increase in Cash and Cash Equivalents",
        CanonicalConcept.NET_CHANGE_IN_CASH,
    ),
])
def test_map_label_new_reconciliation_concepts(
    label: str, expected: CanonicalConcept
) -> None:
    assert map_label(label) == expected


@pytest.mark.parametrize(("label", "expected"), [
    ("Revenue", CanonicalConcept.REVENUE),
    ("Sales", CanonicalConcept.REVENUE),
    ("Turnover", CanonicalConcept.REVENUE),
    ("Income from Operations", CanonicalConcept.REVENUE),
    ("Cost of Goods Sold", CanonicalConcept.COST_OF_GOODS_SOLD),
    ("COGS", CanonicalConcept.COST_OF_GOODS_SOLD),
    ("Gross Profit", CanonicalConcept.GROSS_PROFIT),
    ("Net Profit", CanonicalConcept.NET_PROFIT),
    ("Profit After Tax", CanonicalConcept.NET_PROFIT),
    ("PAT", CanonicalConcept.NET_PROFIT),
    ("Total Assets", CanonicalConcept.TOTAL_ASSETS),
    ("Total Liabilities", CanonicalConcept.TOTAL_LIABILITIES),
    ("Equity", CanonicalConcept.EQUITY),
    ("Cash", CanonicalConcept.CASH),
])
def test_map_label_synonyms(label: str, expected: CanonicalConcept) -> None:
    assert map_label(label) == expected


@pytest.mark.parametrize("variant", [
    "REVENUE", "  Revenue  ", "Revenue:", "Revenue.", "revenue", "Revenue,",
])
def test_map_label_normalizes_case_whitespace_punctuation(variant: str) -> None:
    assert map_label(variant) == CanonicalConcept.REVENUE


@pytest.mark.parametrize("label", [
    "Some Random Heading", "Directors' Report", "", "   ",
])
def test_map_label_unknown_for_unrecognized(label: str) -> None:
    assert map_label(label) == CanonicalConcept.UNKNOWN


def test_normalize_label_public() -> None:
    assert normalize_label("  Revenue: ") == "revenue"
    assert normalize_label("REVENUE, net") == "revenue net"


def test_synonyms_for_revenue() -> None:
    syns = synonyms_for(CanonicalConcept.REVENUE)
    assert set(syns) == {
        "revenue", "sales", "turnover", "revenue from operations",
        "income from operations", "total revenue",
    }


def test_synonyms_for_unknown_is_empty() -> None:
    assert synonyms_for(CanonicalConcept.UNKNOWN) == []


def test_synonyms_for_every_concept_nonempty_except_unknown() -> None:
    for concept in CanonicalConcept:
        if concept is CanonicalConcept.UNKNOWN:
            continue
        assert synonyms_for(concept), f"{concept} has no synonyms"
