"""Face rows become persistent objects, and the note column becomes REFERENCES
edges -- printed navigation the issuer asserted, never a similarity guess."""

from fdi.core.fog_build import (
    build_note_index,
    build_statement_index,
    face_line_items,
)


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_FACE = {
    "bounding_regions": [{"page_number": 150}],
    "spans": [{"offset": 500, "length": 100}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"),
        _cell(0, 2, "2025-26"), _cell(0, 3, "2024-25"),
        _cell(1, 0, "INCOME"),
        _cell(2, 0, "Revenue from operations"), _cell(2, 1, "10"),
        _cell(2, 2, "2,55,324"), _cell(2, 3, "2,40,893"),
        _cell(3, 0, "Total income"), _cell(3, 2, "100"), _cell(3, 3, "95"),
        _cell(4, 0, "Total expenses"), _cell(4, 2, "70"), _cell(4, 3, "65"),
        _cell(5, 0, "Profit before tax"), _cell(5, 2, "30"), _cell(5, 3, "30"),
    ],
}

_NOTE_HEAD = {
    "content": "10. Revenue from operations",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 170}],
    "spans": [{"offset": 9000, "length": 27}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}
_OCR = {"paragraphs": [_NOTE_HEAD], "tables": [_FACE]}


def _build():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    return face_line_items(_OCR, si, ni, basis="consolidated")


def test_every_valued_face_row_becomes_a_line_item():
    items, _ = _build()
    labels = [i.label for i in items]
    assert "Revenue from operations" in labels
    assert "Profit before tax" in labels


def test_indian_digit_grouping_is_parsed_deterministically():
    """'2,55,324' is 255324 -- the model never interprets a separator."""
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.current == 255324.0
    assert revenue.prior == 240893.0


def test_a_row_with_no_amount_is_a_caption_not_a_line_item():
    """RIL prints 'a) Owners of the Company' under three different captions;
    mapping caption rows as values produced several wrong assignments."""
    items, _ = _build()
    assert all(i.label != "INCOME" for i in items)
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.caption == "INCOME"


def test_the_printed_note_column_becomes_a_references_edge():
    items, edges = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    refs = [e for e in edges if e.type == "REFERENCES"]
    assert len(refs) == 1
    assert refs[0].source == revenue.id
    assert refs[0].target == "nt:consolidated:10"


def test_a_row_with_no_printed_ref_gets_no_edge_absent_not_missed():
    items, edges = _build()
    pbt = next(i for i in items if i.label == "Profit before tax")
    assert pbt.ref == ""
    assert not [e for e in edges if e.source == pbt.id and e.type == "REFERENCES"]


def test_a_ref_that_resolves_to_no_note_yields_no_edge():
    """A dangling pointer must not create an edge to a node that is not there."""
    ocr = {"paragraphs": [], "tables": [_FACE]}
    si = build_statement_index(ocr, _BASIS)
    ni = build_note_index(ocr, _BASIS)
    _, edges = face_line_items(ocr, si, ni, basis="consolidated")
    assert not [e for e in edges if e.type == "REFERENCES"]


def test_every_line_item_is_contained_by_its_statement():
    items, edges = _build()
    contains = {e.target for e in edges if e.type == "CONTAINS"}
    assert {i.id for i in items} == contains


def test_provenance_is_carried_on_every_item():
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.page == 150
    assert revenue.table_index == 0
    # `parse_face_table` yields only VALUED rows, so this counts position within
    # that filtered sequence -- not the raw table row. The header and the
    # "INCOME" caption row carry no amount, so revenue is the first item.
    assert revenue.row_index == 0
    # ...which is precisely why it cannot address a cell. `core/provenance.py`
    # indexes the RAW grid, where revenue is row 2 (header 0, "INCOME" 1).
    assert revenue.source_row == 2
    assert revenue.origin == "face"


def test_ids_are_unique_across_the_whole_face():
    items, _ = _build()
    assert len({i.id for i in items}) == len(items)


def test_a_basis_with_no_statements_yields_nothing_not_a_crash():
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    items, edges = face_line_items(_OCR, si, ni, basis="standalone")
    assert items == []
    assert edges == []


def test_malformed_input_never_raises():
    assert face_line_items({}, {}, {}, basis="consolidated") == ([], [])


def test_a_face_row_carries_its_quantity_kind():
    items, _ = _build()
    revenue = next(i for i in items if i.label == "Revenue from operations")
    assert revenue.kind == "currency"
