"""Notes are addressed by OFFSET WINDOW, not page: TCS p301 holds note 13(a) at
offset 897,095 and 13(b) at 897,486, so a page fetch hands both to whichever
concept asks first."""

from fdi.core.fog_build import build_note_index


def _head(text: str, page: int, offset: int) -> dict[str, object]:
    return {
        "content": text,
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": len(text)}],
    }


def _table(page: int, offset: int) -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": 50}],
        "cells": [{"row_index": 0, "column_index": 0, "content": "Land"}],
    }


_BASIS = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 100, "end_page": 200}],
}

_OCR = {
    "paragraphs": [
        _head("10. Revenue from operations", 150, 1000),
        _head("13. Other expenses", 160, 2000),
        _head("(a) Facility costs", 160, 2100),
        _head("(b) Travel", 161, 2400),
    ],
    "tables": [_table(150, 1100), _table(160, 2150), _table(161, 2450)],
}


def test_flat_and_sub_notes_are_both_indexed():
    index = build_note_index(_OCR, _BASIS)
    refs = {n["ref"] for n in index["bases"]["consolidated"]["notes"]}
    assert {"10", "13", "13(a)", "13(b)"} <= refs


def test_the_note_id_is_basis_scoped():
    """A report with both bases has two 'note 8'; unscoped ids collide."""
    index = build_note_index(_OCR, _BASIS)
    ids = {n["id"] for n in index["bases"]["consolidated"]["notes"]}
    assert "nt:consolidated:13(a)" in ids


def test_a_note_carries_its_offset_window_not_just_a_page():
    index = build_note_index(_OCR, _BASIS)
    note = next(
        n for n in index["bases"]["consolidated"]["notes"] if n["ref"] == "13(a)"
    )
    assert note["start_offset"] == 2100
    assert note["end_offset"] == 2400


def test_tables_are_bound_to_the_note_whose_window_contains_them():
    index = build_note_index(_OCR, _BASIS)
    notes = {n["ref"]: n for n in index["bases"]["consolidated"]["notes"]}
    assert notes["13(a)"]["table_indices"] == [1]
    assert notes["13(b)"]["table_indices"] == [2]


def test_the_title_is_kept_it_is_how_uncited_notes_are_found():
    """A note the face never cites is reachable only by its own heading."""
    index = build_note_index(_OCR, _BASIS)
    note = next(
        n for n in index["bases"]["consolidated"]["notes"] if n["ref"] == "10"
    )
    assert note["title"] == "Revenue from operations"


def test_dotted_references_are_accepted():
    """RITES numbers its notes 2.01 / 2.08.2; the old regex parsed 0 of 30."""
    ocr = {
        "paragraphs": [_head("2.17 Revenue From Operations", 150, 1000)],
        "tables": [],
    }
    index = build_note_index(ocr, _BASIS)
    refs = {n["ref"] for n in index["bases"]["consolidated"]["notes"]}
    assert "2.17" in refs


def test_each_basis_gets_its_own_note_namespace():
    basis = {
        "default_basis": "unknown",
        "regions": [
            {"basis": "standalone", "start_page": 100, "end_page": 155},
            {"basis": "consolidated", "start_page": 156, "end_page": 200},
        ],
    }
    index = build_note_index(_OCR, basis)
    assert set(index["bases"]) == {"standalone", "consolidated"}
    standalone_refs = {n["ref"] for n in index["bases"]["standalone"]["notes"]}
    assert "10" in standalone_refs
    assert "13" not in standalone_refs


def test_front_matter_numbering_cannot_hijack_a_note_reference():
    """The Board's Report restarts its own `1.`/`2.` numbering and precedes the
    statements, and `note_windows` keeps the FIRST occurrence by offset. Without
    a statement-page bound the face's printed `1` binds to 'FINANCIAL RESULTS'
    on page 12 -- and the FOG reports that as a healthy resolution."""
    whole_document = {
        "default_basis": "unknown",
        "regions": [{"basis": "consolidated", "start_page": 1, "end_page": 400}],
    }
    ocr = {
        "paragraphs": [
            _head("1. FINANCIAL RESULTS", 12, 100),
            _head("2. DIVIDEND", 13, 200),
            _head("Consolidated Balance Sheet", 148, 800),
            _head("1. Corporate information", 150, 1000),
            _head("2. Significant accounting policies", 151, 1100),
        ],
        "tables": [],
    }
    notes = {n["ref"]: n for n in build_note_index(ocr, whole_document)
             ["bases"]["consolidated"]["notes"]}
    assert notes["1"]["page"] == 150
    assert notes["1"]["title"] == "Corporate information"
    assert notes["2"]["page"] == 151


def test_two_regions_of_one_basis_are_merged_not_overwritten():
    """`core/basis.py` collapses only CONSECUTIVE same-basis anchors, so
    [standalone, consolidated, standalone] is a real shape. Assigning per region
    threw the first region's notes away silently."""
    basis = {
        "default_basis": "unknown",
        "regions": [
            {"basis": "standalone", "start_page": 100, "end_page": 155},
            {"basis": "consolidated", "start_page": 156, "end_page": 180},
            {"basis": "standalone", "start_page": 181, "end_page": 200},
        ],
    }
    ocr = {
        "paragraphs": [
            _head("5. Trade receivables", 150, 1000),
            _head("7. Borrowings", 190, 5000),
        ],
        "tables": [],
    }
    region = build_note_index(ocr, basis)["bases"]["standalone"]
    assert {n["ref"] for n in region["notes"]} == {"5", "7"}
    # The merge is VISIBLE: a reader can tell a merged basis from a single one.
    assert len(region["regions"]) == 2
    assert region["page_lo"] == 100
    assert region["page_hi"] == 200


def test_a_ref_a_second_region_re_uses_is_recorded_not_silently_dropped():
    basis = {
        "default_basis": "unknown",
        "regions": [
            {"basis": "standalone", "start_page": 100, "end_page": 155},
            {"basis": "consolidated", "start_page": 156, "end_page": 180},
            {"basis": "standalone", "start_page": 181, "end_page": 200},
        ],
    }
    ocr = {
        "paragraphs": [
            _head("5. Trade receivables", 150, 1000),
            _head("5. Trade receivables", 190, 5000),
        ],
        "tables": [],
    }
    region = build_note_index(ocr, basis)["bases"]["standalone"]
    assert [n["page"] for n in region["notes"]] == [150]
    assert region["duplicate_refs"] == ["5"]


def test_output_is_deterministic():
    """`==` cannot see set or dict ordering leaking into the body. The artifact
    is content-ADDRESSED, so assert the canonical BYTES and an externally-fixed
    order."""
    from fdi.core.serialization import canonical_json_bytes

    first = build_note_index(_OCR, _BASIS)
    assert canonical_json_bytes(first) == canonical_json_bytes(
        build_note_index(_OCR, _BASIS)
    )
    assert [n["ref"] for n in first["bases"]["consolidated"]["notes"]] == [
        "10", "13", "13(a)", "13(b)",
    ]


def test_malformed_input_never_raises():
    assert build_note_index({}, {})["bases"] == {"unknown": {
        "notes": [], "page_lo": 1, "page_hi": 100_000,
        "regions": [{"page_lo": 1, "page_hi": 100_000}], "duplicate_refs": []}}


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("note_index")
    assert stage.deps == ("ocr", "structure", "basis")
    assert stage.version == 2
