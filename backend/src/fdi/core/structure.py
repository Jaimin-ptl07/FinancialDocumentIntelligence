"""Cross-page table stitching (Microsoft's vertical-merge heuristic). Pure — no I/O."""
from collections.abc import Mapping
from typing import cast

from fdi.core.table_grid import canonicalize_table

_FURNITURE = {"pageHeader", "pageFooter", "pageNumber"}
# Sanity backstop, not the primary discriminator -- _only_furniture_between's
# per-paragraph role scan is the real gate. The one directly measured real
# furniture instance (a running header + page number) is ~28-29 chars; 300 is
# roughly 10x that to also cover multi-line furniture without plausibly
# spanning real, unrelated body content between two unrelated tables.
_MAX_FURNITURE_GAP_CHARS = 300


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


def _headers_compatible(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
    """True iff merging a<-b would not splice together two tables whose OWN
    resolved column headers disagree. A genuine continuation's second slice
    normally has no header cells of its own (Azure does not restate the
    header across a table split) -- that degenerate all-blank case is
    compatible by default. A table that DOES carry its own header (e.g. a
    same-shaped-by-column-count but semantically different schedule, such as
    a prior-year comparative table) must match `a`'s resolved header exactly."""
    b_grid = canonicalize_table(b)
    b_headers = cast(list[str], b_grid["column_headers"])
    if b_grid["header_row_count"] == 0 or not any(h.strip() for h in b_headers):
        return True
    a_grid = canonicalize_table(a)
    return cast(list[str], a_grid["column_headers"]) == b_headers


def _can_merge(a: Mapping[str, object], b: Mapping[str, object],
               paragraphs: list[Mapping[str, object]]) -> bool:
    la, fb = _last_page(a), _first_page(b)
    if la is None or fb is None or fb != la + 1:          # adjacent pages only
        return False
    if a.get("column_count") != b.get("column_count"):    # equal columns
        return False
    gap = _span_start(b) - _span_end(a)                   # reading-order gap
    if not 0 <= gap <= _MAX_FURNITURE_GAP_CHARS:
        return False
    if not _only_furniture_between(a, b, paragraphs):
        return False
    return _headers_compatible(a, b)


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
        "caption": a.get("caption"),
        "footnotes": a.get("footnotes", []),
    }


def merge_cross_page_tables(tables: list[Mapping[str, object]],
                            paragraphs: list[Mapping[str, object]]) -> list[dict[str, object]]:
    # `source_indices` bridges raw ocr["tables"] positions to structure["tables"]
    # positions: a passthrough table keeps its own raw index; a merged table
    # lists every raw index that fed it, in reading order. Captured via
    # enumerate() *before* the reading-order sort below.
    indexed = sorted(enumerate(tables), key=lambda kv: _span_start(kv[1]))
    out: list[dict[str, object]] = []
    i = 0
    while i < len(indexed):
        idx0, first = indexed[i]
        cur: dict[str, object] = {**first}
        source_indices = [idx0]
        j = i + 1
        while j < len(indexed) and _can_merge(cur, indexed[j][1], paragraphs):
            idx_j, tbl_j = indexed[j]
            cur = _merge_two(cur, tbl_j)
            source_indices.append(idx_j)
            j += 1
        cur["source_indices"] = source_indices
        out.append(cur)
        i = j
    return out
