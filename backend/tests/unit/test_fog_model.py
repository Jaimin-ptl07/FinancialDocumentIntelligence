"""The FOG's shapes. Ids must be STABLE and content-addressable: a stage
artifact whose ids move between identical runs breaks caching silently."""

import pytest

from fdi.core.fog import (
    EDGE_TYPES,
    Edge,
    LineItem,
    build_fog_artifact,
    edge_body,
    line_item_body,
    line_item_id,
)

_ITEM = LineItem(
    id="li:st:consolidated:profit_and_loss:r4",
    parent="st:consolidated:profit_and_loss",
    origin="face",
    label="Revenue from operations",
    caption="",
    ref="10",
    current=255324.0,
    prior=240893.0,
    page=253,
    table_index=88,
    row_index=4,
)


def test_the_line_item_id_is_derived_from_its_parent_and_row():
    assert line_item_id("st:consolidated:profit_and_loss", 4) == (
        "li:st:consolidated:profit_and_loss:r4"
    )


def test_the_same_inputs_always_produce_the_same_id():
    assert line_item_id("nt:standalone:13(b)", 7) == line_item_id("nt:standalone:13(b)", 7)


def test_line_item_body_round_trips_every_field():
    body = line_item_body(_ITEM)
    assert body["label"] == "Revenue from operations"
    assert body["current"] == 255324.0
    assert body["prior"] == 240893.0
    assert body["origin"] == "face"
    assert body["ref"] == "10"
    assert body["row_index"] == 4


def test_a_null_amount_stays_null_not_zero():
    item = LineItem(
        id="x", parent="p", origin="note", label="Land", caption="", ref="",
        current=None, prior=None, page=1, table_index=0, row_index=0,
    )
    body = line_item_body(item)
    assert body["current"] is None
    assert body["prior"] is None


def test_edge_body_names_source_and_target_explicitly():
    body = edge_body(Edge("REFERENCES", "li:a", "nt:consolidated:8(a)"))
    assert body == {"type": "REFERENCES", "source": "li:a", "target": "nt:consolidated:8(a)"}


def test_the_edge_vocabulary_is_closed_and_includes_the_reserved_types():
    assert "CONTAINS" in EDGE_TYPES
    assert "REFERENCES" in EDGE_TYPES
    assert "MAPS_TO" in EDGE_TYPES
    assert "GOVERNED_BY" in EDGE_TYPES  # reserved for domain 2, never emitted here


def test_the_artifact_keys_everything_by_basis():
    artifact = build_fog_artifact(
        {"consolidated": ([{"id": "st:consolidated:profit_and_loss"}], [], [_ITEM],
                          [Edge("CONTAINS", "st:consolidated:profit_and_loss", _ITEM.id)])},
        doc_id="doc-1",
        diagnostics={},
    )
    region = artifact["bases"]["consolidated"]
    assert region["statements"][0]["id"] == "st:consolidated:profit_and_loss"
    assert region["line_items"][0]["label"] == "Revenue from operations"
    assert region["edges"][0]["type"] == "CONTAINS"


def test_the_artifact_carries_its_doc_id_and_schema_version():
    artifact = build_fog_artifact({}, doc_id="doc-1", diagnostics={})
    assert artifact["doc_id"] == "doc-1"
    assert artifact["schema_version"] == 1


def test_edges_are_sorted_so_the_artifact_hashes_stably():
    edges = [
        Edge("REFERENCES", "li:b", "nt:x"),
        Edge("CONTAINS", "st:a", "li:b"),
        Edge("CONTAINS", "st:a", "li:a"),
    ]
    artifact = build_fog_artifact(
        {"consolidated": ([], [], [], edges)}, doc_id="d", diagnostics={}
    )
    edges_out = artifact["bases"]["consolidated"]["edges"]
    got = [(e["type"], e["source"], e["target"]) for e in edges_out]
    assert got == sorted(got)


def test_an_unknown_edge_type_is_rejected_loudly():
    """A typo'd edge type would silently produce a graph nothing traverses."""
    with pytest.raises(ValueError, match="unknown edge type"):
        edge_body(Edge("POINTS_AT", "a", "b"))


def test_an_empty_document_is_a_valid_artifact_not_a_crash():
    artifact = build_fog_artifact({}, doc_id="d", diagnostics={})
    assert artifact["bases"] == {}
