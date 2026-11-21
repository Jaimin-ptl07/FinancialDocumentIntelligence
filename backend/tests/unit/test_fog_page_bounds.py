"""The statement-page bound is PER REGION, and every way it loses content says
so.

The bound exists because front matter restarts its own `1.`/`2.` numbering and
`note_windows` keeps the first occurrence by offset, so an unbounded scan binds
the face's printed `1` to the Board's Report. But `find_statement_pages` returns
ONE span for the whole filing, and intersecting that single span with every
basis region is how the fix for that became a worse bug: the standalone face's
span decided where the CONSOLIDATED region ended.

Every test here reproduces a silent loss -- one that leaves the diagnostics
reading exactly as they would for an issuer who filed nothing.
"""

from fdi.core.fog_build import build_fog, build_note_index, build_statement_index


def _head(text: str, page: int, offset: int) -> dict[str, object]:
    return {
        "content": text,
        "role": "sectionHeading",
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": len(text)}],
    }


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


def _pl_table(page: int, offset: int, note_ref: str = "10") -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": offset, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
            _cell(1, 0, "Revenue from operations"), _cell(1, 1, note_ref),
            _cell(1, 2, "90"),
            _cell(2, 0, "Total income"), _cell(2, 2, "100"),
            _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
            _cell(4, 0, "Profit before tax"), _cell(4, 2, "30"),
        ],
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


# ---------------------------------------------------------------------------
# N1 -- probe A: an AGM notice printed BEFORE the statements
# ---------------------------------------------------------------------------

# A perfectly ordinary Indian annual report: notice of AGM at the front, then
# the standalone statement set, then the consolidated one.
_TWO_BASIS_OCR: dict[str, object] = {
    "paragraphs": [
        _head("Notice of Annual General Meeting", 8, 100),
        _head("Standalone Balance Sheet", 150, 10_000),
        _head("10. Revenue from operations", 160, 11_000),
        _head("Consolidated Balance Sheet", 251, 100_000),
        _head("10. Revenue from operations", 260, 101_000),
    ],
    "tables": [
        _pl_table(151, 10_100),
        _note_table(160, 11_100, "Sale of services (standalone)"),
        _pl_table(252, 100_100),
        _note_table(260, 101_100, "Sale of services (consolidated)"),
    ],
}
_TWO_BASIS = {
    "default_basis": "unknown",
    "regions": [
        {"basis": "standalone", "start_page": 150, "end_page": 250},
        {"basis": "consolidated", "start_page": 251, "end_page": 380},
    ],
}


def _two_basis_fog() -> dict[str, object]:
    si = build_statement_index(_TWO_BASIS_OCR, _TWO_BASIS)
    ni = build_note_index(_TWO_BASIS_OCR, _TWO_BASIS)
    return build_fog(_TWO_BASIS_OCR, si, ni, doc_id="two-basis")


def test_one_basis_region_is_never_emptied_by_another_regions_span():
    """Probe A of the fix-wave re-review, the Critical.

    `find_statement_pages` finds no terminator after the first face (the notice
    is on page 8, BEFORE it), so the document-wide span pinned `page_hi` at
    `150 + 90 = 240` -- and the consolidated region, pages 251-380, reduced to
    `hi < lo`. Zero statements, zero notes, zero line items for the PRIMARY
    basis, and nothing in the artifact naming the cause."""
    fog = _two_basis_fog()
    consolidated = fog["bases"]["consolidated"]
    assert [s["page"] for s in consolidated["statements"]] == [252]
    assert [n["ref"] for n in consolidated["notes"]] == ["10"]
    labels = {i["label"] for i in consolidated["line_items"]}
    assert "Sale of services (consolidated)" in labels
    # The standalone set is untouched by the consolidated region in the same way.
    assert [s["page"] for s in fog["bases"]["standalone"]["statements"]] == [151]


def test_a_regions_span_is_derived_from_its_own_pages_not_the_documents():
    """The bound each region got must lie inside that region. A span imported
    from a different region is what made `hi < lo` reachable at all."""
    index = build_note_index(_TWO_BASIS_OCR, _TWO_BASIS)
    for basis, raw_lo, raw_hi in (("standalone", 150, 250), ("consolidated", 251, 380)):
        region = index["bases"][basis]["regions"][0]
        assert region["raw_page_lo"] == raw_lo
        assert region["raw_page_hi"] == raw_hi
        assert raw_lo <= region["page_lo"] <= region["page_hi"] <= raw_hi


