"""Resolution order is memory -> lexical -> LLM. The first two are free and
deterministic, so a second run of the same document costs less and returns the
same answer."""

from fdi.core.concepts import ConceptCandidate
from fdi.core.fog_mapping import (
    MapUnit,
    Resolution,
    mapping_edges,
    resolve_from_memory,
    resolve_lexical,
    units_from_fog,
)
from fdi.core.memory import InMemoryStore, MemoryKey, observe


def _c(tag: str, name: str, aliases: tuple[str, ...] = ()) -> ConceptCandidate:
    return ConceptCandidate(
        tag=tag, name=name, field_path=f"pl.{tag}", depth=1,
        origin="extracted", aliases=aliases,
    )


_CANDS = (
    _c("TCEXP", "Travel and conveyance", ("travel expenses",)),
    _c("OTEXP", "Other expenses"),
)
_UNIT = MapUnit(
    unit_id="nt:consolidated:13",
    kind="note",
    title="Other expenses",
    labels=("Travel expenses", "Facility expenses"),
    candidates=_CANDS,
)


def test_lexical_resolves_an_exact_alias():
    got = resolve_lexical(_UNIT)
    assert got["Travel expenses"].tag == "TCEXP"
    assert got["Travel expenses"].method.startswith("lexical")


def test_lexical_leaves_an_unknown_label_alone_rather_than_guessing():
    assert "Facility expenses" not in resolve_lexical(_UNIT)


def test_memory_resolves_a_label_seen_on_a_previous_document():
    store = InMemoryStore()
    # `recall` returns only TRUSTED entries, so the observation must be
    # confirmed -- one unconfirmed sighting is deliberately not enough.
    observe(
        store,
        MemoryKey.of("Facility expenses", "Other expenses"),
        "OTEXP",
        confidence=1.0,
        method="semantic",
        confirmed=True,
    )
    got = resolve_from_memory(_UNIT, store)
    assert got["Facility expenses"].tag == "OTEXP"
    assert got["Facility expenses"].method == "memory"


def test_memory_returns_nothing_for_an_unseen_label():
    assert resolve_from_memory(_UNIT, InMemoryStore()) == {}


def test_the_memory_key_is_scoped_by_basis_not_just_label_and_section():
    """"Computer equipment" under PPE is a fixed asset, "Computer software"
    under intangibles is not -- and CONSOLIDATED and STANDALONE are different
    numbers for the same concept, the same class of confusion. A mapping
    learned on one basis must never answer for a label under another."""
    facexp = _c("FACEXP", "Facility costs")
    store = InMemoryStore()
    observe(
        store,
        MemoryKey.of("Facility expenses", "Other expenses", "consolidated"),
        "OTEXP", confidence=1.0, method="semantic", confirmed=True,
    )
    observe(
        store,
        MemoryKey.of("Facility expenses", "Other expenses", "standalone"),
        "FACEXP", confidence=1.0, method="semantic", confirmed=True,
    )
    consolidated_unit = MapUnit(
        unit_id="nt:consolidated:13", kind="note", title="Other expenses",
        labels=("Facility expenses",), candidates=(*_CANDS, facexp),
        basis="consolidated",
    )
    standalone_unit = MapUnit(
        unit_id="nt:standalone:13", kind="note", title="Other expenses",
        labels=("Facility expenses",), candidates=(*_CANDS, facexp),
        basis="standalone",
    )
    assert resolve_from_memory(consolidated_unit, store)["Facility expenses"].tag == "OTEXP"
    assert resolve_from_memory(standalone_unit, store)["Facility expenses"].tag == "FACEXP"


def test_memory_never_returns_a_tag_the_unit_does_not_offer():
    """A remembered mapping from another statement must not leak in."""
    store = InMemoryStore()
    observe(
        store,
        MemoryKey.of("Travel expenses", "Other expenses"),
        "GRSPNE",
        confidence=1.0,
        method="semantic",
        confirmed=True,
    )
    assert resolve_from_memory(_UNIT, store) == {}


