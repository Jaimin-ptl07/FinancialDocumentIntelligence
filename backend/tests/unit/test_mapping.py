"""Push-model mapping: place the REPORT's lines on taxonomy concepts, and let
the parent total prove the assignment."""

from fdi.core.concepts import ConceptCandidate
from fdi.core.mapping import (
    ReportLine,
    arbitrate,
    lexical_match,
    map_lines,
    to_records,
    unmapped_labels,
)


def _c(tag: str, name: str, aliases: tuple[str, ...] = ()) -> ConceptCandidate:
    return ConceptCandidate(tag=tag, name=name, field_path=f"assets.{tag}",
                            depth=1, origin="extracted", aliases=aliases)


PPE = [
    _c("GRSLAND", "Land", ("freehold land",)),
    _c("GRSBLDG", "Buildings"),
    _c("GRSPNE", "Plant and equipment", ("computer equipment",)),
    _c("GRSOTTA", "Other tangible assets"),
]


def test_exact_name_and_alias_both_resolve_without_a_model():
    cand, method, score = lexical_match("Buildings", PPE)
    assert (cand.tag, method) == ("GRSBLDG", "name") and score == 1.0

    # the issuer's wording, not ours -- this is the whole point
    cand, method, score = lexical_match("Computer equipment", PPE)
    assert (cand.tag, method) == ("GRSPNE", "alias") and score == 0.95


def test_ambiguous_label_is_refused_rather_than_guessed():
    pair = [_c("A", "Other assets"), _c("B", "Other assets")]
    cand, method, _ = lexical_match("Other assets", pair)
    # first exact hit wins for a true duplicate...
    assert cand is not None
    # ...but an equal-scoring token tie is refused
    tie = [_c("A", "Deposits with banks"), _c("B", "Deposits with others")]
    cand, method, _ = lexical_match("Deposits with", tie)
    assert cand is None and method == "unmapped"


def test_every_line_gets_a_decision_and_unknowns_are_visible():
    lines = [
        ReportLine("Freehold land", 323.0),
        ReportLine("Buildings", 8685.0),
        ReportLine("Right-of-use assets", 9245.0),  # belongs to no PPE sibling
    ]
    rows = map_lines(lines, PPE)

    assert [r.tag for r in rows] == ["GRSLAND", "GRSBLDG", ""]
    assert unmapped_labels(rows) == ["Right-of-use assets"]
    # nothing is silently invisible: the unmapped line still carries its words
    assert rows[2].source_label == "Right-of-use assets"


def test_a_concept_is_not_claimed_twice():
    lines = [ReportLine("Buildings", 10.0), ReportLine("Buildings", 20.0)]
    rows = map_lines(lines, PPE)
    assert rows[0].tag == "GRSBLDG"
    assert rows[1].tag == ""  # left for the semantic pass, not double-assigned


def test_arbiter_corroborates_when_children_tie_to_the_parent():
    rows = map_lines(
        [ReportLine("Land", 323.0), ReportLine("Buildings", 8685.0)], PPE
    )
    verdict = arbitrate(rows, 9008.0)

    assert verdict.status == "corroborated" and verdict.accepted
    assert verdict.delta == 0.0


def test_arbiter_repairs_when_the_residual_names_one_orphan():
    """The gap equals exactly one unmapped line, so that line IS the child."""
    rows = map_lines(
        [
            ReportLine("Land", 323.0),
            ReportLine("Buildings", 8685.0),
            ReportLine("Assorted fittings", 500.0),  # no lexical home
        ],
        PPE,
    )
    verdict = arbitrate(rows, 9508.0, fallback_tag="GRSOTTA")

    assert verdict.status == "repaired" and verdict.accepted
    repaired = next(r for r in verdict.rows if r.source_label == "Assorted fittings")
    assert repaired.tag == "GRSOTTA"
    assert repaired.method == "residual"
    assert "residual" in repaired.note

    # proving something is missing does not say WHAT it is: no bucket, no repair
    assert arbitrate(rows, 9508.0).status == "unreconciled"


def test_arbiter_flags_rather_than_silently_committing_a_broken_sum():
    rows = map_lines(
        [ReportLine("Land", 323.0), ReportLine("Buildings", 8685.0)], PPE
    )
    verdict = arbitrate(rows, 12000.0)

    assert verdict.status == "unreconciled"
    assert verdict.accepted is False
    assert verdict.delta == 2992.0


def test_no_parent_total_leaves_mappings_unproven_not_wrong():
    rows = map_lines([ReportLine("Land", 323.0)], PPE)
    verdict = arbitrate(rows, None)
    assert verdict.status == "no_total" and verdict.accepted is False


def test_records_always_retain_the_issuers_own_wording():
    rows = map_lines([ReportLine("Freehold land", 323.0, page=285)], PPE)
    record = to_records(rows)[0]

    assert record["tag"] == "GRSLAND"
    assert record["source_label"] == "Freehold land"  # audit trail
    assert record["mapping_method"] == "alias"
    assert record["page"] == 285


def test_mapping_is_total_on_empty_and_junk_input():
    assert map_lines([], PPE) == []
    assert lexical_match("", PPE)[0] is None
    assert arbitrate([], None).status == "no_total"
    assert to_records([]) == []


def test_arbiter_aggregates_several_orphans_that_sum_to_the_residual():
    """Real TCS case: the issuer splits PPE into Computer equipment, Office
    equipment and Electrical installations where the taxonomy carries one
    "Other tangible assets" bucket. Their sum tying to the residual IS the
    proof that they belong there."""
    rows = map_lines(
        [
            ReportLine("Buildings", 3785.0),
            ReportLine("Computer equipment", 2109.0),
            ReportLine("Office equipment", 427.0),
            ReportLine("Electrical installations", 388.0),
        ],
        [_c("GRSBLDG", "Buildings"), _c("GRSOTTA", "Other tangible assets")],
    )
    verdict = arbitrate(rows, 6709.0, fallback_tag="GRSOTTA")

    assert verdict.status == "repaired" and verdict.accepted
    assigned = [r for r in verdict.rows if r.tag == "GRSOTTA"]
    assert len(assigned) == 3
    assert {r.source_label for r in assigned} == {
        "Computer equipment", "Office equipment", "Electrical installations"
    }


def test_aggregate_repair_needs_a_bucket_and_will_not_invent_one():
    rows = map_lines(
        [ReportLine("Buildings", 10.0), ReportLine("Odd one", 3.0),
         ReportLine("Another", 2.0)],
        [_c("GRSBLDG", "Buildings")],
    )
    verdict = arbitrate(rows, 15.0)  # no fallback_tag supplied
    assert verdict.status == "unreconciled"
