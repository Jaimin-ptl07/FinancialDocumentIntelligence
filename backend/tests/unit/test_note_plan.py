"""Planning: which notes get opened, and what each is asked."""

from fdi.core.concepts import ConceptCandidate, ConceptGroup, ConceptIndex
from fdi.core.note_plan import (
    build_note_prompt,
    plan_from_face,
    read_note_reply,
    unmapped_face_labels,
)
from fdi.core.statements import FaceRow, NoteWindow


def _cand(tag, name, aliases=(), definition=""):
    return ConceptCandidate(tag=tag, name=name, field_path=name, depth=2,
                            origin="extracted", aliases=tuple(aliases),
                            definition=definition)


# "Other income" is one face line standing in for nine concepts -- the case the
# title path exists for.
OTHER_INCOME = ConceptGroup(
    key="other income", name="Other Income", statement="profit_and_loss",
    candidates=(_cand("INTINC", "Interest Income", ("interest on deposits",)),
                _cand("DIVINC", "Dividend Income"),
                _cand("RENTINC", "Rent Income", ("rental income",))),
)
EXPENSES = ConceptGroup(
    key="other expenses", name="Other Expenses", statement="profit_and_loss",
    candidates=(_cand("PWRFUEL", "Power and Fuel"), _cand("TRAVEL", "Travel Expenses")),
)
REVENUE = ConceptGroup(
    key="revenue from operations", name="Revenue from Operations",
    statement="profit_and_loss",
    candidates=(_cand("RFSP", "Sale of Products"), _cand("RFSS", "Sale of Services")),
)

INDEX = ConceptIndex(
    groups=(OTHER_INCOME, EXPENSES, REVENUE),
    by_key={"other income": OTHER_INCOME, "other expenses": EXPENSES,
            "revenue from operations": REVENUE},
    by_alias={},
)
# What the face rows themselves are matched against: the parents.
FACE_CANDIDATES = (
    _cand("OTHINC", "Other Income"),
    _cand("OTHEXP", "Other Expenses"),
    _cand("RFOGRS", "Revenue from Operations"),
)
# The parents' own groups, so index.for_tag(OTHINC) yields its children.
INDEX_WITH_PARENTS = ConceptIndex(
    groups=(
        ConceptGroup(key="other income", name="Other Income",
                     statement="profit_and_loss",
                     candidates=(_cand("OTHINC", "Other Income"),) + OTHER_INCOME.candidates),
        ConceptGroup(key="other expenses", name="Other Expenses",
                     statement="profit_and_loss",
                     candidates=(_cand("OTHEXP", "Other Expenses"),) + EXPENSES.candidates),
    ),
    by_key={"other income": OTHER_INCOME, "other expenses": EXPENSES},
    by_alias={},
)


def _win(ref, title, page, start, end):
    return NoteWindow(ref=ref, title=title, page=page, start=start, end=end)


def test_a_cited_note_is_opened_and_asked_about_that_concepts_children():
    face = [FaceRow("Other Income", "", "11", 10036.0, 9642.0)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    plan = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES)

    assert plan.facts == (("OTHINC", 10036.0),)
    assert len(plan.units) == 1
    unit = plan.units[0]
    assert unit.ref == "11" and unit.reason == "reference" and unit.serves == ("OTHINC",)
    assert {c.tag for c in unit.concepts} >= {"INTINC", "DIVINC", "RENTINC"}


