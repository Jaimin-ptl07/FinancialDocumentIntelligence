"""A caption is LAST-SEEN and overwriting, so a run of caption rows collapses to
the innermost and the outer section is lost.

MEASURED ON TCS: the balance sheet prints `Liabilities` / `Non-current
liabilities` / `Financial liabilities` as three consecutive amount-less rows.
Both the non-current lease liability (9,453) and the current one (1,830) ended
up with caption 'Financial liabilities', so nothing could tell them apart and
LTOLR resolved to 2,077 from an unrelated note."""

from fdi.core.statements import parse_face_table


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


_BS = {
    "cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2026"),
        _cell(1, 0, "EQUITY AND LIABILITIES"),
        _cell(2, 0, "Equity"),
        _cell(3, 0, "Share capital"), _cell(3, 2, "362"),
        _cell(4, 0, "Liabilities"),
        _cell(5, 0, "Non-current liabilities"),
        _cell(6, 0, "Financial liabilities"),
        _cell(7, 0, "Lease liabilities"), _cell(7, 2, "9,453"),
        _cell(8, 0, "Current liabilities"),
        _cell(9, 0, "Financial liabilities"),
        _cell(10, 0, "Lease liabilities"), _cell(10, 2, "1,830"),
    ],
}


def _rows():
    return {(r.label, r.current): r for r in parse_face_table(_BS)}


def test_two_rows_sharing_a_label_get_different_section_paths():
    rows = _rows()
    non_current = rows[("Lease liabilities", 9453.0)]
    current = rows[("Lease liabilities", 1830.0)]
    assert non_current.section_path != current.section_path


def test_the_path_keeps_the_OUTER_section_the_caption_discarded():
    rows = _rows()
    assert "Non-current liabilities" in rows[("Lease liabilities", 9453.0)].section_path
    assert "Current liabilities" in rows[("Lease liabilities", 1830.0)].section_path


def test_the_path_is_outermost_first():
    path = _rows()[("Lease liabilities", 9453.0)].section_path
    assert path.index("Non-current liabilities") < path.index("Financial liabilities")


def test_the_nearest_caption_is_still_available_unchanged():
    """`caption` keeps its existing meaning; the path is additive."""
    assert _rows()[("Lease liabilities", 9453.0)].caption == "Financial liabilities"


def test_a_sibling_section_does_not_leak_into_the_next():
    """Once `Current liabilities` opens, `Non-current liabilities` must be gone."""
    assert "Non-current liabilities" not in _rows()[("Lease liabilities", 1830.0)].section_path


def test_a_row_under_no_caption_has_an_empty_path():
    table = {"cells": [
        _cell(0, 0, "Particulars"), _cell(0, 1, "2026"),
        _cell(1, 0, "Revenue"), _cell(1, 1, "100"),
    ]}
    assert parse_face_table(table)[0].section_path == ()


def test_a_deeper_caption_does_not_pop_a_shallower_one():
    """Depth is unknown from a flat table, so the path only ever GROWS until a
    known top-level section resets it -- guessing nesting would be worse than
    carrying an extra level."""
    rows = _rows()
    assert "Financial liabilities" in rows[("Lease liabilities", 9453.0)].section_path


def test_malformed_input_never_raises():
    assert parse_face_table({}) == []
    assert parse_face_table({"cells": "nope"}) == []
