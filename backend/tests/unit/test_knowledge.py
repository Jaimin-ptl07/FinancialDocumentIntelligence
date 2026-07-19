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
        "concept_values_count": 0,
        "reconciles": True,
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
        "concept_values_count": 0,
        "reconciles": True,
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


# --- concept_values_count / reconciles (Phase 4) ---

_ONTOLOGY_WITH_VALUES = {
    "schema_version": 2,
    "mappings": [],
    "concept_values": [
        {"concept": "TOTAL_ASSETS", "value": 100.0},
        {"concept": "TOTAL_EQUITY_AND_LIABILITIES", "value": 100.0},
        {"concept": "REVENUE", "value": 50.0},
        {"concept": "REVENUE", "value": 40.0},
    ],
}

_QUALITY_WITH_RECON_ISSUE = {
    "schema_version": 1,
    "issues": [
        {
            "severity": "warning",
            "code": "quality.balance_sheet_imbalance",
            "message": "does not balance",
        },
    ],
    "error_count": 0,
    "warning_count": 1,
    "ok": True,
}

_QUALITY_WITH_ONLY_NON_RECON_ISSUES = {
    "schema_version": 1,
    "issues": [
        {"severity": "warning", "code": "structure.torn_table", "message": "torn"},
        {"severity": "error", "code": "ast.no_sections", "message": "empty"},
    ],
    "error_count": 1,
    "warning_count": 1,
    "ok": False,
}


def test_consolidate_concept_values_count() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY_WITH_VALUES, _QUALITY)
    assert out["concept_values_count"] == 4


def test_consolidate_reconciles_false_when_reconciliation_issue_present() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY_WITH_VALUES, _QUALITY_WITH_RECON_ISSUE)
    assert out["reconciles"] is False


def test_consolidate_reconciles_true_when_no_reconciliation_issue_present() -> None:
    out = consolidate(_SEMANTIC, _ONTOLOGY_WITH_VALUES, _QUALITY_WITH_ONLY_NON_RECON_ISSUES)
    assert out["reconciles"] is True


def test_consolidate_empty_inputs_yield_zero_concept_values_count_and_reconciles_true() -> None:
    out = consolidate({}, {}, {})
    assert out["concept_values_count"] == 0
    assert out["reconciles"] is True
