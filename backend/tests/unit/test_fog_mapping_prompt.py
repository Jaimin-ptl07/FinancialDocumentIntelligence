"""The push model: hand the model a unit's OWN labels and ask which concept each
is -- never "what is the value of GRSPNE?". Unseen vocabulary stops being fatal,
and a label with no home surfaces as unmapped instead of vanishing."""

from fdi.core.concepts import ConceptCandidate
from fdi.core.fog_mapping import MapUnit, build_map_prompt, read_map_reply


def _c(tag: str, name: str, aliases: tuple[str, ...] = ()) -> ConceptCandidate:
    """ConceptCandidate requires field_path/depth/origin as well as tag/name --
    a positional-looking constructor here would not compile."""
    return ConceptCandidate(
        tag=tag, name=name, field_path=f"pl.{tag}", depth=1,
        origin="extracted", aliases=aliases,
    )


_UNIT = MapUnit(
    unit_id="nt:consolidated:13",
    kind="note",
    title="Other expenses",
    labels=("Facility expenses", "Travel expenses", "Total"),
    candidates=(
        _c("TCEXP", "Travel and conveyance", ("travel",)),
        _c("OTEXP", "Other expenses"),
    ),
)


def test_the_prompt_carries_every_label_the_unit_holds():
    _system, user, _dropped = build_map_prompt(_UNIT)
    assert "Facility expenses" in user
    assert "Travel expenses" in user


def test_the_prompt_carries_every_candidate_tag_and_name():
    _system, user, _dropped = build_map_prompt(_UNIT)
    assert "TCEXP" in user
    assert "Travel and conveyance" in user


def test_the_prompt_names_the_unit_so_the_model_knows_the_context():
    _system, user, _dropped = build_map_prompt(_UNIT)
    assert "Other expenses" in user
    assert "nt:consolidated:13" in user


def test_the_system_prompt_forbids_inventing_values():
    """The mapper assigns CONCEPTS. Values are already parsed deterministically,
    and letting the model restate one reintroduces the comma bug."""
    system, _user, _dropped = build_map_prompt(_UNIT)
    assert "value" in system.lower()
    assert "unmapped" in system.lower()


def test_a_unit_within_the_cap_drops_nothing():
    """MAX_CANDIDATES is 40 (fdi.core.concepts); `_UNIT` offers 2."""
    _system, _user, dropped = build_map_prompt(_UNIT)
    assert dropped == 0


def test_candidates_beyond_the_cap_are_dropped_and_the_drop_is_reported():
    """MEASURED: an uncapped note-candidate set ran to ~519 concepts and
    ~12.3k tokens/unit -- `build_map_prompt` was the one place that bypassed
    the `MAX_CANDIDATES` cap every other candidate-set builder respects. A
    silent cap would read as "considered everything"; `dropped` is what makes
    the truncation visible to a caller."""
    from fdi.core.concepts import MAX_CANDIDATES

    many = tuple(
        _c(f"T{i}", f"Concept {i}") for i in range(MAX_CANDIDATES + 5)
    )
    unit = MapUnit("u", "note", "t", ("Label",), many)
    _system, user, dropped = build_map_prompt(unit)
    assert dropped == 5
    assert f"T{MAX_CANDIDATES - 1}" in user
    assert f"T{MAX_CANDIDATES}" not in user


def test_a_reply_maps_row_numbers_to_tags():
    """`_UNIT` has no `item_ids`, so `row_ids` falls back to the label -- the
    reply itself is still keyed by 1-based ROW NUMBER: "Travel expenses" is
    row 2."""
    got = read_map_reply({"2": "TCEXP"}, _UNIT)
    assert got == {"Travel expenses": "TCEXP"}


def test_a_tag_the_unit_never_offered_is_dropped():
    """The model occasionally answers with a plausible concept from elsewhere in
    the taxonomy. A fact attributed to a note that does not mention it is
    unauditable."""
    assert read_map_reply({"2": "GRSPNE"}, _UNIT) == {}


def test_a_row_number_the_unit_never_held_is_dropped():
    """`_UNIT` has 3 rows; row "9" does not exist, so there is no key for the
    reply to arrive on."""
    assert read_map_reply({"9": "TCEXP"}, _UNIT) == {}


def test_an_explicit_unmapped_marker_is_honoured_not_guessed():
    assert read_map_reply({"1": "unmapped"}, _UNIT) == {}


def test_a_null_or_empty_assignment_is_dropped():
    assert read_map_reply({"1": None}, _UNIT) == {}
    assert read_map_reply({"1": ""}, _UNIT) == {}


def test_two_rows_may_not_claim_the_same_tag():
    """One concept, one line. Otherwise a note's components double-count into
    the same tag and its total can never arbitrate them."""
    got = read_map_reply({"2": "TCEXP", "1": "TCEXP"}, _UNIT)
    assert list(got.values()) == ["TCEXP"]
    assert len(got) == 1


