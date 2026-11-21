"""`score_run` is the only seam: every test states a benchmark item, what the
answering endpoint said, and what the score must be -- never how it is
computed. Expected values are worked by hand from the printed figures."""

from fdi.core.answer_scoring import EndpointAnswer, score_run
from fdi.core.benchmark import parse_benchmark


def _bench(*values: tuple[float, str, float], kind: str = "fact_lookup", item_id: str = "q1"):
    return parse_benchmark({
        "version": "v-test",
        "items": [{
            "id": item_id,
            "kind": kind,
            "question": "What was revenue?",
            "gold": {
                "answerable": True,
                "values": [{"value": v, "unit": u, "tolerance": t} for v, u, t in values],
            },
        }],
    })


def _status(answer: str | None, *values: tuple[float, str, float], error: str | None = None):
    report = score_run(_bench(*values), [EndpointAnswer(item_id="q1", answer=answer, error=error)])
    return report.items[0].status


def test_the_printed_figure_scores_correct() -> None:
    assert _status("Revenue was 255324.", (255324.0, "INR crore", 0.5)) == "correct"


def test_a_different_figure_scores_wrong() -> None:
    assert _status("Revenue was 255,000.", (255324.0, "INR crore", 0.5)) == "wrong"


_REVENUE = (271423.0, "INR crore", 0.5)


def test_indian_grouping_with_rupee_sign_and_crore_scores_correct() -> None:
    assert _status("Revenue was ₹2,71,423 crore.", _REVENUE) == "correct"


def test_inr_prefix_and_cr_abbreviation_score_correct() -> None:
    assert _status("Revenue: INR 2,71,423 Cr", _REVENUE) == "correct"


def test_lakh_crore_rescaled_to_the_gold_unit_scores_correct() -> None:
    """2.71 lakh crore = 271,000 crore: the answer's own rounding (to 0.01
    lakh crore) covers the 423 crore gap."""
    assert _status("Revenue was ₹2.71 lakh crore.", _REVENUE) == "correct"


def test_a_two_significant_figure_rounding_is_too_coarse() -> None:
    """2.7 lakh crore is also 271,423 rounded -- but to two figures, which
    states almost nothing. It must not pass unless the item allows it."""
    assert _status("Revenue was ₹2.7 lakh crore.", _REVENUE) == "wrong"
    assert _status(
        "Revenue was ₹2.7 lakh crore.", (271423.0, "INR crore", 1500.0)
    ) == "correct"


def test_the_same_figure_in_the_wrong_scale_scores_wrong() -> None:
    """2,71,423 lakh is 2,714.23 crore: a x100 unit slip is a wrong answer."""
    assert _status("Revenue was ₹2,71,423 lakh.", _REVENUE) == "wrong"


def test_a_crore_figure_matches_a_lakh_gold() -> None:
    """REMC prints in lakh. 140.4691 crore = 14,046.91 lakh."""
    assert _status("Total income was ₹140.4691 crore.", (14046.91, "INR lakh", 0.005)) == "correct"


def test_a_bare_figure_is_read_in_the_gold_unit() -> None:
    assert _status("Basic EPS was ₹7.55.", (7.55, "INR", 0.005)) == "correct"


_REMC_CT = (-2681.75, "INR lakh", 0.005)


def test_brackets_are_a_negative_figure() -> None:
    assert _status("Current tax: ₹(2,681.75) lakh.", _REMC_CT) == "correct"
    assert _status("Current tax: (2,681.75)", _REMC_CT) == "correct"


def test_an_unsigned_figure_matches_on_magnitude() -> None:
    assert _status("Current tax was ₹2,681.75 lakh.", _REMC_CT) == "correct"


def test_an_explicitly_signed_figure_must_match_the_sign() -> None:
    assert _status("Current tax was (16,388) crore.", (16388.0, "INR crore", 0.5)) == "wrong"
    assert _status("Current tax was -16,388 crore.", (16388.0, "INR crore", 0.5)) == "wrong"


