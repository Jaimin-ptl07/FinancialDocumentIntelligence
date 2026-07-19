from fdi.core.structure import merge_cross_page_tables
from fdi.core.table_grid import canonicalize_table


def _t(cols, rows, page, off, length, cells=None, caption=None, footnotes=None):
    d = {"row_count": rows, "column_count": cols,
         "spans": [{"offset": off, "length": length}],
         "bounding_regions": [{"page_number": page, "polygon": []}],
         "cells": cells or []}
    if caption is not None:
        d["caption"] = caption
    if footnotes is not None:
        d["footnotes"] = footnotes
    return d

def _foot(off): return {"role": "pageFooter", "spans": [{"offset": off, "length": 3}]}
def _body(off): return {"role": None, "spans": [{"offset": off, "length": 5}]}
def _furniture(off, length, role="pageFooter"):
    return {"role": role, "spans": [{"offset": off, "length": length}]}

def _header_cell(row, col, content):
    return {"row_index": row, "column_index": col, "kind": "columnHeader", "content": content}

def _data_cell(row, col, content, page=None):
    c = {"row_index": row, "column_index": col, "kind": "data", "content": content}
    if page is not None:
        c["bounding_regions"] = [{"page_number": page, "polygon": []}]
    return c


def test_clean_continuation_merges() -> None:
    a = _t(3, 2, 1, 0, 50)
    b = _t(3, 4, 2, 52, 40)
    # only a footer between, gap 2, adjacent, equal cols
    out = merge_cross_page_tables([a, b], [_foot(50)])
    assert len(out) == 1 and out[0]["row_count"] == 6 and out[0]["stitched"] is True

def test_column_mismatch_does_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(4, 4, 2, 52, 40)], [])) == 2

def test_body_paragraph_between_does_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(3, 4, 2, 52, 40)], [_body(51)])) == 2

def test_non_adjacent_pages_do_not_merge() -> None:
    assert len(merge_cross_page_tables([_t(3, 2, 1, 0, 50), _t(3, 4, 3, 52, 40)], [])) == 2

def test_three_page_run_merges_to_one() -> None:
    a = _t(2, 2, 1, 0, 10)
    b = _t(2, 2, 2, 12, 10)
    c = _t(2, 2, 3, 24, 10)
    out = merge_cross_page_tables([a, b, c], [])
    assert len(out) == 1 and out[0]["row_count"] == 6

def test_unrelated_tables_pass_through() -> None:
    out = merge_cross_page_tables([_t(2, 2, 1, 0, 10), _t(5, 3, 1, 100, 10)], [])
    assert len(out) == 2 and all("stitched" not in t for t in out)


# --- Phase 5: gap ceiling ---------------------------------------------------

def test_realistic_furniture_gap_now_merges() -> None:
    # mirrors the audit's measured "\n60\nAnnual Report 2023-2024\n" (~29 chars) --
    # under the old gap<=2 rule this could never merge.
    gap_len = 29
    a = _t(3, 2, 1, 0, 50)
    b = _t(3, 4, 2, 50 + gap_len, 40)
    out = merge_cross_page_tables([a, b], [_furniture(50, gap_len)])
    assert len(out) == 1 and out[0]["row_count"] == 6 and out[0]["stitched"] is True

def test_gap_exceeding_sanity_ceiling_does_not_merge() -> None:
    gap_len = 1000
    a = _t(3, 2, 1, 0, 50)
    b = _t(3, 4, 2, 50 + gap_len, 40)
    out = merge_cross_page_tables([a, b], [_furniture(50, gap_len)])
    assert len(out) == 2


# --- Phase 5: header-compatibility guard ------------------------------------

def test_mismatched_resolved_headers_blocks_merge_even_with_furniture_gap() -> None:
    # the Fixed-Assets regression: furniture-only gap, equal column_count, but B
    # carries its own header that differs from A's resolved header -> must NOT merge.
    a_cells = [_header_cell(0, i, h) for i, h in enumerate(["Year", "Q1", "Q2"])]
    b_cells = [_header_cell(0, i, h) for i, h in enumerate(["Yr", "H1", "H2"])]
    a = _t(3, 2, 1, 0, 50, cells=a_cells)
    b = _t(3, 3, 2, 79, 40, cells=b_cells)
    out = merge_cross_page_tables([a, b], [_furniture(50, 29)])
    assert len(out) == 2

