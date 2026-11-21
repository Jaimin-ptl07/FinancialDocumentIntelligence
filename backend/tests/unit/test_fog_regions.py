"""A basis may own MORE THAN ONE page region, and the FOG must survive it.

`core/basis.py` collapses only CONSECUTIVE same-basis anchors, so a filing that
prints [standalone statements, consolidated statements, standalone annexure]
yields `[standalone, consolidated, standalone]`. Every consumer used to assume
basis was a unique key, which produced duplicate node ids, duplicate CONTAINS
edges, and notes that vanished without any diagnostic saying so.
"""

from fdi.core.fog_build import build_fog, build_note_index, build_statement_index


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


def _pl_table(page: int, offset: int, pbt: str = "30") -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
            _cell(1, 0, "Revenue from operations"), _cell(1, 1, "5"), _cell(1, 2, "90"),
            _cell(2, 0, "Total income"), _cell(2, 2, "100"),
            _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
            _cell(4, 0, "Profit before tax"), _cell(4, 2, pbt),
        ],
    }


def _head(text: str, page: int, offset: int) -> dict[str, object]:
    return {
        "content": text,
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": len(text)}],
    }


def _note_table(page: int, offset: int, label: str) -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": 50}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, label), _cell(1, 1, "90"),
        ],
    }


# standalone 10-50, consolidated 51-90, standalone 91-120 -- the shape
# `core/basis.py` explicitly does NOT collapse.
_BASIS = {
    "default_basis": "unknown",
    "regions": [
        {"basis": "standalone", "start_page": 10, "end_page": 50},
        {"basis": "consolidated", "start_page": 51, "end_page": 90},
        {"basis": "standalone", "start_page": 91, "end_page": 120},
    ],
}

_OCR = {
    "paragraphs": [
        _head("5. Revenue from operations", 20, 2_000),
        _head("7. Borrowings", 95, 80_000),
    ],
    "tables": [
        # Offsets are spread wider than statements.NOTE_TAIL (20,000) so a
        # note's trailing window cannot swallow the next region's tables.
        _pl_table(20, 1_000),                          # standalone face, region 1
        _note_table(20, 2_100, "Sale of services"),    # note 5, region 1
        _pl_table(60, 50_000),                         # consolidated face
        _pl_table(95, 70_000, pbt="44"),               # standalone face, region 3
        _note_table(95, 80_100, "Term loans"),         # note 7, region 3
    ],
}


def _fog() -> dict[str, object]:
    si = build_statement_index(_OCR, _BASIS)
    ni = build_note_index(_OCR, _BASIS)
    return build_fog(_OCR, si, ni, doc_id="three-regions")


def test_no_line_item_id_appears_twice_anywhere_in_the_graph():
    ids = [
        item["id"]
        for region in _fog()["bases"].values()
        for item in region["line_items"]
    ]
    assert len(set(ids)) == len(ids)


def test_no_statement_id_appears_twice():
    ids = [
        s["id"] for region in _fog()["bases"].values() for s in region["statements"]
    ]
    assert len(set(ids)) == len(ids)


def test_no_contains_edge_is_emitted_twice():
    edges = [
        (e["type"], e["source"], e["target"])
        for region in _fog()["bases"].values()
        for e in region["edges"]
        if e["type"] == "CONTAINS"
    ]
    assert len(set(edges)) == len(edges)


def test_every_contains_edge_points_at_a_node_that_exists():
    fog = _fog()
    for region in fog["bases"].values():
        ids = {i["id"] for i in region["line_items"]}
        targets = {e["target"] for e in region["edges"] if e["type"] == "CONTAINS"}
        assert targets == ids


def test_a_note_in_the_first_region_is_not_thrown_away_by_the_third():
    """Probe 1 of the review: note 5 (page 20) vanished and only note 7
    survived, with nothing recording the loss."""
    region = _fog()["bases"]["standalone"]
    assert {n["ref"] for n in region["notes"]} == {"5", "7"}
    labels = {i["label"] for i in region["line_items"]}
    assert "Sale of services" in labels
    assert "Term loans" in labels


def test_the_diagnostics_say_the_basis_was_assembled_from_two_regions():
    """A merged document must be VISIBLE as merged. Otherwise a reader cannot
    tell a legitimately multi-region filing from a broken one."""
    diag = _fog()["diagnostics"]
    assert diag["standalone"]["regions"] == 2
    assert diag["consolidated"]["regions"] == 1
    assert diag["standalone"]["duplicate_refs"] == 0


def test_the_losing_face_candidate_is_recorded_not_silently_dropped():
    index = build_statement_index(_OCR, _BASIS)
    standalone = [s for s in index["statements"] if s["basis"] == "standalone"]
    assert len(standalone) == 1
    # The page-95 face does not foot (100 - 70 != 44), so the page-20 one wins.
    assert standalone[0]["page"] == 20
    discarded = [s for s in index["discarded"] if s["basis"] == "standalone"]
    assert [s["page"] for s in discarded] == [95]
    assert _fog()["diagnostics"]["standalone"]["statements_discarded"] == 1


def test_a_face_ref_still_resolves_after_the_merge():
    """The failure this guards: `face_refs > 0, refs_resolved == 0` is the
    documented signature of a RITES-style ref-grammar failure. A note index that
    threw its notes away produced exactly that signature and misattributed the
    cause."""
    diag = _fog()["diagnostics"]["standalone"]
    assert diag["face_refs"] == 1
    assert diag["refs_resolved"] == 1