def test_a_fiscal_year_range_is_not_read_as_a_negative_figure() -> None:
    assert _status("In FY2025-26 the loss was 26.", (-26.0, "INR crore", 0.5)) == "correct"
    assert _status("In FY2025-26 nothing was reported.", (-26.0, "INR crore", 0.5)) == "wrong"


def test_only_the_headline_figure_is_the_answer_not_a_prior_year_comparison() -> None:
    """Reading the wrong column is /ask's known failure mode: a figure that
    appears only as the comparison must not score."""
    gold = (48553.0, "INR crore", 0.5)
    assert _status("PAT was ₹45,000 crore, up from ₹48,553 crore in FY2024-25.", gold) == "wrong"
    assert _status("PAT was ₹48,553 crore, up from ₹45,000 crore.", gold) == "correct"


def test_page_note_and_year_references_are_not_figures() -> None:
    assert _status(
        "See page 362: other equity was ₹400 crore.", (362.0, "INR crore", 0.5)
    ) == "wrong"
    assert _status("Per Note 4, current tax was ₹16,388 crore.", (4.0, "INR crore", 0.5)) == "wrong"
    assert _status("Inventories (p. 29) were ₹31 crore.", (29.0, "INR crore", 0.5)) == "wrong"
    assert _status(
        "For the year ended 31 March 2026, PAT was ₹48,553 crore.", (48553.0, "INR crore", 0.5)
    ) == "correct"


def test_an_en_dash_year_range_is_not_a_figure() -> None:
    assert _status("In FY2025–26 nothing was reported.", (26.0, "INR crore", 0.5)) == "wrong"


def test_a_percentage_is_not_an_amount() -> None:
    assert _status("EPS rose 13.4% to ₹150.10.", (13.4, "INR", 0.05)) == "wrong"
    assert _status("EPS rose 13.4 per cent to ₹150.10.", (150.1, "INR", 0.005)) == "correct"


def test_a_minus_before_the_currency_sign_is_kept() -> None:
    assert _status("Deferred tax was -₹450.34 lakh.", (450.34, "INR lakh", 0.005)) == "wrong"
    assert _status("Deferred tax was -₹450.34 lakh.", (-450.34, "INR lakh", 0.005)) == "correct"


def test_tolerance_is_inclusive_at_its_edge() -> None:
    assert _status("EPS was 7.555", (7.55, "INR", 0.005)) == "correct"
    assert _status("EPS was 7.556", (7.55, "INR", 0.005)) == "wrong"


def test_a_multi_value_item_needs_every_value() -> None:
    both = ((7.55, "INR", 0.005), (7.54, "INR", 0.005))
    assert _status("Basic 7.55, diluted 7.54.", *both) == "correct"
    assert _status("Basic and diluted were both 7.55.", *both) == "wrong"


def test_a_failed_observation_is_scored_failed_with_its_reason() -> None:
    report = score_run(
        _bench(_REVENUE),
        [EndpointAnswer(item_id="q1", answer=None, error="timed out after 300s")],
    )
    assert report.items[0].status == "failed"
    assert report.items[0].reason == "timed out after 300s"


def test_items_never_run_are_not_scored() -> None:
    report = score_run(_bench(_REVENUE), [])
    assert report.items == ()
    assert report.overall.run == 0


def test_aggregation_counts_per_kind_and_overall() -> None:
    bench = parse_benchmark({
        "version": "v-test",
        "items": [
            {"id": f"q{i}", "kind": "fact_lookup", "question": "?",
             "gold": {"answerable": True,
                      "values": [{"value": 10.0, "unit": "INR crore", "tolerance": 0.5}]}}
            for i in range(4)
        ],
    })
    report = score_run(bench, [
        EndpointAnswer(item_id="q0", answer="10"),
        EndpointAnswer(item_id="q1", answer="10 crore"),
        EndpointAnswer(item_id="q2", answer="11"),
        EndpointAnswer(item_id="q3", answer=None, error="HTTP 500"),
    ])
    kind = report.by_kind["fact_lookup"]
    assert (kind.run, kind.correct, kind.wrong, kind.failed) == (4, 2, 1, 1)
    assert kind.accuracy == 0.5
    assert report.overall == kind
    assert report.benchmark_size == 4
