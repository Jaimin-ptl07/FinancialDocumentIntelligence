import pytest

from fdi.core.ontology import CanonicalConcept, map_label


def test_canonical_concept_values() -> None:
    assert CanonicalConcept.REVENUE == "REVENUE"
    assert set(CanonicalConcept) == {
        CanonicalConcept.REVENUE, CanonicalConcept.COST_OF_GOODS_SOLD,
        CanonicalConcept.GROSS_PROFIT, CanonicalConcept.OPERATING_EXPENSE,
        CanonicalConcept.NET_PROFIT, CanonicalConcept.TOTAL_ASSETS,
        CanonicalConcept.TOTAL_LIABILITIES, CanonicalConcept.EQUITY, CanonicalConcept.CASH,
        CanonicalConcept.UNKNOWN,
    }


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