def test_units_from_fog_makes_one_unit_per_statement_and_note():
    region = {
        "statements": [{"id": "st:consolidated:profit_and_loss",
                        "statement": "profit_and_loss"}],
        "notes": [{"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [
            {"id": "li:a", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Other expenses", "current": 35230.0},
            {"id": "li:b", "parent": "nt:consolidated:13",
             "origin": "note", "label": "Travel expenses", "current": 1000.0},
        ],
        "edges": [],
    }
    units = units_from_fog(
        region, face_candidates=_CANDS, note_candidates=_CANDS
    )
    by_id = {u.unit_id: u for u in units}
    assert by_id["st:consolidated:profit_and_loss"].kind == "face"
    assert by_id["st:consolidated:profit_and_loss"].labels == ("Other expenses",)
    assert by_id["nt:consolidated:13"].kind == "note"
    assert by_id["nt:consolidated:13"].labels == ("Travel expenses",)


def test_units_from_fog_carries_each_line_items_id():
    """`item_ids` is what lets two rows sharing one label resolve
    independently -- `units_from_fog` must populate it from the same items
    it already walks to build `labels`."""
    region = {
        "statements": [], "notes": [
            {"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [
            {"id": "li:b", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Travel expenses", "current": 1000.0},
            {"id": "li:c", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Travel expenses", "current": 500.0},
        ],
        "edges": [],
    }
    units = units_from_fog(region, face_candidates=_CANDS, note_candidates=_CANDS)
    assert units[0].item_ids == ("li:b", "li:c")


def test_units_from_fog_carries_the_basis_onto_every_unit():
    """`resolve_from_memory` scopes its lookups by `unit.basis` -- so the
    basis a region belongs to must reach the units it produces."""
    region = {
        "statements": [], "notes": [
            {"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [{"id": "li:b", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses", "current": 1000.0}],
        "edges": [],
    }
    units = units_from_fog(
        region, face_candidates=_CANDS, note_candidates=_CANDS, basis="standalone"
    )
    assert units[0].basis == "standalone"


def test_a_unit_with_no_line_items_is_not_emitted():
    """An empty unit would cost an LLM call and answer nothing."""
    region = {
        "statements": [],
        "notes": [{"id": "nt:consolidated:99", "ref": "99", "title": "Empty"}],
        "line_items": [],
        "edges": [],
    }
    assert units_from_fog(region, face_candidates=_CANDS, note_candidates=_CANDS) == []


def test_units_are_ordered_deterministically():
    region = {
        "statements": [],
        "notes": [
            {"id": "nt:consolidated:9", "ref": "9", "title": "B"},
            {"id": "nt:consolidated:13", "ref": "13", "title": "A"},
        ],
        "line_items": [
            {"id": "li:1", "parent": "nt:consolidated:9", "origin": "note",
             "label": "x", "current": 1.0},
            {"id": "li:2", "parent": "nt:consolidated:13", "origin": "note",
             "label": "y", "current": 2.0},
        ],
        "edges": [],
    }
    ids = [u.unit_id for u in units_from_fog(
        region, face_candidates=_CANDS, note_candidates=_CANDS)]
    assert ids == sorted(ids)


def test_mapping_edges_link_the_line_item_to_its_concept():
    """`resolutions` is keyed by ITEM ID (`row_ids(unit)`'s normal path), not
    by label -- `mapping_edges` looks a line item up by its own id."""
    region = {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:b", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
        "edges": [],
    }
    resolutions = {
        "nt:consolidated:13": {
            "li:b": Resolution("Travel expenses", "TCEXP", "lexical", 1.0)
        }
    }
    edges, proof = mapping_edges(resolutions, region)
    assert len(edges) == 1
    assert edges[0].type == "MAPS_TO"
    assert edges[0].source == "li:b"
    assert edges[0].target == "TCEXP"
    assert proof[0]["method"] == "lexical"
    assert proof[0]["line_item"] == "li:b"


def test_edges_are_sorted_so_the_artifact_hashes_stably():
    region = {
        "statements": [], "notes": [],
        "line_items": [
            {"id": "li:z", "parent": "n", "origin": "note", "label": "B", "current": 1.0},
            {"id": "li:a", "parent": "n", "origin": "note", "label": "A", "current": 2.0},
        ],
        "edges": [],
    }
    resolutions = {"n": {
        "li:a": Resolution("A", "TCEXP", "lexical", 1.0),
        "li:z": Resolution("B", "OTEXP", "llm", 0.8),
    }}
    edges, _ = mapping_edges(resolutions, region)
    keys = [(e.source, e.target) for e in edges]
    assert keys == sorted(keys)


def test_two_items_sharing_a_label_resolve_to_two_different_edges():
    """MEASURED ON TCS: 'Lease liabilities' prints twice under two different
    sections and two different item ids. Both must produce their OWN edge --
    the failure this rekey fixes is the second overwriting the first."""
    region = {
        "statements": [], "notes": [],
        "line_items": [
            {"id": "li:ltolr", "parent": "st", "origin": "face",
             "label": "Lease liabilities", "current": 9453.0},
            {"id": "li:stll", "parent": "st", "origin": "face",
             "label": "Lease liabilities", "current": 1830.0},
        ],
        "edges": [],
    }
    resolutions = {"st": {
        "li:ltolr": Resolution("Lease liabilities", "LTOLR", "lexical", 1.0),
        "li:stll": Resolution("Lease liabilities", "STLL", "lexical", 1.0),
    }}
    edges, proof = mapping_edges(resolutions, region)
    targets = {e.source: e.target for e in edges}
    assert targets == {"li:ltolr": "LTOLR", "li:stll": "STLL"}
    assert len(proof) == 2


def test_a_resolution_for_a_line_item_that_does_not_exist_is_dropped():
    region = {"statements": [], "notes": [], "line_items": [], "edges": []}
    resolutions = {"n": {"li:missing": Resolution("A", "TCEXP", "llm", 0.5)}}
    edges, proof = mapping_edges(resolutions, region)
    assert edges == []
    assert proof == []


def test_malformed_input_never_raises():
    assert units_from_fog({}, face_candidates=(), note_candidates=()) == []
    assert mapping_edges({}, {}) == ([], [])


def test_a_count_is_never_mapped_onto_a_currency_concept():
    """MEASURED: the weighted average share count 361,80,87,518 won EQWVR
    (equity share capital, 362) with a contested spread of 3.6 billion. Both are
    valid numbers, so arbitration cannot separate them -- only the KIND can."""
    unit = MapUnit(
        unit_id="nt:consolidated:18", kind="note", title="Earnings per share",
        labels=("Weighted average number of equity shares",),
        kinds=("count",),
        candidates=(_c("EQWVR", "Equity share capital with voting rights",
                       ("weighted average number of equity shares",)),),
    )
    assert resolve_lexical(unit) == {}


def test_a_currency_line_still_maps_onto_a_currency_concept():
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Share capital",), kinds=("currency",),
        candidates=(_c("EQWVR", "Equity share capital with voting rights",
                       ("share capital",)),),
    )
    assert resolve_lexical(unit)["Share capital"].tag == "EQWVR"


def test_a_weak_token_overlap_is_refused_rather_than_guessed():
    """Below the floor, silence beats a guess: an unmapped label is visible and
    a wrong value is not."""
    from fdi.core.fog_mapping import LEXICAL_FLOOR

    assert LEXICAL_FLOOR > 0.5
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Something only faintly related",), kinds=("currency",),
        candidates=(_c("TCEXP", "Travel and conveyance"),),
    )
    assert resolve_lexical(unit) == {}


def test_an_exact_hit_is_never_refused_by_the_floor():
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), kinds=("currency",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"


def test_an_unknown_kind_is_permitted_not_refused():
    """A row we could not classify must not become unmappable -- that would turn
    a classification gap into silent data loss."""
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), kinds=("unknown",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"


def test_a_section_qualifier_picks_the_right_concept_of_a_matching_pair():
    """MEASURED ON TCS: 'Lease liabilities' appears under both Non-current and
    Current liabilities, and the taxonomy has a concept for each. The label
    alone is a tie; the section breaks it, for free."""
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Non-current liabilities", "Financial liabilities"),),
    )
    assert resolve_lexical(unit)["Lease liabilities"].tag == "LTOLR"


def test_the_other_section_picks_the_other_concept():
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("LTOLR", "Lease liabilities - non-current"),
                    _c("STLL", "Lease liabilities - current")),
        sections=(("Current liabilities", "Financial liabilities"),),
    )
    assert resolve_lexical(unit)["Lease liabilities"].tag == "STLL"


