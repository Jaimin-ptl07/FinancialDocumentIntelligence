from fdi.core.entities import build_concept_comparison

_ROW = {
    "table_index": 0,
    "row_label": "Total revenue",
    "values": [{"column_header": "FY23", "raw": "1,000", "value": 1000.0}],
}


def test_build_concept_comparison_status_per_version() -> None:
    member_lookups = [
        {"doc_id": "d3", "doc_key": "k", "version": 3, "rows": [_ROW]},
        {"doc_id": "d1", "doc_key": "k", "version": 1, "rows": None},
        {"doc_id": "d2", "doc_key": "k", "version": 2, "rows": []},
    ]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert out == {
        "schema_version": 1,
        "doc_key": "k",
        "concept": "REVENUE",
        "versions": [
            {"doc_id": "d1", "doc_key": "k", "version": 1, "status": "no_structure", "rows": []},
            {"doc_id": "d2", "doc_key": "k", "version": 2, "status": "no_match", "rows": []},
            {"doc_id": "d3", "doc_key": "k", "version": 3, "status": "matched", "rows": [_ROW]},
        ],
    }


def test_build_concept_comparison_matched_rows_are_unchanged() -> None:
    # proves no re-shaping/renaming of concept_lookup's own row shape
    member_lookups = [{"doc_id": "d1", "doc_key": "k", "version": 1, "rows": [_ROW, _ROW]}]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert out["versions"][0]["rows"] == [_ROW, _ROW]


def test_build_concept_comparison_defensively_resorts_input() -> None:
    member_lookups = [
        {"doc_id": "d2", "doc_key": "k", "version": 2, "rows": []},
        {"doc_id": "d1", "doc_key": "k", "version": 1, "rows": []},
    ]
    out = build_concept_comparison("k", "REVENUE", member_lookups)
    assert [v["version"] for v in out["versions"]] == [1, 2]


def test_build_concept_comparison_empty() -> None:
    assert build_concept_comparison("k", "REVENUE", []) == {
        "schema_version": 1, "doc_key": "k", "concept": "REVENUE", "versions": [],
    }
