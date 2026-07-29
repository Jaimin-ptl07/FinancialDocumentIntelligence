"""Note-reference index: heading scan, face-table binding, and the
missing-top-level-heading case that made TCS file PPE under the wrong note."""

from fdi.core.notes import build_note_map, face_note_refs, normalize_label


def _head(page: int, text: str) -> dict[str, object]:
    return {
        "role": "sectionHeading",
        "content": text,
        "bounding_regions": [{"page_number": page}],
    }


def _face(page: int, rows: list[tuple[str, str]]) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    for r, (label, ref) in enumerate(rows):
        cells.append({"row_index": r, "column_index": 0, "content": label})
        cells.append({"row_index": r, "column_index": 1, "content": ref})
    return {"bounding_regions": [{"page_number": page}], "cells": cells}


def test_top_level_and_sub_notes_are_indexed():
    paras = [
        _head(260, "6) Financial assets"),
        _head(267, "(c) Cash and cash equivalents"),
        _head(281, "7) Leases"),
    ]
    face = [_face(252, [("Cash and cash equivalents", "6(c)")])]
    nm = build_note_map(paras, face, face_pages=[252], page_lo=251, page_hi=320)

    assert nm.by_ref["6"].page == 260
    assert nm.by_ref["6(c)"].page == 267
    assert nm.by_ref["6(c)"].bound_by == "heading"
    assert nm.by_ref["7"].page == 281
    # a note ends where the next one begins
    assert nm.by_ref["6"].end_page == 266


def test_missing_top_level_heading_is_rebound_from_the_face_table():
    """TCS regression: the `8)` heading was never emitted, so `(a) Property,
    plant and equipment` inherited note 7 and a lookup of 8(a) would have
    returned the WRONG page. The face table is authoritative for the number."""
    paras = [
        _head(281, "7) Leases"),
        _head(283, "(a) Property, plant and equipment"),
        _head(288, "(c) Other assets"),
    ]
    face = [
        _face(252, [
            ("Property, plant and equipment", "8(a)"),
            ("Other assets", "8(c)"),
        ])
    ]
    nm = build_note_map(paras, face, face_pages=[252], page_lo=251, page_hi=320)

    assert nm.by_ref["8(a)"].page == 283
    assert nm.by_ref["8(a)"].bound_by == "title"
    assert nm.by_ref["8(c)"].page == 288
    # the misleading heading-derived ref is still present but never consulted
    assert nm.pages_for_label("Property, plant and equipment")[0] == 283


def test_sub_note_letter_must_agree_before_rebinding():
    paras = [_head(283, "(a) Property, plant and equipment")]
    face = [_face(252, [("Property, plant and equipment", "8(b)")])]
    nm = build_note_map(paras, face, face_pages=[252], page_lo=251, page_hi=320)

    assert "8(b)" not in nm.by_ref
    assert "8(b)" in nm.unresolved


def test_page_bounds_exclude_the_agm_notice_renumbering():
    """The notice after the statements restarts at 1) / 4) and would collide."""
    paras = [
        _head(294, "11) Other income"),
        _head(323, "1) Meeting through VC"),
        _head(325, "7) Mandatory Electronic Payment of Dividend"),
    ]
    nm = build_note_map(paras, [], face_pages=[252], page_lo=251, page_hi=322)

    assert nm.by_ref["11"].page == 294
    assert "1" not in nm.by_ref
    assert "7" not in nm.by_ref


def test_pages_for_label_spans_the_whole_note():
    paras = [_head(290, "9) Other equity"), _head(294, "11) Other income")]
    nm = build_note_map(paras, [], face_pages=[252], page_lo=251, page_hi=320)

    assert nm.pages_for_label("Other equity") == [290, 291, 292, 293]
    assert nm.pages_for_label("nonexistent line item") == []


def test_malformed_input_degrades_to_empty_and_never_raises():
    assert build_note_map([], [], face_pages=[], page_lo=1, page_hi=10).by_ref == {}
    junk: list[object] = [None, 42, {"role": "sectionHeading"}, {"content": "x"}]
    nm = build_note_map(junk, junk, face_pages=[1], page_lo=1, page_hi=10)
    assert nm.by_ref == {}
    assert nm.pages_for_ref(None) == []
    assert face_note_refs([None, 7], [1]) == []


def test_normalize_label_is_punctuation_insensitive():
    assert normalize_label("Property, plant and equipment") == normalize_label(
        "Property plant and equipment"
    )
    assert normalize_label(None) == ""


def test_find_statement_pages_bounds_at_the_agm_notice():
    from fdi.core.notes import find_statement_pages

    paras = [
        _head(252, "Standalone Balance Sheet"),
        _head(253, "Standalone Statement of Profit and Loss"),
        _head(294, "11) Other income"),
        _head(323, "Notice of Annual General Meeting"),
    ]
    sp = find_statement_pages(paras)
    assert sp.face_pages == [252, 253]
    assert sp.page_lo == 252
    assert sp.page_hi == 322


def test_find_statement_pages_degrades_when_absent():
    from fdi.core.notes import find_statement_pages

    assert find_statement_pages([]).face_pages == []
    assert find_statement_pages([None, 3]).page_hi == 0
