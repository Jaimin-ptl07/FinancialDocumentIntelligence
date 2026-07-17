"""Cross-page table stitching (Microsoft's vertical-merge heuristic). Pure — no I/O."""
from collections.abc import Mapping
from typing import cast

_FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}


def _span_start(t: Mapping[str, object]) -> int:
    spans = cast(list[dict[str, int]], t.get("spans", []))
    return min((s["offset"] for s in spans), default=0)


def _span_end(t: Mapping[str, object]) -> int:
    spans = cast(list[dict[str, int]], t.get("spans", []))
    return max((s["offset"] + s["length"] for s in spans), default=0)


def _pages(t: Mapping[str, object]) -> list[int]:
    regs = cast(list[dict[str, object]], t.get("bounding_regions", []))
    return [int(cast(int, r["page_number"])) for r in regs]


def _last_page(t: Mapping[str, object]) -> int | None:
    p = _pages(t)
    return max(p) if p else None


def _first_page(t: Mapping[str, object]) -> int | None:
    p = _pages(t)
    return min(p) if p else None


def _only_furniture_between(a: Mapping[str, object], b: Mapping[str, object],
                            paragraphs: list[Mapping[str, object]]) -> bool:
    lo, hi = _span_end(a), _span_start(b)
    for p in paragraphs:
        for s in cast(list[dict[str, int]], p.get("spans", [])):
            if lo <= s["offset"] < hi and p.get("role") not in _FURNITURE:
                return False
    return True


def _can_merge(a: Mapping[str, object], b: Mapping[str, object],
               paragraphs: list[Mapping[str, object]]) -> bool:
    la, fb = _last_page(a), _first_page(b)
    if la is None or fb is None or fb != la + 1:          # adjacent pages only
        return False
    if a.get("column_count") != b.get("column_count"):    # equal columns
        return False
    gap = _span_start(b) - _span_end(a)                   # reading-order gap <= 2
    if not 0 <= gap <= 2:
        return False
    return _only_furniture_between(a, b, paragraphs)


def _merge_two(a: Mapping[str, object], b: Mapping[str, object]) -> dict[str, object]:
    a_rows = int(cast(int, a["row_count"]))
    b_rows = int(cast(int, b["row_count"]))
    a_cells = list(cast(list[dict[str, object]], a["cells"]))
    b_cells = [{**c, "row_index": int(cast(int, c["row_index"])) + a_rows}
               for c in cast(list[dict[str, object]], b["cells"])]
    return {
        "row_count": a_rows + b_rows,
        "column_count": a["column_count"],
        "spans": list(cast(list[object], a.get("spans", [])))
                 + list(cast(list[object], b.get("spans", []))),
        "bounding_regions": list(cast(list[object], a.get("bounding_regions", [])))
                            + list(cast(list[object], b.get("bounding_regions", []))),
        "cells": a_cells + b_cells,
        "stitched": True,
    }


def merge_cross_page_tables(tables: list[Mapping[str, object]],
                            paragraphs: list[Mapping[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(tables, key=_span_start)
    out: list[dict[str, object]] = []
    i = 0
    while i < len(ordered):
        cur: dict[str, object] = {**ordered[i]}
        j = i + 1
        while j < len(ordered) and _can_merge(cur, ordered[j], paragraphs):
            cur = _merge_two(cur, ordered[j])
            j += 1
        out.append(cur)
        i = j
    return out
