"""A dropped answer must not read as a company disclosing nothing."""

from fdi.core.coverage import coverage_report
from fdi.core.extractor import FieldSpec, UnitSpec, assemble_unit_results


def _field(path: str, name: str) -> FieldSpec:
    return FieldSpec(
        field_name=name, data_type="decimal", retrieval_query="q", keywords=(),
        extraction_prompt="p", enum_values=None, module_id=1, module_name="Assets",
        unit_id="U1", field_path=path, tag=path.split(".")[-1],
    )


def test_a_returned_null_and_a_dropped_field_are_distinguishable():
    unit = UnitSpec(unit_id="U1", unit_title="Assets", retrieval_query="q",
                    locator_keywords=(),
                    fields=(_field("assets.CB", "Cash balance"),
                            _field("assets.BB", "Bank balance")))
    # the model answered CB explicitly as absent, and never mentioned BB at all
    raw = {"assets.CB": {"value": None, "confidence": 0.0}}

    records = assemble_unit_results(unit, raw, [])

    assert records[0]["answered"] is True   # asked and answered "not present"
    assert records[1]["answered"] is False  # dropped by the batch


def test_coverage_separates_absent_from_missed():
    statements = [{
        "statement": "assets",
        "rows": [
            {"tag": "CB", "origin": "extracted"},
            {"tag": "BB", "origin": "extracted"},
        ],
    }]
    report = coverage_report(statements, facts={}, unanswered=["BB"])

    totals = report["totals"]
    assert totals["missing_extracted"] == 2
    assert totals["not_answered"] == 1        # only BB was never answered
    assert report["not_answered"] == ["BB"]
    # CB stays a correct null; BB is a recall defect, and they are not summed
    assert set(report["missing_extracted"]) == {"CB", "BB"}


def test_unanswered_defaults_to_empty_so_existing_callers_are_unchanged():
    statements = [{"statement": "assets",
                   "rows": [{"tag": "CB", "origin": "extracted"}]}]
    report = coverage_report(statements, facts={})
    assert report["totals"]["not_answered"] == 0
    assert report["not_answered"] == []