def test_a_note_the_face_never_cites_is_still_opened_by_its_title():
    """TCS's face prints one 'Other income' line with no reference. Without the
    title path the nine concepts beneath it have no route to the note that
    actually lists them."""
    face = [FaceRow("Total Income", "", "", 230974.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    plan = plan_from_face(face, windows, INDEX, FACE_CANDIDATES)

    assert [u.ref for u in plan.units] == ["11"]
    assert plan.units[0].reason == "title"


def test_a_reference_the_note_index_never_found_opens_nothing():
    """absent != missed: a dangling pointer must not fabricate a unit."""
    face = [FaceRow("Other Income", "", "99", 10036.0, None)]
    plan = plan_from_face(face, {}, INDEX_WITH_PARENTS, FACE_CANDIDATES)
    assert plan.units == ()
    assert plan.facts == (("OTHINC", 10036.0),)   # the face value still stands


def test_units_are_ordered_by_position_in_the_document():
    face = [FaceRow("Other Expenses", "", "13", 43071.0, None),
            FaceRow("Other Income", "", "11", 10036.0, None)]
    windows = {"13": _win("13", "Other Expenses", 301, 9_000, 9_500),
               "11": _win("11", "Other Income", 290, 1_000, 2_000)}
    plan = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES)
    assert [u.ref for u in plan.units] == ["11", "13"]


def test_one_call_per_note_even_when_several_rows_cite_it():
    """TCS cites note 15 from both 'Current tax' and 'Deferred tax'."""
    face = [FaceRow("Other Income", "", "11", 10036.0, None),
            FaceRow("Other Expenses", "", "11", 43071.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    plan = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES)
    assert len(plan.units) == 1
    assert plan.units[0].serves == ("OTHINC", "OTHEXP")
    tags = [c.tag for c in plan.units[0].concepts]
    assert len(tags) == len(set(tags))          # merged, not duplicated


def test_face_labels_no_concept_claims_are_reported_not_dropped():
    face = [FaceRow("Provision for doubtful debts written back", "", "", 12.0, None)]
    plan = plan_from_face(face, {}, INDEX, FACE_CANDIDATES)
    assert unmapped_face_labels(plan) == ["Provision for doubtful debts written back"]


def test_prompt_offers_every_concept_with_its_report_side_wordings():
    face = [FaceRow("Other Income", "", "11", 10036.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    unit = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES).units[0]
    prompt = build_note_prompt(unit, "| Interest income | 1,833 |")

    assert "Note 11 - Other Income" in prompt
    assert "INTINC: Interest Income" in prompt
    assert "also reported as: interest on deposits" in prompt
    assert "| Interest income | 1,833 |" in prompt


def test_a_tag_the_unit_never_offered_is_dropped():
    """The model sometimes answers with a plausible concept from elsewhere in
    the taxonomy. A fact attributed to a note that does not mention it is not
    auditable."""
    face = [FaceRow("Other Income", "", "11", 10036.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    unit = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES).units[0]

    got = read_note_reply({"lines": [
        {"label": "Interest income", "tag": "INTINC", "current": 1833, "prior": 1700},
        {"label": "Freight", "tag": "GRSBLDG", "current": 99},      # not offered
        {"label": "Something", "tag": "", "current": 5},            # explicitly none
    ]}, unit)

    assert got == [("INTINC", "Interest income", 1833.0, 1700.0)]


def test_a_line_with_no_usable_number_yields_no_fact():
    face = [FaceRow("Other Income", "", "11", 10036.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    unit = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES).units[0]
    assert read_note_reply({"lines": [
        {"label": "Interest income", "tag": "INTINC", "current": None},
        {"label": "Dividend", "tag": "DIVINC", "current": "1,833"},   # unparsed text
    ]}, unit) == []


def test_reading_a_reply_never_raises_on_any_shape():
    face = [FaceRow("Other Income", "", "11", 10036.0, None)]
    windows = {"11": _win("11", "Other Income", 290, 1_000, 2_000)}
    unit = plan_from_face(face, windows, INDEX_WITH_PARENTS, FACE_CANDIDATES).units[0]
    for junk in (None, [], "text", {"lines": "no"}, {"lines": [None, 7]}, {}):
        assert read_note_reply(junk, unit) == []


def test_planning_never_raises_on_an_empty_face():
    plan = plan_from_face([], {}, INDEX, FACE_CANDIDATES)
    assert plan.facts == () and plan.units == () and plan.unmapped == ()
