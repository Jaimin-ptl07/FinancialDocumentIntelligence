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

_BASIS: dict[str, object] = {
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


def test_a_nil_row_is_a_line_item_with_no_figure_not_a_skip():
    """SUPERSEDES `test_a_dash_row_is_skipped_it_carries_no_figure`.

    The issuer PRINTED `Communication | - | -`: it disclosed the line and
    asserted it is worth nothing. Dropping the row makes a disclosed nil
    indistinguishable from a line that was never printed -- the `absent !=
    missed` violation this artifact exists to prevent -- so it becomes a
    LineItem carrying `current=None`. `line_item_body` keeps the `None` rather
    than collapsing it to 0.0, and `facts_from_fog` ignores a value-less item,
    so nothing downstream reads the nil as a figure."""
    items, _ = _build()
    nil = next(i for i in items if i.label == "Communication")
    assert nil.current is None
    assert nil.prior is None
    assert nil.source_row == 3


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
    # `row_index` counts VALUED rows (the id hashes on it); `source_row` is the
    # raw grid row, and only that can address a cell in `core/provenance.py`.
    assert facility.row_index == 0
    assert facility.source_row == 1


def test_a_bare_year_header_does_not_become_a_line_item():
    """`parse_amount("2025-26")` is None, so the fixtures never noticed -- but
    `parse_amount("2025")` is 2025.0, and `Particulars | 2025 | 2024` minted a
    LineItem labelled 'Particulars' worth 2025. A year entering the graph as an
    amount is the class of bug the comma fix already paid for."""
    table = {
        "bounding_regions": [{"page_number": 301}],
        "spans": [{"offset": 2150, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025"), _cell(0, 2, "2024"),
            _cell(1, 0, "Facility expenses"), _cell(1, 1, "2,966"), _cell(1, 2, "2,700"),
        ],
    }
    ocr = {"paragraphs": [_HEAD], "tables": [table]}
    items, _ = note_line_items(ocr, build_note_index(ocr, _BASIS), basis="consolidated")
    assert [i.label for i in items] == ["Facility expenses"]


def test_a_repeated_label_under_two_blocks_keeps_its_caption():
    """A PPE note prints `Additions` once under 'Gross block' and once under
    'Accumulated depreciation'. Without the caption they are two
    indistinguishable nodes and the note total cannot arbitrate its own
    components -- which is the stated reason note rows exist at all."""
    ppe = {
        "bounding_regions": [{"page_number": 305}],
        "spans": [{"offset": 2150, "length": 200}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, "Gross block"),
            _cell(2, 0, "Additions"), _cell(2, 1, "120"),
            _cell(3, 0, "Accumulated depreciation"),
            _cell(4, 0, "Additions"), _cell(4, 1, "45"),
        ],
    }
    ocr = {"paragraphs": [_HEAD], "tables": [ppe]}
    items, _ = note_line_items(ocr, build_note_index(ocr, _BASIS), basis="consolidated")
    additions = [i for i in items if i.label == "Additions"]
    assert [i.caption for i in additions] == ["Gross block", "Accumulated depreciation"]
    assert len({i.id for i in additions}) == 2


def test_a_nil_row_does_not_become_the_caption_for_the_rows_beneath_it():
    """Probe B of the fix-wave re-review, and the worse half of the nil-row bug.

    Mirroring `parse_face_table`'s "no parseable amount => caption" rule made
    `Communication | - | -` the running caption, so `Deletions` was recorded
    under "Communication" instead of "Gross block". Movement schedules print `-`
    constantly, so this is not a corner case, and a wrong caption is worse than
    an empty one: it is an affirmatively false claim about which block a figure
    belongs to, on the field Phase 3's mapper arbitrates with.

    A caption is STRUCTURALLY a heading -- no amount cells at all. A nil row has
    amount cells the issuer filled in with an explicit nil marker."""
    ppe = {
        "bounding_regions": [{"page_number": 305}],
        "spans": [{"offset": 2150, "length": 300}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"), _cell(0, 2, "2024-25"),
            _cell(1, 0, "Gross block"),
            _cell(2, 0, "Additions"), _cell(2, 1, "120"),
            _cell(3, 0, "Communication"), _cell(3, 1, "-"), _cell(3, 2, "-"),
            _cell(4, 0, "Deletions"), _cell(4, 1, "30"),
            _cell(5, 0, "Accumulated depreciation"),
            _cell(6, 0, "Additions"), _cell(6, 1, "45"),
        ],
    }
    ocr = {"paragraphs": [_HEAD], "tables": [ppe]}
    items, _ = note_line_items(ocr, build_note_index(ocr, _BASIS), basis="consolidated")
    captions = {i.label: i.caption for i in items}
    assert captions["Deletions"] == "Gross block"
    assert captions["Communication"] == "Gross block"
    # A row that really IS structurally a heading must still caption its block.
    assert [i.caption for i in items if i.label == "Additions"] == [
        "Gross block", "Accumulated depreciation",
    ]


def test_a_caption_row_with_blank_amount_cells_is_still_a_caption():
    """An OCR grid usually emits a caption row's amount cells as EMPTY strings.
    Empty is not a nil marker: it carries no assertion, so the row is a heading.
    Getting this backwards would turn every block heading into a value-less
    LineItem and lose the caption entirely."""
    table = {
        "bounding_regions": [{"page_number": 305}],
        "spans": [{"offset": 2150, "length": 200}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "2025-26"),
            _cell(1, 0, "Gross block"), _cell(1, 1, "   "),
            _cell(2, 0, "Additions"), _cell(2, 1, "120"),
        ],
    }
    ocr = {"paragraphs": [_HEAD], "tables": [table]}
    items, _ = note_line_items(ocr, build_note_index(ocr, _BASIS), basis="consolidated")
    assert [i.label for i in items] == ["Additions"]
    assert items[0].caption == "Gross block"


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
