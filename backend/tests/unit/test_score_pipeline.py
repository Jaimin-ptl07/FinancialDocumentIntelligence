"""The baseline scorer reads BOTH shapes -- today's extraction record and
tomorrow's FOG -- so the cutover comparison is apples to apples."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from score_pipeline import facts_from_extraction, facts_from_fog, report  # noqa: E402

from fdi.core.scoring import GroundTruth, score_document  # noqa: E402

_RECORD = {
    "schema_version": 1,
    "fields": [
        {"tag": "RFOGRS", "value": 255324.0, "answered": True},
        {"tag": "TOTFC", "value": {"current": 800.0, "prior": 700.0}, "answered": True},
        {"tag": "PBTR", "value": None, "answered": True},
        {"field_name": "no tag here", "value": 5.0, "answered": True},
    ],
}


def test_extraction_record_flattens_to_a_tag_map():
    facts = facts_from_extraction(_RECORD)
    assert facts["RFOGRS"] == 255324.0
    assert facts["TOTFC"] == {"current": 800.0, "prior": 700.0}


def test_a_null_value_is_absent_not_a_zero():
    """Collapsing null to 0.0 would turn every missed field into a wrong one."""
    assert "PBTR" not in facts_from_extraction(_RECORD)


def test_a_field_without_a_tag_is_skipped():
    facts = facts_from_extraction(_RECORD)
    assert len(facts) == 2


def test_malformed_record_yields_empty_never_raises():
    assert facts_from_extraction({}) == {}
    assert facts_from_extraction({"fields": "nope"}) == {}


def test_fog_returns_empty_until_mapping_exists():
    """Phase 2 emits no MAPS_TO edges; the scorer must say 'nothing', not crash."""
    assert facts_from_fog({"bases": {"consolidated": {"edges": []}}}, basis="consolidated") == {}


def test_report_renders_a_row_per_ground_truth_file():
    gt = GroundTruth("TCS", "FY2025-26", "consolidated", "profit_and_loss",
                     {"RFOGRS": 255324.0}, ("EXCHEXP",))
    score = score_document(gt, {"RFOGRS": 255324.0})
    text = report([(gt, score)])
    assert "TCS" in text
    assert "profit_and_loss" in text
    assert "100.0%" in text


def test_report_of_nothing_is_a_message_not_a_crash():
    assert "no ground truth" in report([]).lower()