def test_a_malformed_reply_yields_nothing_and_never_raises():
    assert read_map_reply(None, _UNIT) == {}
    assert read_map_reply("not a mapping", _UNIT) == {}
    assert read_map_reply({"a": {"nested": 1}}, _UNIT) == {}


def test_a_unit_with_no_candidates_maps_nothing():
    empty = MapUnit("u", "note", "t", ("Label",), ())
    assert read_map_reply({"1": "TCEXP"}, empty) == {}


def test_the_prompt_survives_a_candidate_with_no_aliases():
    """Most taxonomy leaves have none; rendering must not crash or print None."""
    _system, user, _dropped = build_map_prompt(
        MapUnit("u", "note", "t", ("Label",), (_c("OTEXP", "Other expenses"),))
    )
    assert "OTEXP" in user
    assert "None" not in user


def test_the_cap_keeps_the_RELEVANT_candidates_not_the_first_declared():
    """The cap trims ~519 concepts to 40. Truncating in schema DECLARATION order
    leaves the model answering from a set that is 92% blind, and the right
    concept is almost never in it -- a confidently wrong answer from a blind
    shortlist is worse than the cost the cap was saving."""
    from fdi.core.concepts import MAX_CANDIDATES

    noise = tuple(
        _c(f"NOISE{i}", f"Wholly unrelated concept {i}")
        for i in range(MAX_CANDIDATES + 20)
    )
    target = _c("TCEXP", "Travel and conveyance", ("travel expenses",))
    unit = MapUnit(
        unit_id="u", kind="note", title="Other expenses",
        labels=("Travel expenses",),
        candidates=(*noise, target),   # declared LAST, far beyond the cap
    )
    _system, user, dropped = build_map_prompt(unit)
    assert "TCEXP" in user
    assert dropped == len(noise) + 1 - MAX_CANDIDATES


def test_the_capped_prompt_is_deterministic():
    """It feeds a prompt whose answer reaches a content-addressed artifact."""
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",),
        candidates=tuple(_c(f"T{i}", f"Concept {i}") for i in range(60)),
    )
    assert build_map_prompt(unit) == build_map_prompt(unit)


def test_a_unit_with_no_labels_keeps_declaration_order():
    unit = MapUnit("u", "note", "t", (), (_c("A", "Alpha"), _c("B", "Beta")))
    _system, user, _dropped = build_map_prompt(unit)
    assert user.index("A") < user.index("B")


def test_the_prompt_shows_the_section_of_each_label():
    """MEASURED ON TCS: eight line items are labelled exactly 'Lease
    liabilities'. Without its section, the model is asked to distinguish rows
    that are literally identical strings, and LTOLR resolved to 2,077 instead
    of 9,453."""
    unit = MapUnit(
        unit_id="st:consolidated:balance_sheet", kind="face", title="Balance sheet",
        labels=("Lease liabilities", "Lease liabilities"),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities", "Financial liabilities"),
                  ("Current liabilities", "Financial liabilities")),
    )
    _system, user, _dropped = build_map_prompt(unit)
    assert "Non-current liabilities" in user
    assert "Current liabilities" in user


def test_a_label_with_no_section_still_renders():
    unit = MapUnit("u", "note", "t", ("Travel",), (_c("TCEXP", "Travel"),))
    _system, user, _dropped = build_map_prompt(unit)
    assert "Travel" in user


def test_two_identical_labels_are_rendered_as_two_distinct_numbered_rows():
    """MEASURED ON TCS: 'Lease liabilities' prints twice -- once under
    Non-current liabilities (9,453), once under Current (1,830). A prompt
    that lists bare labels gives the model no way to tell the two rows
    apart; the row NUMBER is what makes them addressable independently."""
    unit = MapUnit(
        unit_id="st:consolidated:balance_sheet", kind="face", title="Balance sheet",
        labels=("Lease liabilities", "Lease liabilities"),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities",), ("Current liabilities",)),
        item_ids=("li:ltolr", "li:stll"),
    )
    _system, user, _dropped = build_map_prompt(unit)
    assert "1. Lease liabilities" in user
    assert "2. Lease liabilities" in user
    assert "Non-current liabilities" in user
    assert "Current liabilities" in user


def test_a_reply_assigning_different_tags_to_two_identical_labels_is_honoured():
    """The whole point of the rekey: two rows sharing one caption must be able
    to resolve to two DIFFERENT concepts, keyed by item id rather than by the
    label they happen to share."""
    unit = MapUnit(
        unit_id="st:consolidated:balance_sheet", kind="face", title="Balance sheet",
        labels=("Lease liabilities", "Lease liabilities"),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities",), ("Current liabilities",)),
        item_ids=("li:ltolr", "li:stll"),
    )
    got = read_map_reply({"1": "LTOLR", "2": "STLL"}, unit)
    assert got == {"li:ltolr": "LTOLR", "li:stll": "STLL"}
