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
    _system, user = build_map_prompt(_UNIT)
    assert "Facility expenses" in user
    assert "Travel expenses" in user


def test_the_prompt_carries_every_candidate_tag_and_name():
    _system, user = build_map_prompt(_UNIT)
    assert "TCEXP" in user
    assert "Travel and conveyance" in user


def test_the_prompt_names_the_unit_so_the_model_knows_the_context():
    _system, user = build_map_prompt(_UNIT)
    assert "Other expenses" in user
    assert "nt:consolidated:13" in user


def test_the_system_prompt_forbids_inventing_values():
    """The mapper assigns CONCEPTS. Values are already parsed deterministically,
    and letting the model restate one reintroduces the comma bug."""
    system, _user = build_map_prompt(_UNIT)
    assert "value" in system.lower()
    assert "unmapped" in system.lower()


def test_a_reply_maps_labels_to_tags():
    got = read_map_reply({"Travel expenses": "TCEXP"}, _UNIT)
    assert got == {"Travel expenses": "TCEXP"}


def test_a_tag_the_unit_never_offered_is_dropped():
    """The model occasionally answers with a plausible concept from elsewhere in
    the taxonomy. A fact attributed to a note that does not mention it is
    unauditable."""
    assert read_map_reply({"Travel expenses": "GRSPNE"}, _UNIT) == {}


def test_a_label_the_unit_never_held_is_dropped():
    assert read_map_reply({"Invented line": "TCEXP"}, _UNIT) == {}


def test_an_explicit_unmapped_marker_is_honoured_not_guessed():
    assert read_map_reply({"Facility expenses": "unmapped"}, _UNIT) == {}


def test_a_null_or_empty_assignment_is_dropped():
    assert read_map_reply({"Facility expenses": None}, _UNIT) == {}
    assert read_map_reply({"Facility expenses": ""}, _UNIT) == {}


def test_two_labels_may_not_claim_the_same_tag():
    """One concept, one line. Otherwise a note's components double-count into
    the same tag and its total can never arbitrate them."""
    got = read_map_reply(
        {"Travel expenses": "TCEXP", "Facility expenses": "TCEXP"}, _UNIT
    )
    assert list(got.values()) == ["TCEXP"]
    assert len(got) == 1


def test_a_malformed_reply_yields_nothing_and_never_raises():
    assert read_map_reply(None, _UNIT) == {}
    assert read_map_reply("not a mapping", _UNIT) == {}
    assert read_map_reply({"a": {"nested": 1}}, _UNIT) == {}


def test_a_unit_with_no_candidates_maps_nothing():
    empty = MapUnit("u", "note", "t", ("Label",), ())
    assert read_map_reply({"Label": "TCEXP"}, empty) == {}


def test_the_prompt_survives_a_candidate_with_no_aliases():
    """Most taxonomy leaves have none; rendering must not crash or print None."""
    _system, user = build_map_prompt(
        MapUnit("u", "note", "t", ("Label",), (_c("OTEXP", "Other expenses"),))
    )
    assert "OTEXP" in user
    assert "None" not in user