def test_the_front_matter_bound_still_holds_within_a_region():
    """Per-region must not mean unbounded. The Board's Report restarts `1.`/`2.`
    inside the same basis region as the statements, and `note_windows` keeps the
    first occurrence by offset -- so without a lower bound the face's printed
    `1` still binds to 'FINANCIAL RESULTS' on page 12."""
    ocr = {
        "paragraphs": [
            _head("1. FINANCIAL RESULTS", 12, 100),
            _head("Consolidated Balance Sheet", 148, 800),
            _head("1. Corporate information", 150, 1_000),
        ],
        "tables": [],
    }
    basis = {
        "default_basis": "unknown",
        "regions": [{"basis": "consolidated", "start_page": 1, "end_page": 400}],
    }
    notes = build_note_index(ocr, basis)["bases"]["consolidated"]["notes"]
    assert [(n["ref"], n["page"]) for n in notes] == [("1", 150)]


def test_a_terminator_inside_the_region_still_closes_the_span():
    """The control case: when the notice IS printed after the statements it must
    still end the span, and the artifact must say that is what closed it."""
    ocr = {
        "paragraphs": [
            _head("Consolidated Balance Sheet", 150, 1_000),
            _head("10. Revenue from operations", 160, 2_000),
            _head("Notice of Annual General Meeting", 200, 9_000),
            _head("1. Ordinary business", 201, 9_100),
        ],
        "tables": [],
    }
    basis = {
        "default_basis": "unknown",
        "regions": [{"basis": "consolidated", "start_page": 1, "end_page": 400}],
    }
    region = build_note_index(ocr, basis)["bases"]["consolidated"]
    assert [n["ref"] for n in region["notes"]] == ["10"]
    assert region["regions"][0]["bound"] == "terminator"
    assert region["regions"][0]["page_hi"] == 199
    # Everything past a real terminator is the notice's own numbering, which is
    # what the bound exists to exclude -- it is not loss and must not be counted.
    assert region["regions"][0]["refs_beyond_bound"] == []


# ---------------------------------------------------------------------------
# N1 -- a region that genuinely carries no statements
# ---------------------------------------------------------------------------


def test_a_region_carrying_no_statements_says_so_instead_of_reading_as_a_bug():
    """`absent != missed` for the region itself. A basis region with no face
    inside it produces the same zeros as a region the bound emptied, so the
    artifact has to name which one happened."""
    ocr = {
        "paragraphs": [_head("Standalone Balance Sheet", 20, 1_000)],
        "tables": [_pl_table(21, 1_100, note_ref="")],
    }
    basis = {
        "default_basis": "unknown",
        "regions": [
            {"basis": "standalone", "start_page": 10, "end_page": 50},
            {"basis": "consolidated", "start_page": 51, "end_page": 90},
        ],
    }
    si = build_statement_index(ocr, basis)
    consolidated = next(r for r in si["regions"] if r["basis"] == "consolidated")
    assert consolidated["statements"] == 0
    # No face title inside it, so no bound was applied and the raw span survives.
    assert consolidated["bound"] == "none"
    assert (consolidated["page_lo"], consolidated["page_hi"]) == (51, 90)

    diag = build_fog(ocr, si, build_note_index(ocr, basis), doc_id="d")["diagnostics"]
    assert diag["consolidated"]["regions_without_statements"] == 1
    assert diag["consolidated"]["regions"] == 1
    assert diag["standalone"]["regions_without_statements"] == 0


# ---------------------------------------------------------------------------
# N2 -- probe F: a note printed past the 90-page heuristic
# ---------------------------------------------------------------------------

_TRUNCATED_OCR: dict[str, object] = {
    "paragraphs": [
        _head("Consolidated Balance Sheet", 200, 1_000),
        _head("12. Revenue from operations", 210, 2_000),
        # 100 pages past the face, and no terminator heading anywhere: the span
        # falls back to 200 + 90 = 290 and this note is cut off.
        _head("31. Related party transactions", 300, 900_000),
    ],
    "tables": [
        _pl_table(201, 1_100, note_ref="31"),
        _note_table(300, 900_100, "Key management remuneration"),
    ],
}
_WHOLE_DOCUMENT = {
    "default_basis": "unknown",
    "regions": [{"basis": "consolidated", "start_page": 1, "end_page": 400}],
}


