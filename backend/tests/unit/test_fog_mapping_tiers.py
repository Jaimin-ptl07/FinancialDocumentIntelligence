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
    region = {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:b", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
        "edges": [],
    }
    resolutions = {
        "nt:consolidated:13": {
            "Travel expenses": Resolution("Travel expenses", "TCEXP", "lexical", 1.0)
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
        "A": Resolution("A", "TCEXP", "lexical", 1.0),
        "B": Resolution("B", "OTEXP", "llm", 0.8),
    }}
    edges, _ = mapping_edges(resolutions, region)
    keys = [(e.source, e.target) for e in edges]
    assert keys == sorted(keys)


def test_a_resolution_for_a_line_item_that_does_not_exist_is_dropped():
    region = {"statements": [], "notes": [], "line_items": [], "edges": []}
    resolutions = {"n": {"A": Resolution("A", "TCEXP", "llm", 0.5)}}
    edges, proof = mapping_edges(resolutions, region)
    assert edges == []
    assert proof == []


def test_malformed_input_never_raises():
    assert units_from_fog({}, face_candidates=(), note_candidates=()) == []
    assert mapping_edges({}, {}) == ([], [])
