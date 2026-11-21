"""Composition: reported wins, partial sums are marked, ties must be conclusive."""

from fdi.core.compose import (
    Formula,
    certify,
    compose,
    read_formulas,
    reconcile,
    summarise,
)


def _f(tag, *inputs, kind="derivation"):
    return Formula(tag, kind, tuple((i, 1.0) for i in inputs))


def test_a_complete_sum_is_derived_with_full_coverage():
    got = compose({"A": 10.0, "B": 5.0}, {"P": _f("P", "A", "B")})
    assert got["P"].value == 15.0
    assert got["P"].origin == "derived" and got["P"].complete
    assert got["P"].coverage == 1.0


def test_a_partial_sum_is_computed_and_marked_never_dropped():
    """REGRESSION. Requiring every input took TCS derivation from 10 facts to 1,
    and the full end-to-end run derived 0 of 293 balance-sheet concepts with 11
    parents holding some-but-not-all inputs."""
    got = compose({"A": 10.0}, {"P": _f("P", "A", "B", "C")})

    assert got["P"].value == 10.0
    assert got["P"].origin == "partial"
    assert got["P"].present == 1 and got["P"].total == 3
    assert got["P"].missing == ("B", "C")
    assert not got["P"].complete


def test_a_parent_with_no_inputs_at_all_yields_nothing():
    """absent != missed, and an empty sum is not zero."""
    assert compose({}, {"P": _f("P", "A", "B")}) == {}


def test_reported_wins_over_its_own_formula():
    """TCS prints DEPEXP 4,288 while both its children are null."""
    got = compose({"DEPEXP": 4288.0, "DEP": 1.0}, {"DEPEXP": _f("DEPEXP", "DEP", "AMT")})
    assert "DEPEXP" not in got          # never recomputed
    checks = reconcile({"DEPEXP": 4288.0, "DEP": 1.0},
                       {"DEPEXP": _f("DEPEXP", "DEP", "AMT")}, got)
    assert checks[0].reported == 4288.0 and not checks[0].ties


def test_chains_settle_regardless_of_declaration_order():
    formulas = {"TOP": _f("TOP", "MID", "X"), "MID": _f("MID", "A", "B")}
    got = compose({"A": 1.0, "B": 2.0, "X": 4.0}, formulas)
    assert got["MID"].value == 3.0
    assert got["TOP"].value == 7.0 and got["TOP"].complete


def test_a_complete_answer_is_preferred_over_an_early_partial_one():
    """MID is only derivable on a later pass; TOP must not lock in as partial."""
    formulas = {"TOP": _f("TOP", "MID", "X"), "MID": _f("MID", "A", "B")}
    got = compose({"A": 1.0, "B": 2.0, "X": 4.0}, formulas)
    assert got["TOP"].origin == "derived"


def test_signed_weights_subtract():
    formulas = {"ACDEPR": Formula("ACDEPR", "waterfall",
                                  (("GRS", 1.0), ("NET", -1.0)))}
    got = compose({"GRS": 100.0, "NET": 60.0}, formulas)
    assert got["ACDEPR"].value == 40.0


def test_an_uncertified_partial_cannot_clear_a_reported_total():
    """Without the issuer's own figure to tie against, agreeing on half the
    inputs is evidence we did not read the other half."""
    reported = {"P": 99.0, "A": 10.0}
    formulas = {"P": _f("P", "A", "B", "C")}
    check = reconcile(reported, formulas, compose(reported, formulas))[0]

    assert check.ties is False
    assert check.conclusive is False
    assert summarise([check])["inconclusive"] == 1


def test_summarise_separates_real_gaps_from_unread_inputs():
    reported = {"P": 99.0, "A": 10.0, "B": 5.0, "Q": 10.0, "C": 1.0}
    formulas = {"P": _f("P", "A", "B"), "Q": _f("Q", "C", "D")}
    checks = reconcile(reported, formulas, compose(reported, formulas))

    assert summarise(checks) == {
        "checks": 2, "conclusive": 1, "conclusive_ties": 0,
        "conclusive_gaps": 1, "inconclusive": 1,
    }


def test_read_formulas_understands_all_three_kinds_and_signed_inputs():
    schema = {"modules": [{"retrieval_units": [{"fields": [
        {"tag": "P", "derivation": {"method": "sum", "inputs": ["A", "B"]},
         "fields": [{"tag": "A"}, {"tag": "B"}]},
        {"tag": "W", "waterfall": {"method": "sum",
                                   "inputs": [{"tag": "P", "weight": 1},
                                              {"tag": "T", "weight": -1}]}},
        {"tag": "R", "reconciliation": {"method": "sum", "inputs": ["W"]}},
    ]}]}]}
    got = read_formulas(schema)

    assert got["P"].kind == "derivation" and got["P"].inputs == (("A", 1.0), ("B", 1.0))
    assert got["W"].inputs == (("P", 1.0), ("T", -1.0))
    assert got["R"].kind == "reconciliation"


