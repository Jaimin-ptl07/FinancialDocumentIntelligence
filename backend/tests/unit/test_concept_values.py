from fdi.core.concept_values import map_concept_values


def _table(
    column_headers: list[str],
    rows: list[list[str]],
    bounding_regions: list[dict[str, object]] | None = None,
    header_row_count: int = 1,
) -> dict[str, object]:
    return {
        "bounding_regions": bounding_regions if bounding_regions is not None else [],
        "grid": {
            "n_rows": len(rows) + header_row_count,
            "n_cols": len(column_headers),
            "header_row_count": header_row_count,
            "column_headers": column_headers,
            "grid": [column_headers] * header_row_count + rows,
        },
    }


def test_map_concept_values_flattens_matched_rows_to_row_times_column_records() -> None:
    structure = {
        "tables": [
            _table(
                ["Particulars", "FY24", "FY23"],
                [
                    ["Total revenue from operations", "12,345", "10,000"],
                    ["Total Assets", "16,427.37", "14,213.08"],
                ],
                bounding_regions=[{"page_number": 3}],
            ),
        ],
    }
    out = map_concept_values(structure)
    assert out["schema_version"] == 1
    entries = out["concept_values"]
    assert isinstance(entries, list)
    assert len(entries) == 4  # 2 matched rows x 2 data columns each

    revenue_entries = [e for e in entries if e["row_label"] == "Total revenue from operations"]
    assert revenue_entries == [
        {
            "concept": "REVENUE",
            "row_label": "Total revenue from operations",
            "value": 12345.0,
            "raw": "12,345",
            "column_header": "FY24",
            "column_index": 0,
            "page": 3,
            "table_index": 0,
        },
        {
            "concept": "REVENUE",
            "row_label": "Total revenue from operations",
            "value": 10000.0,
            "raw": "10,000",
            "column_header": "FY23",
            "column_index": 1,
            "page": 3,
            "table_index": 0,
        },
    ]

    assets_entries = [e for e in entries if e["row_label"] == "Total Assets"]
    assert assets_entries[0]["concept"] == "TOTAL_ASSETS"
    assert assets_entries[0]["value"] == 16427.37
    assert assets_entries[0]["table_index"] == 0
    assert assets_entries[0]["page"] == 3


def test_map_concept_values_page_none_when_no_bounding_regions() -> None:
    structure = {
        "tables": [
            _table(
                ["Particulars", "FY24"],
                [["Total Assets", "100"]],
                bounding_regions=[],
            ),
        ],
    }
    out = map_concept_values(structure)
    entries = out["concept_values"]
    assert entries
    assert all(e["page"] is None for e in entries)


def test_map_concept_values_preserves_cost_of_revenue_false_positive() -> None:
    """RT-6 decision 5's known false positive: 'Cost of revenue' contains the
    REVENUE synonym token 'revenue'. map_concept_values must NOT filter it."""
    structure = {
        "tables": [_table(["Particulars", "FY24"], [["Cost of revenue", "900"]])],
    }
    out = map_concept_values(structure)
    entries = out["concept_values"]
    revenue_tagged = [e for e in entries if e["concept"] == "REVENUE"]
    assert len(revenue_tagged) == 1
    assert revenue_tagged[0]["row_label"] == "Cost of revenue"


def test_map_concept_values_empty_structure_degrades_to_empty_list() -> None:
    assert map_concept_values({}) == {"schema_version": 1, "concept_values": []}


def test_map_concept_values_non_list_tables_degrades_to_empty_list() -> None:
    assert map_concept_values({"tables": "not-a-list"}) == {
        "schema_version": 1,
        "concept_values": [],
    }


def test_map_concept_values_malformed_grid_contributes_nothing_siblings_unaffected() -> None:
    structure = {
        "tables": [
            {"bounding_regions": [], "grid": "not-a-mapping"},
            _table(["Particulars", "FY24"], [["Total Assets", "100"]]),
        ],
    }
    out = map_concept_values(structure)
    entries = out["concept_values"]
    assert entries
    assert all(e["table_index"] == 1 for e in entries)


def test_map_concept_values_never_raises_on_non_mapping_table_entry() -> None:
    structure = {"tables": ["not-a-mapping", None, 42]}
    assert map_concept_values(structure) == {"schema_version": 1, "concept_values": []}
