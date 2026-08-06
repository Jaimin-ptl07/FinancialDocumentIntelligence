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
                        "coverage", "confidence", "summary"}


def test_reason_carries_the_facts_through():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert out["facts"]["TCEXP"] == 1000.0


def test_reason_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes

    a = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    b = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_arbitrate_runs_once_per_reason_call(monkeypatch):
    """`_arbitrate` used to run twice per `reason()` call -- once for `facts`,
    once inside `contested_tags` -- over identical inputs for an identical
    answer. `reason` must compute it once and pass it to both."""
    import fdi.core.fog_reason as fog_reason_mod

    calls: list[str] = []
    original = fog_reason_mod._arbitrate

    def counting(*args: object, **kwargs: object) -> object:
        calls.append("call")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fog_reason_mod, "_arbitrate", counting)
    reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert len(calls) == 1


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
    assert got[0]["losers"] == [
        {"label": "Income tax assets (net)", "value": 1259.0}
    ]
    assert got[0]["winner_value"] == 4465.0
    # A large spread means one source is wrong, not that two agree.
    assert got[0]["spread"] == 3206.0


def test_a_note_exact_hit_beats_a_face_token_overlap_guess():
    """EVIDENCE OUTRANKS POSITION. Ranking origin before the evidence tier let a
    face row's fuzzy guess override a note row's exact hit -- the same wrong
    value (1,259 for NETDTA) that arbitration was introduced to prevent."""
    fog = _fog_with([
        {"id": "li:face", "parent": "st", "origin": "face",
         "label": "Income tax assets (net)", "current": 1259.0},
        {"id": "li:note", "parent": "nt", "origin": "note",
         "label": "Deferred tax assets (net)", "current": 4465.0},
    ])
    mapping = _mapping_with([
        ("li:face", "NETDTA", "lexical:name", 0.71),
        ("li:note", "NETDTA", "lexical:name", 1.0),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["NETDTA"] == 4465.0


def test_the_face_still_wins_between_equally_good_matches():
    """Position remains the tie-breaker it was meant to be."""
    fog = _fog_with([
        {"id": "li:note", "parent": "nt", "origin": "note", "label": "X", "current": 1.0},
        {"id": "li:face", "parent": "st", "origin": "face", "label": "X", "current": 2.0},
    ])
    mapping = _mapping_with([
        ("li:note", "T", "lexical:name", 1.0), ("li:face", "T", "lexical:name", 1.0)])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["T"] == 2.0


def test_corroboration_and_mis_mapping_are_distinguishable():
    """Recording only the LABELS was not enough: a face row and its note agreeing
    on 11,027, and a face row losing 11,027 to an unrelated -109, produced the
    SAME record. `spread` is what separates them."""
    from fdi.core.fog_reason import contested_tags

    agree = _fog_with([
        {"id": "li:face", "parent": "st", "origin": "face",
         "label": "Right-of-use assets", "current": 11027.0},
        {"id": "li:note", "parent": "nt", "origin": "note",
         "label": "Right of use assets total", "current": 11027.0},
    ])
    disagree = _fog_with([
        {"id": "li:face", "parent": "st", "origin": "face",
         "label": "Right-of-use assets", "current": 11027.0},
        {"id": "li:note", "parent": "nt", "origin": "note",
         "label": "Payment for acquiring right-of-use assets", "current": -109.0},
    ])
    mapping = _mapping_with([
        ("li:face", "RGHTUA", "lexical:name", 1.0),
        ("li:note", "RGHTUA", "lexical:name", 0.62),
    ])
    assert contested_tags(agree, mapping, basis="consolidated")[0]["spread"] == 0.0
    assert contested_tags(disagree, mapping, basis="consolidated")[0]["spread"] == 11136.0


def test_a_contested_tag_with_three_claimants_does_not_raise_on_sorting():
    """Dicts are not orderable; a naive sorted() over loser records raised."""
    from fdi.core.fog_reason import contested_tags

    fog = _fog_with([
        {"id": f"li:{c}", "parent": "st", "origin": "face", "label": c, "current": v}
        for c, v in (("a", 1.0), ("b", 2.0), ("c", 3.0))
    ])
    mapping = _mapping_with([(f"li:{c}", "T", "lexical:name", 0.7) for c in "abc"])
    got = contested_tags(fog, mapping, basis="consolidated")
    assert got[0]["claimants"] == 3
    assert len(got[0]["losers"]) == 2


# --- validation, confidence and coverage wiring (Task 5) -----------------------

def test_validation_runs_and_reports_its_checks():
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    assert isinstance(out["checks"], list)
    assert out["summary"] != {} or out["checks"] == []


def test_a_partial_composition_never_satisfies_a_reported_total():
    """The measured rule: an incomplete sum may not vouch for a total. Showing
    the number with its coverage is honest; letting it claim the books tie is
    not."""
    out = reason(_FOG, _MAPPING, _SCHEMA, basis="consolidated")
    for record in out["composed"].values():
        if not record["complete"]:
            assert record["coverage"] < 1.0


def _balance_sheet_fog(assets, liabilities, equity):
    return _fog_with([
        {"id": "li:a", "parent": "st", "origin": "face",
         "label": "Total assets", "current": assets},
        {"id": "li:l", "parent": "st", "origin": "face",
         "label": "Total liabilities", "current": liabilities},
        {"id": "li:e", "parent": "st", "origin": "face",
         "label": "Total equity", "current": equity},
    ])


def _balance_sheet_mapping():
    return _mapping_with([
        ("li:a", "TOTNCNCA", "lexical:name", 1.0),
        ("li:l", "TOTL", "lexical:name", 1.0),
        ("li:e", "TOTEQNR", "lexical:name", 1.0),
    ])


def test_a_balanced_sheet_passes_the_identity_check():
    """A = L + E: `validation.py`'s seeded identity should PASS, and
    calibration should record it as corroborating evidence -- not merely
    exist as an empty section."""
    fog = _balance_sheet_fog(100.0, 60.0, 40.0)
    out = reason(fog, _balance_sheet_mapping(), _SCHEMA, basis="consolidated")
    identity = next(c for c in out["checks"] if c["rule_id"] == "identity_balance_sheet")
    assert identity["passed"] is True
    assert identity["expected"] == 100.0
    assert identity["actual"] == 100.0

    conf = out["confidence"]
    assert any(
        "identity_balance_sheet holds" in s
        for tag in ("TOTNCNCA", "TOTL", "TOTEQNR")
        for s in conf[tag]["signals"]
    )


def test_an_unbalanced_sheet_fails_the_identity_check_and_is_flagged():
    """A != L + E: the identity must FAIL, and calibration must record the
    failure as a signal against every tag it names -- not stay silent."""
    fog = _balance_sheet_fog(100.0, 60.0, 30.0)
    out = reason(fog, _balance_sheet_mapping(), _SCHEMA, basis="consolidated")
    identity = next(c for c in out["checks"] if c["rule_id"] == "identity_balance_sheet")
    assert identity["passed"] is False
    assert identity["delta"] == 10.0

    conf = out["confidence"]
    assert any(
        "failed check: identity_balance_sheet" in s
        for tag in ("TOTNCNCA", "TOTL", "TOTEQNR")
        for s in conf[tag]["signals"]
    )


def test_coverage_is_genuinely_populated_from_the_schema():
    """Not just a present key -- real expected/found counts from the taxonomy
    the schema declares, against the facts this basis actually produced."""
    schema = {"modules": [{"module_id": "balance_sheet", "statement": "balance_sheet",
                           "retrieval_units": [{"unit_id": "RU", "fields": [
                               {"tag": "TOTNCNCA", "field_name": "Total assets", "sr_no": "1"},
                               {"tag": "TOTL", "field_name": "Total liabilities", "sr_no": "2"},
                               {"tag": "TOTEQNR", "field_name": "Total equity", "sr_no": "3"},
                               {"tag": "MISSING1", "field_name": "Something unreported",
                                "sr_no": "4"},
                           ]}]}]}
    fog = _balance_sheet_fog(100.0, 60.0, 40.0)
    out = reason(fog, _balance_sheet_mapping(), schema, basis="consolidated")

    totals = out["coverage"]["totals"]
    assert totals["expected"] == 4
    assert totals["found_extracted"] == 3
    assert totals["missing_extracted"] == 1
    assert "MISSING1" in out["coverage"]["missing_extracted"]


def test_a_section_confirmed_match_outranks_the_models_guess():
    """MEASURED ON TCS: the balance sheet prints 'Lease liabilities' under both
    Non-current and Current. The label matches its qualified concept at only
    0.9, so it ranked as the WEAKEST evidence and the model's guess from an
    unrelated note (tier 2) overwrote it -- LTOLR 9,453 -> 2,074 -- turning a
    free 100%/100% liabilities score into 66.7%/66.7%."""
    fog = _fog_with([
        {"id": "li:face", "parent": "st", "origin": "face",
         "label": "Lease liabilities", "current": 9453.0},
        {"id": "li:note", "parent": "nt", "origin": "note",
         "label": "Lease liabilities", "current": 2074.0},
    ])
    mapping = _mapping_with([
        ("li:face", "LTOLR", "lexical:section:name", 0.9),
        ("li:note", "LTOLR", "llm", 0.0),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["LTOLR"] == 9453.0


def test_an_unconfirmed_fuzzy_match_still_loses_to_the_model():
    """The promotion is for SECTION confirmation only -- a bare token overlap
    remains the weakest evidence, which is what stopped NETDTA being 1,259."""
    fog = _fog_with([
        {"id": "li:guess", "parent": "st", "origin": "face",
         "label": "Income tax assets (net)", "current": 1259.0},
        {"id": "li:model", "parent": "nt", "origin": "note",
         "label": "Deferred tax assets", "current": 4465.0},
    ])
    mapping = _mapping_with([
        ("li:guess", "NETDTA", "lexical:name", 0.71),
        ("li:model", "NETDTA", "llm", 0.0),
    ])
    assert facts_from_mapping(fog, mapping, basis="consolidated")["NETDTA"] == 4465.0
