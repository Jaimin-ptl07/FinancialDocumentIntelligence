"""Pure and free. Every input is already on the graph; nothing here calls a
model, so a rule change re-runs at zero marginal cost."""

from fdi.core.fog_reason import facts_from_mapping, reason

_FOG = {
    "bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [
            {"id": "li:a", "parent": "n", "origin": "note", "label": "Travel",
             "current": 1000.0, "prior": 900.0},
            {"id": "li:b", "parent": "n", "origin": "note", "label": "Facility",
             "current": 2966.0, "prior": 2700.0},
            {"id": "li:c", "parent": "n", "origin": "note", "label": "No figure",
             "current": None, "prior": None},
        ],
        "edges": [],
    }},
}
_MAPPING = {
    "bases": {"consolidated": {
        "edges": [
            {"type": "MAPS_TO", "source": "li:a", "target": "TCEXP"},
            {"type": "MAPS_TO", "source": "li:b", "target": "FACEXP"},
            {"type": "MAPS_TO", "source": "li:c", "target": "NILEXP"},
        ],
        "proof": [], "unmapped": [],
    }},
}
_SCHEMA = {"modules": [{"module_id": "profit_and_loss", "statement": "profit_and_loss",
                        "retrieval_units": [{"unit_id": "RU", "fields": []}]}]}


def test_a_mapped_line_item_becomes_a_fact_keyed_by_tag():
    facts = facts_from_mapping(_FOG, _MAPPING, basis="consolidated")
    assert facts["TCEXP"] == 1000.0
    assert facts["FACEXP"] == 2966.0


def test_a_line_item_with_no_figure_produces_no_fact():
    """A disclosed nil is not a zero. Emitting 0.0 would let it satisfy a
    total's reconciliation and claim the books tie."""
    facts = facts_from_mapping(_FOG, _MAPPING, basis="consolidated")
    assert "NILEXP" not in facts


def test_an_unmapped_line_item_produces_no_fact():
    fog = {"bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:x", "parent": "n", "origin": "note",
                        "label": "Orphan", "current": 5.0}],
        "edges": [],
    }}}
    mapping = {"bases": {"consolidated": {"edges": [], "proof": [], "unmapped": ["Orphan"]}}}
    assert facts_from_mapping(fog, mapping, basis="consolidated") == {}


def test_an_edge_to_a_missing_line_item_is_ignored():
    mapping = {"bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": "li:ghost", "target": "TCEXP"}],
        "proof": [], "unmapped": [],
    }}}
    assert facts_from_mapping(_FOG, mapping, basis="consolidated") == {}


def test_a_basis_that_does_not_exist_yields_no_facts():
    assert facts_from_mapping(_FOG, _MAPPING, basis="standalone") == {}


def test_reason_returns_all_five_sections():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert set(out) == {"facts", "composed", "checks", "coverage", "summary"}


def test_reason_carries_the_facts_through():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert out["facts"]["TCEXP"] == 1000.0


def test_reason_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes

    a = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    b = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_malformed_input_never_raises():
    assert facts_from_mapping({}, {}, basis="consolidated") == {}
    out = reason({}, {}, {}, basis="consolidated")
    assert out["facts"] == {}
