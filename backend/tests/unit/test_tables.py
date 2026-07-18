import pytest

from fdi.core.ontology import CanonicalConcept
from fdi.core.tables import filter_tables, find_concept_rows, parse_numeric


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


def _grid(
    column_headers: list[str], rows: list[list[str]], header_row_count: int = 1
) -> dict[str, object]:
    return {
        "n_rows": len(rows) + header_row_count,
        "n_cols": len(column_headers),
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        # header row(s) first, then data rows -- mirrors canonicalize_table's grid shape
        "grid": [column_headers] * header_row_count + rows,
    }


def test_find_concept_rows_matches_qualified_label() -> None:
    grid = _grid(
        ["Particulars", "FY24", "FY23"],
        [["Total revenue from operations", "12,345", "10,000"]],
    )
    matches = find_concept_rows(grid, CanonicalConcept.REVENUE)
    assert matches == [{
        "row_label": "Total revenue from operations",
        "values": [
            {"column_header": "FY24", "raw": "12,345", "value": 12345.0},
            {"column_header": "FY23", "raw": "10,000", "value": 10000.0},
        ],
    }]


def test_find_concept_rows_multi_word_synonym() -> None:
    grid = _grid(["Particulars", "FY24"], [["Cash and cash equivalents", "500"]])
    matches = find_concept_rows(grid, CanonicalConcept.CASH)
    assert matches[0]["row_label"] == "Cash and cash equivalents"
    assert matches[0]["values"] == [{"column_header": "FY24", "raw": "500", "value": 500.0}]


def test_find_concept_rows_known_false_positive_documented() -> None:
    """'Cost of revenue' contains the REVENUE synonym 'revenue' as a token --
    matches REVENUE even though the row is a cost line. Spec decision 5:
    documented, not solved, and pinned here so a future fix is a deliberate
    change, not a silent one."""
    grid = _grid(["Particulars", "FY24"], [["Cost of revenue", "900"]])
    assert len(find_concept_rows(grid, CanonicalConcept.REVENUE)) == 1
    assert find_concept_rows(grid, CanonicalConcept.COST_OF_GOODS_SOLD) == []


def test_find_concept_rows_skips_header_rows() -> None:
    # header row's own text ("Revenue Statement") contains a synonym word but
    # must never be treated as a data row.
    grid = {
        "n_rows": 2, "n_cols": 2, "header_row_count": 1,
        "column_headers": ["Revenue Statement", "FY24"],
        "grid": [["Revenue Statement", "FY24"], ["Net profit", "100"]],
    }
    assert find_concept_rows(grid, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_no_match_returns_empty() -> None:
    grid = _grid(["Particulars", "FY24"], [["Some Random Line", "1"]])
    assert find_concept_rows(grid, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_empty_and_malformed_input() -> None:
    assert find_concept_rows({}, CanonicalConcept.REVENUE) == []
    empty_grid = {"n_rows": 1, "n_cols": 0, "grid": [[]]}
    assert find_concept_rows(empty_grid, CanonicalConcept.REVENUE) == []


def test_find_concept_rows_ragged_row_degrades_not_raises() -> None:
    grid = {
        "n_rows": 1, "n_cols": 3, "header_row_count": 0,
        "column_headers": ["Particulars", "FY24", "FY23"],
        "grid": [["Total revenue", "12,345"]],  # missing the FY23 cell
    }
    matches = find_concept_rows(grid, CanonicalConcept.REVENUE)
    assert matches[0]["values"] == [
        {"column_header": "FY24", "raw": "12,345", "value": 12345.0},
        {"column_header": "FY23", "raw": "", "value": None},
    ]


def test_filter_tables_by_column_header_keyword() -> None:
    t1 = {"grid": {"column_headers": ["Particulars", "FY24"]}}
    t2 = {"grid": {"column_headers": ["Particulars", "FY23"]}}
    assert filter_tables([t1, t2], "fy24") == [t1]
    assert filter_tables([t1, t2], None) == [t1, t2]
    assert filter_tables([t1, t2], "") == [t1, t2]


def test_filter_tables_drops_malformed_grid_only_when_keyword_given() -> None:
    malformed = {"grid": "not-a-mapping"}
    assert filter_tables([malformed], "fy24") == []
    assert filter_tables([malformed], None) == [malformed]
