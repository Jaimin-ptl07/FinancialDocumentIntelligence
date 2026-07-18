from fdi.core.knowledge import consolidate

_SEMANTIC = {
    "schema_version": 1,
    "sections": [
        {"heading": "Balance Sheet", "type": "BALANCE_SHEET", "paragraph_count": 1},
        {"heading": "Directors' Report", "type": "DIRECTORS_REPORT", "paragraph_count": 1},
        {"heading": "Notes", "type": "BALANCE_SHEET", "paragraph_count": 3},
    ],
}

_ONTOLOGY = {
    "schema_version": 1,
    "mappings": [
        {"label": "Total Assets", "concept": "TOTAL_ASSETS"},
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
    ],
}

_QUALITY = {
    "schema_version": 1,
    "issues": [{"severity": "warning", "code": "structure.torn_table", "message": "torn"}],
    "error_count": 0,
    "warning_count": 1,
    "ok": True,
}


def test_consolidate_counts_section_types_and_total() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["section_count"] == 3
    assert out["section_types"] == {"BALANCE_SHEET": 2, "DIRECTORS_REPORT": 1}


def test_consolidate_sorted_unique_concepts_excludes_unknown() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["concepts"] == ["REVENUE", "TOTAL_ASSETS"]
    assert out["concept_count"] == 2


def test_consolidate_echoes_quality_summary() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)
    assert out["quality"] == {"ok": True, "error_count": 0, "warning_count": 1}


def test_consolidate_schema_version_is_1() -> None:
    assert consolidate(_SEMANTIC, _ONTOLOGY, _QUALITY)["schema_version"] == 1


def test_consolidate_empty_inputs_yield_zeros_and_empties() -> None:
    assert consolidate({}, {}, {}) == {
        "schema_version": 1,
        "section_count": 0,
        "section_types": {},
        "concepts": [],
        "concept_count": 0,
        "quality": {"ok": False, "error_count": 0, "warning_count": 0},
    }


def test_consolidate_present_but_keyless_artifacts_degrade_like_empty() -> None:
    assert consolidate(
        {"schema_version": 1}, {"schema_version": 1}, {"schema_version": 1}
    ) == {
        "schema_version": 1,
        "section_count": 0,
        "section_types": {},
        "concepts": [],
        "concept_count": 0,
        "quality": {"ok": False, "error_count": 0, "warning_count": 0},
    }


def test_consolidate_malformed_semantic_sections_degrade_without_raising() -> None:
    semantic = {
        "sections": [
            "not a mapping",
            {"heading": "X", "type": 42, "paragraph_count": 1},
            {"heading": "Y"},
        ],
    }
    out = consolidate(semantic, {}, {})
    assert out["section_count"] == 2  # the string entry doesn't count as a section
    assert out["section_types"] == {}  # neither surviving entry has a usable str type


def test_consolidate_malformed_ontology_mappings_degrade_without_raising() -> None:
    ontology = {
        "mappings": [
            "not a mapping",
            {"label": "X", "concept": 42},
            {"label": "Y"},
            {"label": "Z", "concept": "UNKNOWN"},
        ],
    }
    out = consolidate({}, ontology, {})
    assert out["concepts"] == []
    assert out["concept_count"] == 0


def test_consolidate_malformed_quality_summary_degrades_without_raising() -> None:
    quality = {"ok": "yes", "error_count": "1", "warning_count": None}
    out = consolidate({}, {}, quality)
    assert out["quality"] == {"ok": False, "error_count": 0, "warning_count": 0}
