from fdi.core.provenance import resolve_field_regions


def _table(cell_regions: list[list[list[dict[str, object]]]]) -> dict[str, object]:
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
                ["Property, plant & Equipments", "2018.29", "1838.80"],
            ],
            "cell_regions": cell_regions,
        }
    }


def _region(x: float) -> dict[str, object]:
    return {"page_number": 79, "polygon": [x, 0, x + 1, 0, x + 1, 1, x, 1]}


def test_resolve_field_regions_matches_row_and_splits_current_prior() -> None:
    cell_regions = [
        [[], [], []],
        [[_region(0)], [_region(1)], [_region(2)]],
    ]
    structure = {"tables": [_table(cell_regions)]}

    result = resolve_field_regions(
        keywords=("property", "plant", "equipments"),
        structure=structure,
        fallback_current=[{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
    )

    assert result["current"] == [_region(1)]
    assert result["prior"] == [_region(2)]


def test_resolve_field_regions_falls_back_when_no_row_match() -> None:
    cell_regions = [[[], [], []], [[_region(0)], [_region(1)], [_region(2)]]]
    structure = {"tables": [_table(cell_regions)]}
    fallback = [{"page_number": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}]

    result = resolve_field_regions(
        keywords=("nonexistent", "concept"),
        structure=structure,
        fallback_current=fallback,
    )

    assert result == {"current": fallback, "prior": []}


def test_resolve_field_regions_falls_back_on_empty_structure() -> None:
    fallback = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
    result = resolve_field_regions(keywords=("x",), structure={}, fallback_current=fallback)
    assert result == {"current": fallback, "prior": []}
