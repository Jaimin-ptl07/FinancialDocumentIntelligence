"""fdi.core.evidence_context: the pure reads behind Evidence context (S4).
Each must be total -- an inspect of a damaged artifact says "nothing there"."""

import pytest

from fdi.core import evidence_context as ctxv

OCR = {
    "content": "page one textpage two text",
    "pages": [
        {"page_number": 1, "spans": [{"offset": 0, "length": 13}]},
        {"page_number": 2, "spans": [{"offset": 13, "length": 13}]},
    ],
}

TABLE = {
    "bounding_regions": [{"page_number": 7}],
    "grid": {
        "header_row_count": 1, "column_headers": ["Particulars", "FY26", "FY25"],
        "grid": [["Particulars", "FY26", "FY25"], ["Total assets", "16,427", "15,001"]],
    },
}

FOG = {"bases": {"consolidated": {
    "statements": [{"id": "st:consolidated:balance_sheet", "table_index": 0}],
    "notes": [
        {"id": "nt:consolidated:4", "ref": "4", "title": "4. Property, plant and equipment",
         "table_indices": [3]},
        {"id": "nt:consolidated:5", "ref": "5", "title": "5. Investments", "table_indices": []},
    ],
    "line_items": [{"id": "li:st:consolidated:balance_sheet:r0",
                    "parent": "st:consolidated:balance_sheet"}],
    "edges": [
        {"type": "REFERENCES", "source": "li:st:consolidated:balance_sheet:r0",
         "target": "nt:consolidated:4"},
        {"type": "CONTAINS", "source": "st:consolidated:balance_sheet",
         "target": "li:st:consolidated:balance_sheet:r0"},
    ],
}}}


def test_page_text_slices_by_the_pages_own_spans() -> None:
    assert ctxv.page_text(OCR, 2) == "page two text"
    assert ctxv.page_text(OCR, 3) is None
    assert ctxv.page_numbers(OCR) == [1, 2]
    assert ctxv.window_text(OCR, 5, 8) == "one"
    assert ctxv.window_text(OCR, 9, 2) == ""


@pytest.mark.parametrize("bad", [{}, {"content": 5}, {"content": "x", "pages": "no"},
                                 {"content": "x", "pages": [None, {"page_number": True}]}])
def test_damaged_ocr_degrades(bad: dict[str, object]) -> None:
    assert ctxv.page_text(bad, 1) is None
    assert ctxv.window_text(bad, 0, 1) in ("", "x")


def test_cell_carries_its_row_label_and_column_header() -> None:
    assert ctxv.cell(TABLE, 1, 2) == {
        "row": 1, "col": 2, "value": "15,001",
        "row_label": "Total assets", "column_header": "FY25",
    }
    assert ctxv.cell(TABLE, 2, 0) is None and ctxv.cell(TABLE, 0, 5) is None
    assert ctxv.cell({}, 0, 0) is None
    assert ctxv.table_page(TABLE) == 7


def test_fog_lookups() -> None:
    assert [n["ref"] for n in ctxv.find_notes(FOG, "consolidated", title="property")] == ["4"]
    assert [n["ref"] for n in ctxv.find_notes(FOG, "consolidated", ref="5")] == ["5"]
    assert ctxv.find_notes(FOG, "consolidated") == []
    assert ctxv.notes_for_table(FOG, 3) == [("consolidated", "nt:consolidated:4")]
    assert ctxv.references_from(
        FOG, "consolidated", "li:st:consolidated:balance_sheet:r0"
    ) == ["nt:consolidated:4"]
    assert ctxv.references_from(FOG, "standalone", "anything") == []
    assert ctxv.parent_node(FOG, "consolidated", "st:consolidated:balance_sheet") is not None
    assert ctxv.fog_bases(FOG) == ["consolidated"] and ctxv.fog_bases({}) == []


def test_clip_marks_truncation() -> None:
    assert ctxv.clip("abc", 5) == "abc"
    assert ctxv.clip("abcdef", 3) == "abc\n[... truncated]"
