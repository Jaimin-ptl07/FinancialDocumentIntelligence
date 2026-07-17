from fdi.core.structure import merge_cross_page_tables


def _t(cols, rows, page, off, length, cells=None):
    return {"row_count": rows, "column_count": cols,
            "spans": [{"offset": off, "length": length}],
            "bounding_regions": [{"page_number": page, "polygon": []}],
            "cells": cells or []}

def _foot(off): return {"role": "pageFooter", "spans": [{"offset": off, "length": 3}]}
def _body(off): return {"role": None, "spans": [{"offset": off, "length": 5}]}


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
