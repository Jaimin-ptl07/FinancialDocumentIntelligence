from fdi.core.provenance import resolve_field_regions


def _table(
    cell_regions: list[list[list[dict[str, object]]]],
    row_label: str = "Property, plant & Equipments",
) -> dict[str, object]:
    n_rows = len(cell_regions)
    n_cols = len(cell_regions[0]) if cell_regions else 0
    return {
        "grid": {
            "n_rows": n_rows,
            "n_cols": n_cols,
            "header_row_count": 1,
            "column_headers": ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
            "grid": [
                ["Particulars", "Column 1 (most recent)", "Column 2 (prior)"],
                [row_label, "2018.29", "1838.80"],
            ],
            "cell_regions": cell_regions,
        }
    }


def _region(x: float, page: int = 79) -> dict[str, object]:
    return {"page_number": page, "polygon": [x, 0, x + 1, 0, x + 1, 1, x, 1]}


def test_resolve_field_regions_matches_row_and_splits_current_prior() -> None:
    cell_regions = [
        [[], [], []],
        [[_region(0)], [_region(1)], [_region(2)]],
    ]
    structure = {"tables": [_table(cell_regions)]}

    result = resolve_field_regions(
        keywords=("property", "plant", "equipments"),
        evidence_ids=["t0"],
        structure=structure,
        fallback_current=[{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
    )

    assert result["current"] == [_region(1)]
    assert result["prior"] == [_region(2)]


def test_resolve_field_regions_searches_cited_table_before_others() -> None:
    """The bug this guards against: a field's keywords can coincidentally
    match a row label in an EARLIER, unrelated table -- resolving there
    instead of the table the LLM actually cited would land on the wrong
    page. evidence_ids names table 1; table 0's lookalike row must be
    ignored even though it comes first in `structure["tables"]`."""
    lookalike = _table(
        [[[], [], []], [[_region(100, page=1)], [_region(101, page=1)], [_region(102, page=1)]]],
        row_label="Property, plant & Equipments (schedule reference)",
    )
    cited = _table(
        [[[], [], []], [[_region(0, page=79)], [_region(1, page=79)], [_region(2, page=79)]]],
    )
    structure = {"tables": [lookalike, cited]}

    result = resolve_field_regions(
        keywords=("property", "plant", "equipments"),
        evidence_ids=["doc-1:t1"],
        structure=structure,
        fallback_current=[{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
    )

    assert result["current"] == [_region(1, page=79)]
    assert result["prior"] == [_region(2, page=79)]


def test_resolve_field_regions_falls_back_to_document_wide_when_cited_table_has_no_match() -> None:
    """The cited table exists but its row label doesn't match (e.g. the LLM
    cited the wrong evidence_id, or the field's keywords are looser than the
    row it actually read) -- a document-wide search is still a better answer
    than giving up and using only the coarse fallback."""
    cited_no_match = _table(
        [[[], [], []], [[_region(0)], [_region(1)], [_region(2)]]],
        row_label="Some unrelated line item",
    )
    elsewhere_matches = _table(
        [[[], [], []], [[_region(10, page=5)], [_region(11, page=5)], [_region(12, page=5)]]],
    )
    structure = {"tables": [cited_no_match, elsewhere_matches]}

    result = resolve_field_regions(
        keywords=("property", "plant", "equipments"),
        evidence_ids=["t0"],
        structure=structure,
        fallback_current=[{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
    )

    assert result["current"] == [_region(11, page=5)]
    assert result["prior"] == [_region(12, page=5)]


def test_resolve_field_regions_falls_back_when_no_row_match() -> None:
    cell_regions = [[[], [], []], [[_region(0)], [_region(1)], [_region(2)]]]
    structure = {"tables": [_table(cell_regions)]}
    fallback = [{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}]

    result = resolve_field_regions(
        keywords=("nonexistent", "concept"),
        evidence_ids=["t0"],
        structure=structure,
        fallback_current=fallback,
    )

    assert result == {"current": fallback, "prior": []}


def test_resolve_field_regions_falls_back_on_empty_structure() -> None:
    fallback = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
    result = resolve_field_regions(
        keywords=("x",), evidence_ids=[], structure={}, fallback_current=fallback
    )
    assert result == {"current": fallback, "prior": []}
