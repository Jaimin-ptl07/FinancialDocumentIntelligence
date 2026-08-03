"""Scoring must separate ABSENT from MISSED, or recall is meaningless on a
402-tag taxonomy where ~180 tags cannot apply to a services company."""

from fdi.core.scoring import GroundTruth, parse_groundtruth, score_document

_GT = GroundTruth(
    issuer="TCS",
    fiscal_year="FY2025-26",
    basis="consolidated",
    statement="profit_and_loss",
    values={"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0},
    not_reported=("EXCHEXP", "OTPRVE"),
)


def test_perfect_extraction_scores_1_0():
    produced = {"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.correct == 3
    assert score.missed == 0
    assert score.recall == 1.0
    assert score.precision == 1.0


def test_a_missed_tag_is_not_a_wrong_tag():
    """Producing nothing for a reported tag lowers recall but never precision."""
    produced = {"RFOGRS": 255324.0, "PBTR": None}
    score = score_document(_GT, produced)
    assert score.correct == 1
    assert score.missed == 2
    assert score.wrong == 0
    assert score.precision == 1.0


def test_a_value_on_a_not_reported_tag_is_a_false_positive():
    """The company does not disclose EXCHEXP; inventing it must be punished."""
    produced = {"RFOGRS": 255324.0, "EXCHEXP": 12.0}
    score = score_document(_GT, produced)
    assert score.false_positive == 1
    assert score.absent_correct == 1  # OTPRVE correctly left alone
    assert score.committed == 2
    assert score.precision == 0.5


def test_silence_on_a_not_reported_tag_is_correct_not_absent_credit():
    """absent_correct is reported, but it never inflates recall."""
    produced = {"RFOGRS": 255324.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.absent_correct == 2
    assert score.reported == 3
    assert score.recall == 1.0


def test_wrong_value_counts_against_precision_and_recall():
    produced = {"RFOGRS": 999.0, "PBTR": 65000.0, "TOTFC": 800.0}
    score = score_document(_GT, produced)
    assert score.wrong == 1
    assert score.correct == 2
    assert score.precision == 2 / 3


def test_tags_outside_the_ground_truth_are_ignored_entirely():
    """A tag from another statement is out of scope, not a false positive."""
    produced = {"RFOGRS": 255324.0, "SOMEBSTAG": 5.0}
    score = score_document(_GT, produced)
    assert score.committed == 1
    assert score.false_positive == 0


def test_dual_period_values_are_scored_on_the_current_year():
    produced = {"RFOGRS": {"current": 255324.0, "prior": 240893.0}}
    score = score_document(_GT, produced)
    assert score.correct == 1


def test_tolerance_absorbs_float_noise_but_not_a_real_difference():
    assert score_document(_GT, {"RFOGRS": 255324.004}).correct == 1
    assert score_document(_GT, {"RFOGRS": 255325.0}).correct == 0


def test_empty_ground_truth_yields_zero_not_a_crash():
    empty = GroundTruth("X", "FY", "consolidated", "profit_and_loss", {}, ())
    score = score_document(empty, {})
    assert score.recall == 0.0
    assert score.precision == 0.0


def test_parse_rejects_a_tag_that_is_both_valued_and_not_reported():
    """The two lists must be disjoint or absent-vs-missed is undefined."""
    assert parse_groundtruth({
        "issuer": "TCS", "fiscal_year": "FY2025-26", "basis": "consolidated",
        "statement": "profit_and_loss",
        "values": {"RFOGRS": 1.0}, "not_reported": ["RFOGRS"],
    }) is None


def test_parse_reads_a_well_formed_record():
    gt = parse_groundtruth({
        "issuer": "TCS", "fiscal_year": "FY2025-26", "basis": "consolidated",
        "statement": "profit_and_loss",
        "values": {"RFOGRS": 255324.0}, "not_reported": ["EXCHEXP"],
    })
    assert gt is not None
    assert gt.values == {"RFOGRS": 255324.0}
    assert gt.not_reported == ("EXCHEXP",)


def test_parse_degrades_malformed_input_to_none_never_raises():
    assert parse_groundtruth({}) is None
    assert parse_groundtruth({"values": "not a mapping"}) is None