def test_a_reconciliation_node_is_never_itself_composed():
    """It exists to CHECK a reported number, not to invent one."""
    formulas = {"R": Formula("R", "reconciliation", (("W", 1.0),))}
    assert compose({"W": 5.0}, formulas) == {}


def test_everything_is_total_on_junk():
    assert read_formulas({}) == {}
    assert read_formulas({"modules": "no"}) == {}
    assert read_formulas({"modules": [None, {"retrieval_units": [7]}]}) == {}
    assert compose({}, {}) == {}
    assert reconcile({}, {}, {}) == []
    assert summarise([])["checks"] == 0


def test_coverage_propagates_through_a_partial_input():
    """REGRESSION. A parent whose inputs are themselves half-built partials used
    to report 100% coverage merely because both inputs existed -- on TCS `TOTA`
    claimed a CONCLUSIVE gap (110,270 vs 31,873) while both inputs were partial.
    A conclusion drawn from partial evidence is not a conclusion."""
    formulas = {
        "TOTA": _f("TOTA", "SUB1", "SUB2"),
        "SUB1": _f("SUB1", "A", "B"),        # only A present -> 50%
        "SUB2": _f("SUB2", "C", "D"),        # only C present -> 50%
    }
    got = compose({"A": 10.0, "C": 20.0}, formulas)

    assert got["SUB1"].coverage == 0.5
    assert got["TOTA"].coverage == 0.5       # not 1.0
    assert got["TOTA"].origin == "partial"


def test_a_check_over_partial_inputs_is_not_conclusive():
    formulas = {"TOTA": _f("TOTA", "SUB1"), "SUB1": _f("SUB1", "A", "B")}
    reported = {"TOTA": 110270.0, "A": 10.0}
    check = reconcile(reported, formulas, compose(reported, formulas))[0]

    assert check.conclusive is False
    assert summarise([check])["conclusive_gaps"] == 0


def test_a_fully_supported_chain_stays_conclusive():
    formulas = {"TOP": _f("TOP", "MID"), "MID": _f("MID", "A", "B")}
    reported = {"TOP": 3.0, "A": 1.0, "B": 2.0}
    check = reconcile(reported, formulas, compose(reported, formulas))[0]

    assert check.coverage == 1.0 and check.conclusive and check.ties


def test_a_reported_total_that_ties_proves_the_missing_children_absent():
    """The report certifies itself. TCS's `NONOI` declares nine children, eight
    of which a services company never reports; holding the one that applies it
    read 11% coverage and was called inconclusive -- when it was 100% of what
    TCS could ever report. A tie against the issuer's own figure is that proof,
    and needs no hand-maintained applicability list."""
    reported = {"P": 10.0, "A": 10.0}
    formulas = {"P": _f("P", "A", "B", "C")}
    got = compose(reported, formulas)
    got, certified = certify(reported, formulas, got)
    check = reconcile(reported, formulas, got, certified=certified)[0]

    assert "P" in certified
    assert check.ties and check.certified and check.conclusive
    assert summarise([check])["conclusive_ties"] == 1


def test_certification_propagates_to_the_children_that_contributed():
    """A certified total also certifies the partial children beneath it: their
    own unfound children are equally proven absent."""
    formulas = {"TOP": _f("TOP", "MID"), "MID": _f("MID", "A", "B")}
    reported = {"TOP": 10.0, "A": 10.0}
    got = compose(reported, formulas)
    assert got["MID"].coverage == 0.5

    got, certified = certify(reported, formulas, got)
    assert "MID" in certified
    assert got["MID"].coverage == 1.0 and got["MID"].origin == "certified"


def test_a_total_that_does_not_tie_certifies_nothing():
    """Certification is a proof, not an amnesty for gaps."""
    reported = {"P": 99.0, "A": 10.0}
    formulas = {"P": _f("P", "A", "B")}
    got, certified = certify(reported, formulas, compose(reported, formulas))

    assert certified == set()
    check = reconcile(reported, formulas, got, certified=certified)[0]
    assert not check.conclusive and not check.ties


def test_nothing_is_certified_when_every_input_was_already_present():
    """There is nothing to prove absent, so the coverage rule already covers it."""
    reported = {"P": 15.0, "A": 10.0, "B": 5.0}
    formulas = {"P": _f("P", "A", "B")}
    got, certified = certify(reported, formulas, compose(reported, formulas))

    assert certified == set()
    assert reconcile(reported, formulas, got, certified=certified)[0].conclusive


def test_certification_is_total_on_junk():
    got, certified = certify({}, {}, {})
    assert got == {} and certified == set()
