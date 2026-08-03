"""Note rows become objects too. Today they vanish; as LineItems they are
visible, and the note's own total becomes the arbiter for its components."""

from fdi.core.fog_build import build_note_index, note_line_items


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_NOTE_TABLE = {
    "bounding_regions": [{"page_number": 301}],
    "spans": [{"offset": 2150, "length": 200}],
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"), _cell(0, 2, "2024-25"),
        _cell(1, 0, "Facility expenses"), _cell(1, 1, "2,966"), _cell(1, 2, "2,700"),
        _cell(2, 0, "Travel expenses"), _cell(2, 1, "(468)"), _cell(2, 2, "500"),
        _cell(3, 0, "Communication"), _cell(3, 1, "-"), _cell(3, 2, "-"),
        _cell(4, 0, "Total"), _cell(4, 1, "2,498"), _cell(4, 2, "3,200"),
    ],
}

_HEAD = {
    "content": "13. Other expenses",
    "role": "sectionHeading",
    "bounding_regions": [{"page_number": 301}],
    "spans": [{"offset": 2000, "length": 18}],
}

_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 300, "end_page": 400}],
}
_OCR = {"paragraphs": [_HEAD], "tables": [_NOTE_TABLE]}


def _build():
    ni = build_note_index(_OCR, _BASIS)
    return note_line_items(_OCR, ni, basis="consolidated")


def test_every_valued_note_row_becomes_a_line_item():
    items, _ = _build()
    labels = [i.label for i in items]
    assert "Facility expenses" in labels
    assert "Travel expenses" in labels


def test_an_unmapped_disclosure_survives_instead_of_vanishing():
    """'Facility expenses' has no taxonomy home. It must still be a node."""
    items, _ = _build()
    facility = next(i for i in items if i.label == "Facility expenses")
    assert facility.current == 2966.0


def test_brackets_are_negative():
    items, _ = _build()
    travel = next(i for i in items if i.label == "Travel expenses")
    assert travel.current == -468.0


def test_a_dash_row_is_skipped_it_carries_no_figure():
    items, _ = _build()
    assert all(i.label != "Communication" for i in items)


def test_the_note_total_is_kept_it_is_the_arbiter():
    items, _ = _build()
    total = next(i for i in items if i.label == "Total")
    assert total.current == 2498.0


def test_note_items_are_contained_by_their_note():
    items, edges = _build()
    contains = [e for e in edges if e.type == "CONTAINS"]
    assert all(e.source == "nt:consolidated:13" for e in contains)
    assert {e.target for e in contains} == {i.id for i in items}


def test_the_origin_marks_them_as_note_rows_not_face_rows():
    """Face rows match PARENT concepts, note rows match DESCENDANTS. Conflating
    the two left 15 of TCS's 28 face rows unmapped."""
    items, _ = _build()
    assert all(i.origin == "note" for i in items)


def test_provenance_is_carried():
    items, _ = _build()
    facility = next(i for i in items if i.label == "Facility expenses")
    assert facility.page == 301
    assert facility.table_index == 0


def test_ids_are_unique_across_notes_sharing_a_page():
    """TCS p301 holds two notes; a page-derived id would collide."""
    items, _ = _build()
    assert len({i.id for i in items}) == len(items)


def test_a_note_with_no_tables_yields_nothing():
    ocr = {"paragraphs": [_HEAD], "tables": []}
    ni = build_note_index(ocr, _BASIS)
    assert note_line_items(ocr, ni, basis="consolidated") == ([], [])


def test_malformed_input_never_raises():
    assert note_line_items({}, {}, basis="consolidated") == ([], [])