def _truncated_fog() -> dict[str, object]:
    si = build_statement_index(_TRUNCATED_OCR, _WHOLE_DOCUMENT)
    ni = build_note_index(_TRUNCATED_OCR, _WHOLE_DOCUMENT)
    return build_fog(_TRUNCATED_OCR, si, ni, doc_id="truncated")


def test_a_bound_truncation_is_distinguishable_from_a_grammar_failure():
    """Probe F of the fix-wave re-review.

    `face_refs > 0, refs_resolved == 0` is, verbatim, the signature `build_fog`
    documents as 'the reference grammar failed to parse (the RITES bug)'. Here
    the grammar parsed perfectly and the PAGE BOUND dropped the note. Two
    different causes, one reading -- until `refs_beyond_bound` separates them."""
    diag = _truncated_fog()["diagnostics"]["consolidated"]
    assert diag["face_refs"] == 1
    assert diag["refs_resolved"] == 0
    assert diag["refs_beyond_bound"] == 1


def test_a_real_grammar_failure_reports_no_refs_beyond_the_bound():
    """The other side of the same discrimination: RITES's ref grammar failed
    with every note sitting comfortably inside the span. If `refs_beyond_bound`
    were non-zero here it would separate nothing."""
    ocr = {
        "paragraphs": [
            _head("Consolidated Balance Sheet", 200, 1_000),
            # Not a note heading in any grammar -- nothing is indexed.
            _head("REVENUE FROM OPERATIONS", 210, 2_000),
        ],
        "tables": [_pl_table(201, 1_100, note_ref="31")],
    }
    si = build_statement_index(ocr, _WHOLE_DOCUMENT)
    ni = build_note_index(ocr, _WHOLE_DOCUMENT)
    diag = build_fog(ocr, si, ni, doc_id="grammar")["diagnostics"]["consolidated"]
    assert diag["face_refs"] == 1
    assert diag["refs_resolved"] == 0
    assert diag["refs_beyond_bound"] == 0


def test_the_truncated_region_names_the_refs_it_excluded_and_its_raw_span():
    """A count in `diagnostics` says a truncation happened; the region body says
    WHAT was lost and how much span it was lost from, which is what makes the
    90-page default measurable rather than a guess."""
    region = build_note_index(_TRUNCATED_OCR, _WHOLE_DOCUMENT)["bases"]["consolidated"]
    span = region["regions"][0]
    assert span["bound"] == "default_span"
    assert (span["page_lo"], span["page_hi"]) == (200, 290)
    assert (span["raw_page_lo"], span["raw_page_hi"]) == (1, 400)
    assert span["refs_beyond_bound"] == ["31"]


def test_the_bodies_are_deterministic_with_the_new_region_records():
    """`regions` reaches an artifact body, so its ordering must be fixed by the
    basis artifact's document order, not by a set or a dict."""
    from fdi.core.serialization import canonical_json_bytes

    for build in (build_note_index, build_statement_index):
        first = build(_TWO_BASIS_OCR, _TWO_BASIS)
        assert canonical_json_bytes(first) == canonical_json_bytes(
            build(_TWO_BASIS_OCR, _TWO_BASIS)
        )
    order = [r["basis"] for r in build_statement_index(_TWO_BASIS_OCR, _TWO_BASIS)
             ["regions"]]
    assert order == ["standalone", "consolidated"]


def test_malformed_input_still_never_raises_through_the_new_bound():
    """`fdi.core` is TOTAL. The per-region bound scans paragraphs once per
    region, so a malformed paragraph list gets more chances to raise, not
    fewer."""
    for ocr in (
        {},
        {"paragraphs": "nope"},
        {"paragraphs": [None, 5, {"role": "sectionHeading"}]},
        {"paragraphs": [{"role": "sectionHeading",
                         "content": "Balance Sheet",
                         "bounding_regions": [{"page_number": "x"}]}]},
    ):
        for basis in ({}, {"regions": "x"}, _TWO_BASIS):
            assert isinstance(build_note_index(ocr, basis), dict)
            assert isinstance(build_statement_index(ocr, basis), dict)
