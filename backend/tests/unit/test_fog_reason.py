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


def test_reason_returns_every_section():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert set(out) == {"facts", "contested", "composed", "checks",
                        "coverage", "summary"}


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


# --- arbitration: several line items may claim one tag -------------------------

def _fog_with(items):
    return {"bases": {"consolidated": {
        "statements": [], "notes": [], "line_items": items, "edges": []}}}


def _mapping_with(pairs):
    """pairs: [(line_item_id, tag, method, score)]"""
    return {"bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": i, "target": t} for i, t, _, _ in pairs],
        "proof": [{"line_item": i, "tag": t, "method": m, "score": s}
                  for i, t, m, s in pairs],
        "unmapped": [],
    }}}


def test_the_face_beats_a_cash_flow_line_claiming_the_same_tag():
    """MEASURED ON TCS: 'Right-of-use assets' 11,027 on the balance sheet lost
    to 'Payment including advances for acquiring right-of-use assets' -109,
    purely because the latter sorted later."""
    fog = _fog_with([
        {"id": "li:st:consolidated:balance_sheet:r2", "parent": "st", "origin": "face",
         "label": "Right-of-use assets", "current": 11027.0},
        {"id": "li:st:consolidated:cash_flow:r33", "parent": "st", "origin": "face",
         "label": "Payment including advances for acquiring right-of-use assets",
         "current": -109.0},
    ])
    mapping = _mapping_with([
        ("li:st:consolidated:balance_sheet:r2", "RGHTUA", "lexical:name", 1.0),
        ("li:st:consolidated:cash_flow:r33", "RGHTUA", "lexical:name", 0.62),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["RGHTUA"] == 11027.0


def test_an_exact_hit_beats_a_token_overlap_guess():
    """MEASURED ON TCS: 'Income tax assets (net)' overlapped onto NETDTA
    (Deferred Tax Assets) and overwrote 4,465 with 1,259. `lexical_match`
    returns method "name" for BOTH, so only the SCORE separates them."""
    fog = _fog_with([
        {"id": "li:a", "parent": "st", "origin": "face",
         "label": "Deferred tax assets (net)", "current": 4465.0},
        {"id": "li:z", "parent": "st", "origin": "face",
         "label": "Income tax assets (net)", "current": 1259.0},
    ])
    mapping = _mapping_with([
        ("li:a", "NETDTA", "lexical:name", 1.0),
        ("li:z", "NETDTA", "lexical:name", 0.71),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["NETDTA"] == 4465.0


def test_a_face_row_beats_a_note_row():
    fog = _fog_with([
        {"id": "li:note", "parent": "nt", "origin": "note",
         "label": "Realised loss on equity shares", "current": -5.0},
        {"id": "li:face", "parent": "st", "origin": "face",
         "label": "Share capital", "current": 362.0},
    ])
    mapping = _mapping_with([
        ("li:note", "EQWVR", "lexical:name", 0.8),
        ("li:face", "EQWVR", "lexical:name", 0.8),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["EQWVR"] == 362.0


def test_the_model_beats_a_token_overlap_guess():
    """A considered answer outranks a fuzzy string coincidence -- the semantic
    tier is what took alias-only mapping from 69% to 90%."""
    fog = _fog_with([
        {"id": "li:guess", "parent": "nt", "origin": "note",
         "label": "Something vaguely similar", "current": 1.0},
        {"id": "li:model", "parent": "nt", "origin": "note",
         "label": "Facility expenses", "current": 2966.0},
    ])
    mapping = _mapping_with([
        ("li:guess", "FACEXP", "lexical:name", 0.7),
        ("li:model", "FACEXP", "llm", 0.0),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["FACEXP"] == 2966.0


def test_arbitration_is_stable_across_runs():
    """Two equally-ranked claims must not alternate -- this reaches an artifact."""
    fog = _fog_with([
        {"id": "li:b", "parent": "st", "origin": "face", "label": "X", "current": 2.0},
        {"id": "li:a", "parent": "st", "origin": "face", "label": "X", "current": 1.0},
    ])
    mapping = _mapping_with([
        ("li:b", "T", "lexical:name", 1.0), ("li:a", "T", "lexical:name", 1.0)])
    first = facts_from_mapping(fog, mapping, basis="consolidated")["T"]
    assert first == facts_from_mapping(fog, mapping, basis="consolidated")["T"]


def test_a_single_claimant_is_not_reported_as_contested():
    from fdi.core.fog_reason import contested_tags

    fog = _fog_with([{"id": "li:a", "parent": "st", "origin": "face",
                      "label": "X", "current": 1.0}])
    mapping = _mapping_with([("li:a", "T", "lexical:name", 1.0)])
    assert contested_tags(fog, mapping, basis="consolidated") == []


def test_a_contested_tag_names_its_winner_and_its_losers():
    """Corroboration and mis-mapping look identical from the fact map alone."""
    from fdi.core.fog_reason import contested_tags

    fog = _fog_with([
        {"id": "li:a", "parent": "st", "origin": "face",
         "label": "Deferred tax assets (net)", "current": 4465.0},
        {"id": "li:z", "parent": "st", "origin": "face",
         "label": "Income tax assets (net)", "current": 1259.0},
    ])
    mapping = _mapping_with([
        ("li:a", "NETDTA", "lexical:name", 1.0),
        ("li:z", "NETDTA", "lexical:name", 0.71),
    ])
    got = contested_tags(fog, mapping, basis="consolidated")
    assert got[0]["tag"] == "NETDTA"
    assert got[0]["claimants"] == 2
    assert got[0]["winner_label"] == "Deferred tax assets (net)"
    assert got[0]["losers"] == ["Income tax assets (net)"]