def test_headerless_continuation_with_furniture_gap_still_merges() -> None:
    # the common (~83%) real shape: B has no header cells of its own.
    a_cells = [_header_cell(0, i, h) for i, h in enumerate(["Year", "Q1", "Q2"])]
    a = _t(3, 2, 1, 0, 50, cells=a_cells)
    b = _t(3, 4, 2, 79, 40)  # no cells -> headerless
    out = merge_cross_page_tables([a, b], [_furniture(50, 29)])
    assert len(out) == 1 and out[0]["row_count"] == 6

def test_restated_matching_header_still_merges() -> None:
    headers = ["Year", "Q1", "Q2"]
    a_cells = [_header_cell(0, i, h) for i, h in enumerate(headers)]
    b_cells = [_header_cell(0, i, h) for i, h in enumerate(headers)]
    a = _t(3, 2, 1, 0, 50, cells=a_cells)
    b = _t(3, 3, 2, 79, 40, cells=b_cells)
    out = merge_cross_page_tables([a, b], [_furniture(50, 29)])
    assert len(out) == 1


# --- Phase 5: caption/footnotes survive merge -------------------------------

def test_merge_carries_forward_captions_and_footnotes_from_first_table() -> None:
    a = _t(2, 2, 1, 0, 50, caption={"content": "Schedule 1"}, footnotes=[{"content": "Note A"}])
    b = _t(2, 2, 2, 52, 40, caption={"content": "Schedule 1 cont"},
           footnotes=[{"content": "Note B"}])
    out = merge_cross_page_tables([a, b], [])
    assert len(out) == 1
    assert out[0]["caption"] == {"content": "Schedule 1"}
    assert out[0]["footnotes"] == [{"content": "Note A"}]

def test_merge_defaults_caption_and_footnotes_when_a_has_none() -> None:
    a = _t(2, 2, 1, 0, 50)
    b = _t(2, 2, 2, 52, 40, caption={"content": "should not surface"})
    out = merge_cross_page_tables([a, b], [])
    assert len(out) == 1
    assert out[0]["caption"] is None
    assert out[0]["footnotes"] == []


# --- Phase 5: cell_regions/cell_pages survive merge -------------------------

def test_merged_cell_regions_and_cell_pages_reflect_correct_source_page_per_row() -> None:
    a_cells = [_data_cell(r, c, "x", page=1) for r in range(2) for c in range(2)]
    b_cells = [_data_cell(r, c, "y", page=2) for r in range(2) for c in range(2)]
    a = _t(2, 2, 1, 0, 50, cells=a_cells)
    b = _t(2, 2, 2, 52, 40, cells=b_cells)
    out = merge_cross_page_tables([a, b], [])
    assert len(out) == 1
    grid = canonicalize_table(out[0])
    a_rows = 2
    for r in range(a_rows):
        assert grid["cell_pages"][r] == [1, 1]
    for r in range(a_rows, a_rows + 2):
        assert grid["cell_pages"][r] == [2, 2]


# --- Phase 5: source_indices -------------------------------------------------

def test_source_indices_passthrough_tables_carry_their_own_raw_index() -> None:
    out = merge_cross_page_tables([_t(2, 2, 1, 0, 10), _t(5, 3, 1, 100, 10)], [])
    assert out[0]["source_indices"] == [0]
    assert out[1]["source_indices"] == [1]

def test_source_indices_merged_table_carries_all_raw_indices_in_order() -> None:
    a = _t(2, 2, 1, 0, 10)
    b = _t(2, 2, 2, 12, 10)
    c = _t(2, 2, 3, 24, 10)
    out = merge_cross_page_tables([a, b, c], [])
    assert len(out) == 1
    assert out[0]["source_indices"] == [0, 1, 2]
