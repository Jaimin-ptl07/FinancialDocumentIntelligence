"""Pure validation of a v3.1 field-extraction schema document.

The gate on PUT /schema: a UI that saves a structurally broken schema would
turn POST /extract/document into a silent no-op (exactly the regression this
program's Task 1 exists to fix), so the write must be refused, loudly, with
a JSON path per problem.
"""

import json
from pathlib import Path

from fdi.core.schema import bump_patch_version, validate_extraction_schema


def _valid() -> dict[str, object]:
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 3, "key": "module_3_balance_sheet", "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "retrieval_query": "Standalone Balance Sheet",
                        "locator_keywords": ["balance sheet"],
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "aliases": ["Total assets"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_a_valid_document_has_no_errors() -> None:
    assert validate_extraction_schema(_valid()) == []


def test_the_committed_schema_file_is_valid() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "docs" / "architecture" / "field-extraction-schema.json"
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    assert validate_extraction_schema(doc) == []


def test_a_non_object_document_is_one_error() -> None:
    assert validate_extraction_schema([]) == ["schema must be a JSON object"]
    assert validate_extraction_schema("nope") == ["schema must be a JSON object"]
    assert validate_extraction_schema(None) == ["schema must be a JSON object"]


def test_a_bad_version_is_reported() -> None:
    doc = _valid()
    doc["version"] = "three point one"
    errors = validate_extraction_schema(doc)
    assert any('"version"' in e for e in errors)


def test_empty_modules_is_reported() -> None:
    assert '"modules" must be a non-empty list' in validate_extraction_schema(
        {"version": "3.1.0", "modules": []}
    )


def test_a_v2_flat_schema_is_rejected() -> None:
    """The exact shape that silently produced zero fields for months."""
    doc = {
        "version": "2.0.0",
        "modules": [{"id": 1, "key": "m1", "name": "M1", "fields": [{"field_name": "x"}]}],
    }
    errors = validate_extraction_schema(doc)
    assert any("retrieval_units" in e for e in errors)


def test_duplicate_unit_ids_are_reported() -> None:
    doc = _valid()
    unit = doc["modules"][0]["retrieval_units"][0]
    doc["modules"][0]["retrieval_units"] = [unit, dict(unit)]
    errors = validate_extraction_schema(doc)
    assert any("duplicated" in e for e in errors)


def test_a_blank_unit_retrieval_query_is_reported() -> None:
    doc = _valid()
    doc["modules"][0]["retrieval_units"][0]["retrieval_query"] = "   "
    errors = validate_extraction_schema(doc)
    assert any('"retrieval_query" must be a non-blank string' in e for e in errors)


def test_a_field_missing_required_keys_is_reported_by_path() -> None:
    doc = _valid()
    del doc["modules"][0]["retrieval_units"][0]["fields"][0]["extraction_prompt"]
    errors = validate_extraction_schema(doc)
    assert (
        'modules[0].retrieval_units[0].fields[0]."extraction_prompt" '
        "must be a non-empty string" in errors
    )


def test_an_unknown_data_type_is_reported() -> None:
    doc = _valid()
    doc["modules"][0]["retrieval_units"][0]["fields"][0]["data_type"] = "money"
    errors = validate_extraction_schema(doc)
    assert any("'money' is not one of" in e for e in errors)


def test_validation_never_raises_on_junk() -> None:
    for junk in ({"modules": [1, None, "x"]}, {"modules": [{"retrieval_units": [7]}]},
                 {"modules": [{"retrieval_units": [{"fields": [None]}]}]}):
        assert isinstance(validate_extraction_schema(junk), list)


def test_bump_patch_version() -> None:
    assert bump_patch_version("3.1.0") == "3.1.1"
    assert bump_patch_version("3.1.9") == "3.1.10"
    assert bump_patch_version("10.0.0") == "10.0.1"


def test_bump_patch_version_leaves_an_unparseable_version_alone() -> None:
    """Unreachable from PUT /schema (validation rejects a non-semver version
    before the bump), but total rather than raising."""
    assert bump_patch_version("v3") == "v3"
    assert bump_patch_version("") == ""