def test_a_contradicting_section_refuses_rather_than_guessing():
    """If the only candidate's qualifier contradicts the section, silence beats
    a confident wrong answer."""
    unit = MapUnit(
        unit_id="st", kind="face", title="Balance sheet",
        labels=("Lease liabilities",),
        candidates=(_c("STLL", "Lease liabilities - current"),),
        sections=(("Non-current liabilities",),),
    )
    assert resolve_lexical(unit) == {}


def test_no_section_leaves_existing_behaviour_untouched():
    unit = MapUnit(
        unit_id="u", kind="note", title="t",
        labels=("Travel expenses",), candidates=_CANDS,
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"


def test_an_unqualified_TOTAL_does_not_steal_a_qualified_component():
    """MEASURED ON TCS: the balance sheet prints 'Lease liabilities' twice, under
    Non-current (9,453) and Current (1,830). The taxonomy holds LTOLR, STLL AND
    `LL` -- the unqualified TOTAL, an exact name match scoring 1.000 against the
    qualified pair's 0.900. Both component rows resolved to the total and
    NEITHER split tag resolved. Vetoing contradictions cannot help: `LL` has no
    qualifier to contradict."""
    cands = (
        _c("LL", "Lease liabilities"),
        _c("LTOLR", "Lease liabilities - non-current"),
        _c("STLL", "Lease liabilities - current"),
    )
    non_current = MapUnit(
        unit_id="st", kind="face", title="BS", labels=("Lease liabilities",),
        candidates=cands, sections=(("Non-current liabilities", "Financial liabilities"),),
    )
    current = MapUnit(
        unit_id="st", kind="face", title="BS", labels=("Lease liabilities",),
        candidates=cands, sections=(("Current liabilities", "Financial liabilities"),),
    )
    assert resolve_lexical(non_current)["Lease liabilities"].tag == "LTOLR"
    assert resolve_lexical(current)["Lease liabilities"].tag == "STLL"


def test_an_unqualified_concept_still_wins_when_the_section_says_nothing():
    """A row outside any period block must still reach the total."""
    cands = (
        _c("LL", "Lease liabilities"),
        _c("LTOLR", "Lease liabilities - non-current"),
    )
    unit = MapUnit(
        unit_id="st", kind="face", title="BS", labels=("Lease liabilities",),
        candidates=cands, sections=((),),
    )
    assert resolve_lexical(unit)["Lease liabilities"].tag == "LL"


def test_a_section_the_taxonomy_does_not_split_on_still_resolves():
    """Falling back to the full set keeps such a row mappable, and the
    contradiction veto still applies so the wrong period cannot win."""
    unit = MapUnit(
        unit_id="st", kind="face", title="BS", labels=("Travel expenses",),
        candidates=_CANDS, sections=(("Non-current liabilities",),),
    )
    assert resolve_lexical(unit)["Travel expenses"].tag == "TCEXP"


def _cd(tag: str, name: str, aliases: tuple[str, ...] = (), origin: str = "extracted"):
    from fdi.core.concepts import ConceptCandidate

    return ConceptCandidate(tag=tag, name=name, field_path=f"x.{tag}", depth=1,
                            origin=origin, aliases=aliases)


def test_a_printed_total_maps_to_the_AS_REPORTED_tag_not_the_derived_one():
    """MEASURED ON TCS: 'Total non-current assets' 46,667 resolved to TOTNCA
    (DERIVED) instead of TOTNCR (as reported) because both carry the same alias
    and the derived one is declared first. That puts a READ value where a
    COMPUTED one belongs, leaves the reported slot empty, and destroys the
    reported-vs-computed reconciliation the pair exists for."""
    cands = (
        _cd("TOTNCA", "Total Non-Current Assets", ("total non-current assets",), "derived"),
        _cd("TOTNCR", "Total Non-Current Assets (as reported)", ("total non-current assets",)),
    )
    unit = MapUnit(unit_id="st", kind="face", title="BS",
                   labels=("Total non-current assets",), candidates=cands)
    got = resolve_lexical(unit)
    assert got["Total non-current assets"].tag == "TOTNCR"


def test_a_concept_the_taxonomy_models_ONLY_as_derived_stays_reachable():
    """TOTEQ correctly wins 'Share capital' -- there is no reported counterpart,
    and refusing derived outright would lose the value entirely."""
    cands = (_cd("TOTEQ", "Total Share Capital", ("share capital",), "derived"),)
    unit = MapUnit(unit_id="st", kind="face", title="BS",
                   labels=("Share capital",), candidates=cands)
    assert resolve_lexical(unit)["Share capital"].tag == "TOTEQ"


def test_the_preference_applies_inside_a_section_confirmed_match_too():
    cands = (
        _cd("TOTNCA", "Total Non-Current Assets",
            ("total non-current assets",), "derived"),
        _cd("TOTNCR", "Total Non-Current Assets (as reported)",
            ("total non-current assets",)),
    )
    unit = MapUnit(unit_id="st", kind="face", title="BS",
                   labels=("Total non-current assets",),
                   candidates=cands, sections=(("Non-current assets",),))
    assert resolve_lexical(unit)["Total non-current assets"].tag == "TOTNCR"


def test_only_the_DECLARED_twin_may_take_a_derived_hit():
    """The swap once took ANY extracted concept within the margin. Measured
    on TCS that retagged 108 rows, DESTROYED 14 facts (EEBEXP 1,54,994,
    TOTFC 1,227, TOTTAXEXP 16,033) and CORRUPTED 5 more (CFTAX -14,384 ->
    16,033, another line's value). Face-level ground truth scored none of
    those tags, so the gate stayed green while the graph rotted."""
    cands = (
        _cd("TOTEQ", "Total Share Capital", ("share capital",), "derived"),
        _cd("EQWVR", "Equity share capital with voting rights",
            ("equity share capital",)),
    )
    unit = MapUnit(unit_id="st", kind="face", title="BS",
                   labels=("Share capital",), candidates=cands)
    # EQWVR is NOT "Total Share Capital (as reported)" -- it is a different
    # concept that merely scores nearby, so it must never take the hit.
    assert resolve_lexical(unit)["Share capital"].tag == "TOTEQ"


def test_a_same_named_concept_from_another_statement_is_not_a_twin():
    """MEASURED: "Finance costs" is the name of BOTH the derived TOTFC and the
    cash-flow CFFIN. Treating name equality as the twin relationship moved
    1,227 onto the wrong statement's concept and lost TOTFC entirely."""
    cands = (
        _cd("TOTFC", "Finance costs", (), "derived"),
        _cd("CFFIN", "Finance costs", ()),
    )
    unit = MapUnit(unit_id="st", kind="face", title="P&L",
                   labels=("Finance costs",), candidates=cands)
    assert resolve_lexical(unit)["Finance costs"].tag == "TOTFC"
